"""Assignment Performance Service - Calculate performance metrics for parent dashboard"""
import logging
from datetime import datetime
from typing import List, Dict, Optional, Tuple
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from statistics import mean
from dateutil import parser as date_parser

from models.assignment import Assignment, Submission, SubmissionStatus, CourseModule, CourseModuleItem
from models.grade import Grade
from models.classroom import Class, Subject
from utils.grade_scale import get_letter_grade
from services import grading_service
from services.report_card_pdf_service import compute_subject_ges_totals

logger = logging.getLogger(__name__)


def get_ges_grade(percentage: float, scale: list = None) -> Tuple[str, str]:
    """Convert percentage to grade and description. scale defaults to the
    built-in GES scale — pass a school-configured one (services/grading_service.py)
    to support the multiple grading systems feature."""
    band = get_letter_grade(percentage, scale=scale)
    return band["grade"], band["description"]


def calculate_trend_direction(scores: List[float]) -> str:
    """Determine trend direction from list of scores"""
    if len(scores) < 2:
        return "→ Insufficient data"
    
    # Calculate average of first half vs second half
    mid = len(scores) // 2
    first_half_avg = mean(scores[:mid]) if scores[:mid] else 0
    second_half_avg = mean(scores[mid:]) if scores[mid:] else 0
    
    if second_half_avg > first_half_avg + 3:
        return "↗ Improving"
    elif second_half_avg < first_half_avg - 3:
        return "↘ Declining"
    else:
        return "→ Stable"


def ensure_datetime(dt_value) -> Optional[datetime]:
    """Convert string or datetime to datetime object"""
    if dt_value is None:
        return None
    if isinstance(dt_value, str):
        try:
            return date_parser.parse(dt_value)
        except:
            return None
    return dt_value


