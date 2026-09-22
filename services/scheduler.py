"""In-process job scheduler for platform billing.

This codebase has no task queue (no Celery/RQ) — the billing pieces below
(auto-renewal charging, overdue-suspension enforcement, payment reminders,
mid-cycle proration) all existed as working service methods but were only
ever invoked by a super admin hitting an endpoint by hand. APScheduler
running inside the same FastAPI process is the lightest way to make that
automatic without standing up a new service to deploy and operate.

Jobs run in this order, once a day: proration first (a school's roster may
have grown since the subscription was billed — this adds the prorated
charge to the subscription's balance), then auto-renewal charges (collect
what can be collected, now including any proration just added), then
reminders (tell schools about what's still owed), then the
overdue-suspension sweep (enforce what's still unpaid past the grace
period). Each job opens and closes its own session — there is no request
in progress to borrow one from.
"""
import logging
import os
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlmodel import select

from database import async_session
from models.school import School
from models.billing import PlatformSubscription, SubscriptionStatus
from services.platform_billing_service import PlatformBillingService, subscription_outstanding
from services.proration_service import ProrationService
from services.subscription_suspension_service import SubscriptionSuspensionService
from services.payment_reminder_service import PaymentReminderService
from services.library_fine_service import accrue_overdue_fines
from services.facilities_maintenance_service import run_due_maintenance_schedules
from services.fee_reminder_service import run_fee_reminder_sweep
from services.fee_late_fee_service import run_late_fee_sweep
from services.attendance_risk_service import run_attendance_risk_sweep
from services.gate_attendance_service import run_gate_pickup_escalation_sweep
from services.biometric_device_health_service import run_device_health_sweep
from services.custom_reports_service import run_scheduled_reports_sweep
from services.exam_result_aggregation_service import aggregate_exam_session_to_grades
from models.exam import ExamSession
from services.depreciation_service import DepreciationService
from services.recurring_entry_service import RecurringEntryService

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


async def run_proration_check() -> None:
    """Adds a prorated charge to every subscription whose live student
    roster has grown past what it was last billed for. Runs before
    run_auto_renewals so the same day's renewal charge (if any) already
    reflects it — see ProrationService.check_and_apply_proration, which
    only records the charge/adjustment, it never itself calls Paystack."""
    async with async_session() as session:
        result = await ProrationService().check_and_apply_proration(session, school_id=None)
        if result.get("success"):
            logger.info(
                f"Proration sweep: {result.get('subscriptions_prorated', 0)} subscription(s) charged, "
                f"total GHS {result.get('total_charged', 0)}"
            )
        else:
            logger.error(f"Proration sweep failed: {result.get('error')}")


async def run_auto_renewals() -> None:
    """Attempt to auto-charge every school with a saved card and money
    owed. One school's failure (declined card, no email, etc.) never stops
    the run — process_auto_renewal always returns rather than raising."""
    paystack_secret_key = os.getenv("PAYSTACK_SECRET_KEY", "")
    if not paystack_secret_key:
        logger.info("Skipping auto-renewal run: PAYSTACK_SECRET_KEY not configured")
        return

    billing_service = PlatformBillingService(paystack_secret_key)
    charged = 0
    attempted = 0

    async with async_session() as session:
        result = await session.execute(
            select(PlatformSubscription).where(
                PlatformSubscription.status.in_([SubscriptionStatus.PENDING, SubscriptionStatus.ACTIVE])
            )
        )
        subscriptions = result.scalars().all()

        for sub in subscriptions:
            if subscription_outstanding(sub) <= 0:
                continue
            attempted += 1
            outcome = await billing_service.process_auto_renewal(session, sub)
            if outcome.get("success") and not outcome.get("skipped"):
                charged += 1

    logger.info(f"Auto-renewal run: {charged}/{attempted} subscriptions with a balance were charged")


async def run_payment_reminders() -> None:
    async with async_session() as session:
        result = await PaymentReminderService().send_pending_reminders(session, school_id=None)
        if result.get("success"):
            logger.info(f"Reminder run: {result.get('reminders_sent', 0)} reminders sent")
        else:
            logger.error(f"Reminder run failed: {result.get('error')}")


async def run_overdue_suspension_check() -> None:
    async with async_session() as session:
        result = await SubscriptionSuspensionService().check_and_suspend_overdue(session, school_id=None)
        if result.get("success"):
            logger.info(f"Suspension sweep: {result.get('subscriptions_suspended', 0)} subscriptions suspended")
        else:
            logger.error(f"Suspension sweep failed: {result.get('error')}")


async def run_library_fine_accrual() -> None:
    """Tops up the overdue fine on every still-active loan past its due
    date, so a book that's never returned keeps accruing instead of sitting
    at zero until someone eventually clicks Return."""
    result = await accrue_overdue_fines()
    logger.info(
        f"Library overdue-fine accrual: checked {result['loans_checked']} overdue loan(s), "
        f"posted {result['fines_accrued']} fine(s) totaling {result['total_amount']}"
    )


