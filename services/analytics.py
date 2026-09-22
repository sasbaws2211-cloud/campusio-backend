"""Analytics Service - Computing student performance insights.

Rewritten from scratch — the original version of this file referenced
Grade.grade and Grade.subject, neither of which exist on models.grade.Grade
(the real columns are score/max_score/weight and subject_id), used int-typed
IDs against a schema where every id is a UUID string, and hardcoded every
trend to "stable". None of it had ever actually run successfully against
real data. This version uses the real schema, computes GPA the same way the
rest of the app does (GES letter-grade bands via utils/grade_scale.py,
weighted by Grade.weight), computes trends by comparing against the
student's/class's own previous-term snapshot, and folds in fee delinquency
and discipline incidents as risk signals alongside GPA/attendance/assignment
completion.
"""
from sqlmodel import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime
from typing import Optional, List, Dict, Any
from models.analytics import AnalyticsSnapshot, RiskLevel, ClassPerformanceSummary
from models.grade import Grade
from models.attendance import Attendance, AttendanceStatus
from models.assignment import Submission, Assignment
from models.classroom import Class, Subject
from models.student import Student
from models.school import AcademicTerm
from models.fee import Fee, PaymentStatus
from models.discipline import IncidentAction, IncidentReport, ActionApprovalStatus
from utils.grade_scale import get_letter_grade
from services import grading_service
from services.fee_reminder_service import is_fee_overdue
from services.report_card_pdf_service import compute_overall_ges_score, compute_subject_ges_totals
import json
import logging

logger = logging.getLogger(__name__)


async def _weighted_gpa(session: AsyncSession, student_id: str, school_id: str, academic_term_id: str, scale: list = None) -> Optional[float]:
    """GES-split (SBA/exam 50/50), weighted average across every Grade row
    in the term, converted to the school's grade band (1 = best) — computed
    via compute_overall_ges_score, the same function report cards use, so
    this GPA (and the risk-alert threshold it feeds) agrees with what a
    parent sees printed on the report card for the same term.
    scale: the student's class-level grading scale (services/grading_service.py),
    defaults to the built-in GES scale when not supplied."""
    grades = (await session.execute(
        select(Grade).where(
            Grade.student_id == student_id, Grade.academic_term_id == academic_term_id, Grade.school_id == school_id,
        )
    )).scalars().all()
    if not grades:
        return None
    _, percentage = compute_overall_ges_score(grades)
    return float(get_letter_grade(percentage, scale=scale)["grade"])


async def _previous_term(session: AsyncSession, school_id: str, academic_term_id: str) -> Optional[str]:
    """The chronologically previous AcademicTerm for this school, by
    start_date — terms' start_date is a lexicographically-sortable
    'YYYY-MM-DD' string, same convention used throughout this codebase."""
    current = await session.get(AcademicTerm, academic_term_id)
    if not current:
        return None
    prev = (await session.execute(
        select(AcademicTerm)
        .where(AcademicTerm.school_id == school_id, AcademicTerm.start_date < current.start_date)
        .order_by(AcademicTerm.start_date.desc())
        .limit(1)
    )).scalar_one_or_none()
    return prev.id if prev else None


def _trend(current: Optional[float], previous: Optional[float], higher_is_better: bool = True) -> str:
    if current is None or previous is None:
        return "stable"
    delta = current - previous
    if not higher_is_better:
        delta = -delta
    if delta > 0.5:
        return "improving"
    if delta < -0.5:
        return "declining"
    return "stable"


async def _fee_delinquent(session: AsyncSession, student_id: str, school_id: str) -> bool:
    """Computed fresh from due-date math (services.fee_reminder_service.is_fee_overdue)
    rather than trusted from Fee.status == OVERDUE — that status is only
    ever set by two narrow, easy-to-miss triggers (an opt-in late-fee sweep,
    and a manual admin endpoint nothing schedules) and can silently drift
    out of sync with whether a fee is actually overdue today."""
    fees = (await session.execute(
        select(Fee).where(
            Fee.student_id == student_id, Fee.school_id == school_id,
            Fee.status.in_([PaymentStatus.PENDING, PaymentStatus.PARTIAL, PaymentStatus.OVERDUE]),
        )
    )).scalars().all()
    for f in fees:
        if await is_fee_overdue(session, f):
            return True
    return False


