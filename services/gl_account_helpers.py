"""Shared "get this system account, creating it from the default seed if a
school doesn't have it yet" helper — used by every new GL-posting path this
review added (AR, Paystack clearing, refunds payable, bad debt expense,
payment-processing fees). Needed because `models.finance.seed_coa.
DEFAULT_CHART_OF_ACCOUNTS` is only ever applied at NEW-school setup time
(services.coa_initialization.seed_default_chart_of_accounts) — a school that
was already seeded before an account was added to that list would otherwise
hit a hard "GL Account not found" error the first time new code tries to
post to it. This lazily backfills exactly the one missing account, the same
self-healing pattern already used elsewhere in this codebase (e.g.
GateAttendanceSettings/StaffAttendanceSettings get-or-create)."""
import logging
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from models.finance.chart_of_accounts import GLAccount, GLAccountCreate
from models.finance.seed_coa import DEFAULT_CHART_OF_ACCOUNTS

logger = logging.getLogger(__name__)

_DEFAULTS_BY_CODE = {entry["account_code"]: entry for entry in DEFAULT_CHART_OF_ACCOUNTS}


async def get_or_create_system_account(session: AsyncSession, school_id: str, account_code: str) -> GLAccount:
    """Fetch an active GL account by code, creating it from the standard
    seed template if this school doesn't have it. Raises if `account_code`
    isn't in the default template at all (a genuine programming error, not
    a missing-account situation this can self-heal)."""
    result = await session.execute(
        select(GLAccount).where(GLAccount.school_id == school_id, GLAccount.account_code == account_code, GLAccount.is_active == True)  # noqa: E712
    )
    account = result.scalar_one_or_none()
    if account:
        return account

    template = _DEFAULTS_BY_CODE.get(account_code)
    if not template:
        raise ValueError(f"No default template for GL account code {account_code!r} — cannot auto-create it")

    from services.coa_service import CoaService
    coa_service = CoaService(session)
    logger.info(f"Auto-creating missing system GL account {account_code} ({template['account_name']}) for school {school_id}")
    return await coa_service.create_account(
        school_id=school_id,
        account_data=GLAccountCreate(**template),
        created_by="SYSTEM",
    )
