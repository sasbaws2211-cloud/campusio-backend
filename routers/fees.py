"""Fees router"""
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status, Query
from sqlmodel import select, func, and_, SQLModel
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime, timedelta, date
from typing import Optional, List
import logging

from models.fee import (
    Fee, FeeCreate, FeeStructure, FeeStructureCreate, FeePayment, FeePaymentCreate,
    VoidPaymentRequest, PaymentStatus, PaymentMethod, FeeType,
    FeeInstallment, FeeInstallmentPlanCreate, InstallmentStatus,
    DiscountType, FeeDiscountUpdate, FeeWriteOffRequest,
)
from services import fee_gl_service
from models.student import Student, StudentParent, Parent
from models.classroom import Class
from models.school import AcademicTerm, School
from models.user import User, UserRole
from models.finance import (
    JournalEntryCreate, JournalLineItemCreate, ReferenceType
)
from models.finance.chart_of_accounts import GLAccount
from database import get_session
from auth import get_current_user, require_roles
from services.fee_reminder_service import is_fee_overdue, _effective_due_date
from services.receipt_sequence_service import get_next_receipt_number
from dependencies import resolve_campus_scope, resolve_write_campus_id, assert_campus_access
from services.audit_service import log_event
from services.plan_gating import require_plan_feature

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/fees", tags=["Fees & Payments"])


class AssignFeeToClassRequest(SQLModel):
    structure_id: str
    class_id: str


def _prorate_fee_amount(amount: float, term_start: str, term_end: str, admission_date: str) -> float:
    """Scale a term fee down when the student was admitted after the term
    already started, so a student who joins in the last week of a term
    isn't charged the same amount as one enrolled since day one. Only
    called when School.prorate_fees_for_late_admission is on.

    Returns the full, unmodified amount whenever any date is missing/
    unparseable, or the student's admission_date isn't actually inside this
    term's [start_date, end_date] window (a continuing student's
    admission_date long predates the current term in the common case).
    """
    try:
        start = date.fromisoformat(term_start)
        end = date.fromisoformat(term_end)
        admitted = date.fromisoformat(admission_date)
    except (ValueError, TypeError):
        return amount

    if admitted <= start or admitted > end or end < start:
        return amount

    total_days = (end - start).days + 1
    remaining_days = (end - admitted).days + 1
    fraction = max(0.0, min(1.0, remaining_days / total_days))
    return round(amount * fraction, 2)


async def _has_fee_access(session: AsyncSession, current_user: User, student: Student) -> bool:
    """Ownership rules for viewing a specific student's fee/payment/receipt
    data: admins and teachers see any student in their school; parents only
    their own children; students only themselves. Previously every read
    endpoint below only checked same-school, not that a PARENT/STUDENT
    caller actually owns/is related to the student in question — any
    parent, student, teacher, or other same-school account could view any
    other student's fee balance, discounts, payment methods, and receipts.
    Same shape as grades.py::_has_report_card_access, applied here since
    fees.py never had an equivalent ownership check at all (contrast with
    routers/payments.py::verify_parent_fee_access, which does this correctly
    for the parent-portal payment-initiation flow but was never reused here)."""
    if current_user.role == UserRole.SUPER_ADMIN:
        return True
    if current_user.role in (UserRole.SCHOOL_ADMIN, UserRole.TEACHER):
        return current_user.school_id == student.school_id
    if current_user.role == UserRole.PARENT:
        parent_result = await session.execute(select(Parent).where(Parent.user_id == current_user.id))
        parent = parent_result.scalar_one_or_none()
        if not parent:
            return False
        sp_result = await session.execute(
            select(StudentParent).where(
                StudentParent.parent_id == parent.id,
                StudentParent.student_id == student.id
            )
        )
        return sp_result.scalar_one_or_none() is not None
    if current_user.role == UserRole.STUDENT:
        return current_user.id == student.user_id
    return False


async def _assert_fee_campus_access(session: AsyncSession, current_user: User, fee: Fee) -> None:
    """A campus-scoped admin (current_user.campus_id set) can only act on
    fees belonging to their own campus's students. create_student_fee and
    assign_fee_to_class already enforce this; apply_fee_discount,
    record_payment, void_payment, and create_installment_plan never did —
    a campus-scoped SCHOOL_ADMIN restricted to Campus A could apply a
    discount to, pay, void, or set up an installment plan for a fee
    belonging to a Campus B student in the same school, bypassing the
    campus boundary the rest of this module enforces for creation/
    assignment. No-op (no extra query) for a non-campus-scoped caller."""
    if not current_user.campus_id:
        return
    student_result = await session.execute(select(Student).where(Student.id == fee.student_id))
    student = student_result.scalar_one_or_none()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    assert_campus_access(current_user, student.campus_id)


# Fee Types with descriptions
FEE_TYPES_INFO = [
    {"type": "tuition", "name": "Tuition Fee", "description": "Main school fees for academic instruction"},
    {"type": "examination", "name": "Examination Fee", "description": "Fees for mid-term and end-of-term exams"},
    {"type": "sports", "name": "Sports Fee", "description": "Sports and physical education activities"},
    {"type": "ict", "name": "ICT Fee", "description": "Computer lab and IT resources"},
    {"type": "library", "name": "Library Fee", "description": "Library access and resources"},
    {"type": "maintenance", "name": "Maintenance Fee", "description": "School facilities maintenance"},
    {"type": "pta", "name": "PTA Levy", "description": "Parent-Teacher Association contributions"},
    {"type": "other", "name": "Other", "description": "Miscellaneous fees"},
]


@router.get("/types", response_model=List[dict])
async def get_fee_types():
    """Get all available fee types with descriptions"""
    return FEE_TYPES_INFO


@router.get("/aging", response_model=dict)
async def get_fee_aging_report(
    academic_term_id: Optional[str] = None,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Real 30/60/90/90+ days-overdue aging for actual outstanding student
    balances — previously the only aging-bucket machinery in this codebase
    (models.finance.subledger_reconciliation) only ever worked on manually
    hand-typed rows, never auto-fed from Fee, so a bursar had no tool to
    work an aging list without re-typing every balance by hand first."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    query = select(Fee).where(
        Fee.school_id == school_id,
        Fee.status.in_([PaymentStatus.PENDING, PaymentStatus.PARTIAL, PaymentStatus.OVERDUE])
    )
    if academic_term_id:
        query = query.where(Fee.academic_term_id == academic_term_id)
    campus_id = resolve_campus_scope(current_user)
    if campus_id:
        query = query.join(Student, Student.id == Fee.student_id).where(Student.campus_id == campus_id)

    fees = (await session.execute(query)).scalars().all()

    today = datetime.utcnow().date()
    buckets = {"current": [], "1_30": [], "31_60": [], "61_90": [], "over_90": []}
    for fee in fees:
        balance = fee.amount_due - fee.amount_paid - fee.discount
        if balance <= 0:
            continue
        due_date_str = await _effective_due_date(session, fee)
        days_overdue = 0
        if due_date_str:
            try:
                days_overdue = (today - datetime.fromisoformat(due_date_str).date()).days
            except ValueError:
                days_overdue = 0

        entry = {"fee_id": fee.id, "student_id": fee.student_id, "balance": round(balance, 2), "days_overdue": max(days_overdue, 0)}
        if days_overdue <= 0:
            buckets["current"].append(entry)
        elif days_overdue <= 30:
            buckets["1_30"].append(entry)
        elif days_overdue <= 60:
            buckets["31_60"].append(entry)
        elif days_overdue <= 90:
            buckets["61_90"].append(entry)
        else:
            buckets["over_90"].append(entry)

    return {
        bucket_name: {"count": len(entries), "total": round(sum(e["balance"] for e in entries), 2), "entries": entries}
        for bucket_name, entries in buckets.items()
    }


@router.get("/summary", response_model=dict)
async def get_fee_summary(
    academic_term_id: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Get fee collection summary for the school (outstanding fees only)"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    query = select(Fee).where(
        Fee.school_id == school_id,
        Fee.status.in_([PaymentStatus.PENDING, PaymentStatus.PARTIAL, PaymentStatus.OVERDUE])
    )
    if academic_term_id:
        query = query.where(Fee.academic_term_id == academic_term_id)

    # A campus-scoped admin only sees their own campus's totals here —
    # Fee has no campus_id of its own, so this joins to Student (whose
    # campus_id is the source of truth) rather than trusting anything on
    # the Fee row itself.
    campus_id = resolve_campus_scope(current_user)
    if campus_id:
        query = query.join(Student, Student.id == Fee.student_id).where(Student.campus_id == campus_id)

    result = await session.execute(query)
    fees = result.scalars().all()
    
    total_expected = sum(f.amount_due for f in fees)
    total_collected = sum(f.amount_paid for f in fees)
    total_discount = sum(f.discount for f in fees)
    total_outstanding = total_expected - total_collected - total_discount
    
    # Count by status. "overdue" is computed from due-date math
    # (services.fee_reminder_service.is_fee_overdue) rather than
    # Fee.status == OVERDUE — that status is only ever set by a narrow,
    # opt-in nightly sweep or an unscheduled manual endpoint, so it can't
    # be trusted as this summary's source of truth.
    overdue_count = 0
    for f in fees:
        if await is_fee_overdue(session, f):
            overdue_count += 1
    status_counts = {
        "paid": sum(1 for f in fees if f.status == PaymentStatus.PAID),
        "partial": sum(1 for f in fees if f.status == PaymentStatus.PARTIAL),
        "pending": sum(1 for f in fees if f.status == PaymentStatus.PENDING),
        "overdue": overdue_count,
    }
    
    # Divide by the net billable amount (expected minus discount), matching
    # total_outstanding above — a discount means that portion was never
    # expected to be collected, so it shouldn't count against the rate.
    net_expected = total_expected - total_discount
    collection_rate = round((total_collected / net_expected * 100) if net_expected > 0 else 0, 1)
    
    return {
        "total_expected": total_expected,
        "total_collected": total_collected,
        "total_discount": total_discount,
        "total_outstanding": total_outstanding,
        "collection_rate": collection_rate,
        "total_students_with_fees": len(set(f.student_id for f in fees)),
        "status_breakdown": status_counts
    }


