"""Bridges formal ExamComponentMark scores (routers/exam_marks.py) into the
Grade/report-card pipeline once an ExamSession's results are published.

Before this, a component mark never affected a student's report card/GPA at
all unless a teacher separately re-typed the same score into the general
gradebook (models/grade.py) by hand — the two systems had no link. The only
callers are routers/exams.py::publish_results (manual) and
services/scheduler.py::run_exam_result_auto_publish (results_release_date
sweep); both flip ExamSession.results_published, which is the trigger.
"""
from datetime import datetime
from typing import Optional

from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.exam import ExamSchedule, ExamSession
from models.exam_marks import ExamComponent, ExamComponentMark
from models.grade import AssessmentType, Grade, ReportCard, ReportCardStatus

# Tag prefix stamped onto Grade.remarks for a row this function created —
# lets a re-publish (e.g. after a marking correction) find and update its
# own row instead of inserting a second END_OF_TERM grade, which would
# double-count in services/report_card_pdf_service.py::compute_subject_ges_totals's
# weighted sum over ALL of a student's end_of_term Grade rows for the term.
AUTO_AGGREGATE_TAG = "Auto-aggregated from exam components"


async def aggregate_exam_session_to_grades(
    session: AsyncSession, exam_session: ExamSession, actor_id: Optional[str] = None
) -> dict:
    """Per class+subject schedule in this session, sums each student's
    (non-annulled) component marks — weighted by component.weight, scaled by
    component.max_marks — into one END_OF_TERM Grade row normalized to 0-100,
    using the exact ratio-of-weighted-sums formula compute_subject_ges_totals
    already uses, so the aggregate is consistent with the rest of the report
    card rather than a different average.

    A student/subject that already carries a *manually* entered END_OF_TERM
    Grade for this term (no auto-aggregate tag) is left untouched and
    reported back as a conflict — never silently overwritten, and never
    summed alongside the auto row (that would double-count the same exam).
    """
    schedules = (await session.execute(
        select(ExamSchedule).where(ExamSchedule.exam_session_id == exam_session.id)
    )).scalars().all()

    created, updated, conflicts = 0, 0, []
    recorded_by = actor_id or "system:exam-result-auto-publish"

    for schedule in schedules:
        components = (await session.execute(
            select(ExamComponent).where(ExamComponent.exam_schedule_id == schedule.id)
        )).scalars().all()
        if not components:
            continue
        component_by_id = {c.id: c for c in components}

        marks = (await session.execute(
            select(ExamComponentMark).where(
                ExamComponentMark.exam_component_id.in_(list(component_by_id.keys())),
                ExamComponentMark.annulled == False,  # noqa: E712
            )
        )).scalars().all()

        by_student: dict = {}
        for mark in marks:
            by_student.setdefault(mark.student_id, []).append(mark)

        for student_id, student_marks in by_student.items():
            weighted_score_sum, weighted_max_sum = 0.0, 0.0
            for mark in student_marks:
                component = component_by_id[mark.exam_component_id]
                if component.max_marks <= 0:
                    continue
                weighted_score_sum += mark.score * component.weight
                weighted_max_sum += component.max_marks * component.weight
            if weighted_max_sum <= 0:
                continue
            total_100 = round((weighted_score_sum / weighted_max_sum) * 100, 1)

            existing_rows = (await session.execute(
                select(Grade).where(
                    Grade.student_id == student_id,
                    Grade.subject_id == schedule.subject_id,
                    Grade.class_id == schedule.class_id,
                    Grade.academic_term_id == exam_session.academic_term_id,
                    Grade.assessment_type == AssessmentType.END_OF_TERM,
                )
            )).scalars().all()

            auto_row = next((g for g in existing_rows if g.remarks and g.remarks.startswith(AUTO_AGGREGATE_TAG)), None)
            manual_row = next((g for g in existing_rows if g is not auto_row), None)

            if manual_row is not None:
                conflicts.append({
                    "student_id": student_id,
                    "subject_id": schedule.subject_id,
                    "class_id": schedule.class_id,
                    "existing_grade_id": manual_row.id,
                })
                continue

            if auto_row is not None:
                auto_row.score = total_100
                auto_row.max_score = 100
                auto_row.updated_at = datetime.utcnow()
                session.add(auto_row)
                updated += 1
            else:
                session.add(Grade(
                    school_id=exam_session.school_id,
                    student_id=student_id,
                    class_id=schedule.class_id,
                    subject_id=schedule.subject_id,
                    academic_term_id=exam_session.academic_term_id,
                    assessment_type=AssessmentType.END_OF_TERM,
                    score=total_100,
                    max_score=100,
                    weight=1.0,
                    remarks=f"{AUTO_AGGREGATE_TAG} (exam session: {exam_session.name})",
                    recorded_by=recorded_by,
                ))
                created += 1

    await session.commit()
    return {"created": created, "updated": updated, "conflicts": conflicts}


