"""Student Portal Router - API endpoints for students to view their own information"""
from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File
from fastapi.encoders import jsonable_encoder
from sqlmodel import select, SQLModel
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime, timedelta
from typing import List, Optional
import json
import logging
import os
import uuid
from models.user import User, UserRole
from models.student import Student
from models.grade import Grade
from models.fee import Fee, FeePayment, FeeStructure, PaymentStatus
from models.attendance import Attendance, AttendanceStatus
from models.classroom import Class, Subject
from models.timetable import Timetable, Period, DayOfWeek
from models.school import AcademicTerm
from models.communication import Announcement
from models.staff import Staff
from models.assignment import (
    Assignment, Submission, SubmissionStatus, AssignmentStatus, AssignmentQuestion,
    CourseModule, CourseModuleItem, StudentModuleProgress,
)
from models.hostel import StudentHostel, HostelFee
from models.transport import StudentTransport, TransportFee, Route
from models.library import LibraryItem
from database import get_session
from auth import get_current_user, require_roles
from services.auto_grader import AutoGrader
from services.question_formatting import format_assignment_questions
from services.submission_lifecycle import resolve_submission_status
from services.assignment_grade_bridge import sync_submission_to_grade
from services import grading_service
from services.report_card_pdf_service import compute_subject_ges_totals, compute_overall_ges_score
from utils import grade_scale as shared_ges_scale

router = APIRouter(prefix="/student-portal", tags=["Student Portal"])
logger = logging.getLogger(__name__)


async def _get_current_term_id(session: AsyncSession, school_id: str) -> Optional[str]:
    """Without this, the timetable endpoints below showed every term's entries
    stacked together the moment a school had more than one term of data."""
    result = await session.execute(
        select(AcademicTerm).where(AcademicTerm.school_id == school_id, AcademicTerm.is_current == True)
    )
    term = result.scalar_one_or_none()
    return term.id if term else None


class SubmitAssignmentRequest(SQLModel):
    submission_text: Optional[str] = None
    answers: Optional[dict] = None



# GES Grading Scale (single shared source — see utils/grade_scale.py)
GES_GRADE_SCALE = shared_ges_scale.GES_GRADE_SCALE
get_letter_grade = shared_ges_scale.get_letter_grade


async def get_student_record(user: User, session: AsyncSession) -> Student:
    """Get student record linked to user"""
    result = await session.execute(
        select(Student).where(Student.user_id == user.id)
    )
    student = result.scalar_one_or_none()
    if not student:
        raise HTTPException(status_code=404, detail="Student record not found")
    return student


@router.get("/profile", response_model=dict)
async def get_my_profile(
    current_user: User = Depends(require_roles(UserRole.STUDENT)),
    session: AsyncSession = Depends(get_session)
):
    """Get student's own profile"""
    student = await get_student_record(current_user, session)
    
    # Get class info
    class_name = None
    class_level = None
    if student.class_id:
        class_result = await session.execute(select(Class).where(Class.id == student.class_id))
        cls = class_result.scalar_one_or_none()
        if cls:
            class_name = cls.name
            class_level = cls.level
    
    return {
        "id": student.id,
        "student_id": student.student_id,
        "first_name": student.first_name,
        "last_name": student.last_name,
        "full_name": f"{student.first_name} {student.last_name}",
        "date_of_birth": student.date_of_birth if student.date_of_birth else None,
        "gender": student.gender,
        "class_id": student.class_id,
        "class_name": class_name,
        "class_level": class_level,
        "admission_date": student.admission_date if student.admission_date else None,
        "status": student.status,
        "email": current_user.email
    }


@router.get("/dashboard", response_model=dict)
async def get_student_dashboard(
    term_id: Optional[str] = None,
    current_user: User = Depends(require_roles(UserRole.STUDENT)),
    session: AsyncSession = Depends(get_session)
):
    """Get student dashboard overview, optionally filtered by academic term"""
    from sqlalchemy import or_
    
    student = await get_student_record(current_user, session)
    
    # Get class info
    class_name = None
    class_level = None
    if student.class_id:
        class_result = await session.execute(select(Class).where(Class.id == student.class_id))
        cls = class_result.scalar_one_or_none()
        class_name = cls.name if cls else None
        class_level = cls.level if cls else None
    
    # Get attendance stats (last 30 days, by the attendance date itself, not when it was recorded)
    thirty_days_ago = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")
    attendance_query = select(Attendance).where(
        Attendance.student_id == student.id,
        Attendance.attendance_date >= thirty_days_ago
    )
    
    # Filter by term if provided
    if term_id:
        attendance_query = attendance_query.where(Attendance.academic_term_id == term_id)
    
    attendance_result = await session.execute(attendance_query)
    attendance_records = attendance_result.scalars().all()
    
    # Late counts as attended, consistent with every other attendance-rate
    # calculation in the app (student history, parent portal) — this was the
    # one screen that excluded it, showing a different number for identical data.
    present_count = sum(1 for a in attendance_records if a.status in (AttendanceStatus.PRESENT, AttendanceStatus.LATE))
    total_days = len(attendance_records)
    attendance_rate = round((present_count / total_days * 100) if total_days > 0 else 0, 1)
    
    # Get grades stats
    grades_query = select(Grade).where(Grade.student_id == student.id)
    
    # Filter by term if provided
    if term_id:
        grades_query = grades_query.where(Grade.academic_term_id == term_id)
    
    grades_result = await session.execute(grades_query)
    grades = grades_result.scalars().all()
    
    # Weighted overall average via compute_overall_ges_score, using the
    # school's configured CA:exam split (falls back to 50/50) — the same
    # function report cards use, so this agrees with the report card for
    # the same term.
    schemes = await grading_service.get_school_schemes(session, student.school_id)
    subject_weights = grading_service.build_subject_weights(schemes, class_level, {g.subject_id for g in grades})
    _, overall_avg = compute_overall_ges_score(grades, weights=subject_weights)
    overall_grade = get_letter_grade(overall_avg, scale=grading_service.match_scale(schemes, class_level))

    # Get fee balance
    fee_query = select(Fee).where(Fee.student_id == student.id)
    
    # Filter by term if provided - include fees with matching term OR no term set
    if term_id:
        fee_query = fee_query.where(
            or_(
                Fee.academic_term_id == term_id,
                Fee.academic_term_id == None
            )
        )
    
    fee_result = await session.execute(fee_query)
    fees = fee_result.scalars().all()
    total_due = sum(f.amount_due for f in fees)
    total_paid = sum(f.amount_paid for f in fees)
    total_discount = sum(f.discount for f in fees)
    fee_balance = max(0, total_due - total_paid - total_discount)
    
    # Get upcoming classes (today's timetable)
    today_name = datetime.utcnow().strftime('%A').upper()
    today = DayOfWeek[today_name] if today_name in DayOfWeek.__members__ else None
    if today is not None:
        current_term_id = await _get_current_term_id(session, student.school_id)
        term_filters = [Timetable.academic_term_id == current_term_id] if current_term_id else []
        timetable_result = await session.execute(
            select(Timetable).where(
                Timetable.class_id == student.class_id,
                Timetable.day_of_week == today,
                *term_filters
            )
        )
        today_classes = timetable_result.scalars().all()
    else:
        today_classes = []
    
    # Get recent announcements
    announcement_result = await session.execute(
        select(Announcement).where(
            Announcement.school_id == student.school_id,
            Announcement.is_published == True,
            Announcement.audience.in_(["all", "students"])
        ).order_by(Announcement.publish_date.desc()).limit(3)
    )
    recent_announcements = announcement_result.scalars().all()
    
    return {
        "student": {
            "id": student.id,
            "name": f"{student.first_name} {student.last_name}",
            "student_id": student.student_id,
            "class_name": class_name
        },
        "attendance": {
            "rate": attendance_rate,
            "present": present_count,
            "total_days": total_days
        },
        "academics": {
            "overall_average": overall_avg,
            "overall_grade": overall_grade["grade"],
            "grade_description": overall_grade["description"],
            "subjects_count": len(set(g.subject_id for g in grades)),
            "assessments_count": len(grades)
        },
        "fees": {
            "balance": fee_balance,
            "status": "paid" if fee_balance <= 0 else "outstanding"
        },
        "today_classes": len(today_classes),
        "recent_announcements": [
            {
                "id": a.id,
                "title": a.title,
                "type": a.announcement_type,
                "date": a.publish_date if a.publish_date else None
            }
            for a in recent_announcements
        ]
    }


