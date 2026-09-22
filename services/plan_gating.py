"""Plan-tier feature gating.

BillingPlan (models/billing.py) controls billing cadence — termly vs.
monthly. PlanTier controls feature entitlement — the actual value ladder.
The two are independent: a school can be on either cadence at any tier.

This is deliberately a flat allow-list per tier rather than a hierarchy
table. The tier list is short and expected to stay short — add a module key
here (and to TIER_MODULES / MODULE_LABELS) when gating a new module behind
a plan, the same way routers/integrations.py does today.
"""
from typing import Dict, Set

from fastapi import Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from auth import get_current_user
from database import get_session
from models.billing import BillingConfiguration, PlanTier
from models.user import User, UserRole

# Which modules each tier unlocks. Starter (displayed as "Basic") is
# deliberately empty — it's the entry tier, everything listed here is an
# upsell. Each tier includes everything the tier below it has (Enterprise
# is a superset of Growth, which is a superset of Standard).
#
# Module-key design notes:
# - student_safety merges what the source pricing comparison splits into two
#   marketing categories (pickup-persons/location/release-hold, and
#   on-my-way/queue/status) into one key — both live in routers/security.py,
#   are used on the same screens, and splitting them would just double the
#   Depends()/hasModule() boilerplate for no gating benefit.
# - pickup_advanced absorbs the court-order/custody-restriction endpoint too,
#   rather than giving a single endpoint its own module key.
# - routers/roles.py (RBAC/permission assignment) is deliberately left out of
#   every tier below — schools need to be able to restrict their own staff's
#   access regardless of plan, so it's treated as a security primitive, not
#   a premium feature, even though comparable competitor pricing pages gate it.
_STANDARD_MODULES = {"academic_reports", "staff_attendance", "comms_plus", "fees_plus"}
_GROWTH_ONLY_MODULES = {
    "integrations", "finance_advanced",  # pre-existing, unchanged
    "student_safety", "gate_attendance", "transport_module",
    "homework_module", "comms_premium", "fees_advanced",
}
_ENTERPRISE_ONLY_MODULES = {"payroll", "id_cards", "pickup_advanced"}  # payroll pre-existing, unchanged

TIER_MODULES: Dict[PlanTier, Set[str]] = {
    PlanTier.STARTER: set(),
    PlanTier.STANDARD: set(_STANDARD_MODULES),
    PlanTier.GROWTH: _STANDARD_MODULES | _GROWTH_ONLY_MODULES,
    PlanTier.ENTERPRISE: _STANDARD_MODULES | _GROWTH_ONLY_MODULES | _ENTERPRISE_ONLY_MODULES,
}

MODULE_LABELS: Dict[str, str] = {
    "integrations": "Integrations & API access",
    "finance_advanced": "Advanced Finance (general ledger, budgeting, depreciation, bank reconciliation)",
    "payroll": "Payroll Processing",
    "academic_reports": "Academic Reports, Timetable & Calendar",
    "staff_attendance": "Staff Attendance Tracking",
    "comms_plus": "Announcements, Group Messaging & Surveys",
    "fees_plus": "Online Payments, Auto-Settlement & Fee Reminders",
    "student_safety": "Student Safety & Quick Pickup",
    "gate_attendance": "Gate Attendance & Absence Requests",
    "transport_module": "School Bus & Transport Management",
    "homework_module": "Homework & Assignments",
    "comms_premium": "Message Attachments",
    "fees_advanced": "Accounting Integrations (Tally/QuickBooks)",
    "id_cards": "ID Card Printing",
    "pickup_advanced": "QR Check-In & Custody Controls",
}

# Lowest tier first — used to name the *actual* minimum tier a module needs
# in the 403 message below, since that's no longer always "Growth" now that
# payroll is Enterprise-only.
_TIER_ORDER = [PlanTier.STARTER, PlanTier.STANDARD, PlanTier.GROWTH, PlanTier.ENTERPRISE]
_TIER_DISPLAY_NAMES: Dict[PlanTier, str] = {
    PlanTier.STARTER: "Basic",
    PlanTier.STANDARD: "Standard",
    PlanTier.GROWTH: "Growth",
    PlanTier.ENTERPRISE: "Enterprise",
}


def _minimum_tier_for(module: str) -> str:
    for tier in _TIER_ORDER:
        if module in TIER_MODULES.get(tier, set()):
            return _TIER_DISPLAY_NAMES[tier]
    return "a higher"  # module isn't in any tier's allow-list — misconfiguration, but don't crash the error message over it


async def get_school_plan_tier(session: AsyncSession, school_id: str) -> PlanTier:
    """Defaults to STARTER for a school with no BillingConfiguration row yet
    (get_billing_config in routers/billing.py lazily creates one on first
    read, but plenty of other code paths only ever read this)."""
    result = await session.execute(
        select(BillingConfiguration.plan_tier).where(BillingConfiguration.school_id == school_id)
    )
    tier = result.scalar_one_or_none()
    if not tier:
        return PlanTier.STARTER
    return tier if isinstance(tier, PlanTier) else PlanTier(tier)


def require_plan_feature(module: str):
    """FastAPI dependency factory: 403s with an upgrade-prompt message if
    the current user's school plan tier doesn't include `module`.

    SUPER_ADMIN always passes through — they're platform staff administering
    the module on a school's behalf, not a subscriber of it. A user with no
    school_id (shouldn't normally reach a school-scoped router, but the
    check has to resolve to *something*) also passes through rather than
    raising a confusing error unrelated to the actual request.
    """
    async def _check(
        current_user: User = Depends(get_current_user),
        session: AsyncSession = Depends(get_session),
    ) -> User:
        if current_user.role == UserRole.SUPER_ADMIN or not current_user.school_id:
            return current_user

        tier = await get_school_plan_tier(session, current_user.school_id)
        if module not in TIER_MODULES.get(tier, set()):
            label = MODULE_LABELS.get(module, module)
            min_tier = _minimum_tier_for(module)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"{label} requires the {min_tier} plan or higher. Contact your school administrator to upgrade.",
            )
        return current_user

    return _check
