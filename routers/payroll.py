"""Payroll router for salary contracts and payroll management

ROLE-BASED ACCESS CONTROL:
- SUPER_ADMIN: Full access to all payroll operations
- SCHOOL_ADMIN: Full access including run approval and posting (for their school)
- HR: Can create/update contracts and generate runs (requires SCHOOL_ADMIN or SUPER_ADMIN for approval/posting)
- TEACHER/STUDENT/PARENT: Read-only access (can view own payslips)

SCHOOL SCOPING:
- All endpoints enforce school_id scoping for multi-tenancy
- SUPER_ADMIN can access any school; others limited to their school_id
"""
from fastapi import APIRouter, Depends, HTTPException, status, Query, UploadFile, File
from sqlmodel import SQLModel, select, func
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime, timedelta
from typing import Optional
import pandas as pd
import io

from models.payroll import (
    PayrollContract, PayrollContractCreate, PayrollContractUpdate,
    PayrollRun, PayrollRunCreate, PayrollLineItem, PayrollAdjustment,
    PayrollAdjustmentCreate, PayrollStatus, PayslipResponse, PayrollRunActionRequest,
    PayeBracket, PayeBracketCreate, PayeBracketUpdate,
    SalaryGrade, SalaryGradeCreate, SalaryGradeUpdate,
)
from models.staff import Staff, StaffStatus
from models.staff_loan import StaffLoan, StaffLoanCreate, LoanStatus, LoanWriteOffRequest
from models.leave_encashment import LeaveEncashmentRequest, LeaveEncashmentCreate, LeaveEncashmentStatus
from models.user import User, UserRole
from models.school import School
from database import get_session
from auth import get_current_user, require_permission
from dependencies import resolve_campus_scope, resolve_write_campus_id
from services.payroll_service import PayrollService
from services.staff_loan_service import StaffLoanService
from services.leave_encashment_service import LeaveEncashmentService
from services.statutory_export_service import generate_ssnit_csv, generate_bank_payment_csv, generate_paye_csv
from services.plan_gating import require_plan_feature

router = APIRouter(
    prefix="/payroll", tags=["Payroll"],
    dependencies=[Depends(require_plan_feature("payroll"))],
)


def _assert_deduction_rate_valid(tax_rate_percent: float, pension_rate_percent: float, nssf_rate_percent: float) -> None:
    """Same >100% guard the CSV import path already enforces (see import_contracts_csv)."""
    total_deduction_rate = tax_rate_percent + pension_rate_percent + nssf_rate_percent
    if total_deduction_rate > 100:
        raise HTTPException(
            status_code=400,
            detail=f"Total deduction rate cannot exceed 100% (current: {total_deduction_rate}%)"
        )


# Fields that change what a staff member was actually paid for a period —
# editing these in place on a contract that has ALREADY run through at
# least one payroll calculation silently rewrites pay history with no
# trace. /contracts/{id}/renew is the correct path once payroll has run:
# it deactivates the old contract and creates a dated successor, so the
# old terms stay on record exactly as they were when they applied.
_PAY_AFFECTING_CONTRACT_FIELDS = frozenset({
    "basic_salary", "pay_schedule", "allowance_housing", "allowance_transport",
    "allowance_meals", "allowance_utilities", "allowance_other", "extra_allowances",
    "tax_rate_percent", "tax_calculation_mode", "pension_rate_percent",
    "nssf_rate_percent", "employer_nssf_rate_percent", "nssf_tier2_rate_percent",
    "other_deduction", "extra_deductions", "standard_monthly_hours",
})


async def _assert_contract_edit_allowed(session: AsyncSession, contract: PayrollContract, update_data: dict) -> None:
    """Refuses an in-place edit to a pay-affecting field once payroll has
    ever actually been run for this staff member — see
    _PAY_AFFECTING_CONTRACT_FIELDS. A contract with no payroll history yet
    (e.g. corrected right after being created, before any run touches it)
    can still be freely edited in place."""
    touches_pay_fields = any(key in _PAY_AFFECTING_CONTRACT_FIELDS for key in update_data)
    if not touches_pay_fields:
        return
    existing_line_item = await session.execute(
        select(PayrollLineItem.id).where(PayrollLineItem.staff_id == contract.staff_id).limit(1)
    )
    if existing_line_item.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                "Payroll has already been run for this staff member — pay-affecting fields "
                "(salary, allowances, tax/pension/NSSF rates) can no longer be edited in place, "
                "since that would silently rewrite pay history. Use POST /contracts/{id}/renew "
                "instead, which versions the change with its own effective date."
            ),
        )


# ==================== PAYE Bracket Endpoints ====================
# A school's own progressive income-tax bands, consulted only by a
# PayrollContract with tax_calculation_mode="bracket" — see
# services/payroll_service.py::calculate_paye_from_brackets. Every contract
# left on the default "flat" mode is completely unaffected by this section.

@router.get("/paye-brackets", response_model=list[PayeBracket])
async def list_paye_brackets(
    year: int,
    current_user: User = Depends(require_permission("payroll.contract.manage")),
    session: AsyncSession = Depends(get_session),
):
    if not current_user.school_id:
        raise HTTPException(status_code=400, detail="No school context")
    result = await session.execute(
        select(PayeBracket).where(PayeBracket.school_id == current_user.school_id, PayeBracket.year == year).order_by(PayeBracket.sort_order)
    )
    return result.scalars().all()


@router.post("/paye-brackets", response_model=PayeBracket)
async def create_paye_bracket(
    payload: PayeBracketCreate,
    current_user: User = Depends(require_permission("payroll.contract.manage")),
    session: AsyncSession = Depends(get_session),
):
    if not current_user.school_id:
        raise HTTPException(status_code=400, detail="No school context")
    if payload.upper_bound is not None and payload.upper_bound <= payload.lower_bound:
        raise HTTPException(status_code=400, detail="upper_bound must be greater than lower_bound")
    item = PayeBracket(school_id=current_user.school_id, created_by=current_user.id, **payload.model_dump())
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item


