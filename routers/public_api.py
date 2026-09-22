"""Public API for third-party integrators — authenticated with an API key
(see auth.require_api_scope, models/integrations.py), never a user JWT.

Reads and writes across students, payments, fees, grades, attendance,
staff, classes, announcements, timetable, discipline, library circulation,
support tickets, transport, hostel, admissions, and alumni. Deliberately
excludes payroll/GL financial data and health/medical records — the two
most sensitive data categories in the system, kept off a machine-to-
machine surface as a deliberate judgment call, not an oversight (see this
module's git history / project memory for the reasoning). Otherwise still
narrow within each included module (e.g.
no payroll/payout data in the staff listing) rather than mirroring the
full internal API surface. Expanding further is a separate, later
decision per integrator demand — follow this same pattern:
require_api_scope + explicit school_id scoping from the resolved
ApiKeyContext, never from a client-supplied parameter. Writes reuse the
same cross-tenant validation helpers and, where one already exists, the
same webhook event as their internal-UI counterparts, so a subscriber
never needs to know which path a record came in through and a badly-
formed API write can't create an orphan record the internal UI never
could.

Attribution convention for write endpoints: fields like `recorded_by`/
`created_by` are stamped `f"api_key:{context.api_key_id}"` for audit
trail — but ONLY on fields that are plain strings, not a real DB foreign
key to `users.id` (e.g. `LibraryLoan.issued_by` — that one is left None
for an API-driven checkout instead, since a fake string there would
violate the FK constraint).
"""
import csv
import io
import secrets
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from auth import require_api_scope
from database import get_session
from models.admissions import Applicant, ApplicantCreate
from models.alumni import AlumniRecord, AlumniRecordCreate
from models.assignment import Assignment, AssignmentStatus, Submission
from models.attendance import Attendance, AttendanceCreate, AttendanceStatus
from models.classroom import Class, ClassCreate, Subject
from models.communication import Announcement, AnnouncementCreate
from models.curriculum import (
    CurriculumTopic, TeacherLessonNote, TeacherLessonNoteCreate, TopicCoverageUpdate,
)
from models.discipline import IncidentReport, IncidentReportCreate, IncidentStudent
from models.exam import ExamSchedule, ExamSeatAssignment, ExamSession, ExamScheduleCreate
from models.exam_board import (
    BulkIndexNumberImport, ExamBoardRegistration,
    ExamBoardResult, ExamBoardResultsSubmit, ExamRegistrationStatus,
)
from models.exam_malpractice import MalpracticeCase, MalpracticeCaseCreate
from models.exam_marks import BulkExamComponentMarksUpsert, ExamComponentMark
from models.exam_papers import ExamPaper, QuestionBankItem, QuestionBankItemCreate
from models.exam_remarks import ExamRemarkRequest
from models.fee import Fee, FeeCreate
from models.grade import Grade, GradeCreate
from models.hostel import Room, RoomAllocation, RoomAllocationCreate
from models.integrations import (
    BiometricDevice, BiometricStaffPunch, BiometricStudentPunch,
    LmsAssignmentPush, LmsSubmissionPush, LmsSyncRecord,
)
from models.library import LibraryItem
from models.library_circulation import CopyStatus, LibraryBookCopy, LibraryLoan, LibraryLoanCreate, LoanStatus
from models.payment import OnlineTransaction
from models.school import AcademicTerm, AcademicYear, CalendarEvent, CalendarEventCreate
from models.staff import Staff, StaffCreate, StaffStatus
from models.student import Student, StudentCreate
from models.ticket import Ticket, TicketCreate
from models.timetable import Timetable, TimetableCreate
from models.tracks import StudentTrack, StudentTrackCreate, Track
from models.transport import Route, StudentTransport, StudentTransportCreate
from models.user import User
from routers.academic_calendar import _valid_dates
from routers.attendance import record_staff_punch, validate_attendance_student
from routers.classes import validate_academic_term
from routers.discipline import _serialize_incident
from routers.exam_board import _maybe_mark_results_received
from routers.exam_marks import _get_component_or_404
from routers.exams import _check_class_conflict, _check_room_conflict
from routers.grades import validate_grade_references, validate_grade_score
from routers.library_circulation import DEFAULT_LOAN_DAYS, _serialize_loan
from routers.students import check_class_capacity, _record_enrollment
from routers.timetable import get_current_term_id, validate_timetable_references
from routers.tracks import validate_track_level
from services.ticket_service import TicketService
from services.webhook_service import emit_event
from services.assignment_grade_bridge import sync_submission_to_grade

router = APIRouter(prefix="/api/public/v1", tags=["Public API"])


@router.get("/students")
async def list_students(
    limit: int = Query(50, ge=1, le=200),
    context=Depends(require_api_scope("students.record.view")),
    session: AsyncSession = Depends(get_session),
):
    """Students belonging to the calling API key's school. Scoped strictly
    by the key's own school_id — never a client-supplied one."""
    result = await session.execute(
        select(Student).where(Student.school_id == context.school_id).order_by(Student.first_name).limit(limit)
    )
    return [
        {
            "id": s.id,
            "student_id": s.student_id,
            "first_name": s.first_name,
            "last_name": s.last_name,
            "class_id": s.class_id,
            "status": s.status,
        }
        for s in result.scalars().all()
    ]


@router.get("/classes")
async def list_classes(
    limit: int = Query(50, ge=1, le=200),
    context=Depends(require_api_scope("classes.record.view")),
    session: AsyncSession = Depends(get_session),
):
    """Classes for the calling API key's school — useful for an integrator
    building its own roster from students + classes + staff."""
    result = await session.execute(
        select(Class).where(Class.school_id == context.school_id, Class.is_active == True).order_by(Class.name).limit(limit)  # noqa: E712
    )
    return [
        {
            "id": c.id,
            "name": c.name,
            "level": c.level,
            "section": c.section,
            "capacity": c.capacity,
        }
        for c in result.scalars().all()
    ]


@router.get("/announcements")
async def list_announcements(
    limit: int = Query(20, ge=1, le=100),
    context=Depends(require_api_scope("announcements.record.view")),
    session: AsyncSession = Depends(get_session),
):
    """Published announcements for the calling API key's school, most
    recent first — for an integrator mirroring school news elsewhere."""
    result = await session.execute(
        select(Announcement)
        .where(Announcement.school_id == context.school_id, Announcement.is_published == True)  # noqa: E712
        .order_by(Announcement.publish_date.desc())
        .limit(limit)
    )
    return [
        {
            "id": a.id,
            "title": a.title,
            "content": a.content,
            "announcement_type": a.announcement_type,
            "audience": a.audience,
            "publish_date": a.publish_date,
        }
        for a in result.scalars().all()
    ]


@router.get("/payments/{transaction_id}")
async def get_payment_status(
    transaction_id: str,
    context=Depends(require_api_scope("payments.status.view")),
    session: AsyncSession = Depends(get_session),
):
    """Status of a single payment transaction, scoped to the calling API
    key's school."""
    result = await session.execute(
        select(OnlineTransaction).where(
            OnlineTransaction.id == transaction_id,
            OnlineTransaction.school_id == context.school_id,
        )
    )
    transaction = result.scalar_one_or_none()
    if transaction is None:
        raise HTTPException(status_code=404, detail="Transaction not found")

    return {
        "id": transaction.id,
        "reference": transaction.reference,
        "status": transaction.status,
        "amount": transaction.amount,
        "amount_paid": transaction.amount_paid,
        "currency": transaction.currency,
        "completed_at": transaction.completed_at.isoformat() if transaction.completed_at else None,
    }


@router.get("/students/{student_id}/fees")
async def list_student_fees(
    student_id: str,
    context=Depends(require_api_scope("fees.status.view")),
    session: AsyncSession = Depends(get_session),
):
    """A student's fee/invoice records, scoped to the calling API key's
    school. Fee.school_id already ties every row to one school, so this
    scoping alone is sufficient — same convention as routers/fees.py."""
    result = await session.execute(
        select(Fee).where(Fee.school_id == context.school_id, Fee.student_id == student_id)
    )
    return [
        {
            "id": f.id,
            "student_id": f.student_id,
            "fee_structure_id": f.fee_structure_id,
            "amount_due": f.amount_due,
            "amount_paid": f.amount_paid,
            "status": f.status,
        }
        for f in result.scalars().all()
    ]


@router.get("/students/{student_id}/grades")
async def list_student_grades(
    student_id: str,
    academic_term_id: Optional[str] = Query(None),
    context=Depends(require_api_scope("grades.record.view")),
    session: AsyncSession = Depends(get_session),
):
    """A student's recorded grades, scoped to the calling API key's school."""
    query = select(Grade).where(Grade.school_id == context.school_id, Grade.student_id == student_id)
    if academic_term_id:
        query = query.where(Grade.academic_term_id == academic_term_id)
    result = await session.execute(query)
    return [
        {
            "id": g.id,
            "student_id": g.student_id,
            "subject_id": g.subject_id,
            "class_id": g.class_id,
            "assessment_type": g.assessment_type,
            "score": g.score,
            "max_score": g.max_score,
        }
        for g in result.scalars().all()
    ]


@router.get("/students/{student_id}/attendance")
async def list_student_attendance(
    student_id: str,
    limit: int = Query(100, ge=1, le=500),
    context=Depends(require_api_scope("attendance.record.view")),
    session: AsyncSession = Depends(get_session),
):
    """A student's attendance records, most recent first, scoped to the
    calling API key's school."""
    result = await session.execute(
        select(Attendance)
        .where(Attendance.school_id == context.school_id, Attendance.student_id == student_id)
        .order_by(Attendance.attendance_date.desc())
        .limit(limit)
    )
    return [
        {
            "id": a.id,
            "student_id": a.student_id,
            "class_id": a.class_id,
            "date": a.attendance_date,
            "status": a.status,
        }
        for a in result.scalars().all()
    ]


@router.get("/staff")
async def list_staff(
    limit: int = Query(50, ge=1, le=200),
    context=Depends(require_api_scope("staff.record.view")),
    session: AsyncSession = Depends(get_session),
):
    """Staff directory for the calling API key's school. Deliberately
    excludes payroll/payout fields (salary, bank details, SSNIT number)
    even though this scope grants directory access — those stay behind
    the internal finance/HR permission system, never the public API."""
    result = await session.execute(
        select(Staff).where(Staff.school_id == context.school_id).order_by(Staff.first_name).limit(limit)
    )
    return [
        {
            "id": s.id,
            "staff_id": s.staff_id,
            "first_name": s.first_name,
            "last_name": s.last_name,
            "email": s.email,
            "staff_type": s.staff_type,
            "position": s.position,
            "department": s.department,
            "status": s.status,
        }
        for s in result.scalars().all()
    ]