@router.get("/outstanding", response_model=dict)
async def get_outstanding_fees(
    class_id: Optional[str] = None,
    min_amount: float = 0,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Get students with outstanding fees"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    # Get fees that are not fully paid
    query = select(Fee).where(
        Fee.school_id == school_id,
        Fee.status.in_([PaymentStatus.PENDING, PaymentStatus.PARTIAL, PaymentStatus.OVERDUE])
    )
    
    result = await session.execute(query)
    fees = result.scalars().all()
    
    # Group by student
    student_balances = {}
    for fee in fees:
        balance = fee.amount_due - fee.amount_paid - fee.discount
        if balance > min_amount:
            if fee.student_id not in student_balances:
                student_balances[fee.student_id] = {
                    "student_id": fee.student_id,
                    "total_due": 0,
                    "total_paid": 0,
                    "balance": 0,
                    "fee_count": 0
                }
            student_balances[fee.student_id]["total_due"] += fee.amount_due
            student_balances[fee.student_id]["total_paid"] += fee.amount_paid
            student_balances[fee.student_id]["balance"] += balance
            student_balances[fee.student_id]["fee_count"] += 1
    
    # Get student details
    student_ids = list(student_balances.keys())
    if student_ids:
        students_result = await session.execute(
            select(Student).where(Student.id.in_(student_ids))
        )
        students = {s.id: s for s in students_result.scalars().all()}
        
        # Get parent information - join StudentParent with Parent properly
        from sqlalchemy import join
        student_parents_result = await session.execute(
            select(StudentParent, Parent).where(
                StudentParent.student_id.in_(student_ids),
                StudentParent.parent_id == Parent.id
            )
        )
        student_parents_data = student_parents_result.all()
        
        # Map student to parent info (using first parent as primary)
        student_to_parent = {}
        for sp, parent in student_parents_data:
            if sp.student_id not in student_to_parent:
                student_to_parent[sp.student_id] = {
                    "parent_name": f"{parent.first_name} {parent.last_name}",
                    "parent_phone": parent.phone
                }
        
        # Filter by class/campus if specified — a campus-scoped admin only
        # sees their own campus's students (Fee has no campus_id itself).
        campus_id = resolve_campus_scope(current_user)
        for sid, data in student_balances.items():
            student = students.get(sid)
            if student:
                if class_id and student.class_id != class_id:
                    continue
                if campus_id and student.campus_id != campus_id:
                    continue
                data["student_name"] = f"{student.first_name} {student.last_name}"
                data["student_number"] = student.student_id
                data["class_id"] = student.class_id
                # Add parent info
                parent_info = student_to_parent.get(sid, {})
                data["parent_name"] = parent_info.get("parent_name", "Unknown")
                data["parent_phone"] = parent_info.get("parent_phone", "")
    
    outstanding_list = sorted(
        [v for v in student_balances.values() if "student_name" in v],
        key=lambda x: x["balance"],
        reverse=True
    )
    
    return {
        "total_students": len(outstanding_list),
        "total_outstanding": sum(s["balance"] for s in outstanding_list),
        "students": outstanding_list
    }


@router.post("/structures", response_model=dict)
async def create_fee_structure(
    structure_data: FeeStructureCreate,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    _plan_check: User = Depends(require_plan_feature("fees_plus")),
    session: AsyncSession = Depends(get_session)
):
    """Create a fee structure"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    structure_data.campus_id = resolve_write_campus_id(current_user, structure_data.campus_id)

    structure = FeeStructure(school_id=school_id, **structure_data.model_dump())
    session.add(structure)
    await session.commit()
    await session.refresh(structure)

    await log_event(
        session, actor=current_user, action="fee.structure_created", entity_type="fee_structure",
        entity_id=structure.id, school_id=school_id,
        summary=f"{current_user.email} created a {structure.fee_type} fee structure of GHS {structure.amount:.2f} for {structure.class_level}",
        new_values={"fee_type": structure.fee_type, "class_level": structure.class_level, "amount": structure.amount},
    )

    return {
        "id": structure.id,
        "academic_term_id": structure.academic_term_id,
        "class_level": structure.class_level,
        "campus_id": structure.campus_id,
        "fee_type": structure.fee_type,
        "amount": structure.amount,
        "due_date": structure.due_date,
        "message": "Fee structure created"
    }


@router.get("/structures", response_model=list[dict])
async def list_fee_structures(
    academic_term_id: Optional[str] = None,
    campus_id: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    _plan_check: User = Depends(require_plan_feature("fees_plus")),
    session: AsyncSession = Depends(get_session)
):
    """List fee structures"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    query = select(FeeStructure).where(FeeStructure.school_id == school_id)

    if academic_term_id:
        query = query.where(FeeStructure.academic_term_id == academic_term_id)

    campus_id = resolve_campus_scope(current_user, campus_id)
    if campus_id:
        # School-wide structures (campus_id is None) still apply everywhere,
        # so a campus-scoped view includes those alongside its own campus's.
        query = query.where((FeeStructure.campus_id == campus_id) | (FeeStructure.campus_id.is_(None)))

    result = await session.execute(query)
    structures = result.scalars().all()

    return [
        {
            "id": s.id,
            "academic_term_id": s.academic_term_id,
            "class_level": s.class_level,
            "campus_id": s.campus_id,
            "fee_type": s.fee_type,
            "amount": s.amount,
            "description": s.description,
            "is_mandatory": s.is_mandatory,
            "due_date": s.due_date
        }
        for s in structures
    ]


@router.post("", response_model=dict)
async def create_student_fee(
    fee_data: FeeCreate,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    _plan_check: User = Depends(require_plan_feature("fees_plus")),
    session: AsyncSession = Depends(get_session)
):
    """Create a fee record for a student

    amount_due and academic_term_id are derived from the referenced
    FeeStructure, not taken from the request body — fee_structure_id must
    resolve to a real, same-school structure (mirroring assign_fee_to_class's
    bulk-assignment path), closing off both a cross-tenant fabricated-id risk
    and the ability to set an arbitrary amount_due unrelated to any actual
    fee structure. The client-supplied academic_term_id/amount_due fields in
    the request body are accepted for backward compatibility but ignored.
    """
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    structure_result = await session.execute(
        select(FeeStructure).where(FeeStructure.id == fee_data.fee_structure_id, FeeStructure.school_id == school_id)
    )
    structure = structure_result.scalar_one_or_none()
    if not structure:
        raise HTTPException(status_code=404, detail="Fee structure not found")

    term = None
    if structure.academic_term_id:
        term_result = await session.execute(
            select(AcademicTerm).where(AcademicTerm.id == structure.academic_term_id, AcademicTerm.school_id == school_id)
        )
        term = term_result.scalar_one_or_none()
        if term and term.is_locked:
            raise HTTPException(status_code=423, detail="This academic term is locked and no longer accepts new fees")

    student_result = await session.execute(
        select(Student).where(and_(Student.id == fee_data.student_id, Student.school_id == school_id))
    )
    student = student_result.scalar_one_or_none()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    if current_user.campus_id:
        assert_campus_access(current_user, student.campus_id)
    # A campus-specific structure can't be applied to a student outside that
    # campus — a school-wide structure (campus_id is None) has no such
    # restriction. Same rule assign_fee_to_class already enforces.
    if structure.campus_id and structure.campus_id != student.campus_id:
        raise HTTPException(
            status_code=403,
            detail="This fee structure is scoped to a different campus than the student",
        )

    existing_result = await session.execute(
        select(Fee).where(
            Fee.student_id == fee_data.student_id,
            Fee.fee_structure_id == fee_data.fee_structure_id,
        )
    )
    if existing_result.scalars().first():
        raise HTTPException(
            status_code=409,
            detail="This fee structure is already assigned to this student",
        )

    amount_due = structure.amount
    if term and student.admission_date:
        school_result = await session.execute(select(School).where(School.id == school_id))
        school = school_result.scalar_one_or_none()
        if school and school.prorate_fees_for_late_admission:
            amount_due = _prorate_fee_amount(structure.amount, term.start_date, term.end_date, student.admission_date)

    if fee_data.discount > amount_due:
        raise HTTPException(status_code=400, detail="discount cannot exceed the fee amount due")

    fee = Fee(
        school_id=school_id,
        student_id=fee_data.student_id,
        academic_term_id=structure.academic_term_id,
        fee_structure_id=structure.id,
        amount_due=amount_due,
        discount=fee_data.discount,
        discount_type=fee_data.discount_type,
        discount_reason=fee_data.discount_reason,
    )
    session.add(fee)
    await session.commit()
    await session.refresh(fee)

    # Recognize revenue at invoice time (Dr Accounts Receivable / Cr
    # Revenue) — previously nothing posted to the GL until a payment
    # arrived, so GL 1100 (Accounts Receivable) was seeded but never used
    # and the GL's revenue figure could never be reconciled against a
    # bursar's "fees outstanding" figure. Best-effort: a GL failure here
    # must not block the fee record itself from existing.
    try:
        await fee_gl_service.post_fee_invoice(session, school_id, fee, structure, fee.amount_due - fee.discount)
        await session.commit()
    except Exception as e:
        logger.error(f"Error posting invoice journal entry for fee {fee.id}: {str(e)}")

    from services.webhook_service import emit_event
    await emit_event(
        session, background_tasks, school_id, "fee.invoice.created",
        {
            "id": fee.id,
            "student_id": fee.student_id,
            "amount_due": fee.amount_due,
            "status": fee.status,
        },
    )

    return {
        "id": fee.id,
        "student_id": fee.student_id,
        "amount_due": fee.amount_due,
        "status": fee.status,
        "message": "Fee created"
    }


@router.post("/{fee_id}/installments", response_model=dict)
async def create_installment_plan(
    fee_id: str,
    plan: FeeInstallmentPlanCreate,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    _plan_check: User = Depends(require_plan_feature("fees_plus")),
    session: AsyncSession = Depends(get_session)
):
    """Split a fee's amount_due into a scheduled installment plan (e.g. termly
    split payments). Only allowed before any payment has been recorded
    against the fee — once money has moved, the schedule can't be
    retroactively rewritten without reconciling existing payments against it.
    """
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    fee_result = await session.execute(select(Fee).where(Fee.id == fee_id))
    fee = fee_result.scalar_one_or_none()
    if not fee:
        raise HTTPException(status_code=404, detail="Fee not found")
    if fee.school_id != school_id:
        raise HTTPException(status_code=403, detail="Access denied")
    await _assert_fee_campus_access(session, current_user, fee)

    if fee.amount_paid > 0:
        raise HTTPException(
            status_code=409,
            detail="Cannot create an installment plan after payments have already been recorded against this fee"
        )

    existing_result = await session.execute(
        select(FeeInstallment).where(FeeInstallment.fee_id == fee_id)
    )
    if existing_result.scalars().first():
        raise HTTPException(status_code=409, detail="An installment plan already exists for this fee")

    scheduled_total = round(sum(item.amount_due for item in plan.installments), 2)
    if abs(scheduled_total - round(fee.amount_due, 2)) > 0.01:
        raise HTTPException(
            status_code=400,
            detail=f"Installment amounts must sum to the fee's amount_due (GHS {fee.amount_due:.2f}), got GHS {scheduled_total:.2f}"
        )

    installments = []
    for i, item in enumerate(plan.installments, start=1):
        installment = FeeInstallment(
            school_id=school_id,
            fee_id=fee_id,
            student_id=fee.student_id,
            installment_number=i,
            amount_due=item.amount_due,
            due_date=item.due_date,
        )
        session.add(installment)
        installments.append(installment)

    await session.commit()

    await log_event(
        session, actor=current_user, action="fee.installment_plan_created", entity_type="fee",
        entity_id=fee.id, school_id=school_id,
        summary=f"{current_user.email} set up a {len(installments)}-installment plan for fee {fee.id}",
        new_values={"installment_count": len(installments), "total": scheduled_total},
    )

    return {
        "fee_id": fee_id,
        "installments": [
            {"installment_number": i.installment_number, "amount_due": i.amount_due, "due_date": i.due_date}
            for i in installments
        ],
        "message": f"Created {len(installments)}-installment plan"
    }


@router.get("/{fee_id}/installments", response_model=list[dict])
async def list_installments(
    fee_id: str,
    current_user: User = Depends(get_current_user),
    _plan_check: User = Depends(require_plan_feature("fees_plus")),
    session: AsyncSession = Depends(get_session)
):
    """List the installment schedule for a fee"""
    fee_result = await session.execute(select(Fee).where(Fee.id == fee_id))
    fee = fee_result.scalar_one_or_none()
    if not fee:
        raise HTTPException(status_code=404, detail="Fee not found")

    student_result = await session.execute(select(Student).where(Student.id == fee.student_id))
    student = student_result.scalar_one_or_none()
    if not student or not await _has_fee_access(session, current_user, student):
        raise HTTPException(status_code=403, detail="Access denied")

    result = await session.execute(
        select(FeeInstallment).where(FeeInstallment.fee_id == fee_id).order_by(FeeInstallment.installment_number)
    )
    installments = result.scalars().all()

    return [
        {
            "id": i.id,
            "installment_number": i.installment_number,
            "amount_due": i.amount_due,
            "amount_paid": i.amount_paid,
            "balance": i.amount_due - i.amount_paid,
            "due_date": i.due_date,
            "status": i.status
        }
        for i in installments
    ]


@router.patch("/{fee_id}/discount", response_model=dict)
async def apply_fee_discount(
    fee_id: str,
    body: FeeDiscountUpdate,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    _plan_check: User = Depends(require_plan_feature("fees_plus")),
    session: AsyncSession = Depends(get_session)
):
    """Apply or update a structured discount/scholarship on a fee.

    Discounts must carry a DiscountType (sibling, staff_ward, scholarship,
    hardship, promotional, other) rather than being free-text-only, so
    they're reportable (e.g. "total scholarship value granted this term")
    and there's a record of who approved them.
    """
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    fee_result = await session.execute(select(Fee).where(Fee.id == fee_id))
    fee = fee_result.scalar_one_or_none()
    if not fee:
        raise HTTPException(status_code=404, detail="Fee not found")
    if fee.school_id != school_id:
        raise HTTPException(status_code=403, detail="Access denied")
    await _assert_fee_campus_access(session, current_user, fee)

    if body.discount > fee.amount_due:
        raise HTTPException(status_code=400, detail="discount cannot exceed the fee's amount_due")

    previous_discount = fee.discount
    fee.discount = body.discount
    fee.discount_type = body.discount_type
    fee.discount_reason = body.discount_reason
    fee.discount_approved_by = current_user.id

    balance = fee.amount_due - fee.amount_paid - fee.discount
    if balance <= 0:
        fee.status = PaymentStatus.PAID.value
    elif fee.amount_paid > 0:
        fee.status = PaymentStatus.PARTIAL.value
    else:
        fee.status = PaymentStatus.PENDING.value

    fee.updated_at = datetime.utcnow()
    session.add(fee)
    await session.commit()
    await session.refresh(fee)

    # Keep AR/Revenue consistent with the new net expected amount — a
    # discount changed after the invoice entry posted previously had no GL
    # effect at all, silently leaving AR overstated (or revenue understated)
    # relative to what the fee record itself now says is actually owed.
    try:
        structure_result = await session.execute(select(FeeStructure).where(FeeStructure.id == fee.fee_structure_id))
        fee_structure = structure_result.scalar_one_or_none()
        if fee_structure:
            delta = body.discount - previous_discount
            await fee_gl_service.post_fee_discount_adjustment(session, school_id, fee, fee_structure, delta, current_user.id)
            await session.commit()
    except Exception as e:
        logger.error(f"Error posting discount adjustment journal entry for fee {fee.id}: {str(e)}")

    await log_event(
        session, actor=current_user, action="fee.discount_applied", entity_type="fee",
        entity_id=fee.id, school_id=school_id,
        summary=f"{current_user.email} applied a {body.discount_type} discount of GHS {body.discount:.2f} to fee {fee.id}",
        new_values={"discount": body.discount, "discount_type": body.discount_type, "previous_discount": previous_discount},
    )


class BulkFeeDiscountRequest(SQLModel):
    fee_structure_id: str
    discount_type: DiscountType
    discount_percentage: float
    discount_reason: Optional[str] = None
    student_ids: Optional[List[str]] = None
    class_id: Optional[str] = None


@router.post("/bulk-discount", response_model=dict)
async def apply_bulk_fee_discount(
    body: BulkFeeDiscountRequest,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    _plan_check: User = Depends(require_plan_feature("fees_plus")),
    session: AsyncSession = Depends(get_session)
):
    """Apply the same percentage discount to every matching fee at once —
    e.g. a 10% staff-ward discount across a whole class, or a sibling
    discount across a specific list of students — instead of the
    one-fee-at-a-time /{fee_id}/discount endpoint being the only path for
    something that in practice always applies to a whole category of
    students together.

    Scopes to a single fee_structure_id (one discount decision = one fee
    line, matching apply_fee_discount's own granularity) and either an
    explicit student_ids list or a whole class_id — exactly one of the two
    must be given.
    """
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    if not (0 < body.discount_percentage <= 100):
        raise HTTPException(status_code=400, detail="discount_percentage must be between 0 and 100")
    if bool(body.student_ids) == bool(body.class_id):
        raise HTTPException(status_code=400, detail="Provide exactly one of student_ids or class_id")

    structure_result = await session.execute(
        select(FeeStructure).where(FeeStructure.id == body.fee_structure_id, FeeStructure.school_id == school_id)
    )
    structure = structure_result.scalar_one_or_none()
    if not structure:
        raise HTTPException(status_code=404, detail="Fee structure not found")

    target_student_ids = body.student_ids
    if body.class_id:
        class_result = await session.execute(
            select(Class).where(Class.id == body.class_id, Class.school_id == school_id)
        )
        target_class = class_result.scalar_one_or_none()
        if not target_class:
            raise HTTPException(status_code=404, detail="Class not found")
        assert_campus_access(current_user, target_class.campus_id)
        students_result = await session.execute(
            select(Student.id).where(Student.class_id == body.class_id, Student.school_id == school_id)
        )
        target_student_ids = students_result.scalars().all()

    if not target_student_ids:
        raise HTTPException(status_code=404, detail="No students to apply the discount to")

    fees_result = await session.execute(
        select(Fee).where(
            Fee.school_id == school_id,
            Fee.fee_structure_id == body.fee_structure_id,
            Fee.student_id.in_(target_student_ids),
            Fee.status.notin_([PaymentStatus.WRITTEN_OFF]),
        )
    )
    fees = fees_result.scalars().all()

    updated_count = 0
    total_discount_granted = 0.0
    deltas_by_fee_id = {}
    for fee in fees:
        previous_discount = fee.discount
        discount_amount = round(fee.amount_due * body.discount_percentage / 100, 2)
        if discount_amount > fee.amount_due:
            discount_amount = fee.amount_due
        if discount_amount == previous_discount:
            continue

        fee.discount = discount_amount
        fee.discount_type = body.discount_type
        fee.discount_reason = body.discount_reason
        fee.discount_approved_by = current_user.id

        balance = fee.amount_due - fee.amount_paid - fee.discount
        if balance <= 0:
            fee.status = PaymentStatus.PAID.value
        elif fee.amount_paid > 0:
            fee.status = PaymentStatus.PARTIAL.value
        else:
            fee.status = PaymentStatus.PENDING.value
        fee.updated_at = datetime.utcnow()
        session.add(fee)
        updated_count += 1
        delta = discount_amount - previous_discount
        deltas_by_fee_id[fee.id] = delta
        total_discount_granted += delta

    await session.commit()

    # Keep AR/Revenue consistent with the new net expected amount, per fee —
    # same reasoning as apply_fee_discount. Best-effort: one GL failure
    # shouldn't roll back the discount records already committed above.
    for fee in fees:
        delta = deltas_by_fee_id.get(fee.id)
        if not delta:
            continue
        try:
            await session.refresh(fee)
            await fee_gl_service.post_fee_discount_adjustment(session, school_id, fee, structure, delta, current_user.id)
        except Exception as e:
            logger.error(f"Error posting bulk discount adjustment journal entry for fee {fee.id}: {str(e)}")
    if deltas_by_fee_id:
        await session.commit()

    await log_event(
        session, actor=current_user, action="fee.bulk_discount_applied", entity_type="fee",
        entity_id=body.fee_structure_id, school_id=school_id,
        summary=f"{current_user.email} applied a {body.discount_percentage}% {body.discount_type} discount to {updated_count} fee(s)",
        new_values={"discount_percentage": body.discount_percentage, "discount_type": body.discount_type, "fees_updated": updated_count},
    )

    return {
        "fee_structure_id": body.fee_structure_id,
        "students_targeted": len(target_student_ids),
        "fees_updated": updated_count,
        "total_discount_granted": round(total_discount_granted, 2),
    }

    return {
        "fee_id": fee.id,
        "discount": fee.discount,
        "discount_type": fee.discount_type,
        "discount_approved_by": fee.discount_approved_by,
        "fee_balance": balance,
        "fee_status": fee.status,
        "message": "Discount applied"
    }


@router.get("/student/{student_id}", response_model=dict)
async def get_student_fees(
    student_id: str,
    academic_term_id: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Get all fees for a student"""
    student_result = await session.execute(select(Student).where(Student.id == student_id))
    student = student_result.scalar_one_or_none()

    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    if not await _has_fee_access(session, current_user, student):
        raise HTTPException(status_code=403, detail="Access denied")
    
    query = select(Fee).where(Fee.student_id == student_id)
    
    if academic_term_id:
        query = query.where(Fee.academic_term_id == academic_term_id)
    
    result = await session.execute(query)
    fees = result.scalars().all()
    
    structure_ids = [f.fee_structure_id for f in fees]
    structures = {}
    if structure_ids:
        structure_result = await session.execute(
            select(FeeStructure).where(FeeStructure.id.in_(structure_ids))
        )
        for s in structure_result.scalars().all():
            structures[s.id] = s

    fee_ids = [f.id for f in fees]
    installments_by_fee = {}
    if fee_ids:
        installments_result = await session.execute(
            select(FeeInstallment).where(FeeInstallment.fee_id.in_(fee_ids))
            .order_by(FeeInstallment.installment_number)
        )
        for inst in installments_result.scalars().all():
            installments_by_fee.setdefault(inst.fee_id, []).append(inst)

    fee_list = []
    for fee in fees:
        structure = structures.get(fee.fee_structure_id)
        fee_list.append({
            "id": fee.id,
            "fee_type": structure.fee_type if structure else "unknown",
            "description": structure.description if structure else "N/A",
            "amount_due": fee.amount_due,
            "amount_paid": fee.amount_paid,
            "balance": fee.amount_due - fee.amount_paid - fee.discount,
            "discount": fee.discount,
            "discount_type": fee.discount_type,
            "discount_reason": fee.discount_reason,
            "status": fee.status,
            "due_date": structure.due_date if structure else None,
            "installments": [
                {
                    "id": inst.id,
                    "installment_number": inst.installment_number,
                    "amount_due": inst.amount_due,
                    "amount_paid": inst.amount_paid,
                    "due_date": inst.due_date,
                    "status": inst.status
                }
                for inst in installments_by_fee.get(fee.id, [])
            ]
        })
    
    total_due = sum(f["amount_due"] for f in fee_list)
    total_paid = sum(f["amount_paid"] for f in fee_list)
    total_discount = sum(f["discount"] for f in fee_list)
    
    return {
        "student_id": student_id,
        "student_name": f"{student.first_name} {student.last_name}",
        "summary": {
            "total_fees": total_due,
            "total_paid": total_paid,
            "total_discount": total_discount,
            "balance": total_due - total_paid - total_discount
        },
        "fees": fee_list
    }


async def _allocate_to_installments(session: AsyncSession, fee_id: str, amount: float) -> None:
    """Apply `amount` to a fee's installments in due-date order (earliest
    first), mirroring the same waterfall used for the fee itself. No-op if
    the fee has no installment plan."""
    if amount <= 0:
        return

    result = await session.execute(
        select(FeeInstallment).where(
            FeeInstallment.fee_id == fee_id,
            FeeInstallment.status.in_([InstallmentStatus.PENDING, InstallmentStatus.PARTIAL, InstallmentStatus.OVERDUE])
        ).order_by(FeeInstallment.due_date, FeeInstallment.installment_number).with_for_update()
    )
    installments = result.scalars().all()

    remaining = amount
    for installment in installments:
        if remaining <= 0:
            break
        balance = installment.amount_due - installment.amount_paid
        if balance <= 0:
            continue
        applied = min(remaining, balance)
        installment.amount_paid += applied
        installment.status = (
            InstallmentStatus.PAID.value
            if installment.amount_paid >= installment.amount_due
            else InstallmentStatus.PARTIAL.value
        )
        installment.updated_at = datetime.utcnow()
        session.add(installment)
        remaining -= applied


async def _reverse_installment_allocation(session: AsyncSession, fee_id: str, amount: float) -> None:
    """Undo `amount` worth of installment allocation, most-recently-paid
    installment first. There's no per-payment ledger of which installment a
    given payment funded, so this is a LIFO approximation rather than an
    exact reversal of the original payment — acceptable for the common case
    (voiding the most recent payment) but can misattribute the reversal if
    several payments were made out of due-date order before voiding one."""
    if amount <= 0:
        return

    result = await session.execute(
        select(FeeInstallment).where(
            FeeInstallment.fee_id == fee_id,
            FeeInstallment.amount_paid > 0
        ).order_by(FeeInstallment.installment_number.desc()).with_for_update()
    )
    installments = result.scalars().all()

    remaining = amount
    for installment in installments:
        if remaining <= 0:
            break
        reversible = min(remaining, installment.amount_paid)
        installment.amount_paid -= reversible
        installment.status = (
            InstallmentStatus.PARTIAL.value if installment.amount_paid > 0 else InstallmentStatus.PENDING.value
        )
        installment.updated_at = datetime.utcnow()
        session.add(installment)
        remaining -= reversible


@router.post("/payments", response_model=dict)
async def record_payment(
    payment_data: FeePaymentCreate,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Record a fee payment with overpayment distribution"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    # Lock the fee row for the duration of this transaction so concurrent
    # payments against the same fee can't both read a stale balance.
    # school_id scopes the lookup to the caller's own tenant — without it, a
    # SCHOOL_ADMIN who knows/guesses another school's fee_id could post a
    # payment against it, corrupting that tenant's balance and misrouting
    # the GL entry (posted under this caller's own school_id).
    fee_result = await session.execute(
        select(Fee).where(Fee.id == payment_data.fee_id, Fee.school_id == school_id).with_for_update()
    )
    fee = fee_result.scalar_one_or_none()

    if not fee:
        raise HTTPException(status_code=404, detail="Fee not found")
    await _assert_fee_campus_access(session, current_user, fee)

    # Double-submit guard: an identical payment (same fee, amount, method,
    # recorded by the same user) within the last 30 seconds is a double-click
    # or repeated request, not a second real payment — reject it instead of
    # silently recording the money twice.
    recent_duplicate = await session.execute(
        select(FeePayment).where(
            FeePayment.fee_id == payment_data.fee_id,
            FeePayment.amount == payment_data.amount,
            FeePayment.payment_method == payment_data.payment_method,
            FeePayment.received_by == current_user.id,
            FeePayment.created_at > datetime.utcnow() - timedelta(seconds=30),
            FeePayment.voided == False,
        )
    )
    if recent_duplicate.scalars().first():
        raise HTTPException(
            status_code=409,
            detail="An identical payment was recorded seconds ago. If this is a genuinely separate payment, wait 30 seconds and try again."
        )

    # An external transaction reference (bank/MoMo/cheque) identifies one
    # real-world payment — unlike OnlineTransaction.reference (the Paystack
    # path), which is DB-unique, this manual-entry field had zero duplicate
    # protection at all, so the same reference could be recorded against many
    # different fees, each counted as separate revenue. Scoped to non-voided
    # rows so a corrected re-entry can reuse a reference whose original entry
    # was voided, and checked before this payment creates ANY rows — the
    # overpayment-distribution loop below deliberately reuses this same
    # reference_number across several FeePayment rows for one real
    # transaction, so this can't be a DB-level unique constraint on the
    # column itself.
    if payment_data.reference_number:
        existing_ref = await session.execute(
            select(FeePayment).where(
                FeePayment.school_id == school_id,
                FeePayment.reference_number == payment_data.reference_number,
                FeePayment.voided == False,
            )
        )
        if existing_ref.scalars().first():
            raise HTTPException(
                status_code=409,
                detail=f"Reference number '{payment_data.reference_number}' has already been recorded for another payment"
            )

    receipt_number = await get_next_receipt_number(session, school_id)

    # Get fee structure for GL posting
    fee_structure_result = await session.execute(
        select(FeeStructure).where(FeeStructure.id == fee.fee_structure_id)
    )
    fee_structure = fee_structure_result.scalar_one_or_none()
    
    payment_amount = payment_data.amount
    student_id = fee.student_id
    fee_balance_before = fee.amount_due - fee.amount_paid - fee.discount
    
    # Cap payment to outstanding balance to prevent overpayment of this specific fee
    amount_for_primary_fee = min(payment_amount, fee_balance_before)
    remaining_amount = payment_amount - amount_for_primary_fee
    
    # Record primary fee payment
    primary_payment = FeePayment(
        school_id=school_id,
        fee_id=payment_data.fee_id,
        student_id=student_id,
        amount=amount_for_primary_fee,
        payment_method=payment_data.payment_method,
        reference_number=payment_data.reference_number,
        receipt_number=receipt_number,
        payment_date=payment_data.payment_date,
        remarks=payment_data.remarks,
        received_by=current_user.id
    )
    session.add(primary_payment)
    
    # Update primary fee
    fee.amount_paid += amount_for_primary_fee
    balance = fee.amount_due - fee.amount_paid - fee.discount
    
    if balance <= 0:
        fee.status = PaymentStatus.PAID.value
    else:
        fee.status = PaymentStatus.PARTIAL.value
    
    fee.updated_at = datetime.utcnow()
    session.add(fee)

    await _allocate_to_installments(session, fee.id, amount_for_primary_fee)

    # Create GL journal entry for primary fee payment
    journal_entry_id = None
    try:
        if fee_structure:
            journal_entry_id = await _create_fee_journal_entry(
                session=session,
                school_id=school_id,
                payment=primary_payment,
                fee=fee,
                fee_structure=fee_structure,
                amount=amount_for_primary_fee,
            )
            logger.info(f"Created journal entry {journal_entry_id} for fee payment {primary_payment.id}")
            primary_payment.journal_entry_id = journal_entry_id
            session.add(primary_payment)
    except Exception as e:
        logger.error(f"Error creating journal entry for fee payment: {str(e)}")
        # Continue with payment recording even if GL posting fails
    
    # Distribute excess payment to other outstanding fees
    additional_payments = []
    if remaining_amount > 0:
        logger.info(
            f"Distributing overpayment of GHS {remaining_amount:.2f} "
            f"from payment to other outstanding fees"
        )
        
        # Get other outstanding fees for this student
        other_fees_result = await session.execute(
            select(Fee).where(
                Fee.student_id == student_id,
                Fee.school_id == school_id,
                Fee.id != payment_data.fee_id,
                Fee.status.in_([PaymentStatus.PENDING, PaymentStatus.PARTIAL, PaymentStatus.OVERDUE])
            ).order_by(Fee.created_at).with_for_update()
        )
        other_fees = other_fees_result.scalars().all()
        
        # Distribute remaining amount to other fees
        for other_fee in other_fees:
            if remaining_amount <= 0:
                break
            
            other_fee_balance = other_fee.amount_due - other_fee.amount_paid - other_fee.discount
            if other_fee_balance <= 0:
                continue
            
            amount_for_other_fee = min(remaining_amount, other_fee_balance)
            
            # Create payment record for other fee
            other_payment = FeePayment(
                school_id=school_id,
                fee_id=other_fee.id,
                student_id=student_id,
                amount=amount_for_other_fee,
                payment_method=payment_data.payment_method,
                reference_number=payment_data.reference_number,
                receipt_number=await get_next_receipt_number(session, school_id),
                payment_date=payment_data.payment_date,
                remarks=f"{payment_data.remarks} (Overpayment distribution)",
                received_by=current_user.id
            )
            session.add(other_payment)
            additional_payments.append(other_payment)
            
            # Update other fee
            other_fee.amount_paid += amount_for_other_fee
            other_fee_balance_after = other_fee.amount_due - other_fee.amount_paid - other_fee.discount
            
            if other_fee_balance_after <= 0:
                other_fee.status = PaymentStatus.PAID.value
            else:
                other_fee.status = PaymentStatus.PARTIAL.value
            
            other_fee.updated_at = datetime.utcnow()
            session.add(other_fee)

            await _allocate_to_installments(session, other_fee.id, amount_for_other_fee)

            remaining_amount -= amount_for_other_fee
            
            logger.info(
                f"Distributed GHS {amount_for_other_fee:.2f} to fee {other_fee.id}, "
                f"new balance: {other_fee_balance_after:.2f}"
            )
    
    await session.commit()
    await session.refresh(primary_payment)

    await log_event(
        session, actor=current_user, action="fee.payment_recorded", entity_type="fee_payment",
        entity_id=primary_payment.id, school_id=school_id,
        summary=f"{current_user.email} recorded a payment of GHS {payment_amount:.2f} for student {student_id} (receipt {receipt_number})",
        new_values={"amount": payment_amount, "fee_id": payment_data.fee_id, "receipt_number": receipt_number},
    )

    response = {
        "id": primary_payment.id,
        "receipt_number": receipt_number,
        "amount": payment_amount,
        "amount_applied_to_primary_fee": amount_for_primary_fee,
        "fee_balance": balance,
        "fee_status": fee.status,
        "message": "Payment recorded successfully"
    }
    
    # Add info about overpayment distribution if applicable
    if additional_payments:
        response["overpayments_distributed"] = True
        response["additional_fees_paid_count"] = len(additional_payments)
        response["message"] = f"Payment recorded. Overpayment of GHS {sum(p.amount for p in additional_payments):.2f} applied to {len(additional_payments)} other fee(s)."
    
    if journal_entry_id:
        response["journal_entry_id"] = journal_entry_id
    
    return response


@router.get("/payments/student/{student_id}", response_model=list[dict])
async def get_student_payments(
    student_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Get payment history for a student"""
    student_result = await session.execute(select(Student).where(Student.id == student_id))
    student = student_result.scalar_one_or_none()

    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    if not await _has_fee_access(session, current_user, student):
        raise HTTPException(status_code=403, detail="Access denied")

    result = await session.execute(
        select(FeePayment).where(FeePayment.student_id == student_id).order_by(FeePayment.payment_date.desc())
    )
    payments = result.scalars().all()
    
    return [
        {
            "id": p.id,
            "fee_id": p.fee_id,
            "amount": p.amount,
            "payment_method": p.payment_method,
            "reference_number": p.reference_number,
            "receipt_number": p.receipt_number,
            "payment_date": p.payment_date,
            "remarks": p.remarks,
            "created_at": p.created_at.isoformat(),
            "voided": p.voided,
            "void_reason": p.void_reason
        }
        for p in payments
    ]


@router.post("/payments/{payment_id}/void", response_model=dict)
async def void_payment(
    payment_id: str,
    body: VoidPaymentRequest,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Void a previously recorded payment, reversing its effect on the fee balance.

    The FeePayment row is kept (marked voided) rather than deleted or edited
    in place, so there's always an audit trail of what was reversed and why.
    """
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    payment_result = await session.execute(
        select(FeePayment).where(FeePayment.id == payment_id).with_for_update()
    )
    payment = payment_result.scalar_one_or_none()

    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")

    if payment.school_id != school_id:
        raise HTTPException(status_code=403, detail="Access denied")

    if payment.voided:
        raise HTTPException(status_code=409, detail="Payment has already been voided")

    fee_result = await session.execute(
        select(Fee).where(Fee.id == payment.fee_id, Fee.school_id == school_id).with_for_update()
    )
    fee = fee_result.scalar_one_or_none()
    if not fee:
        raise HTTPException(status_code=404, detail="Associated fee not found")
    await _assert_fee_campus_access(session, current_user, fee)

    fee.amount_paid = max(0.0, fee.amount_paid - payment.amount)
    balance = fee.amount_due - fee.amount_paid - fee.discount

    if balance <= 0:
        fee.status = PaymentStatus.PAID.value
    elif fee.amount_paid > 0:
        fee.status = PaymentStatus.PARTIAL.value
    else:
        fee.status = PaymentStatus.PENDING.value

    fee.updated_at = datetime.utcnow()
    session.add(fee)

    await _reverse_installment_allocation(session, fee.id, payment.amount)

    payment.voided = True
    payment.voided_at = datetime.utcnow()
    payment.voided_by = current_user.id
    payment.void_reason = body.reason
    session.add(payment)

    await session.commit()
    await session.refresh(fee)

    # Reverse the GL journal entry this payment originally posted (if any) —
    # otherwise the ledger permanently overstates cash received and revenue
    # recognized even though the payment itself has been voided. Best-effort,
    # same trade-off as record_payment's own GL posting: don't fail the void
    # itself over a GL problem, just log it for manual reconciliation.
    journal_reversal_id = None
    if payment.journal_entry_id:
        try:
            from services.journal_entry_service import JournalEntryService
            journal_service = JournalEntryService(session)
            _original_entry, reversal_entry = await journal_service.reverse_entry(
                school_id=school_id,
                entry_id=payment.journal_entry_id,
                reversed_by=current_user.id,
                reversal_reason=f"Fee payment {payment.receipt_number} voided: {body.reason}",
            )
            journal_reversal_id = reversal_entry.id if reversal_entry else None
        except Exception as e:
            logger.error(f"Error reversing journal entry {payment.journal_entry_id} for voided payment {payment.id}: {str(e)}")

    await log_event(
        session, actor=current_user, action="fee.payment_voided", entity_type="fee_payment",
        entity_id=payment.id, school_id=school_id,
        summary=f"{current_user.email} voided payment {payment.receipt_number} (GHS {payment.amount:.2f}): {body.reason}",
        new_values={"payment_id": payment.id, "amount": payment.amount, "reason": body.reason},
    )

    return {
        "message": "Payment voided",
        "payment_id": payment.id,
        "reversed_amount": payment.amount,
        "journal_reversal_id": journal_reversal_id,
        "fee_id": fee.id,
        "fee_balance": balance,
        "fee_status": fee.status
    }


@router.post("/{fee_id}/write-off", response_model=dict)
async def write_off_fee(
    fee_id: str,
    body: FeeWriteOffRequest,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Formally write off an uncollectable fee balance (Dr Bad Debt Expense
    / Cr Accounts Receivable) — previously there was no resolution path for
    this at all; a withdrawn, untraceable family's balance just stayed
    PENDING/PARTIAL forever, permanently distorting outstanding-fees totals
    with money that will genuinely never be collected."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    fee_result = await session.execute(
        select(Fee).where(Fee.id == fee_id, Fee.school_id == school_id).with_for_update()
    )
    fee = fee_result.scalar_one_or_none()
    if not fee:
        raise HTTPException(status_code=404, detail="Fee not found")
    await _assert_fee_campus_access(session, current_user, fee)

    if fee.status in (PaymentStatus.PAID.value, PaymentStatus.WRITTEN_OFF.value):
        raise HTTPException(status_code=409, detail=f"Cannot write off a fee in {fee.status} status")

    remaining_balance = fee.amount_due - fee.amount_paid - fee.discount
    if remaining_balance <= 0:
        raise HTTPException(status_code=400, detail="This fee has no remaining balance to write off")

    structure_result = await session.execute(select(FeeStructure).where(FeeStructure.id == fee.fee_structure_id))
    fee_structure = structure_result.scalar_one_or_none()

    journal_entry_id = None
    try:
        journal_entry_id = await fee_gl_service.post_fee_write_off(session, school_id, fee, remaining_balance, current_user.id)
    except Exception as e:
        logger.error(f"Error posting write-off journal entry for fee {fee.id}: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to post write-off to the general ledger — fee was not written off")

    fee.status = PaymentStatus.WRITTEN_OFF.value
    fee.written_off_at = datetime.utcnow()
    fee.written_off_by = current_user.id
    fee.write_off_reason = body.reason
    fee.updated_at = datetime.utcnow()
    session.add(fee)
    await session.commit()
    await session.refresh(fee)

    await log_event(
        session, actor=current_user, action="fee.written_off", entity_type="fee",
        entity_id=fee.id, school_id=school_id,
        summary=f"{current_user.email} wrote off GHS {remaining_balance:.2f} for fee {fee.id}: {body.reason}",
        new_values={"amount_written_off": remaining_balance, "reason": body.reason},
    )

    return {
        "fee_id": fee.id,
        "amount_written_off": remaining_balance,
        "fee_status": fee.status,
        "journal_entry_id": journal_entry_id,
        "message": "Fee written off",
    }


@router.post("/assign-class", response_model=dict)
async def assign_fee_to_class(
    body: AssignFeeToClassRequest,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    _plan_check: User = Depends(require_plan_feature("fees_plus")),
    session: AsyncSession = Depends(get_session)
):
    """Assign a fee structure to all students in a class"""
    structure_id = body.structure_id
    class_id = body.class_id
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    # Get fee structure — school_id scoped, without which an admin who
    # knows/guesses another school's structure_id could bulk-assign its
    # amount_due/academic_term_id to their own students, a cross-tenant
    # leak into invoice amounts and terms.
    structure_result = await session.execute(
        select(FeeStructure).where(FeeStructure.id == structure_id, FeeStructure.school_id == school_id)
    )
    structure = structure_result.scalar_one_or_none()
    if not structure:
        raise HTTPException(status_code=404, detail="Fee structure not found")

    # The single-fee path (create_student_fee) already refuses to create a
    # Fee against a locked term; bulk assignment must enforce the same rule
    # instead of silently invoicing an entire class against a term closed
    # for financial reporting.
    term = None
    if structure.academic_term_id:
        term_result = await session.execute(
            select(AcademicTerm).where(AcademicTerm.id == structure.academic_term_id, AcademicTerm.school_id == school_id)
        )
        term = term_result.scalar_one_or_none()
        if term and term.is_locked:
            raise HTTPException(status_code=423, detail="This academic term is locked and no longer accepts new fees")

    school_result = await session.execute(select(School).where(School.id == school_id))
    school = school_result.scalar_one_or_none()
    prorate_enabled = bool(term and school and school.prorate_fees_for_late_admission)

    class_result = await session.execute(
        select(Class).where(and_(Class.id == class_id, Class.school_id == school_id))
    )
    target_class = class_result.scalar_one_or_none()
    if not target_class:
        raise HTTPException(status_code=404, detail="Class not found")

    # A campus-scoped admin can only assign fees within their own campus.
    assert_campus_access(current_user, target_class.campus_id)
    # A campus-specific fee structure can't be assigned to a class in a
    # different campus — a school-wide structure (campus_id is None) has no
    # such restriction.
    if structure.campus_id and structure.campus_id != target_class.campus_id:
        raise HTTPException(
            status_code=403,
            detail="This fee structure is scoped to a different campus than the selected class",
        )

    # Get students in class
    students_result = await session.execute(
        select(Student).where(
            Student.class_id == class_id,
            Student.school_id == school_id,
            Student.status == "active"
        )
    )
    students = students_result.scalars().all()
    
    if not students:
        raise HTTPException(status_code=404, detail="No students found in class")
    
    # Check for existing fee assignments
    existing_result = await session.execute(
        select(Fee.student_id).where(
            Fee.fee_structure_id == structure_id,
            Fee.student_id.in_([s.id for s in students])
        )
    )
    existing_ids = set(existing_result.scalars().all())
    
    created_count = 0
    created_fees = []
    for student in students:
        if student.id not in existing_ids:
            amount_due = structure.amount
            if prorate_enabled and student.admission_date:
                amount_due = _prorate_fee_amount(structure.amount, term.start_date, term.end_date, student.admission_date)
            fee = Fee(
                school_id=school_id,
                student_id=student.id,
                academic_term_id=structure.academic_term_id,
                fee_structure_id=structure_id,
                amount_due=amount_due,
                status=PaymentStatus.PENDING
            )
            session.add(fee)
            created_fees.append(fee)
            created_count += 1

    await session.commit()

    # Recognize revenue at invoice time for each newly-created fee — same
    # reasoning as create_student_fee's single-fee path. Best-effort per
    # fee: one GL failure must not block the rest of the class from being
    # invoiced.
    for fee in created_fees:
        try:
            await session.refresh(fee)
            await fee_gl_service.post_fee_invoice(session, school_id, fee, structure, fee.amount_due - fee.discount)
        except Exception as e:
            logger.error(f"Error posting invoice journal entry for bulk-assigned fee {fee.id}: {str(e)}")
    if created_fees:
        await session.commit()

    return {
        "message": f"Fee assigned to {created_count} students",
        "total_students": len(students),
        "new_assignments": created_count,
        "already_assigned": len(existing_ids)
    }


@router.get("/class/{class_id}", response_model=dict)
async def get_class_fee_status(
    class_id: str,
    current_user: User = Depends(get_current_user),
    _plan_check: User = Depends(require_plan_feature("fees_plus")),
    session: AsyncSession = Depends(get_session)
):
    """Get fee collection status for a class"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    # Get class info
    class_result = await session.execute(select(Class).where(Class.id == class_id))
    classroom = class_result.scalar_one_or_none()
    if not classroom:
        raise HTTPException(status_code=404, detail="Class not found")
    
    # Get students in class
    students_result = await session.execute(
        select(Student).where(
            Student.class_id == class_id,
            Student.status == "active"
        ).order_by(Student.last_name, Student.first_name)
    )
    students = students_result.scalars().all()
    student_ids = [s.id for s in students]
    
    # Get all fees for these students
    fees_result = await session.execute(
        select(Fee).where(Fee.student_id.in_(student_ids)) if student_ids else select(Fee).where(False)
    )
    fees = fees_result.scalars().all()
    
    # Get fee structures
    structure_ids = list(set(f.fee_structure_id for f in fees))
    structures = {}
    if structure_ids:
        struct_result = await session.execute(
            select(FeeStructure).where(FeeStructure.id.in_(structure_ids))
        )
        structures = {s.id: s for s in struct_result.scalars().all()}
    
    # Group fees by student
    student_fees = {s.id: [] for s in students}
    for fee in fees:
        if fee.student_id in student_fees:
            student_fees[fee.student_id].append(fee)
    
    # Build response
    students_data = []
    class_total_due = 0
    class_total_paid = 0
    class_total_discount = 0

    for student in students:
        student_fee_list = student_fees.get(student.id, [])
        total_due = sum(f.amount_due for f in student_fee_list)
        total_paid = sum(f.amount_paid for f in student_fee_list)
        total_discount = sum(f.discount for f in student_fee_list)
        balance = total_due - total_paid - total_discount

        class_total_due += total_due
        class_total_paid += total_paid
        class_total_discount += total_discount
        
        # Determine overall status
        if not student_fee_list:
            status = "no_fees"
        elif balance <= 0:
            status = "paid"
        elif total_paid > 0:
            status = "partial"
        else:
            status = "pending"
        
        students_data.append({
            "student_id": student.id,
            "student_name": f"{student.first_name} {student.last_name}",
            "student_number": student.student_id,
            "total_due": total_due,
            "total_paid": total_paid,
            "balance": balance,
            "status": status,
            "fee_count": len(student_fee_list)
        })
    
    return {
        "class_id": class_id,
        "class_name": classroom.name,
        "total_students": len(students),
        "class_summary": {
            "total_due": class_total_due,
            "total_paid": class_total_paid,
            "total_discount": class_total_discount,
            # Discount-adjusted, matching each per-student row's own
            # "balance" above — previously this ignored discount entirely,
            # so it disagreed with the sum of the rows shown right above it.
            "balance": class_total_due - class_total_paid - class_total_discount,
            "collection_rate": round(
                (class_total_paid / (class_total_due - class_total_discount) * 100)
                if (class_total_due - class_total_discount) > 0 else 0, 1
            )
        },
        "students": students_data
    }


@router.get("/receipt/{payment_id}", response_model=dict)
async def get_payment_receipt(
    payment_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Get payment receipt details"""
    payment_result = await session.execute(
        select(FeePayment).where(FeePayment.id == payment_id)
    )
    payment = payment_result.scalar_one_or_none()

    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")

    # Get student info
    student_result = await session.execute(
        select(Student).where(Student.id == payment.student_id)
    )
    student = student_result.scalar_one_or_none()

    if not student or not await _has_fee_access(session, current_user, student):
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Get fee and structure info
    fee_result = await session.execute(
        select(Fee).where(Fee.id == payment.fee_id)
    )
    fee = fee_result.scalar_one_or_none()
    
    structure = None
    if fee:
        struct_result = await session.execute(
            select(FeeStructure).where(FeeStructure.id == fee.fee_structure_id)
        )
        structure = struct_result.scalar_one_or_none()
    
    return {
        "receipt_number": payment.receipt_number,
        "payment_date": payment.payment_date,
        "amount": payment.amount,
        "payment_method": payment.payment_method,
        "reference_number": payment.reference_number,
        "student": {
            "id": student.id if student else None,
            "name": f"{student.first_name} {student.last_name}" if student else "Unknown",
            "student_id": student.student_id if student else None
        },
        "fee_type": structure.fee_type if structure else "unknown",
        "fee_description": structure.description if structure else None,
        "remarks": payment.remarks,
        "created_at": payment.created_at.isoformat()
    }


@router.post("/refresh-overdue", response_model=dict)
async def refresh_overdue_fees(
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    _plan_check: User = Depends(require_plan_feature("fees_plus")),
    session: AsyncSession = Depends(get_session)
):
    """Transition PENDING/PARTIAL fees past their fee structure's due date to OVERDUE.

    PaymentStatus.OVERDUE was previously defined and queried against but
    never set anywhere, so no fee ever actually showed as overdue. There's no
    scheduler in this codebase (see LateFeeService for platform billing,
    which uses the same manual-trigger pattern) — this is meant to be called
    periodically by an external cron, the same way /billing/late-fees/apply is.
    """
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    today = datetime.utcnow().date()

    query = select(Fee).where(
        Fee.school_id == school_id,
        Fee.status.in_([PaymentStatus.PENDING, PaymentStatus.PARTIAL])
    )
    result = await session.execute(query)
    fees = result.scalars().all()

    if not fees:
        return {"message": "No fees to check", "marked_overdue": 0, "checked": 0}

    structure_ids = list({f.fee_structure_id for f in fees})
    struct_result = await session.execute(
        select(FeeStructure).where(FeeStructure.id.in_(structure_ids))
    )
    structures = {s.id: s for s in struct_result.scalars().all()}

    fee_ids = [f.id for f in fees]
    installments_result = await session.execute(
        select(FeeInstallment).where(
            FeeInstallment.fee_id.in_(fee_ids),
            FeeInstallment.status.in_([InstallmentStatus.PENDING, InstallmentStatus.PARTIAL])
        )
    )
    installments_by_fee = {}
    for inst in installments_result.scalars().all():
        installments_by_fee.setdefault(inst.fee_id, []).append(inst)

    marked_overdue = 0
    marked_installments_overdue = 0
    for fee in fees:
        fee_installments = installments_by_fee.get(fee.id)

        if fee_installments:
            # An installment plan supersedes the fee structure's single due
            # date — the schedule is the source of truth for what's overdue.
            fee_has_overdue_installment = False
            for installment in fee_installments:
                try:
                    inst_due = datetime.fromisoformat(installment.due_date).date()
                except ValueError:
                    logger.warning(f"Installment {installment.id} has unparseable due_date: {installment.due_date!r}")
                    continue
                if inst_due < today:
                    installment.status = InstallmentStatus.OVERDUE.value
                    installment.updated_at = datetime.utcnow()
                    session.add(installment)
                    marked_installments_overdue += 1
                    fee_has_overdue_installment = True

            if fee_has_overdue_installment and fee.status != PaymentStatus.OVERDUE:
                fee.status = PaymentStatus.OVERDUE.value
                fee.updated_at = datetime.utcnow()
                session.add(fee)
                marked_overdue += 1
            continue

        structure = structures.get(fee.fee_structure_id)
        if not structure or not structure.due_date:
            continue
        try:
            due = datetime.fromisoformat(structure.due_date).date()
        except ValueError:
            logger.warning(f"Fee structure {structure.id} has unparseable due_date: {structure.due_date!r}")
            continue
        if due < today:
            fee.status = PaymentStatus.OVERDUE.value
            fee.updated_at = datetime.utcnow()
            session.add(fee)
            marked_overdue += 1

    await session.commit()

    return {
        "message": f"Marked {marked_overdue} fee(s) and {marked_installments_overdue} installment(s) as overdue",
        "marked_overdue": marked_overdue,
        "marked_installments_overdue": marked_installments_overdue,
        "checked": len(fees)
    }


# ==================== GL Auto-posting Helper ====================

async def _create_fee_journal_entry(
    session: AsyncSession,
    school_id: str,
    payment: FeePayment,
    fee: Fee,
    fee_structure: FeeStructure,
    amount: float,
) -> Optional[str]:
    """
    Create a journal entry for an in-person/manual fee payment posting to GL.

    Posts Dr. 1010 (Business Checking Account) / Cr. 1100 (Accounts
    Receivable) — clearing the receivable that was recognized as revenue
    at INVOICE time (see fee_gl_service.post_fee_invoice, called from
    create_student_fee/assign_fee_to_class), not crediting revenue again
    here. fee_structure/amount are still accepted for backward
    compatibility with existing call sites, though the fee-type-to-revenue-
    account mapping now only matters at invoice time.
    """
    return await fee_gl_service.post_fee_payment(session, school_id, payment, amount, cash_account_code="1010")