@router.get("/grades", response_model=dict)
async def get_my_grades(
    term_id: Optional[str] = None,
    current_user: User = Depends(require_roles(UserRole.STUDENT)),
    session: AsyncSession = Depends(get_session)
):
    """Get student's own grades, optionally filtered by academic term"""
    student = await get_student_record(current_user, session)

    class_level = None
    if student.class_id:
        cls = await session.get(Class, student.class_id)
        class_level = cls.level if cls else None
    schemes = await grading_service.get_school_schemes(session, student.school_id)

    # Build query to get grades
    query = select(Grade).where(Grade.student_id == student.id)
    
    # Filter by term if provided
    if term_id:
        query = query.where(Grade.academic_term_id == term_id)
    
    grades_result = await session.execute(query)
    grades = grades_result.scalars().all()
    
    # Get subjects
    subject_ids = list(set(g.subject_id for g in grades))
    subjects = {}
    if subject_ids:
        subj_result = await session.execute(select(Subject).where(Subject.id.in_(subject_ids)))
        subjects = {s.id: s for s in subj_result.scalars().all()}
    
    # Group by subject
    grades_by_subject = {}
    for grade in grades:
        subject = subjects.get(grade.subject_id)
        subject_name = subject.name if subject else "Unknown"
        subject_code = subject.code if subject else "???"
        
        if subject_name not in grades_by_subject:
            grades_by_subject[subject_name] = {
                "subject_id": grade.subject_id,
                "subject_name": subject_name,
                "subject_code": subject_code,
                "assessments": [],
                "total_score": 0,
                "total_max": 0
            }
        
        percentage = round(grade.score / grade.max_score * 100, 1)
        letter = get_letter_grade(percentage, scale=grading_service.match_scale(schemes, class_level, grade.subject_id))
        
        grades_by_subject[subject_name]["assessments"].append({
            "type": grade.assessment_type,
            "score": grade.score,
            "max_score": grade.max_score,
            "percentage": percentage,
            "grade": letter["grade"],
            "description": letter["description"],
            "date": grade.created_at.isoformat()
        })
        grades_by_subject[subject_name]["total_score"] += grade.score
        grades_by_subject[subject_name]["total_max"] += grade.max_score
    
    # Calculate averages and build subjects list — weighted via
    # compute_subject_ges_totals/compute_overall_ges_score (same functions
    # report cards use, using the school's configured CA:exam split, so
    # this agrees with the report card for the same term).
    subject_weights = grading_service.build_subject_weights(schemes, class_level, subject_ids)
    subject_ges_totals = compute_subject_ges_totals(grades, weights=subject_weights)
    subjects_list = []
    for name, data in grades_by_subject.items():
        avg = subject_ges_totals.get(data["subject_id"], {}).get("total_score", 0.0)
        letter = get_letter_grade(avg, scale=grading_service.match_scale(schemes, class_level, data["subject_id"]))
        subjects_list.append({
            "subject_name": name,
            "subject_code": data["subject_code"],
            "assessments_count": len(data["assessments"]),
            "average_percentage": avg,
            "average_grade": letter["grade"],
            "average_description": letter["description"],
            "assessments": sorted(data["assessments"], key=lambda x: x["date"], reverse=True)
        })

    # Sort by subject name
    subjects_list.sort(key=lambda x: x["subject_name"])

    # Calculate overall average
    _, overall_avg = compute_overall_ges_score(grades, weights=subject_weights)
    overall_grade = get_letter_grade(overall_avg, scale=grading_service.match_scale(schemes, class_level))

    return {
        "overall": {
            "average": overall_avg,
            "grade": overall_grade["grade"],
            "description": overall_grade["description"],
            "total_assessments": len(grades),
            "total_subjects": len(subjects_list)
        },
        "subjects": subjects_list
    }