@router.post("/attendance", status_code=201)
async def create_attendance(
    attendance_data: AttendanceCreate,
    background_tasks: BackgroundTasks,
    context=Depends(require_api_scope("attendance.record.manage")),
    session: AsyncSession = Depends(get_session),
):
    """Record attendance for a student — the same write path the internal
    UI uses (routers/attendance.py:record_attendance), minus the
    teacher-assignment check (an API key isn't a human teacher, so that
    check doesn't apply). Rejects a class/term/student that doesn't
    belong to this API key's school. Emits the same attendance.marked
    event as the internal endpoint, so a webhook subscriber sees this
    identically to a teacher marking it by hand."""
    school_id = context.school_id

    class_result = await session.execute(
        select(Class).where(Class.id == attendance_data.class_id, Class.school_id == school_id)
    )
    if not class_result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="class_id does not exist for this school")

    term_result = await session.execute(
        select(AcademicTerm).where(
            AcademicTerm.id == attendance_data.academic_term_id, AcademicTerm.school_id == school_id
        )
    )
    if not term_result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="academic_term_id does not exist for this school")

    await validate_attendance_student(session, school_id, attendance_data.student_id, attendance_data.class_id)

    existing = await session.execute(
        select(Attendance).where(
            Attendance.school_id == school_id,
            Attendance.student_id == attendance_data.student_id,
            Attendance.attendance_date == attendance_data.attendance_date,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Attendance already recorded for this date")

    attendance = Attendance(
        school_id=school_id,
        recorded_by=f"api_key:{context.api_key_id}",
        **attendance_data.model_dump(),
    )
    session.add(attendance)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=400, detail="Attendance already recorded for this date")
    await session.refresh(attendance)

    await emit_event(
        session, background_tasks, school_id, "attendance.marked",
        {
            "id": attendance.id,
            "student_id": attendance.student_id,
            "class_id": attendance.class_id,
            "date": attendance.attendance_date,
            "status": attendance.status,
        },
    )

    return {
        "id": attendance.id,
        "student_id": attendance.student_id,
        "date": attendance.attendance_date,
        "status": attendance.status,
    }


async def _resolve_active_device(session: AsyncSession, school_id: str, device_serial: str) -> BiometricDevice:
    """Registration is a prerequisite for punches — a valid API key alone
    isn't enough to inject attendance, the device also has to be one the
    school explicitly registered (routers/integrations.py's biometric
    device CRUD) and hasn't deactivated."""
    result = await session.execute(
        select(BiometricDevice).where(
            BiometricDevice.school_id == school_id, BiometricDevice.device_serial == device_serial
        )
    )
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404, detail=f"No device registered with serial {device_serial} for this school")
    if not device.is_active:
        raise HTTPException(status_code=403, detail="This device has been deactivated")
    return device


@router.post("/biometric/student-punch", status_code=201)
async def biometric_student_punch(
    punch: BiometricStudentPunch,
    background_tasks: BackgroundTasks,
    context=Depends(require_api_scope("attendance.record.manage")),
    session: AsyncSession = Depends(get_session),
):
    """A fingerprint/face-scan device (or its vendor push middleware)
    reporting a student's gate scan. Unlike POST /attendance, the caller
    doesn't know class_id/academic_term_id/status — those are resolved
    server-side from the student's current class and the school's current
    term, and status is always PRESENT (a scan means the student is
    physically here). A second scan the same day is a no-op, not an error
    — devices commonly re-report on retry or on a second gate."""
    school_id = context.school_id
    device = await _resolve_active_device(session, school_id, punch.device_serial)

    student_result = await session.execute(
        select(Student).where(Student.school_id == school_id, Student.student_id == punch.student_id)
    )
    student = student_result.scalar_one_or_none()
    if not student:
        raise HTTPException(status_code=400, detail=f"No student with student_id {punch.student_id} for this school")
    if not student.class_id:
        raise HTTPException(status_code=400, detail="Student is not currently assigned to a class")

    academic_term_id = await get_current_term_id(session, school_id)
    if not academic_term_id:
        raise HTTPException(status_code=400, detail="No current academic term is configured for this school")

    punched_at = punch.punched_at or datetime.utcnow()
    attendance_date = punched_at.strftime("%Y-%m-%d")

    existing = await session.execute(
        select(Attendance).where(
            Attendance.school_id == school_id,
            Attendance.student_id == student.id,
            Attendance.attendance_date == attendance_date,
        )
    )
    existing_row = existing.scalar_one_or_none()
    if existing_row:
        device.last_seen_at = datetime.utcnow()
        session.add(device)
        await session.commit()
        return {"id": existing_row.id, "student_id": student.id, "date": attendance_date, "status": existing_row.status, "already_recorded": True}

    attendance = Attendance(
        school_id=school_id, student_id=student.id, class_id=student.class_id,
        academic_term_id=academic_term_id, attendance_date=attendance_date,
        status=AttendanceStatus.PRESENT, recorded_by=f"biometric_device:{device.device_serial}",
    )
    session.add(attendance)
    device.last_seen_at = datetime.utcnow()
    session.add(device)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=400, detail="Attendance already recorded for this date")
    await session.refresh(attendance)

    await emit_event(
        session, background_tasks, school_id, "attendance.marked",
        {
            "id": attendance.id, "student_id": attendance.student_id, "class_id": attendance.class_id,
            "date": attendance.attendance_date, "status": attendance.status,
        },
    )
    return {"id": attendance.id, "student_id": attendance.student_id, "date": attendance.attendance_date, "status": attendance.status, "already_recorded": False}


@router.post("/biometric/staff-punch", status_code=201)
async def biometric_staff_punch(
    punch: BiometricStaffPunch,
    background_tasks: BackgroundTasks,
    context=Depends(require_api_scope("staff_attendance.record.manage")),
    session: AsyncSession = Depends(get_session),
):
    """A fingerprint/face-scan device reporting a staff punch. Auto-toggles
    check-in vs check-out when event_type is omitted (matches how most
    punch-clock devices work — they report a raw scan, not a declared
    direction): no record yet today, or one with no check_in, is a
    check-in; a record with check_in but no check_out is a check-out; a
    third scan the same day is a no-op. An explicit event_type overrides
    the toggle for devices that do distinguish an IN/OUT button press."""
    school_id = context.school_id
    device = await _resolve_active_device(session, school_id, punch.device_serial)

    staff_result = await session.execute(
        select(Staff).where(Staff.school_id == school_id, Staff.staff_id == punch.staff_id, Staff.status == StaffStatus.ACTIVE)
    )
    staff = staff_result.scalar_one_or_none()
    if not staff:
        raise HTTPException(status_code=400, detail=f"No active staff member with staff_id {punch.staff_id} for this school")

    device.last_seen_at = datetime.utcnow()
    session.add(device)

    return await record_staff_punch(
        session, background_tasks, school_id, staff, device.device_serial,
        punched_at=punch.punched_at, event_type=punch.event_type,
    )


# ==================== LMS integration ====================
# Vendor-agnostic: any LMS (or its middleware) capable of an HTTP call can
# push into these — no OAuth app registration or vendor-specific client
# needed. Roster provisioning is already covered by GET /students,
# /classes, /staff above (each already returns enough to build a roster);
# this is the genuinely missing half — mirroring assignments and their
# submissions/grades back into Campusio's own gradebook so they show up
# for the school without a human re-entering them.

@router.post("/lms/assignments", status_code=201)
async def lms_push_assignment(
    payload: LmsAssignmentPush,
    background_tasks: BackgroundTasks,
    context=Depends(require_api_scope("assignments.record.manage")),
    session: AsyncSession = Depends(get_session),
):
    """Mirrors one LMS assignment into Campusio's own Assignment table.
    Idempotent on external_id — re-pushing the same external_id (the
    LMS's own id for the assignment) updates the existing row instead of
    creating a duplicate, since a sync integration re-pushes everything on
    every run by design. Omit external_id to always create a new row."""
    school_id = context.school_id

    if not (await session.execute(select(Class).where(Class.id == payload.class_id, Class.school_id == school_id))).scalar_one_or_none():
        raise HTTPException(status_code=400, detail="class_id does not exist for this school")
    if not (await session.execute(select(Subject).where(Subject.id == payload.subject_id, Subject.school_id == school_id))).scalar_one_or_none():
        raise HTTPException(status_code=400, detail="subject_id does not exist for this school")

    academic_term_id = payload.academic_term_id or await get_current_term_id(session, school_id)
    if not academic_term_id:
        raise HTTPException(status_code=400, detail="No current academic term is configured for this school")
    term = (await session.execute(select(AcademicTerm).where(AcademicTerm.id == academic_term_id, AcademicTerm.school_id == school_id))).scalar_one_or_none()
    if not term:
        raise HTTPException(status_code=400, detail="academic_term_id does not exist for this school")
    if term.is_locked:
        raise HTTPException(status_code=423, detail="This academic term is locked and no longer accepts new assignments")

    staff = (await session.execute(select(Staff).where(Staff.school_id == school_id, Staff.staff_id == payload.staff_id))).scalar_one_or_none()
    if not staff:
        raise HTTPException(status_code=400, detail=f"No staff member with staff_id {payload.staff_id} for this school")
    if not staff.user_id:
        raise HTTPException(status_code=400, detail="This staff member has no linked user account — required to attribute the assignment to a teacher")

    sync_link = None
    if payload.external_id:
        sync_link = (await session.execute(
            select(LmsSyncRecord).where(LmsSyncRecord.school_id == school_id, LmsSyncRecord.external_id == payload.external_id)
        )).scalar_one_or_none()

    now = datetime.utcnow()
    if sync_link:
        assignment = (await session.execute(select(Assignment).where(Assignment.id == sync_link.internal_id))).scalar_one_or_none()
        if not assignment:
            raise HTTPException(status_code=500, detail="Linked assignment record is missing")
        assignment.title = payload.title
        assignment.description = payload.description
        assignment.class_id = payload.class_id
        assignment.subject_id = payload.subject_id
        assignment.teacher_id = staff.id
        assignment.academic_term_id = academic_term_id
        assignment.assignment_type = payload.assignment_type
        assignment.points_possible = payload.points_possible
        assignment.due_date = payload.due_date
        assignment.updated_by = f"api_key:{context.api_key_id}"
        assignment.updated_at = now
        session.add(assignment)
    else:
        assignment = Assignment(
            school_id=school_id, teacher_id=staff.id, class_id=payload.class_id, subject_id=payload.subject_id,
            academic_term_id=academic_term_id, title=payload.title, description=payload.description,
            assignment_type=payload.assignment_type, status=AssignmentStatus.PUBLISHED,
            points_possible=payload.points_possible, due_date=payload.due_date,
            recorded_by=staff.user_id, published_date=now,
        )
        session.add(assignment)
        await session.flush()
        if payload.external_id:
            session.add(LmsSyncRecord(school_id=school_id, external_id=payload.external_id, internal_id=assignment.id))

    await session.commit()
    await session.refresh(assignment)

    await emit_event(
        session, background_tasks, school_id, "lms.assignment.synced",
        {
            "id": assignment.id, "title": assignment.title, "class_id": assignment.class_id,
            "subject_id": assignment.subject_id, "due_date": assignment.due_date.isoformat() if assignment.due_date else None,
        },
    )
    return {
        "id": assignment.id, "title": assignment.title, "class_id": assignment.class_id,
        "subject_id": assignment.subject_id, "status": assignment.status, "due_date": assignment.due_date,
    }


