"""Executive/board-level reporting: a cross-module KPI rollup, plus the
strategic rollups this session's earlier audit found were captured as raw
data but never aggregated — staff turnover, alumni outcomes, PD ROI,
survey/NPS-style insight, multi-year cohort trends, predictive
capacity planning, payroll-vs-budget, asset utilization, and external
exam-board benchmarking. All read-only except the exam-benchmark
reference-data CRUD at the bottom (compliance/accreditation tracking has
its own router — see routers/compliance.py)."""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from auth import require_roles
from database import get_session
from models.alumni import AlumniRecord
from models.classroom import Class, Subject
from models.dashboard_layout import DashboardLayout, DashboardLayoutUpdate
from models.exam_board import ExamBoardRegistration, ExamBoardResult
from models.exam_board_benchmark import ExamBoardBenchmark, ExamBoardBenchmarkCreate, ExamBoardBenchmarkUpdate
from models.fee import Fee, PaymentStatus
from services.fee_reminder_service import is_fee_overdue
from models.finance.budget import Budget
from models.hr import StaffPerformanceReview
from models.hr_admin import StaffExit
from models.hr_development import StaffTraining
from models.hr_recruitment import StaffApplicant, StaffApplicantStatus, Vacancy
from models.inventory import Asset, AssetCondition, AssetStatus
from models.payroll import PayrollRun
from models.staff import Staff, StaffStatus
from models.student import Student, StudentStatus
from models.survey import Survey, SurveyResponse
from models.user import User, UserRole
from services.teacher_effectiveness_service import compute_teacher_effectiveness

router = APIRouter(prefix="/executive-reports", tags=["Executive Reports"])

STAFF_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.HR)

# ── Widget catalog for the customizable executive dashboard ─────────────
# Static catalog: every offerable widget key, its human label, which
# executive-reports endpoint/field it reads its value from (metric widgets),
# and the frontend route a click on the tile drills into. A "link" widget
# (no `field`) needs a required parameter its own endpoint can't default
# (e.g. a fiscal period, an exam name/year) — its tile just links through
# rather than trying to auto-fetch a value.
AVAILABLE_WIDGETS = {
    "active_students": {"label": "Active Students", "endpoint": "kpi-summary", "field": "active_students", "route": "/students"},
    "active_staff": {"label": "Active Staff", "endpoint": "kpi-summary", "field": "active_staff", "route": "/staff"},
    "fee_overdue_balance": {"label": "Fee Overdue Balance", "endpoint": "kpi-summary", "field": "fee_overdue_balance", "route": "/strategic-reports?tab=forecast"},
    "staff_attrition_rate": {"label": "Staff Attrition (12mo)", "endpoint": "kpi-summary", "field": "staff_attrition_rate_12mo", "route": "/executive-insights?tab=turnover"},
    "average_survey_rating": {"label": "Avg. Survey Rating", "endpoint": "kpi-summary", "field": "average_survey_rating", "route": "/executive-insights?tab=survey"},
    "total_alumni": {"label": "Total Alumni", "endpoint": "kpi-summary", "field": "total_alumni", "route": "/executive-insights?tab=alumni"},
    "workforce_net_change": {"label": "Workforce Net Change (12mo)", "endpoint": "workforce-analytics", "field": "net_change_in_window", "route": "/executive-insights?tab=workforce"},
    "pd_training_hours": {"label": "PD Training Hours (12mo)", "endpoint": "pd-roi", "field": "total_training_hours", "route": "/executive-insights?tab=pd-roi"},
    "capacity_utilization_pct": {"label": "Capacity Utilization", "endpoint": "capacity-forecast", "field": "current_utilization_pct", "route": "/executive-insights?tab=capacity"},
    "asset_utilization_pct": {"label": "Asset In-Use Rate", "endpoint": "asset-utilization", "field": "utilization_pct", "route": "/executive-insights?tab=assets"},
    "asset_replacement_due": {"label": "Assets Due for Replacement", "endpoint": "asset-utilization", "field": "replacement_due_count", "route": "/executive-insights?tab=assets"},
    "cohort_count": {"label": "Admission Cohorts Tracked", "endpoint": "cohort-trends", "field": "cohorts", "route": "/executive-insights?tab=cohorts"},
    "payroll_vs_budget": {"label": "Payroll vs Budget", "endpoint": "payroll-vs-budget", "field": None, "route": "/executive-insights?tab=payroll-budget"},
    "exam_benchmark": {"label": "Exam Benchmarking", "endpoint": "exam-benchmark-comparison", "field": None, "route": "/executive-insights?tab=exam-benchmark"},
}

