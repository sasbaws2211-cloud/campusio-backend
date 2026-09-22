"""Resolves which grading scale applies to a given class level / subject,
supporting multiple school-configured GradingScheme rows (models.grade)
instead of one hardcoded scale for the whole school. Falls back to
utils.grade_scale.GES_GRADE_SCALE for any school that hasn't configured a
custom scheme, so existing schools keep grading exactly as before until they
opt in.

Callers should fetch get_school_schemes() ONCE per request/service call and
reuse the result via match_scale() for every grade in that call — not once
per grade — since it's a couple of DB queries.
"""
from typing import Optional

from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.grade import GradingScheme, GradeScale
from utils.grade_scale import GES_GRADE_SCALE, get_letter_grade as _band_lookup


async def get_school_schemes(session: AsyncSession, school_id: str) -> list[dict]:
    """All active GradingScheme rows for a school, each with its ordered
    bands (highest min_score first). A scheme with no bands yet can't grade
    anything and is skipped."""
    schemes_result = await session.execute(
        select(GradingScheme).where(GradingScheme.school_id == school_id, GradingScheme.is_active == True)
    )
    schemes = schemes_result.scalars().all()
    if not schemes:
        return []

    scheme_ids = [s.id for s in schemes]
    bands_result = await session.execute(
        select(GradeScale).where(GradeScale.scheme_id.in_(scheme_ids)).order_by(GradeScale.min_score.desc())
    )
    bands_by_scheme: dict = {}
    for b in bands_result.scalars().all():
        bands_by_scheme.setdefault(b.scheme_id, []).append({
            "grade": b.grade, "min_score": b.min_score, "max_score": b.max_score,
            "description": b.description, "gpa_point": b.gpa_point,
        })

    return [
        {"id": s.id, "name": s.name, "class_level": s.class_level, "subject_id": s.subject_id,
         "bands": bands_by_scheme[s.id], "ca_weight": s.ca_weight, "exam_weight": s.exam_weight}
        for s in schemes if bands_by_scheme.get(s.id)
    ]


def match_scale(schemes: list[dict], class_level: Optional[str] = None, subject_id: Optional[str] = None) -> list[dict]:
    """Best-matching band list for the given scope. Specificity order:
    level+subject match > level-only match > subject-only match > school-wide
    default (both None on the scheme). A scheme scoped to a *different*
    level or subject than requested is never a match. Falls back to the
    built-in GES scale if nothing configured/matched."""
    def specificity(s: dict) -> int:
        level_hit = s["class_level"] is None or s["class_level"] == class_level
        subject_hit = s["subject_id"] is None or s["subject_id"] == subject_id
        if not (level_hit and subject_hit):
            return -1
        return (2 if s["class_level"] is not None else 0) + (2 if s["subject_id"] is not None else 0)

    candidates = [(specificity(s), s) for s in schemes]
    candidates = [(score, s) for score, s in candidates if score >= 0]
    if not candidates:
        return GES_GRADE_SCALE
    _, best = max(candidates, key=lambda pair: pair[0])
    return best["bands"]


def match_weights(schemes: list[dict], class_level: Optional[str] = None, subject_id: Optional[str] = None) -> tuple[float, float]:
    """Best-matching (ca_weight, exam_weight) for the given scope, same
    specificity rule as match_scale (level+subject > level-only >
    subject-only > school-wide default). Falls back to (50.0, 50.0) —
    services/report_card_pdf_service.py's previous hardcoded GES 50/50
    split — if nothing configured/matched, so a school that hasn't set up a
    scheme keeps grading exactly as before."""
    def specificity(s: dict) -> int:
        level_hit = s["class_level"] is None or s["class_level"] == class_level
        subject_hit = s["subject_id"] is None or s["subject_id"] == subject_id
        if not (level_hit and subject_hit):
            return -1
        return (2 if s["class_level"] is not None else 0) + (2 if s["subject_id"] is not None else 0)

    candidates = [(specificity(s), s) for s in schemes]
    candidates = [(score, s) for score, s in candidates if score >= 0]
    if not candidates:
        return (50.0, 50.0)
    _, best = max(candidates, key=lambda pair: pair[0])
    return (best["ca_weight"], best["exam_weight"])


def build_subject_weights(schemes: list[dict], class_level: Optional[str], subject_ids) -> dict:
    """{subject_id: (ca_weight, exam_weight)} for every subject_id in
    `subject_ids`, resolved via match_weights — the shape
    services/report_card_pdf_service.py::compute_subject_ges_totals's
    `weights` parameter expects. Callers already fetching `schemes` once per
    request/class (per this module's own convention) can reuse it here too."""
    return {subject_id: match_weights(schemes, class_level, subject_id) for subject_id in subject_ids}


async def resolve_letter_grade(
    session: AsyncSession,
    percentage: float,
    school_id: str,
    class_level: Optional[str] = None,
    subject_id: Optional[str] = None,
) -> dict:
    """Convenience one-shot resolve + lookup for a single call. Prefer
    get_school_schemes() + match_scale() directly when converting many
    scores in the same request, to avoid re-querying per score."""
    schemes = await get_school_schemes(session, school_id)
    scale = match_scale(schemes, class_level, subject_id)
    return _band_lookup(percentage, scale=scale)