@router.get("/attendance", response_model=dict)
async def get_my_attendance(
    days: int = 30,
    term_id: Optional[str] = None,
    current_user: User = Depends(require_roles(UserRole.STUDENT)),
    session: AsyncSession = Depends(get_session)
):
    """Get student's own attendance history, optionally filtered by academic term"""
    student = await get_student_record(current_user, session)
    
    # Build query for attendance (filtered by the attendance date itself, not when it was recorded,
    # so a late correction to an older date doesn't wrongly appear in a "recent days" window)
    cutoff_date = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    query = select(Attendance).where(
        Attendance.student_id == student.id,
        Attendance.attendance_date >= cutoff_date
    )
    
    # Filter by term if provided
    if term_id:
        query = query.where(Attendance.academic_term_id == term_id)
    
    query = query.order_by(Attendance.attendance_date.desc())
    
    attendance_result = await session.execute(query)
    records = attendance_result.scalars().all()
    
    # Calculate summary
    present = sum(1 for r in records if r.status == AttendanceStatus.PRESENT)
    absent = sum(1 for r in records if r.status == AttendanceStatus.ABSENT)
    late = sum(1 for r in records if r.status == AttendanceStatus.LATE)
    excused = sum(1 for r in records if r.status == AttendanceStatus.EXCUSED)
    total = len(records)
    
    # Group by month
    monthly_stats = {}
    for record in records:
        month_key = record.attendance_date[:7] if record.attendance_date else "unknown"
        if month_key not in monthly_stats:
            monthly_stats[month_key] = {"present": 0, "absent": 0, "late": 0, "total": 0}
        monthly_stats[month_key]["total"] += 1
        if record.status == AttendanceStatus.PRESENT:
            monthly_stats[month_key]["present"] += 1
        elif record.status == AttendanceStatus.ABSENT:
            monthly_stats[month_key]["absent"] += 1
        elif record.status == AttendanceStatus.LATE:
            monthly_stats[month_key]["late"] += 1
    
    return {
        "period_days": days,
        "summary": {
            "present": present,
            "absent": absent,
            "late": late,
            "excused": excused,
            "total_days": total,
            "attendance_rate": round(((present + late) / total * 100) if total > 0 else 0, 1)
        },
        "monthly": monthly_stats,
        "records": [
            {
                "date": r.attendance_date,
                "status": r.status,
                "remarks": r.remarks
            }
            for r in records
        ]
    }


@router.get("/timetable", response_model=dict)
async def get_my_timetable(
    current_user: User = Depends(require_roles(UserRole.STUDENT)),
    session: AsyncSession = Depends(get_session)
):
    """Get student's class timetable"""
    student = await get_student_record(current_user, session)
    
    if not student.class_id:
        return {"message": "No class assigned", "schedule": {}, "periods": []}
    
    # Get class info
    class_result = await session.execute(select(Class).where(Class.id == student.class_id))
    classroom = class_result.scalar_one_or_none()
    
    # Get periods
    periods_result = await session.execute(
        select(Period).where(
            Period.school_id == student.school_id,
            Period.is_active == True
        ).order_by(Period.period_number)
    )
    periods = {p.id: p for p in periods_result.scalars().all()}
    
    # Get timetable entries, scoped to the current term — without this, entries
    # from every past term the class has ever had would show up stacked into
    # the same day/period slots.
    current_term_id = await _get_current_term_id(session, student.school_id)
    term_filters = [Timetable.academic_term_id == current_term_id] if current_term_id else []
    timetable_result = await session.execute(
        select(Timetable).where(Timetable.class_id == student.class_id, *term_filters)
    )
    entries = timetable_result.scalars().all()
    
    # Get subjects and teachers
    subject_ids = list(set(e.subject_id for e in entries))
    teacher_ids = list(set(e.teacher_id for e in entries))
    
    subjects = {}
    if subject_ids:
        subj_result = await session.execute(select(Subject).where(Subject.id.in_(subject_ids)))
        subjects = {s.id: s for s in subj_result.scalars().all()}
    
    teachers = {}
    if teacher_ids:
        teacher_result = await session.execute(select(Staff).where(Staff.id.in_(teacher_ids)))
        teachers = {t.id: t for t in teacher_result.scalars().all()}
    
    # Build schedule
    schedule = {day.value: [] for day in DayOfWeek}
    
    for entry in entries:
        period = periods.get(entry.period_id)
        subject = subjects.get(entry.subject_id)
        teacher = teachers.get(entry.teacher_id)
        
        schedule[entry.day_of_week.value].append({
            "period_id": entry.period_id,
            "period_name": period.name if period else "Unknown",
            "period_number": period.period_number if period else 0,
            "start_time": period.start_time if period else "",
            "end_time": period.end_time if period else "",
            "subject_name": subject.name if subject else "Unknown",
            "subject_code": subject.code if subject else "???",
            "teacher_name": f"{teacher.first_name} {teacher.last_name}" if teacher else "TBA",
            "room": entry.room
        })
    
    # Sort each day's entries by period number
    for day in schedule:
        schedule[day].sort(key=lambda x: x["period_number"])
    
    return {
        "class_name": classroom.name if classroom else "Unknown",
        "class_level": classroom.level if classroom else None,
        "periods": [
            {
                "id": p.id,
                "name": p.name,
                "period_number": p.period_number,
                "start_time": p.start_time,
                "end_time": p.end_time,
                "period_type": p.period_type
            }
            for p in sorted(periods.values(), key=lambda x: x.period_number)
        ],
        "schedule": schedule
    }