@router.post("/lms/submissions", status_code=201)
async def lms_push_submission(
    payload: LmsSubmissionPush,
    background_tasks: BackgroundTasks,
    context=Depends(require_api_scope("assignments.record.manage")),
    session: AsyncSession = Depends(get_session),
):
    """Mirrors one student's submission/grade for an LMS-synced assignment
    into Campusio. Idempotent on the natural (assignment_id, student_id)
    key — no external_id needed, since a student has at most one
    submission per assignment either way."""
    school_id = context.school_id

    assignment = (await session.execute(select(Assignment).where(Assignment.id == payload.assignment_id, Assignment.school_id == school_id))).scalar_one_or_none()
    if not assignment:
        raise HTTPException(status_code=400, detail="assignment_id does not exist for this school")

    student = (await session.execute(select(Student).where(Student.school_id == school_id, Student.student_id == payload.student_id))).scalar_one_or_none()
    if not student:
        raise HTTPException(status_code=400, detail=f"No student with student_id {payload.student_id} for this school")
    if student.class_id != assignment.class_id:
        raise HTTPException(status_code=400, detail="Student is not in the class this assignment belongs to")

    submission = (await session.execute(
        select(Submission).where(Submission.assignment_id == assignment.id, Submission.student_id == student.id)
    )).scalar_one_or_none()

    graded = payload.score is not None
    now = datetime.utcnow()
    if submission:
        submission.status = payload.status
        submission.score = payload.score
        submission.max_score = assignment.points_possible
        submission.feedback = payload.feedback
        submission.submission_date = payload.submitted_at or submission.submission_date or now
        if graded:
            submission.graded_by = f"api_key:{context.api_key_id}"
            submission.graded_date = now
        submission.updated_at = now
        session.add(submission)
    else:
        submission = Submission(
            school_id=school_id, assignment_id=assignment.id, student_id=student.id,
            class_id=assignment.class_id, subject_id=assignment.subject_id,
            status=payload.status, score=payload.score, max_score=assignment.points_possible,
            feedback=payload.feedback, submission_date=payload.submitted_at or now,
            graded_by=f"api_key:{context.api_key_id}" if graded else None,
            graded_date=now if graded else None,
        )
        session.add(submission)

    if graded:
        await sync_submission_to_grade(session, submission, assignment, recorded_by=f"api_key:{context.api_key_id}")

    await session.commit()
    await session.refresh(submission)

    await emit_event(
        session, background_tasks, school_id, "lms.submission.synced",
        {
            "id": submission.id, "assignment_id": submission.assignment_id, "student_id": submission.student_id,
            "status": submission.status, "score": submission.score,
        },
    )
    return {
        "id": submission.id, "assignment_id": submission.assignment_id, "student_id": submission.student_id,
        "status": submission.status, "score": submission.score, "feedback": submission.feedback,
    }


@router.post("/grades", status_code=201)
async def create_grade(
    grade_data: GradeCreate,
    background_tasks: BackgroundTasks,
    context=Depends(require_api_scope("grades.record.manage")),
    session: AsyncSession = Depends(get_session),
):
    """Record a grade for a student — the same write path the internal UI
    uses (routers/grades.py:record_grade), reusing its exact validation
    (score range, cross-tenant reference checks). For an LMS or external
    gradebook pushing scores back into Campusio. Emits the same
    grade.recorded event as the internal endpoint."""
    school_id = context.school_id

    validate_grade_score(grade_data.score, grade_data.max_score)
    await validate_grade_references(
        session, school_id, grade_data.student_id, grade_data.class_id,
        grade_data.subject_id, grade_data.academic_term_id,
    )

    grade = Grade(
        school_id=school_id,
        recorded_by=f"api_key:{context.api_key_id}",
        **grade_data.model_dump(),
    )
    session.add(grade)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=400,
            detail="This student already has a grade recorded for this subject, term, and assessment type",
        )
    await session.refresh(grade)

    await emit_event(
        session, background_tasks, school_id, "grade.recorded",
        {
            "id": grade.id,
            "student_id": grade.student_id,
            "subject_id": grade.subject_id,
            "assessment_type": grade.assessment_type,
            "score": grade.score,
            "max_score": grade.max_score,
        },
    )

    return {
        "id": grade.id,
        "student_id": grade.student_id,
        "subject_id": grade.subject_id,
        "score": grade.score,
        "max_score": grade.max_score,
    }


