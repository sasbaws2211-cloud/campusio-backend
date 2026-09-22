"""HR administration: benefits, conduct, exits, and workforce planning."""
from datetime import datetime
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_current_user, require_permission
from database import get_session
from dependencies import assert_campus_access
from models.hr_admin import (
    BenefitPlan, BenefitPlanCreate, BenefitPlanUpdate,
    StaffBenefit, StaffBenefitCreate, StaffDisciplinaryAction, StaffDisciplinaryActionCreate, StaffDisciplinaryActionUpdate,
    StaffGrievance, StaffGrievanceCreate, StaffGrievanceUpdate, StaffGrievanceStatus,
    StaffExit, StaffExitCreate, StaffExitInterview, StaffExitUpdate, WorkforcePlan, WorkforcePlanCreate,
)
from models.department import Department, DepartmentCreate, DepartmentUpdate
from models.succession_planning import SuccessionPlan, SuccessionPlanCreate, SuccessionPlanUpdate
from models.staff import Staff, StaffStatus, TeacherAssignment
from models.leave_request import LeaveBalance
from models.user import User

router = APIRouter(prefix="/hr/admin", tags=["HR Administration"])


def scope(user: User) -> str:
    if not user.school_id:
        raise HTTPException(status_code=403, detail="No school context")
    return user.school_id


async def check_staff(staff_id: str, user: User, session: AsyncSession):
    result = await session.execute(select(Staff).where(Staff.id == staff_id, Staff.school_id == scope(user)))
    staff = result.scalar_one_or_none()
    if not staff:
        raise HTTPException(status_code=404, detail="Staff member not found")
    assert_campus_access(user, staff.campus_id)


async def _own_staff_id(user: User, session: AsyncSession) -> str | None:
    """Mirrors routers/hr.py's helper of the same name — the caller's own
    linked Staff record, if any. None rather than raising, since a plain
    staff member (not just admin/HR) can hit the self-view/acknowledge
    endpoints below."""
    result = await session.execute(select(Staff).where(Staff.school_id == scope(user), Staff.user_id == user.id))
    staff = result.scalar_one_or_none()
    return staff.id if staff else None


@router.get("/benefits", response_model=list[dict])
async def list_benefits(user: User = Depends(require_permission("hr.benefit.create")), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(StaffBenefit).where(StaffBenefit.school_id == scope(user)).order_by(StaffBenefit.created_at.desc()))
    return [item.model_dump() for item in result.scalars().all()]


@router.post("/benefits", response_model=dict)
async def create_benefit(payload: StaffBenefitCreate, user: User = Depends(require_permission("hr.benefit.create")), session: AsyncSession = Depends(get_session)):
    await check_staff(payload.staff_id, user, session)
    item = StaffBenefit(school_id=scope(user), recorded_by=user.id, **payload.model_dump())
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item.model_dump()


@router.get("/disciplinary-actions", response_model=list[dict])
async def list_disciplinary_actions(user: User = Depends(require_permission("hr.disciplinary_action.create")), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(StaffDisciplinaryAction).where(StaffDisciplinaryAction.school_id == scope(user)).order_by(StaffDisciplinaryAction.incident_date.desc()))
    return [item.model_dump() for item in result.scalars().all()]


