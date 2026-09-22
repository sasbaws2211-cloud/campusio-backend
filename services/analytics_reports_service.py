"""Self-service analytics/reports — data builders shared by the JSON preview
and CSV export endpoints in routers/reports_analytics.py, so the aggregation
logic lives in exactly one place. Each builder returns {"rows": [...],
"summary": {...}} — CSV export just writes that same structure out with
csv.writer, mirroring services/attendance_export_service.py's shape.

Named distinctly from services/reports_service.py (GL financial statements
consumed by routers/finance/reports.py) to avoid colliding with it.
"""
import csv
import io
from collections import defaultdict
from datetime import datetime
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from models.attendance import Attendance, AttendanceStatus
from models.fee import Fee, FeePayment, FeeStructure
from models.grade import ReportCard
from models.student import Student, Gender, StudentStatus
from models.classroom import Class
from models.school import School


async def build_attendance_report(
    session: AsyncSession, school_id: str, start_date: str, end_date: str,
    class_id: Optional[str] = None, campus_id: Optional[str] = None,
) -> dict:
    query = select(Attendance).where(
        Attendance.school_id == school_id,
        Attendance.attendance_date >= start_date,
        Attendance.attendance_date <= end_date,
    )
    if class_id:
        query = query.where(Attendance.class_id == class_id)
    result = await session.execute(query)
    records = result.scalars().all()

    student_ids = {r.student_id for r in records}
    students_map = {}
    if student_ids:
        students_result = await session.execute(select(Student).where(Student.id.in_(student_ids)))
        students_map = {s.id: s for s in students_result.scalars().all()}

    if campus_id:
        allowed_ids = {sid for sid, s in students_map.items() if s.campus_id == campus_id}
        records = [r for r in records if r.student_id in allowed_ids]

    per_student: dict = {}
    for r in records:
        counts = per_student.setdefault(r.student_id, {"present": 0, "absent": 0, "late": 0, "excused": 0})
        status_key = r.status.value if hasattr(r.status, "value") else r.status
        if status_key in counts:
            counts[status_key] += 1

    rows = []
    for student_id, counts in per_student.items():
        student = students_map.get(student_id)
        total = sum(counts.values())
        rate = round(counts["present"] / total * 100, 1) if total else 0
        rows.append({
            "student_id_code": student.student_id if student else student_id,
            "student_name": f"{student.first_name} {student.last_name}" if student else "Unknown",
            "present": counts["present"],
            "absent": counts["absent"],
            "late": counts["late"],
            "excused": counts["excused"],
            "attendance_rate": rate,
        })
    rows.sort(key=lambda r: r["student_name"])

    summary = {
        "total_students": len(rows),
        "total_records": len(records),
        "average_attendance_rate": round(sum(r["attendance_rate"] for r in rows) / len(rows), 1) if rows else 0,
    }
    return {"rows": rows, "summary": summary}


