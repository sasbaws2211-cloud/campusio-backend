"""Students router"""
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status, Query, UploadFile, File
from sqlmodel import select, func, SQLModel
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime
from pathlib import Path
from typing import List, Optional
import re
import secrets
import uuid
from models.student import (
    Student, StudentCreate, StudentStatus, Parent, ParentCreate, StudentParent, StudentEnrollment,
    StudentExitRequest, StudentStatusEvent, StudentDeactivateRequest, StudentReEnrollRequest,
    StudentSibling, StudentSiblingCreate, EmergencyContact, EmergencyContactCreate, EmergencyContactUpdate,
    TransferRequest, TransferRequestCreate, TransferRequestUpdate, CustodyType, ParentCustodyUpdate,
)
from models.classroom import Class, ClassWaitlistEntry
from models.school import School, AcademicTerm
from models.user import User, UserRole
from models.otp import OTPSettings, OTP
from models.fee import Fee
from models.library_circulation import LibraryLoan, LibraryFine, LoanStatus, FineStatus
from models.hostel import StudentHostel, StudentHostelStatus, RoomAllocation, Room
from models.transport import StudentTransport
from database import get_session
from auth import get_current_user, require_roles, get_password_hash, encrypt_onboarding_password, decrypt_onboarding_password
from dependencies import resolve_campus_scope, resolve_write_campus_id, assert_campus_access
from services.csv_import_service import CSVImportService
from services.email_service import email_service
from services.document_service import MAX_FILE_SIZE_BYTES
from services.library_fine_service import is_fine_outstanding

router = APIRouter(prefix="/students", tags=["Students"])

PHOTO_UPLOAD_DIR = Path("uploads/students")
ALLOWED_PHOTO_CONTENT_TYPES = {"image/jpeg", "image/png"}


class AssignStudentClassRequest(SQLModel):
    class_id: str


class PromotionAction(SQLModel):
    student_id: str
    target_class_id: Optional[str] = None  # move the student into this class
    status: Optional[StudentStatus] = None  # or close out their time at the school


class BulkPromoteRequest(SQLModel):
    actions: List[PromotionAction]


class LinkParentRequest(SQLModel):
    """Optional body for POST /parents/{parent_id}/link — kept optional so
    existing callers that link without specifying custody keep working; a
    missing/blank custody_type falls back to StudentParent.custody_type's
    own "guardian" default."""
    custody_type: Optional[str] = None


async def _add_to_waitlist(session: AsyncSession, school_id: str, class_id: str, student_id: str) -> ClassWaitlistEntry:
    """Create a ClassWaitlistEntry at the back of the queue for this class.
    Shared by check_class_capacity(auto_waitlist=True) and the bulk-promotion
    endpoint's own except-branch waitlisting below."""
    max_position_result = await session.execute(
        select(func.max(ClassWaitlistEntry.position)).where(ClassWaitlistEntry.class_id == class_id)
    )
    next_position = (max_position_result.scalar() or 0) + 1
    entry = ClassWaitlistEntry(school_id=school_id, student_id=student_id, class_id=class_id, position=next_position)
    session.add(entry)
    await session.flush()
    return entry


async def check_class_capacity(
    session: AsyncSession, school_id: str, class_id: str, exclude_student_id: Optional[str] = None,
    auto_waitlist: bool = False, student_id: Optional[str] = None,
) -> Optional[ClassWaitlistEntry]:
    """Reject assigning a student into a class that's already at capacity.
    exclude_student_id excludes the student's own existing row from the count,
    so re-saving a student who's already in the class at exactly capacity doesn't
    falsely block.

    school_id scopes the class lookup to the caller's own school — without
    it, any caller who can guess/leak a class UUID from a different school
    (e.g. via convert_applicant's free-text class_id) could get a student
    assigned into another tenant's class entirely, since Class ids aren't
    otherwise guarded against cross-school use here.

    When auto_waitlist=True and the class is full, instead of raising this
    creates a ClassWaitlistEntry for student_id (required in that case) and
    returns it — callers must NOT set the student's class_id when a waitlist
    entry comes back. Default behavior (auto_waitlist=False) is completely
    unchanged: still raises HTTPException(400) and returns None otherwise.

    Locked FOR UPDATE: without it, two concurrent callers (two admins
    enrolling different students at once, or a manual enrollment racing a
    rollover promotion) can both read the same "39/40" count before either
    commits their INSERT, and both pass this check — the class ends up over
    capacity with no record of the overshoot (unlike the auto_waitlist path,
    which only fires when the *stale* read already showed "full")."""
    class_result = await session.execute(select(Class).where(Class.id == class_id, Class.school_id == school_id).with_for_update())
    cls = class_result.scalar_one_or_none()
    if not cls:
        raise HTTPException(status_code=404, detail="Class not found")

    # active only — a student's class_id is never cleared when they exit
    # (graduate/transfer/withdraw/get expelled), so an unfiltered count here
    # can falsely report a class as "full" and block/waitlist a new student
    # from a class that actually has room.
    count_query = select(func.count(Student.id)).where(Student.class_id == class_id, Student.status == StudentStatus.ACTIVE)
    if exclude_student_id:
        count_query = count_query.where(Student.id != exclude_student_id)
    count_result = await session.execute(count_query)
    current_count = count_result.scalar() or 0

    if current_count >= cls.capacity:
        if auto_waitlist:
            if not student_id:
                raise HTTPException(status_code=400, detail="student_id is required to auto-waitlist")
            return await _add_to_waitlist(session, cls.school_id, class_id, student_id)
        raise HTTPException(
            status_code=400,
            detail=f"{cls.name} is at capacity ({cls.capacity} students)"
        )
    return None


async def _get_current_term_id(session: AsyncSession, school_id: str) -> Optional[str]:
    result = await session.execute(
        select(AcademicTerm).where(AcademicTerm.school_id == school_id, AcademicTerm.is_current == True)  # noqa: E712
    )
    term = result.scalar_one_or_none()
    return term.id if term else None


async def _close_open_enrollments(session: AsyncSession, student_id: str, reason: str) -> None:
    """End whatever enrollment row is currently open for this student (if
    any) — used both when a student moves to a new class and when they
    leave the school without one (graduated/transferred/withdrawn)."""
    result = await session.execute(
        select(StudentEnrollment).where(
            StudentEnrollment.student_id == student_id,
            StudentEnrollment.ended_at.is_(None)
        )
    )
    for enrollment in result.scalars().all():
        enrollment.ended_at = datetime.utcnow()
        enrollment.ended_reason = reason
        session.add(enrollment)


async def _release_hostel_and_transport(session: AsyncSession, student: Student, new_status: StudentStatus) -> None:
    """When a student's status moves away from ACTIVE — exit, bulk status
    change, or a general profile edit that changes status — release any
    hostel accommodation, room allocation, or transport enrollment they
    still hold. Without this, a bed/seat stays phantom-occupied forever:
    the exact same root cause as the class_id-never-cleared bug already
    fixed for class rosters (routers/classes.py, check_class_capacity),
    just unfixed in these two other modules — and _compute_exit_clearance's
    own hostel/transport checks would keep reporting this student as still
    active on every future exit-clearance lookup for anyone else."""
    if new_status == StudentStatus.ACTIVE:
        return

    released_hostel_status = {
        StudentStatus.GRADUATED: StudentHostelStatus.GRADUATED,
        StudentStatus.TRANSFERRED: StudentHostelStatus.TRANSFERRED,
    }.get(new_status, StudentHostelStatus.INACTIVE)  # WITHDRAWN/EXPELLED have no dedicated member

    hostel_result = await session.execute(
        select(StudentHostel).where(
            StudentHostel.school_id == student.school_id,
            StudentHostel.student_id == student.id,
            StudentHostel.status == StudentHostelStatus.ACTIVE,
        )
    )
    accommodation = hostel_result.scalar_one_or_none()
    if accommodation:
        if accommodation.room_id:
            room = await session.get(Room, accommodation.room_id)
            if room and room.current_occupancy > 0:
                room.current_occupancy -= 1
                session.add(room)
        accommodation.status = released_hostel_status
        if not accommodation.check_out_date:
            accommodation.check_out_date = datetime.utcnow().strftime("%Y-%m-%d")
        accommodation.updated_at = datetime.utcnow()
        session.add(accommodation)

    allocation_result = await session.execute(
        select(RoomAllocation).where(
            RoomAllocation.school_id == student.school_id,
            RoomAllocation.student_id == student.id,
            RoomAllocation.deallocation_date.is_(None),
        )
    )
    for allocation in allocation_result.scalars().all():
        room = await session.get(Room, allocation.room_id)
        if room and room.current_occupancy > 0:
            room.current_occupancy -= 1
            session.add(room)
        allocation.deallocation_date = datetime.utcnow().strftime("%Y-%m-%d")
        session.add(allocation)

    transport_result = await session.execute(
        select(StudentTransport).where(
            StudentTransport.school_id == student.school_id,
            StudentTransport.student_id == student.id,
            StudentTransport.is_active == True,  # noqa: E712
        )
    )
    for enrollment in transport_result.scalars().all():
        enrollment.is_active = False
        enrollment.updated_at = datetime.utcnow()
        session.add(enrollment)


