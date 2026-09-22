"""Shared submission-lifecycle helpers for the two assignment systems.

Both routers/student_portal.py (core assignments) and routers/extra_classes.py
(paid extra-class assignments) need to decide, at submit time, whether a
submission counts as on-time or late. This used to be computed independently
in three places with identical logic that could silently drift — which is
exactly what happened once (the core `/submit` endpoint never set LATE at all
until that was fixed). One shared function means there's only one place left
to get this right.
"""
from datetime import datetime
from typing import Optional

from models.assignment import SubmissionStatus


def resolve_submission_status(due_date: Optional[datetime], now: Optional[datetime] = None) -> SubmissionStatus:
    """Decide whether a submission happening now should be SUBMITTED or LATE.

    A missing due_date means the assignment has no deadline, so it's never late.
    """
    now = now or datetime.utcnow()
    if due_date and now > due_date:
        return SubmissionStatus.LATE
    return SubmissionStatus.SUBMITTED