@router.patch("/paye-brackets/{bracket_id}", response_model=PayeBracket)
async def update_paye_bracket(
    bracket_id: str, payload: PayeBracketUpdate,
    current_user: User = Depends(require_permission("payroll.contract.manage")),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(select(PayeBracket).where(PayeBracket.id == bracket_id, PayeBracket.school_id == current_user.school_id))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="PAYE bracket not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(item, key, value)
    item.updated_at = datetime.utcnow()
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item


@router.delete("/paye-brackets/{bracket_id}", status_code=204)
async def delete_paye_bracket(
    bracket_id: str,
    current_user: User = Depends(require_permission("payroll.contract.manage")),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(select(PayeBracket).where(PayeBracket.id == bracket_id, PayeBracket.school_id == current_user.school_id))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="PAYE bracket not found")
    await session.delete(item)
    await session.commit()


@router.post("/paye-brackets/seed-defaults", response_model=list[PayeBracket])
async def seed_default_paye_brackets(
    year: int,
    current_user: User = Depends(require_permission("payroll.contract.manage")),
    session: AsyncSession = Depends(get_session),
):
    """Seeds an illustrative starting set of Ghana-style monthly PAYE bands
    for `year` — a starting point to review and correct against the Ghana
    Revenue Authority's actual current published schedule, NOT a maintained
    statutory table (see PayrollService.default_paye_bracket_seed's
    docstring). No-ops (returns the existing rows) if this school already
    has bracket rows for this year, so it's safe to call more than once."""
    if not current_user.school_id:
        raise HTTPException(status_code=400, detail="No school context")
    existing = await session.execute(
        select(PayeBracket).where(PayeBracket.school_id == current_user.school_id, PayeBracket.year == year).order_by(PayeBracket.sort_order)
    )
    existing_rows = existing.scalars().all()
    if existing_rows:
        return existing_rows

    seeded = [
        PayeBracket(school_id=current_user.school_id, year=year, created_by=current_user.id, **band)
        for band in PayrollService.default_paye_bracket_seed()
    ]
    session.add_all(seeded)
    await session.commit()
    for row in seeded:
        await session.refresh(row)
    return seeded


# ==================== Salary Grades ====================
# A school's own reference salary structure (grade + step -> basic
# salary) — see models.payroll.SalaryGrade. Previously every contract's
# basic_salary was independently typed in with nothing to keep pay
# consistent/equitable across staff on the same grade.

@router.get("/salary-grades", response_model=list[dict])
async def list_salary_grades(
    active_only: bool = Query(True),
    current_user: User = Depends(require_permission("payroll.contract.view")),
    session: AsyncSession = Depends(get_session),
):
    if not current_user.school_id:
        raise HTTPException(status_code=400, detail="No school context")
    query = select(SalaryGrade).where(SalaryGrade.school_id == current_user.school_id)
    if active_only:
        query = query.where(SalaryGrade.is_active == True)  # noqa: E712
    result = await session.execute(query.order_by(SalaryGrade.grade_name, SalaryGrade.step))
    return [g.model_dump() for g in result.scalars().all()]


@router.post("/salary-grades", response_model=dict)
async def create_salary_grade(
    payload: SalaryGradeCreate,
    current_user: User = Depends(require_permission("payroll.contract.manage")),
    session: AsyncSession = Depends(get_session),
):
    if not current_user.school_id:
        raise HTTPException(status_code=400, detail="No school context")
    grade = SalaryGrade(school_id=current_user.school_id, created_by=current_user.id, **payload.model_dump())
    session.add(grade)
    await session.commit()
    await session.refresh(grade)
    return grade.model_dump()


@router.patch("/salary-grades/{grade_id}", response_model=dict)
async def update_salary_grade(
    grade_id: str, payload: SalaryGradeUpdate,
    current_user: User = Depends(require_permission("payroll.contract.manage")),
    session: AsyncSession = Depends(get_session),
):
    if not current_user.school_id:
        raise HTTPException(status_code=400, detail="No school context")
    result = await session.execute(select(SalaryGrade).where(SalaryGrade.id == grade_id, SalaryGrade.school_id == current_user.school_id))
    grade = result.scalar_one_or_none()
    if not grade:
        raise HTTPException(status_code=404, detail="Salary grade not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(grade, key, value)
    grade.updated_at = datetime.utcnow()
    session.add(grade)
    await session.commit()
    await session.refresh(grade)
    return grade.model_dump()


@router.delete("/salary-grades/{grade_id}", status_code=204)
async def delete_salary_grade(
    grade_id: str,
    current_user: User = Depends(require_permission("payroll.contract.manage")),
    session: AsyncSession = Depends(get_session),
):
    if not current_user.school_id:
        raise HTTPException(status_code=400, detail="No school context")
    result = await session.execute(select(SalaryGrade).where(SalaryGrade.id == grade_id, SalaryGrade.school_id == current_user.school_id))
    grade = result.scalar_one_or_none()
    if not grade:
        raise HTTPException(status_code=404, detail="Salary grade not found")
    # Soft-delete only — a contract may still reference this grade's id
    # purely for reporting (PayrollContract.salary_grade_id), and that
    # reference should keep resolving rather than pointing at nothing.
    grade.is_active = False
    grade.updated_at = datetime.utcnow()
    session.add(grade)
    await session.commit()


# ==================== Contract Endpoints ====================

@router.post("/contracts", response_model=dict)
async def create_payroll_contract(
    contract_data: PayrollContractCreate,
    current_user: User = Depends(require_permission("payroll.contract.manage")),
    session: AsyncSession = Depends(get_session)
):
    """Create a new payroll contract for a staff member"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=400, detail="No school context")
    
    # Verify staff exists
    result = await session.execute(
        select(Staff).where(
            Staff.id == contract_data.staff_id,
            Staff.school_id == school_id
        )
    )
    staff = result.scalar_one_or_none()
    if not staff:
        raise HTTPException(status_code=404, detail="Staff member not found")
    
    # Check for existing active contract
    result = await session.execute(
        select(PayrollContract).where(
            PayrollContract.school_id == school_id,
            PayrollContract.staff_id == contract_data.staff_id,
            PayrollContract.is_active == True
        )
    )
    existing = result.scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail="Active contract already exists for this staff member")

    _assert_deduction_rate_valid(
        contract_data.tax_rate_percent, contract_data.pension_rate_percent, contract_data.nssf_rate_percent
    )

    if contract_data.salary_grade_id:
        grade_result = await session.execute(
            select(SalaryGrade).where(SalaryGrade.id == contract_data.salary_grade_id, SalaryGrade.school_id == school_id)
        )
        if not grade_result.scalar_one_or_none():
            raise HTTPException(status_code=404, detail="Salary grade not found")

    # Create contract
    contract = PayrollContract(
        school_id=school_id,
        created_by=current_user.id,
        **contract_data.model_dump()
    )
    session.add(contract)
    await session.commit()
    await session.refresh(contract)

    return {
        "id": contract.id,
        "school_id": contract.school_id,
        "staff_id": contract.staff_id,
        "basic_salary": contract.basic_salary,
        "pay_schedule": contract.pay_schedule,
        "total_allowances": (contract.allowance_housing + contract.allowance_transport +
                            contract.allowance_meals + contract.allowance_utilities +
                            contract.allowance_other),
        "tax_rate_percent": contract.tax_rate_percent,
        "employer_nssf_rate_percent": contract.employer_nssf_rate_percent,
        "nssf_tier2_rate_percent": contract.nssf_tier2_rate_percent,
        "salary_grade_id": contract.salary_grade_id,
        "pension_rate_percent": contract.pension_rate_percent,
        "nssf_rate_percent": contract.nssf_rate_percent,
        "other_deduction": contract.other_deduction,
        "status": "active" if contract.is_active else "inactive",
        "created_at": contract.created_at.isoformat()
    }


@router.post("/contracts/import", response_model=dict)
async def import_contracts_csv(
    file: UploadFile = File(...),
    current_user: User = Depends(require_permission("payroll.contract.manage")),
    session: AsyncSession = Depends(get_session)
):
    """Import payroll contracts from CSV file
    
    CSV should have columns: staff_id, basic_salary, pay_schedule, allowance_housing, 
    allowance_transport, allowance_meals, allowance_utilities, allowance_other, 
    allowance_other_description, tax_rate_percent, pension_rate_percent, nssf_rate_percent, 
    other_deduction, other_deduction_description, effective_from, notes
    """
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=400, detail="No school context")
    
    if not file.filename.endswith('.csv'):
        raise HTTPException(status_code=400, detail="File must be a CSV file")
    
    try:
        # Read CSV file
        content = await file.read()
        df = pd.read_csv(io.BytesIO(content))
        
        # Required columns
        required_columns = [
            'staff_id', 'basic_salary', 'pay_schedule', 'effective_from'
        ]
        missing_columns = [col for col in required_columns if col not in df.columns]
        if missing_columns:
            raise ValueError(f"Missing required columns: {', '.join(missing_columns)}")
        
        success_count = 0
        errors = []
        
        for index, row in df.iterrows():
            try:
                staff_id = str(row['staff_id']).strip()
                if not staff_id:
                    errors.append(f"Row {index + 2}: staff_id cannot be empty")
                    continue
                
                # Verify staff exists by staff_id (staff code)
                staff_result = await session.execute(
                    select(Staff).where(
                        Staff.staff_id == staff_id,
                        Staff.school_id == school_id
                    )
                )
                staff = staff_result.scalar_one_or_none()
                if not staff:
                    errors.append(f"Row {index + 2}: Staff code '{staff_id}' not found")
                    continue
                
                # Check for existing active contract using the staff's UUID
                existing_result = await session.execute(
                    select(PayrollContract).where(
                        PayrollContract.school_id == school_id,
                        PayrollContract.staff_id == staff.id,
                        PayrollContract.is_active == True
                    )
                )
                if existing_result.scalar_one_or_none():
                    errors.append(f"Row {index + 2}: Active contract already exists for staff '{staff_id}'")
                    continue
                
                # Parse contract data with the staff's UUID
                contract_data = PayrollContractCreate(
                    staff_id=staff.id,
                    basic_salary=float(row['basic_salary']),
                    pay_schedule=str(row['pay_schedule']).strip().lower(),
                    allowance_housing=float(row.get('allowance_housing', 0) or 0),
                    allowance_transport=float(row.get('allowance_transport', 0) or 0),
                    allowance_meals=float(row.get('allowance_meals', 0) or 0),
                    allowance_utilities=float(row.get('allowance_utilities', 0) or 0),
                    allowance_other=float(row.get('allowance_other', 0) or 0),
                    allowance_other_description=str(row.get('allowance_other_description', '') or '').strip(),
                    tax_rate_percent=float(row.get('tax_rate_percent', 0) or 0),
                    pension_rate_percent=float(row.get('pension_rate_percent', 0) or 0),
                    nssf_rate_percent=float(row.get('nssf_rate_percent', 0) or 0),
                    other_deduction=float(row.get('other_deduction', 0) or 0),
                    other_deduction_description=str(row.get('other_deduction_description', '') or '').strip(),
                    effective_from=str(row['effective_from']).strip(),
                    notes=str(row.get('notes', '') or '').strip()
                )
                
                # Validate deduction rates don't exceed 100%
                total_deduction_rate = (
                    contract_data.tax_rate_percent + 
                    contract_data.pension_rate_percent + 
                    contract_data.nssf_rate_percent
                )
                if total_deduction_rate > 100:
                    errors.append(f"Row {index + 2}: Total deduction rate cannot exceed 100% (current: {total_deduction_rate}%)")
                    continue
                
                # Create contract
                contract = PayrollContract(
                    school_id=school_id,
                    created_by=current_user.id,
                    **contract_data.model_dump()
                )
                session.add(contract)
                success_count += 1
                
            except Exception as e:
                errors.append(f"Row {index + 2}: {str(e)}")
        
        await session.commit()
        
        message = f"Imported {success_count} contracts successfully"
        if errors:
            message = f"{message} with {len(errors)} errors"
        
        return {
            "success": len(errors) == 0,
            "message": message,
            "success_count": success_count,
            "errors": errors
        }
        
    except Exception as e:
        raise HTTPException(
            status_code=400, 
            detail=f"Import failed: {str(e)}"
        )


@router.get("/contracts", response_model=dict)
async def list_payroll_contracts(
    staff_id: Optional[str] = None,
    active_only: bool = Query(True),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """List payroll contracts with pagination"""
    school_id = current_user.school_id
    if not school_id and current_user.role != UserRole.SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="No school context")
    
    query = select(PayrollContract)
    count_query = select(func.count(PayrollContract.id))
    
    if school_id:
        query = query.where(PayrollContract.school_id == school_id)
        count_query = count_query.where(PayrollContract.school_id == school_id)
    
    if active_only:
        query = query.where(PayrollContract.is_active == True)
        count_query = count_query.where(PayrollContract.is_active == True)
    
    if staff_id:
        query = query.where(PayrollContract.staff_id == staff_id)
        count_query = count_query.where(PayrollContract.staff_id == staff_id)
    
    total_result = await session.execute(count_query)
    total = total_result.scalar()
    
    offset = (page - 1) * limit
    query = query.offset(offset).limit(limit).order_by(PayrollContract.created_at.desc())
    
    result = await session.execute(query)
    contracts = result.scalars().all()
    
    # Get staff names
    staff_ids = [c.staff_id for c in contracts]
    staff_map = {}
    if staff_ids:
        staff_result = await session.execute(
            select(Staff).where(Staff.id.in_(staff_ids))
        )
        for s in staff_result.scalars().all():
            staff_map[s.id] = f"{s.first_name} {s.last_name}"
    
    return {
        "items": [
            {
                "id": c.id,
                "staff_id": c.staff_id,
                "staff_name": staff_map.get(c.staff_id, "Unknown"),
                "basic_salary": c.basic_salary,
                "pay_schedule": c.pay_schedule,
                "total_allowances": (c.allowance_housing + c.allowance_transport + 
                                   c.allowance_meals + c.allowance_utilities + 
                                   c.allowance_other),
                "tax_rate_percent": c.tax_rate_percent,
                "pension_rate_percent": c.pension_rate_percent,
                "nssf_rate_percent": c.nssf_rate_percent,
                "other_deduction": c.other_deduction,
                "effective_from": c.effective_from.isoformat(),
                "effective_to": c.effective_to.isoformat() if c.effective_to else None,
                "status": "active" if c.is_active else "inactive",
                "created_at": c.created_at.isoformat()
            }
            for c in contracts
        ],
        "total": total,
        "page": page,
        "limit": limit,
        "pages": (total + limit - 1) // limit
    }


@router.get("/contracts/expiring", response_model=list[dict])
async def list_expiring_contracts(
    days: int = Query(30, ge=0, le=3650),
    current_user: User = Depends(require_permission("payroll.contract.view")),
    session: AsyncSession = Depends(get_session),
):
    """List active contracts already expired or ending within ``days``."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    cutoff = datetime.utcnow() + timedelta(days=days)
    result = await session.execute(
        select(PayrollContract, Staff)
        .join(Staff, Staff.id == PayrollContract.staff_id)
        .where(
            PayrollContract.school_id == school_id,
            PayrollContract.is_active == True,
            PayrollContract.effective_to != None,
            PayrollContract.effective_to <= cutoff,
        )
        .order_by(PayrollContract.effective_to)
    )
    return [
        {
            "id": contract.id,
            "staff_id": contract.staff_id,
            "staff_name": f"{staff.first_name} {staff.last_name}",
            "effective_from": contract.effective_from.isoformat(),
            "effective_to": contract.effective_to.isoformat(),
            "days_until_expiry": (contract.effective_to - datetime.utcnow()).days,
            "status": "expired" if contract.effective_to < datetime.utcnow() else "expiring",
        }
        for contract, staff in result.all()
    ]


class RenewContractRequest(SQLModel):
    effective_from: Optional[datetime] = None
    effective_to: Optional[datetime] = None
    basic_salary: Optional[float] = None
    notes: Optional[str] = None


@router.post("/contracts/{contract_id}/renew", response_model=dict)
async def renew_payroll_contract(
    contract_id: str,
    payload: RenewContractRequest,
    current_user: User = Depends(require_permission("payroll.contract.manage")),
    session: AsyncSession = Depends(get_session)
):
    """Renew an active (expiring or already-expired) contract in one step:
    deactivate the old one and create its successor carrying over the same
    salary/allowance/deduction terms, so a renewal doesn't require the
    caller to separately deactivate-then-recreate (the two-call sequence
    /contracts/{id} PATCH is_active=false then POST /contracts previously
    required, with no continuity between old and new)."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=400, detail="No school context")

    result = await session.execute(
        select(PayrollContract).where(
            PayrollContract.id == contract_id,
            PayrollContract.school_id == school_id,
        )
    )
    old = result.scalar_one_or_none()
    if not old:
        raise HTTPException(status_code=404, detail="Payroll contract not found")
    if not old.is_active:
        raise HTTPException(status_code=400, detail="Only an active contract can be renewed")

    new_effective_from = payload.effective_from or old.effective_to or datetime.utcnow()
    if payload.effective_to and payload.effective_to <= new_effective_from:
        raise HTTPException(status_code=400, detail="New effective_to must be after the new effective_from")

    old.is_active = False
    old.updated_at = datetime.utcnow()

    new_contract = PayrollContract(
        school_id=school_id,
        staff_id=old.staff_id,
        basic_salary=payload.basic_salary if payload.basic_salary is not None else old.basic_salary,
        pay_schedule=old.pay_schedule,
        currency=old.currency,
        allowance_housing=old.allowance_housing,
        allowance_transport=old.allowance_transport,
        allowance_meals=old.allowance_meals,
        allowance_utilities=old.allowance_utilities,
        allowance_other=old.allowance_other,
        allowance_other_description=old.allowance_other_description,
        tax_rate_percent=old.tax_rate_percent,
        pension_rate_percent=old.pension_rate_percent,
        nssf_rate_percent=old.nssf_rate_percent,
        other_deduction=old.other_deduction,
        other_deduction_description=old.other_deduction_description,
        effective_from=new_effective_from,
        effective_to=payload.effective_to,
        is_active=True,
        created_by=current_user.id,
        notes=payload.notes or f"Renewed from contract {old.id}",
    )
    session.add_all([old, new_contract])
    await session.commit()
    await session.refresh(new_contract)

    return {
        "id": new_contract.id,
        "renewed_from_contract_id": old.id,
        "staff_id": new_contract.staff_id,
        "basic_salary": new_contract.basic_salary,
        "effective_from": new_contract.effective_from.isoformat(),
        "effective_to": new_contract.effective_to.isoformat() if new_contract.effective_to else None,
        "status": "active",
        "created_at": new_contract.created_at.isoformat(),
    }


@router.get("/contracts/{contract_id}", response_model=dict)
async def get_payroll_contract(
    contract_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Get payroll contract details"""
    result = await session.execute(
        select(PayrollContract).where(PayrollContract.id == contract_id)
    )
    contract = result.scalar_one_or_none()
    
    if not contract:
        raise HTTPException(status_code=404, detail="Contract not found")
    
    if current_user.role != UserRole.SUPER_ADMIN and current_user.school_id != contract.school_id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Get staff details
    staff_result = await session.execute(
        select(Staff).where(Staff.id == contract.staff_id)
    )
    staff = staff_result.scalar_one_or_none()
    
    return {
        "id": contract.id,
        "staff_id": contract.staff_id,
        "staff_name": f"{staff.first_name} {staff.last_name}" if staff else "Unknown",
        "basic_salary": contract.basic_salary,
        "pay_schedule": contract.pay_schedule,
        "currency": contract.currency,
        "allowances": {
            "housing": contract.allowance_housing,
            "transport": contract.allowance_transport,
            "meals": contract.allowance_meals,
            "utilities": contract.allowance_utilities,
            "other": contract.allowance_other,
            "other_description": contract.allowance_other_description
        },
        "deductions": {
            "tax_rate_percent": contract.tax_rate_percent,
            "pension_rate_percent": contract.pension_rate_percent,
            "nssf_rate_percent": contract.nssf_rate_percent,
            "employer_nssf_rate_percent": contract.employer_nssf_rate_percent,
            "nssf_tier2_rate_percent": contract.nssf_tier2_rate_percent,
            "other_deduction": contract.other_deduction,
            "other_deduction_description": contract.other_deduction_description,
            "extra_deductions": contract.extra_deductions,
        },
        "extra_allowances": contract.extra_allowances,
        "salary_grade_id": contract.salary_grade_id,
        "effective_from": contract.effective_from.isoformat(),
        "effective_to": contract.effective_to.isoformat() if contract.effective_to else None,
        "status": "active" if contract.is_active else "inactive",
        "notes": contract.notes,
        "created_at": contract.created_at.isoformat(),
        "updated_at": contract.updated_at.isoformat()
    }


@router.put("/contracts/{contract_id}", response_model=dict)
async def update_payroll_contract(
    contract_id: str,
    contract_data: PayrollContractUpdate,
    current_user: User = Depends(require_permission("payroll.contract.manage")),
    session: AsyncSession = Depends(get_session)
):
    """Update payroll contract"""
    result = await session.execute(
        select(PayrollContract).where(PayrollContract.id == contract_id)
    )
    contract = result.scalar_one_or_none()
    
    if not contract:
        raise HTTPException(status_code=404, detail="Contract not found")
    
    if current_user.role != UserRole.SUPER_ADMIN and current_user.school_id != contract.school_id:
        raise HTTPException(status_code=403, detail="Access denied")

    # Update fields
    update_data = contract_data.model_dump(exclude_unset=True)
    await _assert_contract_edit_allowed(session, contract, update_data)
    for key, value in update_data.items():
        setattr(contract, key, value)

    _assert_deduction_rate_valid(
        contract.tax_rate_percent, contract.pension_rate_percent, contract.nssf_rate_percent
    )

    contract.updated_at = datetime.utcnow()
    session.add(contract)
    await session.commit()
    await session.refresh(contract)
    
    return {
        "id": contract.id,
        "staff_id": contract.staff_id,
        "basic_salary": contract.basic_salary,
        "pay_schedule": contract.pay_schedule,
        "total_allowances": (contract.allowance_housing + contract.allowance_transport + 
                            contract.allowance_meals + contract.allowance_utilities + 
                            contract.allowance_other),
        "tax_rate_percent": contract.tax_rate_percent,
        "pension_rate_percent": contract.pension_rate_percent,
        "nssf_rate_percent": contract.nssf_rate_percent,
        "other_deduction": contract.other_deduction,
        "status": "active" if contract.is_active else "inactive",
        "updated_at": contract.updated_at.isoformat()
    }


# ==================== Payroll Run Endpoints ====================

@router.post("/runs", response_model=dict)
async def generate_payroll_run(
    payroll_data: PayrollRunCreate,
    current_user: User = Depends(require_permission("payroll.run.generate")),
    session: AsyncSession = Depends(get_session)
):
    """Generate payroll run for a month"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=400, detail="No school context")
    
    service = PayrollService(session)
    result = await service.generate_payroll_run(
        school_id=school_id,
        year=payroll_data.period_year,
        month=payroll_data.period_month,
        current_user=current_user,
        pay_schedule=payroll_data.pay_schedule,
        notes=payroll_data.notes,
        campus_id=resolve_write_campus_id(current_user, payroll_data.campus_id),
    )
    
    # Return 400 only if no payroll was generated at all
    if result.get("success_count", 0) == 0:
        raise HTTPException(
            status_code=400,
            detail=result["message"],
            headers={"X-Errors": str(result.get("errors", [])[:3])}
        )
    
    return result


@router.get("/runs", response_model=dict)
async def list_payroll_runs(
    year: Optional[int] = None,
    month: Optional[int] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """List payroll runs with pagination"""
    school_id = current_user.school_id
    if not school_id and current_user.role != UserRole.SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="No school context")

    query = select(PayrollRun)
    count_query = select(func.count(PayrollRun.id))

    if school_id:
        query = query.where(PayrollRun.school_id == school_id)
        count_query = count_query.where(PayrollRun.school_id == school_id)

    scoped_campus_id = resolve_campus_scope(current_user)
    if scoped_campus_id:
        query = query.where(PayrollRun.campus_id == scoped_campus_id)
        count_query = count_query.where(PayrollRun.campus_id == scoped_campus_id)

    if year:
        query = query.where(PayrollRun.period_year == year)
        count_query = count_query.where(PayrollRun.period_year == year)
    
    if month:
        query = query.where(PayrollRun.period_month == month)
        count_query = count_query.where(PayrollRun.period_month == month)
    
    if status:
        query = query.where(PayrollRun.status == status)
        count_query = count_query.where(PayrollRun.status == status)
    
    total_result = await session.execute(count_query)
    total = total_result.scalar()
    
    offset = (page - 1) * limit
    query = query.offset(offset).limit(limit).order_by(PayrollRun.created_at.desc())
    
    result = await session.execute(query)
    runs = result.scalars().all()
    
    return {
        "items": [
            {
                "id": r.id,
                "period_name": r.period_name,
                "pay_schedule": r.pay_schedule,
                "status": r.status,
                "staff_count": r.staff_count,
                "total_gross": r.total_gross,
                "total_allowances": r.total_allowances,
                "total_deductions": r.total_deductions,
                "total_net": r.total_net,
                "created_at": r.created_at.isoformat()
            }
            for r in runs
        ],
        "total": total,
        "page": page,
        "limit": limit,
        "pages": (total + limit - 1) // limit
    }


@router.get("/runs/{run_id}", response_model=dict)
async def get_payroll_run(
    run_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Get payroll run details"""
    result = await session.execute(
        select(PayrollRun).where(PayrollRun.id == run_id)
    )
    run = result.scalar_one_or_none()
    
    if not run:
        raise HTTPException(status_code=404, detail="Payroll run not found")
    
    if current_user.role != UserRole.SUPER_ADMIN and current_user.school_id != run.school_id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    return {
        "id": run.id,
        "period_name": run.period_name,
        "period_year": run.period_year,
        "period_month": run.period_month,
        "pay_schedule": run.pay_schedule,
        "status": run.status,
        "staff_count": run.staff_count,
        "total_gross": run.total_gross,
        "total_allowances": run.total_allowances,
        "total_deductions": run.total_deductions,
        "total_net": run.total_net,
        "generated_by": run.generated_by,
        "approved_by": run.approved_by,
        "approved_at": run.approved_at.isoformat() if run.approved_at else None,
        "posted_at": run.posted_at.isoformat() if run.posted_at else None,
        "notes": run.notes,
        "created_at": run.created_at.isoformat(),
        "updated_at": run.updated_at.isoformat()
    }


@router.post("/runs/{run_id}/approve", response_model=dict)
async def approve_payroll_run(
    run_id: str,
    data: PayrollRunActionRequest = PayrollRunActionRequest(),
    self_approve_confirm: bool = Query(False, description="Approve a run you generated yourself — only honored when no other admin/HR user exists at this school to approve it instead"),
    current_user: User = Depends(require_permission("payroll.run.approve")),
    session: AsyncSession = Depends(get_session)
):
    """Approve payroll run (SCHOOL_ADMIN and HR only)"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=400, detail="No school context")

    service = PayrollService(session)
    result = await service.approve_payroll_run(
        school_id=school_id,
        payroll_run_id=run_id,
        current_user=current_user,
        notes=data.notes,
        self_approve_confirm=self_approve_confirm,
    )

    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["message"])

    return result


@router.post("/runs/{run_id}/reject", response_model=dict)
async def reject_payroll_run(
    run_id: str,
    data: PayrollRunActionRequest = PayrollRunActionRequest(),
    current_user: User = Depends(require_permission("payroll.run.approve")),
    session: AsyncSession = Depends(get_session)
):
    """Reject payroll run, sending it back for revision (SCHOOL_ADMIN and HR only)"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=400, detail="No school context")

    service = PayrollService(session)
    result = await service.reject_payroll_run(
        school_id=school_id,
        payroll_run_id=run_id,
        current_user=current_user,
        notes=data.notes
    )

    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["message"])

    return result


@router.post("/runs/{run_id}/post", response_model=dict)
async def post_payroll_run(
    run_id: str,
    current_user: User = Depends(require_permission("payroll.run.post")),
    session: AsyncSession = Depends(get_session)
):
    """Post payroll run (finalize - SUPER_ADMIN and SCHOOL_ADMIN only)"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=400, detail="No school context")
    
    service = PayrollService(session)
    result = await service.post_payroll_run(
        school_id=school_id,
        payroll_run_id=run_id,
        posted_by=current_user,
    )

    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["message"])

    return result


class VoidPayrollRunRequest(SQLModel):
    reason: str


@router.post("/runs/{run_id}/void", response_model=dict)
async def void_payroll_run(
    run_id: str,
    payload: VoidPayrollRunRequest,
    current_user: User = Depends(require_permission("payroll.run.post")),
    session: AsyncSession = Depends(get_session)
):
    """Void a POSTED run discovered to be wrong BEFORE any staff member has
    actually been paid — reverses the GL entry and marks the run VOIDED.
    Refused once disbursement has started for any line item (money has
    already moved by then). Same authority tier as posting itself."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=400, detail="No school context")

    service = PayrollService(session)
    result = await service.void_payroll_run(
        school_id=school_id, payroll_run_id=run_id, voided_by=current_user, reason=payload.reason,
    )

    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["message"])

    return result