async def _compute_exit_clearance(session: AsyncSession, school_id: str, student: Student) -> dict:
    """Aggregate the domains that should be settled before a student leaves.
    Stock/asset issuance is deliberately excluded — StockIssuance.issued_to
    is free text with no real FK to Student, so it can't be checked reliably."""
    fee_result = await session.execute(
        select(Fee).where(Fee.school_id == school_id, Fee.student_id == student.id)
    )
    outstanding_balance = round(sum(f.amount_due - f.discount - f.amount_paid for f in fee_result.scalars().all()), 2)

    active_loans = 0
    unpaid_fines = 0.0
    if student.user_id:
        loan_result = await session.execute(
            select(LibraryLoan).where(
                LibraryLoan.school_id == school_id,
                LibraryLoan.borrower_user_id == student.user_id,
                LibraryLoan.status == LoanStatus.ACTIVE.value,
            )
        )
        active_loans = len(loan_result.scalars().all())

        # Candidate PENDING fines, then filtered via is_fine_outstanding —
        # LibraryFine.status alone can't be trusted here: a fine paid off
        # through the general fee ledger (not the library module's own
        # pay_fine) never gets its status flipped, which would otherwise
        # block this student's exit clearance over a fine that's actually
        # already settled.
        fine_result = await session.execute(
            select(LibraryFine).join(LibraryLoan, LibraryFine.loan_id == LibraryLoan.id).where(
                LibraryLoan.school_id == school_id,
                LibraryLoan.borrower_user_id == student.user_id,
                LibraryFine.status == FineStatus.PENDING.value,
            )
        )
        unpaid_fines = 0.0
        for f in fine_result.scalars().all():
            if await is_fine_outstanding(session, f):
                unpaid_fines += f.amount
        unpaid_fines = round(unpaid_fines, 2)

    hostel_result = await session.execute(
        select(StudentHostel).where(
            StudentHostel.school_id == school_id,
            StudentHostel.student_id == student.id,
            StudentHostel.status == StudentHostelStatus.ACTIVE,
        )
    )
    active_hostel = hostel_result.scalar_one_or_none() is not None

    transport_result = await session.execute(
        select(StudentTransport).where(
            StudentTransport.school_id == school_id,
            StudentTransport.student_id == student.id,
            StudentTransport.is_active == True,  # noqa: E712
        )
    )
    active_transport = transport_result.scalar_one_or_none() is not None

    return {
        "fees": {"clear": outstanding_balance <= 0, "outstanding_balance": outstanding_balance},
        "library": {"clear": active_loans == 0 and unpaid_fines <= 0, "active_loans": active_loans, "unpaid_fines": unpaid_fines},
        "hostel": {"clear": not active_hostel, "active_allocation": active_hostel},
        "transport": {"clear": not active_transport, "active_enrollment": active_transport},
    }


async def _record_enrollment(
    session: AsyncSession,
    school_id: str,
    student_id: str,
    class_id: str,
    reason: str
) -> None:
    """Close out the student's current open enrollment row (if any) and open
    a new one in the given class, for the school's current academic term.
    Silently skipped if the school has no current term configured yet —
    matches how Grade/Attendance already tolerate a missing current term
    elsewhere, rather than blocking the class-assignment action over it."""
    term_id = await _get_current_term_id(session, school_id)
    if not term_id:
        return

    await _close_open_enrollments(session, student_id, reason)
    session.add(StudentEnrollment(
        school_id=school_id,
        student_id=student_id,
        class_id=class_id,
        academic_term_id=term_id,
    ))


def _normalize_name(name: str) -> str:
    cleaned = name.strip().lower()
    cleaned = re.sub(r'[^a-z0-9]+', '', cleaned)
    return cleaned


async def _delete_user_with_otp_cleanup(user_id: str, session: AsyncSession):
    """Delete a user and its related OTP records to avoid foreign key violations"""
    # Delete OTP settings for this user
    otp_settings_result = await session.execute(
        select(OTPSettings).where(OTPSettings.user_id == user_id)
    )
    otp_settings = otp_settings_result.scalars().all()
    for otp_setting in otp_settings:
        await session.delete(otp_setting)
    
    # Delete OTP codes for this user
    otp_result = await session.execute(
        select(OTP).where(OTP.user_id == user_id)
    )
    otp_codes = otp_result.scalars().all()
    for otp_code in otp_codes:
        await session.delete(otp_code)
    
    # Delete the user
    user_result = await session.execute(select(User).where(User.id == user_id))
    user = user_result.scalar_one_or_none()
    if user:
        await session.delete(user)


async def _get_school_short_code(school_id: str, session: AsyncSession) -> str:
    if not school_id:
        return 'school'
    result = await session.execute(select(School).where(School.id == school_id))
    school = result.scalar_one_or_none()
    if not school or not getattr(school, 'code', None):
        return 'school'
    return str(school.code).strip().lower()


async def _create_portal_user(
    first_name: str,
    last_name: str,
    school_id: str,
    role: UserRole,
    session: AsyncSession
):
    from sqlalchemy.exc import IntegrityError
    
    school_short = await _get_school_short_code(school_id, session)
    base_local_part = f"{_normalize_name(first_name)}.{_normalize_name(last_name)}"

    email = f"{base_local_part}@{school_short}.school.edu.gh"
    suffix = 1

    while True:
        user_exists = await session.execute(select(User).where(User.email == email))
        if not user_exists.scalar_one_or_none():
            break
        email = f"{base_local_part}{suffix}@{school_short}.school.edu.gh"
        suffix += 1

    password = secrets.token_urlsafe(10)
    user = User(
        email=email,
        password_hash=get_password_hash(password),
        plain_text_password=encrypt_onboarding_password(password),
        first_name=first_name,
        last_name=last_name,
        role=role,
        school_id=school_id,
        is_active=True,
        must_change_password=True
    )

    try:
        session.add(user)
        await session.flush()
    except IntegrityError:
        # Handle race condition - email already exists, fetch it instead
        await session.rollback()
        result = await session.execute(select(User).where(User.email == email))
        existing_user = result.scalar_one_or_none()
        if existing_user:
            return existing_user, decrypt_onboarding_password(existing_user.plain_text_password) or "N/A"
        # If still not found, try with suffix
        email = f"{base_local_part}{suffix}@{school_short}.school.edu.gh"
        user.email = email
        session.add(user)
        await session.flush()

    return user, password