@router.get("/disciplinary-actions/mine", response_model=list[dict])
async def list_my_disciplinary_actions(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    """Previously a staff member had NO way to see their own disciplinary
    record via this API at all — both endpoints above are gated behind
    hr.disciplinary_action.create, a management-only permission."""
    own_id = await _own_staff_id(user, session)
    if not own_id:
        return []
    result = await session.execute(select(StaffDisciplinaryAction).where(StaffDisciplinaryAction.staff_id == own_id).order_by(StaffDisciplinaryAction.incident_date.desc()))
    return [item.model_dump() for item in result.scalars().all()]


@router.post("/disciplinary-actions", response_model=dict)
async def create_disciplinary_action(payload: StaffDisciplinaryActionCreate, user: User = Depends(require_permission("hr.disciplinary_action.create")), session: AsyncSession = Depends(get_session)):
    await check_staff(payload.staff_id, user, session)
    item = StaffDisciplinaryAction(school_id=scope(user), recorded_by=user.id, **payload.model_dump())
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item.model_dump()


@router.patch("/disciplinary-actions/{action_id}", response_model=dict)
async def update_disciplinary_action(
    action_id: str, payload: StaffDisciplinaryActionUpdate,
    user: User = Depends(require_permission("hr.disciplinary_action.create")), session: AsyncSession = Depends(get_session),
):
    """Previously there was no way to ever change a disciplinary action
    once created — no way to resolve it, escalate it, or record an
    outcome. A case just stayed "open" forever."""
    result = await session.execute(select(StaffDisciplinaryAction).where(StaffDisciplinaryAction.id == action_id, StaffDisciplinaryAction.school_id == scope(user)))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Disciplinary action not found")

    update_data = payload.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(item, key, value)
    if update_data.get("status") == "resolved" and not item.resolved_at:
        item.resolved_at = datetime.utcnow()
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item.model_dump()


@router.put("/disciplinary-actions/{action_id}/acknowledge", response_model=dict)
async def acknowledge_disciplinary_action(action_id: str, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    """The affected staff member confirms they've seen this record —
    mirrors routers/hr.py's acknowledge_review split (acknowledgment means
    the staff person actually saw it, distinct from HR marking a case
    resolved via the update endpoint above)."""
    result = await session.execute(select(StaffDisciplinaryAction).where(StaffDisciplinaryAction.id == action_id, StaffDisciplinaryAction.school_id == scope(user)))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Disciplinary action not found")
    own_id = await _own_staff_id(user, session)
    if not own_id or own_id != item.staff_id:
        raise HTTPException(status_code=403, detail="You can only acknowledge your own disciplinary record")

    item.acknowledged_at = datetime.utcnow()
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item.model_dump()


# ==================== Staff grievances ====================
# Previously there was NO channel anywhere for a staff member to raise a
# concern about a colleague, supervisor, or workplace issue — see
# models.hr_admin.StaffGrievance's docstring. Visibility is deliberately
# narrow: HR/admin (hr.grievance.manage) see everything; anyone else only
# ever sees what THEY submitted, never another staff member's grievance
# and never automatically the ones filed against them.

@router.post("/grievances", response_model=dict)
async def submit_grievance(payload: StaffGrievanceCreate, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    own_id = await _own_staff_id(user, session)
    if not own_id:
        raise HTTPException(status_code=403, detail="No staff record linked to your account")
    if payload.against_staff_id:
        await check_staff(payload.against_staff_id, user, session)
    item = StaffGrievance(school_id=scope(user), submitted_by_staff_id=own_id, **payload.model_dump())
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item.model_dump()


@router.get("/grievances", response_model=list[dict])
async def list_grievances(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    from services.permission_service import get_user_permissions
    query = select(StaffGrievance).where(StaffGrievance.school_id == scope(user))
    permissions = await get_user_permissions(session, user)
    if "hr.grievance.manage" not in permissions:
        own_id = await _own_staff_id(user, session)
        if not own_id:
            return []
        query = query.where(StaffGrievance.submitted_by_staff_id == own_id)
    result = await session.execute(query.order_by(StaffGrievance.created_at.desc()))
    return [item.model_dump() for item in result.scalars().all()]


@router.patch("/grievances/{grievance_id}", response_model=dict)
async def update_grievance(
    grievance_id: str, payload: StaffGrievanceUpdate,
    user: User = Depends(require_permission("hr.grievance.manage")), session: AsyncSession = Depends(get_session),
):
    result = await session.execute(select(StaffGrievance).where(StaffGrievance.id == grievance_id, StaffGrievance.school_id == scope(user)))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Grievance not found")

    update_data = payload.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(item, key, value)
    if update_data.get("status") == StaffGrievanceStatus.RESOLVED.value and not item.resolved_at:
        item.resolved_at = datetime.utcnow()
    item.updated_at = datetime.utcnow()
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item.model_dump()


@router.get("/exits", response_model=list[dict])
async def list_exits(user: User = Depends(require_permission("hr.exit.manage")), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(StaffExit).where(StaffExit.school_id == scope(user)).order_by(StaffExit.last_working_date.desc()))
    return [item.model_dump() for item in result.scalars().all()]


@router.post("/exits", response_model=dict)
async def create_exit(payload: StaffExitCreate, user: User = Depends(require_permission("hr.exit.manage")), session: AsyncSession = Depends(get_session)):
    await check_staff(payload.staff_id, user, session)
    item = StaffExit(school_id=scope(user), recorded_by=user.id, **payload.model_dump())
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item.model_dump()


def _resolve_exit_staff_status(item: StaffExit) -> StaffStatus:
    """StaffStatus only has RESIGNED/TERMINATED (no RETIRED/END_OF_CONTRACT
    value) — map the exit record's own categorization onto the closest of
    the two, defaulting to RESIGNED (the less severe implication) rather
    than guessing TERMINATED when the reason is ambiguous."""
    category = (item.primary_reason_category or "").lower()
    exit_type = (item.exit_type or "").lower()
    if category == "involuntary" or "terminat" in exit_type or "dismiss" in exit_type:
        return StaffStatus.TERMINATED
    return StaffStatus.RESIGNED


async def _finalize_staff_exit(item: StaffExit, user: User, session: AsyncSession, background_tasks: BackgroundTasks) -> dict:
    """Runs once, the moment clearance first completes (all three of
    assets/finance/hr_cleared flip true) — previously NOTHING in this
    codebase ever transitioned Staff.status to RESIGNED/TERMINATED,
    deactivated the departing staff member's User account, or flagged
    the class-teacher/department-head/manager/succession-plan assignments
    they leave behind. Returns a dict of what was flagged so HR sees it
    immediately, rather than the school discovering "the class has no
    teacher" weeks later. Never auto-reassigns anything — that's a human
    judgment call this only surfaces, doesn't make."""
    staff_result = await session.execute(select(Staff).where(Staff.id == item.staff_id, Staff.school_id == item.school_id))
    staff = staff_result.scalar_one_or_none()
    if not staff:
        return {}

    staff.status = _resolve_exit_staff_status(item)
    staff.updated_at = datetime.utcnow()
    session.add(staff)

    if staff.user_id:
        user_result = await session.execute(select(User).where(User.id == staff.user_id))
        exiting_user = user_result.scalar_one_or_none()
        if exiting_user:
            exiting_user.is_active = False
            session.add(exiting_user)

    from routers.timetable import get_current_term_id
    warnings = {}

    current_term_id = await get_current_term_id(session, item.school_id)
    if current_term_id:
        class_teacher_result = await session.execute(
            select(TeacherAssignment).where(
                TeacherAssignment.staff_id == staff.id, TeacherAssignment.academic_term_id == current_term_id,
                TeacherAssignment.is_class_teacher == True,  # noqa: E712
            )
        )
        class_teacher_rows = class_teacher_result.scalars().all()
        if class_teacher_rows:
            warnings["class_teacher_assignments"] = [{"class_id": r.class_id} for r in class_teacher_rows]

    dept_result = await session.execute(select(Department).where(Department.school_id == item.school_id, Department.head_staff_id == staff.id))
    dept_rows = dept_result.scalars().all()
    if dept_rows:
        warnings["department_head_of"] = [{"department_id": d.id, "name": d.name} for d in dept_rows]

    reports_result = await session.execute(select(Staff).where(Staff.school_id == item.school_id, Staff.manager_id == staff.id, Staff.status == StaffStatus.ACTIVE))
    reports_rows = reports_result.scalars().all()
    if reports_rows:
        warnings["direct_reports_needing_new_manager"] = [{"staff_id": s.id, "name": f"{s.first_name} {s.last_name}"} for s in reports_rows]

    succession_result = await session.execute(select(SuccessionPlan).where(SuccessionPlan.school_id == item.school_id, SuccessionPlan.current_holder_staff_id == staff.id))
    succession_rows = succession_result.scalars().all()
    if succession_rows:
        successor_ids = {p.successor_staff_id for p in succession_rows if p.successor_staff_id}
        successors_by_id = {}
        if successor_ids:
            successor_result = await session.execute(select(Staff).where(Staff.id.in_(successor_ids)))
            successors_by_id = {s.id: s for s in successor_result.scalars().all()}
        # Previously this only said "now vacant" — succession planning was
        # otherwise never consulted anywhere, including here, where its
        # whole purpose (who's lined up to step in) is most relevant.
        warnings["succession_plans_now_vacant"] = [
            {
                "plan_id": p.id, "position_title": p.position_title, "readiness": p.readiness,
                "successor_staff_id": p.successor_staff_id,
                "successor_name": (
                    f"{successors_by_id[p.successor_staff_id].first_name} {successors_by_id[p.successor_staff_id].last_name}"
                    if p.successor_staff_id in successors_by_id else None
                ),
            }
            for p in succession_rows
        ]

    current_year = datetime.utcnow().year
    balance_result = await session.execute(
        select(LeaveBalance).where(LeaveBalance.school_id == item.school_id, LeaveBalance.staff_id == staff.id, LeaveBalance.year == current_year)
    )
    remaining_balances = [
        {"leave_type": b.leave_type, "remaining_days": b.entitlement_days - b.used_days}
        for b in balance_result.scalars().all()
        if (b.entitlement_days - b.used_days) > 0
    ]
    if remaining_balances:
        warnings["remaining_leave_balance_needs_action"] = remaining_balances

    from services.webhook_service import emit_event
    await emit_event(
        session, background_tasks, item.school_id, "staff.exited",
        {"staff_id": staff.id, "status": staff.status.value, "last_working_date": item.last_working_date, "exit_type": item.exit_type},
    )

    return warnings


@router.patch("/exits/{exit_id}", response_model=dict)
async def update_exit(
    exit_id: str, payload: StaffExitUpdate, background_tasks: BackgroundTasks,
    user: User = Depends(require_permission("hr.exit.manage")), session: AsyncSession = Depends(get_session),
):
    result = await session.execute(select(StaffExit).where(StaffExit.id == exit_id, StaffExit.school_id == scope(user)))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Staff exit record not found")

    was_cleared = item.clearance_status == "cleared"
    update_data = payload.model_dump(exclude_unset=True)
    # finance_cleared has to mean something — a departing staff member with
    # a real outstanding loan/advance balance shouldn't be markable
    # "cleared" while that money is still owed and nothing has recovered
    # or written it off. See services/staff_loan_service.py::write_off_loan
    # for closing out a loan that won't be recovered from this staff member.
    settled_loans_summary = []
    if update_data.get("finance_cleared") is True:
        from models.staff_loan import StaffLoan, LoanStatus
        outstanding_result = await session.execute(
            select(StaffLoan).where(
                StaffLoan.school_id == scope(user),
                StaffLoan.staff_id == item.staff_id,
                StaffLoan.status == LoanStatus.ACTIVE,
                StaffLoan.outstanding_balance > 0,
            )
        )
        outstanding_loans = outstanding_result.scalars().all()
        if outstanding_loans:
            if update_data.get("settle_loans_from_final_pay"):
                # Recover the full outstanding balance from this staff
                # member's final settlement right now, instead of blocking
                # clearance — see StaffLoanService.settle_loan_at_exit.
                from services.staff_loan_service import StaffLoanService
                loan_service = StaffLoanService(session)
                for loan in outstanding_loans:
                    settle_result = await loan_service.settle_loan_at_exit(
                        school_id=scope(user), loan_id=loan.id, staff_exit_id=item.id, settled_by=user.id,
                    )
                    if not settle_result.get("success"):
                        raise HTTPException(status_code=400, detail=f"Could not settle loan {loan.id}: {settle_result.get('error')}")
                    settled_loans_summary.append({"loan_id": loan.id, "settled_amount": settle_result["settled_amount"]})
            else:
                total_outstanding = sum(l.outstanding_balance for l in outstanding_loans)
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Cannot mark finance clearance complete — {len(outstanding_loans)} outstanding loan(s) "
                        f"totaling {total_outstanding:.2f} for this staff member. Recover the balance (deduct from "
                        f"final settlement — pass settle_loans_from_final_pay=true) or write off the loan(s) first."
                    ),
                )

    update_data.pop("settle_loans_from_final_pay", None)
    for key, value in update_data.items():
        setattr(item, key, value)
    if item.assets_cleared and item.finance_cleared and item.hr_cleared:
        item.clearance_status = "cleared"
        item.settled_at = item.settled_at or datetime.utcnow()
    session.add(item)
    await session.commit()
    await session.refresh(item)

    exit_warnings = {}
    if item.clearance_status == "cleared" and not was_cleared:
        exit_warnings = await _finalize_staff_exit(item, user, session, background_tasks)
        await session.commit()

    response = item.model_dump()
    if exit_warnings:
        response["exit_warnings"] = exit_warnings
    if settled_loans_summary:
        response["settled_loans"] = settled_loans_summary
    return response


@router.post("/exits/{exit_id}/suggest-encashment", response_model=dict)
async def suggest_exit_encashment(
    exit_id: str, user: User = Depends(require_permission("hr.exit.manage")), session: AsyncSession = Depends(get_session),
):
    """Turns the `remaining_leave_balance_needs_action` warning
    (_finalize_staff_exit) into a one-click action: creates a real, still
    PENDING (never auto-approved — approval is a separate, deliberate
    step, same as any other leave encashment request) LeaveEncashmentRequest
    for this staff member's current-year unused ANNUAL balance. Previously
    the unused balance was only ever a warning HR had to act on manually
    through a completely separate screen."""
    from models.leave_request import LeaveBalance, LeaveType
    from models.leave_encashment import LeaveEncashmentCreate
    from services.leave_encashment_service import LeaveEncashmentService

    result = await session.execute(select(StaffExit).where(StaffExit.id == exit_id, StaffExit.school_id == scope(user)))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Staff exit record not found")

    year = datetime.utcnow().year
    balance_result = await session.execute(
        select(LeaveBalance).where(
            LeaveBalance.school_id == scope(user), LeaveBalance.staff_id == item.staff_id,
            LeaveBalance.leave_type == LeaveType.ANNUAL, LeaveBalance.year == year,
        )
    )
    balance = balance_result.scalar_one_or_none()
    remaining = (balance.entitlement_days - balance.used_days) if balance else 0
    if remaining <= 0:
        return {"created": False, "message": "No remaining annual leave balance to encash"}

    encashment_result = await LeaveEncashmentService(session).create_request(
        scope(user), LeaveEncashmentCreate(staff_id=item.staff_id, leave_days=remaining, reason=f"Exit encashment — {item.exit_type}"),
        requested_by=user.id,
    )
    if not encashment_result.get("success"):
        return {"created": False, "message": encashment_result.get("error")}
    return {"created": True, **encashment_result}


@router.get("/exits/{exit_id}/final-settlement-preview", response_model=dict)
async def preview_final_settlement(
    exit_id: str, user: User = Depends(require_permission("hr.exit.manage")), session: AsyncSession = Depends(get_session),
):
    """One read-only view of everything that makes up a departing staff
    member's final settlement — previously a prorated final paycheck,
    outstanding loan balance, and unused leave value were three completely
    separate things HR had to check on three separate screens with nothing
    tying them together. The three underlying ACTIONS stay separate and
    deliberate (loan settlement via PATCH .../exits/{id} with
    settle_loans_from_final_pay=true, encashment via
    POST .../suggest-encashment, the final paycheck itself via the normal
    payroll run that covers last_working_date) — this endpoint only
    previews what they'll add up to."""
    from models.staff_loan import StaffLoan, LoanStatus
    from models.leave_request import LeaveBalance, LeaveType
    from models.payroll import PayrollContract
    from services.payroll_service import PayrollService

    result = await session.execute(select(StaffExit).where(StaffExit.id == exit_id, StaffExit.school_id == scope(user)))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Staff exit record not found")

    loans_result = await session.execute(
        select(StaffLoan).where(
            StaffLoan.school_id == scope(user), StaffLoan.staff_id == item.staff_id,
            StaffLoan.status == LoanStatus.ACTIVE, StaffLoan.outstanding_balance > 0,
        )
    )
    outstanding_loans = loans_result.scalars().all()
    total_loan_outstanding = round(sum(l.outstanding_balance for l in outstanding_loans), 2)

    year = datetime.utcnow().year
    balance_result = await session.execute(
        select(LeaveBalance).where(
            LeaveBalance.school_id == scope(user), LeaveBalance.staff_id == item.staff_id,
            LeaveBalance.leave_type == LeaveType.ANNUAL, LeaveBalance.year == year,
        )
    )
    balance = balance_result.scalar_one_or_none()
    remaining_leave_days = (balance.entitlement_days - balance.used_days) if balance else 0

    contract_result = await session.execute(
        select(PayrollContract).where(PayrollContract.staff_id == item.staff_id, PayrollContract.is_active == True)  # noqa: E712
        .order_by(PayrollContract.effective_from.desc())
    )
    contract = contract_result.scalars().first()

    estimated_final_pay = None
    if contract:
        try:
            exit_date = datetime.strptime(item.last_working_date[:10], "%Y-%m-%d")
            service = PayrollService(session)
            fraction = service.calculate_proration_fraction(
                exit_date.year, exit_date.month, contract.effective_from.strftime("%Y-%m-%d"), item.last_working_date,
            )
            gross, _ = service.calculate_gross_amount(contract)
            deductions, _ = service.calculate_deductions(gross, contract)
            estimated_final_pay = round(max(gross - deductions, 0.0) * fraction, 2)
        except (ValueError, TypeError):
            pass

    return {
        "staff_id": item.staff_id,
        "last_working_date": item.last_working_date,
        "estimated_final_pay": estimated_final_pay,
        "estimated_final_pay_note": (
            "Estimate only — the real amount is whatever the payroll run covering "
            "last_working_date actually calculates, including any deduction rules/adjustments."
        ) if estimated_final_pay is not None else "No active payroll contract — cannot estimate",
        "outstanding_loans": [
            {"loan_id": l.id, "loan_type": l.loan_type, "outstanding_balance": l.outstanding_balance} for l in outstanding_loans
        ],
        "total_loan_outstanding": total_loan_outstanding,
        "remaining_annual_leave_days": remaining_leave_days,
        "clearance_status": item.clearance_status,
    }


@router.patch("/exits/{exit_id}/interview", response_model=dict)
async def record_exit_interview(exit_id: str, payload: StaffExitInterview, user: User = Depends(require_permission("hr.exit.manage")), session: AsyncSession = Depends(get_session)):
    """Separate from clearance — a retention/culture signal, doesn't gate
    or get gated by the asset/finance/HR clearance sign-off."""
    result = await session.execute(select(StaffExit).where(StaffExit.id == exit_id, StaffExit.school_id == scope(user)))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Staff exit record not found")
    item.exit_interview_completed = True
    item.exit_interview_date = payload.exit_interview_date
    item.exit_interview_notes = payload.exit_interview_notes
    item.would_recommend_employer = payload.would_recommend_employer
    item.primary_reason_category = payload.primary_reason_category
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item.model_dump()


# ==================== Benefit plan catalog ====================

@router.get("/benefit-plans", response_model=list[dict])
async def list_benefit_plans(active_only: bool = False, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    query = select(BenefitPlan).where(BenefitPlan.school_id == scope(user))
    if active_only:
        query = query.where(BenefitPlan.is_active == True)  # noqa: E712
    result = await session.execute(query.order_by(BenefitPlan.name))
    return [item.model_dump() for item in result.scalars().all()]


@router.post("/benefit-plans", response_model=dict)
async def create_benefit_plan(payload: BenefitPlanCreate, user: User = Depends(require_permission("hr.benefit_plan.manage")), session: AsyncSession = Depends(get_session)):
    item = BenefitPlan(school_id=scope(user), created_by=user.id, **payload.model_dump())
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item.model_dump()


@router.patch("/benefit-plans/{plan_id}", response_model=dict)
async def update_benefit_plan(plan_id: str, payload: BenefitPlanUpdate, user: User = Depends(require_permission("hr.benefit_plan.manage")), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(BenefitPlan).where(BenefitPlan.id == plan_id, BenefitPlan.school_id == scope(user)))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Benefit plan not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(item, key, value)
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item.model_dump()


# ==================== Departments & org chart ====================

@router.get("/departments", response_model=list[dict])
async def list_departments(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(Department).where(Department.school_id == scope(user)).order_by(Department.name))
    return [item.model_dump() for item in result.scalars().all()]


@router.post("/departments", response_model=dict)
async def create_department(payload: DepartmentCreate, user: User = Depends(require_permission("hr.department.manage")), session: AsyncSession = Depends(get_session)):
    if payload.head_staff_id:
        await check_staff(payload.head_staff_id, user, session)
    item = Department(school_id=scope(user), created_by=user.id, **payload.model_dump())
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item.model_dump()


@router.patch("/departments/{department_id}", response_model=dict)
async def update_department(department_id: str, payload: DepartmentUpdate, user: User = Depends(require_permission("hr.department.manage")), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(Department).where(Department.id == department_id, Department.school_id == scope(user)))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Department not found")
    if payload.head_staff_id:
        await check_staff(payload.head_staff_id, user, session)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(item, key, value)
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item.model_dump()


@router.get("/org-chart", response_model=dict)
async def get_org_chart(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    """A tree built purely from Staff.manager_id — no new hierarchy model
    needed since that field already exists and already drives performance-
    review manager visibility. Department heads (from the registry above)
    are attached to each node when a match exists; nodes with no
    manager_id (or whose manager isn't active) are returned as roots."""
    school_id_val = scope(user)
    staff_result = await session.execute(select(Staff).where(Staff.school_id == school_id_val, Staff.status == StaffStatus.ACTIVE))
    all_staff = {s.id: s for s in staff_result.scalars().all()}

    dept_result = await session.execute(select(Department).where(Department.school_id == school_id_val))
    dept_heads_by_name = {d.name: d.head_staff_id for d in dept_result.scalars().all()}

    def node(s: Staff) -> dict:
        return {
            "staff_id": s.id, "name": f"{s.first_name} {s.last_name}", "position": s.position,
            "department": s.department, "is_department_head": dept_heads_by_name.get(s.department) == s.id,
            "children": [],
        }

    nodes = {sid: node(s) for sid, s in all_staff.items()}
    roots = []
    for sid, s in all_staff.items():
        if s.manager_id and s.manager_id in nodes:
            nodes[s.manager_id]["children"].append(nodes[sid])
        else:
            roots.append(nodes[sid])

    return {"roots": roots, "unassigned_count": len(all_staff) - sum(len(n["children"]) for n in nodes.values())}


# ==================== Succession planning ====================

@router.get("/succession-plans", response_model=list[dict])
async def list_succession_plans(user: User = Depends(require_permission("hr.succession_plan.manage")), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(SuccessionPlan).where(SuccessionPlan.school_id == scope(user)).order_by(SuccessionPlan.position_title))
    return [item.model_dump() for item in result.scalars().all()]


@router.post("/succession-plans", response_model=dict)
async def create_succession_plan(payload: SuccessionPlanCreate, user: User = Depends(require_permission("hr.succession_plan.manage")), session: AsyncSession = Depends(get_session)):
    if payload.current_holder_staff_id:
        await check_staff(payload.current_holder_staff_id, user, session)
    if payload.successor_staff_id:
        await check_staff(payload.successor_staff_id, user, session)

    data = payload.model_dump()
    # Auto-derive from the actual holder's own record rather than trusting
    # a free-typed string that can drift from it — previously `department`
    # was whatever the caller typed, with nothing tying it back to
    # Staff.department (or the optional Department registry) at all.
    if not data.get("department") and payload.current_holder_staff_id:
        holder = (await session.execute(select(Staff).where(Staff.id == payload.current_holder_staff_id))).scalar_one_or_none()
        if holder and holder.department:
            data["department"] = holder.department

    item = SuccessionPlan(school_id=scope(user), created_by=user.id, **data)
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item.model_dump()


@router.patch("/succession-plans/{plan_id}", response_model=dict)
async def update_succession_plan(plan_id: str, payload: SuccessionPlanUpdate, user: User = Depends(require_permission("hr.succession_plan.manage")), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(SuccessionPlan).where(SuccessionPlan.id == plan_id, SuccessionPlan.school_id == scope(user)))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Succession plan not found")
    if payload.successor_staff_id:
        await check_staff(payload.successor_staff_id, user, session)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(item, key, value)
    item.updated_at = datetime.utcnow()
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item.model_dump()


@router.get("/workforce-plans", response_model=list[dict])
async def list_workforce_plans(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(WorkforcePlan).where(WorkforcePlan.school_id == scope(user)).order_by(WorkforcePlan.period.desc()))
    return [item.model_dump() for item in result.scalars().all()]


@router.post("/workforce-plans", response_model=dict)
async def create_workforce_plan(payload: WorkforcePlanCreate, user: User = Depends(require_permission("hr.workforce_plan.manage")), session: AsyncSession = Depends(get_session)):
    if payload.planned_positions < 0 or payload.current_positions < 0:
        raise HTTPException(status_code=422, detail="Position counts cannot be negative")
    item = WorkforcePlan(school_id=scope(user), created_by=user.id, **payload.model_dump())
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item.model_dump()


@router.patch("/workforce-plans/{plan_id}/sync", response_model=dict)
async def sync_workforce_plan(plan_id: str, user: User = Depends(require_permission("hr.workforce_plan.manage")), session: AsyncSession = Depends(get_session)):
    """Recomputes current_positions from live, active Staff in the plan's
    department — replaces whatever was last entered manually (or by a
    previous sync) rather than merging, since "current" only means
    something as of right now."""
    result = await session.execute(select(WorkforcePlan).where(WorkforcePlan.id == plan_id, WorkforcePlan.school_id == scope(user)))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Workforce plan not found")

    count_result = await session.execute(
        select(Staff).where(Staff.school_id == scope(user), Staff.status == StaffStatus.ACTIVE, Staff.department == item.department)
    )
    live_count = len(count_result.scalars().all())
    item.current_positions = live_count
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item.model_dump()