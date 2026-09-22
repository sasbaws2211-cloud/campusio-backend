"""Teacher effectiveness composite score — an objective score combining
grade improvement, attendance rate, and PD (professional development)
completion for the classes/subjects a teacher actually teaches. Deliberately
excludes any subjective performance rating (StaffPerformanceReview.overall_rating)
— the user explicitly scoped this out.

Data joins mirror routers/strategic_reports.py::teacher_workload (the
existing template for "which classes/subjects does this Timetable teacher_id
teach"), and the PD-hours query mirrors routers/executive_reports.py::pd_roi
minus its subjective-rating join.

Composite scoring (0-100, higher is better):
  - 40% grade improvement:  (this term's avg % across the teacher's
    class/subject pairs) minus (the previous term's avg % for the same
    pairs), normalized onto a 0-100 scale by treating a ±20-percentage-point
    swing as the extremes (no data / no prior term = a neutral 50).
  - 30% attendance rate:    present / total Attendance rows across the
    teacher's classes for the term, used directly as a 0-100 score.
  - 30% PD completion:      total StaffTraining hours completed by the
    teacher within the lookback window, capped at 20 hours = 100%.
These weights are a judgment call (documented here, not hidden) — grade
improvement is weighted highest because it is the most direct signal of
classroom impact; attendance and PD completion are supporting signals.
"""
from collections import defaultdict
from datetime import date, timedelta
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select, func

from models.attendance import Attendance, AttendanceStatus
from models.grade import Grade
from models.hr_development import StaffTraining
from models.staff import Staff
from models.timetable import Timetable
from services.analytics import _previous_term
from services.report_card_pdf_service import compute_overall_ges_score

GRADE_WEIGHT = 0.40
ATTENDANCE_WEIGHT = 0.30
PD_WEIGHT = 0.30

GRADE_IMPROVEMENT_SWING_PCT = 20.0  # a ±20pp swing is treated as the 0/100 extremes
PD_HOURS_FOR_FULL_SCORE = 20.0  # 20 hours of completed training within the window = 100%
PD_LOOKBACK_MONTHS = 12


def _normalize_improvement(improvement: Optional[float]) -> float:
    """Maps a percentage-point improvement onto 0-100, clipped at a ±20pp
    swing. No prior-term data to compare against returns a neutral 50 rather
    than penalizing the teacher for a data gap."""
    if improvement is None:
        return 50.0
    clipped = max(-GRADE_IMPROVEMENT_SWING_PCT, min(GRADE_IMPROVEMENT_SWING_PCT, improvement))
    return (clipped + GRADE_IMPROVEMENT_SWING_PCT) / (2 * GRADE_IMPROVEMENT_SWING_PCT) * 100