@router.post("/students", status_code=201)
async def create_student(
    student_data: StudentCreate,
    background_tasks: BackgroundTasks,
    context=Depends(require_api_scope("students.record.manage")),
    session: AsyncSession = Depends(get_session),
):
    """Create a student — for a district roster-sync integration pushing
    enrollment data into Campusio. Reuses the internal endpoint's
    student_id generation/uniqueness check and class-capacity/enrollment
    tracking (routers/students.py:check_class_capacity/_record_enrollment).
    Deliberately does NOT auto-create a student portal login the way the
    internal UI endpoint does — a bulk roster sync has no use for a fresh
    portal password on every synced record, and returning credentials in
    a machine-to-machine API response is the kind of thing worth avoiding
    unless a caller actually asks for it. Emits the same student.created
    event as the internal endpoint."""
    school_id = context.school_id

    if not student_data.student_id:
        student_data.student_id = f"STU-{datetime.now().year}-{secrets.token_hex(3).upper()}"

    existing = await session.execute(
        select(Student).where(Student.school_id == school_id, Student.student_id == student_data.student_id)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Student ID already exists")

    if student_data.class_id:
        await check_class_capacity(session, school_id, student_data.class_id)

    student = Student(school_id=school_id, **student_data.model_dump())
    session.add(student)
    await session.flush()

    if student_data.class_id:
        await _record_enrollment(session, school_id, student.id, student_data.class_id, reason="admitted")

    await session.commit()
    await session.refresh(student)

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

    return {
        "id": student.id,
        "student_id": student.student_id,
        "first_name": student.first_name,
        "last_name": student.last_name,
        "status": student.status,
    }


@router.post("/staff", status_code=201)
async def create_staff(
    staff_data: StaffCreate,
    background_tasks: BackgroundTasks,
    context=Depends(require_api_scope("staff.record.manage")),
    session: AsyncSession = Depends(get_session),
):
    """Create a staff member — for an HR system pushing new hires into
    Campusio. Reuses the internal endpoint's staff_id generation/
    uniqueness check (routers/staff.py:create_staff) and emits the same
    staff.created event. Like the internal endpoint, this does not create
    a portal login — that stays a separate, deliberate step."""
    school_id = context.school_id

    if not staff_data.staff_id:
        staff_data.staff_id = f"STF-{datetime.now().year}-{secrets.token_hex(3).upper()}"

    existing = await session.execute(
        select(Staff).where(Staff.school_id == school_id, Staff.staff_id == staff_data.staff_id)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Staff ID already exists")

    staff = Staff(school_id=school_id, **staff_data.model_dump())
    session.add(staff)
    await session.commit()
    await session.refresh(staff)

    await emit_event(
        session, background_tasks, school_id, "staff.created",
        {
            "id": staff.id,
            "staff_id": staff.staff_id,
            "first_name": staff.first_name,
            "last_name": staff.last_name,
            "position": staff.position,
        },
    )

    return {
        "id": staff.id,
        "staff_id": staff.staff_id,
        "first_name": staff.first_name,
        "last_name": staff.last_name,
        "status": staff.status,
    }


@router.post("/fees", status_code=201)
async def create_fee(
    fee_data: FeeCreate,
    background_tasks: BackgroundTasks,
    context=Depends(require_api_scope("fees.status.manage")),
    session: AsyncSession = Depends(get_session),
):
    """Create a fee/invoice for a student — for an external billing system
    assigning charges into Campusio. Reuses the internal endpoint's
    duplicate-fee-structure check (routers/fees.py:create_student_fee) and
    emits the same fee.invoice.created event."""
    school_id = context.school_id

    existing = await session.execute(
        select(Fee).where(
            Fee.student_id == fee_data.student_id,
            Fee.fee_structure_id == fee_data.fee_structure_id,
        )
    )
    if existing.scalars().first():
        raise HTTPException(status_code=409, detail="This fee structure is already assigned to this student")

    fee = Fee(school_id=school_id, **fee_data.model_dump())
    session.add(fee)
    await session.commit()
    await session.refresh(fee)

    await emit_event(
        session, background_tasks, school_id, "fee.invoice.created",
        {
            "id": fee.id,
            "student_id": fee.student_id,
            "amount_due": fee.amount_due,
            "status": fee.status,
        },
    )

    return {
        "id": fee.id,
        "student_id": fee.student_id,
        "amount_due": fee.amount_due,
        "status": fee.status,
    }


@router.post("/classes", status_code=201)
async def create_class(
    class_data: ClassCreate,
    background_tasks: BackgroundTasks,
    context=Depends(require_api_scope("classes.record.manage")),
    session: AsyncSession = Depends(get_session),
):
    """Create a class — for an external timetabling/scheduling system
    pushing its class list into Campusio. Reuses the internal endpoint's
    academic-term validation (routers/classes.py:validate_academic_term)
    and duplicate-class handling, and emits a new class.created event."""
    school_id = context.school_id

    await validate_academic_term(session, school_id, class_data.academic_term_id)

    cls = Class(school_id=school_id, **class_data.model_dump())
    session.add(cls)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=400, detail="A class with this name, level, and section already exists")
    await session.refresh(cls)

    await emit_event(
        session, background_tasks, school_id, "class.created",
        {
            "id": cls.id,
            "name": cls.name,
            "level": cls.level,
            "section": cls.section,
        },
    )

    return {
        "id": cls.id,
        "name": cls.name,
        "level": cls.level,
        "section": cls.section,
    }


@router.post("/announcements", status_code=201)
async def create_announcement(
    announcement_data: AnnouncementCreate,
    background_tasks: BackgroundTasks,
    context=Depends(require_api_scope("announcements.record.manage")),
    session: AsyncSession = Depends(get_session),
):
    """Publish an announcement — for an external communication/marketing
    tool pushing school news into Campusio. Mirrors the internal endpoint
    (routers/communication.py:create_announcement) and emits a new
    announcement.published event, attributing created_by to the API key
    the same way writes elsewhere in this file attribute recorded_by."""
    school_id = context.school_id

    announcement = Announcement(
        school_id=school_id,
        created_by=f"api_key:{context.api_key_id}",
        **announcement_data.model_dump(),
    )
    session.add(announcement)
    await session.commit()
    await session.refresh(announcement)

    await emit_event(
        session, background_tasks, school_id, "announcement.published",
        {
            "id": announcement.id,
            "title": announcement.title,
            "announcement_type": announcement.announcement_type,
            "audience": announcement.audience,
        },
    )

    return {
        "id": announcement.id,
        "title": announcement.title,
        "announcement_type": announcement.announcement_type,
        "is_published": announcement.is_published,
    }


@router.get("/timetable/{class_id}")
async def get_class_timetable(
    class_id: str,
    academic_term_id: Optional[str] = Query(None),
    context=Depends(require_api_scope("timetable.record.view")),
    session: AsyncSession = Depends(get_session),
):
    """A class's weekly timetable, scoped to the calling API key's school.
    Defaults to the school's current term, same as the internal endpoint
    (routers/timetable.py:get_current_term_id)."""
    class_result = await session.execute(
        select(Class).where(Class.id == class_id, Class.school_id == context.school_id)
    )
    if not class_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Class not found")

    term_id = academic_term_id or await get_current_term_id(session, context.school_id)
    if not term_id:
        return []

    result = await session.execute(
        select(Timetable).where(Timetable.school_id == context.school_id, Timetable.class_id == class_id, Timetable.academic_term_id == term_id)
    )
    return [
        {
            "id": t.id,
            "class_id": t.class_id,
            "subject_id": t.subject_id,
            "teacher_id": t.teacher_id,
            "period_id": t.period_id,
            "day_of_week": t.day_of_week,
            "room": t.room,
        }
        for t in result.scalars().all()
    ]


@router.post("/timetable", status_code=201)
async def create_timetable_entry(
    timetable_data: TimetableCreate,
    background_tasks: BackgroundTasks,
    context=Depends(require_api_scope("timetable.record.manage")),
    session: AsyncSession = Depends(get_session),
):
    """Create a timetable entry — for an external scheduling system pushing
    a class schedule into Campusio. Reuses the internal endpoint's full
    validation (routers/timetable.py:create_timetable_entry): cross-tenant
    FK checks, slot-already-occupied, teacher-double-booked, and
    room-conflict — the same rules a schedule built by hand has to follow.
    Emits a new timetable.created event."""
    school_id = context.school_id

    await validate_timetable_references(
        session, school_id, timetable_data.class_id, timetable_data.subject_id,
        timetable_data.teacher_id, timetable_data.period_id, timetable_data.academic_term_id,
    )

    existing = await session.execute(
        select(Timetable).where(
            Timetable.school_id == school_id,
            Timetable.class_id == timetable_data.class_id,
            Timetable.period_id == timetable_data.period_id,
            Timetable.day_of_week == timetable_data.day_of_week,
            Timetable.academic_term_id == timetable_data.academic_term_id,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Time slot already occupied for this class")

    teacher_conflict = await session.execute(
        select(Timetable).where(
            Timetable.school_id == school_id,
            Timetable.teacher_id == timetable_data.teacher_id,
            Timetable.period_id == timetable_data.period_id,
            Timetable.day_of_week == timetable_data.day_of_week,
            Timetable.academic_term_id == timetable_data.academic_term_id,
        )
    )
    if teacher_conflict.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="This teacher is already scheduled in another class for this period")

    if timetable_data.room:
        room_conflict = await session.execute(
            select(Timetable).where(
                Timetable.school_id == school_id,
                Timetable.room == timetable_data.room,
                Timetable.period_id == timetable_data.period_id,
                Timetable.day_of_week == timetable_data.day_of_week,
                Timetable.academic_term_id == timetable_data.academic_term_id,
                Timetable.class_id != timetable_data.class_id,
            )
        )
        if room_conflict.scalar_one_or_none():
            raise HTTPException(status_code=400, detail=f"Room '{timetable_data.room}' is already booked for another class in this period")

    timetable = Timetable(school_id=school_id, **timetable_data.model_dump())
    session.add(timetable)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="This slot was just booked by someone else — please retry")
    await session.refresh(timetable)

    await emit_event(
        session, background_tasks, school_id, "timetable.created",
        {
            "id": timetable.id,
            "class_id": timetable.class_id,
            "subject_id": timetable.subject_id,
            "teacher_id": timetable.teacher_id,
            "day_of_week": timetable.day_of_week,
        },
    )

    return {
        "id": timetable.id,
        "class_id": timetable.class_id,
        "subject_id": timetable.subject_id,
        "day_of_week": timetable.day_of_week,
    }


@router.get("/students/{student_id}/discipline")
async def list_student_discipline_incidents(
    student_id: str,
    limit: int = Query(50, ge=1, le=200),
    context=Depends(require_api_scope("discipline.record.view")),
    session: AsyncSession = Depends(get_session),
):
    """Discipline incidents involving a student, scoped to the calling API
    key's school. Mirrors the internal endpoint's student_id filter
    (routers/discipline.py:list_incidents)."""
    subquery = select(IncidentStudent.incident_id).where(IncidentStudent.student_id == student_id)
    result = await session.execute(
        select(IncidentReport)
        .where(IncidentReport.school_id == context.school_id, IncidentReport.id.in_(subquery))
        .order_by(IncidentReport.created_at.desc())
        .limit(limit)
    )
    return [await _serialize_incident(session, i) for i in result.scalars().all()]


@router.post("/discipline", status_code=201)
async def create_discipline_incident(
    data: IncidentReportCreate,
    background_tasks: BackgroundTasks,
    context=Depends(require_api_scope("discipline.record.manage")),
    session: AsyncSession = Depends(get_session),
):
    """Report a discipline incident — for an external behavior-tracking
    tool pushing records into Campusio. Mirrors the internal endpoint
    (routers/discipline.py:create_incident) exactly, including its one
    validation rule (at least one linked student), and emits a new
    discipline.incident.reported event. `reporter_staff_id` is stamped
    with the API key's attribution, same convention as `recorded_by`
    elsewhere in this file."""
    school_id = context.school_id
    if not data.student_ids:
        raise HTTPException(status_code=400, detail="At least one student must be linked to the incident")

    incident_fields = data.model_dump(exclude={"student_ids"})
    incident = IncidentReport(**incident_fields, school_id=school_id, reporter_staff_id=f"api_key:{context.api_key_id}")
    session.add(incident)
    await session.flush()

    for student_id in data.student_ids:
        session.add(IncidentStudent(incident_id=incident.id, student_id=student_id))

    await session.commit()
    await session.refresh(incident)

    await emit_event(
        session, background_tasks, school_id, "discipline.incident.reported",
        {
            "id": incident.id,
            "category": incident.category,
            "severity": incident.severity,
            "student_ids": data.student_ids,
        },
    )

    return await _serialize_incident(session, incident)


@router.get("/library/items")
async def list_library_items(
    limit: int = Query(50, ge=1, le=200),
    context=Depends(require_api_scope("library.catalog.view")),
    session: AsyncSession = Depends(get_session),
):
    """Published library catalog items for the calling API key's school —
    for an integrator building its own discovery UI over Campusio's
    e-library."""
    result = await session.execute(
        select(LibraryItem)
        .where(LibraryItem.school_id == context.school_id, LibraryItem.is_published == True)  # noqa: E712
        .order_by(LibraryItem.title)
        .limit(limit)
    )
    return [
        {
            "id": item.id,
            "title": item.title,
            "author": item.author,
            "material_type": item.material_type,
            "content_type": item.content_type,
            "isbn": item.isbn,
            "publication_year": item.publication_year,
        }
        for item in result.scalars().all()
    ]


@router.post("/library/loans", status_code=201)
async def create_library_loan(
    payload: LibraryLoanCreate,
    background_tasks: BackgroundTasks,
    context=Depends(require_api_scope("library.loan.manage")),
    session: AsyncSession = Depends(get_session),
):
    """Check out a library book copy — for an external circulation
    device/kiosk (e.g. a barcode scanner at a library desk). Reuses the
    internal endpoint's copy/borrower/reservation logic
    (routers/library_circulation.py:issue_loan) and emits a new
    library.loan.issued event. `issued_by` is left unset (unlike
    `recorded_by` elsewhere in this file) since it's a real foreign key to
    `users.id` — an API key has no corresponding human staff row to
    attribute it to."""
    school_id = context.school_id

    # Locked (FOR UPDATE) as the FIRST select of this row, sibling of the
    # same race just fixed on the internal endpoint
    # (routers/library_circulation.py::issue_loan) — deliberately NOT using
    # the shared _get_copy_or_404 helper here (it selects unlocked), since a
    # second, re-fetching locked select after that earlier unlocked load
    # would hit the ORM identity map and return the already-loaded Python
    # object's stale attributes rather than the freshly-locked row.
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

    if copy.status != CopyStatus.AVAILABLE.value:
        raise HTTPException(status_code=400, detail=f"This copy is not available (status: {copy.status})")

    issue_date = datetime.utcnow().date()
    due_date = payload.due_date or (issue_date + timedelta(days=DEFAULT_LOAN_DAYS)).isoformat()

    loan = LibraryLoan(
        school_id=school_id,
        copy_id=copy.id,
        borrower_user_id=payload.borrower_user_id,
        issued_by=None,
        issue_date=issue_date.isoformat(),
        due_date=due_date,
        status=LoanStatus.ACTIVE.value,
    )
    session.add(loan)

    copy.status = CopyStatus.CHECKED_OUT.value
    copy.updated_at = datetime.utcnow()

    await session.commit()
    await session.refresh(loan)

    await emit_event(
        session, background_tasks, school_id, "library.loan.issued",
        {
            "id": loan.id,
            "copy_id": loan.copy_id,
            "borrower_user_id": loan.borrower_user_id,
            "due_date": loan.due_date,
        },
    )

    return await _serialize_loan(session, loan)


@router.get("/tickets")
async def list_tickets(
    ticket_status: Optional[str] = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=200),
    context=Depends(require_api_scope("tickets.record.view")),
    session: AsyncSession = Depends(get_session),
):
    """Support tickets for the calling API key's school."""
    query = select(Ticket).where(Ticket.school_id == context.school_id)
    if ticket_status:
        query = query.where(Ticket.status == ticket_status)
    result = await session.execute(query.order_by(Ticket.created_at.desc()).limit(limit))
    return [
        {
            "id": t.id,
            "category": t.category,
            "priority": t.priority,
            "title": t.title,
            "status": t.status,
            "assigned_to_id": t.assigned_to_id,
            "created_at": t.created_at.isoformat(),
        }
        for t in result.scalars().all()
    ]


