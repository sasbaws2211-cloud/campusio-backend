"""Teacher Dashboard router"""
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlmodel import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime
from models.user import User, UserRole
from models.staff import Staff
from models.assignment import Assignment, AssignmentStatus, Submission, SubmissionStatus
from database import get_session
from auth import require_roles

router = APIRouter(prefix="/teacher/dashboard", tags=["Teacher Portal - Dashboard"])


@router.get("", response_model=dict)
async def get_dashboard(
    term_id: Optional[str] = None,
    current_user: User = Depends(require_roles(UserRole.TEACHER)),
    session: AsyncSession = Depends(get_session)
):
    """
    Get teacher dashboard overview with key metrics.
    GES Aligned: Shows pending assessments, submissions status
    Optionally filtered by academic term.
    """
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=400, detail="No school context")
    
    try:
        # Get staff record to get teacher_id
        result = await session.execute(
            select(Staff).where(
                Staff.user_id == current_user.id,
                Staff.school_id == school_id
            )
        )
        staff = result.scalar_one_or_none()
        if not staff:
            raise HTTPException(status_code=400, detail="No teacher context")
        
        teacher_id = staff.id
        
        # Get my active assignments
        assignments_query = select(func.count(Assignment.id)).where(
            Assignment.school_id == school_id,
            Assignment.teacher_id == teacher_id,
            Assignment.status == AssignmentStatus.PUBLISHED
        )
        if term_id:
            assignments_query = assignments_query.where(Assignment.academic_term_id == term_id)
        result = await session.execute(assignments_query)
        active_assignments = result.scalar() or 0
        
        # Get pending submissions for MY assignments (not graded) — must join
        # to Assignment and scope by teacher_id, otherwise this counts every
        # teacher's pending submissions school-wide.
        from sqlalchemy import and_
        submissions_query = select(func.count(Submission.id)).select_from(Submission).join(
            Assignment,
            Submission.assignment_id == Assignment.id
        ).where(
            and_(
                Submission.school_id == school_id,
                Assignment.teacher_id == teacher_id,
                Submission.status.in_([SubmissionStatus.SUBMITTED, SubmissionStatus.LATE])
            )
        )
        if term_id:
            submissions_query = submissions_query.where(Assignment.academic_term_id == term_id)
        result = await session.execute(submissions_query)
        pending_grading = result.scalar() or 0
        
        # Get total students I teach
        from models.staff import TeacherAssignment
        result = await session.execute(
            select(func.count(TeacherAssignment.id)).where(
                TeacherAssignment.school_id == school_id,
                TeacherAssignment.staff_id == teacher_id
            )
        )
        assignments_count = result.scalar() or 0
        
        return {
            "active_assignments": active_assignments,
            "pending_grading": pending_grading,
            "class_assignments": assignments_count,
            "timestamp": datetime.utcnow().isoformat()
        }
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching dashboard: {str(e)}")


@router.get("/quick-stats", response_model=dict)
async def get_quick_stats(
    term_id: Optional[str] = None,
    current_user: User = Depends(require_roles(UserRole.TEACHER)),
    session: AsyncSession = Depends(get_session)
):
    """
    Get quick statistics for dashboard widgets.
    Returns numeric counts for UI display.
    Optionally filtered by academic term.
    """
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=400, detail="No school context")
    
    try:
        # Get staff record to get teacher_id
        result = await session.execute(
            select(Staff).where(
                Staff.user_id == current_user.id,
                Staff.school_id == school_id
            )
        )
        staff = result.scalar_one_or_none()
        if not staff:
            raise HTTPException(status_code=400, detail="No teacher context")
        
        teacher_id = staff.id
        
        # Total assignments created
        total_query = select(func.count(Assignment.id)).where(
            Assignment.school_id == school_id,
            Assignment.teacher_id == teacher_id
        )
        if term_id:
            total_query = total_query.where(Assignment.academic_term_id == term_id)
        result = await session.execute(total_query)
        total_assignments = result.scalar() or 0
        
        # Draft assignments (not published)
        draft_query = select(func.count(Assignment.id)).where(
            Assignment.school_id == school_id,
            Assignment.teacher_id == teacher_id,
            Assignment.status == AssignmentStatus.DRAFT
        )
        if term_id:
            draft_query = draft_query.where(Assignment.academic_term_id == term_id)
        result = await session.execute(draft_query)
        draft_count = result.scalar() or 0
        
        # Total submissions received for MY assignments — must join to
        # Assignment and scope by teacher_id, otherwise this counts every
        # teacher's submissions school-wide despite being labeled "personal".
        total_subs_query = select(func.count(Submission.id)).select_from(Submission).join(
            Assignment,
            Submission.assignment_id == Assignment.id
        ).where(
            Submission.school_id == school_id,
            Assignment.teacher_id == teacher_id,
            Submission.status == SubmissionStatus.SUBMITTED
        )
        if term_id:
            total_subs_query = total_subs_query.where(Assignment.academic_term_id == term_id)
        result = await session.execute(total_subs_query)
        total_submissions = result.scalar() or 0

        # Submissions graded for MY assignments — same teacher_id scoping.
        graded_query = select(func.count(Submission.id)).select_from(Submission).join(
            Assignment,
            Submission.assignment_id == Assignment.id
        ).where(
            Submission.school_id == school_id,
            Assignment.teacher_id == teacher_id,
            Submission.status == SubmissionStatus.GRADED
        )
        if term_id:
            graded_query = graded_query.where(Assignment.academic_term_id == term_id)
        result = await session.execute(graded_query)
        graded_submissions = result.scalar() or 0
        
        return {
            "total_assignments": total_assignments,
            "draft_assignments": draft_count,
            "published_assignments": total_assignments - draft_count,
            "total_submissions": total_submissions,
            "graded_submissions": graded_submissions,
            "pending_submissions": total_submissions - graded_submissions
        }
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching stats: {str(e)}")
