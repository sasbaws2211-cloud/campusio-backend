"""Shared AssignmentQuestion formatting for student/teacher-facing responses.

This logic used to be duplicated three times — twice in routers/student_portal.py
(the assignments list and detail endpoints) and once in routers/extra_classes.py —
with small drift between the copies (e.g. one used a bare `except:`, another had
no error handling around json.loads at all). One bug fix here now covers all three
callers instead of needing to be applied three times.

Callers stay in control of the answer-key-leak policy: `reveal_answers` is decided
by each router based on its own rules (core: reveal once the student's submission
is graded; extra classes: reveal to the teacher only) rather than being baked into
this function, since that's a per-flow product decision, not shared mechanics.
"""
import json
from typing import Iterable


def format_assignment_questions(questions: Iterable, *, reveal_answers: bool) -> list[dict]:
    """Format a list of AssignmentQuestion rows for an API response.

    - Parses the JSON-encoded `options` field defensively (malformed/missing options
      degrade to an empty list rather than raising).
    - For `matching` questions, splits "left=right" pairs into separate `options`
      (left side) and `items` (right side) lists for the frontend's matching UI.
    - Includes `correct_answer` as `answer` only when `reveal_answers` is True.
    """
    formatted = []
    for q in questions:
        question_data = {
            "id": q.id,
            "question": q.question_text,
            "type": q.question_type,
            "points": q.points,
        }
        if reveal_answers:
            question_data["answer"] = q.correct_answer

        parsed_options = []
        if q.options:
            try:
                parsed_options = json.loads(q.options) if isinstance(q.options, str) else q.options
            except (TypeError, ValueError):
                parsed_options = []

        if q.question_type == "matching":
            left_items, right_items = [], []
            for pair in parsed_options:
                if isinstance(pair, str) and "=" in pair:
                    left, right = pair.split("=", 1)
                    left_items.append(left.strip())
                    right_items.append(right.strip())
                else:
                    left_items.append(str(pair))
            question_data["options"] = left_items
            question_data["items"] = right_items
        else:
            question_data["options"] = parsed_options
            question_data["items"] = []

        formatted.append(question_data)

    return formatted