async def _flag_report_card_for_review(session: AsyncSession, student_id: str, academic_term_id: str) -> bool:
    """If the student has an APPROVED report card for this term, flip it
    back to DRAFT so a parent stops seeing an approved snapshot built on a
    score that's since changed. Returns whether one was flagged."""
    report_card = (await session.execute(
        select(ReportCard).where(
            ReportCard.student_id == student_id,
            ReportCard.academic_term_id == academic_term_id,
        )
    )).scalar_one_or_none()
    if not report_card or report_card.status != ReportCardStatus.APPROVED.value:
        return False
    report_card.status = ReportCardStatus.DRAFT.value
    report_card.approved_by = None
    report_card.approved_at = None
    session.add(report_card)
    return True


async def reaggregate_and_flag_report_card(
    session: AsyncSession, exam_session: ExamSession, student_id: str, actor_id: Optional[str] = None
) -> dict:
    """Re-run this session's aggregation after something changed one
    student's component mark after the fact — an exam-remark grant
    (routers/exam_remarks.py) or a malpractice-case annulment
    (routers/exam_malpractice.py). Before this, neither of those flows
    touched Grade/ReportCard at all: a parent could keep looking at an
    approved report card built on a score that had since been formally
    revised or annulled, with nothing to tell them (or staff) it was stale.

    Only ever flags the ONE student whose mark actually changed — other
    students' already-approved report cards for the same session are
    untouched, since nothing about their own result changed."""
    aggregation = await aggregate_exam_session_to_grades(session, exam_session, actor_id=actor_id)
    flagged = await _flag_report_card_for_review(session, student_id, exam_session.academic_term_id)
    if flagged:
        await session.commit()
    aggregation["report_card_flagged"] = flagged
    return aggregation


async def revert_exam_session_grades(session: AsyncSession, exam_session: ExamSession) -> dict:
    """Undo aggregate_exam_session_to_grades's effect for this session —
    called when results are unpublished (routers/exams.py::unpublish_results)
    after being found wrong. Before this, unpublishing only hid the marks
    from students/parents again; the Grade rows the earlier publish had
    already created stayed in place and kept counting toward GPA/report
    cards even though the exam was no longer supposed to be visible.

    Removes only the exact auto-aggregated Grade rows THIS session created
    — matched by the same remarks tag aggregate_exam_session_to_grades
    stamps, which embeds the session's own name — never a manually-entered
    Grade (no tag), and in the rare case of two same-named sessions in the
    same term, only rows whose tag matches this session's exact name.
    Flags any already-approved ReportCard for an affected student back to
    DRAFT for the same reason reaggregate_and_flag_report_card does."""
    tag = f"{AUTO_AGGREGATE_TAG} (exam session: {exam_session.name})"
    schedules = (await session.execute(
        select(ExamSchedule).where(ExamSchedule.exam_session_id == exam_session.id)
    )).scalars().all()

    grades_removed, report_cards_flagged = 0, 0
    for schedule in schedules:
        rows = (await session.execute(
            select(Grade).where(
                Grade.class_id == schedule.class_id,
                Grade.subject_id == schedule.subject_id,
                Grade.academic_term_id == exam_session.academic_term_id,
                Grade.assessment_type == AssessmentType.END_OF_TERM,
                Grade.remarks == tag,
            )
        )).scalars().all()
        for grade in rows:
            student_id = grade.student_id
            await session.delete(grade)
            grades_removed += 1
            if await _flag_report_card_for_review(session, student_id, exam_session.academic_term_id):
                report_cards_flagged += 1

    await session.commit()
    return {"grades_removed": grades_removed, "report_cards_flagged": report_cards_flagged}
