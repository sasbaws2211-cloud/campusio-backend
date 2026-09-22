"""ZKTeco ADMS/Push protocol receiver.

A biometric terminal — a face-recognition unit at the canteen counter, a
fingerprint unit at the staff gate, or one at the student entrance — pushes
recognition/scan events here over plain HTTP, with no vendor middleware in
between. One receiver, one protocol parser, shared by all three use cases:
which one a given push means is decided by the registered device's
`purpose` (models.integrations.BiometricDevicePurpose) — CANTEEN resolves
the PIN against Student.biometric_device_pin and broadcasts a
canteen_face_scan event; STAFF_ATTENDANCE resolves it against
Staff.biometric_device_pin and records a punch via
routers.attendance.record_staff_punch; STUDENT_ATTENDANCE resolves it
against Student.biometric_device_pin (the same field CANTEEN uses — a
student can be enrolled on a canteen device and a gate device
independently) and records a gate check-in/out via
services.gate_attendance_service.record_gate_punch. Both attendance
branches call the same function routers/public_api.py's API-key-
authenticated punch endpoints use, for a device with vendor middleware in
between instead.

This is NOT a JWT-authenticated
router: the device has no way to do that, so every route here is public by
necessity. The security model is: only a pre-registered, active device
serial (models.integrations.BiometricDevice.device_serial — the SAME
registry routers/integrations.py's admin CRUD and routers/public_api.py's
attendance-punch endpoints already use, deliberately reused here rather
than a second device table) can trigger anything, and even then a
recognized push only PRE-FILLS a student's identity on a staff member's
already-authenticated screen (routers/canteen_wallet.py's ScanChargeTab) —
it can never move money by itself, since charging still requires a staff
POST /canteen-wallet/orders call exactly as a manual card scan does.

Unlike the existing punch endpoints in routers/public_api.py (which expect
an API-key-authenticated caller — typically vendor middleware — that
already knows the school's own student_id), this router is for a device
that speaks the ADMS protocol directly with no middleware in between. It
only knows its own internal numeric PIN per enrolled face, which is why
Student gains a separate biometric_device_pin field rather than reusing
student_id here.

Field names/formats below are the best-effort reading of third-party ADMS
documentation, not the official ZKTeco spec or a real device — every
request is logged in full at INFO level specifically so the first real
terminal's handshake can be inspected and any mismatch corrected quickly.
"""
import logging
from datetime import datetime
from typing import Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import PlainTextResponse
from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_session
from models.integrations import BiometricDevice, BiometricDevicePurpose, BiometricRejectedScan
from models.staff import Staff, StaffStatus
from models.student import Student
from routers.attendance import record_staff_punch
from services import gate_attendance_service
from services.canteen_wallet_service import CanteenWalletService, build_canteen_wallet_snapshot
from services.broadcaster import broadcaster

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/adms", tags=["Biometric ADMS"])


def _parse_attlog(body: str) -> List[Dict[str, str]]:
    """Each line is PIN=...\\tDateTime=...\\tVerified=...\\tStatus=... — split
    on TAB primarily (the documented ADMS delimiter), since DateTime's own
    value contains a literal space ("2026-09-02 07:15:00") that a naive
    whitespace-split would wrongly break the field on, silently truncating
    every timestamp to just the date. Falls back to whitespace-splitting
    only when a line has no tabs at all, for a device that turns out to
    delimit differently — accepting DateTime may be malformed in that
    fallback case, since it isn't confirmed against real hardware yet."""
    records = []
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        tokens = line.split("\t") if "\t" in line else line.split()
        fields = {}
        for token in tokens:
            token = token.strip()
            if "=" in token:
                key, _, value = token.partition("=")
                fields[key.strip().upper()] = value.strip()
        if "PIN" in fields:
            records.append(fields)
    return records


def _parse_attlog_datetime(value: Optional[str]) -> Optional[datetime]:
    """The device's own reported scan time, when parseable — used instead
    of server-receipt time so a punch buffered/replayed after a network
    outage still lands on the day/time it actually happened, and so
    late-vs-present is computed against when the person actually scanned."""
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    logger.warning(f"[ADMS cdata] unparseable DateTime {value!r} — falling back to receipt time")
    return None


