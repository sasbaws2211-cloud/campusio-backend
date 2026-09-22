import os
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
import json
import uuid
from sqlalchemy import select, and_, func
from typing import Optional, List
from datetime import datetime, timedelta

from database import get_session
from auth import get_current_user, require_roles
from models.user import User, UserRole
from models.assignment import AssignmentQuestion
from models.extra_class import (
    ExtraClass, ExtraClassCreate, ExtraClassUpdate, ExtraClassEnrollment, ExtraClassSession,
    ExtraClassAssignment, ExtraClassSubmission, ExtraClassGrade, ExtraClassBillingCycle,
    ExtraClassPayment, ExtraClassReminderLog, ExtraClassStatus,
    EnrollmentStatus, BillingInterval, SubmissionStatus
)
from models.student import Student, StudentParent, Parent
from models.staff import Staff, PayoutVerificationStatus
from models.classroom import Subject, Class
from models.communication import SMSNotification
from models.payment import OnlineTransaction, TransactionStatus, TransactionType
from services.sms_service import sms_service
from services.question_formatting import format_assignment_questions
from services.submission_lifecycle import resolve_submission_status
from services.online_payment_service import OnlinePaymentService
from services.extra_class_service import (
    check_capacity,
    ensure_current_billing_cycle,
)

ADMIN_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)


def normalize_status(status):
    return status.value if hasattr(status, "value") else str(status)


async def build_extra_class_progress(extra_class_id: str, student_id: str, session: AsyncSession) -> dict:
    assignments_result = await session.execute(
        select(ExtraClassAssignment).where(ExtraClassAssignment.extra_class_id == extra_class_id).order_by(ExtraClassAssignment.due_date)
    )
    assignments = assignments_result.scalars().all()
    assignment_ids = [assignment.id for assignment in assignments]

    submissions = {}
    if assignment_ids:
        submissions_result = await session.execute(
            select(ExtraClassSubmission).where(
                ExtraClassSubmission.student_id == student_id,
                ExtraClassSubmission.assignment_id.in_(assignment_ids)
            )
        )
        for submission in submissions_result.scalars().all():
            submissions[submission.assignment_id] = submission

    grades = {}
    if assignment_ids:
        grades_result = await session.execute(
            select(ExtraClassGrade).where(
                ExtraClassGrade.student_id == student_id,
                ExtraClassGrade.assignment_id.in_(assignment_ids)
            )
        )
        for grade in grades_result.scalars().all():
            grades[grade.assignment_id] = grade

    assignment_rows = []
    summary = {
        "total_assignments": len(assignments),
        "submitted": 0,
        "graded": 0,
        "late": 0,
        "overdue": 0,
        "pending": 0,
        "average_score": 0.0,
    }
    total_score_pct = 0.0
    scored_count = 0
    now = datetime.utcnow()

    for assignment in assignments:
        submission = submissions.get(assignment.id)
        grade = grades.get(assignment.id)
        status = "pending"
        if submission:
            if submission.status == SubmissionStatus.GRADED or grade:
                status = "graded"
            elif submission.status == SubmissionStatus.LATE:
                status = "late"
            elif submission.status == SubmissionStatus.SUBMITTED:
                status = "submitted"
            else:
                status = submission.status.value if hasattr(submission.status, 'value') else str(submission.status)
        elif assignment.due_date and now > assignment.due_date:
            status = "overdue"

        if status == "graded":
            summary["graded"] += 1
        elif status == "submitted":
            summary["submitted"] += 1
        elif status == "late":
            summary["late"] += 1
        elif status == "overdue":
            summary["overdue"] += 1
        else:
            summary["pending"] += 1

        # Averaged as each assignment's own percentage (score/max_score),
        # not a raw mean of Grade.score — assignments can have different
        # max_score values, so summing raw points would silently mix, say,
        # a 20-point quiz and a 100-point exam into a meaningless number
        # (the same bug already fixed in services/assignment_performance.py).
        # Only counts assignments with an actual recorded score — a
        # submission marked "graded" with no ExtraClassGrade yet shouldn't
        # drag the average toward 0.
        percentage = round((grade.score / assignment.max_score) * 100, 1) if grade and grade.score is not None and assignment.max_score else None
        if percentage is not None:
            total_score_pct += percentage
            scored_count += 1

        assignment_rows.append({
            "id": assignment.id,
            "title": assignment.title,
            "description": assignment.description,
            "instructions": assignment.instructions,
            "due_date": assignment.due_date.isoformat() if assignment.due_date else None,
            "max_score": assignment.max_score,
            "status": status,
            "submitted_at": submission.submitted_at.isoformat() if submission and submission.submitted_at else None,
            "graded": status == "graded",
            "score": grade.score if grade else None,
            "feedback": grade.feedback if grade else None,
            "percentage": percentage,
        })

    if scored_count > 0:
        summary["average_score"] = round(total_score_pct / scored_count, 1)

    sessions_result = await session.execute(
        select(ExtraClassSession).where(ExtraClassSession.extra_class_id == extra_class_id).order_by(ExtraClassSession.session_date)
    )
    sessions = sessions_result.scalars().all()
    session_count = len(sessions)
    attendance_total = sum((s.attendance_count or 0) for s in sessions)
    average_attendance = round(attendance_total / session_count, 1) if session_count else 0.0
    last_session_date = None
    next_session_date = None
    future_sessions = [s for s in sessions if s.session_date and s.session_date > now]
    past_sessions = [s for s in sessions if s.session_date and s.session_date <= now]
    if past_sessions:
        last_session_date = max(past_sessions, key=lambda s: s.session_date).session_date.isoformat()
    if future_sessions:
        next_session_date = min(future_sessions, key=lambda s: s.session_date).session_date.isoformat()

    attendance_summary = {
        "sessions_held": session_count,
        "attendance_total": attendance_total,
        "average_attendance": average_attendance,
        "last_session_date": last_session_date,
        "next_session_date": next_session_date,
        "sessions": [
            {
                "id": s.id,
                "session_date": s.session_date.isoformat(),
                "topic": s.topic,
                "attendance_count": s.attendance_count,
                "notes": s.notes,
            }
            for s in sessions
        ]
    }

    return {
        "assignments": assignment_rows,
        "summary": summary,
        "attendance": attendance_summary,
    }

async def build_enrollment_payload(
    enrollment: ExtraClassEnrollment,
    extra_class: ExtraClass,
    billing_cycle: ExtraClassBillingCycle | None = None,
    session: AsyncSession | None = None
) -> dict:
    extra_class_payload = serialize_extra_class(extra_class)
    teacher_name = None
    if session is not None and extra_class.teacher_id:
        staff_result = await session.execute(select(Staff).where(Staff.id == extra_class.teacher_id))
        staff = staff_result.scalar_one_or_none()
        if not staff:
            staff_result = await session.execute(select(Staff).where(Staff.user_id == extra_class.teacher_id))
            staff = staff_result.scalar_one_or_none()
        if staff:
            teacher_name = f"{staff.first_name} {staff.last_name}"
        else:
            user_result = await session.execute(select(User).where(User.id == extra_class.teacher_id))
            user = user_result.scalar_one_or_none()
            if user:
                teacher_name = f"{user.first_name} {user.last_name}"
    if teacher_name:
        extra_class_payload["teacher_name"] = teacher_name

    payload = {
        "id": enrollment.id,
        "student_id": enrollment.student_id,
        "status": normalize_status(enrollment.status),
        "extra_class": extra_class_payload,
        "teacher_name": teacher_name or extra_class_payload.get("teacher_name"),
    }
    # A pending request past capacity is effectively waitlisted — it'll only
    # move to approved once someone else's slot frees up (see
    # withdraw_enrollment's auto-promotion). Flag it so the parent isn't left
    # thinking "pending" just means "the teacher hasn't looked yet".
    if enrollment.status == EnrollmentStatus.PENDING and session is not None:
        current_count, max_allowed = await check_capacity(session, extra_class)
        payload["waitlisted"] = current_count >= max_allowed
    else:
        payload["waitlisted"] = False
    if billing_cycle:
        payload.update({
            "billing_cycle_id": billing_cycle.id,
            "billing_amount": billing_cycle.amount,
            "next_due_date": billing_cycle.next_due_date.strftime('%Y-%m-%d %H:%M:%S') if billing_cycle.next_due_date else None,
            "billing_status": billing_cycle.status,
        })
    if session is not None:
        payload["progress"] = await build_extra_class_progress(extra_class.id, enrollment.student_id, session)
    return payload

