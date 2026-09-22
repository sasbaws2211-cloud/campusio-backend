"""Physical book circulation: copies, issue/return, fines, reservations.

Fines are posted as real Fee rows (via a system-managed "Library Fines"
FeeStructure per school/term) so they show up in the student's actual fee
balance, not a library-only shadow ledger. Staff borrowers have no Fee
model, so their fines are tracked only on LibraryFine (fee_id stays null).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlmodel import select
from sqlalchemy import func, or_
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_current_user, require_roles
from database import get_session
from dependencies import get_current_school_id
from models.library import LibraryItem
from models.library_circulation import (
    LibraryBookCopy,
    LibraryBookCopyCreate,
    LibraryBookCopyUpdate,
    LibraryLoan,
    LibraryLoanCreate,
    LibraryLoanReportLost,
    LibraryFine,
    LibraryFineWaive,
    LibraryFinePayment,
    LibraryFineSettings,
    LibraryFineSettingsUpdate,
    LibraryReservation,
    LibraryReservationCreate,
    LibraryPatronStatus,
    LibraryPatronStatusUpdate,
    PatronStatus,
    CopyRetire,
    CopyStatus,
    LoanStatus,
    FineStatus,
    ReservationStatus,
)
from models.student import Student
from models.staff import Staff
from models.user import User, UserRole
from models.fee import Fee, FeePayment, PaymentStatus, PaymentMethod, DiscountType
from services.library_fine_service import post_fine, reconcile_overdue_fine, get_or_create_fine_settings, is_fine_outstanding

router = APIRouter(prefix="/library", tags=["Library Circulation"])

CIRCULATION_ROLES = (UserRole.SCHOOL_ADMIN, UserRole.SUPER_ADMIN, UserRole.TEACHER)
SETTINGS_ROLES = (UserRole.SCHOOL_ADMIN, UserRole.SUPER_ADMIN)
DEFAULT_LOAN_DAYS = 14


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_copy_or_404(session: AsyncSession, copy_id: str, school_id: str) -> LibraryBookCopy:
    result = await session.execute(
        select(LibraryBookCopy).where(LibraryBookCopy.id == copy_id, LibraryBookCopy.school_id == school_id)
    )
    copy = result.scalar_one_or_none()
    if not copy:
        raise HTTPException(status_code=404, detail="Book copy not found")
    return copy


async def _get_loan_or_404(session: AsyncSession, loan_id: str, school_id: str) -> LibraryLoan:
    result = await session.execute(
        select(LibraryLoan).where(LibraryLoan.id == loan_id, LibraryLoan.school_id == school_id)
    )
    loan = result.scalar_one_or_none()
    if not loan:
        raise HTTPException(status_code=404, detail="Loan not found")
    return loan


async def _get_student_by_user_id(session: AsyncSession, user_id: str) -> Optional[Student]:
    result = await session.execute(select(Student).where(Student.user_id == user_id))
    return result.scalar_one_or_none()


async def _get_staff_by_user_id(session: AsyncSession, user_id: str) -> Optional[Staff]:
    result = await session.execute(select(Staff).where(Staff.user_id == user_id))
    return result.scalar_one_or_none()


async def _get_or_create_patron_status(session: AsyncSession, school_id: str, user_id: str) -> LibraryPatronStatus:
    result = await session.execute(
        select(LibraryPatronStatus).where(LibraryPatronStatus.school_id == school_id, LibraryPatronStatus.user_id == user_id)
    )
    record = result.scalar_one_or_none()
    if not record:
        record = LibraryPatronStatus(school_id=school_id, user_id=user_id)
        session.add(record)
        await session.flush()
    return record


async def _active_loan_count(session: AsyncSession, school_id: str, user_id: str) -> int:
    result = await session.execute(
        select(func.count(LibraryLoan.id)).where(
            LibraryLoan.school_id == school_id, LibraryLoan.borrower_user_id == user_id, LibraryLoan.status == LoanStatus.ACTIVE.value
        )
    )
    return result.scalar() or 0


def _copy_to_dict(copy: LibraryBookCopy) -> dict:
    return {
        "id": copy.id,
        "school_id": copy.school_id,
        "item_id": copy.item_id,
        "barcode": copy.barcode,
        "accession_number": copy.accession_number,
        "condition": copy.condition,
        "status": copy.status,
        "location": copy.location,
        "retired_reason": copy.retired_reason,
        "retired_at": copy.retired_at,
        "created_at": copy.created_at,
        "updated_at": copy.updated_at,
    }


def _reservation_to_dict(reservation: LibraryReservation) -> dict:
    return {
        "id": reservation.id,
        "item_id": reservation.item_id,
        "user_id": reservation.user_id,
        "status": reservation.status,
        "reserved_at": reservation.reserved_at,
        "expires_at": reservation.expires_at,
        "fulfilled_loan_id": reservation.fulfilled_loan_id,
    }


async def _serialize_loan(session: AsyncSession, loan: LibraryLoan) -> dict:
    copy = (await session.execute(select(LibraryBookCopy).where(LibraryBookCopy.id == loan.copy_id))).scalar_one_or_none()
    item = None
    if copy:
        item = (await session.execute(select(LibraryItem).where(LibraryItem.id == copy.item_id))).scalar_one_or_none()

    borrower_name = None
    borrower_type = None
    if loan.borrower_user_id:
        student = await _get_student_by_user_id(session, loan.borrower_user_id)
        if student:
            borrower_name = f"{student.first_name} {student.last_name}"
            borrower_type = "student"
        else:
            staff = await _get_staff_by_user_id(session, loan.borrower_user_id)
            if staff:
                borrower_name = f"{staff.first_name} {staff.last_name}"
                borrower_type = "staff"

    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    is_overdue = loan.status == LoanStatus.ACTIVE.value and loan.due_date < today_str

    return {
        "id": loan.id,
        "copy_id": loan.copy_id,
        "barcode": copy.barcode if copy else None,
        "item_id": copy.item_id if copy else None,
        "item_title": item.title if item else None,
        "borrower_user_id": loan.borrower_user_id,
        "borrower_name": borrower_name,
        "borrower_type": borrower_type,
        "issued_by": loan.issued_by,
        "issue_date": loan.issue_date,
        "due_date": loan.due_date,
        "return_date": loan.return_date,
        "status": loan.status,
        "is_overdue": is_overdue,
        "renewal_count": loan.renewal_count,
        "created_at": loan.created_at,
        "updated_at": loan.updated_at,
    }


# ---------------------------------------------------------------------------
# Copies
# ---------------------------------------------------------------------------

@router.post("/copies", response_model=dict, status_code=status.HTTP_201_CREATED)
async def create_copy(
    payload: LibraryBookCopyCreate,
    current_user: User = Depends(require_roles(*CIRCULATION_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = await get_current_school_id(current_user)
    item = (
        await session.execute(select(LibraryItem).where(LibraryItem.id == payload.item_id, LibraryItem.school_id == school_id))
    ).scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Library item not found")

    existing = (
        await session.execute(
            select(LibraryBookCopy).where(LibraryBookCopy.school_id == school_id, LibraryBookCopy.barcode == payload.barcode)
        )
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail="A copy with this barcode already exists")

    copy = LibraryBookCopy(
        school_id=school_id,
        item_id=payload.item_id,
        barcode=payload.barcode,
        accession_number=payload.accession_number,
        condition=payload.condition,
        location=payload.location,
    )
    session.add(copy)
    await session.commit()
    await session.refresh(copy)
    return _copy_to_dict(copy)


@router.get("/copies", response_model=List[dict])
async def list_copies(
    item_id: Optional[str] = Query(default=None),
    status_filter: Optional[str] = Query(default=None, alias="status"),
    search: Optional[str] = Query(default=None),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    school_id = await get_current_school_id(current_user)
    stmt = select(LibraryBookCopy).where(LibraryBookCopy.school_id == school_id)
    if item_id:
        stmt = stmt.where(LibraryBookCopy.item_id == item_id)
    if status_filter:
        stmt = stmt.where(LibraryBookCopy.status == status_filter)
    if search:
        term = f"%{search}%"
        stmt = stmt.where(or_(LibraryBookCopy.barcode.ilike(term), LibraryBookCopy.accession_number.ilike(term)))
    result = await session.execute(stmt.order_by(LibraryBookCopy.created_at.desc()))
    return [_copy_to_dict(c) for c in result.scalars().all()]


@router.put("/copies/{copy_id}", response_model=dict)
async def update_copy(
    copy_id: str,
    payload: LibraryBookCopyUpdate,
    current_user: User = Depends(require_roles(*CIRCULATION_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = await get_current_school_id(current_user)
    copy = await _get_copy_or_404(session, copy_id, school_id)
    if payload.barcode is not None:
        copy.barcode = payload.barcode
    if payload.accession_number is not None:
        copy.accession_number = payload.accession_number
    if payload.condition is not None:
        copy.condition = payload.condition
    if payload.status is not None:
        copy.status = payload.status
    if payload.location is not None:
        copy.location = payload.location
    copy.updated_at = datetime.utcnow()
    await session.commit()
    await session.refresh(copy)
    return _copy_to_dict(copy)


@router.delete("/copies/{copy_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_copy(
    copy_id: str,
    current_user: User = Depends(require_roles(*CIRCULATION_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = await get_current_school_id(current_user)
    copy = await _get_copy_or_404(session, copy_id, school_id)
    if copy.status == CopyStatus.CHECKED_OUT.value:
        raise HTTPException(status_code=400, detail="Cannot delete a copy that is currently checked out")
    await session.delete(copy)
    await session.commit()


@router.post("/copies/{copy_id}/retire", response_model=dict)
async def retire_copy(
    copy_id: str,
    payload: CopyRetire,
    current_user: User = Depends(require_roles(*CIRCULATION_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    """Weeding/deaccessioning — formally removes a damaged or obsolete copy
    from circulation with a recorded reason, distinct from a copy that's
    merely LOST on one loan. A retired copy can never be reserved or
    checked out again (still visible in service history via list_copies)."""
    school_id = await get_current_school_id(current_user)
    copy = await _get_copy_or_404(session, copy_id, school_id)
    if copy.status == CopyStatus.CHECKED_OUT.value:
        raise HTTPException(status_code=400, detail="Cannot retire a copy that is currently checked out")
    if copy.status == CopyStatus.RETIRED.value:
        raise HTTPException(status_code=400, detail="This copy is already retired")
    copy.status = CopyStatus.RETIRED.value
    copy.retired_reason = payload.reason
    copy.retired_at = datetime.utcnow()
    copy.updated_at = datetime.utcnow()
    await session.commit()
    await session.refresh(copy)
    return _copy_to_dict(copy)


# ---------------------------------------------------------------------------
# Borrower lookup
# ---------------------------------------------------------------------------

@router.get("/borrowers/lookup", response_model=List[dict])
async def lookup_borrowers(
    query: str = Query(..., min_length=2),
    current_user: User = Depends(require_roles(*CIRCULATION_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = await get_current_school_id(current_user)
    term = f"%{query}%"

    students = (
        await session.execute(
            select(Student)
            .where(
                Student.school_id == school_id,
                or_(Student.first_name.ilike(term), Student.last_name.ilike(term), Student.student_id.ilike(term)),
            )
            .limit(10)
        )
    ).scalars().all()

    staff_rows = (
        await session.execute(
            select(Staff)
            .where(
                Staff.school_id == school_id,
                or_(Staff.first_name.ilike(term), Staff.last_name.ilike(term), Staff.staff_id.ilike(term)),
            )
            .limit(10)
        )
    ).scalars().all()

    results = []
    for s in students:
        if not s.user_id:
            continue
        results.append(
            {"user_id": s.user_id, "type": "student", "name": f"{s.first_name} {s.last_name}", "identifier": s.student_id, "class_id": s.class_id}
        )
    for st in staff_rows:
        if not st.user_id:
            continue
        results.append(
            {"user_id": st.user_id, "type": "staff", "name": f"{st.first_name} {st.last_name}", "identifier": st.staff_id, "position": st.position}
        )
    return results


@router.get("/patrons/{user_id}", response_model=dict)
async def get_patron_card(
    user_id: str,
    current_user: User = Depends(require_roles(*CIRCULATION_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    """A borrower's standing at a glance — used at the circulation desk
    before issuing a loan: status, current loan count vs. their limit, and
    outstanding fines."""
    school_id = await get_current_school_id(current_user)
    borrower = (await session.execute(select(User).where(User.id == user_id, User.school_id == school_id))).scalar_one_or_none()
    if not borrower:
        raise HTTPException(status_code=404, detail="Patron not found")

    student = await _get_student_by_user_id(session, user_id)
    is_student = student is not None
    if is_student:
        name, identifier, patron_type = f"{student.first_name} {student.last_name}", student.student_id, "student"
    else:
        staff = await _get_staff_by_user_id(session, user_id)
        name, identifier, patron_type = (f"{staff.first_name} {staff.last_name}", staff.staff_id, "staff") if staff else (None, None, "unknown")

    settings = await get_or_create_fine_settings(session, school_id)
    loan_limit = settings.max_loans_student if is_student else settings.max_loans_staff
    current_loans = await _active_loan_count(session, school_id, user_id)

    patron_status = await _get_or_create_patron_status(session, school_id, user_id)
    await session.commit()

    # Candidate PENDING fines, then filtered in Python via is_fine_outstanding
    # — LibraryFine.status alone can't be trusted (see that function's
    # docstring: a fine's linked Fee paid off through the general fee
    # ledger never gets LibraryFine.status updated).
    pending_fines_result = await session.execute(
        select(LibraryFine)
        .join(LibraryLoan, LibraryLoan.id == LibraryFine.loan_id)
        .where(LibraryLoan.borrower_user_id == user_id, LibraryFine.school_id == school_id, LibraryFine.status == FineStatus.PENDING.value)
    )
    outstanding = 0.0
    for f in pending_fines_result.scalars().all():
        if await is_fine_outstanding(session, f):
            outstanding += f.amount

    return {
        "user_id": user_id,
        "name": name,
        "identifier": identifier,
        "type": patron_type,
        "status": patron_status.status,
        "suspended_reason": patron_status.reason if patron_status.status == PatronStatus.SUSPENDED.value else None,
        "active_loans": current_loans,
        "loan_limit": loan_limit,
        "at_limit": current_loans >= loan_limit,
        "outstanding_fines": round(float(outstanding), 2),
    }


@router.put("/patrons/{user_id}/status", response_model=dict)
async def set_patron_status(
    user_id: str,
    payload: LibraryPatronStatusUpdate,
    current_user: User = Depends(require_roles(*SETTINGS_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = await get_current_school_id(current_user)
    borrower = (await session.execute(select(User).where(User.id == user_id, User.school_id == school_id))).scalar_one_or_none()
    if not borrower:
        raise HTTPException(status_code=404, detail="Patron not found")

    record = await _get_or_create_patron_status(session, school_id, user_id)
    record.status = payload.status.value
    record.reason = payload.reason
    record.updated_by = current_user.id
    record.updated_at = datetime.utcnow()
    await session.commit()
    return {"user_id": user_id, "status": record.status, "reason": record.reason}


# ---------------------------------------------------------------------------
# Barcode / RFID scan lookup
# ---------------------------------------------------------------------------

@router.get("/scan/{barcode}", response_model=dict)
async def scan_lookup(
    barcode: str,
    current_user: User = Depends(require_roles(*CIRCULATION_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    """Look up a book copy by barcode/RFID and report whether it currently
    has an active loan, so circulation staff can decide checkout-vs-return
    from a single scan instead of guessing which flow to run."""
    school_id = await get_current_school_id(current_user)

    copy = (
        await session.execute(
            select(LibraryBookCopy).where(LibraryBookCopy.school_id == school_id, LibraryBookCopy.barcode == barcode)
        )
    ).scalar_one_or_none()
    if not copy:
        raise HTTPException(status_code=404, detail="No copy found with that barcode")

    loan = (
        await session.execute(
            select(LibraryLoan).where(
                LibraryLoan.school_id == school_id,
                LibraryLoan.copy_id == copy.id,
                LibraryLoan.status == LoanStatus.ACTIVE.value,
            )
        )
    ).scalar_one_or_none()

    item = (
        await session.execute(select(LibraryItem).where(LibraryItem.id == copy.item_id))
    ).scalar_one_or_none()

    return {
        **_copy_to_dict(copy),
        "item_title": item.title if item else None,
        "active_loan": await _serialize_loan(session, loan) if loan else None,
    }


# ---------------------------------------------------------------------------
# Loans
# ---------------------------------------------------------------------------

@router.post("/loans", response_model=dict, status_code=status.HTTP_201_CREATED)
async def issue_loan(
    payload: LibraryLoanCreate,
    current_user: User = Depends(require_roles(*CIRCULATION_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = await get_current_school_id(current_user)

    # Locked (FOR UPDATE) as the FIRST select of this row in the session —
    # copy.status is set to CHECKED_OUT below, and without this two
    # concurrent checkout requests for the same copy/barcode can both read
    # AVAILABLE and both succeed, lending one physical book to two
    # different borrowers. (Locking on a SECOND, re-fetching select after
    # an earlier unlocked load of the same row is NOT equivalent — the ORM
    # identity map returns the already-loaded Python object with its stale
    # cached attributes rather than the freshly-locked row's values, unless
    # that second select also carries .populate_existing(). Simplest fix:
    # never do the earlier unlocked load in the first place.)
    copy = None
    if payload.copy_id:
        result = await session.execute(
            select(LibraryBookCopy).where(LibraryBookCopy.id == payload.copy_id, LibraryBookCopy.school_id == school_id).with_for_update()
        )
        copy = result.scalar_one_or_none()
        if not copy:
            raise HTTPException(status_code=404, detail="Book copy not found")
    elif payload.barcode:
        copy = (
            await session.execute(
                select(LibraryBookCopy).where(LibraryBookCopy.school_id == school_id, LibraryBookCopy.barcode == payload.barcode).with_for_update()
            )
        ).scalar_one_or_none()
        if not copy:
            raise HTTPException(status_code=404, detail="No copy found with that barcode")
    else:
        raise HTTPException(status_code=400, detail="copy_id or barcode is required")

    borrower = (
        await session.execute(select(User).where(User.id == payload.borrower_user_id, User.school_id == school_id))
    ).scalar_one_or_none()
    if not borrower:
        raise HTTPException(status_code=404, detail="Borrower not found")

    patron_status = await _get_or_create_patron_status(session, school_id, payload.borrower_user_id)
    if patron_status.status == PatronStatus.SUSPENDED.value:
        raise HTTPException(status_code=400, detail=f"This patron's borrowing privileges are suspended: {patron_status.reason or 'no reason given'}")

    settings = await get_or_create_fine_settings(session, school_id)
    is_student = await _get_student_by_user_id(session, payload.borrower_user_id) is not None
    loan_limit = settings.max_loans_student if is_student else settings.max_loans_staff
    current_loans = await _active_loan_count(session, school_id, payload.borrower_user_id)
    if current_loans >= loan_limit:
        raise HTTPException(status_code=400, detail=f"This patron already has {current_loans} active loan(s), at their limit of {loan_limit}")

    reservation = None
    if copy.status == CopyStatus.RESERVED.value:
        reservation = (
            await session.execute(
                select(LibraryReservation)
                .where(
                    LibraryReservation.school_id == school_id,
                    LibraryReservation.item_id == copy.item_id,
                    LibraryReservation.status == ReservationStatus.PENDING.value,
                )
                .order_by(LibraryReservation.reserved_at.asc())
            )
        ).scalars().first()
        if reservation and reservation.user_id != payload.borrower_user_id:
            raise HTTPException(status_code=400, detail="This copy is reserved for another patron")
    elif copy.status != CopyStatus.AVAILABLE.value:
        raise HTTPException(status_code=400, detail=f"This copy is not available (status: {copy.status})")

    issue_date = datetime.utcnow().date()
    due_date = payload.due_date or (issue_date + timedelta(days=DEFAULT_LOAN_DAYS)).isoformat()

    loan = LibraryLoan(
        school_id=school_id,
        copy_id=copy.id,
        borrower_user_id=payload.borrower_user_id,
        issued_by=current_user.id,
        issue_date=issue_date.isoformat(),
        due_date=due_date,
        status=LoanStatus.ACTIVE.value,
    )
    session.add(loan)

    if reservation:
        reservation.status = ReservationStatus.FULFILLED.value
        reservation.fulfilled_loan_id = loan.id

    copy.status = CopyStatus.CHECKED_OUT.value
    copy.updated_at = datetime.utcnow()

    await session.commit()
    await session.refresh(loan)
    return await _serialize_loan(session, loan)


@router.post("/loans/{loan_id}/return", response_model=dict)
async def return_loan(
    loan_id: str,
    current_user: User = Depends(require_roles(*CIRCULATION_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = await get_current_school_id(current_user)
    loan = await _get_loan_or_404(session, loan_id, school_id)
    if loan.status != LoanStatus.ACTIVE.value:
        raise HTTPException(status_code=400, detail="This loan is not active")

    copy = await _get_copy_or_404(session, loan.copy_id, school_id)
    today = datetime.utcnow().date()
    loan.return_date = today.isoformat()
    loan.status = LoanStatus.RETURNED.value
    loan.updated_at = datetime.utcnow()

    settings = await get_or_create_fine_settings(session, school_id)
    fine = await reconcile_overdue_fine(session, loan, today, settings)

    reservation = (
        await session.execute(
            select(LibraryReservation)
            .where(
                LibraryReservation.school_id == school_id,
                LibraryReservation.item_id == copy.item_id,
                LibraryReservation.status == ReservationStatus.PENDING.value,
            )
            .order_by(LibraryReservation.reserved_at.asc())
        )
    ).scalars().first()
    copy.status = CopyStatus.RESERVED.value if reservation else CopyStatus.AVAILABLE.value
    copy.updated_at = datetime.utcnow()

    await session.commit()
    await session.refresh(loan)
    result = await _serialize_loan(session, loan)
    if fine:
        result["fine"] = {"id": fine.id, "amount": fine.amount, "reason": fine.reason, "fee_id": fine.fee_id}
    return result


@router.post("/loans/{loan_id}/renew", response_model=dict)
async def renew_loan(
    loan_id: str,
    current_user: User = Depends(require_roles(*CIRCULATION_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = await get_current_school_id(current_user)
    loan = await _get_loan_or_404(session, loan_id, school_id)
    if loan.status != LoanStatus.ACTIVE.value:
        raise HTTPException(status_code=400, detail="Only active loans can be renewed")

    copy = await _get_copy_or_404(session, loan.copy_id, school_id)
    pending_reservation = (
        await session.execute(
            select(LibraryReservation).where(
                LibraryReservation.school_id == school_id,
                LibraryReservation.item_id == copy.item_id,
                LibraryReservation.status == ReservationStatus.PENDING.value,
            )
        )
    ).scalars().first()
    if pending_reservation:
        raise HTTPException(status_code=400, detail="Cannot renew — another patron has this title on hold")

    due = datetime.strptime(loan.due_date, "%Y-%m-%d").date()
    loan.due_date = (due + timedelta(days=DEFAULT_LOAN_DAYS)).isoformat()
    loan.renewal_count += 1
    loan.updated_at = datetime.utcnow()
    await session.commit()
    await session.refresh(loan)
    return await _serialize_loan(session, loan)


@router.post("/loans/{loan_id}/report-lost", response_model=dict)
async def report_lost(
    loan_id: str,
    payload: LibraryLoanReportLost,
    current_user: User = Depends(require_roles(*CIRCULATION_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = await get_current_school_id(current_user)
    loan = await _get_loan_or_404(session, loan_id, school_id)
    if loan.status != LoanStatus.ACTIVE.value:
        raise HTTPException(status_code=400, detail="Only active loans can be reported lost")

    copy = await _get_copy_or_404(session, loan.copy_id, school_id)
    loan.status = LoanStatus.LOST.value
    loan.updated_at = datetime.utcnow()
    copy.status = CopyStatus.LOST.value
    copy.updated_at = datetime.utcnow()

    fine = await post_fine(session, loan, payload.replacement_cost, "Lost book — replacement cost")

    await session.commit()
    await session.refresh(loan)
    result = await _serialize_loan(session, loan)
    result["fine"] = {"id": fine.id, "amount": fine.amount, "reason": fine.reason, "fee_id": fine.fee_id}
    return result


@router.get("/loans", response_model=dict)
async def list_loans(
    status_filter: Optional[str] = Query(default=None, alias="status"),
    borrower_user_id: Optional[str] = Query(default=None),
    overdue_only: bool = Query(default=False),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=20, ge=1, le=100),
    current_user: User = Depends(require_roles(*CIRCULATION_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = await get_current_school_id(current_user)
    stmt = select(LibraryLoan).where(LibraryLoan.school_id == school_id)
    count_stmt = select(func.count(LibraryLoan.id)).where(LibraryLoan.school_id == school_id)

    if status_filter:
        stmt = stmt.where(LibraryLoan.status == status_filter)
        count_stmt = count_stmt.where(LibraryLoan.status == status_filter)
    if borrower_user_id:
        stmt = stmt.where(LibraryLoan.borrower_user_id == borrower_user_id)
        count_stmt = count_stmt.where(LibraryLoan.borrower_user_id == borrower_user_id)
    if overdue_only:
        today_str = datetime.utcnow().strftime("%Y-%m-%d")
        stmt = stmt.where(LibraryLoan.status == LoanStatus.ACTIVE.value, LibraryLoan.due_date < today_str)
        count_stmt = count_stmt.where(LibraryLoan.status == LoanStatus.ACTIVE.value, LibraryLoan.due_date < today_str)

    total = (await session.execute(count_stmt)).scalar() or 0
    offset = (page - 1) * limit
    stmt = stmt.order_by(LibraryLoan.created_at.desc()).offset(offset).limit(limit)
    loans = (await session.execute(stmt)).scalars().all()
    items = [await _serialize_loan(session, loan) for loan in loans]

    return {"items": items, "total": total, "page": page, "limit": limit, "pages": (total + limit - 1) // limit if total else 0}


@router.get("/loans/mine", response_model=List[dict])
async def my_loans(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    school_id = await get_current_school_id(current_user)
    result = await session.execute(
        select(LibraryLoan)
        .where(LibraryLoan.school_id == school_id, LibraryLoan.borrower_user_id == current_user.id)
        .order_by(LibraryLoan.created_at.desc())
    )
    loans = result.scalars().all()
    return [await _serialize_loan(session, loan) for loan in loans]


# ---------------------------------------------------------------------------
# Fines
# ---------------------------------------------------------------------------

@router.post("/fines/{fine_id}/pay", response_model=dict)
async def pay_fine(
    fine_id: str,
    payload: LibraryFinePayment = LibraryFinePayment(),
    current_user: User = Depends(require_roles(*CIRCULATION_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = await get_current_school_id(current_user)
    fine = (
        await session.execute(select(LibraryFine).where(LibraryFine.id == fine_id, LibraryFine.school_id == school_id))
    ).scalar_one_or_none()
    if not fine:
        raise HTTPException(status_code=404, detail="Fine not found")
    if fine.status != FineStatus.PENDING.value:
        raise HTTPException(status_code=400, detail="This fine is not pending")

    try:
        payment_method = PaymentMethod(payload.payment_method)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid payment method")

    if fine.fee_id:
        fee = (await session.execute(select(Fee).where(Fee.id == fine.fee_id))).scalar_one_or_none()
        if fee:
            balance = fee.amount_due - fee.amount_paid - fee.discount
            if balance > 0:
                payment = FeePayment(
                    school_id=school_id,
                    fee_id=fee.id,
                    student_id=fee.student_id,
                    amount=balance,
                    payment_method=payment_method,
                    receipt_number=f"LIB-{datetime.utcnow().strftime('%Y%m%d')}-{str(uuid.uuid4())[:8].upper()}",
                    payment_date=datetime.utcnow().strftime("%Y-%m-%d"),
                    remarks=f"Library fine payment: {fine.reason}",
                    received_by=current_user.id,
                )
                session.add(payment)
                fee.amount_paid += balance
                fee.status = PaymentStatus.PAID.value
                fee.updated_at = datetime.utcnow()

    fine.status = FineStatus.PAID.value
    await session.commit()
    return {"id": fine.id, "status": fine.status}


@router.post("/fines/{fine_id}/waive", response_model=dict)
async def waive_fine(
    fine_id: str,
    payload: LibraryFineWaive,
    current_user: User = Depends(require_roles(*CIRCULATION_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = await get_current_school_id(current_user)
    fine = (
        await session.execute(select(LibraryFine).where(LibraryFine.id == fine_id, LibraryFine.school_id == school_id))
    ).scalar_one_or_none()
    if not fine:
        raise HTTPException(status_code=404, detail="Fine not found")
    if fine.status != FineStatus.PENDING.value:
        raise HTTPException(status_code=400, detail="This fine is not pending")

    if fine.fee_id:
        fee = (await session.execute(select(Fee).where(Fee.id == fine.fee_id))).scalar_one_or_none()
        if fee:
            remaining = fee.amount_due - fee.amount_paid - fee.discount
            if remaining > 0:
                fee.discount += remaining
                fee.discount_type = DiscountType.OTHER
                fee.discount_reason = f"Library fine waived: {payload.reason}"
                fee.status = PaymentStatus.PAID.value
                fee.updated_at = datetime.utcnow()

    fine.status = FineStatus.WAIVED.value
    fine.waived_by = current_user.id
    fine.waived_reason = payload.reason
    await session.commit()
    return {"id": fine.id, "status": fine.status}


@router.get("/fines", response_model=dict)
async def list_fines(
    status_filter: Optional[str] = Query(default=None, alias="status"),
    mine: bool = Query(default=False),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    school_id = await get_current_school_id(current_user)
    if not mine and current_user.role not in CIRCULATION_ROLES:
        raise HTTPException(status_code=403, detail="Not authorized")

    stmt = select(LibraryFine).join(LibraryLoan, LibraryLoan.id == LibraryFine.loan_id).where(LibraryFine.school_id == school_id)
    count_stmt = (
        select(func.count(LibraryFine.id)).join(LibraryLoan, LibraryLoan.id == LibraryFine.loan_id).where(LibraryFine.school_id == school_id)
    )
    if mine:
        stmt = stmt.where(LibraryLoan.borrower_user_id == current_user.id)
        count_stmt = count_stmt.where(LibraryLoan.borrower_user_id == current_user.id)
    if status_filter:
        stmt = stmt.where(LibraryFine.status == status_filter)
        count_stmt = count_stmt.where(LibraryFine.status == status_filter)

    total = (await session.execute(count_stmt)).scalar() or 0
    offset = (page - 1) * limit
    stmt = stmt.order_by(LibraryFine.created_at.desc()).offset(offset).limit(limit)
    fines = (await session.execute(stmt)).scalars().all()

    items = []
    for fine in fines:
        loan = (await session.execute(select(LibraryLoan).where(LibraryLoan.id == fine.loan_id))).scalar_one_or_none()
        serialized_loan = await _serialize_loan(session, loan) if loan else None
        items.append(
            {
                "id": fine.id,
                "loan_id": fine.loan_id,
                "amount": fine.amount,
                "reason": fine.reason,
                "fee_id": fine.fee_id,
                "status": fine.status,
                "waived_reason": fine.waived_reason,
                "created_at": fine.created_at,
                "borrower_name": serialized_loan["borrower_name"] if serialized_loan else None,
                "item_title": serialized_loan["item_title"] if serialized_loan else None,
            }
        )

    return {"items": items, "total": total, "page": page, "limit": limit, "pages": (total + limit - 1) // limit if total else 0}


@router.get("/reports/circulation", response_model=dict)
async def circulation_report(
    start_date: Optional[str] = Query(default=None),
    end_date: Optional[str] = Query(default=None),
    current_user: User = Depends(require_roles(*CIRCULATION_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    """The real circulation report the fines-only Analytics tab was
    missing: checkouts/returns over a period, an overdue list, a most-
    borrowed-titles ranking, and a day-by-day checkout trend."""
    school_id = await get_current_school_id(current_user)
    today = datetime.utcnow().date()
    start = start_date or (today - timedelta(days=30)).isoformat()
    end = end_date or today.isoformat()

    loans_in_range = (
        await session.execute(
            select(LibraryLoan).where(LibraryLoan.school_id == school_id, LibraryLoan.issue_date >= start, LibraryLoan.issue_date <= end)
        )
    ).scalars().all()
    returns_in_range = (
        await session.execute(
            select(func.count(LibraryLoan.id)).where(
                LibraryLoan.school_id == school_id, LibraryLoan.return_date.is_not(None),
                LibraryLoan.return_date >= start, LibraryLoan.return_date <= end,
            )
        )
    ).scalar() or 0
    active_loans = (
        await session.execute(
            select(func.count(LibraryLoan.id)).where(LibraryLoan.school_id == school_id, LibraryLoan.status == LoanStatus.ACTIVE.value)
        )
    ).scalar() or 0

    today_str = today.isoformat()
    overdue_loans = (
        await session.execute(
            select(LibraryLoan)
            .where(LibraryLoan.school_id == school_id, LibraryLoan.status == LoanStatus.ACTIVE.value, LibraryLoan.due_date < today_str)
            .order_by(LibraryLoan.due_date.asc())
        )
    ).scalars().all()
    overdue_list = [await _serialize_loan(session, loan) for loan in overdue_loans[:50]]

    checkouts_by_day: dict[str, int] = {}
    for loan in loans_in_range:
        checkouts_by_day[loan.issue_date] = checkouts_by_day.get(loan.issue_date, 0) + 1

    most_borrowed = []
    copy_ids = list({loan.copy_id for loan in loans_in_range})
    if copy_ids:
        item_by_copy = dict(
            (await session.execute(select(LibraryBookCopy.id, LibraryBookCopy.item_id).where(LibraryBookCopy.id.in_(copy_ids)))).all()
        )
        item_counts: dict[str, int] = {}
        for loan in loans_in_range:
            item_id = item_by_copy.get(loan.copy_id)
            if item_id:
                item_counts[item_id] = item_counts.get(item_id, 0) + 1
        top_item_ids = sorted(item_counts.items(), key=lambda kv: -kv[1])[:10]
        if top_item_ids:
            title_by_id = dict(
                (await session.execute(select(LibraryItem.id, LibraryItem.title).where(LibraryItem.id.in_([i for i, _ in top_item_ids])))).all()
            )
            most_borrowed = [{"item_id": item_id, "title": title_by_id.get(item_id, "Unknown"), "checkouts": count} for item_id, count in top_item_ids]

    return {
        "start_date": start,
        "end_date": end,
        "checkouts_total": len(loans_in_range),
        "returns_total": returns_in_range,
        "active_loans": active_loans,
        "overdue_count": len(overdue_loans),
        "overdue_loans": overdue_list,
        "checkouts_by_day": [{"date": d, "count": c} for d, c in sorted(checkouts_by_day.items())],
        "most_borrowed": most_borrowed,
    }


@router.get("/fines/summary", response_model=dict)
async def fines_summary(
    current_user: User = Depends(require_roles(*CIRCULATION_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = await get_current_school_id(current_user)
    fines = (await session.execute(select(LibraryFine).where(LibraryFine.school_id == school_id))).scalars().all()

    # A fine's own status can lag reality (see is_fine_outstanding's
    # docstring) — bucket by what's actually still owed, not the raw flag,
    # so a fine paid off through the general fee ledger doesn't keep
    # showing up as outstanding here.
    by_status: dict[str, dict] = {}
    pending = []
    for f in fines:
        effective_status = f.status
        if f.status == FineStatus.PENDING.value:
            if await is_fine_outstanding(session, f):
                pending.append(f)
            else:
                effective_status = FineStatus.PAID.value
        bucket = by_status.setdefault(effective_status, {"count": 0, "amount": 0.0})
        bucket["count"] += 1
        bucket["amount"] += f.amount
    by_status = {k: {"count": v["count"], "amount": round(v["amount"], 2)} for k, v in by_status.items()}
    borrower_totals: dict[str, dict] = {}
    for f in pending:
        loan = (await session.execute(select(LibraryLoan).where(LibraryLoan.id == f.loan_id))).scalar_one_or_none()
        if not loan:
            continue
        serialized = await _serialize_loan(session, loan)
        key = loan.borrower_user_id or "unknown"
        bucket = borrower_totals.setdefault(key, {"borrower_name": serialized["borrower_name"], "amount": 0.0})
        bucket["amount"] += f.amount
    top_borrowers = sorted(
        ({"borrower_name": v["borrower_name"], "amount": round(v["amount"], 2)} for v in borrower_totals.values()),
        key=lambda x: x["amount"], reverse=True
    )[:10]

    outstanding = by_status.get(FineStatus.PENDING.value, {"count": 0, "amount": 0.0})
    return {
        "outstanding_count": outstanding["count"],
        "outstanding_amount": outstanding["amount"],
        "by_status": by_status,
        "top_borrowers": top_borrowers,
    }


@router.get("/fine-settings", response_model=dict)
async def get_fine_settings(
    current_user: User = Depends(require_roles(*CIRCULATION_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = await get_current_school_id(current_user)
    settings = await get_or_create_fine_settings(session, school_id)
    await session.commit()
    return {
        "fine_per_day": settings.fine_per_day, "max_fine_days": settings.max_fine_days,
        "max_loans_student": settings.max_loans_student, "max_loans_staff": settings.max_loans_staff,
    }


@router.put("/fine-settings", response_model=dict)
async def update_fine_settings(
    payload: LibraryFineSettingsUpdate,
    current_user: User = Depends(require_roles(*SETTINGS_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = await get_current_school_id(current_user)
    settings = await get_or_create_fine_settings(session, school_id)
    if payload.fine_per_day is not None:
        if payload.fine_per_day < 0:
            raise HTTPException(status_code=422, detail="fine_per_day cannot be negative")
        settings.fine_per_day = payload.fine_per_day
    if payload.max_fine_days is not None:
        if payload.max_fine_days < 0:
            raise HTTPException(status_code=422, detail="max_fine_days cannot be negative")
        settings.max_fine_days = payload.max_fine_days
    if payload.max_loans_student is not None:
        if payload.max_loans_student < 1:
            raise HTTPException(status_code=422, detail="max_loans_student must be at least 1")
        settings.max_loans_student = payload.max_loans_student
    if payload.max_loans_staff is not None:
        if payload.max_loans_staff < 1:
            raise HTTPException(status_code=422, detail="max_loans_staff must be at least 1")
        settings.max_loans_staff = payload.max_loans_staff
    settings.updated_at = datetime.utcnow()
    await session.commit()
    return {
        "fine_per_day": settings.fine_per_day, "max_fine_days": settings.max_fine_days,
        "max_loans_student": settings.max_loans_student, "max_loans_staff": settings.max_loans_staff,
    }


# ---------------------------------------------------------------------------
# Reservations
# ---------------------------------------------------------------------------

@router.post("/reservations", response_model=dict, status_code=status.HTTP_201_CREATED)
async def create_reservation(
    payload: LibraryReservationCreate,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    school_id = await get_current_school_id(current_user)
    target_user_id = payload.user_id if (payload.user_id and current_user.role in CIRCULATION_ROLES) else current_user.id

    item = (
        await session.execute(select(LibraryItem).where(LibraryItem.id == payload.item_id, LibraryItem.school_id == school_id))
    ).scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Library item not found")

    existing = (
        await session.execute(
            select(LibraryReservation).where(
                LibraryReservation.school_id == school_id,
                LibraryReservation.item_id == payload.item_id,
                LibraryReservation.user_id == target_user_id,
                LibraryReservation.status == ReservationStatus.PENDING.value,
            )
        )
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail="You already have a pending reservation for this title")

    reservation = LibraryReservation(school_id=school_id, item_id=payload.item_id, user_id=target_user_id)
    session.add(reservation)
    await session.commit()
    await session.refresh(reservation)
    return _reservation_to_dict(reservation)


@router.delete("/reservations/{reservation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_reservation(
    reservation_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    school_id = await get_current_school_id(current_user)
    reservation = (
        await session.execute(
            select(LibraryReservation).where(LibraryReservation.id == reservation_id, LibraryReservation.school_id == school_id)
        )
    ).scalar_one_or_none()
    if not reservation:
        raise HTTPException(status_code=404, detail="Reservation not found")
    if reservation.user_id != current_user.id and current_user.role not in CIRCULATION_ROLES:
        raise HTTPException(status_code=403, detail="Not authorized")
    reservation.status = ReservationStatus.CANCELLED.value
    await session.commit()


@router.get("/reservations", response_model=List[dict])
async def list_reservations(
    item_id: Optional[str] = Query(default=None),
    mine: bool = Query(default=False),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    school_id = await get_current_school_id(current_user)
    stmt = select(LibraryReservation).where(
        LibraryReservation.school_id == school_id, LibraryReservation.status == ReservationStatus.PENDING.value
    )
    if item_id:
        stmt = stmt.where(LibraryReservation.item_id == item_id)
    if mine:
        stmt = stmt.where(LibraryReservation.user_id == current_user.id)
    elif current_user.role not in CIRCULATION_ROLES:
        stmt = stmt.where(LibraryReservation.user_id == current_user.id)

    result = await session.execute(stmt.order_by(LibraryReservation.reserved_at.asc()))
    return [_reservation_to_dict(r) for r in result.scalars().all()]