async def compute_teacher_effectiveness(
    session: AsyncSession, school_id: str, academic_term_id: str, staff_id: Optional[str] = None,
) -> dict:
    timetable_query = select(Timetable).where(Timetable.school_id == school_id, Timetable.academic_term_id == academic_term_id)
    if staff_id:
        timetable_query = timetable_query.where(Timetable.teacher_id == staff_id)
    timetable_rows = (await session.execute(timetable_query)).scalars().all()
    if not timetable_rows:
        return {"academic_term_id": academic_term_id, "teachers": []}

    teacher_ids = {t.teacher_id for t in timetable_rows}
    staff_map = {s.id: s for s in (await session.execute(select(Staff).where(Staff.id.in_(teacher_ids)))).scalars().all()}

    per_teacher_pairs = defaultdict(set)  # teacher_id -> {(class_id, subject_id), ...}
    per_teacher_classes = defaultdict(set)  # teacher_id -> {class_id, ...}
    for t in timetable_rows:
        per_teacher_pairs[t.teacher_id].add((t.class_id, t.subject_id))
        per_teacher_classes[t.teacher_id].add(t.class_id)

    prior_term_id = await _previous_term(session, school_id, academic_term_id)

    since = (date.today() - timedelta(days=PD_LOOKBACK_MONTHS * 30)).isoformat()
    trainings = (await session.execute(
        select(StaffTraining).where(
            StaffTraining.school_id == school_id, StaffTraining.staff_id.in_(teacher_ids),
            StaffTraining.completion_date.is_not(None), StaffTraining.completion_date >= since,
        )
    )).scalars().all()
    pd_hours_by_staff = defaultdict(float)
    for tr in trainings:
        pd_hours_by_staff[tr.staff_id] += tr.hours or 0

    teachers = []
    for teacher_id, pairs in per_teacher_pairs.items():
        staff = staff_map.get(teacher_id)
        class_ids = per_teacher_classes[teacher_id]

        # ── Grade improvement component ── GES-split, weighted average via
        # compute_overall_ges_score (the same function report cards use),
        # so this agrees with what report cards show for the same rows —
        # previously a plain unweighted mean of raw (score, max_score).
        current_grade_rows = []
        prior_grade_rows = []
        for class_id, subject_id in pairs:
            current_grade_rows.extend((await session.execute(
                select(Grade).where(
                    Grade.school_id == school_id, Grade.academic_term_id == academic_term_id,
                    Grade.class_id == class_id, Grade.subject_id == subject_id,
                )
            )).scalars().all())
            if prior_term_id:
                prior_grade_rows.extend((await session.execute(
                    select(Grade).where(
                        Grade.school_id == school_id, Grade.academic_term_id == prior_term_id,
                        Grade.class_id == class_id, Grade.subject_id == subject_id,
                    )
                )).scalars().all())

        current_avg = compute_overall_ges_score(current_grade_rows)[1] if current_grade_rows else None
        prior_avg = compute_overall_ges_score(prior_grade_rows)[1] if (prior_term_id and prior_grade_rows) else None
        improvement = (current_avg - prior_avg) if (current_avg is not None and prior_avg is not None) else None
        grade_component = _normalize_improvement(improvement)

        # ── Attendance component ── PRESENT+LATE counts as present, matching
        # every other attendance-rate consumer in this codebase (report
        # cards, portals, strategic/risk reports) — a late arrival is still
        # physical presence.
        present_count = (await session.execute(
            select(func.count(Attendance.id)).where(
                Attendance.school_id == school_id, Attendance.academic_term_id == academic_term_id,
                Attendance.class_id.in_(class_ids), Attendance.status.in_([AttendanceStatus.PRESENT, AttendanceStatus.LATE]),
            )
        )).scalar() or 0
        total_count = (await session.execute(
            select(func.count(Attendance.id)).where(
                Attendance.school_id == school_id, Attendance.academic_term_id == academic_term_id,
                Attendance.class_id.in_(class_ids),
            )
        )).scalar() or 0
        attendance_component = (present_count / total_count * 100) if total_count > 0 else 50.0

        # ── PD completion component ──
        pd_hours = pd_hours_by_staff.get(teacher_id, 0.0)
        pd_component = min(pd_hours / PD_HOURS_FOR_FULL_SCORE, 1.0) * 100

        composite = (
            grade_component * GRADE_WEIGHT
            + attendance_component * ATTENDANCE_WEIGHT
            + pd_component * PD_WEIGHT
        )

        teachers.append({
            "staff_id": teacher_id,
            "teacher_name": f"{staff.first_name} {staff.last_name}" if staff else "Unknown",
            "composite_score": round(composite, 1),
            "components": {
                "grade_improvement": {
                    "score": round(grade_component, 1),
                    "current_term_avg_pct": round(current_avg, 1) if current_avg is not None else None,
                    "prior_term_avg_pct": round(prior_avg, 1) if prior_avg is not None else None,
                    "improvement_pct_points": round(improvement, 1) if improvement is not None else None,
                },
                "attendance_rate": {
                    "score": round(attendance_component, 1),
                    "present": present_count,
                    "total": total_count,
                },
                "pd_completion": {
                    "score": round(pd_component, 1),
                    "hours_completed": pd_hours,
                },
            },
            "classes_taught": len(class_ids),
            "subjects_taught": len({s for _, s in pairs}),
        })

    teachers.sort(key=lambda t: -t["composite_score"])
    return {"academic_term_id": academic_term_id, "prior_term_id": prior_term_id, "teachers": teachers}