@router.post("/runs/{run_id}/pay", response_model=dict)
async def disburse_payroll_run(
    run_id: str,
    current_user: User = Depends(require_permission("payroll.run.post")),
    session: AsyncSession = Depends(get_session)
):
    """Actually pay staff for a posted run via Paystack Transfers — separate
    from /post, which only books the GL liability. Safe to call again for a
    run that partially failed: it only retries line items still marked
    unpaid/failed, never double-pays one that already succeeded."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=400, detail="No school context")

    service = PayrollService(session)
    result = await service.disburse_payroll_run(
        school_id=school_id,
        payroll_run_id=run_id,
        disbursed_by=current_user,
    )

    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["message"])

    return result


# ==================== Payslip & Line Items ====================

@router.get("/runs/{run_id}/lines", response_model=dict)
async def get_payroll_line_items(
    run_id: str,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Get payroll line items for a run"""
    # Verify run exists
    result = await session.execute(
        select(PayrollRun).where(PayrollRun.id == run_id)
    )
    run = result.scalar_one_or_none()
    
    if not run:
        raise HTTPException(status_code=404, detail="Payroll run not found")
    
    if current_user.role != UserRole.SUPER_ADMIN and current_user.school_id != run.school_id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Get line items
    query = select(PayrollLineItem)
    count_query = select(func.count(PayrollLineItem.id))
    
    query = query.where(PayrollLineItem.payroll_run_id == run_id)
    count_query = count_query.where(PayrollLineItem.payroll_run_id == run_id)
    
    total_result = await session.execute(count_query)
    total = total_result.scalar()
    
    offset = (page - 1) * limit
    query = query.offset(offset).limit(limit).order_by(PayrollLineItem.created_at)
    
    result = await session.execute(query)
    line_items = result.scalars().all()
    
    # Get staff names
    staff_ids = [li.staff_id for li in line_items]
    staff_map = {}
    if staff_ids:
        staff_result = await session.execute(
            select(Staff).where(Staff.id.in_(staff_ids))
        )
        for s in staff_result.scalars().all():
            staff_map[s.id] = f"{s.first_name} {s.last_name}"
    
    return {
        "items": [
            {
                "id": li.id,
                "staff_id": li.staff_id,
                "staff_name": staff_map.get(li.staff_id, "Unknown"),
                "basic_salary": li.basic_salary,
                "total_allowances": li.total_allowances,
                "gross_amount": li.gross_amount,
                "tax_amount": li.tax_amount,
                "pension_amount": li.pension_amount,
                "nssf_amount": li.nssf_amount,
                "other_deductions": li.other_deductions,
                "total_deductions": li.total_deductions,
                "total_adjustments": li.total_adjustments,
                "net_amount": li.net_amount,
                "payment_status": li.payment_status,
                "paid_at": li.paid_at.isoformat() if li.paid_at else None,
                "transfer_reference": li.transfer_reference,
                "payment_failure_reason": li.payment_failure_reason,
            }
            for li in line_items
        ],
        "total": total,
        "page": page,
        "limit": limit,
        "pages": (total + limit - 1) // limit
    }


