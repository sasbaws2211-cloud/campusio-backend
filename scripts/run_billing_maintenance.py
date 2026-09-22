#!/usr/bin/env python3
"""
Management Script: Daily Platform Billing Maintenance

Usage:
    python scripts/run_billing_maintenance.py                  # Run all three steps
    python scripts/run_billing_maintenance.py --skip-reminders  # Only late fees + suspension
    python scripts/run_billing_maintenance.py --dry-run         # Log what would run, apply nothing

This is the piece that was missing for late fees, overdue suspension, and
payment reminders: there is no in-process task scheduler anywhere in this
codebase (see comments in services/subscription_suspension_service.py and
routers/fees.py) — those features are only reachable by hitting their API
endpoints. This script drives the same service methods the endpoints use,
directly against the database, so it can be invoked by an external
scheduler (cron, systemd timer, Windows Task Scheduler, a cloud scheduler
job) on whatever cadence makes sense (daily is typical for billing).

Order matters: proration runs first (so growth is billed before late fees
are assessed on the resulting total), then late fees, then the suspension
check — so a subscription that just crossed the suspension threshold is
suspended with both proration and late fees already reflected in its
outstanding balance.

Example crontab entry (run once a day at 02:00 server time):
    0 2 * * * cd /path/to/campusio_backend && venv/bin/python scripts/run_billing_maintenance.py >> logs/billing_maintenance.log 2>&1
"""
import asyncio
import argparse
import logging
from pathlib import Path
import sys

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from config import get_settings

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

settings = get_settings()


async def get_database_session():
    """Create async database session (standalone, outside FastAPI's DI)."""
    database_url = settings.database_url
    if database_url.startswith("postgresql://"):
        database_url = database_url.replace("postgresql://", "postgresql+asyncpg://", 1)

    engine = create_async_engine(database_url, echo=False)
    async_session = sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    return async_session, engine


async def run_proration(session: AsyncSession, dry_run: bool) -> dict:
    from services.proration_service import ProrationService

    if dry_run:
        logger.info("[DRY RUN] Skipping proration check")
        return {"success": True, "dry_run": True}

    result = await ProrationService().check_and_apply_proration(session, school_id=None)
    if result.get("success"):
        logger.info(f"Proration: {result['message']} (GHS {result.get('total_charged', 0)} charged)")
    else:
        logger.error(f"Proration step failed: {result.get('error')}")
    return result


async def run_late_fees(session: AsyncSession, dry_run: bool) -> dict:
    from services.late_fee_service import LateFeeService

    overdue = await LateFeeService().get_overdue_subscriptions(session, school_id=None)
    if dry_run:
        logger.info(f"[DRY RUN] {len(overdue)} subscriptions currently overdue with a balance")
        return {"success": True, "dry_run": True, "candidates": len(overdue)}

    result = await LateFeeService().check_and_apply_late_fees(session, school_id=None)
    if result.get("success"):
        logger.info(f"Late fees: {result['message']}")
    else:
        logger.error(f"Late fees step failed: {result.get('error')}")
    return result


async def run_suspension_check(session: AsyncSession, dry_run: bool) -> dict:
    from services.subscription_suspension_service import SubscriptionSuspensionService

    if dry_run:
        logger.info("[DRY RUN] Skipping suspension check (would suspend eligible overdue subscriptions)")
        return {"success": True, "dry_run": True}

    result = await SubscriptionSuspensionService().check_and_suspend_overdue(session, school_id=None)
    if result.get("success"):
        logger.info(f"Suspension check: {result['message']}")
    else:
        logger.error(f"Suspension step failed: {result.get('error')}")
    return result


async def run_reminders(session: AsyncSession, dry_run: bool) -> dict:
    from services.payment_reminder_service import PaymentReminderService

    if dry_run:
        logger.info("[DRY RUN] Skipping reminder send")
        return {"success": True, "dry_run": True}

    result = await PaymentReminderService().send_pending_reminders(session, school_id=None)
    if result.get("success"):
        logger.info(f"Reminders: {result['message']}")
    else:
        logger.error(f"Reminders step failed: {result.get('error')}")
    return result


async def main():
    parser = argparse.ArgumentParser(description="Run daily platform billing maintenance")
    parser.add_argument("--skip-proration", action="store_true", help="Skip mid-term student-growth proration")
    parser.add_argument("--skip-late-fees", action="store_true", help="Skip late fee application")
    parser.add_argument("--skip-suspension", action="store_true", help="Skip overdue suspension check")
    parser.add_argument("--skip-reminders", action="store_true", help="Skip payment reminders")
    parser.add_argument("--dry-run", action="store_true", help="Log what would run without applying anything")
    args = parser.parse_args()

    async_session, engine = await get_database_session()
    exit_code = 0

    try:
        async with async_session() as session:
            if not args.skip_proration:
                result = await run_proration(session, args.dry_run)
                if not result.get("success"):
                    exit_code = 1

            if not args.skip_late_fees:
                result = await run_late_fees(session, args.dry_run)
                if not result.get("success"):
                    exit_code = 1

            if not args.skip_suspension:
                result = await run_suspension_check(session, args.dry_run)
                if not result.get("success"):
                    exit_code = 1

            if not args.skip_reminders:
                result = await run_reminders(session, args.dry_run)
                if not result.get("success"):
                    exit_code = 1
    finally:
        await engine.dispose()

    if exit_code:
        logger.error("Billing maintenance run finished with errors")
    else:
        logger.info("Billing maintenance run complete")
    sys.exit(exit_code)


if __name__ == "__main__":
    asyncio.run(main())