@router.post("/iclock/cdata")
async def adms_cdata(request: Request, background_tasks: BackgroundTasks, session: AsyncSession = Depends(get_session)):
    query = dict(request.query_params)
    body_bytes = await request.body()
    body = body_bytes.decode("utf-8", errors="replace")

    # Defensive logging (deliberate, per the plan this was scoped from) —
    # this is the primary tool for reconciling real-device output against
    # the assumptions below, once hardware is actually available to test.
    logger.info(f"[ADMS cdata] query={query} body={body!r}")

    sn = query.get("SN")
    table = query.get("table", "")

    if not sn:
        return PlainTextResponse("OK")

    result = await session.execute(select(BiometricDevice).where(BiometricDevice.device_serial == sn))
    device = result.scalar_one_or_none()
    if not device or not device.is_active:
        logger.warning(f"[ADMS cdata] push from unregistered/inactive device SN={sn!r} — ignored")
        return PlainTextResponse("OK")

    device.last_seen_at = datetime.utcnow()
    # A device that just reported in is no longer "stale" — clear any prior
    # alert so a FUTURE outage (not this same one) gets a fresh alert
    # instead of staying permanently suppressed. See services.scheduler's
    # device-health sweep, which sets this.
    device.stale_alerted_at = None
    session.add(device)

    if table.upper() == "ATTLOG":
        for record in _parse_attlog(body):
            pin = record.get("PIN")
            if not pin:
                continue

            if device.purpose == BiometricDevicePurpose.STAFF_ATTENDANCE:
                staff_result = await session.execute(
                    select(Staff).where(Staff.biometric_device_pin == pin, Staff.school_id == device.school_id, Staff.status == StaffStatus.ACTIVE)
                )
                staff = staff_result.scalar_one_or_none()
                if not staff:
                    # Also covers a departed staff member whose enrollment PIN is
                    # still on file but whose Staff.status is no longer ACTIVE
                    # (see routers/hr_admin.py's exit-clearance finalization) —
                    # their fingerprint should stop producing punches the moment
                    # they leave, not keep clocking them in indefinitely.
                    logger.info(f"[ADMS cdata] PIN={pin!r} on device {sn!r} has no matching active staff member — ignored")
                    session.add(BiometricRejectedScan(school_id=device.school_id, device_serial=sn, purpose=device.purpose, pin=pin, reason="no_matching_staff"))
                    continue
                punched_at = _parse_attlog_datetime(record.get("DATETIME"))
                await record_staff_punch(session, background_tasks, device.school_id, staff, device.device_serial, punched_at=punched_at)
                continue

            if device.purpose == BiometricDevicePurpose.STUDENT_ATTENDANCE:
                student_result = await session.execute(
                    select(Student).where(Student.biometric_device_pin == pin, Student.school_id == device.school_id)
                )
                student = student_result.scalar_one_or_none()
                if not student:
                    logger.info(f"[ADMS cdata] PIN={pin!r} on device {sn!r} has no matching student — ignored")
                    session.add(BiometricRejectedScan(school_id=device.school_id, device_serial=sn, purpose=device.purpose, pin=pin, reason="no_matching_student"))
                    continue
                punched_at = _parse_attlog_datetime(record.get("DATETIME"))
                await gate_attendance_service.record_gate_punch(
                    session, background_tasks, device.school_id, student, device.device_serial, punched_at=punched_at,
                )
                continue

            # CANTEEN (the other declared purpose) — resolve a student and
            # broadcast to any staff screen waiting on this school's SSE stream.
            result = await session.execute(
                select(Student).where(Student.biometric_device_pin == pin, Student.school_id == device.school_id)
            )
            student = result.scalar_one_or_none()
            if not student:
                logger.info(f"[ADMS cdata] PIN={pin!r} on device {sn!r} has no matching student — ignored")
                session.add(BiometricRejectedScan(school_id=device.school_id, device_serial=sn, purpose=device.purpose, pin=pin, reason="no_matching_student"))
                continue

            service = CanteenWalletService(session)
            account = await service.get_or_create_account(session=session, school_id=student.school_id, student_id=student.id)

            await broadcaster.publish({
                "type": "canteen_face_scan",
                "school_id": student.school_id,
                "student_id": student.id,
                "first_name": student.first_name,
                "last_name": student.last_name,
                "photo_url": student.photo_url,
                "wallet": build_canteen_wallet_snapshot(account=account),
            })

    await session.commit()
    return PlainTextResponse("OK")


@router.get("/iclock/getrequest")
async def adms_getrequest(request: Request):
    # Command-polling endpoint — devices call this expecting server-to-device
    # commands (e.g. "add this user"). Not implemented yet: see the plan's
    # Context note on why centrally-managed enrollment isn't in scope for
    # this protocol. Always answering OK/empty keeps the device's polling
    # loop happy without it retrying aggressively.
    logger.info(f"[ADMS getrequest] query={dict(request.query_params)}")
    return PlainTextResponse("OK")
