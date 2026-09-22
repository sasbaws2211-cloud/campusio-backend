"""Bridges a graded Submission (routers/teacher/assignments.py,
routers/public_api.py's LMS sync, routers/student_portal.py's quiz
auto-grade) into the Grade/report-card pipeline.

Before this, a score entered while grading an assignment submission never
affected a student's report card at all unless a teacher separately
re-typed the same number into the general gradebook (models/grade.py) by
hand — the two systems had no link, despite Assignment.assignment_type
being explicitly documented as "GES-aligned ... CA components"
(models/assignment.py). Mirrors the shape of
services/exam_result_aggregation_service.py, the equivalent bridge already
in place for formal exam components -> Grade.

Each graded Submission maps to its OWN Grade row (not summed together the
way exam components are) — a term's several classwork/quiz instances are
each their own row, and services/report_card_pdf_service.py::
compute_subject_ges_totals already correctly sums every non-exam Grade row
for a subject/term via a weighted ratio-of-sums, so multiple rows of the
same assessment_type combine correctly with no extra logic needed here.
"""
from datetime import datetime
from typing import Optional

from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.assignment import Assignment, AssignmentType, Submission
from models.grade import AssessmentType, Grade
from services.exam_result_aggregation_service import _flag_report_card_for_review

# Assignment's CA-component types don't share vocabulary with Grade's
# assessment types (models/assignment.py:19 "classwork" vs models/grade.py:11
# "class_work") even where the concept is identical -- this is the
# translation table. WORKSHEET has no dedicated Grade bucket; "supplementary
# work" (its own docstring) is closest in kind to classwork.
ASSIGNMENT_TYPE_TO_ASSESSMENT_TYPE = {
    AssignmentType.CLASSWORK: AssessmentType.CLASS_WORK,
    AssignmentType.HOMEWORK: AssessmentType.HOMEWORK,
    AssignmentType.QUIZ: AssessmentType.QUIZ,
    AssignmentType.PROJECT: AssessmentType.PROJECT,
    AssignmentType.WORKSHEET: AssessmentType.CLASS_WORK,
}

# Stamped onto Grade.remarks for a row this bridge created, keyed to the
# specific assignment so a re-grade finds and updates its own row instead
# of inserting a second one for the same piece of work. A teacher's own
# directly-entered classwork/quiz score (typed straight into the general
# gradebook, never touching the assignment/submission system at all) has no
# such tag and is a completely separate row -- both legitimately count
# toward the same CA bucket, so there's no collision to resolve there.
def _sync_tag(assignment_id: str) -> str:
    return f"Auto-synced from assignment {assignment_id}"


async def sync_submission_to_grade(
    session: AsyncSession,
    submission: Submission,
    assignment: Assignment,
    recorded_by: str,
) -> Optional[Grade]:
    """Create or update the Grade row that mirrors one graded Submission.
    No-ops (returns None) if the submission has no score yet -- ungraded
    work shouldn't contribute a 0 to continuous assessment."""
    if submission.score is None:
        return None

    assessment_type = ASSIGNMENT_TYPE_TO_ASSESSMENT_TYPE.get(assignment.assignment_type, AssessmentType.CLASS_WORK)
    tag = _sync_tag(assignment.id)
    max_score = submission.max_score or assignment.points_possible or 100.0

    existing = (await session.execute(
        select(Grade).where(
            Grade.student_id == submission.student_id,
            Grade.subject_id == assignment.subject_id,
            Grade.class_id == assignment.class_id,
            Grade.academic_term_id == assignment.academic_term_id,
            Grade.remarks == tag,
        )
    )).scalar_one_or_none()

    if existing:
        existing.score = submission.score
        existing.max_score = max_score
        existing.assessment_type = assessment_type
        existing.updated_at = datetime.utcnow()
        session.add(existing)
        grade = existing
    else:
        grade = Grade(
            school_id=submission.school_id,
            student_id=submission.student_id,
            class_id=assignment.class_id,
            subject_id=assignment.subject_id,
            academic_term_id=assignment.academic_term_id,
            assessment_type=assessment_type,
            score=submission.score,
            max_score=max_score,
            weight=1.0,
            remarks=tag,
            recorded_by=recorded_by,
        )
        session.add(grade)

    await session.flush()
    # Same reasoning as the exam-aggregation bridge: if a report card was
    # already approved and this score just changed, it's no longer covering
    # what's actually being shown, so it needs a fresh sign-off.
    await _flag_report_card_for_review(session, submission.student_id, assignment.academic_term_id)
    return grade


async def remove_submission_grade(session: AsyncSession, submission: Submission, assignment: Assignment) -> bool:
    """Removes this submission's auto-synced Grade row, if one exists --
    for the case a submission is un-graded/reset back to not-submitted.
    Returns whether a row was actually removed."""
    tag = _sync_tag(assignment.id)
    existing = (await session.execute(
        select(Grade).where(
            Grade.student_id == submission.student_id,
            Grade.subject_id == assignment.subject_id,
            Grade.class_id == assignment.class_id,
            Grade.academic_term_id == assignment.academic_term_id,
            Grade.remarks == tag,
        )
    )).scalar_one_or_none()
    if not existing:
        return False
    await session.delete(existing)
    await session.flush()
    await _flag_report_card_for_review(session, submission.student_id, assignment.academic_term_id)
    return True