@router.get("/my-payslips", response_model=dict)
async def list_my_payslips(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Self-service payslip history for the logged-in staff member — the
    only view of payroll data that existed before this was the admin-only
    PayrollPage. Only shows POSTED (finalized) runs; a run still in
    draft/generated/approved hasn't had its numbers locked in yet."""
    staff_result = await session.execute(select(Staff).where(Staff.user_id == current_user.id))
    staff = staff_result.scalar_one_or_none()
    if not staff:
        raise HTTPException(status_code=404, detail="No staff profile linked to this account")

    lines_result = await session.execute(
        select(PayrollLineItem).where(PayrollLineItem.staff_id == staff.id).order_by(PayrollLineItem.created_at.desc())
    )
    line_items = lines_result.scalars().all()

    run_ids = [li.payroll_run_id for li in line_items]
    runs_map = {}
    if run_ids:
        runs_result = await session.execute(select(PayrollRun).where(PayrollRun.id.in_(run_ids)))
        for r in runs_result.scalars().all():
            runs_map[r.id] = r

    contract_result = await session.execute(
        select(PayrollContract).where(PayrollContract.staff_id == staff.id, PayrollContract.is_active == True)  # noqa: E712
        .order_by(PayrollContract.effective_from.desc())
    )
    contract = contract_result.scalars().first()
    currency = contract.currency if contract else "GHS"

    items = []
    for li in line_items:
        run = runs_map.get(li.payroll_run_id)
        if not run or run.status != PayrollStatus.POSTED:
            continue
        items.append({
            "id": li.id,
            "payroll_run_id": li.payroll_run_id,
            "period_name": run.period_name,
            "basic_salary": li.basic_salary,
            "total_allowances": li.total_allowances,
            "gross_amount": li.gross_amount,
            "tax_amount": li.tax_amount,
            "pension_amount": li.pension_amount,
            "nssf_amount": li.nssf_amount,
            "other_deductions": li.other_deductions,
            "total_deductions": li.total_deductions,
            "total_adjustments": li.total_adjustments,
            "net_amount": li.net_amount,
            "currency": currency,
            "payment_status": li.payment_status,
            "paid_at": li.paid_at.isoformat() if li.paid_at else None,
            "posted_at": run.posted_at.isoformat() if run.posted_at else None,
        })

    return {"items": items, "total": len(items)}


@router.get("/my-payslips/ytd", response_model=dict)
async def get_my_ytd_summary(
    year: int = Query(..., description="Calendar year, e.g. 2026"),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Self-service year-to-date earnings/tax summary for the logged-in
    staff member — previously the only aggregate like this
    (GET /payroll/annual-summary) was an admin-only CSV export across every
    staff member; a staff member had no way to get their own YTD figures
    (e.g. for a personal tax filing or a bank loan reference letter)
    without asking an admin to run that export for them."""
    staff_result = await session.execute(select(Staff).where(Staff.user_id == current_user.id))
    staff = staff_result.scalar_one_or_none()
    if not staff:
        raise HTTPException(status_code=404, detail="No staff profile linked to this account")

    service = PayrollService(session)
    return await service.get_ytd_summary_for_staff(staff.school_id, staff.id, year)


async def _check_payslip_access(
    run_id: str,
    staff_id: str,
    current_user: User,
    session: AsyncSession,
) -> PayrollRun:
    """Shared permission check for the JSON and PDF payslip endpoints:
    SUPER_ADMIN can view any payslip; SCHOOL_ADMIN/HR can view any payslip
    within their own school; anyone else may only view their own — matched
    by the linked Staff record's user_id, not by role (a plain role check
    would let any STUDENT/TEACHER through unconditionally)."""
    result = await session.execute(
        select(PayrollRun).where(PayrollRun.id == run_id)
    )
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Payroll run not found")

    staff_result = await session.execute(
        select(Staff).where(Staff.id == staff_id)
    )
    staff = staff_result.scalar_one_or_none()

    is_own_payslip = bool(staff and staff.user_id == current_user.id)
    is_school_admin = current_user.role in (UserRole.SCHOOL_ADMIN, UserRole.HR) and current_user.school_id == run.school_id
    if current_user.role != UserRole.SUPER_ADMIN and not is_school_admin and not is_own_payslip:
        raise HTTPException(status_code=403, detail="Access denied")

    return run


@router.get("/payslips/{run_id}/{staff_id}", response_model=dict)
async def get_payslip(
    run_id: str,
    staff_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Get payslip for a staff member in a payroll run"""
    run = await _check_payslip_access(run_id, staff_id, current_user, session)

    service = PayrollService(session)
    payslip = await service.get_payslip_data(run.school_id, run_id, staff_id)
    if not payslip:
        raise HTTPException(status_code=404, detail="Payslip not found")

    return payslip


@router.get("/payslips/{run_id}/{staff_id}/pdf")
async def get_payslip_pdf(
    run_id: str,
    staff_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Download a payslip as a PDF. Same access rules as the JSON payslip
    endpoint and the same underlying data (PayrollService.get_payslip_data),
    so the two never disagree."""
    from starlette.responses import StreamingResponse
    from services.payslip_pdf_service import PayslipPDFService

    run = await _check_payslip_access(run_id, staff_id, current_user, session)

    service = PayrollService(session)
    payslip = await service.get_payslip_data(run.school_id, run_id, staff_id)
    if not payslip:
        raise HTTPException(status_code=404, detail="Payslip not found")

    try:
        pdf_bytes = PayslipPDFService().generate_pdf(payslip)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to generate payslip PDF: {str(e)}")

    if not pdf_bytes:
        raise HTTPException(status_code=500, detail="Failed to generate PDF")

    safe_name = payslip["staff_name"].replace(" ", "_")
    safe_period = payslip["period_name"].replace(" ", "_")
    filename = f"payslip_{safe_name}_{safe_period}.pdf"

    return StreamingResponse(
        iter([pdf_bytes]),
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


# ==================== Payroll Adjustments ====================

@router.post("/adjustments", response_model=dict, status_code=201)
async def create_payroll_adjustment(
    data: PayrollAdjustmentCreate,
    current_user: User = Depends(require_permission("payroll.adjustment.create")),
    session: AsyncSession = Depends(get_session)
):
    """Create a pending bonus/penalty adjustment against a staff member's
    line item on a payroll run. Only allowed while the run is still
    draft/generated. Pending until a separate approval step applies it."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=400, detail="No school context")

    service = PayrollService(session)
    result = await service.create_adjustment(
        school_id=school_id,
        payroll_run_id=data.payroll_run_id,
        staff_id=data.staff_id,
        adjustment_type=data.adjustment_type,
        amount=data.amount,
        reason=data.reason,
        created_by=current_user.id,
    )

    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))

    return result


