"""Library fine posting, settings, and overdue accrual.

Fines are posted as real Fee rows (via a system-managed "Library Fines"
FeeStructure per school/term) so they show up in the student's actual fee
balance, not a library-only shadow ledger. Staff borrowers have no Fee
model, so their fines are tracked only on LibraryFine (fee_id stays null).

reconcile_overdue_fine is the single source of truth for "how much overdue
fine should this loan have accrued by date X" — it is idempotent (safe to
call repeatedly for the same loan/date without double-charging) because it
always tops up to the target total rather than posting a fresh fine each
time. Both the return-time calculation and the daily accrual job call it,
so a loan returned the same day the accrual job already ran doesn't get
charged twice for the same days.
"""
import logging
from datetime import date, datetime
from typing import Optional

from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import async_session
from models.library_circulation import LibraryLoan, LibraryFine, LoanStatus, LibraryFineSettings, FineStatus
from models.student import Student
from models.school import AcademicTerm
from models.fee import Fee, FeeStructure, PaymentStatus, FeeType

logger = logging.getLogger(__name__)

OVERDUE_REASON_PREFIX = "Overdue"


async def is_fine_outstanding(session: AsyncSession, fine: LibraryFine) -> bool:
    """Whether this fine is actually still owed. LibraryFine.status is only
    ever updated by this module's own pay_fine/waive_fine (routers/library_circulation.py)
    — a fine's linked Fee can also be paid off through the general fee
    ledger (routers/fees.py's cashier/parent payment endpoint, or
    services/online_payment_service.py's online payment flow, both of
    which distribute a payment across every outstanding Fee for a student
    with no idea LibraryFine exists), which leaves status stuck at PENDING
    forever even though the money's been collected. When a fee_id exists,
    check the Fee's actual balance instead of trusting the possibly-stale
    status flag; a staff borrower's fine (no Student, so no Fee, so
    fee_id is null) has nothing else to check against, so status is the
    only source of truth for them."""
    if fine.status != FineStatus.PENDING.value:
        return False
    if not fine.fee_id:
        return True
    fee = await session.get(Fee, fine.fee_id)
    if not fee:
        return True
    return (fee.amount_due - fee.amount_paid - fee.discount) > 0


async def get_or_create_fine_settings(session: AsyncSession, school_id: str) -> LibraryFineSettings:
    result = await session.execute(select(LibraryFineSettings).where(LibraryFineSettings.school_id == school_id))
    settings = result.scalar_one_or_none()
    if not settings:
        settings = LibraryFineSettings(school_id=school_id)
        session.add(settings)
        await session.flush()
    return settings


async def _get_current_academic_term_id(session: AsyncSession, school_id: str) -> Optional[str]:
    result = await session.execute(
        select(AcademicTerm).where(AcademicTerm.school_id == school_id, AcademicTerm.is_current == True)  # noqa: E712
    )
    term = result.scalar_one_or_none()
    return term.id if term else None


async def _get_or_create_library_fine_structure(session: AsyncSession, school_id: str, academic_term_id: str) -> FeeStructure:
    marker = "Library fines (system-managed)"
    existing = await session.execute(
        select(FeeStructure).where(
            FeeStructure.school_id == school_id,
            FeeStructure.academic_term_id == academic_term_id,
            FeeStructure.fee_type == FeeType.LIBRARY,
            FeeStructure.description == marker,
        )
    )
    structure = existing.scalar_one_or_none()
    if structure:
        return structure
    structure = FeeStructure(
        school_id=school_id,
        academic_term_id=academic_term_id,
        class_level="all",
        fee_type=FeeType.LIBRARY,
        amount=0,
        description=marker,
        is_mandatory=False,
        due_date=datetime.utcnow().strftime("%Y-%m-%d"),
    )
    session.add(structure)
    await session.flush()
    return structure


async def post_fine(session: AsyncSession, loan: LibraryLoan, amount: float, reason: str) -> LibraryFine:
    fee_id = None
    if loan.borrower_user_id:
        student = (await session.execute(select(Student).where(Student.user_id == loan.borrower_user_id))).scalar_one_or_none()
        if student:
            term_id = await _get_current_academic_term_id(session, loan.school_id)
            if term_id:
                structure = await _get_or_create_library_fine_structure(session, loan.school_id, term_id)
                fee = Fee(
                    school_id=loan.school_id,
                    student_id=student.id,
                    academic_term_id=term_id,
                    fee_structure_id=structure.id,
                    amount_due=amount,
                    status=PaymentStatus.PENDING,
                )
                session.add(fee)
                await session.flush()
                fee_id = fee.id

    fine = LibraryFine(school_id=loan.school_id, loan_id=loan.id, amount=amount, reason=reason, fee_id=fee_id)
    session.add(fine)
    await session.flush()
    return fine


async def reconcile_overdue_fine(
    session: AsyncSession, loan: LibraryLoan, as_of: date, settings: LibraryFineSettings
) -> Optional[LibraryFine]:
    """Top up this loan's posted overdue fine to match days-late * rate as
    of `as_of` (capped at max_fine_days). Returns the incremental fine
    posted, or None if nothing was owed beyond what's already posted."""
    due = datetime.strptime(loan.due_date, "%Y-%m-%d").date()
    if as_of <= due:
        return None

    days_late = min((as_of - due).days, settings.max_fine_days)
    target_total = round(days_late * settings.fine_per_day, 2)

    existing = await session.execute(
        select(LibraryFine).where(LibraryFine.loan_id == loan.id, LibraryFine.reason.like(f"{OVERDUE_REASON_PREFIX}%"))
    )
    already_posted = round(sum(f.amount for f in existing.scalars().all()), 2)

    increment = round(target_total - already_posted, 2)
    if increment <= 0:
        return None
    return await post_fine(session, loan, increment, f"{OVERDUE_REASON_PREFIX} — accrued through {as_of.isoformat()}")


async def accrue_overdue_fines() -> dict:
    """Daily sweep across every school: top up the overdue fine on every
    still-active loan past its due date, so a book that's never returned
    still accrues rather than sitting at zero until someone clicks Return."""
    today = datetime.utcnow().date()
    today_str = today.isoformat()
    loans_checked = 0
    fines_accrued = 0
    total_amount = 0.0

    async with async_session() as session:
        result = await session.execute(
            select(LibraryLoan).where(LibraryLoan.status == LoanStatus.ACTIVE.value, LibraryLoan.due_date < today_str)
        )
        loans = result.scalars().all()
        loans_checked = len(loans)

        settings_cache: dict[str, LibraryFineSettings] = {}
        for loan in loans:
            settings = settings_cache.get(loan.school_id)
            if not settings:
                settings = await get_or_create_fine_settings(session, loan.school_id)
                settings_cache[loan.school_id] = settings
            fine = await reconcile_overdue_fine(session, loan, today, settings)
            if fine:
                fines_accrued += 1
                total_amount += fine.amount

        await session.commit()

    return {"loans_checked": loans_checked, "fines_accrued": fines_accrued, "total_amount": round(total_amount, 2)}