@router.post("/tickets", status_code=201)
async def create_ticket(
    ticket_data: TicketCreate,
    background_tasks: BackgroundTasks,
    context=Depends(require_api_scope("tickets.record.manage")),
    session: AsyncSession = Depends(get_session),
):
    """Open a support ticket — for an external helpdesk tool. Delegates to
    the same service the internal UI uses (services/ticket_service.py:
    TicketService.create_ticket), attributing created_by_id to the API
    key, and emits a new ticket.created event."""
    school_id = context.school_id

    ticket = await TicketService.create_ticket(
        session, school_id, f"api_key:{context.api_key_id}", ticket_data
    )

    await emit_event(
        session, background_tasks, school_id, "ticket.created",
        {
            "id": ticket.id,
            "category": ticket.category,
            "priority": ticket.priority,
            "title": ticket.title,
            "status": ticket.status,
        },
    )

    return {
        "id": ticket.id,
        "category": ticket.category,
        "priority": ticket.priority,
        "title": ticket.title,
        "status": ticket.status,
    }


@router.get("/transport/routes")
async def list_transport_routes(
    limit: int = Query(50, ge=1, le=200),
    context=Depends(require_api_scope("transport.record.view")),
    session: AsyncSession = Depends(get_session),
):
    """Bus routes for the calling API key's school."""
    result = await session.execute(
        select(Route).where(Route.school_id == context.school_id).order_by(Route.route_name).limit(limit)
    )
    return [
        {
            "id": r.id,
            "route_name": r.route_name,
            "route_code": r.route_code,
            "start_point": r.start_point,
            "end_point": r.end_point,
            "pickup_time": r.pickup_time,
            "dropoff_time": r.dropoff_time,
            "fee_amount": r.fee_amount,
            "status": r.status,
        }
        for r in result.scalars().all()
    ]


@router.post("/transport/enrollments", status_code=201)
async def create_transport_enrollment(
    enrollment_data: StudentTransportCreate,
    background_tasks: BackgroundTasks,
    context=Depends(require_api_scope("transport.record.manage")),
    session: AsyncSession = Depends(get_session),
):
    """Enroll a student on a bus route — for an external transport/fleet
    system pushing enrollments into Campusio. Mirrors the internal
    endpoint's checks exactly (routers/transport.py:enroll_student_transport):
    student and route must belong to this school, and the student can't
    already be enrolled on the same route. Emits a new
    transport.enrollment.created event."""
    school_id = context.school_id

    student_result = await session.execute(
        select(Student).where(Student.id == enrollment_data.student_id, Student.school_id == school_id)
    )
    if not student_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Student not found")

    route_result = await session.execute(
        select(Route).where(Route.id == enrollment_data.route_id, Route.school_id == school_id)
    )
    if not route_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Route not found")

    existing = await session.execute(
        select(StudentTransport).where(
            StudentTransport.student_id == enrollment_data.student_id,
            StudentTransport.route_id == enrollment_data.route_id,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Student is already enrolled in this route")

    enrollment = StudentTransport(school_id=school_id, **enrollment_data.model_dump())
    session.add(enrollment)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=400, detail="Student is already enrolled in this route")
    await session.refresh(enrollment)

    await emit_event(
        session, background_tasks, school_id, "transport.enrollment.created",
        {
            "id": enrollment.id,
            "student_id": enrollment.student_id,
            "route_id": enrollment.route_id,
        },
    )

    return {
        "id": enrollment.id,
        "student_id": enrollment.student_id,
        "route_id": enrollment.route_id,
        "enrollment_date": enrollment.enrollment_date,
    }


@router.get("/hostel/rooms")
async def list_hostel_rooms(
    limit: int = Query(50, ge=1, le=200),
    context=Depends(require_api_scope("hostel.record.view")),
    session: AsyncSession = Depends(get_session),
):
    """Hostel rooms for the calling API key's school."""
    result = await session.execute(
        select(Room).where(Room.school_id == context.school_id).order_by(Room.room_number).limit(limit)
    )
    return [
        {
            "id": r.id,
            "hostel_id": r.hostel_id,
            "room_number": r.room_number,
            "room_type": r.room_type,
            "capacity": r.capacity,
            "current_occupancy": r.current_occupancy,
            "status": r.status,
        }
        for r in result.scalars().all()
    ]


@router.post("/hostel/allocations", status_code=201)
async def create_hostel_allocation(
    allocation_data: RoomAllocationCreate,
    background_tasks: BackgroundTasks,
    context=Depends(require_api_scope("hostel.record.manage")),
    session: AsyncSession = Depends(get_session),
):
    """Allocate a hostel room to a student — for an external boarding-
    management system. Mirrors the internal endpoint's capacity check
    (routers/hostel.py:allocate_room_to_student), including the same
    one-active-allocation-per-student dedupe check and the row lock
    (.with_for_update()) on the Room select — this endpoint had reintroduced
    both of those exact races/gaps that were already fixed on the internal
    path (see project_campusio_toctou_races_batch_fix memory). Increments
    Room.current_occupancy the same way the internal endpoint does, so the
    two paths can't drift the counter out of sync with each other. Emits a
    new hostel.allocation.created event."""
    school_id = context.school_id

    # A student can only hold one active (not yet deallocated) room
    # allocation at a time — same dedupe check the internal endpoint
    # enforces (routers/hostel.py::allocate_room_to_student); this endpoint
    # had omitted it, so repeated external calls could double-allocate a
    # student to two rooms simultaneously.
    existing_result = await session.execute(
        select(RoomAllocation).where(
            RoomAllocation.student_id == allocation_data.student_id,
            RoomAllocation.school_id == school_id,
            RoomAllocation.deallocation_date.is_(None),
        )
    )
    if existing_result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="This student already has an active room allocation — deallocate it first")

    # Locked (FOR UPDATE), same rationale as the internal endpoint — without
    # this, two concurrent external calls can both read the same
    # current_occupancy and both pass the capacity check, overbooking the room.
    room_result = await session.execute(
        select(Room).where(Room.id == allocation_data.room_id, Room.school_id == school_id).with_for_update()
    )
    room = room_result.scalar_one_or_none()
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")

    if room.current_occupancy >= room.capacity:
        raise HTTPException(status_code=400, detail="Room is at full capacity")

    allocation = RoomAllocation(school_id=school_id, **allocation_data.model_dump())
    session.add(allocation)
    room.current_occupancy += 1
    session.add(room)
    await session.commit()
    await session.refresh(allocation)

    await emit_event(
        session, background_tasks, school_id, "hostel.allocation.created",
        {
            "id": allocation.id,
            "student_id": allocation.student_id,
            "room_id": allocation.room_id,
            "hostel_id": allocation.hostel_id,
        },
    )

    return {
        "id": allocation.id,
        "student_id": allocation.student_id,
        "room_id": allocation.room_id,
        "hostel_id": allocation.hostel_id,
        "allocation_date": allocation.allocation_date,
    }


@router.post("/hostel/allocations/{allocation_id}/deallocate")
async def deallocate_hostel_allocation(
    allocation_id: str,
    background_tasks: BackgroundTasks,
    context=Depends(require_api_scope("hostel.record.manage")),
    session: AsyncSession = Depends(get_session),
):
    """Free a hostel room allocation — the external-system counterpart to
    POST /hostel/allocations. Until this existed, an allocation made through
    the public API had no way to be released except a student exiting
    (routers/students.py's _release_hostel_and_transport); mirrors the
    internal admin endpoint (routers/hostel.py:deallocate_room_from_student).
    Emits a new hostel.allocation.deallocated event."""
    school_id = context.school_id

    result = await session.execute(
        select(RoomAllocation).where(
            RoomAllocation.id == allocation_id, RoomAllocation.school_id == school_id
        )
    )
    allocation = result.scalar_one_or_none()
    if not allocation:
        raise HTTPException(status_code=404, detail="Allocation not found")
    if allocation.deallocation_date:
        raise HTTPException(status_code=400, detail="This allocation has already been deallocated")

    room_result = await session.execute(select(Room).where(Room.id == allocation.room_id))
    room = room_result.scalar_one_or_none()
    if room and room.current_occupancy > 0:
        room.current_occupancy -= 1
        session.add(room)

    allocation.deallocation_date = datetime.utcnow().strftime("%Y-%m-%d")
    session.add(allocation)
    await session.commit()
    await session.refresh(allocation)

    await emit_event(
        session, background_tasks, school_id, "hostel.allocation.deallocated",
        {
            "id": allocation.id,
            "student_id": allocation.student_id,
            "room_id": allocation.room_id,
            "hostel_id": allocation.hostel_id,
        },
    )

    return {
        "id": allocation.id,
        "student_id": allocation.student_id,
        "room_id": allocation.room_id,
        "hostel_id": allocation.hostel_id,
        "deallocation_date": allocation.deallocation_date,
    }


@router.get("/admissions/applicants")
async def list_applicants(
    application_status: Optional[str] = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=200),
    context=Depends(require_api_scope("admissions.record.view")),
    session: AsyncSession = Depends(get_session),
):
    """Admissions applicants for the calling API key's school."""
    query = select(Applicant).where(Applicant.school_id == context.school_id)
    if application_status:
        query = query.where(Applicant.status == application_status)
    result = await session.execute(query.order_by(Applicant.created_at.desc()).limit(limit))
    return [
        {
            "id": a.id,
            "first_name": a.first_name,
            "last_name": a.last_name,
            "date_of_birth": a.date_of_birth,
            "gender": a.gender,
            "status": a.status,
            "applying_for_class_id": a.applying_for_class_id,
            "entrance_test_date": a.entrance_test_date,
            "entrance_test_score": a.entrance_test_score,
        }
        for a in result.scalars().all()
    ]


@router.post("/admissions/applicants", status_code=201)
async def create_applicant(
    applicant_data: ApplicantCreate,
    background_tasks: BackgroundTasks,
    context=Depends(require_api_scope("admissions.record.manage")),
    session: AsyncSession = Depends(get_session),
):
    """Create an admissions applicant — for a third-party admissions
    CRM managing prospective students across multiple schools it works
    with, distinct from routers/public_admissions.py's unauthenticated
    single-school "apply now" form (that one takes a narrower field set
    from an anonymous parent, is IP-rate-limited/honeypot-protected, and
    optionally drives a Paystack application-fee flow — a different
    caller and purpose entirely). Mirrors the internal endpoint
    (routers/admissions.py:create_applicant), including the fact that
    neither validates applying_for_class_id/applying_for_term_id against
    the school's actual classes/terms — a known gap shared by both paths,
    not introduced here. Emits a new admissions.applicant.created event."""
    school_id = context.school_id

    applicant = Applicant(school_id=school_id, **applicant_data.model_dump())
    session.add(applicant)
    await session.commit()
    await session.refresh(applicant)

    await emit_event(
        session, background_tasks, school_id, "admissions.applicant.created",
        {
            "id": applicant.id,
            "first_name": applicant.first_name,
            "last_name": applicant.last_name,
            "status": applicant.status,
        },
    )

    return {
        "id": applicant.id,
        "first_name": applicant.first_name,
        "last_name": applicant.last_name,
        "status": applicant.status,
    }