class BulkPayrollAdjustmentRequest(SQLModel):
    payroll_run_id: str
    staff_ids: list[str]
    adjustment_type: str  # e.g. "bonus", "back_pay", "penalty"
    amount: float  # Same flat amount for every staff_id — signed, same convention as PayrollAdjustmentCreate.amount
    reason: str


@router.post("/adjustments/bulk", response_model=dict, status_code=201)
async def create_bulk_payroll_adjustment(
    data: BulkPayrollAdjustmentRequest,
    current_user: User = Depends(require_permission("payroll.adjustment.create")),
    session: AsyncSession = Depends(get_session)
):
    """Create the same pending adjustment (e.g. an annual bonus, a back-pay
    correction after a rate change) against MANY staff members on one run
    at once — previously the only way to record something like a whole-
    school bonus was one POST /adjustments call per staff member. Each
    adjustment is created independently and still requires its own
    separate approval, same as a single adjustment."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=400, detail="No school context")
    if not data.staff_ids:
        raise HTTPException(status_code=400, detail="staff_ids cannot be empty")

    service = PayrollService(session)
    created = []
    failed = []
    for staff_id in data.staff_ids:
        result = await service.create_adjustment(
            school_id=school_id, payroll_run_id=data.payroll_run_id, staff_id=staff_id,
            adjustment_type=data.adjustment_type, amount=data.amount, reason=data.reason,
            created_by=current_user.id,
        )
        if result.get("success"):
            created.append({"staff_id": staff_id, "adjustment_id": result["adjustment_id"]})
        else:
            failed.append({"staff_id": staff_id, "error": result.get("error")})

    return {
        "created_count": len(created), "failed_count": len(failed),
        "created": created, "failed": failed,
    }


@router.get("/adjustments", response_model=dict)
async def list_payroll_adjustments(
    payroll_run_id: Optional[str] = None,
    staff_id: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """List payroll adjustments, optionally filtered by run and/or staff member."""
    school_id = current_user.school_id
    if not school_id and current_user.role != UserRole.SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="No school context")

    service = PayrollService(session)
    adjustments = await service.list_adjustments(
        school_id=school_id, payroll_run_id=payroll_run_id, staff_id=staff_id
    )

    staff_ids = [a.staff_id for a in adjustments]
    staff_map = {}
    if staff_ids:
        staff_result = await session.execute(select(Staff).where(Staff.id.in_(staff_ids)))
        for s in staff_result.scalars().all():
            staff_map[s.id] = f"{s.first_name} {s.last_name}"

    return {
        "items": [
            {
                "id": a.id,
                "payroll_run_id": a.payroll_run_id,
                "staff_id": a.staff_id,
                "staff_name": staff_map.get(a.staff_id, "Unknown"),
                "adjustment_type": a.adjustment_type,
                "amount": a.amount,
                "reason": a.reason,
                "created_by": a.created_by,
                "approved_by": a.approved_by,
                "approved_at": a.approved_at.isoformat() if a.approved_at else None,
                "status": "approved" if a.approved_by else "pending",
                "created_at": a.created_at.isoformat(),
            }
            for a in adjustments
        ],
        "total": len(adjustments),
    }


@router.post("/adjustments/{adjustment_id}/approve", response_model=dict)
async def approve_payroll_adjustment(
    adjustment_id: str,
    current_user: User = Depends(require_permission("payroll.adjustment.approve")),
    session: AsyncSession = Depends(get_session)
):
    """Approve a pending adjustment, immediately folding it into the
    staff member's line item net_amount and the run's total_net.
    Requires a step up from creation (SUPER_ADMIN/SCHOOL_ADMIN only, not
    HR) — matches the same authority gap as run generation vs approval."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=400, detail="No school context")

    service = PayrollService(session)
    result = await service.approve_adjustment(
        school_id=school_id, adjustment_id=adjustment_id, approved_by=current_user.id
    )

    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))

    return result