router = APIRouter(prefix="/extra-classes", tags=["extra-classes"])


@router.post("", response_model=dict)
async def create_extra_class(
    payload: ExtraClassCreate,
    current_user: User = Depends(require_roles(UserRole.TEACHER)),
    session: AsyncSession = Depends(get_session),
):
    school_id = current_user.school_id
    staff_result = await session.execute(select(Staff).where(Staff.user_id == current_user.id))
    staff = staff_result.scalar_one_or_none()
    if not staff:
        raise HTTPException(status_code=404, detail="Teacher staff profile not found")

    extra_class = ExtraClass(
        school_id=school_id,
        teacher_id=staff.id,
        subject_id=payload.subject_id,
        class_id=payload.class_id,
        title=payload.title,
        description=payload.description,
        pricing_type=payload.pricing_type,
        price=payload.price,
        billing_interval=payload.billing_interval,
        payout_frequency=payload.payout_frequency,
        session_duration_hours=payload.session_duration_hours,
        frequency_per_week=payload.frequency_per_week,
        max_students_per_session=payload.max_students_per_session,
        contact_phone=payload.contact_phone,
        contact_email=payload.contact_email,
        meeting_link=payload.meeting_link,
        schedule=payload.schedule,
        start_date=payload.start_date,
        end_date=payload.end_date,
        status=payload.status,
    )
    session.add(extra_class)
    await session.commit()
    await session.refresh(extra_class)
    return {"message": "Extra class created", "extra_class": {
        "id": extra_class.id,
        "title": extra_class.title,
        "status": normalize_status(extra_class.status),
    }}