@router.get("/fees", response_model=dict)
async def get_my_fees(
    term_id: Optional[str] = None,
    current_user: User = Depends(require_roles(UserRole.STUDENT)),
    session: AsyncSession = Depends(get_session)
):
    """Get student's fee details, optionally filtered by academic term"""
    from sqlalchemy import or_
    
    student = await get_student_record(current_user, session)
    
    # Build query for fees - get all student fees
    query = select(Fee).where(Fee.student_id == student.id)
    
    # Filter by term if provided - include fees with matching term OR no term set
    if term_id:
        query = query.where(
            or_(
                Fee.academic_term_id == term_id,
                Fee.academic_term_id == None
            )
        )
    
    fee_result = await session.execute(query)
    fees = fee_result.scalars().all()
    
    # Get fee structures
    structure_ids = list(set(f.fee_structure_id for f in fees))
    structures = {}
    if structure_ids:
        struct_result = await session.execute(select(FeeStructure).where(FeeStructure.id.in_(structure_ids)))
        structures = {s.id: s for s in struct_result.scalars().all()}
    
    # Get payments
    fee_ids = [f.id for f in fees]
    payments = []
    if fee_ids:
        payment_result = await session.execute(
            select(FeePayment).where(FeePayment.fee_id.in_(fee_ids))
        )
        payments = payment_result.scalars().all()
    
    # Build response
    fees_list = []
    total_due = 0
    total_paid = 0
    total_discount = 0

    for fee in fees:
        structure = structures.get(fee.fee_structure_id)
        fee_payments = [p for p in payments if p.fee_id == fee.id]

        # Calculate balance (handle case where discount might be None)
        discount = fee.discount if fee.discount else 0
        balance = fee.amount_due - fee.amount_paid - discount

        fees_list.append({
            "id": fee.id,
            "fee_type": structure.fee_type if structure else "unknown",
            "description": structure.description if structure else None,
            "amount_due": fee.amount_due,
            "amount_paid": fee.amount_paid,
            "balance": balance,
            "status": fee.status,
            "due_date": structure.due_date if structure else None,
            "payments_count": len(fee_payments)
        })
        total_due += fee.amount_due
        total_paid += fee.amount_paid
        total_discount += discount

    total_balance = max(0, total_due - total_paid - total_discount)
    return {
        "summary": {
            "total_due": total_due,
            "total_paid": total_paid,
            "balance": total_balance,
            "status": "paid" if total_balance <= 0 else "outstanding"
        },
        "fees": fees_list
    }


@router.get("/library", response_model=List[dict])
async def get_student_library(
    search: Optional[str] = None,
    category_id: Optional[str] = None,
    material_type: Optional[str] = None,
    current_user: User = Depends(require_roles(UserRole.STUDENT)),
    session: AsyncSession = Depends(get_session),
):
    """Return published library resources visible to the current student."""
    student = await get_student_record(current_user, session)
    school_id = student.school_id if hasattr(student, "school_id") else None
    if not school_id:
        raise HTTPException(status_code=400, detail="Student school context is missing")

    stmt = select(LibraryItem).where(LibraryItem.school_id == school_id, LibraryItem.is_published == True)
    if search:
        search_term = f"%{search.lower()}%"
        from sqlalchemy import or_
        stmt = stmt.where(
            or_(
                LibraryItem.title.ilike(search_term),
                LibraryItem.description.ilike(search_term),
                LibraryItem.tags.ilike(search_term),
            )
        )
    if category_id:
        stmt = stmt.where(LibraryItem.category_id == category_id)
    if material_type:
        stmt = stmt.where(LibraryItem.material_type == material_type)

    result = await session.execute(stmt.order_by(LibraryItem.created_at.desc()))
    items = result.scalars().all()

    student_class_id = getattr(student, "class_id", None)
    visible_items = []
    for item in items:
        allowed_class_ids = []
        if item.class_ids:
            allowed_class_ids = [value.strip() for value in str(item.class_ids).split(",") if value.strip()]
        if not allowed_class_ids or (student_class_id and student_class_id in allowed_class_ids):
            visible_items.append(item)

    return [
        {
            "id": item.id,
            "title": item.title,
            "description": item.description,
            "material_type": item.material_type,
            "content_type": item.content_type,
            "category_id": item.category_id,
            "class_ids": [value.strip() for value in str(item.class_ids).split(",") if value.strip()] if item.class_ids else [],
            "file_url": item.file_url,
            "external_url": item.external_url,
            "tags": (item.tags.split(",") if item.tags else []),
            "is_featured": item.is_featured,
        }
        for item in visible_items
    ]


@router.get("/announcements", response_model=List[dict])
async def get_student_announcements(
    limit: int = 20,
    current_user: User = Depends(require_roles(UserRole.STUDENT)),
    session: AsyncSession = Depends(get_session)
):
    """Get announcements for students"""
    student = await get_student_record(current_user, session)
    
    result = await session.execute(
        select(Announcement).where(
            Announcement.school_id == student.school_id,
            Announcement.is_published == True,
            Announcement.audience.in_(["all", "students"])
        ).order_by(Announcement.publish_date.desc()).limit(limit)
    )
    announcements = result.scalars().all()
    
    return [
        {
            "id": a.id,
            "title": a.title,
            "content": a.content,
            "announcement_type": a.announcement_type,
            "publish_date": a.publish_date if a.publish_date else None
        }
        for a in announcements
    ]


# ============================================================================
# ASSIGNMENTS ENDPOINTS
# ============================================================================

@router.get("/assignments/my-assignments", response_model=dict)
async def get_my_assignments(
    term_id: Optional[str] = None,
    assignment_id: Optional[str] = None,
    current_user: User = Depends(require_roles(UserRole.STUDENT)),
    session: AsyncSession = Depends(get_session)
):
    """Get assignments for the current student, optionally filtered by academic term or specific assignment ID"""
    student = await get_student_record(current_user, session)
    
    if not student.class_id:
        return {"assignments": [], "message": "No class assigned"}
    
    # Build query for assignments
    query = select(Assignment).where(
        Assignment.school_id == student.school_id,
        Assignment.class_id == student.class_id,
        Assignment.status == AssignmentStatus.PUBLISHED
    )
    
    # Filter by specific assignment if provided (from module quiz click)
    if assignment_id:
        query = query.where(Assignment.id == assignment_id)
    # Filter by term if provided
    elif term_id:
        query = query.where(Assignment.academic_term_id == term_id)
    
    query = query.order_by(Assignment.due_date)
    
    assignments_result = await session.execute(query)
    assignments = assignments_result.scalars().all()
    
    # Get student's submissions for these assignments
    submissions = {}
    if assignments:
        assignment_ids = [a.id for a in assignments]
        submissions_result = await session.execute(
            select(Submission).where(
                Submission.student_id == student.id,
                Submission.assignment_id.in_(assignment_ids)
            )
        )
        for sub in submissions_result.scalars().all():
            submissions[sub.assignment_id] = sub
    
    # Get teacher and subject info
    teacher_info = {}
    subject_info = {}
    
    assignment_list = []
    for assignment in assignments:
        # Get teacher name
        if assignment.teacher_id not in teacher_info:
            teacher_result = await session.execute(
                select(Staff).where(Staff.id == assignment.teacher_id)
            )
            staff = teacher_result.scalar_one_or_none()
            teacher_info[assignment.teacher_id] = f"{staff.first_name} {staff.last_name}" if staff else "Unknown"
        
        # Get subject name
        if assignment.subject_id not in subject_info:
            subject_result = await session.execute(
                select(Subject).where(Subject.id == assignment.subject_id)
            )
            subject = subject_result.scalar_one_or_none()
            subject_info[assignment.subject_id] = subject.name if subject else "Unknown"
        
        # Get questions for this assignment
        questions_result = await session.execute(
            select(AssignmentQuestion).where(AssignmentQuestion.assignment_id == assignment.id)
        )
        questions = questions_result.scalars().all()

        submission = submissions.get(assignment.id)
        is_graded = submission is not None and submission.status == SubmissionStatus.GRADED

        # Only reveal the correct answer once the submission has been graded,
        # otherwise a student can read the answer key before attempting it.
        formatted_questions = format_assignment_questions(questions, reveal_answers=is_graded)

        assignment_list.append({
            "id": assignment.id,
            "title": assignment.title,
            "description": assignment.description,
            "subject_name": subject_info[assignment.subject_id],
            "teacher_name": teacher_info[assignment.teacher_id],
            "assignment_type": assignment.assignment_type,
            "due_date": assignment.due_date.isoformat() if assignment.due_date else None,
            "created_at": assignment.created_date.isoformat() if assignment.created_date else None,
            "submitted_at": submission.submission_date.isoformat() if submission and submission.submission_date else None,
            "graded": submission.status == SubmissionStatus.GRADED if submission else False,
            "score": submission.score if submission else None,
            "max_score": submission.max_score if submission else assignment.points_possible,
            "percentage": round((submission.score / (submission.max_score or assignment.points_possible)) * 100, 1) if submission and submission.score else None,
            "feedback": submission.feedback if submission else None,
            "graded_at": submission.graded_date.isoformat() if submission and submission.graded_date else None,
            "points_possible": assignment.points_possible,
            "questions": formatted_questions
        })
    
    return {
        "assignments": assignment_list,
        "message": f"Found {len(assignment_list)} assignments"
    }


@router.get("/assignments/{assignment_id}", response_model=dict)
async def get_assignment_detail(
    assignment_id: str,
    current_user: User = Depends(require_roles(UserRole.STUDENT)),
    session: AsyncSession = Depends(get_session)
):
    """Get full assignment details with questions for a student"""
    student = await get_student_record(current_user, session)
    
    # Get assignment
    assignment_result = await session.execute(
        select(Assignment).where(
            Assignment.id == assignment_id,
            Assignment.school_id == student.school_id,
            Assignment.class_id == student.class_id,
            Assignment.status == AssignmentStatus.PUBLISHED
        )
    )
    assignment = assignment_result.scalar_one_or_none()
    
    if not assignment:
        raise HTTPException(status_code=404, detail="Assignment not found or not accessible")
    
    # Get questions
    questions_result = await session.execute(
        select(AssignmentQuestion).where(AssignmentQuestion.assignment_id == assignment_id)
    )
    questions = questions_result.scalars().all()

    # Get student's submission if it exists, so we know whether it's safe to reveal answers
    submission_result = await session.execute(
        select(Submission).where(
            Submission.assignment_id == assignment_id,
            Submission.student_id == student.id
        )
    )
    submission = submission_result.scalar_one_or_none()
    is_graded = submission is not None and submission.status == SubmissionStatus.GRADED

    # Only reveal the correct answer once the submission has been graded,
    # otherwise a student can read the answer key before attempting it.
    formatted_questions = format_assignment_questions(questions, reveal_answers=is_graded)

    # Get teacher name
    teacher_result = await session.execute(
        select(Staff).where(Staff.id == assignment.teacher_id)
    )
    staff = teacher_result.scalar_one_or_none()
    teacher_name = f"{staff.first_name} {staff.last_name}" if staff else "Unknown"
    
    # Get subject name
    subject_result = await session.execute(
        select(Subject).where(Subject.id == assignment.subject_id)
    )
    subject = subject_result.scalar_one_or_none()
    subject_name = subject.name if subject else "Unknown"

    return {
        "assignment": {
            "id": assignment.id,
            "class_id": assignment.class_id,
            "subject_id": assignment.subject_id,
            "teacher_id": assignment.teacher_id,
            "title": assignment.title,
            "description": assignment.description,
            "due_date": assignment.due_date.isoformat() if assignment.due_date else None,
            "points_possible": assignment.points_possible,
            "total_points": assignment.points_possible,
            "instructions": assignment.instructions,
            "assignment_type": assignment.assignment_type,
            "status": assignment.status,
            "created_at": assignment.created_date.isoformat() if assignment.created_date else None,
            "teacher_name": teacher_name,
            "subject_name": subject_name,
            "questions": formatted_questions,
        },
        "submission": {
            "submitted_at": submission.submission_date.isoformat() if submission and submission.submission_date else None,
            "status": submission.status if submission else None,
            "score": submission.score if submission else None,
        } if submission else None,
        "message": "Assignment details retrieved successfully"
    }


MAX_SUBMISSION_FILE_SIZE = 10 * 1024 * 1024  # 10MB


