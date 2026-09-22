"""External Exam Board Management Router"""
import csv
import io
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse
from sqlmodel import select, and_, func
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime
from typing import Optional, List

from models.exam_board import (
    ExamBoardRegistration, ExamBoardRegistrationCreate, ExamBoardRegistrationUpdate, ExamRegistrationStatus,
    BulkExamBoardRegistrationCreate, ExamBoardFeePayment, BulkIndexNumberImport,
    ExamBoardRegistrationSubject, ExamBoardResult, ExamBoardResultsSubmit, BulkResultsImport,
    ExamSeatingAssignment, ExamSeatingAssignmentCreate, ExamSeatingAssignmentUpdate,
    InvigilationDuty, InvigilationDutyCreate, InvigilationDutyUpdate,
)
from models.student import Student
from models.classroom import Subject
from models.school import AcademicTerm
from models.user import User
from models.fee import Fee, FeeStructure, FeePayment, PaymentStatus, PaymentMethod, FeeType
from database import get_session
from auth import get_current_user, require_permission
from services.hall_ticket_pdf_service import HallTicketPDFService

router = APIRouter(prefix="/exam-board", tags=["Exam Board"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REGISTRATION_STATUS_ORDER = [
    ExamRegistrationStatus.PENDING,
    ExamRegistrationStatus.SUBMITTED,
    ExamRegistrationStatus.CONFIRMED,
    ExamRegistrationStatus.INDEX_ISSUED,
    ExamRegistrationStatus.RESULTS_RECEIVED,
]


def _assert_valid_registration_status_transition(current: ExamRegistrationStatus, new: ExamRegistrationStatus) -> None:
    """update_registration lets a caller set registration_status directly with
    no guard at all — a registration could jump straight from PENDING to
    RESULTS_RECEIVED, or be moved backward out of INDEX_ISSUED after an index
    number was already issued. RESULTS_RECEIVED/CANCELLED are terminal;
    otherwise moves must follow the sequence above (CANCELLED is reachable
    from any non-terminal state)."""
    if current == new:
        return
    if current in (ExamRegistrationStatus.RESULTS_RECEIVED, ExamRegistrationStatus.CANCELLED):
        raise HTTPException(status_code=400, detail=f"Cannot change status once a registration is {current.value}")
    if new == ExamRegistrationStatus.CANCELLED:
        return
    if _REGISTRATION_STATUS_ORDER.index(new) < _REGISTRATION_STATUS_ORDER.index(current):
        raise HTTPException(status_code=400, detail=f"Cannot move registration status backward from {current.value} to {new.value}")


async def _assert_seat_available(
    session: AsyncSession, school_id: str, exam_name: str, exam_year: str,
    room: str, seat_number: str, exclude_assignment_id: Optional[str] = None,
) -> None:
    """ExamSeatingAssignment.registration_id is DB-unique (one seat per
    registration) but (room, seat_number) is not — nothing stopped two
    different registrations sharing the exact same physical seat. Scoped to
    the same exam sitting (exam_name+exam_year), since two different
    sittings can legitimately reuse the same room/seat on different dates."""
    query = select(ExamSeatingAssignment).join(
        ExamBoardRegistration, ExamBoardRegistration.id == ExamSeatingAssignment.registration_id
    ).where(
        ExamSeatingAssignment.school_id == school_id,
        ExamSeatingAssignment.room == room,
        ExamSeatingAssignment.seat_number == seat_number,
        ExamBoardRegistration.exam_name == exam_name,
        ExamBoardRegistration.exam_year == exam_year,
    )
    if exclude_assignment_id:
        query = query.where(ExamSeatingAssignment.id != exclude_assignment_id)
    if (await session.execute(query)).scalar_one_or_none():
        raise HTTPException(status_code=409, detail=f"Seat {seat_number} in room {room} is already assigned to another registration for this exam sitting")


async def _assert_index_number_available(
    session: AsyncSession, school_id: str, exam_name: str, exam_year: str,
    index_number: str, exclude_registration_id: Optional[str] = None,
) -> None:
    """ExamBoardRegistration.index_number has no DB-level uniqueness — the
    board's own official candidate identifier for a sitting, so two
    registrations silently sharing one is a genuine data-integrity break,
    not just cosmetic. Scoped to the same exam sitting (exam_name+exam_year)
    since index numbers are reissued fresh for each sitting."""
    query = select(ExamBoardRegistration).where(
        ExamBoardRegistration.school_id == school_id,
        ExamBoardRegistration.exam_name == exam_name,
        ExamBoardRegistration.exam_year == exam_year,
        ExamBoardRegistration.index_number == index_number,
    )
    if exclude_registration_id:
        query = query.where(ExamBoardRegistration.id != exclude_registration_id)
    if (await session.execute(query)).scalar_one_or_none():
        raise HTTPException(status_code=409, detail=f"Index number {index_number} is already assigned to another registration for this exam sitting")


async def _get_current_academic_term_id(session: AsyncSession, school_id: str) -> Optional[str]:
    result = await session.execute(
        select(AcademicTerm).where(AcademicTerm.school_id == school_id, AcademicTerm.is_current == True)  # noqa: E712
    )
    term = result.scalar_one_or_none()
    return term.id if term else None


async def _get_or_create_exam_board_fee_structure(session: AsyncSession, school_id: str, academic_term_id: str) -> FeeStructure:
    marker = "Exam board registration fees (system-managed)"
    existing = await session.execute(
        select(FeeStructure).where(
            FeeStructure.school_id == school_id,
            FeeStructure.academic_term_id == academic_term_id,
            FeeStructure.fee_type == FeeType.EXAMINATION,
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
        fee_type=FeeType.EXAMINATION,
        amount=0,
        description=marker,
        is_mandatory=False,
        due_date=datetime.utcnow().strftime("%Y-%m-%d"),
    )
    session.add(structure)
    await session.flush()
    return structure


async def _attach_fee(session: AsyncSession, registration: ExamBoardRegistration, amount: float) -> None:
    """Create the linked Fee for a registration's board fee, if one isn't
    already attached — lazy, since the fee amount can be set at creation or
    added later via an edit."""
    if registration.fee_id or amount <= 0:
        return
    term_id = await _get_current_academic_term_id(session, registration.school_id)
    if not term_id:
        return
    structure = await _get_or_create_exam_board_fee_structure(session, registration.school_id, term_id)
    fee = Fee(
        school_id=registration.school_id,
        student_id=registration.student_id,
        academic_term_id=term_id,
        fee_structure_id=structure.id,
        amount_due=amount,
        status=PaymentStatus.PENDING,
    )
    session.add(fee)
    await session.flush()
    registration.fee_id = fee.id


async def _sync_subjects(session: AsyncSession, school_id: str, registration: ExamBoardRegistration, subject_ids: List[str]) -> None:
    """Replace this registration's linked subjects with subject_ids, and
    recompute the display string the hall ticket template reads."""
    existing = await session.execute(
        select(ExamBoardRegistrationSubject).where(ExamBoardRegistrationSubject.registration_id == registration.id)
    )
    for link in existing.scalars().all():
        await session.delete(link)
    await session.flush()

    if not subject_ids:
        registration.subjects_registered = None
        return

    subjects_result = await session.execute(
        select(Subject).where(Subject.id.in_(subject_ids), Subject.school_id == school_id)
    )
    subjects = subjects_result.scalars().all()
    found_ids = {s.id for s in subjects}
    missing = [sid for sid in subject_ids if sid not in found_ids]
    if missing:
        raise HTTPException(status_code=400, detail=f"Unknown subject id(s): {', '.join(missing)}")

    for subject in subjects:
        session.add(ExamBoardRegistrationSubject(school_id=school_id, registration_id=registration.id, subject_id=subject.id))
    registration.subjects_registered = ", ".join(sorted(s.name for s in subjects))


@router.get("/registrations", response_model=List[dict])
async def list_registrations(
    exam_name: Optional[str] = None,
    exam_year: Optional[str] = None,
    status: Optional[ExamRegistrationStatus] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    current_user: User = Depends(require_permission("exam_board.registration.view")),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    query = select(ExamBoardRegistration).where(ExamBoardRegistration.school_id == school_id)
    if exam_name:
        query = query.where(ExamBoardRegistration.exam_name == exam_name)
    if exam_year:
        query = query.where(ExamBoardRegistration.exam_year == exam_year)
    if status:
        query = query.where(ExamBoardRegistration.registration_status == status)
    query = query.order_by(ExamBoardRegistration.created_at.desc()).offset(skip).limit(limit)

    result = await session.execute(query)
    return [jsonable_encoder(r) for r in result.scalars().all()]


@router.get("/registrations/analytics", response_model=dict)
async def registrations_analytics(
    exam_name: Optional[str] = None,
    exam_year: Optional[str] = None,
    current_user: User = Depends(require_permission("exam_board.registration.view")),
    session: AsyncSession = Depends(get_session)
):
    """Registered as a static path ahead of /registrations/{registration_id}
    — FastAPI matches routes in registration order, so this must stay
    before the dynamic route or 'analytics' gets swallowed as an id."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    query = select(ExamBoardRegistration).where(ExamBoardRegistration.school_id == school_id)
    if exam_name:
        query = query.where(ExamBoardRegistration.exam_name == exam_name)
    if exam_year:
        query = query.where(ExamBoardRegistration.exam_year == exam_year)
    registrations = (await session.execute(query)).scalars().all()

    by_status: dict = {}
    for r in registrations:
        status_key = r.registration_status.value if hasattr(r.registration_status, "value") else r.registration_status
        by_status[status_key] = by_status.get(status_key, 0) + 1

    fee_ids = [r.fee_id for r in registrations if r.fee_id]
    total_owed = 0.0
    total_collected = 0.0
    if fee_ids:
        fees = (await session.execute(select(Fee).where(Fee.id.in_(fee_ids)))).scalars().all()
        total_owed = round(sum(f.amount_due for f in fees), 2)
        total_collected = round(sum(f.amount_paid for f in fees), 2)

    registration_ids = [r.id for r in registrations]
    grade_distribution: dict = {}
    subjects_with_results = 0
    if registration_ids:
        results = (await session.execute(
            select(ExamBoardResult).where(ExamBoardResult.registration_id.in_(registration_ids))
        )).scalars().all()
        subjects_with_results = len(results)
        for r in results:
            key = r.grade or "Ungraded"
            grade_distribution[key] = grade_distribution.get(key, 0) + 1

    return {
        "total_registrations": len(registrations),
        "by_status": by_status,
        "fee": {
            "total_owed": total_owed,
            "total_collected": total_collected,
            "outstanding": round(total_owed - total_collected, 2),
        },
        "results": {
            "subjects_with_results": subjects_with_results,
            "grade_distribution": grade_distribution,
        },
    }


async def _check_duplicate_registration(session: AsyncSession, school_id: str, student_id: str, exam_name: str, exam_year: str) -> bool:
    existing = await session.execute(
        select(ExamBoardRegistration).where(
            ExamBoardRegistration.school_id == school_id,
            ExamBoardRegistration.student_id == student_id,
            ExamBoardRegistration.exam_name == exam_name,
            ExamBoardRegistration.exam_year == exam_year,
        )
    )
    return existing.scalar_one_or_none() is not None


@router.post("/registrations", response_model=dict)
async def create_registration(
    data: ExamBoardRegistrationCreate,
    current_user: User = Depends(require_permission("exam_board.registration.manage")),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    if await _check_duplicate_registration(session, school_id, data.student_id, data.exam_name, data.exam_year):
        raise HTTPException(status_code=409, detail="This student is already registered for this exam sitting")

    # index_number has no DB-level uniqueness (see _assert_index_number_available's
    # own docstring) -- update_registration already calls this check, but
    # create_registration skipped it, so two registrations could be created
    # sharing the same board-issued index number in one shot.
    if data.index_number:
        await _assert_index_number_available(session, school_id, data.exam_name, data.exam_year, data.index_number)

    payload = data.dict(exclude={"subject_ids"})
    registration = ExamBoardRegistration(**payload, school_id=school_id)
    session.add(registration)
    await session.flush()

    await _sync_subjects(session, school_id, registration, data.subject_ids)
    if data.registration_fee_amount:
        await _attach_fee(session, registration, data.registration_fee_amount)

    await session.commit()
    await session.refresh(registration)
    return jsonable_encoder(registration)


@router.post("/registrations/bulk", response_model=dict)
async def bulk_create_registrations(
    data: BulkExamBoardRegistrationCreate,
    current_user: User = Depends(require_permission("exam_board.registration.manage")),
    session: AsyncSession = Depends(get_session)
):
    """Register a whole cohort (e.g. a graduating class) for one exam
    sitting in a single call. Students already registered for this
    exam_name/exam_year are skipped rather than failing the whole batch."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    created = []
    skipped = []
    for student_id in data.student_ids:
        student = (await session.execute(
            select(Student).where(Student.id == student_id, Student.school_id == school_id)
        )).scalar_one_or_none()
        if not student:
            skipped.append({"student_id": student_id, "reason": "Student not found"})
            continue
        if await _check_duplicate_registration(session, school_id, student_id, data.exam_name, data.exam_year):
            skipped.append({"student_id": student_id, "reason": "Already registered for this sitting"})
            continue

        registration = ExamBoardRegistration(
            school_id=school_id,
            student_id=student_id,
            exam_name=data.exam_name,
            exam_year=data.exam_year,
            registration_fee_amount=data.registration_fee_amount,
            exam_center=data.exam_center,
            notes=data.notes,
        )
        session.add(registration)
        await session.flush()

        await _sync_subjects(session, school_id, registration, data.subject_ids)
        if data.registration_fee_amount:
            await _attach_fee(session, registration, data.registration_fee_amount)

        created.append(registration.id)

    await session.commit()
    return {"created": created, "skipped": skipped, "created_count": len(created), "skipped_count": len(skipped)}


@router.get("/registrations/{registration_id}", response_model=dict)
async def get_registration(
    registration_id: str,
    current_user: User = Depends(require_permission("exam_board.registration.view")),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(ExamBoardRegistration).where(
            and_(ExamBoardRegistration.id == registration_id, ExamBoardRegistration.school_id == school_id)
        )
    )
    registration = result.scalar_one_or_none()
    if not registration:
        raise HTTPException(status_code=404, detail="Registration not found")
    return jsonable_encoder(registration)


@router.put("/registrations/{registration_id}", response_model=dict)
async def update_registration(
    registration_id: str,
    data: ExamBoardRegistrationUpdate,
    current_user: User = Depends(require_permission("exam_board.registration.manage")),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(ExamBoardRegistration).where(
            and_(ExamBoardRegistration.id == registration_id, ExamBoardRegistration.school_id == school_id)
        )
    )
    registration = result.scalar_one_or_none()
    if not registration:
        raise HTTPException(status_code=404, detail="Registration not found")

    update_data = data.dict(exclude_unset=True)
    subject_ids = update_data.pop("subject_ids", None)
    if "registration_status" in update_data:
        _assert_valid_registration_status_transition(registration.registration_status, update_data["registration_status"])
    if update_data.get("index_number"):
        new_exam_name = update_data.get("exam_name", registration.exam_name)
        new_exam_year = update_data.get("exam_year", registration.exam_year)
        await _assert_index_number_available(
            session, school_id, new_exam_name, new_exam_year, update_data["index_number"], exclude_registration_id=registration.id,
        )
    for key, value in update_data.items():
        setattr(registration, key, value)
    registration.updated_at = datetime.utcnow()

    if subject_ids is not None:
        await _sync_subjects(session, school_id, registration, subject_ids)
    if registration.registration_fee_amount:
        await _attach_fee(session, registration, registration.registration_fee_amount)

    session.add(registration)
    await session.commit()
    await session.refresh(registration)
    return jsonable_encoder(registration)


@router.delete("/registrations/{registration_id}", response_model=dict)
async def delete_registration(
    registration_id: str,
    current_user: User = Depends(require_permission("exam_board.registration.manage")),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(ExamBoardRegistration).where(
            and_(ExamBoardRegistration.id == registration_id, ExamBoardRegistration.school_id == school_id)
        )
    )
    registration = result.scalar_one_or_none()
    if not registration:
        raise HTTPException(status_code=404, detail="Registration not found")

    await session.delete(registration)
    await session.commit()
    return {"message": "Registration deleted successfully", "id": registration_id}


@router.get("/registrations/{registration_id}/hall-ticket")
async def get_hall_ticket(
    registration_id: str,
    current_user: User = Depends(require_permission("exam_board.registration.pay")),
    session: AsyncSession = Depends(get_session)
):
    """Download the hall ticket for a registration as a PDF"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(ExamBoardRegistration).where(
            and_(ExamBoardRegistration.id == registration_id, ExamBoardRegistration.school_id == school_id)
        )
    )
    registration = result.scalar_one_or_none()
    if not registration:
        raise HTTPException(status_code=404, detail="Registration not found")

    student_result = await session.execute(
        select(Student).where(and_(Student.id == registration.student_id, Student.school_id == school_id))
    )
    student = student_result.scalar_one_or_none()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    from models.school import School
    school_result = await session.execute(select(School).where(School.id == school_id))
    school = school_result.scalar_one_or_none()

    seating_result = await session.execute(
        select(ExamSeatingAssignment).where(ExamSeatingAssignment.registration_id == registration_id)
    )
    seating = seating_result.scalar_one_or_none()

    data = {
        "school_name": school.name if school else "School",
        "student_name": f"{student.first_name} {student.last_name}",
        "student_id_code": student.student_id,
        "date_of_birth": student.date_of_birth,
        "gender": student.gender.value if hasattr(student.gender, "value") else student.gender,
        "exam_name": registration.exam_name,
        "exam_year": registration.exam_year,
        "index_number": registration.index_number,
        "subjects_registered": registration.subjects_registered,
        "exam_center": seating.room if seating else registration.exam_center,
        "seat_number": seating.seat_number if seating else None,
    }

    try:
        pdf_bytes = HallTicketPDFService().generate_pdf(data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to generate hall ticket PDF: {str(e)}")

    safe_name = data["student_name"].replace(" ", "_")
    filename = f"hall_ticket_{safe_name}_{registration.exam_name}_{registration.exam_year}.pdf".replace(" ", "_")

    return StreamingResponse(
        iter([pdf_bytes]),
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@router.post("/registrations/{registration_id}/pay-fee", response_model=dict)
async def pay_registration_fee(
    registration_id: str,
    payload: ExamBoardFeePayment = ExamBoardFeePayment(),
    current_user: User = Depends(require_permission("exam_board.registration.pay")),
    session: AsyncSession = Depends(get_session)
):
    """Record a real payment against the exam board fee owed for this
    registration — creates a FeePayment against the linked Fee rather than
    just flipping a boolean."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    registration = (await session.execute(
        select(ExamBoardRegistration).where(
            and_(ExamBoardRegistration.id == registration_id, ExamBoardRegistration.school_id == school_id)
        )
    )).scalar_one_or_none()
    if not registration:
        raise HTTPException(status_code=404, detail="Registration not found")
    if not registration.fee_id:
        raise HTTPException(status_code=400, detail="This registration has no fee to pay")

    # Locked FOR UPDATE: without it, two concurrent payment calls (a parent
    # double-tapping "pay", or two staff processing the same walk-in payment
    # at once) can both read the same stale balance, both pass the
    # amount <= balance check below, and both commit -- overpaying the fee.
    # Same lock already used for the identical read-then-update shape in
    # routers/fees.py's own fee-payment endpoint.
    fee = (await session.execute(select(Fee).where(Fee.id == registration.fee_id).with_for_update())).scalar_one_or_none()
    if not fee:
        raise HTTPException(status_code=404, detail="Linked fee not found")

    balance = round(fee.amount_due - fee.amount_paid - fee.discount, 2)
    if balance <= 0:
        raise HTTPException(status_code=400, detail="This fee is already fully paid")

    try:
        payment_method = PaymentMethod(payload.payment_method)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid payment method")

    amount = payload.amount if payload.amount is not None else balance
    if amount <= 0 or amount > balance:
        raise HTTPException(status_code=400, detail=f"Amount must be between 0 and the outstanding balance (GHS {balance})")

    payment = FeePayment(
        school_id=school_id,
        fee_id=fee.id,
        student_id=fee.student_id,
        amount=amount,
        payment_method=payment_method,
        receipt_number=f"EXB-{datetime.utcnow().strftime('%Y%m%d')}-{registration.id[:8].upper()}",
        payment_date=datetime.utcnow().strftime("%Y-%m-%d"),
        remarks=f"Exam board registration fee: {registration.exam_name} {registration.exam_year}",
        received_by=current_user.id,
    )
    session.add(payment)
    fee.amount_paid += amount
    fully_paid = round(fee.amount_due - fee.amount_paid - fee.discount, 2) <= 0
    fee.status = PaymentStatus.PAID.value if fully_paid else PaymentStatus.PARTIAL.value
    fee.updated_at = datetime.utcnow()

    registration.registration_fee_paid = fully_paid
    registration.updated_at = datetime.utcnow()
    session.add(fee)
    session.add(registration)
    await session.commit()

    return {
        "registration_id": registration.id, "fee_id": fee.id, "amount_paid": fee.amount_paid,
        "amount_due": fee.amount_due, "balance": round(fee.amount_due - fee.amount_paid - fee.discount, 2),
        "fully_paid": fully_paid,
    }


@router.post("/registrations/bulk-index-numbers", response_model=dict)
async def bulk_import_index_numbers(
    data: BulkIndexNumberImport,
    current_user: User = Depends(require_permission("exam_board.registration.manage")),
    session: AsyncSession = Depends(get_session)
):
    """Attach index numbers issued by the board back to registrations in
    bulk, matching on the school's own student_id code. csv_text lines look
    like 'student_id,index_number', a header row is tolerated and skipped."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    updated = []
    not_found = []
    seen_index_numbers = {}  # index_number -> student_code, for same-batch duplicate detection
    reader = csv.reader(io.StringIO(data.csv_text.strip()))
    for row in reader:
        if len(row) < 2:
            continue
        student_code, index_number = row[0].strip(), row[1].strip()
        if not student_code or student_code.lower() in ("student_id", "student id"):
            continue

        student = (await session.execute(
            select(Student).where(Student.school_id == school_id, Student.student_id == student_code)
        )).scalar_one_or_none()
        if not student:
            not_found.append({"student_id": student_code, "reason": "Student not found"})
            continue

        registration = (await session.execute(
            select(ExamBoardRegistration).where(
                ExamBoardRegistration.school_id == school_id,
                ExamBoardRegistration.student_id == student.id,
                ExamBoardRegistration.exam_name == data.exam_name,
                ExamBoardRegistration.exam_year == data.exam_year,
            )
        )).scalar_one_or_none()
        if not registration:
            not_found.append({"student_id": student_code, "reason": "No matching registration for this sitting"})
            continue

        if index_number:
            if index_number in seen_index_numbers:
                not_found.append({"student_id": student_code, "reason": f"index_number {index_number} duplicated in this import (already used for {seen_index_numbers[index_number]})"})
                continue
            try:
                await _assert_index_number_available(session, school_id, data.exam_name, data.exam_year, index_number, exclude_registration_id=registration.id)
            except HTTPException as exc:
                not_found.append({"student_id": student_code, "reason": exc.detail})
                continue
            seen_index_numbers[index_number] = student_code

        registration.index_number = index_number
        # Never regress a registration that's already further along (e.g.
        # results already received) back to index_issued.
        if registration.registration_status in (
            ExamRegistrationStatus.PENDING, ExamRegistrationStatus.SUBMITTED, ExamRegistrationStatus.CONFIRMED
        ):
            registration.registration_status = ExamRegistrationStatus.INDEX_ISSUED
        registration.updated_at = datetime.utcnow()
        session.add(registration)
        updated.append({"student_id": student_code, "registration_id": registration.id, "index_number": index_number})

    await session.commit()
    return {"updated": updated, "not_found": not_found, "updated_count": len(updated), "not_found_count": len(not_found)}


@router.get("/registrations/{registration_id}/subjects", response_model=List[dict])
async def list_registration_subjects(
    registration_id: str,
    current_user: User = Depends(require_permission("exam_board.registration.view")),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    registration = (await session.execute(
        select(ExamBoardRegistration).where(
            and_(ExamBoardRegistration.id == registration_id, ExamBoardRegistration.school_id == school_id)
        )
    )).scalar_one_or_none()
    if not registration:
        raise HTTPException(status_code=404, detail="Registration not found")

    links = (await session.execute(
        select(ExamBoardRegistrationSubject).where(ExamBoardRegistrationSubject.registration_id == registration_id)
    )).scalars().all()
    if not links:
        return []
    subject_ids = [link.subject_id for link in links]
    subjects = (await session.execute(select(Subject).where(Subject.id.in_(subject_ids)))).scalars().all()
    return [{"subject_id": s.id, "subject_name": s.name, "subject_code": s.code} for s in subjects]


# ============================================================================
# RESULTS
# ============================================================================

@router.get("/registrations/{registration_id}/results", response_model=List[dict])
async def list_results(
    registration_id: str,
    current_user: User = Depends(require_permission("exam_board.result.view")),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    registration = (await session.execute(
        select(ExamBoardRegistration).where(
            and_(ExamBoardRegistration.id == registration_id, ExamBoardRegistration.school_id == school_id)
        )
    )).scalar_one_or_none()
    if not registration:
        raise HTTPException(status_code=404, detail="Registration not found")

    results = (await session.execute(
        select(ExamBoardResult).where(ExamBoardResult.registration_id == registration_id)
    )).scalars().all()
    subject_ids = [r.subject_id for r in results]
    subjects = {}
    if subject_ids:
        subject_rows = (await session.execute(select(Subject).where(Subject.id.in_(subject_ids)))).scalars().all()
        subjects = {s.id: s.name for s in subject_rows}

    return [
        {**jsonable_encoder(r), "subject_name": subjects.get(r.subject_id, r.subject_id)}
        for r in results
    ]


@router.post("/registrations/{registration_id}/results", response_model=dict)
async def submit_results(
    registration_id: str,
    data: ExamBoardResultsSubmit,
    current_user: User = Depends(require_permission("exam_board.result.manage")),
    session: AsyncSession = Depends(get_session)
):
    """Record (or update) per-subject results for a registration. Once
    every subject the student was registered for has a result, the
    registration's status flips to results_received."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    registration = (await session.execute(
        select(ExamBoardRegistration).where(
            and_(ExamBoardRegistration.id == registration_id, ExamBoardRegistration.school_id == school_id)
        )
    )).scalar_one_or_none()
    if not registration:
        raise HTTPException(status_code=404, detail="Registration not found")
    if registration.registration_status in (ExamRegistrationStatus.PENDING, ExamRegistrationStatus.CANCELLED):
        raise HTTPException(status_code=400, detail=f"Cannot record results for a registration that is {registration.registration_status.value}")

    # ExamBoardResult.subject_id has no DB-level foreign key, so without this
    # any string is accepted and persisted -- corrupting registrations_analytics'
    # grade distribution and any subject-level reporting. bulk_import_results
    # (the CSV path) already validates via Subject.code; this JSON path didn't.
    if data.results:
        subject_ids = {entry.subject_id for entry in data.results}
        found_subject_ids = set((await session.execute(
            select(Subject.id).where(Subject.id.in_(subject_ids), Subject.school_id == school_id)
        )).scalars().all())
        if found_subject_ids != subject_ids:
            raise HTTPException(status_code=400, detail="One or more subject_id values do not exist for this school")

    for entry in data.results:
        existing = (await session.execute(
            select(ExamBoardResult).where(
                ExamBoardResult.registration_id == registration_id, ExamBoardResult.subject_id == entry.subject_id
            )
        )).scalar_one_or_none()
        if existing:
            existing.grade = entry.grade
            existing.score = entry.score
            existing.remarks = entry.remarks
            existing.updated_at = datetime.utcnow()
            session.add(existing)
        else:
            session.add(ExamBoardResult(
                school_id=school_id, registration_id=registration_id, subject_id=entry.subject_id,
                grade=entry.grade, score=entry.score, remarks=entry.remarks, recorded_by=current_user.id,
            ))

    await session.flush()
    await _maybe_mark_results_received(session, registration)
    await session.commit()
    await session.refresh(registration)
    return jsonable_encoder(registration)


