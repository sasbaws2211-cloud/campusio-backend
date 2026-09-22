"""Single source of truth for the GES (Ghana Education Service) grading scale.

Previously this 9-band table was hardcoded independently in four places
(routers/grades.py, routers/student_portal.py, services/assignment_performance.py,
services/report_card_pdf_service.py), which meant a scale change had to be made
in four places at once and could silently drift out of sync.
"""

GES_GRADE_SCALE = [
    {"grade": "1", "min_score": 80, "max_score": 100, "description": "Excellent", "gpa_point": 1.0, "interpretation": "Highest"},
    {"grade": "2", "min_score": 70, "max_score": 79, "description": "Very Good", "gpa_point": 2.0, "interpretation": "Above Average"},
    {"grade": "3", "min_score": 60, "max_score": 69, "description": "Good", "gpa_point": 3.0, "interpretation": "Average"},
    {"grade": "4", "min_score": 55, "max_score": 59, "description": "Credit", "gpa_point": 4.0, "interpretation": "Below Average"},
    {"grade": "5", "min_score": 50, "max_score": 54, "description": "Pass", "gpa_point": 5.0, "interpretation": "Pass"},
    {"grade": "6", "min_score": 45, "max_score": 49, "description": "Weak Pass", "gpa_point": 6.0, "interpretation": "Weak Pass"},
    {"grade": "7", "min_score": 40, "max_score": 44, "description": "Very Weak", "gpa_point": 7.0, "interpretation": "Very Weak"},
    {"grade": "8", "min_score": 35, "max_score": 39, "description": "Poor", "gpa_point": 8.0, "interpretation": "Poor"},
    {"grade": "9", "min_score": 0, "max_score": 34, "description": "Fail", "gpa_point": 9.0, "interpretation": "Lowest/Fail"},
]


def get_letter_grade(percentage: float, scale: list = None) -> dict:
    """Convert a percentage score to its grade band. Returns the fail band for
    out-of-range/None input rather than raising, matching prior call-site behavior.

    scale: an ordered list of band dicts (grade/min_score/max_score/description/
    gpa_point), same shape as GES_GRADE_SCALE. Defaults to GES_GRADE_SCALE —
    pass a school's configured scale via services/grading_service.py to
    support the multiple/configurable grading systems feature instead.

    Matches by min_score alone (the highest band whose min_score the score
    clears) rather than a closed [min_score, max_score] range. Scores here
    are percentages rounded to 1 decimal place (see
    services/report_card_pdf_service.py::compute_subject_ges_totals), so a
    fractional value like 79.5 used to fall strictly between one band's
    integer max_score (79) and the next band's min_score (80), matching
    neither — and silently returning scale[-1], the WORST band, for a
    near-top score. Sorted descending here (defensively — every current
    caller already provides bands in that order) so the first band cleared
    is always the correct, highest one; every real number from a band's own
    min_score up to (but not including) the next band's min_score belongs
    to it, so this can never fall through for an in-range score."""
    scale = scale or GES_GRADE_SCALE
    if percentage is None or percentage < 0:
        return scale[-1]
    ordered = sorted(scale, key=lambda band: band["min_score"], reverse=True)
    for band in ordered:
        if percentage >= band["min_score"]:
            return band
    return scale[-1]