@router.post("/assignments/{assignment_id}/submit-file", response_model=dict)
async def submit_assignment_file(
    assignment_id: str,
    file: UploadFile = File(...),
    current_user: User = Depends(require_roles(UserRole.STUDENT)),
    session: AsyncSession = Depends(get_session)
):
    """Submit an assignment as an uploaded file (PDF, image, document, etc.).

    Kept as a dedicated multipart endpoint, separate from `/submit`, because
    FastAPI can't mix File()/Form() parsing with the JSON body the quiz-answer
    submission flow (`/submit` with an `answers` dict) relies on.
    """
    student = await get_student_record(current_user, session)

    assignment_result = await session.execute(
        select(Assignment).where(Assignment.id == assignment_id)
    )
    assignment = assignment_result.scalar_one_or_none()

    if not assignment:
        raise HTTPException(status_code=404, detail="Assignment not found")

    if assignment.class_id != student.class_id or assignment.school_id != student.school_id:
        raise HTTPException(status_code=403, detail="You do not have access to this assignment")

    if assignment.status != AssignmentStatus.PUBLISHED:
        raise HTTPException(status_code=400, detail="This assignment is not yet published")

    existing_submission_result = await session.execute(
        select(Submission).where(
            Submission.assignment_id == assignment_id,
            Submission.student_id == student.id
        )
    )
    submission = existing_submission_result.scalar_one_or_none()

    if submission and submission.status == SubmissionStatus.GRADED:
        raise HTTPException(
            status_code=400,
            detail="This assignment has already been graded and cannot be submitted again"
        )

    contents = await file.read()
    if len(contents) > MAX_SUBMISSION_FILE_SIZE:
        raise HTTPException(status_code=400, detail="File exceeds the 10MB submission limit")
    if len(contents) == 0:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    upload_dir = os.path.join("uploads", "submissions", assignment_id)
    os.makedirs(upload_dir, exist_ok=True)
    safe_name = f"{student.id}_{uuid.uuid4().hex[:8]}_{os.path.basename(file.filename or 'submission')}"
    with open(os.path.join(upload_dir, safe_name), "wb") as f:
        f.write(contents)
    file_url = f"/uploads/submissions/{assignment_id}/{safe_name}"

    now = datetime.utcnow()
    submission_status = resolve_submission_status(assignment.due_date, now)

    if not submission:
        submission = Submission(
            id=str(uuid.uuid4()),
            school_id=student.school_id,
            assignment_id=assignment_id,
            student_id=student.id,
            class_id=student.class_id,
            subject_id=assignment.subject_id,
            status=submission_status,
            submission_urls=json.dumps([file_url]),
            submission_date=now,
            max_score=assignment.points_possible
        )
        session.add(submission)
    else:
        existing_urls = json.loads(submission.submission_urls) if submission.submission_urls else []
        existing_urls.append(file_url)
        submission.submission_urls = json.dumps(existing_urls)
        submission.status = submission_status
        submission.submission_date = now

    await session.commit()
    await session.refresh(submission)

    return {
        "message": "Assignment file submitted successfully",
        "submission_id": submission.id,
        "status": submission.status,
        "file_url": file_url,
        "submitted_at": submission.submission_date.isoformat() if submission.submission_date else None
    }


@router.post("/assignments/{assignment_id}/submit", response_model=dict)
async def submit_assignment(
    assignment_id: str,
    body: SubmitAssignmentRequest,
    current_user: User = Depends(require_roles(UserRole.STUDENT)),
    session: AsyncSession = Depends(get_session)
):
    """
    Submit an assignment with either text/file or quiz answers.
    
    Args:
        submission_text: Text submission or answers JSON string
        answers: Dict of {question_id: answer} for quiz submissions
    """
    submission_text = body.submission_text
    answers = body.answers
    student = await get_student_record(current_user, session)
    
    # Get assignment
    assignment_result = await session.execute(
        select(Assignment).where(Assignment.id == assignment_id)
    )
    assignment = assignment_result.scalar_one_or_none()
    
    if not assignment:
        raise HTTPException(status_code=404, detail="Assignment not found")

    # Verify student has access to this assignment
    if assignment.class_id != student.class_id or assignment.school_id != student.school_id:
        raise HTTPException(status_code=403, detail="You do not have access to this assignment")

    if assignment.status != AssignmentStatus.PUBLISHED:
        raise HTTPException(status_code=400, detail="This assignment is not yet published")

    # Check if assignment is already graded - prevent resubmission
    existing_submission_result = await session.execute(
        select(Submission).where(
            Submission.assignment_id == assignment_id,
            Submission.student_id == student.id
        )
    )
    existing_submission = existing_submission_result.scalar_one_or_none()
    
    if existing_submission and existing_submission.status == SubmissionStatus.GRADED:
        raise HTTPException(
            status_code=400, 
            detail="This assignment has already been graded and cannot be submitted again"
        )
    
    # Prepare submission data
    answers_json = None
    auto_grade_data = None
    
    # If answers provided, store as JSON and attempt auto-grading
    if answers:
        answers_json = json.dumps(answers)
        
        # Get assignment questions for grading
        questions_result = await session.execute(
            select(AssignmentQuestion).where(
                AssignmentQuestion.assignment_id == assignment_id
            )
        )
        questions = questions_result.scalars().all()
        
        # Auto-grade if configured
        if assignment.rubric:
            try:
                rubric_settings = json.loads(assignment.rubric)
                if rubric_settings.get("allow_auto_grade", False):
                    auto_grader = AutoGrader()
                    auto_grade_data = await auto_grader.auto_grade_submission(
                        answers_json,
                        questions,
                        float(assignment.points_possible or 100)
                    )
            except Exception as e:
                logger.warning(f"Auto-grading failed for assignment {assignment_id}, student {student.id}: {e}")
                auto_grade_data = None  # Continue without auto-grading if it fails

    now = datetime.utcnow()
    submission_status = resolve_submission_status(assignment.due_date, now)

    # Get or create submission
    submission_result = await session.execute(
        select(Submission).where(
            Submission.assignment_id == assignment_id,
            Submission.student_id == student.id
        )
    )
    submission = submission_result.scalar_one_or_none()

    if not submission:
        # Create new submission
        submission = Submission(
            id=str(uuid.uuid4()),
            school_id=student.school_id,
            assignment_id=assignment_id,
            student_id=student.id,
            class_id=student.class_id,
            subject_id=assignment.subject_id,
            status=submission_status,
            submission_text=answers_json or submission_text,
            submission_date=now,
            max_score=assignment.points_possible
        )
        session.add(submission)
    else:
        # Update existing submission
        submission.submission_text = answers_json or submission_text
        submission.status = submission_status
        submission.submission_date = now
    
    # If auto-graded, set the score
    if auto_grade_data and auto_grade_data.get("can_full_auto_grade"):
        submission.score = auto_grade_data["total_score"]
        submission.status = SubmissionStatus.GRADED
        submission.graded_date = datetime.utcnow()
        # Store grading details in rubric_scores for reference
        submission.rubric_scores = json.dumps({
            "auto_graded": True,
            "question_scores": auto_grade_data["question_scores"],
            "feedback": auto_grade_data["feedback"]
        })
        await sync_submission_to_grade(session, submission, assignment, recorded_by="system:auto-grade")

    await session.commit()
    await session.refresh(submission)
    
    response = {
        "message": "Assignment submitted successfully",
        "submission_id": submission.id,
        "status": submission.status,
        "submitted_at": submission.submission_date.isoformat() if submission.submission_date else None
    }
    
    # Include auto-grading results if available
    if auto_grade_data:
        response["auto_graded"] = True
        response["grading"] = {
            "score": auto_grade_data["total_score"],
            "max_score": auto_grade_data["max_score"],
            "percentage": auto_grade_data["percentage"],
            "feedback": auto_grade_data["feedback"],
            "can_full_auto_grade": auto_grade_data["can_full_auto_grade"]
        }
    
    return response

# ============================================================================
# OPTIONAL MODULES - ENROLLMENT STATUS ENDPOINTS
# ============================================================================

@router.get("/enrollment-status", response_model=dict)
async def get_enrollment_status(
    current_user: User = Depends(require_roles(UserRole.STUDENT)),
    session: AsyncSession = Depends(get_session)
):
    """Check which optional modules student is enrolled in"""
    student = await get_student_record(current_user, session)
    
    # Check hostel enrollment
    hostel_result = await session.execute(
        select(StudentHostel).where(
            StudentHostel.student_id == student.id,
            StudentHostel.school_id == student.school_id,
            StudentHostel.status == "active"
        )
    )
    has_hostel = hostel_result.scalar_one_or_none() is not None
    
    # Check transport enrollment
    transport_result = await session.execute(
        select(StudentTransport).where(
            StudentTransport.student_id == student.id,
            StudentTransport.school_id == student.school_id,
            StudentTransport.is_active == True
        )
    )
    has_transport = transport_result.scalars().first() is not None
    
    return {
        "has_hostel": has_hostel,
        "has_transport": has_transport,
        "enabled_modules": {
            "hostel": has_hostel,
            "transport": has_transport,
            "fees": True,
            "grades": True,
            "attendance": True,
            "assignments": True,
            "timetable": True
        }
    }


@router.get("/hostel/status", response_model=dict)
async def get_hostel_status(
    current_user: User = Depends(require_roles(UserRole.STUDENT)),
    session: AsyncSession = Depends(get_session)
):
    """Get student's hostel info if enrolled"""
    student = await get_student_record(current_user, session)
    
    hostel_result = await session.execute(
        select(StudentHostel).where(
            StudentHostel.student_id == student.id,
            StudentHostel.school_id == student.school_id,
            StudentHostel.status == "active"
        )
    )
    hostel = hostel_result.scalar_one_or_none()
    
    if not hostel:
        raise HTTPException(status_code=404, detail="Not enrolled in hostel")
    
    # Get recent fees for this hostel
    fees_result = await session.execute(
        select(HostelFee).where(
            HostelFee.student_id == student.id,
            HostelFee.school_id == student.school_id
        ).order_by(HostelFee.created_at.desc()).limit(10)
    )
    fees = fees_result.scalars().all()
    
    return {
        "enrolled": True,
        "hostel_id": hostel.hostel_id,
        "assigned_date": hostel.check_in_date,
        "room_number": hostel.room_id or "TBA",
        "academic_year": hostel.academic_year,
        "parent_contact": hostel.parent_contact,
        "emergency_contact": hostel.emergency_contact,
        "emergency_contact_phone": hostel.emergency_contact_phone,
        "recent_fees": [
            {
                "id": f.id,
                "fee_type": f.fee_type,
                "amount_due": f.amount_due,
                "amount_paid": f.amount_paid,
                "balance": f.amount_due - f.amount_paid - f.discount,
                "is_paid": f.is_paid,
                "due_date": f.due_date,
                "payment_date": f.payment_date,
                "payment_method": f.payment_method
            }
            for f in fees
        ]
    }


# ============================================================================
# COURSE MODULES - Lightweight LMS (read-only for students + completion toggle)
# ============================================================================

@router.get("/course-modules", response_model=List[dict])
async def list_my_course_modules(
    current_user: User = Depends(require_roles(UserRole.STUDENT)),
    session: AsyncSession = Depends(get_session)
):
    """Published course modules for the student's current class, with each
    item's completion status. Quiz/assessment items (assignment_id set)
    derive "completed" from the student's Submission — self-reporting a
    quiz as watched/done doesn't make sense — and carry the assignment's
    title/due date/the student's score if graded. Video/material items
    keep the manual StudentModuleProgress checkmark."""
    student = await get_student_record(current_user, session)

    if not student.class_id:
        return []

    modules_result = await session.execute(
        select(CourseModule).where(
            CourseModule.school_id == student.school_id,
            CourseModule.class_id == student.class_id,
            CourseModule.is_published == True,
        ).order_by(CourseModule.order_index, CourseModule.created_at)
    )
    modules = modules_result.scalars().all()
    module_ids = [m.id for m in modules]
    if not module_ids:
        return []

    items_result = await session.execute(
        select(CourseModuleItem)
        .where(CourseModuleItem.module_id.in_(module_ids))
        .order_by(CourseModuleItem.order_index, CourseModuleItem.created_at)
    )
    items = items_result.scalars().all()
    item_ids = [i.id for i in items]

    completed_item_ids = set()
    progress_item_ids = [i.id for i in items if not i.assignment_id]
    if progress_item_ids:
        progress_result = await session.execute(
            select(StudentModuleProgress.module_item_id).where(
                StudentModuleProgress.student_id == student.id,
                StudentModuleProgress.module_item_id.in_(progress_item_ids),
            )
        )
        completed_item_ids = {row[0] for row in progress_result.all()}

    assignment_ids = [i.assignment_id for i in items if i.assignment_id]
    assignments_by_id = {}
    submissions_by_assignment = {}
    if assignment_ids:
        assignments_result = await session.execute(
            select(Assignment).where(Assignment.id.in_(assignment_ids))
        )
        assignments_by_id = {a.id: a for a in assignments_result.scalars().all()}

        submissions_result = await session.execute(
            select(Submission).where(
                Submission.student_id == student.id,
                Submission.assignment_id.in_(assignment_ids),
            )
        )
        submissions_by_assignment = {s.assignment_id: s for s in submissions_result.scalars().all()}

    items_by_module = {}
    for item in items:
        entry = {**jsonable_encoder(item)}
        if item.assignment_id:
            assignment = assignments_by_id.get(item.assignment_id)
            submission = submissions_by_assignment.get(item.assignment_id)
            entry["completed"] = submission is not None and submission.status != SubmissionStatus.NOT_SUBMITTED
            entry["assignment"] = {
                "id": assignment.id,
                "title": assignment.title,
                "due_date": assignment.due_date.isoformat() if assignment and assignment.due_date else None,
                "points_possible": assignment.points_possible if assignment else None,
            } if assignment else None
            entry["submission_status"] = submission.status if submission else "not_submitted"
            entry["score"] = submission.score if submission else None
        else:
            entry["completed"] = item.id in completed_item_ids
        items_by_module.setdefault(item.module_id, []).append(entry)

    return [
        {**jsonable_encoder(m), "items": items_by_module.get(m.id, [])}
        for m in modules
    ]


@router.post("/course-modules/items/{item_id}/complete", response_model=dict)
async def complete_module_item(
    item_id: str,
    current_user: User = Depends(require_roles(UserRole.STUDENT)),
    session: AsyncSession = Depends(get_session)
):
    """Mark a module item complete (idempotent)."""
    student = await get_student_record(current_user, session)

    item_result = await session.execute(
        select(CourseModuleItem).where(CourseModuleItem.id == item_id, CourseModuleItem.school_id == student.school_id)
    )
    item = item_result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Module item not found")
    if item.assignment_id:
        raise HTTPException(status_code=400, detail="This item is a quiz/assignment — completion is determined by your submission, not marked manually")

    existing_result = await session.execute(
        select(StudentModuleProgress).where(
            StudentModuleProgress.student_id == student.id,
            StudentModuleProgress.module_item_id == item_id,
        )
    )
    if existing_result.scalar_one_or_none():
        return {"success": True, "completed": True}

    session.add(StudentModuleProgress(school_id=student.school_id, student_id=student.id, module_item_id=item_id))
    await session.commit()
    return {"success": True, "completed": True}


@router.delete("/course-modules/items/{item_id}/complete", response_model=dict)
async def uncomplete_module_item(
    item_id: str,
    current_user: User = Depends(require_roles(UserRole.STUDENT)),
    session: AsyncSession = Depends(get_session)
):
    """Unmark a module item as complete."""
    student = await get_student_record(current_user, session)

    existing_result = await session.execute(
        select(StudentModuleProgress).where(
            StudentModuleProgress.student_id == student.id,
            StudentModuleProgress.module_item_id == item_id,
        )
    )
    progress = existing_result.scalar_one_or_none()
    if progress:
        await session.delete(progress)
        await session.commit()
    return {"success": True, "completed": False}


@router.get("/transport/status", response_model=dict)
async def get_transport_status(
    current_user: User = Depends(require_roles(UserRole.STUDENT)),
    session: AsyncSession = Depends(get_session)
):
    """Get student's transport info if enrolled"""
    student = await get_student_record(current_user, session)
    
    transport_result = await session.execute(
        select(StudentTransport)
        .where(
            StudentTransport.student_id == student.id,
            StudentTransport.school_id == student.school_id,
            StudentTransport.is_active == True
        )
        .order_by(StudentTransport.updated_at.desc(), StudentTransport.created_at.desc())
        .limit(1)
    )
    transport = transport_result.scalars().first()
    
    if not transport:
        raise HTTPException(status_code=404, detail="Not enrolled in transport")
    
    # Get route details
    route_result = await session.execute(
        select(Route).where(Route.id == transport.route_id)
    )
    route = route_result.scalar_one_or_none()
    
    # Get fees for this route
    fees_result = await session.execute(
        select(TransportFee).where(
            TransportFee.student_id == student.id,
            TransportFee.school_id == student.school_id
        ).order_by(TransportFee.created_at.desc()).limit(10)
    )
    fees = fees_result.scalars().all()
    
    return {
        "enrolled": True,
        "route_id": transport.route_id,
        "route_name": route.route_name if route else "Unknown",
        "pickup_point": transport.pickup_point or "N/A",
        "dropoff_point": transport.dropoff_point or "N/A",
        "enrollment_date": transport.enrollment_date,
        "emergency_contact": transport.emergency_contact,
        "emergency_contact_phone": transport.emergency_contact_phone,
        "recent_fees": [
            {
                "id": f.id,
                "fee_type": f.fee_type,
                "amount_due": f.amount_due,
                "amount_paid": f.amount_paid,
                "balance": f.amount_due - f.amount_paid - f.discount,
                "is_paid": f.is_paid,
                "due_date": f.due_date,
                "payment_date": f.payment_date,
                "payment_method": f.payment_method
            }
            for f in fees
        ]
    }