@router.get("/alumni/records")
async def list_alumni_records(
    graduation_year: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    context=Depends(require_api_scope("alumni.record.view")),
    session: AsyncSession = Depends(get_session),
):
    """Alumni records for the calling API key's school. Excludes
    phone/email/address (PII) from this list response — an integrator
    that specifically needs contact details for outreach would be a
    separate, more narrowly scoped endpoint, not folded into a general
    directory listing."""
    query = select(AlumniRecord).where(AlumniRecord.school_id == context.school_id)
    if graduation_year:
        query = query.where(AlumniRecord.graduation_year == graduation_year)
    result = await session.execute(query.order_by(AlumniRecord.graduation_year.desc()).limit(limit))
    return [
        {
            "id": a.id,
            "first_name": a.first_name,
            "last_name": a.last_name,
            "graduation_year": a.graduation_year,
            "last_class_completed": a.last_class_completed,
            "current_occupation": a.current_occupation,
            "opt_in_communications": a.opt_in_communications,
        }
        for a in result.scalars().all()
    ]


@router.post("/alumni/records", status_code=201)
async def create_alumni_record(
    data: AlumniRecordCreate,
    background_tasks: BackgroundTasks,
    context=Depends(require_api_scope("alumni.record.manage")),
    session: AsyncSession = Depends(get_session),
):
    """Create an alumni record — for an external alumni-relations/CRM
    tool. Mirrors the internal endpoint (routers/alumni.py:
    create_alumni_record) — a straight insert, no dedup check on either
    path. Emits a new alumni.record.created event."""
    school_id = context.school_id

    record = AlumniRecord(school_id=school_id, **data.model_dump())
    session.add(record)
    await session.commit()
    await session.refresh(record)

    await emit_event(
        session, background_tasks, school_id, "alumni.record.created",
        {
            "id": record.id,
            "first_name": record.first_name,
            "last_name": record.last_name,
            "graduation_year": record.graduation_year,
        },
    )

    return {
        "id": record.id,
        "first_name": record.first_name,
        "last_name": record.last_name,
        "graduation_year": record.graduation_year,
    }


# ============================================================================
# CURRICULUM
# ============================================================================

@router.get("/curriculum/topics")
async def list_curriculum_topics(
    class_id: Optional[str] = None,
    subject_id: Optional[str] = None,
    academic_term_id: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    context=Depends(require_api_scope("curriculum.record.view")),
    session: AsyncSession = Depends(get_session),
):
    """Curriculum topics (scheme of work) for the calling API key's school."""
    query = select(CurriculumTopic).where(CurriculumTopic.school_id == context.school_id)
    if class_id:
        query = query.where(CurriculumTopic.class_id == class_id)
    if subject_id:
        query = query.where(CurriculumTopic.subject_id == subject_id)
    if academic_term_id:
        query = query.where(CurriculumTopic.academic_term_id == academic_term_id)
    result = await session.execute(query.order_by(CurriculumTopic.sequence).limit(limit))
    return [
        {
            "id": t.id, "class_id": t.class_id, "subject_id": t.subject_id,
            "academic_term_id": t.academic_term_id, "title": t.title,
            "sequence": t.sequence, "planned_week": t.planned_week, "status": t.status,
        }
        for t in result.scalars().all()
    ]


@router.get("/curriculum/lesson-notes")
async def list_curriculum_lesson_notes(
    class_id: Optional[str] = None,
    subject_id: Optional[str] = None,
    lesson_date: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    context=Depends(require_api_scope("curriculum.record.view")),
    session: AsyncSession = Depends(get_session),
):
    """Teacher lesson notes for the calling API key's school."""
    query = select(TeacherLessonNote).where(TeacherLessonNote.school_id == context.school_id)
    if class_id:
        query = query.where(TeacherLessonNote.class_id == class_id)
    if subject_id:
        query = query.where(TeacherLessonNote.subject_id == subject_id)
    if lesson_date:
        query = query.where(TeacherLessonNote.lesson_date == lesson_date)
    result = await session.execute(query.order_by(TeacherLessonNote.lesson_date.desc()).limit(limit))
    return [
        {
            "id": n.id, "teacher_id": n.teacher_id, "class_id": n.class_id,
            "subject_id": n.subject_id, "lesson_date": n.lesson_date, "content": n.content,
        }
        for n in result.scalars().all()
    ]


@router.post("/curriculum/lesson-notes", status_code=201)
async def create_curriculum_lesson_note(
    data: TeacherLessonNoteCreate,
    background_tasks: BackgroundTasks,
    context=Depends(require_api_scope("curriculum.record.manage")),
    session: AsyncSession = Depends(get_session),
):
    """Push a lesson note — for an external curriculum-planning tool.
    Unlike the internal endpoint (routers/curriculum.py:create_lesson_note),
    which infers the caller's own teacher_id from their session, an API key
    has no such identity — teacher_id is REQUIRED here and validated as a
    real Staff row in this school. Mirrors the internal upsert-on-
    (teacher,class,subject,date) behavior. Emits a new
    curriculum.lesson_note.recorded event."""
    school_id = context.school_id
    if not data.teacher_id:
        raise HTTPException(status_code=422, detail="teacher_id is required")

    staff_result = await session.execute(
        select(Staff).where(Staff.id == data.teacher_id, Staff.school_id == school_id)
    )
    if not staff_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Teacher (staff) not found")

    class_result = await session.execute(select(Class).where(Class.id == data.class_id, Class.school_id == school_id))
    if not class_result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="class_id does not exist for this school")

    if data.academic_term_id:
        await validate_academic_term(session, school_id, data.academic_term_id)

    existing_result = await session.execute(
        select(TeacherLessonNote).where(
            TeacherLessonNote.school_id == school_id,
            TeacherLessonNote.teacher_id == data.teacher_id,
            TeacherLessonNote.class_id == data.class_id,
            TeacherLessonNote.subject_id == data.subject_id,
            TeacherLessonNote.lesson_date == data.lesson_date,
        )
    )
    note = existing_result.scalar_one_or_none()
    if note:
        note.content = data.content
        note.academic_term_id = data.academic_term_id
        note.updated_at = datetime.utcnow()
    else:
        note = TeacherLessonNote(school_id=school_id, **data.model_dump())
    session.add(note)
    await session.commit()
    await session.refresh(note)

    await emit_event(
        session, background_tasks, school_id, "curriculum.lesson_note.recorded",
        {"id": note.id, "teacher_id": note.teacher_id, "class_id": note.class_id, "subject_id": note.subject_id, "lesson_date": note.lesson_date},
    )

    return {
        "id": note.id, "teacher_id": note.teacher_id, "class_id": note.class_id,
        "subject_id": note.subject_id, "lesson_date": note.lesson_date, "content": note.content,
    }


@router.patch("/curriculum/topics/{topic_id}/coverage")
async def update_curriculum_topic_coverage(
    topic_id: str,
    data: TopicCoverageUpdate,
    background_tasks: BackgroundTasks,
    context=Depends(require_api_scope("curriculum.record.manage")),
    session: AsyncSession = Depends(get_session),
):
    """Mark a curriculum topic's coverage status — for an external planning
    tool tracking scheme-of-work progress. Mirrors
    routers/curriculum.py:update_topic_coverage. Emits a new
    curriculum.topic.coverage_updated event."""
    if data.status not in ("planned", "in_progress", "completed", "deferred"):
        raise HTTPException(status_code=422, detail="Invalid coverage status")

    result = await session.execute(
        select(CurriculumTopic).where(CurriculumTopic.id == topic_id, CurriculumTopic.school_id == context.school_id)
    )
    topic = result.scalar_one_or_none()
    if not topic:
        raise HTTPException(status_code=404, detail="Curriculum topic not found")

    topic.status = data.status
    topic.updated_at = datetime.utcnow()
    session.add(topic)
    await session.commit()

    await emit_event(
        session, background_tasks, context.school_id, "curriculum.topic.coverage_updated",
        {"id": topic.id, "class_id": topic.class_id, "subject_id": topic.subject_id, "status": topic.status},
    )

    return {"id": topic.id, "status": topic.status}


# ============================================================================
# TRACKS
# ============================================================================

@router.get("/tracks")
async def list_tracks_public(
    academic_term_id: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    context=Depends(require_api_scope("tracks.record.view")),
    session: AsyncSession = Depends(get_session),
):
    """Subject/elective tracks for the calling API key's school."""
    query = select(Track).where(Track.school_id == context.school_id)
    if academic_term_id:
        query = query.where(Track.academic_term_id == academic_term_id)
    result = await session.execute(query.order_by(Track.name).limit(limit))
    return [
        {"id": t.id, "name": t.name, "academic_term_id": t.academic_term_id, "class_level": t.class_level, "is_active": t.is_active}
        for t in result.scalars().all()
    ]


@router.get("/students/{student_id}/track")
async def get_student_track_public(
    student_id: str,
    academic_term_id: str = Query(..., description="Academic term to look up the track for"),
    context=Depends(require_api_scope("tracks.record.view")),
    session: AsyncSession = Depends(get_session),
):
    """A student's track enrollment for a given term."""
    student_result = await session.execute(
        select(Student).where(Student.id == student_id, Student.school_id == context.school_id)
    )
    if not student_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Student not found")

    st_result = await session.execute(
        select(StudentTrack).where(
            StudentTrack.student_id == student_id,
            StudentTrack.academic_term_id == academic_term_id,
            StudentTrack.school_id == context.school_id,
        )
    )
    student_track = st_result.scalar_one_or_none()
    if not student_track:
        return {"track": None, "student_track_id": None}

    track = await session.get(Track, student_track.track_id)
    return {
        "track": {"id": track.id, "name": track.name} if track else None,
        "student_track_id": student_track.id,
    }