async def build_fee_collection_report(
    session: AsyncSession, school_id: str, start_date: str, end_date: str,
    class_id: Optional[str] = None, campus_id: Optional[str] = None, fee_type: Optional[str] = None,
) -> dict:
    query = select(FeePayment).where(
        FeePayment.school_id == school_id,
        FeePayment.payment_date >= start_date,
        FeePayment.payment_date <= end_date,
        FeePayment.voided == False,  # noqa: E712 — SQLAlchemy comparison, not a Python bool check
    )
    result = await session.execute(query)
    payments = result.scalars().all()

    fee_ids = {p.fee_id for p in payments}
    fees_map = {}
    if fee_ids:
        fees_result = await session.execute(select(Fee).where(Fee.id.in_(fee_ids)))
        fees_map = {f.id: f for f in fees_result.scalars().all()}

    structure_ids = {f.fee_structure_id for f in fees_map.values()}
    structures_map = {}
    if structure_ids:
        structures_result = await session.execute(select(FeeStructure).where(FeeStructure.id.in_(structure_ids)))
        structures_map = {s.id: s for s in structures_result.scalars().all()}

    student_ids = {p.student_id for p in payments}
    students_map = {}
    if student_ids:
        students_result = await session.execute(select(Student).where(Student.id.in_(student_ids)))
        students_map = {s.id: s for s in students_result.scalars().all()}

    rows = []
    for p in payments:
        fee = fees_map.get(p.fee_id)
        structure = structures_map.get(fee.fee_structure_id) if fee else None
        student = students_map.get(p.student_id)

        if class_id and (not student or student.class_id != class_id):
            continue
        if campus_id and (not student or student.campus_id != campus_id):
            continue
        if fee_type and (not structure or structure.fee_type != fee_type):
            continue

        rows.append({
            "receipt_number": p.receipt_number,
            "student_id_code": student.student_id if student else p.student_id,
            "student_name": f"{student.first_name} {student.last_name}" if student else "Unknown",
            "fee_type": structure.fee_type if structure else "",
            "amount": p.amount,
            "payment_method": p.payment_method.value if hasattr(p.payment_method, "value") else p.payment_method,
            "payment_date": p.payment_date,
            "reference_number": p.reference_number or "",
        })
    rows.sort(key=lambda r: r["payment_date"])

    summary = {
        "total_payments": len(rows),
        "total_collected": round(sum(r["amount"] for r in rows), 2),
    }

    # ── Monthly time series (additive — `summary` above is unchanged, so
    # existing CSV export stays byte-for-byte compatible) ───────────────
    # Collected: bucket each row's payment_date (a "YYYY-MM-DD" str) by
    # string-slicing to "YYYY-MM" — NOT .strftime(), payment_date is a str
    # field, not a date/datetime column.
    collected_by_month = defaultdict(float)
    for r in rows:
        month = r["payment_date"][:7] if r["payment_date"] and len(r["payment_date"]) >= 7 else "unknown"
        collected_by_month[month] += r["amount"]

    # Billed: independent of payments — every Fee due in the window (via its
    # FeeStructure.due_date, since Fee itself carries no due_date of its
    # own), under the same class/campus/fee_type filters used for collected.
    billed_by_month = defaultdict(float)
    all_fees = (await session.execute(select(Fee).where(Fee.school_id == school_id))).scalars().all()
    if all_fees:
        all_structure_ids = {f.fee_structure_id for f in all_fees}
        all_structures_map = structures_map
        missing_structure_ids = all_structure_ids - set(all_structures_map.keys())
        if missing_structure_ids:
            more_structures = (await session.execute(select(FeeStructure).where(FeeStructure.id.in_(missing_structure_ids)))).scalars().all()
            all_structures_map = {**all_structures_map, **{s.id: s for s in more_structures}}

        all_student_ids = {f.student_id for f in all_fees}
        missing_student_ids = all_student_ids - set(students_map.keys())
        all_students_map = students_map
        if missing_student_ids:
            more_students = (await session.execute(select(Student).where(Student.id.in_(missing_student_ids)))).scalars().all()
            all_students_map = {**all_students_map, **{s.id: s for s in more_students}}

        for fee in all_fees:
            structure = all_structures_map.get(fee.fee_structure_id)
            if not structure or not structure.due_date or len(structure.due_date) < 7:
                continue
            if not (start_date <= structure.due_date <= end_date):
                continue
            student = all_students_map.get(fee.student_id)
            if class_id and (not student or student.class_id != class_id):
                continue
            if campus_id and (not student or student.campus_id != campus_id):
                continue
            if fee_type and structure.fee_type != fee_type:
                continue
            # Net of discount — a discount means that portion was never
            # expected to be collected, so it shouldn't inflate "billed"
            # or deflate the collection_rate computed from it below.
            billed_by_month[structure.due_date[:7]] += fee.amount_due - fee.discount

    months = sorted(set(collected_by_month) | set(billed_by_month))
    by_month = [
        {
            "month": m,
            "billed": round(billed_by_month.get(m, 0), 2),
            "collected": round(collected_by_month.get(m, 0), 2),
            "collection_rate": round(collected_by_month.get(m, 0) / billed_by_month[m] * 100, 1) if billed_by_month.get(m) else None,
        }
        for m in months
    ]

    return {"rows": rows, "summary": summary, "by_month": by_month}