async def run_preventive_maintenance_sweep() -> None:
    """Opens a FacilityWorkOrder for every school's overdue preventive
    maintenance schedules — see services/facilities_maintenance_service.py."""
    async with async_session() as session:
        result = await run_due_maintenance_schedules(session, school_id=None)
        logger.info(
            f"Preventive maintenance sweep: {result['work_orders_created']} work order(s) created "
            f"from {result['schedules_checked']} due schedule(s) across all schools"
        )


async def run_exam_result_auto_publish() -> None:
    """Publishes exam results for every ExamSession whose
    results_release_date has arrived and isn't published yet — a scheduled
    release date takes effect on its own without anyone clicking Publish.
    Manual publish/unpublish (routers/exams.py) can still act at any time;
    this only ever flips unpublished -> published, never the reverse."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    published = 0
    async with async_session() as session:
        result = await session.execute(
            select(ExamSession).where(
                ExamSession.results_published == False,  # noqa: E712
                ExamSession.results_release_date.is_not(None),
                ExamSession.results_release_date <= today,
            )
        )
        sessions = result.scalars().all()
        for exam_session in sessions:
            exam_session.results_published = True
            exam_session.results_published_at = datetime.utcnow()
            session.add(exam_session)
            published += 1
        if sessions:
            await session.commit()
            for exam_session in sessions:
                await aggregate_exam_session_to_grades(session, exam_session)
    logger.info(f"Exam result auto-publish: {published} exam session(s) published")


async def run_gate_pickup_escalation() -> None:
    """A student checked in at the gate this morning with no check-out
    recorded is a genuine safeguarding blind spot if nobody notices before
    the day ends — this was previously entirely reactive (an admin had to
    think to open the "still on campus" view). See
    services.gate_attendance_service.run_gate_pickup_escalation_sweep."""
    result = await run_gate_pickup_escalation_sweep()
    logger.info(f"Gate pickup escalation sweep: {result['escalated']} student(s) flagged as checked-in-not-checked-out")


async def run_biometric_device_health() -> None:
    """A dead biometric device (gate, canteen, or staff clock-in) previously
    produced silent zero-attendance with nobody notified — see
    services.biometric_device_health_service.run_device_health_sweep."""
    result = await run_device_health_sweep()
    logger.info(f"Biometric device health sweep: {result['flagged']} device(s) newly flagged as stale")


async def run_depreciation_sweep() -> None:
    """Posts the monthly depreciation entry for every active schedule due to
    run, for every school. Unlike the billing/fee sweeps above,
    DepreciationService.generate_due_depreciation is school-scoped (no
    school_id=None "all schools" mode), so this loops over every School row
    itself — one school's failure never stops the rest, same as the
    auto-renewal loop above."""
    posted = 0
    async with async_session() as session:
        result = await session.execute(select(School.id))
        school_ids = result.scalars().all()
        for school_id in school_ids:
            try:
                entries = await DepreciationService(session).generate_due_depreciation(
                    school_id=school_id, created_by="SYSTEM",
                )
                posted += len(entries)
            except Exception as e:
                logger.error(f"Depreciation sweep failed for school {school_id}: {e}")
    logger.info(f"Depreciation sweep: {posted} entry(ies) posted across all schools")


async def run_recurring_entries_sweep() -> None:
    """Generates due recurring journal-entry drafts and auto-reverses due
    accrual entries, for every school. Same per-school loop reasoning as
    run_depreciation_sweep above — RecurringEntryService's methods are
    school-scoped with no "all schools" mode of their own."""
    generated = 0
    reversed_count = 0
    async with async_session() as session:
        result = await session.execute(select(School.id))
        school_ids = result.scalars().all()
        for school_id in school_ids:
            service = RecurringEntryService(session)
            try:
                gen_results = await service.generate_due_entries(school_id=school_id, created_by="SYSTEM")
                generated += sum(1 for r in gen_results if r["status"] == "created")
            except Exception as e:
                logger.error(f"Recurring entry generation failed for school {school_id}: {e}")
            try:
                rev_results = await service.reverse_due_accruals(school_id=school_id, reversed_by="SYSTEM")
                reversed_count += sum(1 for r in rev_results if r["status"] == "reversed")
            except Exception as e:
                logger.error(f"Accrual auto-reversal failed for school {school_id}: {e}")
    logger.info(
        f"Recurring entries sweep: {generated} entry(ies) generated, "
        f"{reversed_count} accrual(s) auto-reversed across all schools"
    )


async def run_webhook_redelivery() -> None:
    """Retries every failed WebhookDelivery below the attempt cap — see
    scripts/redeliver_failed_webhooks.py's own docstring for why this has
    to be polling-based (no durable queue in this codebase). Runs far more
    often than everything else here since a webhook subscriber being
    briefly unreachable shouldn't mean waiting until the next day for the
    retry."""
    from scripts.redeliver_failed_webhooks import redeliver_failed_webhooks
    await redeliver_failed_webhooks()


async def run_daily_billing_cycle() -> None:
    """The single daily job — runs the four steps in sequence so a
    proration charge just added is reflected in the same day's
    auto-renewal attempt, and an auto-charge that just succeeded is
    reflected before the reminder and suspension steps read subscription
    status."""
    logger.info("Starting daily billing cycle (proration -> auto-renewals -> reminders -> suspension check)")
    await run_proration_check()
    await run_auto_renewals()
    await run_payment_reminders()
    await run_overdue_suspension_check()
    logger.info("Daily billing cycle complete")


def start_scheduler() -> None:
    """Called once from server.py's startup event. A module-level guard
    keeps a second call (e.g. an app reload in the same process) from
    registering the job twice."""
    global _scheduler
    if _scheduler is not None:
        return

    _scheduler = AsyncIOScheduler(timezone="UTC")
    # Every 10 minutes, all day — a subscriber being briefly unreachable
    # shouldn't mean waiting for the next nightly window like every other
    # job here does.
    _scheduler.add_job(
        run_webhook_redelivery,
        trigger=IntervalTrigger(minutes=10),
        id="webhook_redelivery",
        replace_existing=True,
        misfire_grace_time=120,
    )
    # Every 30 minutes, all day — a dead device deserves faster notice than
    # a once-daily job, same reasoning as webhook redelivery above.
    _scheduler.add_job(
        run_biometric_device_health,
        trigger=IntervalTrigger(minutes=30),
        id="biometric_device_health",
        replace_existing=True,
        misfire_grace_time=300,
    )
    # 01:00 UTC — before the billing cycle, same outside-school-hours window.
    _scheduler.add_job(
        run_library_fine_accrual,
        trigger=CronTrigger(hour=1, minute=0),
        id="library_fine_accrual",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    # 02:00 UTC — outside school-hours traffic for Ghana/West Africa (GMT,
    # no DST), and comfortably after most other nightly maintenance.
    _scheduler.add_job(
        run_daily_billing_cycle,
        trigger=CronTrigger(hour=2, minute=0),
        id="daily_billing_cycle",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    # 03:00 UTC — after billing, its own quiet window.
    _scheduler.add_job(
        run_preventive_maintenance_sweep,
        trigger=CronTrigger(hour=3, minute=0),
        id="preventive_maintenance_sweep",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    # 03:30 UTC — GL-affecting sweeps grouped together, after billing/facilities,
    # well before the school day (fee reminders, attendance) starts.
    _scheduler.add_job(
        run_depreciation_sweep,
        trigger=CronTrigger(hour=3, minute=30),
        id="depreciation_sweep",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    # 03:45 UTC — right after depreciation, same GL-maintenance window.
    _scheduler.add_job(
        run_recurring_entries_sweep,
        trigger=CronTrigger(hour=3, minute=45),
        id="recurring_entries_sweep",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    # 05:00 UTC — after maintenance, before the fee-reminder sweep.
    _scheduler.add_job(
        run_exam_result_auto_publish,
        trigger=CronTrigger(hour=5, minute=0),
        id="exam_result_auto_publish",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    # 05:45 UTC — before fee reminders, so a reminder sent moments later
    # reflects any late fee this sweep just applied.
    _scheduler.add_job(
        run_late_fee_sweep,
        trigger=CronTrigger(hour=5, minute=45),
        id="late_fee_sweep",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    # 06:00 UTC — separate from the 02:00 platform-billing cycle above,
    # which reminds SCHOOLS about their own Campusio subscription, not
    # parents about student fees.
    _scheduler.add_job(
        run_fee_reminder_sweep,
        trigger=CronTrigger(hour=6, minute=0),
        id="fee_reminder_sweep",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    # 06:30 UTC — after fee reminders, its own quiet window.
    _scheduler.add_job(
        run_attendance_risk_sweep,
        trigger=CronTrigger(hour=6, minute=30),
        id="attendance_risk_sweep",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    # 07:00 UTC — last of the daily jobs, so any report covering "yesterday"
    # runs after every other sweep has finished writing its own data.
    _scheduler.add_job(
        run_scheduled_reports_sweep,
        trigger=CronTrigger(hour=7, minute=0),
        id="scheduled_reports_sweep",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    # 16:00 UTC — Ghana/West Africa is GMT with no DST, so this is 4pm local:
    # comfortably after a typical Basic/JHS dismissal time, giving parents a
    # reasonable window to actually check a child out before this fires.
    _scheduler.add_job(
        run_gate_pickup_escalation,
        trigger=CronTrigger(hour=16, minute=0),
        id="gate_pickup_escalation",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    _scheduler.start()
    logger.info(
        "Billing scheduler started — webhook redelivery every 10 minutes, library fine accrual at 01:00 UTC, "
        "billing cycle at 02:00 UTC, preventive maintenance sweep at 03:00 UTC, depreciation sweep at 03:30 UTC, "
        "recurring entries sweep at 03:45 UTC, exam result auto-publish at 05:00 UTC, "
        "late fee sweep at 05:45 UTC, fee reminders at 06:00 UTC, attendance risk sweep at 06:30 UTC, scheduled reports at 07:00 UTC, "
        "gate pickup escalation at 16:00 UTC, biometric device health every 30 minutes"
    )


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