async def _maybe_mark_results_received(session: AsyncSession, registration: ExamBoardRegistration) -> None:
    registered_subjects = (await session.execute(
        select(ExamBoardRegistrationSubject.subject_id).where(ExamBoardRegistrationSubject.registration_id == registration.id)
    )).scalars().all()
    if not registered_subjects:
        return
    # Only subjects with an actual grade or score count as "received" — a
    # blank ExamBoardResult row (e.g. from a results form submitted with
    # some subjects left empty) must not falsely flip the status.
    result_subjects = (await session.execute(
        select(ExamBoardResult.subject_id).where(
            ExamBoardResult.registration_id == registration.id,
            (ExamBoardResult.grade.is_not(None)) | (ExamBoardResult.score.is_not(None)),
        )
    )).scalars().all()
    if set(registered_subjects).issubset(set(result_subjects)):
        registration.registration_status = ExamRegistrationStatus.RESULTS_RECEIVED
        registration.updated_at = datetime.utcnow()
        session.add(registration)


@router.post("/results/bulk-import", response_model=dict)
async def bulk_import_results(
    data: BulkResultsImport,
    current_user: User = Depends(require_permission("exam_board.result.manage")),
    session: AsyncSession = Depends(get_session)
):
    """Bulk-record results the board releases as a spreadsheet. csv_text
    lines look like 'student_id,subject_code,grade,score', header tolerated."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    updated = []
    not_found = []
    touched_registrations = {}
    reader = csv.reader(io.StringIO(data.csv_text.strip()))
    for row in reader:
        if len(row) < 3:
            continue
        student_code, subject_code = row[0].strip(), row[1].strip()
        grade = row[2].strip() if len(row) > 2 else None
        score_raw = row[3].strip() if len(row) > 3 else None
        if not student_code or student_code.lower() in ("student_id", "student id"):
            continue

        student = (await session.execute(
            select(Student).where(Student.school_id == school_id, Student.student_id == student_code)
        )).scalar_one_or_none()
        if not student:
            not_found.append({"student_id": student_code, "subject_code": subject_code, "reason": "Student not found"})
            continue

        subject = (await session.execute(
            select(Subject).where(Subject.school_id == school_id, Subject.code == subject_code)
        )).scalar_one_or_none()
        if not subject:
            not_found.append({"student_id": student_code, "subject_code": subject_code, "reason": "Subject code not found"})
            continue

        registration = (await session.execute(
            select(ExamBoardRegistration).where(
                ExamBoardRegistration.school_id == school_id,
                ExamBoardRegistration.student_id == student.id,
                ExamBoardRegistration.exam_name == data.exam_name,
                ExamBoardRegistration.exam_year == data.exam_year,
            )
        )).scalar_one_or_none()
        if not registration:
            not_found.append({"student_id": student_code, "subject_code": subject_code, "reason": "No matching registration for this sitting"})
            continue
        if registration.registration_status in (ExamRegistrationStatus.PENDING, ExamRegistrationStatus.CANCELLED):
            not_found.append({"student_id": student_code, "subject_code": subject_code, "reason": f"Registration is {registration.registration_status.value}, cannot record results"})
            continue

        score = float(score_raw) if score_raw else None
        existing = (await session.execute(
            select(ExamBoardResult).where(
                ExamBoardResult.registration_id == registration.id, ExamBoardResult.subject_id == subject.id
            )
        )).scalar_one_or_none()
        if existing:
            existing.grade = grade or None
            existing.score = score
            existing.updated_at = datetime.utcnow()
            session.add(existing)
        else:
            session.add(ExamBoardResult(
                school_id=school_id, registration_id=registration.id, subject_id=subject.id,
                grade=grade or None, score=score, recorded_by=current_user.id,
            ))

        touched_registrations[registration.id] = registration
        updated.append({"student_id": student_code, "subject_code": subject_code, "grade": grade, "score": score})

    await session.flush()
    for registration in touched_registrations.values():
        await _maybe_mark_results_received(session, registration)
    await session.commit()

    return {"updated": updated, "not_found": not_found, "updated_count": len(updated), "not_found_count": len(not_found)}


# ============================================================================
# SEATING ASSIGNMENTS
# ============================================================================

@router.get("/seating", response_model=List[dict])
async def list_seating_assignments(
    exam_name: Optional[str] = None,
    exam_year: Optional[str] = None,
    current_user: User = Depends(require_permission("exam_board.seating.view")),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    query = select(ExamSeatingAssignment).where(ExamSeatingAssignment.school_id == school_id)
    if exam_name or exam_year:
        reg_query = select(ExamBoardRegistration.id).where(ExamBoardRegistration.school_id == school_id)
        if exam_name:
            reg_query = reg_query.where(ExamBoardRegistration.exam_name == exam_name)
        if exam_year:
            reg_query = reg_query.where(ExamBoardRegistration.exam_year == exam_year)
        query = query.where(ExamSeatingAssignment.registration_id.in_(reg_query))

    result = await session.execute(query)
    return [jsonable_encoder(s) for s in result.scalars().all()]


@router.post("/seating", response_model=dict)
async def assign_seating(
    data: ExamSeatingAssignmentCreate,
    current_user: User = Depends(require_permission("exam_board.seating.manage")),
    session: AsyncSession = Depends(get_session)
):
    """Assign a room/seat to an exam registration (one seat per registration)"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    reg_result = await session.execute(
        select(ExamBoardRegistration).where(
            and_(ExamBoardRegistration.id == data.registration_id, ExamBoardRegistration.school_id == school_id)
        )
    )
    registration = reg_result.scalar_one_or_none()
    if not registration:
        raise HTTPException(status_code=404, detail="Registration not found")

    existing_result = await session.execute(
        select(ExamSeatingAssignment).where(ExamSeatingAssignment.registration_id == data.registration_id)
    )
    if existing_result.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="This registration already has a seat assignment")

    # (room, seat_number) has no DB-level uniqueness — check for a collision
    # within the same exam sitting (exam_name+exam_year), since two different
    # sittings can legitimately reuse the same room/seat on different dates.
    await _assert_seat_available(session, school_id, registration.exam_name, registration.exam_year, data.room, data.seat_number)

    assignment = ExamSeatingAssignment(**data.dict(), school_id=school_id)
    session.add(assignment)
    await session.commit()
    await session.refresh(assignment)
    return jsonable_encoder(assignment)


@router.put("/seating/{assignment_id}", response_model=dict)
async def update_seating_assignment(
    assignment_id: str,
    data: ExamSeatingAssignmentUpdate,
    current_user: User = Depends(require_permission("exam_board.seating.manage")),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(ExamSeatingAssignment).where(
            and_(ExamSeatingAssignment.id == assignment_id, ExamSeatingAssignment.school_id == school_id)
        )
    )
    assignment = result.scalar_one_or_none()
    if not assignment:
        raise HTTPException(status_code=404, detail="Seating assignment not found")

    update_data = data.dict(exclude_unset=True)
    new_room = update_data.get("room", assignment.room)
    new_seat_number = update_data.get("seat_number", assignment.seat_number)
    if (new_room, new_seat_number) != (assignment.room, assignment.seat_number):
        registration = await session.get(ExamBoardRegistration, assignment.registration_id)
        await _assert_seat_available(session, school_id, registration.exam_name, registration.exam_year, new_room, new_seat_number, exclude_assignment_id=assignment.id)

    for key, value in update_data.items():
        setattr(assignment, key, value)
    assignment.updated_at = datetime.utcnow()

    session.add(assignment)
    await session.commit()
    await session.refresh(assignment)
    return jsonable_encoder(assignment)


@router.delete("/seating/{assignment_id}", response_model=dict)
async def delete_seating_assignment(
    assignment_id: str,
    current_user: User = Depends(require_permission("exam_board.seating.manage")),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(ExamSeatingAssignment).where(
            and_(ExamSeatingAssignment.id == assignment_id, ExamSeatingAssignment.school_id == school_id)
        )
    )
    assignment = result.scalar_one_or_none()
    if not assignment:
        raise HTTPException(status_code=404, detail="Seating assignment not found")

    await session.delete(assignment)
    await session.commit()
    return {"message": "Seating assignment deleted successfully", "id": assignment_id}


