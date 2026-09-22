"""iCal (RFC 5545) generation for the academic calendar export/feed —
hand-rolled rather than a dependency, since neither `icalendar` nor `ics`
is in requirements.txt and the app only ever needs to emit a handful of
VEVENT fields (UID/DTSTAMP/DTSTART/DTEND/SUMMARY/DESCRIPTION/CATEGORIES),
well short of justifying a full parsing/generation library.
"""
import secrets
from datetime import date, datetime, timedelta
from typing import Iterable

from models.school import CalendarEvent


def generate_feed_token() -> str:
    return secrets.token_urlsafe(32)


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> str:
    """RFC 5545 3.1: content lines over 75 octets must be folded, each
    continuation line starting with a single leading space."""
    encoded = line.encode("utf-8")
    if len(encoded) <= 75:
        return line
    out = []
    while len(line.encode("utf-8")) > 75:
        out.append(line[:75])
        line = " " + line[75:]
    out.append(line)
    return "\r\n".join(out)


def build_ics(events: Iterable[CalendarEvent], calendar_name: str) -> str:
    now_stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Campusio//Academic Calendar//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        _fold(f"X-WR-CALNAME:{_escape(calendar_name)}"),
        "X-PUBLISHED-TTL:PT12H",
    ]
    for event in events:
        try:
            start = date.fromisoformat(event.start_date)
            end = date.fromisoformat(event.end_date)
        except ValueError:
            continue
        dtend_exclusive = end + timedelta(days=1)  # RFC 5545: DATE-valued DTEND is exclusive
        lines.append("BEGIN:VEVENT")
        lines.append(f"UID:{event.id}@campusio")
        lines.append(f"DTSTAMP:{now_stamp}")
        lines.append(f"DTSTART;VALUE=DATE:{start.strftime('%Y%m%d')}")
        lines.append(f"DTEND;VALUE=DATE:{dtend_exclusive.strftime('%Y%m%d')}")
        lines.append(_fold(f"SUMMARY:{_escape(event.title)}"))
        if event.description:
            lines.append(_fold(f"DESCRIPTION:{_escape(event.description)}"))
        lines.append(_fold(f"CATEGORIES:{_escape((event.event_type or 'event').upper())}"))
        lines.append("TRANSP:TRANSPARENT" if not event.is_instructional else "TRANSP:OPAQUE")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"