class AssignmentPerformanceService:
    """Service for calculating assignment performance metrics"""
    
    def __init__(self, session: AsyncSession):
        self.session = session
    
    # ==================== Metric 1: Assignment Performance Index ====================
    
    async def calculate_api(self, submissions: List[Submission], scale: list = None) -> Dict:
        """
        Calculate Assignment Performance Index (API)
        
        API = (Total Points Earned / Total Points Possible) × 100
        
        Returns:
            Dict with percentage, grade, description, trend, and status
        """
        if not submissions:
            return {
                "percentage": 0,
                "grade": "9",
                "description": "Fail",
                "trend": "→ No data",
                "status": "No grades yet"
            }
        
        # Filter to only graded submissions with scores
        graded_submissions = [s for s in submissions if s.score is not None and s.max_score]
        
        if not graded_submissions:
            return {
                "percentage": 0,
                "grade": "9",
                "description": "Fail",
                "trend": "→ Awaiting grades",
                "status": "Grades pending"
            }
        
        total_earned = sum(s.score for s in graded_submissions)
        total_possible = sum(s.max_score for s in graded_submissions)
        
        if total_possible == 0:
            return {
                "percentage": 0,
                "grade": "9",
                "description": "Fail",
                "trend": "→ No data",
                "status": "No grades yet"
            }
        
        percentage = (total_earned / total_possible) * 100
        grade, description = get_ges_grade(percentage, scale=scale)
        
        # Calculate trend
        scores = [s.score for s in graded_submissions]
        trend = calculate_trend_direction(scores)
        
        # Determine status
        if percentage >= 70:
            status = "✓ On Track"
        elif percentage >= 50:
            status = "⚠ Needs Improvement"
        else:
            status = "❌ Needs Help"
        
        return {
            "percentage": round(percentage, 1),
            "grade": grade,
            "description": description,
            "trend": trend,
            "status": status
        }
    
    # ==================== Metric 2: Subject-Wise Performance ====================
    
    async def calculate_subject_performance(
        self, grades: List[Grade], session: AsyncSession, schemes: list = None, class_level: str = None
    ) -> Dict:
        """
        Calculate performance by subject

        schemes/class_level: from services.grading_service.get_school_schemes()
        — each subject's grade is matched against the most specific configured
        scheme for (class_level, that subject_id), falling back to the GES
        scale when nothing's configured.

        Returns:
            Dict mapping subject names to their performance stats
        """
        if not grades:
            return {}
        
        # Group grades by subject
        subjects = {}
        for grade in grades:
            if grade.subject_id not in subjects:
                subjects[grade.subject_id] = {
                    "grades": [],
                    "subject_name": None
                }
            subjects[grade.subject_id]["grades"].append(grade)
        
        # Get subject names
        result = {}
        for subject_id, data in subjects.items():
            # Fetch subject name if not cached
            if data["subject_name"] is None:
                subject_result = await session.execute(
                    select(Subject).where(Subject.id == subject_id)
                )
                subject = subject_result.scalar_one_or_none()
                subject_name = subject.name if subject else "Unknown"
            else:
                subject_name = data["subject_name"]
            
            scores = [g.score for g in data["grades"] if g.score is not None]

            if not scores:
                continue

            # GES-split, weighted percentage via compute_subject_ges_totals —
            # the same function report cards use, so this agrees with the
            # report card for the same subject/term. Previously a raw mean
            # of Grade.score fed straight into grade-banding as if it were
            # already a percentage — wrong whenever max_score varies between
            # assessments (e.g. a 18/20 quiz was banded as "18%").
            avg_score = compute_subject_ges_totals(data["grades"]).get(subject_id, {}).get("total_score", 0.0)
            subject_scale = grading_service.match_scale(schemes or [], class_level, subject_id)
            grade, description = get_ges_grade(avg_score, scale=subject_scale)
            trend = calculate_trend_direction([round(g.score / g.max_score * 100, 1) for g in data["grades"] if g.score is not None and g.max_score])
            
            result[subject_name] = {
                "average_score": round(avg_score, 1),
                "grade": grade,
                "description": description,
                "count": len(data["grades"]),
                "trend": trend
            }
        
        # Sort by average score descending
        return dict(sorted(result.items(), key=lambda x: x[1]["average_score"], reverse=True))
    
    # ==================== Metric 3: Completion & Punctuality ====================
    
    async def calculate_completion_metrics(
        self, 
        submissions: List[Submission],
        assignments: List[Assignment]
    ) -> Dict:
        """
        Calculate completion rate and punctuality metrics
        
        Returns:
            Dict with completion_rate, submitted_count, late_count, etc.
        """
        total = len(assignments)
        submitted = len([s for s in submissions if s.status in [SubmissionStatus.SUBMITTED, SubmissionStatus.GRADED]])
        
        completion_rate = (submitted / total * 100) if total > 0 else 0
        
        # Create a map of assignment_id to due_date
        due_dates = {a.id: a.due_date for a in assignments}
        
        # Calculate punctuality
        on_time = 0
        late = 0
        
        for submission in submissions:
            if submission.status in [SubmissionStatus.SUBMITTED, SubmissionStatus.GRADED]:
                if submission.submission_date and submission.assignment_id in due_dates:
                    due_date = due_dates[submission.assignment_id]
                    if due_date and submission.submission_date <= due_date:
                        on_time += 1
                    else:
                        late += 1
        
        punctuality_rate = (on_time / submitted * 100) if submitted > 0 else 0
        
        outstanding = total - submitted
        
        return {
            "completion_rate": round(completion_rate, 1),
            "submitted_count": submitted,
            "total_count": total,
            "outstanding": outstanding,
            "on_time_count": on_time,
            "late_count": late,
            "punctuality_rate": round(punctuality_rate, 1)
        }
    
    # ==================== Metric 4: Assessment Type Breakdown ====================
    
    async def calculate_assessment_type_breakdown(self, grades: List[Grade], scale: list = None) -> Dict:
        """
        Calculate performance by assessment type
        (classwork, homework, quiz, project, worksheet, etc.)
        
        Returns:
            Dict mapping assessment types to their statistics
        """
        if not grades:
            return {}
        
        # Group grades by assessment type. Scores are stored as each grade's
        # own percentage (score/max_score*100), not raw points — a raw mean
        # here would silently mix assessments with different max_score
        # values (e.g. a 20-point quiz and a 100-point exam) into one
        # meaningless number, then band it as if it were already a
        # percentage.
        types = {}
        for grade in grades:
            atype = grade.assessment_type  # from models
            if atype not in types:
                types[atype] = {"scores": [], "count": 0}

            if grade.score is not None and grade.max_score:
                types[atype]["scores"].append(round(grade.score / grade.max_score * 100, 1))
                types[atype]["count"] += 1
        
        result = {}
        type_order = ["classwork", "homework", "quiz", "project", "worksheet"]
        
        for atype in type_order:
            if atype in types and types[atype]["scores"]:
                scores = types[atype]["scores"]
                avg = mean(scores)
                grade, description = get_ges_grade(avg, scale=scale)

                result[atype] = {
                    "average": round(avg, 1),
                    "count": types[atype]["count"],
                    "best": round(max(scores), 1),
                    "lowest": round(min(scores), 1),
                    "trend": calculate_trend_direction(scores)
                }
        
        # Add any types not in the predefined order
        for atype, data in types.items():
            if atype not in result and data["scores"]:
                scores = data["scores"]
                avg = mean(scores)
                grade, description = get_ges_grade(avg, scale=scale)

                result[atype] = {
                    "average": round(avg, 1),
                    "count": data["count"],
                    "best": round(max(scores), 1),
                    "lowest": round(min(scores), 1),
                    "trend": calculate_trend_direction(scores)
                }
        
        return result
    
    # ==================== Metric 5: Progress Trend Analysis ====================
    
    async def calculate_progress_trend(
        self,
        submissions: List[Submission],
        term_start_date: datetime
    ) -> Dict:
        """
        Calculate progress trend across 4 windows during the term
        
        Returns:
            Dict with window_averages, direction, and change_percentage
        """
        # Ensure term_start_date is a datetime
        term_start_date = ensure_datetime(term_start_date)
        if not term_start_date:
            term_start_date = datetime.utcnow()
        
        # Filter to graded submissions with dates and a usable max_score —
        # same normalization gap already fixed in calculate_subject_performance/
        # calculate_assessment_type_breakdown: a raw mean of Submission.score
        # silently mixes assignments with different max_score values (a 20-point
        # quiz and a 100-point exam) into one meaningless number.
        graded_subs = [
            s for s in submissions
            if s.score is not None and s.graded_date and s.max_score
        ]
        
        if not graded_subs:
            return {
                "window_averages": {
                    "window_1": None,
                    "window_2": None,
                    "window_3": None,
                    "window_4": None
                },
                "direction": "→ Insufficient data",
                "change_percentage": 0
            }
        
        # Ensure all graded_dates are datetime objects
        graded_dates = [ensure_datetime(s.graded_date) for s in graded_subs]
        graded_dates = [d for d in graded_dates if d is not None]
        
        if not graded_dates:
            return {
                "window_averages": {
                    "window_1": None,
                    "window_2": None,
                    "window_3": None,
                    "window_4": None
                },
                "direction": "→ Insufficient data",
                "change_percentage": 0
            }
        
        # Calculate term duration
        end_date = max(graded_dates)
        duration_days = (end_date - term_start_date).days
        window_size = max(1, duration_days // 4)
        
        # Distribute submissions into windows
        windows = {
            "window_1": [],
            "window_2": [],
            "window_3": [],
            "window_4": []
        }
        
        for submission, graded_date in zip(graded_subs, graded_dates):
            days_elapsed = (graded_date - term_start_date).days
            window_num = min(days_elapsed // window_size, 3)
            windows[f"window_{window_num + 1}"].append(round(submission.score / submission.max_score * 100, 1))
        
        # Calculate averages for each window
        window_avgs = {}
        for window, scores in windows.items():
            if scores:
                window_avgs[window] = round(mean(scores), 1)
            else:
                window_avgs[window] = None
        
        # Determine trend direction
        valid_avgs = [v for v in window_avgs.values() if v is not None]
        
        if len(valid_avgs) >= 2:
            if valid_avgs[-1] > valid_avgs[0] + 3:
                direction = "↗ Improving"
                change = round(((valid_avgs[-1] - valid_avgs[0]) / valid_avgs[0] * 100), 1)
            elif valid_avgs[-1] < valid_avgs[0] - 3:
                direction = "↘ Declining"
                change = round(((valid_avgs[-1] - valid_avgs[0]) / valid_avgs[0] * 100), 1)
            else:
                direction = "→ Stable"
                change = round(((valid_avgs[-1] - valid_avgs[0]) / valid_avgs[0] * 100), 1)
        else:
            direction = "- Insufficient data"
            change = 0
        
        return {
            "window_averages": window_avgs,
            "direction": direction,
            "change_percentage": change
        }
    
    # ==================== Main Metrics Aggregation ====================
    
    async def get_all_metrics(
        self,
        student_id: str,
        academic_term_id: str,
        current_user,
        session: AsyncSession
    ) -> Dict:
        """
        Get all 5 performance metrics for a student
        
        Returns:
            Dict containing all calculated metrics
        """
        try:
            # Fetch all necessary data
            # Get student
            from models.student import Student
            student_result = await session.execute(
                select(Student).where(Student.id == student_id)
            )
            student = student_result.scalar_one_or_none()
            
            if not student:
                return {"error": "Student not found"}
            
            course_module_assignment_ids = select(CourseModuleItem.assignment_id).join(
                CourseModule, CourseModule.id == CourseModuleItem.module_id
            ).where(
                CourseModuleItem.school_id == student.school_id,
                CourseModuleItem.assignment_id.is_not(None),
                CourseModule.school_id == student.school_id,
                CourseModule.class_id == student.class_id,
                CourseModule.academic_term_id == academic_term_id,
                CourseModule.is_published == True,
            )

            # Get graded submissions
            submissions_result = await session.execute(
                select(Submission)
                .join(Assignment, Submission.assignment_id == Assignment.id)
                .where(
                    (Submission.student_id == student_id) &
                    (Submission.status == SubmissionStatus.GRADED) &
                    (Assignment.academic_term_id == academic_term_id) &
                    Assignment.id.in_(course_module_assignment_ids)
                )
            )
            submissions = submissions_result.scalars().all()
            
            # Get all assignments for the student's class this term
            assignments_result = await session.execute(
                select(Assignment)
                .where(
                    (Assignment.class_id == student.class_id) &
                    (Assignment.academic_term_id == academic_term_id) &
                    (Assignment.status == "published") &
                    Assignment.id.in_(course_module_assignment_ids)
                )
            )
            assignments = assignments_result.scalars().all()
            
            # Get grades for the term
            grades_result = await session.execute(
                select(Grade)
                .where(
                    (Grade.student_id == student_id) &
                    (Grade.academic_term_id == academic_term_id)
                )
            )
            grades = grades_result.scalars().all()
            
            # Get term start date
            from models.school import AcademicTerm
            term_result = await session.execute(
                select(AcademicTerm).where(AcademicTerm.id == academic_term_id)
            )
            term = term_result.scalar_one_or_none()
            
            # Ensure term_start is a datetime object
            if term and term.start_date:
                term_start = ensure_datetime(term.start_date)
            else:
                term_start = None
            
            if term_start is None:
                term_start = datetime.utcnow()

            # Resolve this student's grading scale once (school-configured
            # scheme matching their class level, or the built-in GES fallback).
            classroom = await session.get(Class, student.class_id) if student.class_id else None
            schemes = await grading_service.get_school_schemes(session, student.school_id)
            class_level = classroom.level if classroom else None
            overall_scale = grading_service.match_scale(schemes, class_level)

            # Calculate all metrics
            api = await self.calculate_api(submissions, scale=overall_scale)
            subject_perf = await self.calculate_subject_performance(grades, session, schemes=schemes, class_level=class_level)
            completion = await self.calculate_completion_metrics(submissions, assignments)
            assessment_types = await self.calculate_assessment_type_breakdown(grades, scale=overall_scale)
            trend = await self.calculate_progress_trend(submissions, term_start)
            
            return {
                "assignment_performance_index": api,
                "subject_performance": subject_perf,
                "completion_and_punctuality": completion,
                "assessment_type_breakdown": assessment_types,
                "progress_trend": trend,
                "summary": {
                    "total_assignments": len(assignments),
                    "submitted": completion["submitted_count"],
                    "outstanding": completion["outstanding"],
                    "term_id": str(academic_term_id),
                    "as_of": datetime.utcnow().isoformat()
                }
            }
        
        except Exception as e:
            logger.error(f"Error calculating metrics: {str(e)}")
            return {"error": f"Failed to calculate metrics: {str(e)}"}