@router.post("/tracks/{track_id}/students", status_code=201)
async def enroll_student_in_track_public(
    track_id: str,
    data: StudentTrackCreate,
    background_tasks: BackgroundTasks,
    context=Depends(require_api_scope("tracks.record.manage")),
    session: AsyncSession = Depends(get_session),
):
    """Enroll a student in a track for a term — for an external
    timetabling/electives-selection tool. Mirrors the internal endpoint's
    one-track-per-term rule (routers/tracks.py:enroll_student_in_track).
    Emits a new tracks.student.enrolled event."""
    school_id = context.school_id

    track_result = await session.execute(select(Track).where(Track.id == track_id, Track.school_id == school_id))
    track = track_result.scalar_one_or_none()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")

    student_result = await session.execute(
        select(Student).where(Student.id == data.student_id, Student.school_id == school_id)
    )
    student = student_result.scalar_one_or_none()
    if not student:
        raise HTTPException(status_code=400, detail="student_id does not exist for this school")

    await validate_track_level(session, student, track)

    await validate_academic_term(session, school_id, data.academic_term_id)

    existing = await session.execute(
        select(StudentTrack).where(
            StudentTrack.student_id == data.student_id,
            StudentTrack.academic_term_id == data.academic_term_id,
            StudentTrack.school_id == school_id,
        )
    )
    existing_row = existing.scalar_one_or_none()
    if existing_row:
        if existing_row.track_id == track_id:
            raise HTTPException(status_code=400, detail="Student is already enrolled in this track for this term")
        raise HTTPException(status_code=400, detail="Student is already enrolled in a different track for this term")

    enrollment = StudentTrack(school_id=school_id, track_id=track_id, student_id=data.student_id, academic_term_id=data.academic_term_id)
    session.add(enrollment)
    await session.commit()
    await session.refresh(enrollment)

    await emit_event(
        session, background_tasks, school_id, "tracks.student.enrolled",
        {"id": enrollment.id, "student_id": enrollment.student_id, "track_id": enrollment.track_id},
    )

    return {"id": enrollment.id, "student_id": enrollment.student_id, "track_id": enrollment.track_id, "academic_term_id": enrollment.academic_term_id}


# ============================================================================
# ACADEMIC CALENDAR
# ============================================================================

@router.get("/academic-calendar/years")
async def list_academic_years_public(
    limit: int = Query(50, ge=1, le=200),
    context=Depends(require_api_scope("academic_calendar.record.view")),
    session: AsyncSession = Depends(get_session),
):
    """Academic years for the calling API key's school."""
    result = await session.execute(
        select(AcademicYear).where(AcademicYear.school_id == context.school_id).order_by(AcademicYear.start_date.desc()).limit(limit)
    )
    return [
        {"id": y.id, "name": y.name, "start_date": y.start_date, "end_date": y.end_date, "status": y.status, "is_current": y.is_current}
        for y in result.scalars().all()
    ]


@router.get("/academic-calendar/events")
async def list_calendar_events_public(
    academic_year_id: Optional[str] = None,
    limit: int = Query(100, ge=1, le=500),
    context=Depends(require_api_scope("academic_calendar.record.view")),
    session: AsyncSession = Depends(get_session),
):
    """Calendar events for the calling API key's school."""
    query = select(CalendarEvent).where(CalendarEvent.school_id == context.school_id)
    if academic_year_id:
        query = query.where(CalendarEvent.academic_year_id == academic_year_id)
    result = await session.execute(query.order_by(CalendarEvent.start_date).limit(limit))
    return [
        {
            "id": e.id, "academic_year_id": e.academic_year_id, "title": e.title,
            "event_type": e.event_type, "start_date": e.start_date, "end_date": e.end_date,
            "is_instructional": e.is_instructional,
        }
        for e in result.scalars().all()
    ]


@router.post("/academic-calendar/events", status_code=201)
async def create_calendar_event_public(
    data: CalendarEventCreate,
    background_tasks: BackgroundTasks,
    context=Depends(require_api_scope("academic_calendar.record.manage")),
    session: AsyncSession = Depends(get_session),
):
    """Push a calendar event (term break, exam blackout date, ...) — for an
    external timetabling/scheduling tool. Mirrors
    routers/academic_calendar.py:create_calendar_event's date validation and
    academic_year_id school-scoping. Emits a new
    academic_calendar.event.created event."""
    school_id = context.school_id
    _valid_dates(data.start_date, data.end_date)

    if data.academic_year_id:
        year_result = await session.execute(
            select(AcademicYear).where(AcademicYear.id == data.academic_year_id, AcademicYear.school_id == school_id)
        )
        if not year_result.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="academic_year_id does not exist for this school")

    event = CalendarEvent(school_id=school_id, **data.model_dump())
    session.add(event)
    await session.commit()
    await session.refresh(event)

    await emit_event(
        session, background_tasks, school_id, "academic_calendar.event.created",
        {"id": event.id, "title": event.title, "start_date": event.start_date, "end_date": event.end_date},
    )

    return {
        "id": event.id, "title": event.title, "event_type": event.event_type,
        "start_date": event.start_date, "end_date": event.end_date,
    }


# ============================================================================
# EXAMS (internal scheduling)
# ============================================================================

@router.get("/exams/sessions")
async def list_exam_sessions_public(
    academic_term_id: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    context=Depends(require_api_scope("exams.record.view")),
    session: AsyncSession = Depends(get_session),
):
    """Internal exam sessions for the calling API key's school."""
    query = select(ExamSession).where(ExamSession.school_id == context.school_id)
    if academic_term_id:
        query = query.where(ExamSession.academic_term_id == academic_term_id)
    result = await session.execute(query.order_by(ExamSession.start_date.desc()).limit(limit))
    return [
        {
            "id": s.id, "academic_term_id": s.academic_term_id, "name": s.name,
            "start_date": s.start_date, "end_date": s.end_date, "status": s.status,
            "results_published": s.results_published,
        }
        for s in result.scalars().all()
    ]


@router.get("/exams/schedules/{schedule_id}/seating")
async def get_exam_seating_public(
    schedule_id: str,
    context=Depends(require_api_scope("exams.record.view")),
    session: AsyncSession = Depends(get_session),
):
    """Seat assignments for an exam schedule."""
    schedule_result = await session.execute(
        select(ExamSchedule).where(ExamSchedule.id == schedule_id, ExamSchedule.school_id == context.school_id)
    )
    if not schedule_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Exam schedule not found")

    result = await session.execute(
        select(ExamSeatAssignment).where(ExamSeatAssignment.exam_schedule_id == schedule_id).order_by(ExamSeatAssignment.room, ExamSeatAssignment.seat_number)
    )
    return [
        {"student_id": a.student_id, "room": a.room, "seat_number": a.seat_number}
        for a in result.scalars().all()
    ]


@router.post("/exams/sessions/{exam_session_id}/schedules", status_code=201)
async def create_exam_schedule_public(
    exam_session_id: str,
    data: ExamScheduleCreate,
    background_tasks: BackgroundTasks,
    context=Depends(require_api_scope("exams.record.manage")),
    session: AsyncSession = Depends(get_session),
):
    """Push an exam schedule (a class+subject sitting) — for an external
    timetabling SaaS. Mirrors routers/exams.py:create_exam_schedule's real
    room/class conflict detection. Emits a new exams.schedule.created event."""
    school_id = context.school_id

    session_result = await session.execute(
        select(ExamSession).where(ExamSession.id == exam_session_id, ExamSession.school_id == school_id)
    )
    exam_session = session_result.scalar_one_or_none()
    if not exam_session:
        raise HTTPException(status_code=404, detail="Exam session not found")

    class_result = await session.execute(select(Class).where(Class.id == data.class_id, Class.school_id == school_id))
    if not class_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Class not found")

    if data.start_time >= data.end_time:
        raise HTTPException(status_code=400, detail="start_time must be before end_time")

    await _check_room_conflict(session, school_id, data.exam_date, data.room, data.start_time, data.end_time)
    await _check_class_conflict(session, school_id, data.class_id, data.exam_date, data.start_time, data.end_time)

    schedule = ExamSchedule(school_id=school_id, exam_session_id=exam_session_id, **data.model_dump())
    session.add(schedule)
    await session.commit()
    await session.refresh(schedule)

    await emit_event(
        session, background_tasks, school_id, "exams.schedule.created",
        {"id": schedule.id, "exam_session_id": schedule.exam_session_id, "class_id": schedule.class_id, "subject_id": schedule.subject_id},
    )

    return {
        "id": schedule.id, "exam_session_id": schedule.exam_session_id, "class_id": schedule.class_id,
        "subject_id": schedule.subject_id, "exam_date": schedule.exam_date,
        "start_time": schedule.start_time, "end_time": schedule.end_time, "room": schedule.room,
    }


# ============================================================================
# EXAM PAPERS
# ============================================================================

@router.get("/exam-papers")
async def list_exam_papers_public(
    subject_id: Optional[str] = None,
    status: Optional[str] = None,
    exam_schedule_id: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    context=Depends(require_api_scope("exam_papers.record.view")),
    session: AsyncSession = Depends(get_session),
):
    """Exam papers for the calling API key's school."""
    query = select(ExamPaper).where(ExamPaper.school_id == context.school_id)
    if subject_id:
        query = query.where(ExamPaper.subject_id == subject_id)
    if status:
        query = query.where(ExamPaper.status == status)
    if exam_schedule_id:
        query = query.where(ExamPaper.exam_schedule_id == exam_schedule_id)
    result = await session.execute(query.order_by(ExamPaper.created_at.desc()).limit(limit))
    return [
        {
            "id": p.id, "subject_id": p.subject_id, "exam_schedule_id": p.exam_schedule_id,
            "title": p.title, "total_marks": p.total_marks, "status": p.status,
        }
        for p in result.scalars().all()
    ]


@router.post("/exam-papers/question-bank", status_code=201)
async def create_question_bank_item_public(
    data: QuestionBankItemCreate,
    background_tasks: BackgroundTasks,
    context=Depends(require_api_scope("exam_papers.record.manage")),
    session: AsyncSession = Depends(get_session),
):
    """Push a question-bank item — for a vendor/national question-bank
    service. Mirrors routers/exam_papers.py:create_question. Emits a new
    exam_papers.question.created event."""
    school_id = context.school_id

    subject_result = await session.execute(select(Subject).where(Subject.id == data.subject_id, Subject.school_id == school_id))
    if not subject_result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="subject_id does not exist for this school")

    item = QuestionBankItem(
        school_id=school_id, created_by=f"api_key:{context.api_key_id}",
        **{**data.model_dump(), "question_type": data.question_type.value},
    )
    session.add(item)
    await session.commit()
    await session.refresh(item)

    await emit_event(
        session, background_tasks, school_id, "exam_papers.question.created",
        {"id": item.id, "subject_id": item.subject_id, "question_type": item.question_type},
    )

    return {
        "id": item.id, "subject_id": item.subject_id, "topic": item.topic,
        "question_type": item.question_type, "marks": item.marks, "difficulty": item.difficulty,
    }


# ============================================================================
# EXAM MARKS
# ============================================================================

@router.get("/exam-marks/students/{student_id}/marks")
async def list_student_exam_marks_public(
    student_id: str,
    limit: int = Query(100, ge=1, le=500),
    context=Depends(require_api_scope("exam_marks.record.view")),
    session: AsyncSession = Depends(get_session),
):
    """Exam component marks for a student — API key sees everything,
    including unpublished sessions (it's authenticated at school level,
    same as staff, not as a parent/student)."""
    result = await session.execute(
        select(ExamComponentMark).where(
            ExamComponentMark.student_id == student_id, ExamComponentMark.school_id == context.school_id
        ).order_by(ExamComponentMark.created_at.desc()).limit(limit)
    )
    return [
        {
            "id": m.id, "exam_component_id": m.exam_component_id, "score": m.score,
            "annulled": m.annulled, "remarks": m.remarks,
        }
        for m in result.scalars().all()
    ]


