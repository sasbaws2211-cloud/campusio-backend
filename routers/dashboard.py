"""Dashboard router - Analytics and Overview"""
from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import case
from datetime import datetime, timezone, date, timedelta
from models.student import Student, StudentStatus
from models.staff import Staff, StaffType, StaffStatus
from models.classroom import Class
from models.attendance import Attendance, AttendanceStatus
from models.fee import Fee, PaymentStatus
from models.school import School, AcademicTerm
from models.user import User, UserRole
from models.billing import PlatformSubscription, SubscriptionStatus, BillingPlan
from models.ticket import Ticket, TicketStatus
from database import get_session
from auth import get_current_user, require_roles
from dependencies import resolve_campus_scope
from services.finance_dashboard_service import compute_financial_snapshot

router = APIRouter(prefix="/dashboard", tags=["Dashboard"])


@router.get("/overview", response_model=dict)
async def get_dashboard_overview(
    campus_id: str | None = None,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Get dashboard overview statistics"""
    school_id = current_user.school_id
    
    if current_user.role == UserRole.SUPER_ADMIN:
        # 🚀 OPTIMIZATION: Combined single query instead of multiple count queries
        super_admin_stats = await session.execute(
            select(
                func.count(func.distinct(School.id)).label("total_schools"),
                func.count(func.distinct(Student.id)).label("total_students"),
                func.count(func.distinct(Staff.id)).label("total_staff")
            )
            .select_from(School)
            .outerjoin(Student, Student.school_id == School.id)
            .outerjoin(Staff, Staff.school_id == School.id)
            .where(
                (Student.status == StudentStatus.ACTIVE) | (Student.id == None),
                (Staff.status == StaffStatus.ACTIVE) | (Staff.id == None)
            )
        )
        row = super_admin_stats.first()
        
        return {
            "role": "super_admin",
            "stats": {
                "total_schools": row.total_schools or 0,
                "total_students": row.total_students or 0,
                "total_staff": row.total_staff or 0
            }
        }
    
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    campus_id = resolve_campus_scope(current_user, campus_id)

    # 🚀 OPTIMIZATION: Combined statistics query - avoid multiple database round trips
    today = date.today().isoformat()

    staff_join_cond = and_(Staff.school_id == school_id, Staff.status == StaffStatus.ACTIVE)
    class_join_cond = and_(Class.school_id == school_id, Class.is_active == True)
    student_where = [Student.school_id == school_id, Student.status == StudentStatus.ACTIVE]
    # Attendance/Fee carry no campus_id (see models/campus.py) — a campus view
    # narrows students/staff/classes, but attendance-today and fee totals stay
    # school-wide until those tables grow campus scoping too.
    if campus_id:
        staff_join_cond = and_(staff_join_cond, Staff.campus_id == campus_id)
        class_join_cond = and_(class_join_cond, Class.campus_id == campus_id)
        student_where.append(Student.campus_id == campus_id)

    stats_result = await session.execute(
        select(
            func.count(func.distinct(Student.id)).label("total_students"),
            func.count(func.distinct(Staff.id)).label("total_staff"),
            func.count(func.distinct(
                case(
                    (Staff.staff_type == StaffType.TEACHING, Staff.id),
                    else_=None
                )
            )).label("total_teachers"),
            func.count(func.distinct(Class.id)).label("total_classes"),
            func.count(func.distinct(
                case(
                    (Attendance.status == AttendanceStatus.PRESENT, Attendance.id),
                    else_=None
                )
            )).label("present_count"),
            func.count(func.distinct(
                case(
                    (Attendance.status == AttendanceStatus.ABSENT, Attendance.id),
                    else_=None
                )
            )).label("absent_count"),
            func.sum(Fee.amount_due).label("total_fees"),
            func.sum(Fee.amount_paid).label("total_fees_collected"),
            func.sum(Fee.discount).label("total_fees_discount")
        )
        .select_from(Student)
        .outerjoin(Staff, staff_join_cond)
        .outerjoin(Class, class_join_cond)
        .outerjoin(Attendance, and_(
            Attendance.school_id == school_id,
            Attendance.attendance_date == today
        ))
        .outerjoin(Fee, Fee.school_id == school_id)
        .where(*student_where)
    )

    row = stats_result.first()

    total_fees = row.total_fees or 0
    collected = row.total_fees_collected or 0
    fees_discount = row.total_fees_discount or 0
    # Net billable (due minus discount) — a discount means that portion was
    # never expected to be collected, so it shouldn't inflate "outstanding"
    # or deflate "collection_rate" below.
    net_fees = total_fees - fees_discount
    outstanding = net_fees - collected
    present = row.present_count or 0
    absent = row.absent_count or 0

    current_term_result = await session.execute(
        select(AcademicTerm).where(
            AcademicTerm.school_id == school_id,
            AcademicTerm.is_current == True
        )
    )
    has_current_term = current_term_result.scalar_one_or_none() is not None

    # Real-time revenue/profit — same shared GL-balance calculation
    # routers/finance_reports.py::get_dashboard_metrics uses (see
    # services/finance_dashboard_service.py), so this stays additive to the
    # existing billed/collected/outstanding fee figures above rather than a
    # second, possibly-divergent implementation. Never raises — an all-zero
    # snapshot just means the school hasn't posted any GL journal entries yet.
    financial_snapshot = await compute_financial_snapshot(session, school_id)

    return {
        "role": current_user.role,
        "campus_id": campus_id,
        "has_current_term": has_current_term,
        "stats": {
            "total_students": row.total_students or 0,
            "total_staff": row.total_staff or 0,
            "total_teachers": row.total_teachers or 0,
            "total_classes": row.total_classes or 0,
            "attendance_today": {
                "present": present,
                "absent": absent,
                "attendance_rate": round(present / (present + absent) * 100, 1) if (present + absent) > 0 else 0
            },
            "fees": {
                "total": total_fees,
                "collected": collected,
                "discount": fees_discount,
                "outstanding": outstanding,
                "collection_rate": round(collected / net_fees * 100, 1) if net_fees > 0 else 0
            },
            "finance": {
                "net_profit": financial_snapshot["net_profit"],
                "profit_margin": financial_snapshot["profit_margin"],
                "revenue_by_source": financial_snapshot["revenue_by_source"],
            }
        }
    }


@router.get("/class-summary", response_model=list[dict])
async def get_class_summary(
    campus_id: str | None = None,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Get summary for all classes"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    campus_id = resolve_campus_scope(current_user, campus_id)
    class_filters = [Class.school_id == school_id, Class.is_active == True]
    if campus_id:
        class_filters.append(Class.campus_id == campus_id)

    classes_result = await session.execute(
        select(Class).where(*class_filters).order_by(Class.level, Class.name)
    )
    classes = classes_result.scalars().all()
    
    summaries = []
    for cls in classes:
        student_count_result = await session.execute(
            select(func.count(Student.id)).where(
                Student.class_id == cls.id,
                Student.status == StudentStatus.ACTIVE
            )
        )
        student_count = student_count_result.scalar()
        
        today = date.today().isoformat()
        attendance_result = await session.execute(
            select(Attendance).where(
                Attendance.class_id == cls.id,
                Attendance.attendance_date == today
            )
        )
        attendance = attendance_result.scalars().all()
        present = sum(1 for a in attendance if a.status == AttendanceStatus.PRESENT)
        late = sum(1 for a in attendance if a.status == AttendanceStatus.LATE)

        summaries.append({
            "id": cls.id,
            "name": cls.name,
            "level": cls.level,
            "capacity": cls.capacity,
            "student_count": student_count,
            "attendance_today": present,
            "attendance_rate": round((present + late) / student_count * 100, 1) if student_count > 0 else 0
        })
    
    return summaries


@router.get("/gender-distribution", response_model=dict)
async def get_gender_distribution(
    campus_id: str | None = None,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Get student gender distribution"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    from models.student import Gender

    campus_id = resolve_campus_scope(current_user, campus_id)
    base_filters = [Student.school_id == school_id, Student.status == StudentStatus.ACTIVE]
    if campus_id:
        base_filters.append(Student.campus_id == campus_id)

    male_result = await session.execute(
        select(func.count(Student.id)).where(*base_filters, Student.gender == Gender.MALE)
    )
    male_count = male_result.scalar()

    female_result = await session.execute(
        select(func.count(Student.id)).where(*base_filters, Student.gender == Gender.FEMALE)
    )
    female_count = female_result.scalar()
    
    total = male_count + female_count
    
    return {
        "male": male_count,
        "female": female_count,
        "total": total,
        "male_percentage": round(male_count / total * 100, 1) if total > 0 else 0,
        "female_percentage": round(female_count / total * 100, 1) if total > 0 else 0
    }


@router.get("/super-admin/oversight", response_model=dict)
async def get_super_admin_oversight(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Get cross-school oversight data for super admin"""

    # Check role
    if current_user.role != UserRole.SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="Only super admins can access this endpoint")

    # 1. Get all schools
    schools_result = await session.execute(select(School))
    schools = schools_result.scalars().all()

    # 2. Get active student counts grouped by school_id
    students_result = await session.execute(
        select(Student.school_id, func.count(Student.id).label("count"))
        .where(Student.status == StudentStatus.ACTIVE)
        .group_by(Student.school_id)
    )
    students_by_school = {row.school_id: row.count for row in students_result.all()}

    # 3. Get active staff counts grouped by school_id
    staff_result = await session.execute(
        select(Staff.school_id, func.count(Staff.id).label("count"))
        .where(Staff.status == StaffStatus.ACTIVE)
        .group_by(Staff.school_id)
    )
    staff_by_school = {row.school_id: row.count for row in staff_result.all()}

    # 4. Get subscription sums grouped by school_id. Sums final_amount_due
    # (net of discount/late-fee/proration — what the school actually owes),
    # not total_amount_due (gross student_count x unit_price before any of
    # that) — otherwise a bulk-discounted school shows inflated outstanding
    # and an artificially low collection_rate below, the same bug fixed for
    # student fees in routers/fees.py.
    subscription_sums_result = await session.execute(
        select(
            PlatformSubscription.school_id,
            func.sum(PlatformSubscription.final_amount_due).label("total_due"),
            func.sum(PlatformSubscription.amount_paid).label("total_paid")
        )
        .group_by(PlatformSubscription.school_id)
    )
    subscription_sums = {
        row.school_id: (row.total_due or 0.0, row.total_paid or 0.0)
        for row in subscription_sums_result.all()
    }

    # 4b. Get lightweight subscription data to find latest per school
    all_subscriptions_result = await session.execute(
        select(
            PlatformSubscription.school_id,
            PlatformSubscription.status,
            PlatformSubscription.billing_plan,
            PlatformSubscription.billing_date
        )
        .order_by(PlatformSubscription.billing_date.desc())
    )
    all_subscriptions = all_subscriptions_result.all()

    # Find latest subscription per school by billing_date
    latest_subs = {}
    for row in all_subscriptions:
        if row.school_id not in latest_subs:
            latest_subs[row.school_id] = row

    # 5. Get overdue subscriptions (status PENDING or SUSPENDED with due_date < now)
    now = datetime.utcnow()
    overdue_result = await session.execute(
        select(PlatformSubscription)
        .where(
            PlatformSubscription.status.in_([SubscriptionStatus.PENDING, SubscriptionStatus.SUSPENDED]),
            PlatformSubscription.due_date < now
        )
    )
    overdue_subs = overdue_result.scalars().all()
    overdue_by_school = {sub.school_id: True for sub in overdue_subs}
    overdue_subscriptions_count = len(overdue_subs)
    # final_amount_due (net of discount/late-fee/proration), matching the
    # `outstanding` figure above — this was using total_amount_due (gross),
    # overstating "overdue" for any discounted/prorated subscription.
    overdue_amount = sum((s.final_amount_due - s.amount_paid) for s in overdue_subs)

    # 6. Get open ticket counts grouped by school_id
    tickets_result = await session.execute(
        select(Ticket.school_id, func.count(Ticket.id).label("count"))
        .where(Ticket.status.in_([TicketStatus.OPEN, TicketStatus.IN_PROGRESS]))
        .group_by(Ticket.school_id)
    )
    tickets_by_school = {row.school_id: row.count for row in tickets_result.all()}

    # Build school data
    schools_data = []
    total_students = 0
    total_staff = 0
    total_revenue_expected = 0.0
    total_revenue_collected = 0.0

    for school in schools:
        students = students_by_school.get(school.id, 0)
        staff = staff_by_school.get(school.id, 0)
        total_due, total_paid = subscription_sums.get(school.id, (0.0, 0.0))
        outstanding = max(total_due - total_paid, 0.0)

        # Get latest subscription for this school
        latest_sub = latest_subs.get(school.id)
        if latest_sub:
            # Handle enum conversions for status and billing_plan
            subscription_status = latest_sub.status.value if hasattr(latest_sub.status, 'value') else latest_sub.status
            billing_plan = latest_sub.billing_plan if isinstance(latest_sub.billing_plan, str) else (latest_sub.billing_plan.value if hasattr(latest_sub.billing_plan, 'value') else latest_sub.billing_plan)
        else:
            subscription_status = None
            billing_plan = None

        is_overdue = school.id in overdue_by_school
        open_tickets = tickets_by_school.get(school.id, 0)

        schools_data.append({
            "id": school.id,
            "name": school.name,
            "code": school.code,
            "city": school.city if school.city else None,
            "region": school.region if school.region else None,
            "is_active": school.is_active,
            "access_suspended": school.access_suspended,
            "created_at": school.created_at.isoformat(),
            "students": students,
            "staff": staff,
            "billing_plan": billing_plan,
            "subscription_status": subscription_status,
            "total_amount_due": total_due,
            "amount_paid": total_paid,
            "outstanding": outstanding,
            "overdue": is_overdue,
            "open_tickets": open_tickets
        })

        total_students += students
        total_staff += staff
        total_revenue_expected += total_due
        total_revenue_collected += total_paid

    # Sort by outstanding descending
    schools_data.sort(key=lambda x: x["outstanding"], reverse=True)

    # Calculate metrics
    collection_rate = round((total_revenue_collected / total_revenue_expected * 100), 1) if total_revenue_expected > 0 else 0.0
    active_schools = sum(1 for s in schools if s.is_active)

    return {
        "totals": {
            "schools": len(schools),
            "active_schools": active_schools,
            "students": total_students,
            "staff": total_staff,
            "revenue_expected": total_revenue_expected,
            "revenue_collected": total_revenue_collected,
            "collection_rate": collection_rate,
            "overdue_subscriptions": overdue_subscriptions_count,
            "overdue_amount": overdue_amount,
            "open_tickets": sum(tickets_by_school.values())
        },
        "schools": schools_data
    }