@router.delete("/adjustments/{adjustment_id}", response_model=dict)
async def delete_payroll_adjustment(
    adjustment_id: str,
    current_user: User = Depends(require_permission("payroll.adjustment.delete")),
    session: AsyncSession = Depends(get_session)
):
    """Retract a pending (not yet approved) adjustment."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=400, detail="No school context")

    service = PayrollService(session)
    result = await service.delete_adjustment(school_id=school_id, adjustment_id=adjustment_id)

    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))

    return result


# ==================== Staff Loans / Advances ====================

@router.post("/loans", response_model=dict, status_code=201)
async def create_staff_loan(
    data: StaffLoanCreate,
    current_user: User = Depends(require_permission("payroll.loan.create")),
    session: AsyncSession = Depends(get_session)
):
    """Request a staff loan/advance. Pending until approved — approval is
    what actually disburses it and posts the GL entry."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=400, detail="No school context")

    result = await StaffLoanService(session).create_loan(
        school_id=school_id, data=data, requested_by=current_user.id
    )
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@router.get("/loans", response_model=dict)
async def list_staff_loans(
    staff_id: Optional[str] = None,
    status: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """List staff loans/advances, optionally filtered by staff member and/or status."""
    school_id = current_user.school_id
    if not school_id and current_user.role != UserRole.SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="No school context")

    loans = await StaffLoanService(session).list_loans(
        school_id=school_id, staff_id=staff_id, status=LoanStatus(status) if status else None
    )

    staff_ids = [l.staff_id for l in loans]
    staff_map = {}
    if staff_ids:
        staff_result = await session.execute(select(Staff).where(Staff.id.in_(staff_ids)))
        for s in staff_result.scalars().all():
            staff_map[s.id] = f"{s.first_name} {s.last_name}"

    return {
        "items": [
            {
                "id": l.id,
                "staff_id": l.staff_id,
                "staff_name": staff_map.get(l.staff_id, "Unknown"),
                "loan_type": l.loan_type,
                "principal_amount": l.principal_amount,
                "installment_amount": l.installment_amount,
                "total_installments": l.total_installments,
                "installments_paid": l.installments_paid,
                "outstanding_balance": l.outstanding_balance,
                "status": l.status,
                "reason": l.reason,
                "start_period_year": l.start_period_year,
                "start_period_month": l.start_period_month,
                "created_at": l.created_at.isoformat(),
            }
            for l in loans
        ],
        "total": len(loans),
    }