@router.post("/exam-marks/components/{component_id}/marks/bulk", status_code=201)
async def bulk_upsert_exam_marks_public(
    component_id: str,
    data: BulkExamComponentMarksUpsert,
    background_tasks: BackgroundTasks,
    context=Depends(require_api_scope("exam_marks.record.manage")),
    session: AsyncSession = Depends(get_session),
):
    """Bulk-push component marks — for a scanning/OCR marking system.
    Mirrors routers/exam_marks.py:upsert_marks's score-range validation.
    Emits a new exam_marks.recorded event."""
    school_id = context.school_id
    component = await _get_component_or_404(session, school_id, component_id)

    existing_result = await session.execute(
        select(ExamComponentMark).where(ExamComponentMark.exam_component_id == component_id)
    )
    existing_by_student = {m.student_id: m for m in existing_result.scalars().all()}

    created, updated = 0, 0
    for entry in data.marks:
        if entry.score < 0 or entry.score > component.max_marks:
            raise HTTPException(status_code=422, detail=f"Score for student {entry.student_id} must be between 0 and {component.max_marks}")
        existing = existing_by_student.get(entry.student_id)
        if existing:
            existing.score = entry.score
            existing.remarks = entry.remarks
            existing.recorded_by = f"api_key:{context.api_key_id}"
            existing.updated_at = datetime.utcnow()
            session.add(existing)
            updated += 1
        else:
            session.add(ExamComponentMark(
                school_id=school_id, exam_component_id=component_id, student_id=entry.student_id,
                score=entry.score, remarks=entry.remarks, recorded_by=f"api_key:{context.api_key_id}",
            ))
            created += 1

    await session.commit()

    await emit_event(
        session, background_tasks, school_id, "exam_marks.recorded",
        {"exam_component_id": component_id, "created": created, "updated": updated},
    )

    return {"created": created, "updated": updated}


# ============================================================================
# EXAM REMARKS (read-only — see plan for why no write scope exists)
# ============================================================================

@router.get("/exam-remarks")
async def list_exam_remark_requests_public(
    student_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    context=Depends(require_api_scope("exam_remarks.record.view")),
    session: AsyncSession = Depends(get_session),
):
    """Exam remark/recheck requests for the calling API key's school —
    read-only. Every write in this domain is identity-bound to a specific
    human requester or reviewer (see routers/exam_remarks.py); there's no
    write scope for this domain."""
    query = select(ExamRemarkRequest).where(ExamRemarkRequest.school_id == context.school_id)
    if student_id:
        query = query.where(ExamRemarkRequest.student_id == student_id)
    if status:
        query = query.where(ExamRemarkRequest.status == status)
    result = await session.execute(query.order_by(ExamRemarkRequest.created_at.desc()).limit(limit))
    return [
        {
            "id": r.id, "student_id": r.student_id, "exam_component_id": r.exam_component_id,
            "reason": r.reason, "original_score": r.original_score, "revised_score": r.revised_score,
            "status": r.status,
        }
        for r in result.scalars().all()
    ]


# ============================================================================
# EXAM MALPRACTICE
# ============================================================================

@router.get("/exam-malpractice/cases")
async def list_malpractice_cases_public(
    status: Optional[str] = None,
    exam_schedule_id: Optional[str] = None,
    student_id: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    context=Depends(require_api_scope("exam_malpractice.record.view")),
    session: AsyncSession = Depends(get_session),
):
    """Exam malpractice cases for the calling API key's school."""
    query = select(MalpracticeCase).where(MalpracticeCase.school_id == context.school_id)
    if status:
        query = query.where(MalpracticeCase.status == status)
    if exam_schedule_id:
        query = query.where(MalpracticeCase.exam_schedule_id == exam_schedule_id)
    if student_id:
        query = query.where(MalpracticeCase.student_id == student_id)
    result = await session.execute(query.order_by(MalpracticeCase.created_at.desc()).limit(limit))
    return [
        {
            "id": c.id, "exam_schedule_id": c.exam_schedule_id, "student_id": c.student_id,
            "category": c.category, "description": c.description, "status": c.status,
        }
        for c in result.scalars().all()
    ]


@router.post("/exam-malpractice/cases", status_code=201)
async def create_malpractice_case_public(
    data: MalpracticeCaseCreate,
    background_tasks: BackgroundTasks,
    context=Depends(require_api_scope("exam_malpractice.record.manage")),
    session: AsyncSession = Depends(get_session),
):
    """Report an exam malpractice case — for e.g. a proctoring/anti-
    cheating system flagging incidents automatically. Mirrors
    routers/exam_malpractice.py:create_case's FK validation. Investigation
    and resolution (with its mark-annulment side effect) stay staff-only —
    not exposed here. Emits a new exam_malpractice.case.reported event."""
    school_id = context.school_id

    schedule_result = await session.execute(
        select(ExamSchedule).where(ExamSchedule.id == data.exam_schedule_id, ExamSchedule.school_id == school_id)
    )
    if not schedule_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Exam schedule not found")

    student_result = await session.execute(
        select(Student).where(Student.id == data.student_id, Student.school_id == school_id)
    )
    if not student_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Student not found")

    case = MalpracticeCase(
        school_id=school_id, reported_by=f"api_key:{context.api_key_id}",
        exam_schedule_id=data.exam_schedule_id, student_id=data.student_id,
        exam_component_id=data.exam_component_id, category=data.category.value, description=data.description,
    )
    session.add(case)
    await session.commit()
    await session.refresh(case)

    await emit_event(
        session, background_tasks, school_id, "exam_malpractice.case.reported",
        {"id": case.id, "exam_schedule_id": case.exam_schedule_id, "student_id": case.student_id, "category": case.category},
    )

    return {
        "id": case.id, "exam_schedule_id": case.exam_schedule_id, "student_id": case.student_id,
        "category": case.category, "status": case.status,
    }


# ============================================================================
# EXAM BOARD (external board — WAEC/BECE etc.)
# ============================================================================

@router.get("/exam-board/registrations")
async def list_exam_board_registrations_public(
    exam_name: Optional[str] = None,
    exam_year: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    context=Depends(require_api_scope("exam_board.record.view")),
    session: AsyncSession = Depends(get_session),
):
    """External exam board registrations for the calling API key's school."""
    query = select(ExamBoardRegistration).where(ExamBoardRegistration.school_id == context.school_id)
    if exam_name:
        query = query.where(ExamBoardRegistration.exam_name == exam_name)
    if exam_year:
        query = query.where(ExamBoardRegistration.exam_year == exam_year)
    if status:
        query = query.where(ExamBoardRegistration.registration_status == status)
    result = await session.execute(query.order_by(ExamBoardRegistration.created_at.desc()).limit(limit))
    return [
        {
            "id": r.id, "student_id": r.student_id, "exam_name": r.exam_name, "exam_year": r.exam_year,
            "index_number": r.index_number, "registration_status": r.registration_status,
        }
        for r in result.scalars().all()
    ]


@router.get("/exam-board/registrations/{registration_id}/results")
async def list_exam_board_results_public(
    registration_id: str,
    context=Depends(require_api_scope("exam_board.record.view")),
    session: AsyncSession = Depends(get_session),
):
    """Per-subject board results for a registration."""
    registration_result = await session.execute(
        select(ExamBoardRegistration).where(
            ExamBoardRegistration.id == registration_id, ExamBoardRegistration.school_id == context.school_id
        )
    )
    if not registration_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Registration not found")

    results = (await session.execute(
        select(ExamBoardResult).where(ExamBoardResult.registration_id == registration_id)
    )).scalars().all()
    return [
        {"subject_id": r.subject_id, "grade": r.grade, "score": r.score, "remarks": r.remarks}
        for r in results
    ]


@router.post("/exam-board/registrations/{registration_id}/results", status_code=201)
async def submit_exam_board_results_public(
    registration_id: str,
    data: ExamBoardResultsSubmit,
    background_tasks: BackgroundTasks,
    context=Depends(require_api_scope("exam_board.record.manage")),
    session: AsyncSession = Depends(get_session),
):
    """Push official per-subject results back into Campusio — the clearest
    use case in this whole domain: the exam board's own system or its
    results-distribution middleware. Mirrors
    routers/exam_board.py:submit_results, including the
    _maybe_mark_results_received status-flip. Emits a new
    exam_board.result.recorded event."""
    school_id = context.school_id

    registration = (await session.execute(
        select(ExamBoardRegistration).where(
            ExamBoardRegistration.id == registration_id, ExamBoardRegistration.school_id == school_id
        )
    )).scalar_one_or_none()
    if not registration:
        raise HTTPException(status_code=404, detail="Registration not found")

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
                grade=entry.grade, score=entry.score, remarks=entry.remarks,
                recorded_by=f"api_key:{context.api_key_id}",
            ))

    await session.flush()
    await _maybe_mark_results_received(session, registration)
    await session.commit()
    await session.refresh(registration)

    await emit_event(
        session, background_tasks, school_id, "exam_board.result.recorded",
        {"registration_id": registration.id, "registration_status": registration.registration_status},
    )

    return {"id": registration.id, "registration_status": registration.registration_status}


@router.post("/exam-board/registrations/bulk-index-numbers", status_code=201)
async def bulk_import_index_numbers_public(
    data: BulkIndexNumberImport,
    background_tasks: BackgroundTasks,
    context=Depends(require_api_scope("exam_board.record.manage")),
    session: AsyncSession = Depends(get_session),
):
    """The board issuing index numbers back to the school in bulk. Mirrors
    routers/exam_board.py:bulk_import_index_numbers's CSV format
    ('student_id,index_number', header tolerated) and status-progression
    guard. Emits a single summary exam_board.registration.index_issued
    event (not one per row)."""
    school_id = context.school_id

    updated = []
    not_found = []
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

        registration.index_number = index_number
        if registration.registration_status in (
            ExamRegistrationStatus.PENDING, ExamRegistrationStatus.SUBMITTED, ExamRegistrationStatus.CONFIRMED
        ):
            registration.registration_status = ExamRegistrationStatus.INDEX_ISSUED
        registration.updated_at = datetime.utcnow()
        session.add(registration)
        updated.append({"student_id": student_code, "registration_id": registration.id, "index_number": index_number})

    await session.commit()

    await emit_event(
        session, background_tasks, school_id, "exam_board.registration.index_issued",
        {"exam_name": data.exam_name, "exam_year": data.exam_year, "updated_count": len(updated)},
    )

    return {"updated": updated, "not_found": not_found, "updated_count": len(updated), "not_found_count": len(not_found)}