@router.get("", response_model=dict)
async def list_extra_classes(
    subject_id: Optional[str] = None,
    class_id: Optional[str] = None,
    status: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    query = select(ExtraClass).where(ExtraClass.school_id == current_user.school_id)
    if subject_id:
        query = query.where(ExtraClass.subject_id == subject_id)
    if class_id:
        query = query.where(ExtraClass.class_id == class_id)
    if status:
        query = query.where(ExtraClass.status == status)
    elif current_user.role in (UserRole.PARENT, UserRole.STUDENT):
        # Parents/students browsing to register should only ever see classes
        # the teacher has actually published — draft classes a teacher is
        # still setting up, or closed/cancelled ones, stay hidden unless a
        # status is explicitly requested (teachers/admins reviewing their
        # own classes pass an explicit status or none via /teacher/me).
        query = query.where(ExtraClass.status == ExtraClassStatus.PUBLISHED)

    result = await session.execute(query.order_by(ExtraClass.created_at.desc()))
    classes = result.scalars().all()
    payloads = []
    for c in classes:
        payload = serialize_extra_class(c)
        enrolled_count, _ = await check_capacity(session, c)
        payload["enrolled_count"] = enrolled_count
        payloads.append(payload)
    return {"extra_classes": payloads}


@router.get("/teacher/me", response_model=dict)
async def list_teacher_extra_classes(
    current_user: User = Depends(require_roles(UserRole.TEACHER)),
    session: AsyncSession = Depends(get_session),
):
    school_id = current_user.school_id
    staff_result = await session.execute(select(Staff).where(Staff.user_id == current_user.id))
    staff = staff_result.scalar_one_or_none()
    if not staff:
        raise HTTPException(status_code=404, detail="Teacher staff profile not found")

    result = await session.execute(
        select(ExtraClass).where(
            and_(ExtraClass.school_id == school_id, ExtraClass.teacher_id == staff.id)
        ).order_by(ExtraClass.created_at.desc())
    )
    classes = result.scalars().all()
    payloads = []
    for c in classes:
        payload = serialize_extra_class(c)
        enrolled_count, _ = await check_capacity(session, c)
        payload["enrolled_count"] = enrolled_count
        payloads.append(payload)
    return {"extra_classes": payloads}


@router.get("/{extra_class_id}", response_model=dict)
async def get_extra_class(
    extra_class_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(select(ExtraClass).where(ExtraClass.id == extra_class_id))
    extra_class = result.scalar_one_or_none()
    if not extra_class:
        raise HTTPException(status_code=404, detail="Extra class not found")
    if extra_class.school_id != current_user.school_id:
        raise HTTPException(status_code=403, detail="Access denied")
    return {"extra_class": serialize_extra_class(extra_class)}


@router.put("/{extra_class_id}", response_model=dict)
async def update_extra_class(
    extra_class_id: str,
    payload: ExtraClassUpdate,
    current_user: User = Depends(require_roles(UserRole.TEACHER, *ADMIN_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(select(ExtraClass).where(ExtraClass.id == extra_class_id))
    extra_class = result.scalar_one_or_none()
    if not extra_class:
        raise HTTPException(status_code=404, detail="Extra class not found")
    if extra_class.school_id != current_user.school_id:
        raise HTTPException(status_code=403, detail="Access denied")

    if current_user.role == UserRole.TEACHER:
        staff_result = await session.execute(select(Staff).where(Staff.user_id == current_user.id))
        staff = staff_result.scalar_one_or_none()
        if not staff or extra_class.teacher_id != staff.id:
            raise HTTPException(status_code=403, detail="You can only update your own classes")

    updates = payload.model_dump(exclude_unset=True)

    # A teacher publishes their own class directly — no admin approval step.
    # The one hard gate is financial, not editorial: a class can't go live
    # before its teacher's payout details are verified, since publish is the
    # moment parents start being able to pay into it and payments settle via
    # the teacher's Paystack subaccount.
    if updates.get("status") == ExtraClassStatus.PUBLISHED:
        teacher_result = await session.execute(select(Staff).where(Staff.id == extra_class.teacher_id))
        teacher_staff = teacher_result.scalar_one_or_none()
        if not teacher_staff or teacher_staff.payout_verification_status != PayoutVerificationStatus.VERIFIED:
            raise HTTPException(
                status_code=400,
                detail="Your payout details haven't been verified yet — publish is blocked until a school admin verifies your bank/mobile money details.",
            )

    previous_status = extra_class.status
    for field, value in updates.items():
        setattr(extra_class, field, value)
    extra_class.updated_at = datetime.utcnow()

    # Cancelling a class shouldn't leave unpaid billing cycles sitting
    # around implying money is still owed for lessons that will never happen.
    if extra_class.status == ExtraClassStatus.CANCELLED and previous_status != ExtraClassStatus.CANCELLED:
        cycles_result = await session.execute(
            select(ExtraClassBillingCycle).where(
                ExtraClassBillingCycle.extra_class_id == extra_class_id,
                ExtraClassBillingCycle.status.in_(["pending", "overdue"]),
            )
        )
        for cycle in cycles_result.scalars().all():
            cycle.status = "cancelled"
            cycle.updated_at = datetime.utcnow()
            session.add(cycle)

    await session.commit()
    await session.refresh(extra_class)
    return {"message": "Extra class updated", "extra_class": serialize_extra_class(extra_class)}


@router.post("/{extra_class_id}/enroll", response_model=dict)
async def enroll_student(
    extra_class_id: str,
    payload: dict,
    current_user: User = Depends(require_roles(UserRole.PARENT)),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(select(ExtraClass).where(ExtraClass.id == extra_class_id))
    extra_class = result.scalar_one_or_none()
    if not extra_class:
        raise HTTPException(status_code=404, detail="Extra class not found")
    if extra_class.school_id != current_user.school_id:
        raise HTTPException(status_code=403, detail="Access denied")

    parent_result = await session.execute(select(Parent).where(Parent.user_id == current_user.id))
    parent = parent_result.scalar_one_or_none()
    if not parent:
        raise HTTPException(status_code=404, detail="Parent profile not found")

    link_result = await session.execute(select(StudentParent).where(StudentParent.parent_id == parent.id))
    linked_students = link_result.scalars().all()
    if not linked_students:
        raise HTTPException(status_code=400, detail="No linked students found for this parent")

    student_id = payload.get("student_id") if isinstance(payload, dict) else getattr(payload, "student_id", None)
    if not student_id and len(linked_students) == 1:
        student_id = linked_students[0].student_id

    if not student_id:
        raise HTTPException(status_code=400, detail="Please select a child to register")
    if student_id not in {ls.student_id for ls in linked_students}:
        raise HTTPException(status_code=403, detail="You can only register your own child")

    existing = await session.execute(
        select(ExtraClassEnrollment).where(
            and_(
                ExtraClassEnrollment.extra_class_id == extra_class_id,
                ExtraClassEnrollment.student_id == student_id,
            )
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="You already requested enrollment for this class")

    enrollment = ExtraClassEnrollment(
        school_id=current_user.school_id,
        extra_class_id=extra_class_id,
        student_id=student_id,
        parent_id=parent.id,
    )
    session.add(enrollment)
    await session.commit()
    await session.refresh(enrollment)
    return {"message": "Enrollment requested", "enrollment": {"id": enrollment.id, "status": normalize_status(enrollment.status)}}


@router.get("/{extra_class_id}/enrollments", response_model=dict)
async def list_enrollments(
    extra_class_id: str,
    current_user: User = Depends(require_roles(UserRole.TEACHER)),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(select(ExtraClass).where(ExtraClass.id == extra_class_id))
    extra_class = result.scalar_one_or_none()
    if not extra_class:
        raise HTTPException(status_code=404, detail="Extra class not found")

    staff_result = await session.execute(select(Staff).where(Staff.user_id == current_user.id))
    staff = staff_result.scalar_one_or_none()
    if not staff or extra_class.teacher_id != staff.id:
        raise HTTPException(status_code=403, detail="You can only view registrations for your own classes")

    enrollments_result = await session.execute(
        select(ExtraClassEnrollment).where(ExtraClassEnrollment.extra_class_id == extra_class_id)
    )
    enrollments = enrollments_result.scalars().all()
    return {"enrollments": [await serialize_enrollment(e, session) for e in enrollments]}


def _approve_enrollment_and_create_cycle(session: AsyncSession, extra_class: ExtraClass, enrollment: ExtraClassEnrollment) -> ExtraClassBillingCycle:
    """Shared by the teacher's manual approve action and waitlist auto-promotion
    on withdrawal — both need the exact same approved-state + first-billing-cycle
    setup, just triggered by a different event."""
    enrollment.status = EnrollmentStatus.APPROVED
    enrollment.approved_at = datetime.utcnow()
    enrollment.updated_at = datetime.utcnow()
    session.add(enrollment)

    billing_cycle = ExtraClassBillingCycle(
        school_id=extra_class.school_id,
        extra_class_id=extra_class.id,
        teacher_id=extra_class.teacher_id,
        enrollment_id=enrollment.id,
        parent_id=enrollment.parent_id,
        student_id=enrollment.student_id,
        amount=extra_class.price,
        interval=extra_class.billing_interval,
        next_due_date=datetime.utcnow() + timedelta(days=30 if extra_class.billing_interval == BillingInterval.MONTHLY else 7),
        status="pending",
    )
    session.add(billing_cycle)

    payment = ExtraClassPayment(
        school_id=extra_class.school_id,
        billing_cycle_id=billing_cycle.id,
        amount=extra_class.price,
        payment_method="pending",
        reference=None,
        status="pending",
    )
    session.add(payment)
    return billing_cycle


@router.post("/{extra_class_id}/enrollments/{enrollment_id}/approve", response_model=dict)
async def approve_enrollment(
    extra_class_id: str,
    enrollment_id: str,
    current_user: User = Depends(require_roles(UserRole.TEACHER)),
    session: AsyncSession = Depends(get_session),
):
    extra_class_result = await session.execute(select(ExtraClass).where(ExtraClass.id == extra_class_id))
    extra_class = extra_class_result.scalar_one_or_none()
    if not extra_class:
        raise HTTPException(status_code=404, detail="Extra class not found")

    staff_result = await session.execute(select(Staff).where(Staff.user_id == current_user.id))
    staff = staff_result.scalar_one_or_none()
    if not staff or extra_class.teacher_id != staff.id:
        raise HTTPException(status_code=403, detail="You can only manage your own classes")

    enrollment_result = await session.execute(select(ExtraClassEnrollment).where(ExtraClassEnrollment.id == enrollment_id))
    enrollment = enrollment_result.scalar_one_or_none()
    if not enrollment or enrollment.extra_class_id != extra_class_id:
        raise HTTPException(status_code=404, detail="Enrollment not found")

    current_count, max_allowed = await check_capacity(session, extra_class)
    if current_count >= max_allowed:
        raise HTTPException(
            status_code=400,
            detail=f"This class is full ({current_count}/{max_allowed} students) — reject or wait for a spot to free up before approving another",
        )

    billing_cycle = _approve_enrollment_and_create_cycle(session, extra_class, enrollment)

    await session.commit()
    await session.refresh(billing_cycle)
    return {"message": "Enrollment approved", "enrollment": {"id": enrollment.id, "status": normalize_status(enrollment.status)}, "billing_cycle": {"id": billing_cycle.id, "amount": billing_cycle.amount, "next_due_date": billing_cycle.next_due_date.isoformat() if billing_cycle.next_due_date else None}}


@router.post("/{extra_class_id}/enrollments/{enrollment_id}/reject", response_model=dict)
async def reject_enrollment(
    extra_class_id: str,
    enrollment_id: str,
    current_user: User = Depends(require_roles(UserRole.TEACHER)),
    session: AsyncSession = Depends(get_session),
):
    extra_class_result = await session.execute(select(ExtraClass).where(ExtraClass.id == extra_class_id))
    extra_class = extra_class_result.scalar_one_or_none()
    if not extra_class:
        raise HTTPException(status_code=404, detail="Extra class not found")

    staff_result = await session.execute(select(Staff).where(Staff.user_id == current_user.id))
    staff = staff_result.scalar_one_or_none()
    if not staff or extra_class.teacher_id != staff.id:
        raise HTTPException(status_code=403, detail="You can only manage your own classes")

    enrollment_result = await session.execute(select(ExtraClassEnrollment).where(ExtraClassEnrollment.id == enrollment_id))
    enrollment = enrollment_result.scalar_one_or_none()
    if not enrollment or enrollment.extra_class_id != extra_class_id:
        raise HTTPException(status_code=404, detail="Enrollment not found")

    enrollment.status = EnrollmentStatus.REJECTED
    enrollment.rejected_at = datetime.utcnow()
    enrollment.updated_at = datetime.utcnow()
    await session.commit()
    return {"message": "Enrollment rejected", "enrollment": {"id": enrollment.id, "status": normalize_status(enrollment.status)}}


@router.post("/{extra_class_id}/enrollments/{enrollment_id}/withdraw", response_model=dict)
async def withdraw_enrollment(
    extra_class_id: str,
    enrollment_id: str,
    current_user: User = Depends(require_roles(UserRole.PARENT)),
    session: AsyncSession = Depends(get_session),
):
    """A parent pulling their child out of a class. Before this endpoint
    existed, the only way to stop being billed was for the teacher to close
    the whole class for every enrolled family — there was no way for one
    parent to leave on their own. Withdrawing also frees a capacity slot, so
    the oldest still-pending request (the de facto waitlist) gets
    auto-promoted into it, same as if the teacher had approved it manually.
    """
    extra_class_result = await session.execute(select(ExtraClass).where(ExtraClass.id == extra_class_id))
    extra_class = extra_class_result.scalar_one_or_none()
    if not extra_class:
        raise HTTPException(status_code=404, detail="Extra class not found")

    parent_result = await session.execute(select(Parent).where(Parent.user_id == current_user.id))
    parent = parent_result.scalar_one_or_none()
    if not parent:
        raise HTTPException(status_code=404, detail="Parent profile not found")

    enrollment_result = await session.execute(select(ExtraClassEnrollment).where(ExtraClassEnrollment.id == enrollment_id))
    enrollment = enrollment_result.scalar_one_or_none()
    if not enrollment or enrollment.extra_class_id != extra_class_id or enrollment.parent_id != parent.id:
        raise HTTPException(status_code=404, detail="Enrollment not found")

    if enrollment.status not in (EnrollmentStatus.PENDING, EnrollmentStatus.APPROVED, EnrollmentStatus.ACTIVE):
        raise HTTPException(status_code=400, detail=f"Nothing to withdraw — status is already {normalize_status(enrollment.status)}")

    was_occupying_a_slot = enrollment.status in (EnrollmentStatus.APPROVED, EnrollmentStatus.ACTIVE)

    enrollment.status = EnrollmentStatus.WITHDRAWN
    enrollment.withdrawn_at = datetime.utcnow()
    enrollment.updated_at = datetime.utcnow()
    session.add(enrollment)

    # Stop future billing — any cycle that hasn't been paid yet for this
    # enrollment shouldn't still be owed once the child has left the class.
    cycles_result = await session.execute(
        select(ExtraClassBillingCycle).where(
            ExtraClassBillingCycle.enrollment_id == enrollment.id,
            ExtraClassBillingCycle.status.in_(["pending", "overdue"]),
        )
    )
    for cycle in cycles_result.scalars().all():
        cycle.status = "cancelled"
        cycle.updated_at = datetime.utcnow()
        session.add(cycle)

    promoted_student_id = None
    if was_occupying_a_slot:
        current_count, max_allowed = await check_capacity(session, extra_class)
        if current_count < max_allowed:
            waitlist_result = await session.execute(
                select(ExtraClassEnrollment)
                .where(
                    ExtraClassEnrollment.extra_class_id == extra_class_id,
                    ExtraClassEnrollment.status == EnrollmentStatus.PENDING,
                )
                .order_by(ExtraClassEnrollment.created_at.asc())
            )
            next_in_line = waitlist_result.scalars().first()
            if next_in_line:
                _approve_enrollment_and_create_cycle(session, extra_class, next_in_line)
                promoted_student_id = next_in_line.student_id

    await session.commit()
    return {
        "message": "Enrollment withdrawn",
        "enrollment": {"id": enrollment.id, "status": normalize_status(enrollment.status)},
        "promoted_student_id": promoted_student_id,
    }


@router.get("/student/me", response_model=dict)
async def list_student_enrollments(
    current_user: User = Depends(require_roles(UserRole.STUDENT)),
    session: AsyncSession = Depends(get_session),
):
    student_result = await session.execute(select(Student).where(Student.user_id == current_user.id))
    student = student_result.scalar_one_or_none()
    if not student:
        raise HTTPException(status_code=404, detail="Student profile not found")

    enrollments_result = await session.execute(
        select(ExtraClassEnrollment).where(ExtraClassEnrollment.student_id == student.id)
    )
    enrollments = enrollments_result.scalars().all()
    items = []
    for enrollment in enrollments:
        extra_class_result = await session.execute(select(ExtraClass).where(ExtraClass.id == enrollment.extra_class_id))
        extra_class = extra_class_result.scalar_one_or_none()
        if extra_class:
            await ensure_current_billing_cycle(session, enrollment, extra_class)
            billing_cycle_result = await session.execute(
                select(ExtraClassBillingCycle)
                .where(ExtraClassBillingCycle.enrollment_id == enrollment.id)
                .order_by(ExtraClassBillingCycle.created_at.desc())
            )
            billing_cycle = billing_cycle_result.scalars().first()
            items.append(await build_enrollment_payload(enrollment, extra_class, billing_cycle, session))
    await session.commit()
    return {"enrollments": items}


@router.get("/parent/me", response_model=dict)
async def list_parent_enrollments(
    current_user: User = Depends(require_roles(UserRole.PARENT)),
    session: AsyncSession = Depends(get_session),
):
    parent_result = await session.execute(select(Parent).where(Parent.user_id == current_user.id))
    parent = parent_result.scalar_one_or_none()
    if not parent:
        raise HTTPException(status_code=404, detail="Parent profile not found")

    enrollments_result = await session.execute(
        select(ExtraClassEnrollment).where(ExtraClassEnrollment.parent_id == parent.id)
    )
    enrollments = enrollments_result.scalars().all()
    items = []
    for enrollment in enrollments:
        extra_class_result = await session.execute(select(ExtraClass).where(ExtraClass.id == enrollment.extra_class_id))
        extra_class = extra_class_result.scalar_one_or_none()
        if extra_class:
            await ensure_current_billing_cycle(session, enrollment, extra_class)
            billing_cycle_result = await session.execute(
                select(ExtraClassBillingCycle)
                .where(ExtraClassBillingCycle.enrollment_id == enrollment.id)
                .order_by(ExtraClassBillingCycle.created_at.desc())
            )
            billing_cycle = billing_cycle_result.scalars().first()
            items.append(await build_enrollment_payload(enrollment, extra_class, billing_cycle, session))
    await session.commit()
    return {"enrollments": items}


@router.post("/{extra_class_id}/sessions", response_model=dict)
async def create_extra_class_session(
    extra_class_id: str,
    payload: dict,
    current_user: User = Depends(require_roles(UserRole.TEACHER)),
    session: AsyncSession = Depends(get_session),
):
    extra_class_result = await session.execute(select(ExtraClass).where(ExtraClass.id == extra_class_id))
    extra_class = extra_class_result.scalar_one_or_none()
    if not extra_class:
        raise HTTPException(status_code=404, detail="Extra class not found")

    staff_result = await session.execute(select(Staff).where(Staff.user_id == current_user.id))
    staff = staff_result.scalar_one_or_none()
    if not staff or extra_class.teacher_id != staff.id:
        raise HTTPException(status_code=403, detail="You can only create sessions for your own classes")

    session_record = ExtraClassSession(
        school_id=current_user.school_id,
        extra_class_id=extra_class_id,
        session_date=datetime.fromisoformat(payload["session_date"]),
        topic=payload.get("topic"),
        notes=payload.get("notes"),
        attendance_count=payload.get("attendance_count", 0),
    )
    session.add(session_record)
    await session.commit()
    await session.refresh(session_record)
    return {"message": "Session created", "session": {"id": session_record.id, "topic": session_record.topic}}


@router.get("/{extra_class_id}/sessions", response_model=dict)
async def list_extra_class_sessions(
    extra_class_id: str,
    current_user: User = Depends(require_roles(UserRole.TEACHER, *ADMIN_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    extra_class_result = await session.execute(select(ExtraClass).where(ExtraClass.id == extra_class_id))
    extra_class = extra_class_result.scalar_one_or_none()
    if not extra_class:
        raise HTTPException(status_code=404, detail="Extra class not found")
    if extra_class.school_id != current_user.school_id:
        raise HTTPException(status_code=403, detail="Access denied")

    if current_user.role == UserRole.TEACHER:
        staff_result = await session.execute(select(Staff).where(Staff.user_id == current_user.id))
        staff = staff_result.scalar_one_or_none()
        if not staff or extra_class.teacher_id != staff.id:
            raise HTTPException(status_code=403, detail="You can only view sessions for your own classes")

    sessions_result = await session.execute(
        select(ExtraClassSession)
        .where(ExtraClassSession.extra_class_id == extra_class_id)
        .order_by(ExtraClassSession.session_date.desc())
    )
    sessions = sessions_result.scalars().all()
    return {
        "sessions": [
            {
                "id": s.id,
                "session_date": s.session_date.isoformat() if s.session_date else None,
                "topic": s.topic,
                "notes": s.notes,
                "attendance_count": s.attendance_count,
            }
            for s in sessions
        ]
    }


@router.post("/{extra_class_id}/billing-cycles/{billing_cycle_id}/pay", response_model=dict)
async def pay_billing_cycle(
    extra_class_id: str,
    billing_cycle_id: str,
    payload: dict,
    current_user: User = Depends(require_roles(UserRole.PARENT)),
    session: AsyncSession = Depends(get_session),
):
    """Initiates a real Paystack payment for a billing cycle.

    This used to just trust whatever amount/reference the client sent and
    mark the cycle paid on the spot — a parent (or anyone scripting a
    request) could mark their own bill "paid" with no money moving, and
    that fabricated "paid" cycle would then count toward what the teacher
    could request as a payout. Now it follows the same
    initiate-then-webhook-confirms pattern as canteen top-ups: this only
    returns a Paystack checkout link, and the cycle is only marked paid by
    apply_billing_payment() once the webhook verifies a real charge.
    """
    parent_result = await session.execute(select(Parent).where(Parent.user_id == current_user.id))
    parent = parent_result.scalar_one_or_none()
    if not parent:
        raise HTTPException(status_code=404, detail="Parent profile not found")

    billing_cycle_result = await session.execute(select(ExtraClassBillingCycle).where(ExtraClassBillingCycle.id == billing_cycle_id))
    billing_cycle = billing_cycle_result.scalar_one_or_none()
    if not billing_cycle or billing_cycle.extra_class_id != extra_class_id:
        raise HTTPException(status_code=404, detail="Billing cycle not found")
    if billing_cycle.parent_id != parent.id:
        raise HTTPException(status_code=403, detail="Access denied")
    if billing_cycle.status == "paid":
        raise HTTPException(status_code=400, detail="This billing cycle is already paid")

    # The class can't have been published without this already being true
    # (see update_extra_class), but a class can theoretically sit published
    # for a long time — re-check here too, defense in depth, rather than
    # trusting a state that was only true at publish time.
    teacher_result = await session.execute(select(Staff).where(Staff.id == billing_cycle.teacher_id))
    teacher_staff = teacher_result.scalar_one_or_none()
    if not teacher_staff or teacher_staff.payout_verification_status != PayoutVerificationStatus.VERIFIED or not teacher_staff.paystack_subaccount_code:
        raise HTTPException(status_code=503, detail="This class's teacher payout isn't verified — payment is temporarily unavailable")

    paystack_secret_key = os.getenv("PAYSTACK_SECRET_KEY", "")
    if not paystack_secret_key:
        raise HTTPException(status_code=503, detail="Payment gateway not configured")

    payment_service = OnlinePaymentService(paystack_secret_key)
    transaction_id = f"TXN-{uuid.uuid4().hex[:12].upper()}"

    transaction = OnlineTransaction(
        school_id=current_user.school_id,
        fee_id=billing_cycle.extra_class_id,
        student_id=billing_cycle.student_id,
        parent_id=current_user.id,
        billing_cycle_id=billing_cycle.id,
        amount=billing_cycle.amount,
        gateway="paystack",
        reference=transaction_id,
        transaction_type=TransactionType.EXTRA_CLASS_FEE,
        status=TransactionStatus.PENDING,
    )
    session.add(transaction)
    await session.flush()

    paystack_result = await payment_service.paystack.initialize_payment(
        amount_kobo=int(billing_cycle.amount * 100),
        email=current_user.email,
        reference=transaction_id,
        metadata={"billing_cycle_id": billing_cycle.id, "extra_class_id": extra_class_id},
        subaccount=teacher_staff.paystack_subaccount_code,
    )

    if not paystack_result.get("success"):
        transaction.status = TransactionStatus.FAILED
        transaction.failed_reason = paystack_result.get("error", "Payment initialization failed")
        session.add(transaction)
        await session.commit()
        raise HTTPException(status_code=500, detail="Payment initialization failed")

    transaction.payment_url = paystack_result["authorization_url"]
    transaction.access_code = paystack_result["access_code"]
    transaction.reference = paystack_result["reference"]
    transaction.status = TransactionStatus.PROCESSING
    session.add(transaction)
    await session.commit()

    return {
        "success": True,
        "transaction_id": str(transaction.id),
        "payment_url": paystack_result["authorization_url"],
        "reference": paystack_result["reference"],
        "amount": billing_cycle.amount,
        "message": "Billing cycle payment initialized",
    }


@router.get("/{extra_class_id}/earnings", response_model=dict)
async def get_class_earnings(
    extra_class_id: str,
    current_user: User = Depends(require_roles(UserRole.TEACHER)),
    session: AsyncSession = Depends(get_session),
):
    """Read-only earnings summary for a class.

    There's no payout request/approval step anymore — each paid billing
    cycle already settled straight to the teacher's own bank/MoMo account
    via their Paystack subaccount at payment time. This is just a record of
    what's been collected, for the teacher's own reconciliation.
    """
    staff_result = await session.execute(select(Staff).where(Staff.user_id == current_user.id))
    staff = staff_result.scalar_one_or_none()
    if not staff:
        raise HTTPException(status_code=404, detail="Teacher staff profile not found")

    result = await session.execute(
        select(ExtraClassBillingCycle).where(
            ExtraClassBillingCycle.extra_class_id == extra_class_id,
            ExtraClassBillingCycle.teacher_id == staff.id,
            ExtraClassBillingCycle.status == "paid",
        )
    )
    cycles = result.scalars().all()
    total = round(sum(c.amount for c in cycles), 2)
    return {
        "total_earned": total,
        "cycle_count": len(cycles),
        "payout_verification_status": staff.payout_verification_status.value if hasattr(staff.payout_verification_status, "value") else str(staff.payout_verification_status),
        "cycles": [
            {"id": c.id, "amount": c.amount, "paid_at": c.paid_at.isoformat() if c.paid_at else None}
            for c in sorted(cycles, key=lambda c: c.paid_at or datetime.min, reverse=True)
        ],
    }


@router.post("/{extra_class_id}/assignments", response_model=dict)
async def create_extra_class_assignment(
    extra_class_id: str,
    payload: dict,
    current_user: User = Depends(require_roles(UserRole.TEACHER)),
    session: AsyncSession = Depends(get_session),
):
    extra_class_result = await session.execute(select(ExtraClass).where(ExtraClass.id == extra_class_id))
    extra_class = extra_class_result.scalar_one_or_none()
    if not extra_class:
        raise HTTPException(status_code=404, detail="Extra class not found")

    staff_result = await session.execute(select(Staff).where(Staff.user_id == current_user.id))
    staff = staff_result.scalar_one_or_none()
    if not staff or extra_class.teacher_id != staff.id:
        raise HTTPException(status_code=403, detail="You can only create assignments for your own classes")

    title = str(payload.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="Assignment title is required")

    assignment = ExtraClassAssignment(
        school_id=current_user.school_id,
        extra_class_id=extra_class_id,
        teacher_id=staff.id,
        title=title,
        description=str(payload.get("description") or "").strip() or None,
        instructions=str(payload.get("instructions") or "").strip() or None,
        due_date=datetime.fromisoformat(payload["due_date"]) if payload.get("due_date") else None,
        max_score=float(payload.get("max_score", 100.0) or 100.0),
    )
    session.add(assignment)
    await session.commit()
    await session.refresh(assignment)

    for raw_question in payload.get("questions", []) or []:
        if not isinstance(raw_question, dict):
            continue

        question_text = str(raw_question.get("question_text") or raw_question.get("question") or "").strip()
        if not question_text:
            continue

        raw_options = raw_question.get("options")
        if isinstance(raw_options, str):
            options_list = [item.strip() for item in raw_options.split('|') if item.strip()]
        elif isinstance(raw_options, list):
            options_list = [str(item).strip() for item in raw_options if str(item).strip()]
        else:
            options_list = []

        question = AssignmentQuestion(
            id=str(uuid.uuid4()),
            school_id=current_user.school_id,
            assignment_id=assignment.id,
            question_text=question_text,
            question_type=raw_question.get("question_type") or raw_question.get("type") or "essay",
            options=json.dumps(options_list) if options_list else None,
            correct_answer=str(raw_question.get("correct_answer") or raw_question.get("answer") or "").strip() or None,
            points=float(raw_question.get("points", 1.0) or 1.0),
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        session.add(question)

    await session.commit()

    return {"message": "Assignment created", "assignment": {"id": assignment.id, "title": assignment.title}}


async def _get_owned_assignment_or_404(extra_class_id: str, assignment_id: str, current_user: User, session: AsyncSession) -> ExtraClassAssignment:
    extra_class_result = await session.execute(select(ExtraClass).where(ExtraClass.id == extra_class_id))
    extra_class = extra_class_result.scalar_one_or_none()
    if not extra_class:
        raise HTTPException(status_code=404, detail="Extra class not found")

    staff_result = await session.execute(select(Staff).where(Staff.user_id == current_user.id))
    staff = staff_result.scalar_one_or_none()
    if not staff or extra_class.teacher_id != staff.id:
        raise HTTPException(status_code=403, detail="You can only manage assignments for your own classes")

    assignment_result = await session.execute(
        select(ExtraClassAssignment).where(
            ExtraClassAssignment.id == assignment_id,
            ExtraClassAssignment.extra_class_id == extra_class_id,
        )
    )
    assignment = assignment_result.scalar_one_or_none()
    if not assignment:
        raise HTTPException(status_code=404, detail="Assignment not found")
    return assignment


@router.put("/{extra_class_id}/assignments/{assignment_id}", response_model=dict)
async def update_extra_class_assignment(
    extra_class_id: str,
    assignment_id: str,
    payload: dict,
    current_user: User = Depends(require_roles(UserRole.TEACHER)),
    session: AsyncSession = Depends(get_session),
):
    assignment = await _get_owned_assignment_or_404(extra_class_id, assignment_id, current_user, session)

    if "title" in payload:
        title = str(payload.get("title") or "").strip()
        if not title:
            raise HTTPException(status_code=400, detail="Assignment title is required")
        assignment.title = title
    if "description" in payload:
        assignment.description = str(payload.get("description") or "").strip() or None
    if "instructions" in payload:
        assignment.instructions = str(payload.get("instructions") or "").strip() or None
    if "due_date" in payload:
        assignment.due_date = datetime.fromisoformat(payload["due_date"]) if payload.get("due_date") else None
    if "max_score" in payload:
        assignment.max_score = float(payload.get("max_score") or 100.0)
    assignment.updated_at = datetime.utcnow()
    session.add(assignment)

    if "questions" in payload and payload.get("questions"):
        existing_questions_result = await session.execute(
            select(AssignmentQuestion).where(AssignmentQuestion.assignment_id == assignment_id)
        )
        for existing_question in existing_questions_result.scalars().all():
            await session.delete(existing_question)

        for raw_question in payload.get("questions", []) or []:
            if not isinstance(raw_question, dict):
                continue
            question_text = str(raw_question.get("question_text") or raw_question.get("question") or "").strip()
            if not question_text:
                continue

            raw_options = raw_question.get("options")
            if isinstance(raw_options, str):
                options_list = [item.strip() for item in raw_options.split('|') if item.strip()]
            elif isinstance(raw_options, list):
                options_list = [str(item).strip() for item in raw_options if str(item).strip()]
            else:
                options_list = []

            session.add(AssignmentQuestion(
                id=str(uuid.uuid4()),
                school_id=current_user.school_id,
                assignment_id=assignment.id,
                question_text=question_text,
                question_type=raw_question.get("question_type") or raw_question.get("type") or "essay",
                options=json.dumps(options_list) if options_list else None,
                correct_answer=str(raw_question.get("correct_answer") or raw_question.get("answer") or "").strip() or None,
                points=float(raw_question.get("points", 1.0) or 1.0),
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            ))

    await session.commit()
    return {"message": "Assignment updated", "assignment": {"id": assignment.id, "title": assignment.title}}


@router.delete("/{extra_class_id}/assignments/{assignment_id}", response_model=dict)
async def delete_extra_class_assignment(
    extra_class_id: str,
    assignment_id: str,
    current_user: User = Depends(require_roles(UserRole.TEACHER)),
    session: AsyncSession = Depends(get_session),
):
    assignment = await _get_owned_assignment_or_404(extra_class_id, assignment_id, current_user, session)

    questions_result = await session.execute(
        select(AssignmentQuestion).where(AssignmentQuestion.assignment_id == assignment_id)
    )
    for question in questions_result.scalars().all():
        await session.delete(question)

    submissions_result = await session.execute(
        select(ExtraClassSubmission).where(ExtraClassSubmission.assignment_id == assignment_id)
    )
    submission_ids = [s.id for s in submissions_result.scalars().all()]
    if submission_ids:
        grades_result = await session.execute(
            select(ExtraClassGrade).where(ExtraClassGrade.submission_id.in_(submission_ids))
        )
        for grade in grades_result.scalars().all():
            await session.delete(grade)

        resubmit_result = await session.execute(
            select(ExtraClassSubmission).where(ExtraClassSubmission.assignment_id == assignment_id)
        )
        for submission in resubmit_result.scalars().all():
            await session.delete(submission)

    await session.delete(assignment)
    await session.commit()
    return {"message": "Assignment deleted", "assignment_id": assignment_id}


@router.get("/{extra_class_id}/assignments/{assignment_id}", response_model=dict)
async def get_extra_class_assignment(
    extra_class_id: str,
    assignment_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    extra_class_result = await session.execute(select(ExtraClass).where(ExtraClass.id == extra_class_id))
    extra_class = extra_class_result.scalar_one_or_none()
    if not extra_class:
        raise HTTPException(status_code=404, detail="Extra class not found")
    if extra_class.school_id != current_user.school_id:
        raise HTTPException(status_code=403, detail="Access denied")

    assignment_result = await session.execute(
        select(ExtraClassAssignment).where(
            ExtraClassAssignment.id == assignment_id,
            ExtraClassAssignment.extra_class_id == extra_class_id,
        )
    )
    assignment = assignment_result.scalar_one_or_none()
    if not assignment:
        raise HTTPException(status_code=404, detail="Assignment not found")

    # Correct answers are only for the class's own teacher — not just any
    # TEACHER account, which would let a teacher pull another teacher's
    # answer key for a class they don't run.
    is_owning_teacher = False
    if current_user.role == UserRole.TEACHER:
        staff_result = await session.execute(select(Staff).where(Staff.user_id == current_user.id))
        staff = staff_result.scalar_one_or_none()
        is_owning_teacher = bool(staff and extra_class.teacher_id == staff.id)

    questions_result = await session.execute(
        select(AssignmentQuestion).where(AssignmentQuestion.assignment_id == assignment_id)
    )
    questions = format_assignment_questions(
        questions_result.scalars().all(),
        reveal_answers=is_owning_teacher,
    )

    return {
        "assignment": {
            "id": assignment.id,
            "title": assignment.title,
            "description": assignment.description,
            "instructions": assignment.instructions,
            "due_date": assignment.due_date.isoformat() if assignment.due_date else None,
            "max_score": assignment.max_score,
            "questions": questions,
            "questions_count": len(questions),
        }
    }


@router.post("/{extra_class_id}/assignments/{assignment_id}/submit", response_model=dict)
async def submit_extra_class_assignment(
    extra_class_id: str,
    assignment_id: str,
    payload: dict,
    current_user: User = Depends(require_roles(UserRole.STUDENT)),
    session: AsyncSession = Depends(get_session),
):
    extra_class_result = await session.execute(select(ExtraClass).where(ExtraClass.id == extra_class_id))
    extra_class = extra_class_result.scalar_one_or_none()
    if not extra_class:
        raise HTTPException(status_code=404, detail="Extra class not found")

    student_result = await session.execute(select(Student).where(Student.user_id == current_user.id))
    student = student_result.scalar_one_or_none()
    if not student:
        raise HTTPException(status_code=404, detail="Student profile not found")

    assignment_result = await session.execute(
        select(ExtraClassAssignment).where(
            ExtraClassAssignment.id == assignment_id,
            ExtraClassAssignment.extra_class_id == extra_class_id,
        )
    )
    assignment = assignment_result.scalar_one_or_none()
    if not assignment:
        raise HTTPException(status_code=404, detail="Assignment not found")

    enrollment_result = await session.execute(
        select(ExtraClassEnrollment).where(
            ExtraClassEnrollment.extra_class_id == extra_class_id,
            ExtraClassEnrollment.student_id == student.id,
            ExtraClassEnrollment.status.in_([EnrollmentStatus.APPROVED, EnrollmentStatus.ACTIVE]),
        )
    )
    enrollment = enrollment_result.scalar_one_or_none()
    if not enrollment:
        raise HTTPException(status_code=403, detail="You are not enrolled in this extra class")

    answers = payload.get("answers")
    submission_text = payload.get("submission_text")
    if answers is not None:
        submission_text = json.dumps(answers)

    now = datetime.utcnow()
    new_status = resolve_submission_status(assignment.due_date, now)

    existing_result = await session.execute(
        select(ExtraClassSubmission).where(
            ExtraClassSubmission.assignment_id == assignment_id,
            ExtraClassSubmission.student_id == student.id,
        )
    )
    submission = existing_result.scalar_one_or_none()
    if not submission:
        submission = ExtraClassSubmission(
            school_id=current_user.school_id,
            extra_class_id=extra_class_id,
            assignment_id=assignment_id,
            student_id=student.id,
            status=new_status,
            submission_text=submission_text,
            submission_file_url=payload.get("submission_file_url"),
            submitted_at=now,
        )
        session.add(submission)
    else:
        was_graded = submission.status == SubmissionStatus.GRADED
        submission.status = new_status
        submission.submission_text = submission_text if submission_text is not None else submission.submission_text
        submission.submission_file_url = payload.get("submission_file_url", submission.submission_file_url)
        submission.submitted_at = now
        submission.updated_at = now
        session.add(submission)

        # The content changed since this was graded — the old score/feedback
        # no longer reflects what's actually submitted, so drop it rather
        # than let a stale grade sit next to the new answer looking current.
        if was_graded:
            grade_result = await session.execute(
                select(ExtraClassGrade).where(ExtraClassGrade.submission_id == submission.id)
            )
            stale_grade = grade_result.scalar_one_or_none()
            if stale_grade:
                await session.delete(stale_grade)

    await session.commit()
    await session.refresh(submission)
    return {"message": "Submission recorded", "submission": serialize_submission(submission)}


@router.get("/{extra_class_id}/assignments/{assignment_id}/submissions", response_model=dict)
async def list_extra_class_submissions(
    extra_class_id: str,
    assignment_id: str,
    current_user: User = Depends(require_roles(UserRole.TEACHER)),
    session: AsyncSession = Depends(get_session),
):
    extra_class_result = await session.execute(select(ExtraClass).where(ExtraClass.id == extra_class_id))
    extra_class = extra_class_result.scalar_one_or_none()
    if not extra_class:
        raise HTTPException(status_code=404, detail="Extra class not found")

    staff_result = await session.execute(select(Staff).where(Staff.user_id == current_user.id))
    staff = staff_result.scalar_one_or_none()
    if not staff or extra_class.teacher_id != staff.id:
        raise HTTPException(status_code=403, detail="You can only view submissions for your own classes")

    submissions_result = await session.execute(
        select(ExtraClassSubmission).where(
            ExtraClassSubmission.extra_class_id == extra_class_id,
            ExtraClassSubmission.assignment_id == assignment_id,
        )
    )
    submissions = submissions_result.scalars().all()
    return {"submissions": [await serialize_submission_detail(item, session) for item in submissions]}


@router.post("/{extra_class_id}/assignments/{assignment_id}/submissions/{submission_id}/grade", response_model=dict)
async def grade_extra_class_submission(
    extra_class_id: str,
    assignment_id: str,
    submission_id: str,
    payload: dict,
    current_user: User = Depends(require_roles(UserRole.TEACHER)),
    session: AsyncSession = Depends(get_session),
):
    extra_class_result = await session.execute(select(ExtraClass).where(ExtraClass.id == extra_class_id))
    extra_class = extra_class_result.scalar_one_or_none()
    if not extra_class:
        raise HTTPException(status_code=404, detail="Extra class not found")

    staff_result = await session.execute(select(Staff).where(Staff.user_id == current_user.id))
    staff = staff_result.scalar_one_or_none()
    if not staff or extra_class.teacher_id != staff.id:
        raise HTTPException(status_code=403, detail="You can only grade submissions for your own classes")

    submission_result = await session.execute(select(ExtraClassSubmission).where(ExtraClassSubmission.id == submission_id))
    submission = submission_result.scalar_one_or_none()
    if not submission or submission.extra_class_id != extra_class_id or submission.assignment_id != assignment_id:
        raise HTTPException(status_code=404, detail="Submission not found")

    grade_result = await session.execute(
        select(ExtraClassGrade).where(ExtraClassGrade.submission_id == submission.id)
    )
    grade = grade_result.scalar_one_or_none()
    if not grade:
        grade = ExtraClassGrade(
            school_id=current_user.school_id,
            extra_class_id=extra_class_id,
            assignment_id=assignment_id,
            submission_id=submission.id,
            student_id=submission.student_id,
            teacher_id=staff.id,
            score=payload.get("score", 0.0),
            feedback=payload.get("feedback"),
        )
        session.add(grade)
    else:
        grade.score = payload.get("score", grade.score)
        grade.feedback = payload.get("feedback", grade.feedback)
        grade.teacher_id = staff.id
        grade.updated_at = datetime.utcnow()

    submission.status = SubmissionStatus.GRADED
    submission.updated_at = datetime.utcnow()
    await session.commit()
    await session.refresh(grade)
    return {"message": "Grade recorded", "grade": serialize_grade(grade)}


async def _send_reminder_to_enrolled_parents(
    session: AsyncSession,
    current_user: User,
    extra_class_id: str,
    message: str,
    reminder_type: str,
    billing_cycle_id: str = "",
) -> int:
    """Sends `message` by SMS to every approved/active parent of a class,
    logging each attempt. Shared by the payment-due reminder and the
    upcoming-session reminder — both are the same "notify my enrolled
    parents" action, just with different copy and a different log tag."""
    enrollment_result = await session.execute(
        select(ExtraClassEnrollment).where(
            ExtraClassEnrollment.extra_class_id == extra_class_id,
            ExtraClassEnrollment.status.in_([EnrollmentStatus.APPROVED, EnrollmentStatus.ACTIVE]),
        )
    )
    enrollments = enrollment_result.scalars().all()

    reminder_logs = []
    sms_sent_count = 0

    for enrollment in enrollments:
        parent_result = await session.execute(select(Parent).where(Parent.id == enrollment.parent_id))
        parent = parent_result.scalar_one_or_none()
        if not parent or not parent.phone:
            continue

        if not sms_service.validate_phone_number(parent.phone):
            continue

        formatted_phone = sms_service.format_phone_number(parent.phone)
        sms_result = await sms_service.send_sms([formatted_phone], message)
        reminder_status = "sent" if sms_result.get("success") else "failed"
        if sms_result.get("success"):
            sms_sent_count += 1

        reminder = ExtraClassReminderLog(
            school_id=current_user.school_id,
            billing_cycle_id=billing_cycle_id,
            parent_id=parent.id,
            reminder_type=reminder_type,
            message=message,
            status=reminder_status,
        )
        reminder_logs.append(reminder)
        session.add(reminder)

        session.add(
            SMSNotification(
                school_id=current_user.school_id,
                recipient_phone=formatted_phone,
                recipient_name=f"{parent.first_name} {parent.last_name}".strip(),
                message=message,
                notification_type="extra_class_reminder",
                status="sent" if sms_result.get("success") else "failed",
                message_id=sms_result.get("message_ids", [None])[0] if sms_result.get("message_ids") else None,
                error_message=sms_result.get("error") if not sms_result.get("success") else None,
                sent_at=datetime.utcnow() if sms_result.get("success") else None,
            )
        )

    if not reminder_logs:
        raise HTTPException(status_code=400, detail="No valid parent phone numbers were found for this class")

    return sms_sent_count


@router.post("/{extra_class_id}/remind-parent", response_model=dict)
async def remind_parent_for_payment(
    extra_class_id: str,
    payload: dict,
    current_user: User = Depends(require_roles(UserRole.TEACHER)),
    session: AsyncSession = Depends(get_session),
):
    extra_class_result = await session.execute(select(ExtraClass).where(ExtraClass.id == extra_class_id))
    extra_class = extra_class_result.scalar_one_or_none()
    if not extra_class:
        raise HTTPException(status_code=404, detail="Extra class not found")

    staff_result = await session.execute(select(Staff).where(Staff.user_id == current_user.id))
    staff = staff_result.scalar_one_or_none()
    if not staff or extra_class.teacher_id != staff.id:
        raise HTTPException(status_code=403, detail="You can only remind parents for your own classes")

    reminder_message = payload.get("message") or f"Reminder: payment for {extra_class.title} is due. Please settle it promptly."

    sms_sent_count = await _send_reminder_to_enrolled_parents(
        session, current_user, extra_class_id, reminder_message, "due_reminder", payload.get("billing_cycle_id", "")
    )

    await session.commit()
    return {
        "message": f"Reminder SMS sent to {sms_sent_count} parent(s)",
        "reminder": {"message": reminder_message, "sent_count": sms_sent_count},
    }


@router.post("/{extra_class_id}/remind-session", response_model=dict)
async def remind_upcoming_session(
    extra_class_id: str,
    payload: dict,
    current_user: User = Depends(require_roles(UserRole.TEACHER)),
    session: AsyncSession = Depends(get_session),
):
    """A teacher-triggered nudge that a session is coming up — there's no
    scheduler in this codebase (see subscription_suspension_service.py) to
    fire this automatically at a fixed time before class starts, so it's a
    manual action, same as the existing payment reminder."""
    extra_class_result = await session.execute(select(ExtraClass).where(ExtraClass.id == extra_class_id))
    extra_class = extra_class_result.scalar_one_or_none()
    if not extra_class:
        raise HTTPException(status_code=404, detail="Extra class not found")

    staff_result = await session.execute(select(Staff).where(Staff.user_id == current_user.id))
    staff = staff_result.scalar_one_or_none()
    if not staff or extra_class.teacher_id != staff.id:
        raise HTTPException(status_code=403, detail="You can only remind parents for your own classes")

    default_schedule = f" ({extra_class.schedule})" if extra_class.schedule else ""
    reminder_message = payload.get("message") or f"Reminder: {extra_class.title}{default_schedule} is coming up. See you there!"

    sms_sent_count = await _send_reminder_to_enrolled_parents(
        session, current_user, extra_class_id, reminder_message, "session_reminder"
    )

    await session.commit()
    return {
        "message": f"Session reminder sent to {sms_sent_count} parent(s)",
        "reminder": {"message": reminder_message, "sent_count": sms_sent_count},
    }


# ── School-admin oversight ──────────────────────────────────────────────────
# Before this, extra classes were entirely teacher-run with no visibility for
# the school: a teacher could set any price, approve their own enrollments,
# and (before the payment fix above) self-report payment, with nobody at the
# school able to see revenue, disputes, or pending payouts. These endpoints
# give SCHOOL_ADMIN/SUPER_ADMIN the same oversight this app already has for
# fees, payroll, and canteen.

@router.get("/admin/overview", response_model=dict)
async def get_extra_classes_admin_overview(
    current_user: User = Depends(require_roles(*ADMIN_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    classes_result = await session.execute(
        select(ExtraClass).where(ExtraClass.school_id == current_user.school_id).order_by(ExtraClass.created_at.desc())
    )
    classes = classes_result.scalars().all()

    rows = []
    for extra_class in classes:
        enrolled_count, _ = await check_capacity(session, extra_class)

        revenue_result = await session.execute(
            select(func.coalesce(func.sum(ExtraClassBillingCycle.amount), 0.0)).where(
                ExtraClassBillingCycle.extra_class_id == extra_class.id,
                ExtraClassBillingCycle.status == "paid",
            )
        )
        total_revenue = revenue_result.scalar() or 0.0

        teacher_name = None
        payout_status = None
        staff_result = await session.execute(select(Staff).where(Staff.id == extra_class.teacher_id))
        staff = staff_result.scalar_one_or_none()
        if staff:
            teacher_name = f"{staff.first_name} {staff.last_name}"
            payout_status = staff.payout_verification_status.value if hasattr(staff.payout_verification_status, "value") else str(staff.payout_verification_status)

        rows.append({
            "id": extra_class.id,
            "title": extra_class.title,
            "teacher_name": teacher_name,
            "teacher_payout_status": payout_status,
            "status": normalize_status(extra_class.status),
            "price": extra_class.price,
            "billing_interval": normalize_status(extra_class.billing_interval),
            "enrolled_count": enrolled_count,
            "max_students_per_session": extra_class.max_students_per_session,
            # Money never pools in the school's account — each paid cycle
            # already settled straight to the teacher via their Paystack
            # subaccount. This is a revenue record, not a balance owed.
            "total_revenue": round(total_revenue, 2),
        })

    return {"extra_classes": rows}


@router.get("/{extra_class_id}/assignments", response_model=dict)
async def list_extra_class_assignments(
    extra_class_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(select(ExtraClassAssignment).where(ExtraClassAssignment.extra_class_id == extra_class_id))
    assignments = result.scalars().all()
    assignment_ids = [assignment.id for assignment in assignments]
    question_counts = {}
    if assignment_ids:
        question_count_result = await session.execute(
            select(AssignmentQuestion.assignment_id, func.count(AssignmentQuestion.id))
            .where(AssignmentQuestion.assignment_id.in_(assignment_ids))
            .group_by(AssignmentQuestion.assignment_id)
        )
        for row in question_count_result.all():
            question_counts[row[0]] = row[1]

    student = None
    if current_user.role == UserRole.STUDENT:
        student_result = await session.execute(select(Student).where(Student.user_id == current_user.id))
        student = student_result.scalar_one_or_none()

    submissions = {}
    if student and assignment_ids:
        submissions_result = await session.execute(
            select(ExtraClassSubmission).where(
                ExtraClassSubmission.student_id == student.id,
                ExtraClassSubmission.assignment_id.in_(assignment_ids)
            )
        )
        for submission in submissions_result.scalars().all():
            submissions[submission.assignment_id] = submission

    assignment_rows = []
    now = datetime.utcnow()
    for assignment in assignments:
        submission = submissions.get(assignment.id)
        status = 'pending'
        graded_flag = False
        submitted_at = None

        if submission:
            status = normalize_status(submission.status)
            submitted_at = submission.submitted_at.isoformat() if submission.submitted_at else None
            graded_flag = status == 'graded'
        elif assignment.due_date and now > assignment.due_date:
            status = 'overdue'

        assignment_rows.append({
            "id": assignment.id,
            "title": assignment.title,
            "description": assignment.description,
            "instructions": assignment.instructions,
            "due_date": assignment.due_date.isoformat() if assignment.due_date else None,
            "max_score": assignment.max_score,
            "questions_count": question_counts.get(assignment.id, 0),
            "status": status,
            "submitted_at": submitted_at,
            "graded": graded_flag,
        })

    return {"assignments": assignment_rows}


def serialize_submission(submission: ExtraClassSubmission) -> dict:
    return {
        "id": submission.id,
        "assignment_id": submission.assignment_id,
        "student_id": submission.student_id,
        "status": submission.status.value if hasattr(submission.status, 'value') else str(submission.status),
        "submission_text": submission.submission_text,
        "submission_file_url": submission.submission_file_url,
        "submitted_at": submission.submitted_at.isoformat() if submission.submitted_at else None,
        "created_at": submission.created_at.isoformat() if submission.created_at else None,
    }


def serialize_grade(grade: ExtraClassGrade) -> dict:
    return {
        "id": grade.id,
        "assignment_id": grade.assignment_id,
        "submission_id": grade.submission_id,
        "student_id": grade.student_id,
        "teacher_id": grade.teacher_id,
        "score": grade.score,
        "feedback": grade.feedback,
        "graded_at": grade.graded_at.isoformat() if grade.graded_at else None,
    }


async def serialize_submission_detail(submission: ExtraClassSubmission, session: AsyncSession) -> dict:
    student_result = await session.execute(select(Student).where(Student.id == submission.student_id))
    student = student_result.scalar_one_or_none()
    student_name = f"{student.first_name} {student.last_name}" if student else "Unknown"
    grade_result = await session.execute(select(ExtraClassGrade).where(ExtraClassGrade.submission_id == submission.id))
    grade = grade_result.scalar_one_or_none()
    payload = serialize_submission(submission)
    payload.update({
        "student_name": student_name,
        "submission_date": submission.submitted_at.isoformat() if submission.submitted_at else None,
        "grade": serialize_grade(grade) if grade else None,
    })
    return payload


def serialize_extra_class(extra_class: ExtraClass) -> dict:
    return {
        "id": extra_class.id,
        "teacher_id": extra_class.teacher_id,
        "subject_id": extra_class.subject_id,
        "class_id": extra_class.class_id,
        "title": extra_class.title,
        "description": extra_class.description,
        "pricing_type": extra_class.pricing_type,
        "price": extra_class.price,
        "billing_interval": extra_class.billing_interval.value if hasattr(extra_class.billing_interval, 'value') else str(extra_class.billing_interval),
        "payout_frequency": extra_class.payout_frequency.value if hasattr(extra_class.payout_frequency, 'value') else str(extra_class.payout_frequency),
        "session_duration_hours": extra_class.session_duration_hours,
        "frequency_per_week": extra_class.frequency_per_week,
        "max_students_per_session": extra_class.max_students_per_session,
        "contact_phone": extra_class.contact_phone,
        "contact_email": extra_class.contact_email,
        "meeting_link": extra_class.meeting_link,
        "schedule": extra_class.schedule,
        "start_date": extra_class.start_date.isoformat() if extra_class.start_date else None,
        "end_date": extra_class.end_date.isoformat() if extra_class.end_date else None,
        "status": extra_class.status.value if hasattr(extra_class.status, 'value') else str(extra_class.status),
        "created_at": extra_class.created_at.isoformat() if extra_class.created_at else None,
    }


async def serialize_enrollment(enrollment: ExtraClassEnrollment, session: AsyncSession) -> dict:
    student_result = await session.execute(select(Student).where(Student.id == enrollment.student_id))
    student = student_result.scalar_one_or_none()
    student_name = f"{student.first_name} {student.last_name}" if student else "Unknown"
    return {
        "id": enrollment.id,
        "student_id": enrollment.student_id,
        "student_name": student_name,
        "status": enrollment.status.value if hasattr(enrollment.status, 'value') else str(enrollment.status),
        "created_at": enrollment.created_at.isoformat() if enrollment.created_at else None,
    }