async def build_academic_performance_report(
    session: AsyncSession, school_id: str, academic_term_id: str,
    class_id: Optional[str] = None, campus_id: Optional[str] = None,
) -> dict:
    query = select(ReportCard).where(
        ReportCard.school_id == school_id,
        ReportCard.academic_term_id == academic_term_id,
    )
    if class_id:
        query = query.where(ReportCard.class_id == class_id)
    result = await session.execute(query)
    cards = result.scalars().all()

    student_ids = {c.student_id for c in cards}
    students_map = {}
    if student_ids:
        students_result = await session.execute(select(Student).where(Student.id.in_(student_ids)))
        students_map = {s.id: s for s in students_result.scalars().all()}

    class_ids = {c.class_id for c in cards}
    classes_map = {}
    if class_ids:
        classes_result = await session.execute(select(Class).where(Class.id.in_(class_ids)))
        classes_map = {c.id: c for c in classes_result.scalars().all()}

    rows = []
    for c in cards:
        student = students_map.get(c.student_id)
        if campus_id and (not student or student.campus_id != campus_id):
            continue
        cls = classes_map.get(c.class_id)
        rows.append({
            "student_id_code": student.student_id if student else c.student_id,
            "student_name": f"{student.first_name} {student.last_name}" if student else "Unknown",
            "class_name": cls.name if cls else "",
            "average_score": c.average_score,
            "position": c.position,
            "class_size": c.class_size,
            "attendance_percentage": c.attendance_percentage,
            "status": c.status.value if hasattr(c.status, "value") else c.status,
        })
    rows.sort(key=lambda r: (r["class_name"], -(r["average_score"] or 0)))

    summary = {
        "total_students": len(rows),
        "average_score_overall": round(sum(r["average_score"] or 0 for r in rows) / len(rows), 1) if rows else 0,
    }
    return {"rows": rows, "summary": summary}


async def build_enrollment_report(
    session: AsyncSession, school_id: str,
    class_id: Optional[str] = None, campus_id: Optional[str] = None,
) -> dict:
    class_query = select(Class).where(Class.school_id == school_id, Class.is_active == True)
    if class_id:
        class_query = class_query.where(Class.id == class_id)
    if campus_id:
        class_query = class_query.where(Class.campus_id == campus_id)
    classes_result = await session.execute(class_query.order_by(Class.level, Class.name))
    classes = classes_result.scalars().all()

    students_result = await session.execute(
        select(Student).where(Student.school_id == school_id, Student.status == StudentStatus.ACTIVE)
    )
    students = students_result.scalars().all()
    by_class: dict = {}
    for s in students:
        by_class.setdefault(s.class_id, []).append(s)

    rows = []
    for cls in classes:
        roster = by_class.get(cls.id, [])
        male = sum(1 for s in roster if s.gender == Gender.MALE)
        female = sum(1 for s in roster if s.gender == Gender.FEMALE)
        rows.append({
            "class_name": cls.name,
            "level": cls.level.value if hasattr(cls.level, "value") else cls.level,
            "capacity": cls.capacity,
            "enrolled": len(roster),
            "male": male,
            "female": female,
            "utilization_pct": round(len(roster) / cls.capacity * 100, 1) if cls.capacity else 0,
        })

    summary = {
        "total_classes": len(rows),
        "total_enrolled": sum(r["enrolled"] for r in rows),
    }
    return {"rows": rows, "summary": summary}


def _to_csv(school_name: str, title: str, meta_lines: list, rows: list, summary: dict) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["School", school_name])
    writer.writerow([title])
    for line in meta_lines:
        writer.writerow(line)
    writer.writerow(["Generated", datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")])
    writer.writerow([])

    if rows:
        headers = list(rows[0].keys())
        writer.writerow(headers)
        for row in rows:
            writer.writerow([row.get(h, "") for h in headers])
    else:
        writer.writerow(["No data for the selected filters"])

    writer.writerow([])
    writer.writerow(["Summary"])
    for key, value in summary.items():
        writer.writerow([key, value])

    return buffer.getvalue()


async def report_to_csv(session: AsyncSession, school_id: str, title: str, meta_lines: list, report: dict) -> str:
    school_result = await session.execute(select(School).where(School.id == school_id))
    school = school_result.scalar_one_or_none()
    return _to_csv(school.name if school else "", title, meta_lines, report["rows"], report["summary"])