DEFAULT_DASHBOARD_WIDGETS = [
    "active_students", "active_staff", "fee_overdue_balance",
    "staff_attrition_rate", "average_survey_rating", "total_alumni",
]


def _school_id(user: User) -> str:
    if not user.school_id:
        raise HTTPException(status_code=403, detail="No school context")
    return user.school_id


# ── Staff Turnover ───────────────────────────────────────────────────────

@router.get("/staff-turnover", response_model=dict)
async def staff_turnover(
    months: int = Query(12, le=36),
    current_user: User = Depends(require_roles(*STAFF_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    since = datetime.utcnow() - timedelta(days=months * 30)

    active_staff = (await session.execute(
        select(Staff).where(Staff.school_id == school_id, Staff.status == StaffStatus.ACTIVE.value)
    )).scalars().all()
    exits = (await session.execute(
        select(StaffExit).where(StaffExit.school_id == school_id, StaffExit.created_at >= since)
    )).scalars().all()

    total_ever = len(active_staff) + len(exits)
    attrition_rate = round(len(exits) / total_ever * 100, 1) if total_ever else 0

    today = date.today()
    buckets = {"<1 year": 0, "1-3 years": 0, "3-5 years": 0, "5-10 years": 0, "10+ years": 0}
    for s in active_staff:
        try:
            joined = date.fromisoformat(s.date_joined)
            years = (today - joined).days / 365.25
        except (ValueError, TypeError):
            continue
        if years < 1:
            buckets["<1 year"] += 1
        elif years < 3:
            buckets["1-3 years"] += 1
        elif years < 5:
            buckets["3-5 years"] += 1
        elif years < 10:
            buckets["5-10 years"] += 1
        else:
            buckets["10+ years"] += 1

    by_reason = defaultdict(int)
    by_exit_type = defaultdict(int)
    for e in exits:
        by_reason[e.reason or "unspecified"] += 1
        by_exit_type[e.exit_type] += 1

    return {
        "active_staff": len(active_staff),
        "exits_in_window": len(exits),
        "attrition_rate": attrition_rate,
        "tenure_distribution": buckets,
        "exits_by_reason": dict(by_reason),
        "exits_by_type": dict(by_exit_type),
    }


# ── Workforce Analytics (department breakdown, hires vs exits, time-to-fill) ──

@router.get("/workforce-analytics", response_model=dict)
async def workforce_analytics(
    months: int = Query(12, le=36),
    current_user: User = Depends(require_roles(*STAFF_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    since = datetime.utcnow() - timedelta(days=months * 30)

    active_staff = (await session.execute(
        select(Staff).where(Staff.school_id == school_id, Staff.status == StaffStatus.ACTIVE.value)
    )).scalars().all()

    by_department = defaultdict(int)
    for s in active_staff:
        by_department[s.department or "Unassigned"] += 1

    hires = (await session.execute(
        select(StaffApplicant).where(
            StaffApplicant.school_id == school_id, StaffApplicant.status == StaffApplicantStatus.HIRED,
            StaffApplicant.updated_at >= since,
        )
    )).scalars().all()
    exits = (await session.execute(
        select(StaffExit).where(StaffExit.school_id == school_id, StaffExit.created_at >= since)
    )).scalars().all()

    by_month_hires = defaultdict(int)
    for h in hires:
        by_month_hires[h.updated_at.strftime("%Y-%m")] += 1
    by_month_exits = defaultdict(int)
    for e in exits:
        by_month_exits[e.created_at.strftime("%Y-%m")] += 1
    months_seen = sorted(set(by_month_hires) | set(by_month_exits))
    net_change_by_month = [
        {"month": m, "hires": by_month_hires.get(m, 0), "exits": by_month_exits.get(m, 0), "net_change": by_month_hires.get(m, 0) - by_month_exits.get(m, 0)}
        for m in months_seen
    ]

    filled_vacancy_ids = {h.vacancy_id for h in hires if h.vacancy_id}
    vacancies = {}
    if filled_vacancy_ids:
        vacancies = {v.id: v for v in (await session.execute(select(Vacancy).where(Vacancy.id.in_(filled_vacancy_ids)))).scalars().all()}
    fill_days = []
    for h in hires:
        vacancy = vacancies.get(h.vacancy_id) if h.vacancy_id else None
        if vacancy:
            fill_days.append((h.updated_at - vacancy.created_at).days)
    avg_time_to_fill = round(sum(fill_days) / len(fill_days), 1) if fill_days else None

    return {
        "active_staff": len(active_staff),
        "by_department": dict(sorted(by_department.items(), key=lambda kv: -kv[1])),
        "hires_in_window": len(hires),
        "exits_in_window": len(exits),
        "net_change_in_window": len(hires) - len(exits),
        "net_change_by_month": net_change_by_month,
        "average_time_to_fill_days": avg_time_to_fill,
        "vacancies_used_for_time_to_fill": len(fill_days),
    }


# ── Alumni Outcomes ──────────────────────────────────────────────────────

@router.get("/alumni-outcomes", response_model=dict)
async def alumni_outcomes(current_user: User = Depends(require_roles(*STAFF_ROLES)), session: AsyncSession = Depends(get_session)):
    school_id = _school_id(current_user)
    alumni = (await session.execute(select(AlumniRecord).where(AlumniRecord.school_id == school_id))).scalars().all()

    by_year = defaultdict(lambda: {"total": 0, "with_institution": 0, "with_occupation": 0})
    occupation_counts = defaultdict(int)
    for a in alumni:
        bucket = by_year[a.graduation_year]
        bucket["total"] += 1
        if a.current_institution:
            bucket["with_institution"] += 1
        if a.current_occupation:
            bucket["with_occupation"] += 1
            occupation_counts[a.current_occupation] += 1

    by_year_rows = [
        {
            "graduation_year": year, "total": v["total"],
            "institution_placement_rate": round(v["with_institution"] / v["total"] * 100, 1) if v["total"] else 0,
            "employment_rate": round(v["with_occupation"] / v["total"] * 100, 1) if v["total"] else 0,
        }
        for year, v in sorted(by_year.items())
    ]
    top_occupations = sorted(occupation_counts.items(), key=lambda kv: -kv[1])[:10]

    return {
        "total_alumni": len(alumni),
        "by_graduation_year": by_year_rows,
        "top_occupations": [{"occupation": o, "count": c} for o, c in top_occupations],
    }


# ── Staff PD ROI ──────────────────────────────────────────────────────────

@router.get("/pd-roi", response_model=dict)
async def pd_roi(
    months: int = Query(12, le=36),
    current_user: User = Depends(require_roles(*STAFF_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    since = (date.today() - timedelta(days=months * 30)).isoformat()

    trainings = (await session.execute(
        select(StaffTraining).where(StaffTraining.school_id == school_id, StaffTraining.completion_date >= since)
    )).scalars().all()
    trained_staff_ids = {t.staff_id for t in trainings}
    total_hours = sum(t.hours or 0 for t in trainings)

    active_staff = (await session.execute(
        select(Staff.id).where(Staff.school_id == school_id, Staff.status == StaffStatus.ACTIVE.value)
    )).scalars().all()
    untrained_staff_ids = set(active_staff) - trained_staff_ids

    async def _avg_latest_rating(staff_ids: set) -> Optional[float]:
        if not staff_ids:
            return None
        reviews = (await session.execute(
            select(StaffPerformanceReview).where(StaffPerformanceReview.staff_id.in_(staff_ids), StaffPerformanceReview.overall_rating.is_not(None))
        )).scalars().all()
        latest_by_staff = {}
        for r in reviews:
            existing = latest_by_staff.get(r.staff_id)
            if not existing or r.created_at > existing.created_at:
                latest_by_staff[r.staff_id] = r
        ratings = [r.overall_rating for r in latest_by_staff.values()]
        return round(sum(ratings) / len(ratings), 2) if ratings else None

    return {
        "trainings_completed": len(trainings),
        "total_training_hours": total_hours,
        "staff_trained": len(trained_staff_ids),
        "avg_performance_rating_trained": await _avg_latest_rating(trained_staff_ids),
        "avg_performance_rating_untrained": await _avg_latest_rating(untrained_staff_ids),
    }


# ── Survey / NPS-style Insights ──────────────────────────────────────────

@router.get("/survey-insights", response_model=dict)
async def survey_insights(current_user: User = Depends(require_roles(*STAFF_ROLES)), session: AsyncSession = Depends(get_session)):
    school_id = _school_id(current_user)
    surveys = (await session.execute(select(Survey).where(Survey.school_id == school_id))).scalars().all()
    survey_type_by_id = {s.id: s.survey_type for s in surveys}
    responses = (await session.execute(select(SurveyResponse).where(SurveyResponse.school_id == school_id))).scalars().all()
    rated = [r for r in responses if r.rating is not None]

    by_month = defaultdict(list)
    by_type = defaultdict(list)
    for r in rated:
        by_month[r.submitted_at.strftime("%Y-%m")].append(r.rating)
        stype = survey_type_by_id.get(r.survey_id, "unknown")
        by_type[stype].append(r.rating)

    return {
        "total_responses": len(responses),
        "rated_responses": len(rated),
        "overall_average_rating": round(sum(r.rating for r in rated) / len(rated), 2) if rated else None,
        "average_rating_by_month": {m: round(sum(v) / len(v), 2) for m, v in sorted(by_month.items())},
        "average_rating_by_survey_type": {t: round(sum(v) / len(v), 2) for t, v in by_type.items()},
    }


# ── Multi-year Cohort Trends ──────────────────────────────────────────────

@router.get("/cohort-trends", response_model=dict)
async def cohort_trends(current_user: User = Depends(require_roles(*STAFF_ROLES)), session: AsyncSession = Depends(get_session)):
    school_id = _school_id(current_user)
    students = (await session.execute(select(Student).where(Student.school_id == school_id))).scalars().all()

    by_cohort = defaultdict(lambda: defaultdict(int))
    for s in students:
        year = s.admission_date[:4] if s.admission_date and len(s.admission_date) >= 4 else "unknown"
        by_cohort[year][s.status] += 1

    rows = []
    for year, statuses in sorted(by_cohort.items()):
        total = sum(statuses.values())
        rows.append({
            "admission_year": year, "total": total,
            "active": statuses.get(StudentStatus.ACTIVE.value, 0),
            "graduated": statuses.get(StudentStatus.GRADUATED.value, 0),
            "transferred": statuses.get(StudentStatus.TRANSFERRED.value, 0),
            "withdrawn": statuses.get(StudentStatus.WITHDRAWN.value, 0),
            "retention_rate": round((statuses.get(StudentStatus.ACTIVE.value, 0) + statuses.get(StudentStatus.GRADUATED.value, 0)) / total * 100, 1) if total else 0,
        })

    return {"cohorts": rows}


# ── Predictive Enrollment / Capacity Planning ────────────────────────────

@router.get("/capacity-forecast", response_model=dict)
async def capacity_forecast(
    terms_ahead: int = Query(2, le=8),
    current_user: User = Depends(require_roles(*STAFF_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    active_students = (await session.execute(
        select(func.count(Student.id)).where(Student.school_id == school_id, Student.status == StudentStatus.ACTIVE.value)
    )).scalar() or 0

    total_capacity = (await session.execute(
        select(func.coalesce(func.sum(Class.capacity), 0)).where(Class.school_id == school_id, Class.is_active == True)  # noqa: E712
    )).scalar() or 0

    one_year_ago = (date.today() - timedelta(days=365)).isoformat()
    new_last_year = (await session.execute(
        select(func.count(Student.id)).where(Student.school_id == school_id, Student.admission_date >= one_year_ago)
    )).scalar() or 0
    growth_rate = (new_last_year / active_students) if active_students else 0

    current_utilization = round(active_students / total_capacity * 100, 1) if total_capacity else None

    projections = []
    projected = float(active_students)
    for term in range(1, terms_ahead + 1):
        # ~3 terms/year, so apply a third of the annual growth rate per term
        projected *= (1 + growth_rate / 3)
        utilization = round(projected / total_capacity * 100, 1) if total_capacity else None
        projections.append({
            "term": term, "projected_students": round(projected),
            "projected_utilization_pct": utilization,
            "over_capacity": bool(total_capacity and projected > total_capacity),
        })

    return {
        "current_active_students": active_students,
        "total_capacity": total_capacity,
        "current_utilization_pct": current_utilization,
        "annual_growth_rate_pct": round(growth_rate * 100, 1),
        "projections": projections,
    }


# ── Payroll vs Budget ─────────────────────────────────────────────────────

@router.get("/payroll-vs-budget", response_model=dict)
async def payroll_vs_budget(
    fiscal_period_id: str,
    current_user: User = Depends(require_roles(*STAFF_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    from models.finance.fiscal_period import FiscalPeriod
    period = await session.get(FiscalPeriod, fiscal_period_id)
    if not period or period.school_id != school_id:
        raise HTTPException(status_code=404, detail="Fiscal period not found")

    total_budget = (await session.execute(
        select(func.coalesce(func.sum(Budget.budgeted_amount), 0)).where(Budget.school_id == school_id, Budget.fiscal_period_id == fiscal_period_id)
    )).scalar() or 0

    runs = (await session.execute(select(PayrollRun).where(PayrollRun.school_id == school_id))).scalars().all()
    matching_runs = []
    for r in runs:
        try:
            run_date = datetime(r.period_year, r.period_month, 1)
        except ValueError:
            continue
        if period.start_date <= run_date <= period.end_date:
            matching_runs.append(r)

    total_payroll = sum(r.total_net for r in matching_runs)
    payroll_pct_of_budget = round(float(total_payroll) / float(total_budget) * 100, 1) if total_budget else None

    return {
        "fiscal_period_id": fiscal_period_id,
        "total_budget": float(total_budget),
        "total_payroll_net": total_payroll,
        "payroll_pct_of_budget": payroll_pct_of_budget,
        "payroll_runs_counted": len(matching_runs),
    }


# ── Asset Utilization ─────────────────────────────────────────────────────

@router.get("/asset-utilization", response_model=dict)
async def asset_utilization(
    replacement_age_years: int = Query(5, le=30),
    current_user: User = Depends(require_roles(*STAFF_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    assets = (await session.execute(select(Asset).where(Asset.school_id == school_id))).scalars().all()
    total = len(assets)

    by_status = defaultdict(int)
    by_condition = defaultdict(int)
    for a in assets:
        by_status[a.status] += 1
        by_condition[a.condition] += 1

    cutoff = (date.today() - timedelta(days=replacement_age_years * 365)).isoformat()
    replacement_due = [
        a for a in assets
        if a.purchase_date and a.purchase_date <= cutoff and a.condition in (AssetCondition.POOR.value, AssetCondition.DAMAGED.value)
    ]
    estimated_replacement_cost = sum(a.purchase_cost or 0 for a in replacement_due)

    return {
        "total_assets": total,
        "utilization_pct": round(by_status.get(AssetStatus.IN_USE.value, 0) / total * 100, 1) if total else 0,
        "by_status": dict(by_status),
        "by_condition": dict(by_condition),
        "replacement_due_count": len(replacement_due),
        "estimated_replacement_cost": estimated_replacement_cost,
        "replacement_due_assets": [{"id": a.id, "name": a.name, "tag_number": a.tag_number, "purchase_date": a.purchase_date, "condition": a.condition} for a in replacement_due],
    }


# ── Exam Board Benchmarking ───────────────────────────────────────────────

def _benchmark_to_dict(b: ExamBoardBenchmark) -> dict:
    return {
        "id": b.id, "exam_name": b.exam_name, "exam_year": b.exam_year, "subject_id": b.subject_id,
        "national_average_score": b.national_average_score, "regional_average_score": b.regional_average_score,
        "source": b.source, "created_at": b.created_at,
    }


@router.post("/exam-benchmarks", response_model=dict)
async def create_benchmark(payload: ExamBoardBenchmarkCreate, current_user: User = Depends(require_roles(*STAFF_ROLES)), session: AsyncSession = Depends(get_session)):
    school_id = _school_id(current_user)
    item = ExamBoardBenchmark(school_id=school_id, created_by=current_user.id, **payload.model_dump())
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return _benchmark_to_dict(item)


@router.get("/exam-benchmarks", response_model=List[dict])
async def list_benchmarks(
    exam_name: Optional[str] = None, exam_year: Optional[str] = None,
    current_user: User = Depends(require_roles(*STAFF_ROLES)), session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    stmt = select(ExamBoardBenchmark).where(ExamBoardBenchmark.school_id == school_id)
    if exam_name:
        stmt = stmt.where(ExamBoardBenchmark.exam_name == exam_name)
    if exam_year:
        stmt = stmt.where(ExamBoardBenchmark.exam_year == exam_year)
    result = await session.execute(stmt.order_by(ExamBoardBenchmark.exam_year.desc()))
    return [_benchmark_to_dict(b) for b in result.scalars().all()]


@router.put("/exam-benchmarks/{benchmark_id}", response_model=dict)
async def update_benchmark(benchmark_id: str, payload: ExamBoardBenchmarkUpdate, current_user: User = Depends(require_roles(*STAFF_ROLES)), session: AsyncSession = Depends(get_session)):
    school_id = _school_id(current_user)
    item = (await session.execute(select(ExamBoardBenchmark).where(ExamBoardBenchmark.id == benchmark_id, ExamBoardBenchmark.school_id == school_id))).scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Benchmark not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(item, key, value)
    item.updated_at = datetime.utcnow()
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return _benchmark_to_dict(item)


@router.delete("/exam-benchmarks/{benchmark_id}", response_model=dict)
async def delete_benchmark(benchmark_id: str, current_user: User = Depends(require_roles(*STAFF_ROLES)), session: AsyncSession = Depends(get_session)):
    school_id = _school_id(current_user)
    item = (await session.execute(select(ExamBoardBenchmark).where(ExamBoardBenchmark.id == benchmark_id, ExamBoardBenchmark.school_id == school_id))).scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Benchmark not found")
    await session.delete(item)
    await session.commit()
    return {"success": True, "message": "Benchmark deleted"}


@router.get("/exam-benchmark-comparison", response_model=dict)
async def benchmark_comparison(
    exam_name: str, exam_year: str,
    current_user: User = Depends(require_roles(*STAFF_ROLES)), session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    registrations = (await session.execute(
        select(ExamBoardRegistration.id).where(ExamBoardRegistration.school_id == school_id, ExamBoardRegistration.exam_name == exam_name, ExamBoardRegistration.exam_year == exam_year)
    )).scalars().all()
    if not registrations:
        return {"subjects": []}

    results = (await session.execute(
        select(ExamBoardResult.subject_id, func.avg(ExamBoardResult.score)).where(
            ExamBoardResult.school_id == school_id, ExamBoardResult.registration_id.in_(registrations), ExamBoardResult.score.is_not(None),
        ).group_by(ExamBoardResult.subject_id)
    )).all()

    benchmarks = (await session.execute(
        select(ExamBoardBenchmark).where(ExamBoardBenchmark.school_id == school_id, ExamBoardBenchmark.exam_name == exam_name, ExamBoardBenchmark.exam_year == exam_year)
    )).scalars().all()
    benchmark_by_subject = {b.subject_id: b for b in benchmarks}

    subject_ids = [r[0] for r in results]
    subjects_map = {}
    if subject_ids:
        subjects_map = {s.id: s.name for s in (await session.execute(select(Subject).where(Subject.id.in_(subject_ids)))).scalars().all()}

    rows = []
    for subject_id, avg_score in results:
        benchmark = benchmark_by_subject.get(subject_id)
        rows.append({
            "subject_id": subject_id, "subject_name": subjects_map.get(subject_id, "Unknown"),
            "school_average_score": round(avg_score, 1),
            "national_average_score": benchmark.national_average_score if benchmark else None,
            "regional_average_score": benchmark.regional_average_score if benchmark else None,
            "vs_national": round(avg_score - benchmark.national_average_score, 1) if benchmark and benchmark.national_average_score is not None else None,
        })

    return {"exam_name": exam_name, "exam_year": exam_year, "subjects": rows}


# ── Executive KPI Summary ────────────────────────────────────────────────

@router.get("/kpi-summary", response_model=dict)
async def kpi_summary(current_user: User = Depends(require_roles(*STAFF_ROLES)), session: AsyncSession = Depends(get_session)):
    """One board-facing screen combining academics, finance, HR, and
    operations — the piece nothing else in this app rolls up together."""
    school_id = _school_id(current_user)

    active_students = (await session.execute(
        select(func.count(Student.id)).where(Student.school_id == school_id, Student.status == StudentStatus.ACTIVE.value)
    )).scalar() or 0

    active_staff = (await session.execute(
        select(func.count(Staff.id)).where(Staff.school_id == school_id, Staff.status == StaffStatus.ACTIVE.value)
    )).scalar() or 0

    # Computed from due-date math (services.fee_reminder_service.is_fee_overdue)
    # rather than Fee.status == OVERDUE, which is only ever set by a
    # narrow, opt-in nightly sweep or an unscheduled manual endpoint and
    # can silently disagree with whether a fee is actually overdue today.
    candidate_fees = (await session.execute(
        select(Fee).where(
            Fee.school_id == school_id,
            Fee.status.in_([PaymentStatus.PENDING.value, PaymentStatus.PARTIAL.value, PaymentStatus.OVERDUE.value]),
        )
    )).scalars().all()
    overdue_balance = 0.0
    for fee in candidate_fees:
        if await is_fee_overdue(session, fee):
            overdue_balance += fee.amount_due - fee.amount_paid - fee.discount

    since_12mo = datetime.utcnow() - timedelta(days=365)
    staff_exits_12mo = (await session.execute(
        select(func.count(StaffExit.id)).where(StaffExit.school_id == school_id, StaffExit.created_at >= since_12mo)
    )).scalar() or 0
    attrition_rate = round(staff_exits_12mo / (active_staff + staff_exits_12mo) * 100, 1) if (active_staff + staff_exits_12mo) else 0

    responses = (await session.execute(select(SurveyResponse).where(SurveyResponse.school_id == school_id))).scalars().all()
    rated = [r.rating for r in responses if r.rating is not None]
    avg_survey_rating = round(sum(rated) / len(rated), 2) if rated else None

    total_alumni = (await session.execute(select(func.count(AlumniRecord.id)).where(AlumniRecord.school_id == school_id))).scalar() or 0

    return {
        "active_students": active_students,
        "active_staff": active_staff,
        "fee_overdue_balance": round(overdue_balance, 2),
        "staff_attrition_rate_12mo": attrition_rate,
        "average_survey_rating": avg_survey_rating,
        "total_alumni": total_alumni,
        "generated_at": datetime.utcnow().isoformat(),
    }


# ── Customizable Executive Dashboard: widget catalog + saved layout ──────

@router.get("/available-widgets", response_model=dict)
async def available_widgets(current_user: User = Depends(require_roles(*STAFF_ROLES))):
    """Every widget the dashboard picker can offer — label, the endpoint/field
    it reads its value from (or None for a "link" widget with a required
    param this catalog can't default), and the frontend route it drills into."""
    return {"widgets": [{"key": key, **spec} for key, spec in AVAILABLE_WIDGETS.items()]}


@router.get("/dashboard-layout", response_model=dict)
async def get_dashboard_layout(current_user: User = Depends(require_roles(*STAFF_ROLES)), session: AsyncSession = Depends(get_session)):
    """The current user's saved widget layout, or the original fixed 6-tile
    set as a sensible default when nothing has been saved yet."""
    layout = (await session.execute(
        select(DashboardLayout).where(DashboardLayout.user_id == current_user.id)
    )).scalar_one_or_none()
    if not layout:
        return {"widgets": DEFAULT_DASHBOARD_WIDGETS, "is_default": True}
    try:
        widgets = json.loads(layout.widgets)
    except (TypeError, ValueError):
        widgets = DEFAULT_DASHBOARD_WIDGETS
    return {"widgets": widgets, "is_default": False, "updated_at": layout.updated_at}


@router.put("/dashboard-layout", response_model=dict)
async def save_dashboard_layout(
    payload: DashboardLayoutUpdate,
    current_user: User = Depends(require_roles(*STAFF_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    invalid = [w for w in payload.widgets if w not in AVAILABLE_WIDGETS]
    if invalid:
        raise HTTPException(status_code=422, detail=f"Unknown widget key(s): {invalid}")

    layout = (await session.execute(
        select(DashboardLayout).where(DashboardLayout.user_id == current_user.id)
    )).scalar_one_or_none()
    if layout:
        layout.widgets = json.dumps(payload.widgets)
        layout.updated_at = datetime.utcnow()
    else:
        layout = DashboardLayout(school_id=school_id, user_id=current_user.id, widgets=json.dumps(payload.widgets))
    session.add(layout)
    await session.commit()
    await session.refresh(layout)
    return {"widgets": json.loads(layout.widgets), "is_default": False, "updated_at": layout.updated_at}


# ── Teacher Effectiveness ─────────────────────────────────────────────────

@router.get("/teacher-effectiveness", response_model=dict)
async def teacher_effectiveness(
    academic_term_id: str,
    staff_id: Optional[str] = None,
    current_user: User = Depends(require_roles(*STAFF_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    """Objective composite score (grade improvement + attendance rate + PD
    completion — see services/teacher_effectiveness_service.py for the exact
    weighting) for every teacher with timetable entries in the given term.
    Deliberately excludes any subjective performance rating."""
    school_id = _school_id(current_user)
    return await compute_teacher_effectiveness(session, school_id, academic_term_id, staff_id)