# ============================================================================
# INVIGILATION DUTIES
# ============================================================================

@router.get("/invigilation", response_model=List[dict])
async def list_invigilation_duties(
    exam_name: Optional[str] = None,
    exam_year: Optional[str] = None,
    current_user: User = Depends(require_permission("exam_board.invigilation.view")),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    query = select(InvigilationDuty).where(InvigilationDuty.school_id == school_id)
    if exam_name:
        query = query.where(InvigilationDuty.exam_name == exam_name)
    if exam_year:
        query = query.where(InvigilationDuty.exam_year == exam_year)
    query = query.order_by(InvigilationDuty.duty_date)

    result = await session.execute(query)
    return [jsonable_encoder(d) for d in result.scalars().all()]


@router.post("/invigilation", response_model=dict)
async def create_invigilation_duty(
    data: InvigilationDutyCreate,
    current_user: User = Depends(require_permission("exam_board.invigilation.manage")),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    duty = InvigilationDuty(**data.dict(), school_id=school_id)
    session.add(duty)
    await session.commit()
    await session.refresh(duty)
    return jsonable_encoder(duty)


@router.put("/invigilation/{duty_id}", response_model=dict)
async def update_invigilation_duty(
    duty_id: str,
    data: InvigilationDutyUpdate,
    current_user: User = Depends(require_permission("exam_board.invigilation.manage")),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(InvigilationDuty).where(and_(InvigilationDuty.id == duty_id, InvigilationDuty.school_id == school_id))
    )
    duty = result.scalar_one_or_none()
    if not duty:
        raise HTTPException(status_code=404, detail="Invigilation duty not found")

    update_data = data.dict(exclude_unset=True)
    for key, value in update_data.items():
        setattr(duty, key, value)

    session.add(duty)
    await session.commit()
    await session.refresh(duty)
    return jsonable_encoder(duty)


@router.delete("/invigilation/{duty_id}", response_model=dict)
async def delete_invigilation_duty(
    duty_id: str,
    current_user: User = Depends(require_permission("exam_board.invigilation.manage")),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(InvigilationDuty).where(and_(InvigilationDuty.id == duty_id, InvigilationDuty.school_id == school_id))
    )
    duty = result.scalar_one_or_none()
    if not duty:
        raise HTTPException(status_code=404, detail="Invigilation duty not found")

    await session.delete(duty)
    await session.commit()
    return {"message": "Invigilation duty deleted successfully", "id": duty_id}