@router.get("/loans/{loan_id}", response_model=dict)
async def get_staff_loan(
    loan_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Get a single staff loan/advance."""
    result = await session.execute(select(StaffLoan).where(StaffLoan.id == loan_id))
    loan = result.scalar_one_or_none()
    if not loan:
        raise HTTPException(status_code=404, detail="Loan not found")
    if current_user.role != UserRole.SUPER_ADMIN and current_user.school_id != loan.school_id:
        raise HTTPException(status_code=403, detail="Access denied")

    return {
        "id": loan.id,
        "school_id": loan.school_id,
        "staff_id": loan.staff_id,
        "loan_type": loan.loan_type,
        "principal_amount": loan.principal_amount,
        "installment_amount": loan.installment_amount,
        "total_installments": loan.total_installments,
        "installments_paid": loan.installments_paid,
        "outstanding_balance": loan.outstanding_balance,
        "status": loan.status,
        "reason": loan.reason,
        "start_period_year": loan.start_period_year,
        "start_period_month": loan.start_period_month,
        "approved_by": loan.approved_by,
        "approved_at": loan.approved_at.isoformat() if loan.approved_at else None,
        "disbursed_at": loan.disbursed_at.isoformat() if loan.disbursed_at else None,
        "created_at": loan.created_at.isoformat(),
    }


@router.post("/loans/{loan_id}/approve", response_model=dict)
async def approve_staff_loan(
    loan_id: str,
    current_user: User = Depends(require_permission("payroll.loan.approve")),
    session: AsyncSession = Depends(get_session)
):
    """Approve and disburse a pending loan/advance (SUPER_ADMIN and SCHOOL_ADMIN only,
    matching the same authority gap as payroll run/adjustment approval)."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=400, detail="No school context")

    result = await StaffLoanService(session).approve_loan(
        school_id=school_id, loan_id=loan_id, approved_by=current_user.id
    )
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@router.post("/loans/{loan_id}/reject", response_model=dict)
async def reject_staff_loan(
    loan_id: str,
    current_user: User = Depends(require_permission("payroll.loan.reject")),
    session: AsyncSession = Depends(get_session)
):
    """Reject a pending loan/advance request."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=400, detail="No school context")

    result = await StaffLoanService(session).reject_loan(
        school_id=school_id, loan_id=loan_id, rejected_by=current_user.id
    )
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@router.post("/loans/{loan_id}/write-off", response_model=dict)
async def write_off_staff_loan(
    loan_id: str,
    payload: LoanWriteOffRequest,
    current_user: User = Depends(require_permission("payroll.loan.approve")),
    session: AsyncSession = Depends(get_session)
):
    """Write off a loan's remaining outstanding balance — e.g. for a staff
    member exiting the school whose final settlement won't recover it.
    Reuses payroll.loan.approve (the same authority tier that can approve
    a disbursement in the first place); self-write-off is blocked the same
    way self-approval is."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=400, detail="No school context")

    result = await StaffLoanService(session).write_off_loan(
        school_id=school_id, loan_id=loan_id, written_off_by=current_user.id, reason=payload.reason
    )
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@router.get("/loans/{loan_id}/repayments", response_model=dict)
async def list_staff_loan_repayments(
    loan_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """List the repayment history for a loan/advance."""
    school_id = current_user.school_id
    if not school_id and current_user.role != UserRole.SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="No school context")

    service = StaffLoanService(session)
    loan = await service.get_loan(school_id, loan_id)
    if not loan:
        raise HTTPException(status_code=404, detail="Loan not found")

    repayments = await service.list_repayments(school_id, loan_id)
    return {
        "items": [
            {
                "id": r.id,
                "payroll_run_id": r.payroll_run_id,
                "installment_number": r.installment_number,
                "amount_paid": r.amount_paid,
                "balance_after": r.balance_after,
                "created_at": r.created_at.isoformat(),
            }
            for r in repayments
        ],
        "total": len(repayments),
    }


# ==================== Leave Encashment ====================

@router.post("/leave-encashments", response_model=dict, status_code=201)
async def create_leave_encashment(
    data: LeaveEncashmentCreate,
    current_user: User = Depends(require_permission("payroll.leave_encashment.create")),
    session: AsyncSession = Depends(get_session)
):
    """Request a leave encashment payout. leave_days is entered manually,
    but is validated against the staff member's ANNUAL LeaveBalance."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=400, detail="No school context")

    result = await LeaveEncashmentService(session).create_request(
        school_id=school_id, data=data, requested_by=current_user.id
    )
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@router.get("/leave-encashments", response_model=dict)
async def list_leave_encashments(
    staff_id: Optional[str] = None,
    status: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """List leave encashment requests, optionally filtered by staff member and/or status."""
    school_id = current_user.school_id
    if not school_id and current_user.role != UserRole.SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="No school context")

    requests = await LeaveEncashmentService(session).list_requests(
        school_id=school_id, staff_id=staff_id, status=LeaveEncashmentStatus(status) if status else None
    )

    staff_ids = [r.staff_id for r in requests]
    staff_map = {}
    if staff_ids:
        staff_result = await session.execute(select(Staff).where(Staff.id.in_(staff_ids)))
        for s in staff_result.scalars().all():
            staff_map[s.id] = f"{s.first_name} {s.last_name}"

    return {
        "items": [
            {
                "id": r.id,
                "staff_id": r.staff_id,
                "staff_name": staff_map.get(r.staff_id, "Unknown"),
                "leave_days": r.leave_days,
                "daily_rate": r.daily_rate,
                "encashment_amount": r.encashment_amount,
                "reason": r.reason,
                "status": r.status,
                "payroll_run_id": r.payroll_run_id,
                "created_at": r.created_at.isoformat(),
            }
            for r in requests
        ],
        "total": len(requests),
    }


@router.get("/leave-encashments/{request_id}", response_model=dict)
async def get_leave_encashment(
    request_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Get a single leave encashment request."""
    school_id = current_user.school_id
    if not school_id and current_user.role != UserRole.SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="No school context")

    request = await LeaveEncashmentService(session).get_request(school_id, request_id)
    if not request:
        raise HTTPException(status_code=404, detail="Request not found")
    if current_user.role != UserRole.SUPER_ADMIN and current_user.school_id != request.school_id:
        raise HTTPException(status_code=403, detail="Access denied")

    return {
        "id": request.id,
        "staff_id": request.staff_id,
        "leave_days": request.leave_days,
        "daily_rate": request.daily_rate,
        "encashment_amount": request.encashment_amount,
        "reason": request.reason,
        "status": request.status,
        "approved_by": request.approved_by,
        "approved_at": request.approved_at.isoformat() if request.approved_at else None,
        "payroll_run_id": request.payroll_run_id,
        "created_at": request.created_at.isoformat(),
    }


