"""Third round of strategic-analytics rollups — all read-only aggregation
over data that already exists module-by-module: financial ratios/solvency,
transport/hostel/canteen utilization, scholarship-outcome effectiveness,
utility consumption trends, parent engagement, vendor performance, health
trends, discipline trends, and classroom-space utilization, plus a basic
data-quality/record-completeness check. See models/strategic_goals.py and
models/risk_register.py for the two genuinely new registries this round
added (routers/strategic_goals.py, routers/risk_register.py)."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Optional

from dateutil.relativedelta import relativedelta
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from auth import require_roles
from database import get_session
from models.canteen_wallet import CanteenOrder, CanteenOrderItem, CanteenWalletLedgerEntry
from models.classroom import Class
from models.discipline import IncidentReport
from models.facilities import UtilityMeter, UtilityReading
from models.fee import Fee, DiscountType
from models.finance.chart_of_accounts import GLAccount, AccountType, AccountCategory
from models.health import ClinicVisit, ImmunizationRecord
from models.hostel import HostelFee
from models.procurement import PurchaseOrder, GoodsReceivedNote, Supplier
from models.ptm import PTMBooking, PTMBookingStatus
from models.staff import Staff
from models.student import Student, StudentStatus, StudentParent, Parent
from models.timetable import Timetable, Period
from models.transport import Route, Vehicle, VehicleMaintenance, TransportFee
from models.user import User, UserRole
from models.communication import Message
from services.analytics import _weighted_gpa
from services.reports_service import ReportsService
from services.hostel_service import HostelService

router = APIRouter(prefix="/operational-insights", tags=["Operational Insights"])

STAFF_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.HR)


def _school_id(user: User) -> str:
    if not user.school_id:
        raise HTTPException(status_code=403, detail="No school context")
    return user.school_id


# ── Financial Ratios / Solvency ──────────────────────────────────────────

CURRENT_ASSET_CATEGORIES = {AccountCategory.BANK_ACCOUNTS.value, AccountCategory.ACCOUNTS_RECEIVABLE.value, AccountCategory.PREPAID_EXPENSES.value}
CURRENT_LIABILITY_CATEGORIES = {AccountCategory.ACCOUNTS_PAYABLE.value, AccountCategory.SALARIES_PAYABLE.value, AccountCategory.SHORT_TERM_DEBT.value}


@router.get("/financial-ratios", response_model=dict)
async def financial_ratios(
    months: int = Query(6, le=24),
    current_user: User = Depends(require_roles(*STAFF_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    accounts = (await session.execute(select(GLAccount).where(GLAccount.school_id == school_id, GLAccount.is_active == True))).scalars().all()  # noqa: E712

    def _sum(pred) -> float:
        return float(sum(a.current_balance for a in accounts if pred(a)))

    current_assets = _sum(lambda a: a.account_category in CURRENT_ASSET_CATEGORIES)
    current_liabilities = _sum(lambda a: a.account_category in CURRENT_LIABILITY_CATEGORIES)
    total_liabilities = _sum(lambda a: a.account_type == AccountType.LIABILITY.value)
    total_equity = _sum(lambda a: a.account_type == AccountType.EQUITY.value)
    cash_balance = _sum(lambda a: a.account_category == AccountCategory.BANK_ACCOUNTS.value)

    today = date.today()
    reports = ReportsService(session)
    trend = []
    for i in range(months - 1, -1, -1):
        month_start = (today.replace(day=1) - relativedelta(months=i))
        month_end = (month_start + relativedelta(months=1)) - timedelta(days=1)
        try:
            pl = await reports.generate_profit_loss(school_id, datetime.combine(month_start, datetime.min.time()), datetime.combine(month_end, datetime.max.time()))
            margin = float(pl.operating_income / pl.total_revenue * 100) if pl.total_revenue else None
            trend.append({"month": month_start.strftime("%Y-%m"), "revenue": float(pl.total_revenue), "expenses": float(pl.total_operating_expenses), "operating_margin_pct": round(margin, 1) if margin is not None else None})
        except Exception:
            trend.append({"month": month_start.strftime("%Y-%m"), "revenue": 0, "expenses": 0, "operating_margin_pct": None})

    trailing_expenses = sum(t["expenses"] for t in trend)
    avg_daily_expense = (trailing_expenses / (months * 30)) if trailing_expenses else 0
    days_cash_on_hand = round(cash_balance / avg_daily_expense, 1) if avg_daily_expense else None

    return {
        "current_ratio": round(current_assets / current_liabilities, 2) if current_liabilities else None,
        "debt_to_equity": round(total_liabilities / total_equity, 2) if total_equity else None,
        "days_cash_on_hand": days_cash_on_hand,
        "cash_balance": cash_balance,
        "current_assets": current_assets,
        "current_liabilities": current_liabilities,
        "operating_margin_trend": trend,
    }


# ── Transport Utilization ────────────────────────────────────────────────

@router.get("/transport-utilization", response_model=dict)
async def transport_utilization(current_user: User = Depends(require_roles(*STAFF_ROLES)), session: AsyncSession = Depends(get_session)):
    school_id = _school_id(current_user)
    routes = (await session.execute(select(Route).where(Route.school_id == school_id))).scalars().all()
    vehicles = {v.id: v for v in (await session.execute(select(Vehicle).where(Vehicle.school_id == school_id))).scalars().all()}

    rows = []
    for r in routes:
        vehicle = vehicles.get(r.vehicle_id) if r.vehicle_id else None
        capacity = vehicle.seating_capacity if vehicle else None
        utilization = round(r.student_count / capacity * 100, 1) if capacity else None
        revenue = (await session.execute(
            select(func.coalesce(func.sum(TransportFee.amount_paid), 0)).where(TransportFee.school_id == school_id, TransportFee.route_id == r.id)
        )).scalar() or 0
        rows.append({"route_id": r.id, "route_name": r.route_name, "student_count": r.student_count, "vehicle_capacity": capacity, "utilization_pct": utilization, "fee_revenue_collected": revenue})

    total_maintenance_cost = (await session.execute(
        select(func.coalesce(func.sum(VehicleMaintenance.cost), 0)).where(VehicleMaintenance.school_id == school_id)
    )).scalar() or 0

    return {"routes": rows, "total_vehicles": len(vehicles), "total_maintenance_cost": total_maintenance_cost}


# ── Hostel Occupancy Trend ────────────────────────────────────────────────

@router.get("/hostel-occupancy", response_model=dict)
async def hostel_occupancy(current_user: User = Depends(require_roles(*STAFF_ROLES)), session: AsyncSession = Depends(get_session)):
    school_id = _school_id(current_user)
    report = await HostelService(session).get_hostel_occupancy_report(school_id)

    total_revenue = (await session.execute(
        select(func.coalesce(func.sum(HostelFee.amount_paid), 0)).where(HostelFee.school_id == school_id)
    )).scalar() or 0
    report["revenue_per_occupied_bed"] = round(total_revenue / report["total_occupied"], 2) if report["total_occupied"] else None
    report["total_revenue"] = total_revenue
    return report


# ── Canteen Financial Insights ───────────────────────────────────────────

@router.get("/canteen-financials", response_model=dict)
async def canteen_financials(months: int = Query(6, le=24), current_user: User = Depends(require_roles(*STAFF_ROLES)), session: AsyncSession = Depends(get_session)):
    school_id = _school_id(current_user)
    since = datetime.utcnow() - timedelta(days=months * 30)

    orders = (await session.execute(
        select(CanteenOrder).where(CanteenOrder.school_id == school_id, CanteenOrder.status == "completed", CanteenOrder.completed_at >= since)
    )).scalars().all()

    revenue_by_month = defaultdict(float)
    for o in orders:
        revenue_by_month[o.completed_at.strftime("%Y-%m")] += o.total

    order_ids = [o.id for o in orders]
    item_totals = defaultdict(lambda: {"quantity": 0, "revenue": 0.0})
    if order_ids:
        items = (await session.execute(select(CanteenOrderItem).where(CanteenOrderItem.order_id.in_(order_ids)))).scalars().all()
        for it in items:
            item_totals[it.item_name]["quantity"] += it.quantity
            item_totals[it.item_name]["revenue"] += it.line_total
    popular_items = sorted(({"item_name": k, **v} for k, v in item_totals.items()), key=lambda r: -r["quantity"])[:10]

    topups = (await session.execute(
        select(CanteenWalletLedgerEntry).where(CanteenWalletLedgerEntry.event_type == "parent_topup", CanteenWalletLedgerEntry.created_at >= since)
    )).scalars().all()
    topup_by_month = defaultdict(float)
    for t in topups:
        topup_by_month[t.created_at.strftime("%Y-%m")] += t.amount

    return {
        "total_revenue": round(sum(revenue_by_month.values()), 2),
        "revenue_by_month": {k: round(v, 2) for k, v in sorted(revenue_by_month.items())},
        "popular_items": popular_items,
        "wallet_topups_by_month": {k: round(v, 2) for k, v in sorted(topup_by_month.items())},
    }


# ── Scholarship / Financial Aid Effectiveness ────────────────────────────

@router.get("/scholarship-effectiveness", response_model=dict)
async def scholarship_effectiveness(
    academic_term_id: str,
    current_user: User = Depends(require_roles(*STAFF_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    scholarship_student_ids = set((await session.execute(
        select(Fee.student_id).where(Fee.school_id == school_id, Fee.discount_type == DiscountType.SCHOLARSHIP.value, Fee.discount > 0).distinct()
    )).scalars().all())

    all_active = (await session.execute(select(Student).where(Student.school_id == school_id, Student.status == StudentStatus.ACTIVE.value))).scalars().all()
    non_scholarship_ids = {s.id for s in all_active} - scholarship_student_ids

    async def _summarize(ids: set) -> dict:
        if not ids:
            return {"count": 0, "avg_gpa": None, "retention_rate": None}
        gpas = []
        for sid in ids:
            gpa = await _weighted_gpa(session, sid, school_id, academic_term_id)
            if gpa is not None:
                gpas.append(gpa)
        active_count = (await session.execute(select(func.count(Student.id)).where(Student.id.in_(ids), Student.status == StudentStatus.ACTIVE.value))).scalar() or 0
        return {
            "count": len(ids),
            "avg_gpa": round(sum(gpas) / len(gpas), 2) if gpas else None,
            "retention_rate": round(active_count / len(ids) * 100, 1) if ids else None,
        }

    return {
        "scholarship_recipients": await _summarize(scholarship_student_ids),
        "non_recipients": await _summarize(non_scholarship_ids),
    }


# ── Utility / Sustainability Trends ──────────────────────────────────────

@router.get("/utility-trends", response_model=dict)
async def utility_trends(months: int = Query(12, le=36), current_user: User = Depends(require_roles(*STAFF_ROLES)), session: AsyncSession = Depends(get_session)):
    school_id = _school_id(current_user)
    since = (date.today() - timedelta(days=months * 30)).isoformat()

    meters = {m.id: m for m in (await session.execute(select(UtilityMeter).where(UtilityMeter.school_id == school_id))).scalars().all()}
    readings = (await session.execute(
        select(UtilityReading).where(UtilityReading.school_id == school_id, UtilityReading.reading_date >= since)
    )).scalars().all()

    by_type_month = defaultdict(lambda: defaultdict(float))
    cost_by_type = defaultdict(float)
    for r in readings:
        meter = meters.get(r.meter_id)
        utility_type = meter.utility_type if meter else "unknown"
        month = r.reading_date[:7] if r.reading_date and len(r.reading_date) >= 7 else "unknown"
        by_type_month[utility_type][month] += r.consumption or 0
        if r.cost:
            cost_by_type[utility_type] += r.cost

    return {
        "consumption_by_type_and_month": {t: dict(sorted(m.items())) for t, m in by_type_month.items()},
        "total_cost_by_type": dict(cost_by_type),
        "meters_tracked": len(meters),
    }


# ── Parent/Guardian Engagement ────────────────────────────────────────────

@router.get("/parent-engagement", response_model=dict)
async def parent_engagement(current_user: User = Depends(require_roles(*STAFF_ROLES)), session: AsyncSession = Depends(get_session)):
    school_id = _school_id(current_user)
    parents = (await session.execute(select(Parent).where(Parent.school_id == school_id, Parent.user_id.is_not(None)))).scalars().all()
    parent_user_ids = [p.user_id for p in parents]

    active_logins_30d = 0
    if parent_user_ids:
        cutoff = datetime.utcnow() - timedelta(days=30)
        users = (await session.execute(select(User).where(User.id.in_(parent_user_ids)))).scalars().all()
        active_logins_30d = sum(1 for u in users if u.last_login and u.last_login >= cutoff)
    login_rate_30d = round(active_logins_30d / len(parents) * 100, 1) if parents else None

    messages_to_parents = []
    if parent_user_ids:
        messages_to_parents = (await session.execute(
            select(Message).where(Message.school_id == school_id, Message.receiver_id.in_(parent_user_ids))
        )).scalars().all()
    read_rate = round(sum(1 for m in messages_to_parents if m.is_read) / len(messages_to_parents) * 100, 1) if messages_to_parents else None

    ptm_bookings = (await session.execute(select(PTMBooking).where(PTMBooking.school_id == school_id))).scalars().all()
    confirmed = sum(1 for b in ptm_bookings if b.status == PTMBookingStatus.CONFIRMED.value)
    ptm_confirmation_rate = round(confirmed / len(ptm_bookings) * 100, 1) if ptm_bookings else None

    return {
        "total_parents_with_portal_access": len(parents),
        "portal_login_rate_30d": login_rate_30d,
        "message_read_rate": read_rate,
        "total_messages_to_parents": len(messages_to_parents),
        "ptm_booking_confirmation_rate": ptm_confirmation_rate,
        "total_ptm_bookings": len(ptm_bookings),
    }


# ── Vendor / Supplier Performance ────────────────────────────────────────

@router.get("/vendor-performance", response_model=dict)
async def vendor_performance(current_user: User = Depends(require_roles(*STAFF_ROLES)), session: AsyncSession = Depends(get_session)):
    school_id = _school_id(current_user)
    suppliers = {s.id: s for s in (await session.execute(select(Supplier).where(Supplier.school_id == school_id))).scalars().all()}
    orders = (await session.execute(select(PurchaseOrder).where(PurchaseOrder.school_id == school_id))).scalars().all()
    grns = (await session.execute(select(GoodsReceivedNote).where(GoodsReceivedNote.school_id == school_id))).scalars().all()
    grn_by_po = defaultdict(list)
    for g in grns:
        grn_by_po[g.purchase_order_id].append(g)

    per_supplier = defaultdict(lambda: {"order_count": 0, "on_time_count": 0, "received_count": 0})
    for po in orders:
        stat = per_supplier[po.supplier_id]
        stat["order_count"] += 1
        received = grn_by_po.get(po.id, [])
        if received and po.expected_date:
            earliest = min(g.received_at for g in received)
            stat["received_count"] += 1
            if earliest.date().isoformat() <= po.expected_date:
                stat["on_time_count"] += 1

    rows = []
    for supplier_id, stat in per_supplier.items():
        supplier = suppliers.get(supplier_id)
        rows.append({
            "supplier_id": supplier_id, "supplier_name": supplier.name if supplier else "Unknown",
            "order_count": stat["order_count"],
            "on_time_delivery_rate": round(stat["on_time_count"] / stat["received_count"] * 100, 1) if stat["received_count"] else None,
        })
    rows.sort(key=lambda r: -r["order_count"])
    return {"suppliers": rows}


# ── Health & Wellness Trends ──────────────────────────────────────────────

@router.get("/health-trends", response_model=dict)
async def health_trends(months: int = Query(12, le=36), current_user: User = Depends(require_roles(*STAFF_ROLES)), session: AsyncSession = Depends(get_session)):
    school_id = _school_id(current_user)
    since = (date.today() - timedelta(days=months * 30)).isoformat()

    visits = (await session.execute(select(ClinicVisit).where(ClinicVisit.school_id == school_id, ClinicVisit.visit_date >= since))).scalars().all()
    visits_by_month = defaultdict(int)
    for v in visits:
        month = v.visit_date[:7] if v.visit_date and len(v.visit_date) >= 7 else "unknown"
        visits_by_month[month] += 1

    active_students = (await session.execute(select(func.count(Student.id)).where(Student.school_id == school_id, Student.status == StudentStatus.ACTIVE.value))).scalar() or 0
    immunized_student_ids = set((await session.execute(
        select(ImmunizationRecord.student_id).where(ImmunizationRecord.school_id == school_id).distinct()
    )).scalars().all())
    immunization_compliance_rate = round(len(immunized_student_ids) / active_students * 100, 1) if active_students else None

    return {
        "total_visits": len(visits),
        "visits_by_month": dict(sorted(visits_by_month.items())),
        "immunization_compliance_rate": immunization_compliance_rate,
        "students_with_immunization_record": len(immunized_student_ids),
    }


# ── Discipline Trends ──────────────────────────────────────────────────────

@router.get("/discipline-trends", response_model=dict)
async def discipline_trends(months: int = Query(12, le=36), current_user: User = Depends(require_roles(*STAFF_ROLES)), session: AsyncSession = Depends(get_session)):
    school_id = _school_id(current_user)
    since = (date.today() - timedelta(days=months * 30)).isoformat()

    incidents = (await session.execute(select(IncidentReport).where(IncidentReport.school_id == school_id, IncidentReport.incident_date >= since))).scalars().all()
    by_month = defaultdict(int)
    by_category = defaultdict(int)
    by_severity = defaultdict(int)
    for i in incidents:
        month = i.incident_date[:7] if i.incident_date and len(i.incident_date) >= 7 else "unknown"
        by_month[month] += 1
        by_category[i.category] += 1
        by_severity[i.severity if isinstance(i.severity, str) else i.severity.value] += 1

    return {
        "total_incidents": len(incidents),
        "by_month": dict(sorted(by_month.items())),
        "by_category": dict(by_category),
        "by_severity": dict(by_severity),
    }


# ── Classroom / Space Utilization ────────────────────────────────────────

@router.get("/space-utilization", response_model=dict)
async def space_utilization(
    academic_term_id: str,
    current_user: User = Depends(require_roles(*STAFF_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    timetable_rows = (await session.execute(select(Timetable).where(Timetable.school_id == school_id, Timetable.academic_term_id == academic_term_id))).scalars().all()
    total_periods = (await session.execute(select(func.count(Period.id)).where(Period.school_id == school_id, Period.is_active == True))).scalar() or 0  # noqa: E712

    classes = {c.id: c for c in (await session.execute(select(Class).where(Class.school_id == school_id))).scalars().all()}
    booked_by_class = defaultdict(int)
    for t in timetable_rows:
        booked_by_class[t.class_id] += 1

    days_per_week = 5  # Monday-Friday, matching models.timetable.DayOfWeek
    max_slots_per_class = total_periods * days_per_week

    rows = []
    for class_id, booked in booked_by_class.items():
        cls = classes.get(class_id)
        rows.append({
            "class_id": class_id, "class_name": cls.name if cls else "Unknown",
            "periods_booked": booked, "max_periods_per_week": max_slots_per_class,
            "utilization_pct": round(booked / max_slots_per_class * 100, 1) if max_slots_per_class else None,
        })
    rows.sort(key=lambda r: -(r["utilization_pct"] or 0))
    return {"academic_term_id": academic_term_id, "classes": rows}


# ── Data Quality / Record Completeness ───────────────────────────────────

@router.get("/data-quality", response_model=dict)
async def data_quality(current_user: User = Depends(require_roles(*STAFF_ROLES)), session: AsyncSession = Depends(get_session)):
    school_id = _school_id(current_user)
    students = (await session.execute(select(Student).where(Student.school_id == school_id, Student.status == StudentStatus.ACTIVE.value))).scalars().all()
    staff = (await session.execute(select(Staff).where(Staff.school_id == school_id))).scalars().all()

    def _pct_missing(rows, field) -> Optional[float]:
        if not rows:
            return None
        missing = sum(1 for r in rows if not getattr(r, field, None))
        return round(missing / len(rows) * 100, 1)

    return {
        "students": {
            "total": len(students),
            "missing_photo_pct": _pct_missing(students, "photo_url"),
            "missing_address_pct": _pct_missing(students, "address"),
            "missing_class_assignment_pct": _pct_missing(students, "class_id"),
        },
        "staff": {
            "total": len(staff),
            "missing_photo_pct": _pct_missing(staff, "photo_url"),
            "missing_department_pct": _pct_missing(staff, "department"),
            "missing_qualification_pct": _pct_missing(staff, "qualification"),
        },
    }
