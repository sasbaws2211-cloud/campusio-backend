"""Staff attendance CSV export — mirrors services/statutory_export_service.py's
shape (stdlib csv + io.StringIO, no new dependency): metadata header rows,
column headers, one row per record, then a totals row.
"""
import csv
import io
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from models.attendance import StaffAttendance, AttendanceStatus
from models.staff import Staff
from models.school import School


def _recorded_via(record: StaffAttendance) -> str:
    if record.recorded_by == "SYSTEM":
        return "Auto-absent"
    if record.via_qr:
        return "Self clock-in (QR)"
    if record.leave_request_id:
        return "Approved leave"
    if record.check_in:
        return "Self clock-in"
    return "Admin/HR entry"


async def generate_staff_attendance_csv(
    session: AsyncSession, school_id: str, start_date: str, end_date: str
) -> str:
    school_result = await session.execute(select(School).where(School.id == school_id))
    school = school_result.scalar_one_or_none()

    records_result = await session.execute(
        select(StaffAttendance).where(
            StaffAttendance.school_id == school_id,
            StaffAttendance.attendance_date >= start_date,
            StaffAttendance.attendance_date <= end_date,
        ).order_by(StaffAttendance.staff_id, StaffAttendance.attendance_date)
    )
    records = records_result.scalars().all()

    staff_ids = {r.staff_id for r in records}
    staff_map = {}
    if staff_ids:
        staff_result = await session.execute(select(Staff).where(Staff.id.in_(staff_ids)))
        for s in staff_result.scalars().all():
            staff_map[s.id] = s

    buffer = io.StringIO()
    writer = csv.writer(buffer)

    writer.writerow(["School", school.name if school else ""])
    writer.writerow(["Period", f"{start_date} to {end_date}"])
    writer.writerow(["Generated", datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")])
    writer.writerow([])

    writer.writerow([
        "Staff ID", "Staff Name", "Position", "Date", "Status",
        "Check In", "Check Out", "Recorded Via", "Remarks",
    ])

    present = absent = late = excused = 0
    for r in records:
        staff = staff_map.get(r.staff_id)
        if r.status == AttendanceStatus.PRESENT:
            present += 1
        elif r.status == AttendanceStatus.ABSENT:
            absent += 1
        elif r.status == AttendanceStatus.LATE:
            late += 1
        elif r.status == AttendanceStatus.EXCUSED:
            excused += 1

        writer.writerow([
            staff.staff_id if staff else r.staff_id,
            f"{staff.first_name} {staff.last_name}" if staff else "Unknown",
            staff.position if staff else "",
            r.attendance_date,
            r.status.value if hasattr(r.status, "value") else r.status,
            r.check_in or "",
            r.check_out or "",
            _recorded_via(r),
            r.remarks or "",
        ])

    writer.writerow([])
    writer.writerow([
        "TOTAL", "", "", "", f"{len(records)} record(s)",
        f"Present: {present}", f"Absent: {absent}", f"Late: {late}", f"Excused: {excused}",
    ])

    return buffer.getvalue()
