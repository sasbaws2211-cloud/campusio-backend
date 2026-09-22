"""Teacher Portal - Timetable Management Router

Endpoints for teachers to:
- View their assigned class timetables
- View periods and schedules
- Export timetable
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, func
from typing import Optional, List
from datetime import datetime

from models.timetable import Timetable, Period
from models.staff import TeacherAssignment, Staff
from models.classroom import Class, Subject
from models.student import Student
from models.school import AcademicTerm
from database import get_session
from auth import get_current_user, require_roles
from models.user import User, UserRole


router = APIRouter(prefix="/teacher/timetable", tags=["teacher-timetable"])


async def _resolve_term_id(session: AsyncSession, school_id: str, academic_term_id: Optional[str]) -> Optional[str]:
    """Without this, every schedule query here ignored term entirely — once a
    school has more than one term's worth of timetable entries for the same
    class, they'd all render stacked into the same day/period slots."""
    if academic_term_id:
        return academic_term_id
    result = await session.execute(
        select(AcademicTerm).where(AcademicTerm.school_id == school_id, AcademicTerm.is_current == True)
    )
    term = result.scalar_one_or_none()
    return term.id if term else None


@router.get("/my-schedule", response_model=dict)
async def get_my_timetable(
    academic_term_id: Optional[str] = None,
    current_user: User = Depends(require_roles(UserRole.TEACHER)),
    session: AsyncSession = Depends(get_session),
):
    """Get the teacher's complete timetable across all assigned classes,
    scoped to the given (or current) academic term."""
    try:
        school_id = current_user.school_id
        term_id = await _resolve_term_id(session, school_id, academic_term_id)

        # Get staff record for current user
        staff_result = await session.execute(
            select(Staff).where(Staff.user_id == current_user.id)
        )
        staff = staff_result.scalar_one_or_none()
        
        if not staff:
            # Fallback: try to match by email and school
            staff_result = await session.execute(
                select(Staff).where(
                    Staff.email == current_user.email,
                    Staff.school_id == current_user.school_id
                )
            )
            staff = staff_result.scalar_one_or_none()
        
        if not staff:
            raise HTTPException(status_code=403, detail="Staff profile not found")
        
        teacher_id = staff.id
        
        # Get all assignments for this teacher
        assignment_result = await session.execute(
            select(TeacherAssignment).where(
                and_(
                    TeacherAssignment.school_id == school_id,
                    TeacherAssignment.staff_id == teacher_id,
                )
            )
        )
        assignments = assignment_result.scalars().all()
        
        if not assignments:
            return {
                "teacher_id": teacher_id,
                "timetable": {},
                "message": "No classes assigned"
            }
        
        # Collect all class IDs and subject IDs
        class_ids = [a.class_id for a in assignments]
        subject_ids = [a.subject_id for a in assignments]
        
        # Get timetable entries for these classes and this teacher
        term_filters = [Timetable.academic_term_id == term_id] if term_id else []
        timetable_result = await session.execute(
            select(Timetable).where(
                and_(
                    Timetable.school_id == school_id,
                    Timetable.class_id.in_(class_ids),
                    Timetable.teacher_id == teacher_id,
                    *term_filters,
                )
            )
        )
        timetable_entries = timetable_result.scalars().all()

        # Organize by day and period (Mon-Fri only)
        schedule_by_day = {
            "Monday": [],
            "Tuesday": [],
            "Wednesday": [],
            "Thursday": [],
            "Friday": [],
        }
        
        for entry in timetable_entries:
            # Get period info
            period_result = await session.execute(
                select(Period).where(Period.id == entry.period_id)
            )
            period = period_result.scalar()
            
            # Get class info
            class_result = await session.execute(
                select(Class).where(Class.id == entry.class_id)
            )
            class_obj = class_result.scalar()
            
            # Get subject info
            subject_result = await session.execute(
                select(Subject).where(Subject.id == entry.subject_id)
            )
            subject = subject_result.scalar()
            
            # Convert Enum to string and title case to match dict keys
            day_name = (entry.day_of_week.value if entry.day_of_week else "monday").title()
            
            schedule_entry = {
                "class_id": entry.class_id,
                "class_name": class_obj.name if class_obj else "Unknown",
                "subject_id": entry.subject_id,
                "subject_name": subject.name if subject else "Unknown",
                "period_id": entry.period_id,
                "period_name": period.name if period else "Unknown",
                "period_number": period.period_number if period else 0,
                "start_time": period.start_time if period and period.start_time else None,
                "end_time": period.end_time if period and period.end_time else None,
            }
            
            schedule_by_day[day_name].append(schedule_entry)
        
        # Sort entries by period number within each day
        for day in schedule_by_day:
            schedule_by_day[day].sort(key=lambda x: x.get("period_number", 0))
        
        return {
            "teacher_id": teacher_id,
            "timetable": schedule_by_day,
            "message": "Timetable retrieved successfully"
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error retrieving timetable: {str(e)}")


@router.get("/class/{class_id}", response_model=dict)
async def get_class_timetable(
    class_id: str,
    academic_term_id: Optional[str] = None,
    current_user: User = Depends(require_roles(UserRole.TEACHER)),
    session: AsyncSession = Depends(get_session),
):
    """
    Get timetable for a specific class, scoped to the given (or current) term.
    Only accessible to teacher assigned to teach that class.
    """
    try:
        school_id = current_user.school_id
        term_id = await _resolve_term_id(session, school_id, academic_term_id)

        # Get staff record for current user
        staff_result = await session.execute(
            select(Staff).where(Staff.user_id == current_user.id)
        )
        staff = staff_result.scalar_one_or_none()
        
        if not staff:
            # Fallback: try to match by email and school
            staff_result = await session.execute(
                select(Staff).where(
                    Staff.email == current_user.email,
                    Staff.school_id == current_user.school_id
                )
            )
            staff = staff_result.scalar_one_or_none()
        
        if not staff:
            raise HTTPException(status_code=403, detail="Staff profile not found")
        
        teacher_id = staff.id
        
        # Verify teacher is assigned to this class
        assignment_result = await session.execute(
            select(TeacherAssignment).where(
                and_(
                    TeacherAssignment.school_id == school_id,
                    TeacherAssignment.staff_id == teacher_id,
                    TeacherAssignment.class_id == class_id,
                )
            )
        )
        assignment = assignment_result.scalar()
        
        if not assignment:
            raise HTTPException(status_code=403, detail="Not authorized to view this class timetable")
        
        # Get class info
        class_result = await session.execute(
            select(Class).where(Class.id == class_id)
        )
        class_obj = class_result.scalar()
        
        if not class_obj:
            raise HTTPException(status_code=404, detail="Class not found")
        
        # Get all timetable entries for this class
        term_filters = [Timetable.academic_term_id == term_id] if term_id else []
        timetable_result = await session.execute(
            select(Timetable).where(
                and_(
                    Timetable.school_id == school_id,
                    Timetable.class_id == class_id,
                    *term_filters,
                )
            ).order_by(Timetable.day_of_week, Timetable.period_id)
        )
        timetable_entries = timetable_result.scalars().all()
        
        # Organize by day and period (Mon-Fri only)
        schedule_by_day = {
            "Monday": [],
            "Tuesday": [],
            "Wednesday": [],
            "Thursday": [],
            "Friday": [],
        }
        
        for entry in timetable_entries:
            # Get period info
            period_result = await session.execute(
                select(Period).where(Period.id == entry.period_id)
            )
            period = period_result.scalar()
            
            # Get subject info
            subject_result = await session.execute(
                select(Subject).where(Subject.id == entry.subject_id)
            )
            subject = subject_result.scalar()
            
            # Get teacher info
            staff_result = await session.execute(
                select(Staff).where(Staff.id == entry.teacher_id)
            )
            staff = staff_result.scalar()
            
            # Convert Enum to string and title case to match dict keys
            day_name = (entry.day_of_week.value if entry.day_of_week else "monday").title()
            
            schedule_entry = {
                "subject_id": entry.subject_id,
                "subject_name": subject.name if subject else "Unknown",
                "teacher_id": entry.teacher_id,
                "teacher_name": f"{staff.first_name} {staff.last_name}" if staff else "Unknown",
                "period_id": entry.period_id,
                "period_name": period.name if period else "Unknown",
                "period_number": period.period_number if period else 0,
                "start_time": period.start_time if period and period.start_time else None,
                "end_time": period.end_time if period and period.end_time else None,
                "room": entry.room or "Not assigned",
            }
            
            schedule_by_day[day_name].append(schedule_entry)
        
        # Sort entries by period number within each day
        for day in schedule_by_day:
            schedule_by_day[day].sort(key=lambda x: x.get("period_number", 0))
        
        return {
            "class_id": class_id,
            "class_name": class_obj.name,
            "timetable": schedule_by_day,
            "message": "Class timetable retrieved successfully"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error retrieving class timetable: {str(e)}")


@router.get("/periods", response_model=dict)
async def get_school_periods(
    current_user: User = Depends(require_roles(UserRole.TEACHER)),
    session: AsyncSession = Depends(get_session),
):
    """Get all periods defined for the school (used in timetables)."""
    try:
        school_id = current_user.school_id
        
        # Get all periods for this school
        result = await session.execute(
            select(Period).where(Period.school_id == school_id)
            .order_by(Period.period_number)
        )
        periods = result.scalars().all()
        
        periods_list = [
            {
                "period_id": p.id,
                "period_number": p.period_number,
                "name": p.name,
                "start_time": p.start_time if p.start_time else None,
                "end_time": p.end_time if p.end_time else None,
            }
            for p in periods
        ]
        
        return {
            "items": periods_list,
            "total": len(periods_list),
            "message": "School periods retrieved successfully"
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error retrieving periods: {str(e)}")


@router.get("/day/{day_of_week}", response_model=dict)
async def get_teacher_schedule_by_day(
    day_of_week: str,
    academic_term_id: Optional[str] = None,
    current_user: User = Depends(require_roles(UserRole.TEACHER)),
    session: AsyncSession = Depends(get_session),
):
    """
    Get teacher's schedule for a specific day of the week, scoped to the
    given (or current) term.

    day_of_week: Monday, Tuesday, Wednesday, Thursday, Friday
    """
    try:
        school_id = current_user.school_id
        term_id = await _resolve_term_id(session, school_id, academic_term_id)

        # Get staff record for current user
        staff_result = await session.execute(
            select(Staff).where(Staff.user_id == current_user.id)
        )
        staff = staff_result.scalar_one_or_none()
        
        if not staff:
            # Fallback: try to match by email and school
            staff_result = await session.execute(
                select(Staff).where(
                    Staff.email == current_user.email,
                    Staff.school_id == current_user.school_id
                )
            )
            staff = staff_result.scalar_one_or_none()
        
        if not staff:
            raise HTTPException(status_code=403, detail="Staff profile not found")
        
        teacher_id = staff.id
        
        valid_days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
        if day_of_week not in valid_days:
            raise HTTPException(status_code=400, detail=f"Invalid day. Must be one of: {', '.join(valid_days)}")
        
        # Convert to lowercase for database enum
        day_of_week_enum = day_of_week.lower()
        
        # Get timetable entries for this teacher on this day
        term_filters = [Timetable.academic_term_id == term_id] if term_id else []
        timetable_result = await session.execute(
            select(Timetable).where(
                and_(
                    Timetable.school_id == school_id,
                    Timetable.teacher_id == teacher_id,
                    Timetable.day_of_week == day_of_week_enum,
                    *term_filters,
                )
            ).order_by(Timetable.period_id)
        )
        timetable_entries = timetable_result.scalars().all()
        
        schedule_list = []
        for entry in timetable_entries:
            # Get period info
            period_result = await session.execute(
                select(Period).where(Period.id == entry.period_id)
            )
            period = period_result.scalar()
            
            # Get class info
            class_result = await session.execute(
                select(Class).where(Class.id == entry.class_id)
            )
            class_obj = class_result.scalar()
            
            # Get subject info
            subject_result = await session.execute(
                select(Subject).where(Subject.id == entry.subject_id)
            )
            subject = subject_result.scalar()
            
            schedule_entry = {
                "class_id": entry.class_id,
                "class_name": class_obj.name if class_obj else "Unknown",
                "subject_id": entry.subject_id,
                "subject_name": subject.name if subject else "Unknown",
                "period_id": entry.period_id,
                "period_name": period.name if period else "Unknown",
                "period_number": period.period_number if period else 0,
                "start_time": period.start_time if period and period.start_time else None,
                "end_time": period.end_time if period and period.end_time else None,
                "room": entry.room or "Not assigned",
            }
            
            schedule_list.append(schedule_entry)
        
        return {
            "day_of_week": day_of_week,
            "schedule": schedule_list,
            "total_periods": len(schedule_list),
            "message": "Day schedule retrieved successfully"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error retrieving day schedule: {str(e)}")