@router.post("", response_model=dict)
async def create_student(
    student_data: StudentCreate,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Create a new student"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=400, detail="No school context")

    student_data.campus_id = resolve_write_campus_id(current_user, student_data.campus_id)

    if not student_data.student_id:
        student_data.student_id = f"STU-{datetime.now().year}-{secrets.token_hex(3).upper()}"

    result = await session.execute(
        select(Student).where(
            Student.school_id == school_id,
            Student.student_id == student_data.student_id
        )
    )
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Student ID already exists")

    requested_class_id = student_data.class_id
    waitlist_entry = None
    if requested_class_id:
        # Don't set class_id yet — capacity is only checked (and, if full,
        # the waitlist entry created) after the student row exists below.
        student_data.class_id = None

    student = Student(school_id=school_id, **student_data.model_dump())
    session.add(student)
    await session.flush()

    if requested_class_id:
        waitlist_entry = await check_class_capacity(session, school_id, requested_class_id, auto_waitlist=True, student_id=student.id)
        if waitlist_entry is None:
            student.class_id = requested_class_id
            session.add(student)
            await _record_enrollment(session, school_id, student.id, requested_class_id, reason="admitted")

    portal_user, portal_password = await _create_portal_user(
        first_name=student.first_name,
        last_name=student.last_name,
        school_id=school_id,
        role=UserRole.STUDENT,
        session=session
    )

    student.user_id = portal_user.id
    session.add(student)
    await session.commit()
    await session.refresh(student)

    from services.webhook_service import emit_event
    await emit_event(
        session, background_tasks, school_id, "student.created",
        {
            "id": student.id,
            "student_id": student.student_id,
            "first_name": student.first_name,
            "last_name": student.last_name,
            "class_id": student.class_id,
        },
    )

    response = {
        "id": student.id,
        "school_id": student.school_id,
        "student_id": student.student_id,
        "first_name": student.first_name,
        "last_name": student.last_name,
        "status": student.status,
        "created_at": student.created_at.isoformat(),
        "portal_account": {
            "email": portal_user.email,
            "password": portal_password
        }
    }
    # Additive — the account is always created; these two fields just flag
    # that the requested class was full and the student was queued instead
    # of enrolled directly. (See check_class_capacity's auto_waitlist docstring.)
    if waitlist_entry:
        response["waitlisted"] = True
        response["position"] = waitlist_entry.position
    return response


@router.post("/import", response_model=dict)
async def import_students_csv(
    file: UploadFile = File(...),
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Import students from CSV file"""
    if not file.filename.endswith('.csv'):
        raise HTTPException(status_code=400, detail="File must be a CSV file")
    
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=400, detail="No school context")
    
    # Read file content
    content = await file.read()
    
    # Import students
    import_service = CSVImportService(session, school_id, current_user)
    result = await import_service.import_students(content) 

    # 🔹 DEBUG: Print all row-level errors
    print("CSV Import Errors:", result.get("errors"))
    
    if not result['success'] and result['success_count'] == 0:
        raise HTTPException(status_code=400, detail=result['message'])
    
    return result


@router.get("", response_model=dict)
async def list_students(
    class_id: Optional[str] = None,
    campus_id: Optional[str] = None,
    status: Optional[StudentStatus] = None,
    search: Optional[str] = None,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """List students with pagination.

    STUDENT was previously able to page/search the entire school's student
    directory (names, DOB, photos) with no restriction — that has no known
    legitimate use case and is now rejected outright. PARENT was ALSO able
    to browse the entire school (the frontend's ParentQRPage passes
    parent_id as a filter, but this endpoint never implemented it — a
    parent got back every student in the school, not just their own
    children, and the page happened to work anyway only because the
    dev/demo school has few students). PARENT is now force-scoped to their
    own linked children via get_parent_children_ids, fixing both the
    leak and that latent bug in the same change. Every other (staff) role
    keeps its existing unrestricted school-wide access."""
    if current_user.role == UserRole.STUDENT:
        raise HTTPException(status_code=403, detail="Access denied")

    school_id = current_user.school_id
    if not school_id and current_user.role != UserRole.SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="No school context")

    query = select(Student)
    count_query = select(func.count(Student.id))

    if current_user.role == UserRole.PARENT:
        from routers.parent import get_parent_children_ids
        child_ids = await get_parent_children_ids(current_user, session)
        if not child_ids:
            return {"items": [], "total": 0, "page": page, "limit": limit}
        query = query.where(Student.id.in_(child_ids))
        count_query = count_query.where(Student.id.in_(child_ids))

    if school_id:
        query = query.where(Student.school_id == school_id)
        count_query = count_query.where(Student.school_id == school_id)

    if class_id:
        query = query.where(Student.class_id == class_id)
        count_query = count_query.where(Student.class_id == class_id)

    campus_id = resolve_campus_scope(current_user, campus_id)
    if campus_id:
        query = query.where(Student.campus_id == campus_id)
        count_query = count_query.where(Student.campus_id == campus_id)

    if status:
        query = query.where(Student.status == status)
        count_query = count_query.where(Student.status == status)
    
    if search:
        search_filter = (
            Student.first_name.ilike(f"%{search}%") |
            Student.last_name.ilike(f"%{search}%") |
            Student.student_id.ilike(f"%{search}%")
        )
        query = query.where(search_filter)
        count_query = count_query.where(search_filter)
    
    total_result = await session.execute(count_query)
    total = total_result.scalar()
    
    offset = (page - 1) * limit
    query = query.offset(offset).limit(limit).order_by(Student.first_name)
    
    result = await session.execute(query)
    students = result.scalars().all()
    
    class_ids = [s.class_id for s in students if s.class_id]
    class_names = {}
    if class_ids:
        class_result = await session.execute(select(Class).where(Class.id.in_(class_ids)))
        for c in class_result.scalars().all():
            class_names[c.id] = c.name
    
    return {
        "items": [
            {
                "id": s.id,
                "school_id": s.school_id,
                "student_id": s.student_id,
                "first_name": s.first_name,
                "last_name": s.last_name,
                "full_name": f"{s.first_name} {s.last_name}",
                "date_of_birth": s.date_of_birth,
                "gender": s.gender,
                "admission_date": s.admission_date,
                "class_id": s.class_id,
                "class_name": class_names.get(s.class_id, "Unassigned"),
                "campus_id": s.campus_id,
                "status": s.status,
                "photo_url": s.photo_url
            }
            for s in students
        ],
        "total": total,
        "page": page,
        "limit": limit,
        "pages": (total + limit - 1) // limit
    }


@router.post("/promote", response_model=dict)
async def promote_students(
    body: BulkPromoteRequest,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Year-end bulk promotion. Each action either moves a student into a new
    class (promotion, or repeat-in-place by targeting their current class) or
    closes out their time at the school (graduated/transferred/withdrawn); if
    both are given, the class move wins and status is reset to active, since
    that's what "promoted" means. Actions are applied independently: a bad row
    (missing student, class at capacity, wrong school) is recorded as an error
    and skipped rather than aborting the whole batch, the same idempotent-bulk
    pattern used by attendance."""
    results = {"promoted": [], "status_changed": [], "errors": []}

    for action in body.actions:
        if not action.target_class_id and not action.status:
            results["errors"].append({
                "student_id": action.student_id,
                "error": "Provide target_class_id or status"
            })
            continue

        result = await session.execute(select(Student).where(Student.id == action.student_id))
        student = result.scalar_one_or_none()

        if not student:
            results["errors"].append({"student_id": action.student_id, "error": "Student not found"})
            continue

        if current_user.role == UserRole.SCHOOL_ADMIN and current_user.school_id != student.school_id:
            results["errors"].append({"student_id": action.student_id, "error": "Access denied"})
            continue
        try:
            assert_campus_access(current_user, student.campus_id)
        except HTTPException:
            results["errors"].append({"student_id": action.student_id, "error": "Access denied"})
            continue

        previous_status = student.status.value

        if action.target_class_id:
            class_changed = action.target_class_id != student.class_id
            if class_changed:
                try:
                    await check_class_capacity(session, student.school_id, action.target_class_id, exclude_student_id=student.id)
                except HTTPException as e:
                    # Class is full — waitlist the student instead of just
                    # dropping them with no path forward. auto_waitlist=True
                    # isn't used here so the exact raise-400 detail message
                    # above is preserved unchanged; this just additionally
                    # queues them once that error has been recorded.
                    waitlist_entry = await _add_to_waitlist(session, student.school_id, action.target_class_id, student.id)
                    results["errors"].append({
                        "student_id": action.student_id, "error": e.detail,
                        "waitlisted": True, "position": waitlist_entry.position,
                    })
                    continue
            student.class_id = action.target_class_id
            student.status = StudentStatus.ACTIVE
            if class_changed:
                await _record_enrollment(session, student.school_id, student.id, action.target_class_id, reason="promoted")
            results["promoted"].append({"student_id": student.id, "class_id": action.target_class_id})
        elif action.status:
            student.status = action.status
            await _close_open_enrollments(session, student.id, reason=action.status.value)
            await _release_hostel_and_transport(session, student, action.status)
            results["status_changed"].append({"student_id": student.id, "status": action.status})

        if student.status.value != previous_status:
            session.add(StudentStatusEvent(
                school_id=student.school_id, student_id=student.id,
                from_status=previous_status, to_status=student.status.value,
                reason=None, changed_by=current_user.id,
            ))

        student.updated_at = datetime.utcnow()
        session.add(student)
        await session.flush()

    await session.commit()
    return results


@router.get("/{student_id}", response_model=dict)
async def get_student(
    student_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Get student details"""
    result = await session.execute(select(Student).where(Student.id == student_id))
    student = result.scalar_one_or_none()
    
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    
    if current_user.role != UserRole.SUPER_ADMIN and current_user.school_id != student.school_id:
        raise HTTPException(status_code=403, detail="Access denied")
    assert_campus_access(current_user, student.campus_id)

    # Portal login credentials (this student's and their parents') are only
    # shown to admins managing accounts, the student themselves, or a parent
    # linked to this specific student — not to every same-school user this
    # endpoint is otherwise open to (a TEACHER, or an unrelated PARENT/STUDENT,
    # who would otherwise walk away with someone else's plaintext password).
    can_see_own_credentials = (
        current_user.role in (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)
        or current_user.id == student.user_id
    )
    if not can_see_own_credentials and current_user.role == UserRole.PARENT:
        from routers.parent import get_parent_children_ids
        can_see_own_credentials = student_id in await get_parent_children_ids(current_user, session)

    # Same reasoning, applied to medical/contact PII rather than
    # credentials: a STUDENT or unrelated PARENT viewing someone else's
    # record has no reason to see their address, medical conditions,
    # religion, or their parents' phone/email — but every staff role
    # (TEACHER included — duty-of-care needs medical info) keeps full
    # access, since this gate is specifically about the STUDENT/PARENT
    # audience browsing records that aren't their own/their child's.
    can_see_sensitive_details = (
        current_user.role not in (UserRole.STUDENT, UserRole.PARENT)
        or can_see_own_credentials
    )

    portal_account = None
    if student.user_id:
        if can_see_own_credentials:
            result_user = await session.execute(select(User).where(User.id == student.user_id))
            portal_user = result_user.scalar_one_or_none()
            if portal_user:
                portal_account = {
                    "email": portal_user.email,
                    "password": decrypt_onboarding_password(portal_user.plain_text_password)
                }
    else:
        portal_user, portal_password = await _create_portal_user(
            first_name=student.first_name,
            last_name=student.last_name,
            school_id=student.school_id,
            role=UserRole.STUDENT,
            session=session
        )
        student.user_id = portal_user.id
        session.add(student)
        await session.commit()
        if can_see_own_credentials:
            portal_account = {
                "email": portal_user.email,
                "password": portal_password
            }
    
    class_name = "Unassigned"
    if student.class_id:
        class_result = await session.execute(select(Class).where(Class.id == student.class_id))
        cls = class_result.scalar_one_or_none()
        if cls:
            class_name = cls.name
    
    parent_links = await session.execute(
        select(StudentParent).where(StudentParent.student_id == student_id)
    )
    parent_link_rows = parent_links.scalars().all()
    parent_ids = [p.parent_id for p in parent_link_rows]
    custody_by_parent_id = {p.parent_id: p.custody_type for p in parent_link_rows}
    restriction_by_parent_id = {
        p.parent_id: {"is_pickup_restricted": p.is_pickup_restricted, "restriction_reason": p.restriction_reason}
        for p in parent_link_rows
    }

    parents = []
    if parent_ids:
        parent_result = await session.execute(select(Parent).where(Parent.id.in_(parent_ids)))
        parent_objects = parent_result.scalars().all()

        for p in parent_objects:
            # A parent's own credentials are only shown to that parent
            # themselves or a privileged admin — not to a co-parent, the
            # student, or any other same-school viewer of this record.
            can_see_this_parent_credentials = (
                current_user.role in (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)
                or current_user.id == p.user_id
            )
            parent_portal = None
            if p.user_id:
                if can_see_this_parent_credentials:
                    user_result = await session.execute(select(User).where(User.id == p.user_id))
                    user_obj = user_result.scalar_one_or_none()
                    if user_obj:
                        parent_portal = {"email": user_obj.email, "password": decrypt_onboarding_password(user_obj.plain_text_password)}
            else:
                portal_user, portal_password = await _create_portal_user(
                    first_name=p.first_name,
                    last_name=p.last_name,
                    school_id=p.school_id,
                    role=UserRole.PARENT,
                    session=session
                )
                p.user_id = portal_user.id
                session.add(p)
                await session.commit()
                if can_see_this_parent_credentials:
                    parent_portal = {"email": portal_user.email, "password": portal_password}

            parents.append({
                "id": p.id,
                "first_name": p.first_name,
                "last_name": p.last_name,
                "relationship": p.relationship,
                "phone": p.phone if can_see_sensitive_details else None,
                "email": p.email if can_see_sensitive_details else None,
                "is_emergency_contact": p.is_emergency_contact,
                "custody_type": custody_by_parent_id.get(p.id),
                "is_pickup_restricted": restriction_by_parent_id.get(p.id, {}).get("is_pickup_restricted", False),
                "restriction_reason": restriction_by_parent_id.get(p.id, {}).get("restriction_reason"),
                "portal_account": parent_portal
            })

    return {
        "id": student.id,
        "school_id": student.school_id,
        "student_id": student.student_id,
        "first_name": student.first_name,
        "last_name": student.last_name,
        "full_name": f"{student.first_name} {student.last_name}",
        "date_of_birth": student.date_of_birth,
        "gender": student.gender,
        "admission_date": student.admission_date,
        "class_id": student.class_id,
        "class_name": class_name,
        "address": student.address if can_see_sensitive_details else None,
        "nationality": student.nationality if can_see_sensitive_details else None,
        "religion": student.religion if can_see_sensitive_details else None,
        "blood_group": student.blood_group if can_see_sensitive_details else None,
        "medical_conditions": student.medical_conditions if can_see_sensitive_details else None,
        "status": student.status,
        "photo_url": student.photo_url,
        "exit_date": student.exit_date,
        "exit_reason": student.exit_reason,
        "transfer_destination_school": student.transfer_destination_school,
        "admission_type": student.admission_type,
        "previous_school_name": student.previous_school_name,
        "transfer_certificate_number": student.transfer_certificate_number,
        "custom_fields": student.custom_fields,
        "portal_account": portal_account,
        "parents": parents,
        "created_at": student.created_at.isoformat()
    }


@router.get("/{student_id}/enrollments", response_model=list)
async def get_student_enrollments(
    student_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Year-scoped class history for a student — every class/term they've
    been placed in, newest first, distinct from the single current class_id
    on the student record."""
    result = await session.execute(select(Student).where(Student.id == student_id))
    student = result.scalar_one_or_none()

    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    if current_user.role != UserRole.SUPER_ADMIN and current_user.school_id != student.school_id:
        raise HTTPException(status_code=403, detail="Access denied")
    assert_campus_access(current_user, student.campus_id)

    enrollments_result = await session.execute(
        select(StudentEnrollment)
        .where(StudentEnrollment.student_id == student_id)
        .order_by(StudentEnrollment.enrolled_at.desc())
    )
    enrollments = enrollments_result.scalars().all()

    class_ids = list({e.class_id for e in enrollments})
    term_ids = list({e.academic_term_id for e in enrollments})

    class_names = {}
    if class_ids:
        class_result = await session.execute(select(Class).where(Class.id.in_(class_ids)))
        for c in class_result.scalars().all():
            class_names[c.id] = c.name

    term_labels = {}
    if term_ids:
        term_result = await session.execute(select(AcademicTerm).where(AcademicTerm.id.in_(term_ids)))
        for t in term_result.scalars().all():
            term_labels[t.id] = f"{t.term.value.capitalize()} Term, {t.academic_year}"

    return [
        {
            "id": e.id,
            "class_id": e.class_id,
            "class_name": class_names.get(e.class_id, "Unknown"),
            "academic_term_id": e.academic_term_id,
            "term_label": term_labels.get(e.academic_term_id, "Unknown Term"),
            "enrolled_at": e.enrolled_at.isoformat(),
            "ended_at": e.ended_at.isoformat() if e.ended_at else None,
            "ended_reason": e.ended_reason,
        }
        for e in enrollments
    ]


@router.get("/{student_id}/mastery-records", response_model=list)
async def get_student_mastery_records(
    student_id: str,
    academic_term_id: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Standards-based mastery assessments for a student — parallel/optional
    alongside the numeric Grade endpoints (GET /grades/student/{id}), see
    models.grade.StandardMasteryRecord. Written by POST /grades/mastery-records;
    exposed here (rather than under /grades) so the URL matches the rest of
    this router's per-student sub-resources (enrollments, parents, siblings)."""
    from models.grade import StandardMasteryRecord
    from models.curriculum import CurriculumStandard

    result = await session.execute(select(Student).where(Student.id == student_id))
    student = result.scalar_one_or_none()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    if current_user.role != UserRole.SUPER_ADMIN and current_user.school_id != student.school_id:
        raise HTTPException(status_code=403, detail="Access denied")
    assert_campus_access(current_user, student.campus_id)

    query = select(StandardMasteryRecord).where(
        StandardMasteryRecord.student_id == student_id, StandardMasteryRecord.school_id == student.school_id
    )
    if academic_term_id:
        query = query.where(StandardMasteryRecord.academic_term_id == academic_term_id)
    records_result = await session.execute(query.order_by(StandardMasteryRecord.assessed_at.desc()))
    records = records_result.scalars().all()

    standard_ids = list({r.standard_id for r in records})
    standards_map = {}
    if standard_ids:
        standards_result = await session.execute(select(CurriculumStandard).where(CurriculumStandard.id.in_(standard_ids)))
        standards_map = {s.id: s for s in standards_result.scalars().all()}

    return [
        {
            **record.model_dump(),
            "standard_code": standards_map[record.standard_id].code if record.standard_id in standards_map else None,
            "standard_title": standards_map[record.standard_id].title if record.standard_id in standards_map else None,
        }
        for record in records
    ]


@router.get("/{student_id}/siblings", response_model=list)
async def get_student_siblings(
    student_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Merges two sources of sibling relationships: students who share a
    registered parent via StudentParent (derived, tag "shared_parent") and
    explicit StudentSibling rows for step/half-siblings who don't share a
    parent record (tag "explicit"). Deduped by the other student's id —
    an explicit row wins over a derived one for the same pair since it
    carries a more specific relationship_type."""
    result = await session.execute(select(Student).where(Student.id == student_id))
    student = result.scalar_one_or_none()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    if current_user.role != UserRole.SUPER_ADMIN and current_user.school_id != student.school_id:
        raise HTTPException(status_code=403, detail="Access denied")
    assert_campus_access(current_user, student.campus_id)

    entries: dict = {}

    parent_links_result = await session.execute(
        select(StudentParent.parent_id).where(StudentParent.student_id == student_id)
    )
    parent_ids = list(parent_links_result.scalars().all())
    if parent_ids:
        shared_result = await session.execute(
            select(StudentParent.student_id).where(
                StudentParent.parent_id.in_(parent_ids),
                StudentParent.student_id != student_id,
            )
        )
        for sid in set(shared_result.scalars().all()):
            entries[sid] = {"id": None, "sibling_student_id": sid, "relationship_type": "sibling", "source": "shared_parent"}

    explicit_result = await session.execute(
        select(StudentSibling).where(StudentSibling.student_id == student_id)
    )
    for link in explicit_result.scalars().all():
        entries[link.sibling_student_id] = {
            "id": link.id,
            "sibling_student_id": link.sibling_student_id,
            "relationship_type": link.relationship_type,
            "source": "explicit",
        }

    if not entries:
        return []

    sibling_students_result = await session.execute(select(Student).where(Student.id.in_(entries.keys())))
    sibling_students = {s.id: s for s in sibling_students_result.scalars().all()}

    payload = []
    for sid, info in entries.items():
        s = sibling_students.get(sid)
        if not s:
            continue
        payload.append({
            **info,
            "first_name": s.first_name,
            "last_name": s.last_name,
            "full_name": f"{s.first_name} {s.last_name}",
            "student_code": s.student_id,
            "class_id": s.class_id,
            "status": s.status,
            "photo_url": s.photo_url,
        })
    return payload


@router.post("/{student_id}/siblings", response_model=dict)
async def add_student_sibling(
    student_id: str,
    payload: StudentSiblingCreate,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Create an explicit sibling link — for step/half-siblings who don't
    share a registered Parent record. Siblings who DO share a parent are
    already visible via GET .../siblings without needing this."""
    result = await session.execute(select(Student).where(Student.id == student_id))
    student = result.scalar_one_or_none()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    if current_user.role == UserRole.SCHOOL_ADMIN and current_user.school_id != student.school_id:
        raise HTTPException(status_code=403, detail="Access denied")
    assert_campus_access(current_user, student.campus_id)

    if payload.sibling_student_id == student_id:
        raise HTTPException(status_code=400, detail="A student cannot be linked as their own sibling")

    sibling_result = await session.execute(select(Student).where(Student.id == payload.sibling_student_id))
    sibling = sibling_result.scalar_one_or_none()
    if not sibling:
        raise HTTPException(status_code=404, detail="Sibling student not found")
    if sibling.school_id != student.school_id:
        raise HTTPException(status_code=400, detail="Sibling must be in the same school")

    existing_result = await session.execute(
        select(StudentSibling).where(
            StudentSibling.student_id == student_id,
            StudentSibling.sibling_student_id == payload.sibling_student_id,
        )
    )
    if existing_result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Sibling link already exists")

    link = StudentSibling(
        school_id=student.school_id,
        student_id=student_id,
        sibling_student_id=payload.sibling_student_id,
        relationship_type=payload.relationship_type,
        created_by=current_user.id,
    )
    session.add(link)
    await session.commit()
    await session.refresh(link)

    return {
        "id": link.id,
        "student_id": link.student_id,
        "sibling_student_id": link.sibling_student_id,
        "relationship_type": link.relationship_type,
        "message": "Sibling linked successfully",
    }


@router.delete("/{student_id}/siblings/{sibling_id}", response_model=dict)
async def remove_student_sibling(
    student_id: str,
    sibling_id: str,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Removes an explicit StudentSibling row. A derived (shared-parent)
    entry has no row of its own to delete — remove the shared parent link
    instead if that relationship should stop showing up."""
    result = await session.execute(select(Student).where(Student.id == student_id))
    student = result.scalar_one_or_none()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    if current_user.role == UserRole.SCHOOL_ADMIN and current_user.school_id != student.school_id:
        raise HTTPException(status_code=403, detail="Access denied")
    assert_campus_access(current_user, student.campus_id)

    link_result = await session.execute(
        select(StudentSibling).where(StudentSibling.id == sibling_id, StudentSibling.student_id == student_id)
    )
    link = link_result.scalar_one_or_none()
    if not link:
        raise HTTPException(status_code=404, detail="Sibling link not found")

    await session.delete(link)
    await session.commit()
    return {"message": "Sibling link removed", "id": sibling_id}


@router.post("/{student_id}/photo", response_model=dict)
async def upload_student_photo(
    student_id: str,
    file: UploadFile = File(...),
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Saves to uploads/students/{school_id}/{student_id}/{uuid}_{filename}
    and stamps Student.photo_url with the web path — servable via the
    existing app.mount("/uploads", ...) static mount in server.py."""
    result = await session.execute(select(Student).where(Student.id == student_id))
    student = result.scalar_one_or_none()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    if current_user.role == UserRole.SCHOOL_ADMIN and current_user.school_id != student.school_id:
        raise HTTPException(status_code=403, detail="Access denied")
    assert_campus_access(current_user, student.campus_id)

    if file.content_type not in ALLOWED_PHOTO_CONTENT_TYPES:
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {file.content_type}. Allowed: JPEG, PNG.")
    content = await file.read()
    if len(content) == 0:
        raise HTTPException(status_code=400, detail="File is empty.")
    if len(content) > MAX_FILE_SIZE_BYTES:
        raise HTTPException(status_code=400, detail="Photo exceeds the size limit.")

    filename = f"{uuid.uuid4()}_{Path(file.filename or 'photo').name}"
    target_dir = PHOTO_UPLOAD_DIR / student.school_id / student.id
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / filename).write_bytes(content)

    student.photo_url = f"/uploads/students/{student.school_id}/{student.id}/{filename}"
    student.updated_at = datetime.utcnow()
    session.add(student)
    await session.commit()

    return {"message": "Photo uploaded", "photo_url": student.photo_url}


@router.put("/{student_id}", response_model=dict)
async def update_student(
    student_id: str,
    student_data: StudentCreate,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Update student details"""
    result = await session.execute(select(Student).where(Student.id == student_id))
    student = result.scalar_one_or_none()
    
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    
    if current_user.role == UserRole.SCHOOL_ADMIN and current_user.school_id != student.school_id:
        raise HTTPException(status_code=403, detail="Access denied")
    assert_campus_access(current_user, student.campus_id)
    student_data.campus_id = resolve_write_campus_id(current_user, student_data.campus_id)

    if student.status == StudentStatus.ACTIVE and student_data.status in (
        StudentStatus.GRADUATED, StudentStatus.TRANSFERRED, StudentStatus.WITHDRAWN, StudentStatus.EXPELLED
    ):
        raise HTTPException(
            status_code=422,
            detail="Use the Exit Student action to end a student's enrollment — it captures a reason and checks clearance."
        )
    if student.status == StudentStatus.ACTIVE and student_data.status == StudentStatus.INACTIVE:
        raise HTTPException(
            status_code=422,
            detail="Use the Deactivate Student action to place a student on inactive status — it captures a reason."
        )

    waitlist_entry = None
    if student_data.class_id and student_data.class_id != student.class_id:
        waitlist_entry = await check_class_capacity(
            session, student.school_id, student_data.class_id, exclude_student_id=student_id,
            auto_waitlist=True, student_id=student_id,
        )
        if waitlist_entry is not None:
            # Class is full — leave the student in their current class
            # (or unassigned) rather than moving them into an over-capacity one.
            student_data.class_id = student.class_id

    previous_class_id = student.class_id
    new_class_id = student_data.class_id
    previous_status = student.status.value

    for key, value in student_data.model_dump().items():
        setattr(student, key, value)

    if student.status.value != previous_status:
        session.add(StudentStatusEvent(
            school_id=student.school_id, student_id=student.id,
            from_status=previous_status, to_status=student.status.value,
            reason=None, changed_by=current_user.id,
        ))
        await _release_hostel_and_transport(session, student, student.status)

    student.updated_at = datetime.utcnow()
    session.add(student)

    if new_class_id != previous_class_id:
        if new_class_id:
            await _record_enrollment(session, student.school_id, student.id, new_class_id, reason="reassigned")
        else:
            await _close_open_enrollments(session, student.id, reason="unassigned")

    await session.commit()

    if waitlist_entry:
        return {"waitlisted": True, "position": waitlist_entry.position}
    return {"message": "Student updated successfully"}


@router.get("/{student_id}/exit-clearance", response_model=dict)
async def get_exit_clearance(
    student_id: str,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Preview whether a student is clear to exit (fees, library, hostel,
    transport) without recording anything."""
    result = await session.execute(select(Student).where(Student.id == student_id))
    student = result.scalar_one_or_none()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    if current_user.role == UserRole.SCHOOL_ADMIN and current_user.school_id != student.school_id:
        raise HTTPException(status_code=403, detail="Access denied")

    clearance = await _compute_exit_clearance(session, student.school_id, student)
    clearance["overall_clear"] = all(item["clear"] for item in clearance.values())
    return clearance


@router.post("/{student_id}/exit", response_model=dict)
async def exit_student(
    student_id: str,
    payload: StudentExitRequest,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """End a student's time at the school — the only path that records an
    exit reason, checks clearance, and writes a StudentStatusEvent audit row."""
    result = await session.execute(select(Student).where(Student.id == student_id))
    student = result.scalar_one_or_none()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    if current_user.role == UserRole.SCHOOL_ADMIN and current_user.school_id != student.school_id:
        raise HTTPException(status_code=403, detail="Access denied")
    assert_campus_access(current_user, student.campus_id)

    if payload.status not in (StudentStatus.GRADUATED, StudentStatus.TRANSFERRED, StudentStatus.WITHDRAWN, StudentStatus.EXPELLED):
        raise HTTPException(status_code=422, detail="Exit status must be graduated, transferred, withdrawn, or expelled")
    if not payload.reason or not payload.reason.strip():
        raise HTTPException(status_code=422, detail="A reason is required")
    if student.status != StudentStatus.ACTIVE:
        raise HTTPException(status_code=400, detail=f"Student is already {student.status.value}")

    clearance = await _compute_exit_clearance(session, student.school_id, student)
    overall_clear = all(item["clear"] for item in clearance.values())
    if not overall_clear and not payload.confirm_incomplete_clearance:
        raise HTTPException(
            status_code=409,
            detail="Exit clearance is incomplete — check outstanding fees, library loans, hostel, or transport before proceeding, or confirm to override."
        )

    old_status = student.status.value
    student.status = payload.status
    student.exit_date = payload.exit_date or datetime.utcnow().strftime("%Y-%m-%d")
    student.exit_reason = payload.reason.strip()
    student.transfer_destination_school = (
        payload.transfer_destination_school.strip() if payload.status == StudentStatus.TRANSFERRED and payload.transfer_destination_school else None
    )
    student.updated_at = datetime.utcnow()
    session.add(student)

    await _close_open_enrollments(session, student.id, reason=payload.status.value)
    await _release_hostel_and_transport(session, student, payload.status)
    session.add(StudentStatusEvent(
        school_id=student.school_id, student_id=student.id,
        from_status=old_status, to_status=payload.status.value,
        reason=student.exit_reason, changed_by=current_user.id,
    ))

    await session.commit()
    return {"message": "Student exit recorded", "status": student.status.value, "clearance": clearance}


@router.post("/{student_id}/deactivate", response_model=dict)
async def deactivate_student(
    student_id: str,
    payload: StudentDeactivateRequest,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Place an active student on temporary INACTIVE status (leave of
    absence, medical leave, disciplinary suspension) — unlike exit_student,
    this does NOT close enrollments or run exit clearance, since the
    student is expected to return. Reverse with /reactivate."""
    result = await session.execute(select(Student).where(Student.id == student_id))
    student = result.scalar_one_or_none()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    if current_user.role == UserRole.SCHOOL_ADMIN and current_user.school_id != student.school_id:
        raise HTTPException(status_code=403, detail="Access denied")
    assert_campus_access(current_user, student.campus_id)

    if not payload.reason or not payload.reason.strip():
        raise HTTPException(status_code=422, detail="A reason is required")
    if student.status != StudentStatus.ACTIVE:
        raise HTTPException(status_code=400, detail=f"Only an active student can be deactivated (currently {student.status.value})")

    old_status = student.status.value
    student.status = StudentStatus.INACTIVE
    student.updated_at = datetime.utcnow()
    session.add(student)
    session.add(StudentStatusEvent(
        school_id=student.school_id, student_id=student.id,
        from_status=old_status, to_status=StudentStatus.INACTIVE.value,
        reason=payload.reason.strip(), changed_by=current_user.id,
    ))
    await session.commit()
    return {"message": "Student deactivated", "status": student.status.value}


@router.post("/{student_id}/reactivate", response_model=dict)
async def reactivate_student(
    student_id: str,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Return an INACTIVE student to ACTIVE status."""
    result = await session.execute(select(Student).where(Student.id == student_id))
    student = result.scalar_one_or_none()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    if current_user.role == UserRole.SCHOOL_ADMIN and current_user.school_id != student.school_id:
        raise HTTPException(status_code=403, detail="Access denied")
    assert_campus_access(current_user, student.campus_id)

    if student.status != StudentStatus.INACTIVE:
        raise HTTPException(status_code=400, detail=f"Only an inactive student can be reactivated (currently {student.status.value})")

    old_status = student.status.value
    student.status = StudentStatus.ACTIVE
    student.updated_at = datetime.utcnow()
    session.add(student)
    session.add(StudentStatusEvent(
        school_id=student.school_id, student_id=student.id,
        from_status=old_status, to_status=StudentStatus.ACTIVE.value,
        reason=None, changed_by=current_user.id,
    ))
    await session.commit()
    return {"message": "Student reactivated", "status": student.status.value}


@router.post("/{student_id}/re-enroll", response_model=dict)
async def re_enroll_student(
    student_id: str,
    payload: StudentReEnrollRequest,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Bring a formerly graduated/transferred/withdrawn/expelled student
    back to ACTIVE status in a (possibly new) class — distinct from
    /reactivate, which only ever reverses the temporary INACTIVE status."""
    result = await session.execute(select(Student).where(Student.id == student_id))
    student = result.scalar_one_or_none()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    if current_user.role == UserRole.SCHOOL_ADMIN and current_user.school_id != student.school_id:
        raise HTTPException(status_code=403, detail="Access denied")
    assert_campus_access(current_user, student.campus_id)

    if student.status not in (StudentStatus.GRADUATED, StudentStatus.TRANSFERRED, StudentStatus.WITHDRAWN, StudentStatus.EXPELLED):
        raise HTTPException(
            status_code=400,
            detail=f"Only a graduated, transferred, withdrawn, or expelled student can be re-enrolled (currently {student.status.value})"
        )
    if not payload.reason or not payload.reason.strip():
        raise HTTPException(status_code=422, detail="A reason is required")

    waitlist_entry = await check_class_capacity(
        session, student.school_id, payload.class_id, exclude_student_id=student.id,
        auto_waitlist=True, student_id=student.id,
    )
    if waitlist_entry is not None:
        await session.commit()
        return {"waitlisted": True, "position": waitlist_entry.position}

    old_status = student.status.value
    student.status = StudentStatus.ACTIVE
    student.class_id = payload.class_id
    student.exit_date = None
    student.exit_reason = None
    student.transfer_destination_school = None
    student.updated_at = datetime.utcnow()
    session.add(student)

    await _record_enrollment(session, student.school_id, student.id, payload.class_id, reason="re-enrolled")
    session.add(StudentStatusEvent(
        school_id=student.school_id, student_id=student.id,
        from_status=old_status, to_status=StudentStatus.ACTIVE.value,
        reason=payload.reason.strip(), changed_by=current_user.id,
    ))

    await session.commit()
    return {"message": "Student re-enrolled", "status": student.status.value, "class_id": student.class_id}


@router.put("/{student_id}/class", response_model=dict)
async def assign_student_to_class(
    student_id: str,
    body: AssignStudentClassRequest,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Assign student to a class"""
    class_id = body.class_id
    result = await session.execute(select(Student).where(Student.id == student_id))
    student = result.scalar_one_or_none()
    
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    
    if current_user.role == UserRole.SCHOOL_ADMIN and current_user.school_id != student.school_id:
        raise HTTPException(status_code=403, detail="Access denied")
    assert_campus_access(current_user, student.campus_id)

    class_result = await session.execute(select(Class).where(Class.id == class_id))
    cls = class_result.scalar_one_or_none()

    if not cls:
        raise HTTPException(status_code=404, detail="Class not found")

    previous_class_id = student.class_id
    class_changed = class_id != previous_class_id

    waitlist_entry = None
    if class_changed:
        waitlist_entry = await check_class_capacity(
            session, student.school_id, class_id, exclude_student_id=student_id,
            auto_waitlist=True, student_id=student_id,
        )

    if waitlist_entry is not None:
        await session.commit()
        return {"waitlisted": True, "position": waitlist_entry.position}

    student.class_id = class_id
    student.updated_at = datetime.utcnow()
    session.add(student)

    if class_changed:
        await _record_enrollment(session, student.school_id, student.id, class_id, reason="reassigned")

    await session.commit()

    return {"message": f"Student assigned to {cls.name}"}


@router.get("/{student_id}/parents", response_model=list)
async def get_student_parents(
    student_id: str,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.TEACHER)),
    session: AsyncSession = Depends(get_session)
):
    """Get parents linked to a student"""
    # Verify student exists
    try:
        result = await session.execute(select(Student).where(Student.id == student_id))
        student = result.scalar_one_or_none()
        
        if not student:
            raise HTTPException(status_code=404, detail="Student not found")

        if current_user.role != UserRole.SUPER_ADMIN and current_user.school_id != student.school_id:
            raise HTTPException(status_code=403, detail="Access denied")
        assert_campus_access(current_user, student.campus_id)

        # Get student-parent relationships
        result = await session.execute(
            select(StudentParent).where(StudentParent.student_id == student_id)
        )
        student_parents = result.scalars().all()
        
        if not student_parents:
            return []
        
        # Get parent details
        parent_ids = [sp.parent_id for sp in student_parents]
        custody_by_parent_id = {sp.parent_id: sp.custody_type for sp in student_parents}
        restriction_by_parent_id = {
            sp.parent_id: {"is_pickup_restricted": sp.is_pickup_restricted, "restriction_reason": sp.restriction_reason}
            for sp in student_parents
        }
        result = await session.execute(
            select(Parent).where(Parent.id.in_(parent_ids))
        )
        parents = result.scalars().all()

        # Plaintext portal credentials are only for admins managing accounts --
        # not for a TEACHER, who this endpoint is otherwise open to.
        can_see_credentials = current_user.role in (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)

        parent_payload = []
        for p in parents:
            parent_portal = None
            if p.user_id:
                if can_see_credentials:
                    user_result = await session.execute(select(User).where(User.id == p.user_id))
                    user_obj = user_result.scalar_one_or_none()
                    if user_obj:
                        parent_portal = {
                            "email": user_obj.email,
                            "password": decrypt_onboarding_password(user_obj.plain_text_password)
                        }
            else:
                portal_user, portal_password = await _create_portal_user(
                    first_name=p.first_name,
                    last_name=p.last_name,
                    school_id=student.school_id,
                    role=UserRole.PARENT,
                    session=session
                )
                p.user_id = portal_user.id
                session.add(p)
                await session.commit()
                if can_see_credentials:
                    parent_portal = {
                        "email": portal_user.email,
                        "password": portal_password
                    }

            parent_payload.append({
                "id": p.id,
                "parent_id": p.id,
                "user_id": p.user_id,  # Add user_id for messaging
                "first_name": p.first_name,
                "last_name": p.last_name,
                "relationship": p.relationship,
                "phone": p.phone,
                "email": p.email,
                "occupation": p.occupation,
                "address": p.address,
                "is_emergency_contact": p.is_emergency_contact,
                "custody_type": custody_by_parent_id.get(p.id),
                "is_pickup_restricted": restriction_by_parent_id.get(p.id, {}).get("is_pickup_restricted", False),
                "restriction_reason": restriction_by_parent_id.get(p.id, {}).get("restriction_reason"),
                "portal_account": parent_portal
            })

        return parent_payload
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{student_id}/parents/{parent_id}", response_model=dict)
async def remove_parent_from_student(
    student_id: str,
    parent_id: str,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Remove a parent/guardian link from a student"""
    result = await session.execute(select(Student).where(Student.id == student_id))
    student = result.scalar_one_or_none()

    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    if current_user.role == UserRole.SCHOOL_ADMIN and current_user.school_id != student.school_id:
        raise HTTPException(status_code=403, detail="Access denied")
    assert_campus_access(current_user, student.campus_id)

    parent_result = await session.execute(select(Parent).where(Parent.id == parent_id))
    parent = parent_result.scalar_one_or_none()

    if not parent:
        raise HTTPException(status_code=404, detail="Parent not found")

    link_result = await session.execute(
        select(StudentParent).where(
            StudentParent.student_id == student_id,
            StudentParent.parent_id == parent_id
        )
    )
    link = link_result.scalar_one_or_none()

    if not link:
        raise HTTPException(status_code=404, detail="Parent is not linked to this student")

    await session.delete(link)
    await session.commit()

    return {
        "message": "Parent removed successfully",
        "student_id": student_id,
        "parent_id": parent_id
    }


@router.post("/{student_id}/parents", response_model=dict)
async def add_parent(
    student_id: str,
    parent_data: ParentCreate,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Add a parent/guardian to a student"""
    result = await session.execute(select(Student).where(Student.id == student_id))
    student = result.scalar_one_or_none()

    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    if current_user.role == UserRole.SCHOOL_ADMIN and current_user.school_id != student.school_id:
        raise HTTPException(status_code=403, detail="Access denied")
    assert_campus_access(current_user, student.campus_id)

    school_result = await session.execute(select(School).where(School.id == student.school_id))
    school = school_result.scalar_one_or_none()
    school_name = school.name if school else "School ERP"

    custody_type = parent_data.custody_type
    if custody_type and custody_type not in {c.value for c in CustodyType}:
        raise HTTPException(status_code=422, detail="Invalid custody type")

    parent_fields = parent_data.model_dump()
    parent_fields.pop("custody_type", None)
    parent = Parent(school_id=student.school_id, **parent_fields)
    session.add(parent)
    await session.flush()

    portal_user, portal_password = await _create_portal_user(
        first_name=parent.first_name,
        last_name=parent.last_name,
        school_id=student.school_id,
        role=UserRole.PARENT,
        session=session
    )

    parent.user_id = portal_user.id
    link_kwargs = {"student_id": student_id, "parent_id": parent.id}
    if custody_type:
        link_kwargs["custody_type"] = custody_type
    link = StudentParent(**link_kwargs)
    session.add(link)
    session.add(parent)
    await session.commit()
    await session.refresh(parent)

    if parent.email:
        student_name = f"{student.first_name} {student.last_name}"
        parent_name = f"{parent.first_name} {parent.last_name}"
        background_tasks.add_task(
            email_service.send_portal_credentials,
            to=parent.email,
            parent_name=parent_name,
            student_name=student_name,
            login_email=portal_user.email,
            temp_password=portal_password,
            school_name=school_name,
        )

    return {
        "id": parent.id,
        "first_name": parent.first_name,
        "last_name": parent.last_name,
        "message": "Parent added successfully",
        "portal_account": {
            "email": portal_user.email,
            "password": portal_password
        }
    }


@router.post("/{student_id}/parents/{parent_id}/link", response_model=dict)
async def link_existing_parent(
    student_id: str,
    parent_id: str,
    payload: LinkParentRequest = LinkParentRequest(),
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Link an existing parent to another student"""
    # Verify student exists
    result = await session.execute(select(Student).where(Student.id == student_id))
    student = result.scalar_one_or_none()

    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    if current_user.role == UserRole.SCHOOL_ADMIN and current_user.school_id != student.school_id:
        raise HTTPException(status_code=403, detail="Access denied")
    assert_campus_access(current_user, student.campus_id)

    # Verify parent exists
    result = await session.execute(select(Parent).where(Parent.id == parent_id))
    parent = result.scalar_one_or_none()

    if not parent:
        raise HTTPException(status_code=404, detail="Parent not found")

    # Check if parent is already linked to this student
    result = await session.execute(
        select(StudentParent).where(
            StudentParent.student_id == student_id,
            StudentParent.parent_id == parent_id
        )
    )
    existing = result.scalar_one_or_none()

    if existing:
        raise HTTPException(status_code=400, detail="Parent is already linked to this student")

    custody_type = payload.custody_type
    if custody_type and custody_type not in {c.value for c in CustodyType}:
        raise HTTPException(status_code=422, detail="Invalid custody type")

    # Create the link
    link_kwargs = {"student_id": student_id, "parent_id": parent_id}
    if custody_type:
        link_kwargs["custody_type"] = custody_type
    link = StudentParent(**link_kwargs)
    session.add(link)
    await session.commit()

    return {
        "message": "Parent linked to student successfully",
        "student_id": student_id,
        "parent_id": parent_id,
        "custody_type": link.custody_type,
    }


@router.put("/{student_id}/parents/{parent_id}/custody", response_model=dict)
async def update_parent_custody(
    student_id: str,
    parent_id: str,
    payload: ParentCustodyUpdate,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Update the custody type on an existing (student, parent) link."""
    result = await session.execute(select(Student).where(Student.id == student_id))
    student = result.scalar_one_or_none()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    if current_user.role == UserRole.SCHOOL_ADMIN and current_user.school_id != student.school_id:
        raise HTTPException(status_code=403, detail="Access denied")
    assert_campus_access(current_user, student.campus_id)

    if payload.custody_type not in {c.value for c in CustodyType}:
        raise HTTPException(status_code=422, detail="Invalid custody type")

    link_result = await session.execute(
        select(StudentParent).where(
            StudentParent.student_id == student_id,
            StudentParent.parent_id == parent_id,
        )
    )
    link = link_result.scalar_one_or_none()
    if not link:
        raise HTTPException(status_code=404, detail="Parent is not linked to this student")

    link.custody_type = payload.custody_type
    session.add(link)
    await session.commit()

    return {
        "message": "Custody type updated",
        "student_id": student_id,
        "parent_id": parent_id,
        "custody_type": link.custody_type,
    }


@router.post("/{student_id}/portal-credentials", response_model=dict)
async def regenerate_student_portal_credentials(
    student_id: str,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Regenerate portal credentials for a student"""
    result = await session.execute(select(Student).where(Student.id == student_id))
    student = result.scalar_one_or_none()
    
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    
    if current_user.role == UserRole.SCHOOL_ADMIN and current_user.school_id != student.school_id:
        raise HTTPException(status_code=403, detail="Access denied")
    assert_campus_access(current_user, student.campus_id)

    # Delete existing user if exists (with OTP cleanup)
    if student.user_id:
        await _delete_user_with_otp_cleanup(student.user_id, session)
    
    # Create new user
    portal_user, portal_password = await _create_portal_user(
        first_name=student.first_name,
        last_name=student.last_name,
        school_id=student.school_id,
        role=UserRole.STUDENT,
        session=session
    )
    
    student.user_id = portal_user.id
    session.add(student)
    await session.commit()
    
    return {
        "portal_account": {
            "email": portal_user.email,
            "password": portal_password
        }
    }


@router.post("/{student_id}/parents/{parent_id}/portal-credentials", response_model=dict)
async def regenerate_parent_portal_credentials(
    student_id: str,
    parent_id: str,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Regenerate portal credentials for a parent"""
    # Verify student exists and access
    student_result = await session.execute(select(Student).where(Student.id == student_id))
    student = student_result.scalar_one_or_none()
    
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    
    if current_user.role == UserRole.SCHOOL_ADMIN and current_user.school_id != student.school_id:
        raise HTTPException(status_code=403, detail="Access denied")
    assert_campus_access(current_user, student.campus_id)

    # Verify parent exists and is linked to student
    parent_result = await session.execute(select(Parent).where(Parent.id == parent_id))
    parent = parent_result.scalar_one_or_none()
    
    if not parent:
        raise HTTPException(status_code=404, detail="Parent not found")
    
    link_result = await session.execute(
        select(StudentParent).where(
            StudentParent.student_id == student_id,
            StudentParent.parent_id == parent_id
        )
    )
    link = link_result.scalar_one_or_none()
    
    if not link:
        raise HTTPException(status_code=404, detail="Parent not linked to this student")
    
    # Delete existing user if exists (with OTP cleanup)
    if parent.user_id:
        await _delete_user_with_otp_cleanup(parent.user_id, session)
    
    # Create new user
    portal_user, portal_password = await _create_portal_user(
        first_name=parent.first_name,
        last_name=parent.last_name,
        school_id=parent.school_id,
        role=UserRole.PARENT,
        session=session
    )
    
    parent.user_id = portal_user.id
    session.add(parent)
    await session.commit()

    return {
        "portal_account": {
            "email": portal_user.email,
            "password": portal_password
        }
    }


# ─────────────────────────────── Emergency Contacts ───────────────────────────────

async def _get_student_or_404(student_id: str, current_user: User, session: AsyncSession) -> Student:
    result = await session.execute(select(Student).where(Student.id == student_id))
    student = result.scalar_one_or_none()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    if current_user.role != UserRole.SUPER_ADMIN and current_user.school_id != student.school_id:
        raise HTTPException(status_code=403, detail="Access denied")
    assert_campus_access(current_user, student.campus_id)
    return student


@router.get("/{student_id}/emergency-contacts", response_model=list)
async def get_emergency_contacts(
    student_id: str,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.TEACHER)),
    session: AsyncSession = Depends(get_session)
):
    """A dedicated emergency-contact list for a student, distinct from
    Parent.is_emergency_contact — an emergency contact (neighbor, family
    friend) may not be a registered parent/guardian at all."""
    student = await _get_student_or_404(student_id, current_user, session)
    contacts_result = await session.execute(
        select(EmergencyContact).where(EmergencyContact.student_id == student.id).order_by(EmergencyContact.priority_order)
    )
    return [c.model_dump() for c in contacts_result.scalars().all()]


@router.post("/{student_id}/emergency-contacts", response_model=dict)
async def add_emergency_contact(
    student_id: str,
    payload: EmergencyContactCreate,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    student = await _get_student_or_404(student_id, current_user, session)
    contact = EmergencyContact(school_id=student.school_id, student_id=student.id, **payload.model_dump())
    session.add(contact)
    await session.commit()
    await session.refresh(contact)
    return contact.model_dump()


@router.put("/{student_id}/emergency-contacts/{contact_id}", response_model=dict)
async def update_emergency_contact(
    student_id: str,
    contact_id: str,
    payload: EmergencyContactUpdate,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    student = await _get_student_or_404(student_id, current_user, session)
    contact_result = await session.execute(
        select(EmergencyContact).where(EmergencyContact.id == contact_id, EmergencyContact.student_id == student.id)
    )
    contact = contact_result.scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Emergency contact not found")

    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(contact, key, value)
    session.add(contact)
    await session.commit()
    await session.refresh(contact)
    return contact.model_dump()


@router.delete("/{student_id}/emergency-contacts/{contact_id}", response_model=dict)
async def remove_emergency_contact(
    student_id: str,
    contact_id: str,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    student = await _get_student_or_404(student_id, current_user, session)
    contact_result = await session.execute(
        select(EmergencyContact).where(EmergencyContact.id == contact_id, EmergencyContact.student_id == student.id)
    )
    contact = contact_result.scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Emergency contact not found")

    await session.delete(contact)
    await session.commit()
    return {"message": "Emergency contact removed", "id": contact_id}


# ─────────────────────────────── Transfer Requests ─────────────────────────────────
# Deliberately additive/observational — see TransferRequest's docstring in
# models/student.py. Reaching status="released" here does NOT auto-call
# exit_student; Student.status still only ever changes via POST /exit.

@router.get("/{student_id}/transfer-requests", response_model=list)
async def get_transfer_requests(
    student_id: str,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    student = await _get_student_or_404(student_id, current_user, session)
    result = await session.execute(
        select(TransferRequest).where(TransferRequest.student_id == student.id).order_by(TransferRequest.created_at.desc())
    )
    return [t.model_dump() for t in result.scalars().all()]


@router.post("/{student_id}/transfer-requests", response_model=dict)
async def create_transfer_request(
    student_id: str,
    payload: TransferRequestCreate,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    student = await _get_student_or_404(student_id, current_user, session)
    transfer = TransferRequest(
        school_id=student.school_id, student_id=student.id, requested_by=current_user.id,
        **payload.model_dump()
    )
    session.add(transfer)
    await session.commit()
    await session.refresh(transfer)
    return transfer.model_dump()


VALID_TRANSFER_REQUEST_STATUSES = {"requested", "records_prepared", "released", "acknowledged"}


@router.put("/{student_id}/transfer-requests/{transfer_id}", response_model=dict)
async def update_transfer_request(
    student_id: str,
    transfer_id: str,
    payload: TransferRequestUpdate,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    student = await _get_student_or_404(student_id, current_user, session)
    transfer_result = await session.execute(
        select(TransferRequest).where(TransferRequest.id == transfer_id, TransferRequest.student_id == student.id)
    )
    transfer = transfer_result.scalar_one_or_none()
    if not transfer:
        raise HTTPException(status_code=404, detail="Transfer request not found")

    update_data = payload.model_dump(exclude_unset=True)
    if "status" in update_data and update_data["status"] not in VALID_TRANSFER_REQUEST_STATUSES:
        raise HTTPException(status_code=422, detail="Invalid transfer request status")

    for key, value in update_data.items():
        setattr(transfer, key, value)
    transfer.updated_at = datetime.utcnow()
    session.add(transfer)
    await session.commit()
    await session.refresh(transfer)
    return transfer.model_dump()