async def _discipline_incident_count(session: AsyncSession, student_id: str, school_id: str, academic_term_id: str) -> int:
    # Filters directly on IncidentAction.student_id (the student the action
    # was actually taken against), matching routers/discipline.py's
    # get_student_demerit_tally — the previous version joined IncidentStudent
    # on incident_id only, so an approved action taken against one student
    # on a multi-student incident report was also counted for every other
    # student merely linked to that same report, silently inflating their
    # discipline-incidents risk factor for something they weren't
    # disciplined for.
    result = await session.execute(
        select(func.count(IncidentAction.id))
        .join(IncidentReport, IncidentReport.id == IncidentAction.incident_id)
        .where(
            IncidentAction.student_id == student_id,
            IncidentAction.school_id == school_id,
            IncidentAction.approval_status == ActionApprovalStatus.APPROVED,
            IncidentReport.academic_term_id == academic_term_id,
        )
    )
    return result.scalar() or 0


class AnalyticsService:
    """Service for computing student and class performance insights"""

    @staticmethod
    async def calculate_student_snapshot(
        session: AsyncSession,
        student_id: str,
        school_id: str,
        academic_term_id: str
    ) -> AnalyticsSnapshot:
        student = await session.get(Student, student_id)
        term = await session.get(AcademicTerm, academic_term_id)
        if not student or not term:
            raise ValueError("Student or term not found")

        classroom = await session.get(Class, student.class_id) if student.class_id else None
        schemes = await grading_service.get_school_schemes(session, school_id)
        scale = grading_service.match_scale(schemes, classroom.level if classroom else None)

        overall_gpa = await _weighted_gpa(session, student_id, school_id, academic_term_id, scale=scale)

        total_days = (await session.execute(
            select(func.count(Attendance.id)).where(
                Attendance.student_id == student_id, Attendance.school_id == school_id, Attendance.academic_term_id == academic_term_id,
            )
        )).scalar() or 0
        present_days = (await session.execute(
            select(func.count(Attendance.id)).where(
                Attendance.student_id == student_id, Attendance.school_id == school_id, Attendance.academic_term_id == academic_term_id,
                Attendance.status.in_([AttendanceStatus.PRESENT, AttendanceStatus.LATE]),
            )
        )).scalar() or 0
        attendance_rate = (present_days / total_days * 100) if total_days > 0 else None

        total_submissions = (await session.execute(
            select(func.count(Submission.id))
            .join(Assignment, Assignment.id == Submission.assignment_id)
            .where(Submission.student_id == student_id, Submission.school_id == school_id, Assignment.academic_term_id == academic_term_id)
        )).scalar() or 0
        submitted = (await session.execute(
            select(func.count(Submission.id))
            .join(Assignment, Assignment.id == Submission.assignment_id)
            .where(
                Submission.student_id == student_id, Submission.school_id == school_id, Assignment.academic_term_id == academic_term_id,
                Submission.status != "not_submitted",
            )
        )).scalar() or 0
        completion_rate = (submitted / total_submissions * 100) if total_submissions > 0 else None

        # GES-split, weighted total via compute_subject_ges_totals — the same
        # function report cards use — rather than a raw avg(score/max_score),
        # which ignores the SBA/exam split and Grade.weight and can disagree
        # with (even contradict) what the student's actual report card shows.
        student_grades = (await session.execute(
            select(Grade).where(Grade.student_id == student_id, Grade.academic_term_id == academic_term_id, Grade.school_id == school_id)
        )).scalars().all()
        subject_totals = compute_subject_ges_totals(student_grades)

        best_subject = worst_subject = None
        best_grade = worst_grade = None
        if subject_totals:
            subjects_map = {s.id: s.name for s in (await session.execute(select(Subject).where(Subject.id.in_(list(subject_totals.keys()))))).scalars().all()}
            ordered = sorted(subject_totals.items(), key=lambda kv: kv[1]["total_score"])
            worst_subject_id, worst_totals = ordered[0]
            best_subject_id, best_totals = ordered[-1]
            worst_subject = subjects_map.get(worst_subject_id, "Unknown")
            worst_grade = int(get_letter_grade(worst_totals["total_score"], scale=grading_service.match_scale(schemes, classroom.level if classroom else None, worst_subject_id))["grade"])
            best_subject = subjects_map.get(best_subject_id, "Unknown")
            best_grade = int(get_letter_grade(best_totals["total_score"], scale=grading_service.match_scale(schemes, classroom.level if classroom else None, best_subject_id))["grade"])

        prev_term_id = await _previous_term(session, school_id, academic_term_id)
        prev_gpa = prev_attendance = None
        if prev_term_id:
            prev_snapshot = (await session.execute(
                select(AnalyticsSnapshot).where(
                    AnalyticsSnapshot.student_id == student_id, AnalyticsSnapshot.academic_term_id == prev_term_id, AnalyticsSnapshot.school_id == school_id,
                ).order_by(AnalyticsSnapshot.captured_at.desc()).limit(1)
            )).scalar_one_or_none()
            if prev_snapshot:
                prev_gpa, prev_attendance = prev_snapshot.overall_gpa, prev_snapshot.attendance_rate
            else:
                prev_gpa = await _weighted_gpa(session, student_id, school_id, prev_term_id, scale=scale)

        # GPA band is 1 (best) to 9 (worst) — lower is better, so invert for the trend helper.
        gpa_trend = _trend(overall_gpa, prev_gpa, higher_is_better=False)
        attendance_trend = _trend(attendance_rate, prev_attendance, higher_is_better=True)

        risk_factors = []
        risk_level = RiskLevel.LOW

        if overall_gpa is not None:
            if overall_gpa >= 7:
                risk_factors.append("low_gpa")
                risk_level = RiskLevel.HIGH
            elif overall_gpa >= 6:
                risk_level = RiskLevel.MODERATE

        if attendance_rate is not None and attendance_rate < 70:
            risk_factors.append("poor_attendance")
            risk_level = RiskLevel.HIGH if risk_level == RiskLevel.MODERATE else (RiskLevel.MODERATE if risk_level == RiskLevel.LOW else risk_level)

        if completion_rate is not None and completion_rate < 50:
            risk_factors.append("low_assignment_completion")
            if risk_level == RiskLevel.LOW:
                risk_level = RiskLevel.MODERATE

        if await _fee_delinquent(session, student_id, school_id):
            risk_factors.append("fee_delinquent")
            if risk_level == RiskLevel.LOW:
                risk_level = RiskLevel.MODERATE

        incident_count = await _discipline_incident_count(session, student_id, school_id, academic_term_id)
        if incident_count > 0:
            risk_factors.append("discipline_incidents")
            if risk_level == RiskLevel.LOW:
                risk_level = RiskLevel.MODERATE
            elif incident_count >= 2 and risk_level == RiskLevel.MODERATE:
                risk_level = RiskLevel.HIGH

        if len(risk_factors) >= 3:
            risk_level = RiskLevel.CRITICAL

        snapshot = AnalyticsSnapshot(
            school_id=school_id, student_id=student_id, academic_term_id=academic_term_id,
            overall_gpa=overall_gpa, attendance_rate=attendance_rate, assignment_completion_rate=completion_rate,
            best_subject=best_subject, best_subject_grade=best_grade, worst_subject=worst_subject, worst_subject_grade=worst_grade,
            gpa_trend=gpa_trend, attendance_trend=attendance_trend,
            risk_level=risk_level, risk_factors=json.dumps(risk_factors) if risk_factors else None,
        )
        session.add(snapshot)
        return snapshot

    @staticmethod
    async def calculate_class_summary(
        session: AsyncSession,
        class_id: str,
        school_id: str,
        academic_term_id: str
    ) -> ClassPerformanceSummary:
        student_ids = (await session.execute(
            select(Student.id).where(Student.class_id == class_id, Student.school_id == school_id, Student.status == "active")
        )).scalars().all()
        total_students = len(student_ids)

        if total_students == 0:
            return ClassPerformanceSummary(school_id=school_id, class_id=class_id, academic_term_id=academic_term_id, total_students=0)

        classroom = await session.get(Class, class_id)
        schemes = await grading_service.get_school_schemes(session, school_id)
        scale = grading_service.match_scale(schemes, classroom.level if classroom else None)

        per_student_gpa = {}
        at_risk_count = passing_count = failing_count = 0
        for sid in student_ids:
            gpa = await _weighted_gpa(session, sid, school_id, academic_term_id, scale=scale)
            per_student_gpa[sid] = gpa
            if gpa is not None:
                if gpa >= 7:
                    at_risk_count += 1
                    failing_count += 1
                else:
                    passing_count += 1

        valid_gpas = [g for g in per_student_gpa.values() if g is not None]
        avg_gpa = sum(valid_gpas) / len(valid_gpas) if valid_gpas else None

        present_count = (await session.execute(
            select(func.count(Attendance.id)).where(
                Attendance.student_id.in_(student_ids), Attendance.academic_term_id == academic_term_id, Attendance.school_id == school_id,
                Attendance.status.in_([AttendanceStatus.PRESENT, AttendanceStatus.LATE]),
            )
        )).scalar() or 0
        total_count = (await session.execute(
            select(func.count(Attendance.id)).where(
                Attendance.student_id.in_(student_ids), Attendance.academic_term_id == academic_term_id, Attendance.school_id == school_id,
            )
        )).scalar() or 0
        avg_attendance = (present_count / total_count * 100) if total_count > 0 else None

        # Same GES-split, weighted total via compute_subject_ges_totals as the
        # per-student best/worst-subject computation above — computed per
        # student, then averaged per subject across the class, rather than a
        # raw avg(score/max_score) that ignores the SBA/exam split and weight.
        class_grades = (await session.execute(
            select(Grade).where(Grade.student_id.in_(student_ids), Grade.academic_term_id == academic_term_id, Grade.school_id == school_id)
        )).scalars().all()
        grades_by_student: dict = {}
        for g in class_grades:
            grades_by_student.setdefault(g.student_id, []).append(g)
        subject_totals_sum: dict = {}
        for student_grades_list in grades_by_student.values():
            for subject_id, totals in compute_subject_ges_totals(student_grades_list).items():
                acc = subject_totals_sum.setdefault(subject_id, [0.0, 0])
                acc[0] += totals["total_score"]
                acc[1] += 1
        subject_avg = {sid: acc[0] / acc[1] for sid, acc in subject_totals_sum.items() if acc[1] > 0}

        strongest_subject = weakest_subject = None
        if subject_avg:
            subjects_map = {s.id: s.name for s in (await session.execute(select(Subject).where(Subject.id.in_(list(subject_avg.keys()))))).scalars().all()}
            ordered = sorted(subject_avg.items(), key=lambda kv: kv[1])
            weakest_subject = subjects_map.get(ordered[0][0], "Unknown")
            strongest_subject = subjects_map.get(ordered[-1][0], "Unknown")

        prev_term_id = await _previous_term(session, school_id, academic_term_id)
        prev_avg_gpa = prev_at_risk = None
        if prev_term_id:
            prev_summary = (await session.execute(
                select(ClassPerformanceSummary).where(
                    ClassPerformanceSummary.class_id == class_id, ClassPerformanceSummary.academic_term_id == prev_term_id, ClassPerformanceSummary.school_id == school_id,
                ).order_by(ClassPerformanceSummary.last_updated.desc()).limit(1)
            )).scalar_one_or_none()
            if prev_summary:
                prev_avg_gpa, prev_at_risk = prev_summary.average_gpa, prev_summary.students_at_risk

        gpa_trend = _trend(avg_gpa, prev_avg_gpa, higher_is_better=False)
        if prev_at_risk is None:
            at_risk_trend = "stable"
        elif at_risk_count > prev_at_risk:
            at_risk_trend = "more_at_risk"
        elif at_risk_count < prev_at_risk:
            at_risk_trend = "fewer_at_risk"
        else:
            at_risk_trend = "stable"

        summary = ClassPerformanceSummary(
            school_id=school_id, class_id=class_id, academic_term_id=academic_term_id, total_students=total_students,
            average_gpa=avg_gpa, average_attendance_rate=avg_attendance,
            students_at_risk=at_risk_count, students_passing=passing_count, students_failing=failing_count,
            strongest_subject=strongest_subject, weakest_subject=weakest_subject,
            gpa_trend=gpa_trend, at_risk_trend=at_risk_trend,
        )
        session.add(summary)
        return summary

    @staticmethod
    async def get_at_risk_students(
        session: AsyncSession,
        school_id: str,
        academic_term_id: Optional[str] = None,
        limit: int = 20
    ) -> List[Dict[str, Any]]:
        query = select(
            AnalyticsSnapshot.student_id, AnalyticsSnapshot.overall_gpa, AnalyticsSnapshot.attendance_rate,
            AnalyticsSnapshot.risk_level, AnalyticsSnapshot.risk_factors, AnalyticsSnapshot.captured_at,
            Student.first_name, Student.last_name, Student.student_id,
        ).join(Student, Student.id == AnalyticsSnapshot.student_id).where(
            AnalyticsSnapshot.school_id == school_id,
            AnalyticsSnapshot.risk_level.in_([RiskLevel.HIGH.value, RiskLevel.CRITICAL.value]),
        )
        if academic_term_id:
            query = query.where(AnalyticsSnapshot.academic_term_id == academic_term_id)
        query = query.order_by(AnalyticsSnapshot.captured_at.desc())

        # Recalculating inserts a new historical row rather than overwriting
        # (AnalyticsSnapshot is a periodic snapshot log, see its docstring) —
        # keep only the most recent row per student for this "current state" view.
        results = await session.execute(query)
        seen = set()
        rows = []
        for r in results.fetchall():
            if r[0] in seen:
                continue
            seen.add(r[0])
            rows.append({
                "student_id": r[0], "gpa": r[1], "attendance_rate": r[2], "risk_level": r[3],
                "risk_factors": json.loads(r[4]) if r[4] else [], "name": f"{r[6]} {r[7]}", "student_code": r[8],
            })
        rows.sort(key=lambda x: {"critical": 0, "high": 1}.get(x["risk_level"], 2))
        return rows[:limit]

    @staticmethod
    async def get_class_performance_comparison(
        session: AsyncSession,
        school_id: str,
        academic_term_id: str
    ) -> List[Dict[str, Any]]:
        summaries = (await session.execute(
            select(ClassPerformanceSummary).where(
                ClassPerformanceSummary.school_id == school_id, ClassPerformanceSummary.academic_term_id == academic_term_id,
            ).order_by(ClassPerformanceSummary.last_updated.desc())
        )).scalars().all()

        # Same "keep only the latest row per class" dedupe as get_at_risk_students.
        seen = set()
        latest_summaries = []
        for s in summaries:
            if s.class_id in seen:
                continue
            seen.add(s.class_id)
            latest_summaries.append(s)
        latest_summaries.sort(key=lambda s: (s.average_gpa if s.average_gpa is not None else 999))

        class_summaries = []
        for summary in latest_summaries:
            class_row = await session.get(Class, summary.class_id)
            class_summaries.append({
                "class_id": summary.class_id, "class_name": class_row.name if class_row else "Unknown",
                "total_students": summary.total_students, "average_gpa": summary.average_gpa,
                "average_attendance": summary.average_attendance_rate, "students_at_risk": summary.students_at_risk,
                "students_passing": summary.students_passing, "students_failing": summary.students_failing,
                "strongest_subject": summary.strongest_subject, "weakest_subject": summary.weakest_subject,
                "gpa_trend": summary.gpa_trend, "at_risk_trend": summary.at_risk_trend,
            })
        return class_summaries