@router.post("/leave-encashments/{request_id}/approve", response_model=dict)
async def approve_leave_encashment(
    request_id: str,
    current_user: User = Depends(require_permission("payroll.leave_encashment.approve")),
    session: AsyncSession = Depends(get_session)
):
    """Approve a leave encashment request (SUPER_ADMIN and SCHOOL_ADMIN only).
    Does not pay it out — it's picked up automatically the next time a
    payroll run is generated for this staff member."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=400, detail="No school context")

    result = await LeaveEncashmentService(session).approve_request(
        school_id=school_id, request_id=request_id, approved_by=current_user.id
    )
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@router.post("/leave-encashments/{request_id}/reject", response_model=dict)
async def reject_leave_encashment(
    request_id: str,
    current_user: User = Depends(require_permission("payroll.leave_encashment.reject")),
    session: AsyncSession = Depends(get_session)
):
    """Reject a pending leave encashment request."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=400, detail="No school context")

    result = await LeaveEncashmentService(session).reject_request(
        school_id=school_id, request_id=request_id, rejected_by=current_user.id
    )
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


# ==================== Statutory Export (SSNIT) ====================

@router.get("/runs/{run_id}/ssnit-export")
async def export_ssnit_contributions(
    run_id: str,
    current_user: User = Depends(require_permission("payroll.export.manage")),
    session: AsyncSession = Depends(get_session)
):
    """Download a Ghana SSNIT contribution schedule CSV for a payroll run.
    Only available once the run's numbers are final (APPROVED or POSTED) —
    a DRAFT/GENERATED run's deductions can still change."""
    from starlette.responses import StreamingResponse

    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=400, detail="No school context")

    result = await session.execute(
        select(PayrollRun).where(PayrollRun.id == run_id, PayrollRun.school_id == school_id)
    )
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Payroll run not found")
    if run.status not in (PayrollStatus.APPROVED, PayrollStatus.POSTED):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot export a run in {run.status} status — it must be approved or posted first",
        )

    csv_content = await generate_ssnit_csv(session, school_id, run_id)
    if csv_content is None:
        raise HTTPException(status_code=404, detail="Payroll run not found")

    filename = f"ssnit_contributions_{run.period_name.replace(' ', '_')}.csv"
    return StreamingResponse(
        iter([csv_content]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@router.get("/runs/{run_id}/paye-export")
async def export_paye_withholding(
    run_id: str,
    current_user: User = Depends(require_permission("payroll.export.manage")),
    session: AsyncSession = Depends(get_session)
):
    """Download a PAYE (income tax) withholding schedule CSV for a payroll
    run — the GRA-remittance counterpart of the SSNIT export above. Same
    finality gate: only available once the run's numbers are final."""
    from starlette.responses import StreamingResponse

    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=400, detail="No school context")

    result = await session.execute(
        select(PayrollRun).where(PayrollRun.id == run_id, PayrollRun.school_id == school_id)
    )
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Payroll run not found")
    if run.status not in (PayrollStatus.APPROVED, PayrollStatus.POSTED):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot export a run in {run.status} status — it must be approved or posted first",
        )

    csv_content = await generate_paye_csv(session, school_id, run_id)
    if csv_content is None:
        raise HTTPException(status_code=404, detail="Payroll run not found")

    filename = f"paye_withholding_{run.period_name.replace(' ', '_')}.csv"
    return StreamingResponse(
        iter([csv_content]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@router.get("/runs/{run_id}/bank-payment-export")
async def export_bank_payment_file(
    run_id: str,
    current_user: User = Depends(require_permission("payroll.export.manage")),
    session: AsyncSession = Depends(get_session)
):
    """Download a generic bulk-payment CSV for a payroll run — the
    alternative to paying via the live Paystack transfer flow, for a
    finance officer taking the file to their own bank's bulk-upload
    portal instead. Same finality gate as the SSNIT export."""
    from starlette.responses import StreamingResponse

    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=400, detail="No school context")

    result = await session.execute(
        select(PayrollRun).where(PayrollRun.id == run_id, PayrollRun.school_id == school_id)
    )
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Payroll run not found")
    if run.status not in (PayrollStatus.APPROVED, PayrollStatus.POSTED):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot export a run in {run.status} status — it must be approved or posted first",
        )

    export = await generate_bank_payment_csv(session, school_id, run_id)
    if export is None:
        raise HTTPException(status_code=404, detail="Payroll run not found")

    filename = f"bank_payment_{run.period_name.replace(' ', '_')}.csv"
    return StreamingResponse(
        iter([export["csv"]]),
        media_type="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename={filename}",
            "X-Staff-Included": str(export["included"]),
            "X-Staff-Skipped": str(export["skipped_no_payout_details"]),
        }
    )


@router.get("/annual-summary")
async def export_annual_payroll_summary(
    year: int = Query(..., description="Calendar year, e.g. 2026"),
    current_user: User = Depends(require_permission("payroll.export.manage")),
    session: AsyncSession = Depends(get_session)
):
    """Year-end per-staff totals (gross/tax/pension/net) across every
    APPROVED or POSTED run whose period falls in the given year — the raw
    numbers behind any statutory year-end filing, without assuming one
    specific government form's exact layout (Ghana's GRA/SSNIT annual
    return formats aren't modeled here; this is the input data for
    whichever one applies)."""
    from starlette.responses import StreamingResponse
    import csv, io

    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=400, detail="No school context")

    runs_result = await session.execute(
        select(PayrollRun).where(
            PayrollRun.school_id == school_id,
            PayrollRun.status.in_([PayrollStatus.APPROVED, PayrollStatus.POSTED]),
        )
    )
    runs = [r for r in runs_result.scalars().all() if str(year) in (r.period_name or "")]
    run_ids = [r.id for r in runs]

    totals: dict[str, dict] = {}
    if run_ids:
        lines_result = await session.execute(
            select(PayrollLineItem).where(PayrollLineItem.payroll_run_id.in_(run_ids))
        )
        for li in lines_result.scalars().all():
            bucket = totals.setdefault(li.staff_id, {"gross": 0.0, "tax": 0.0, "pension": 0.0, "nssf": 0.0, "net": 0.0, "runs": 0})
            bucket["gross"] += float(li.gross_amount or 0.0)
            bucket["tax"] += float(li.tax_amount or 0.0)
            bucket["pension"] += float(li.pension_amount or 0.0)
            bucket["nssf"] += float(li.nssf_amount or 0.0)
            bucket["net"] += float(li.net_amount or 0.0)
            bucket["runs"] += 1

    staff_map = {}
    if totals:
        staff_result = await session.execute(select(Staff).where(Staff.id.in_(list(totals.keys()))))
        for s in staff_result.scalars().all():
            staff_map[s.id] = s

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([f"Annual Payroll Summary — {year}"])
    writer.writerow(["Runs included", len(runs)])
    writer.writerow([])
    writer.writerow(["Staff ID", "Staff Name", "SSNIT Number", "Pay Periods", "Gross", "Tax (PAYE)", "Pension", "SSNIT", "Net Paid"])
    for staff_id, t in totals.items():
        staff = staff_map.get(staff_id)
        writer.writerow([
            staff.staff_id if staff else staff_id,
            f"{staff.first_name} {staff.last_name}" if staff else "Unknown",
            (staff.ssnit_number if staff else "") or "Not set",
            t["runs"], f"{t['gross']:.2f}", f"{t['tax']:.2f}", f"{t['pension']:.2f}", f"{t['nssf']:.2f}", f"{t['net']:.2f}",
        ])

    filename = f"annual_payroll_summary_{year}.csv"
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )
