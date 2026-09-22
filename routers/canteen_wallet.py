import os
import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import SQLModel, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import require_roles
from database import get_session
from models.canteen_wallet import (
    CanteenItem,
    CanteenOrder,
    CanteenOrderItem,
    CanteenOrderStatus,
    CanteenWalletAccount,
)
from models.certificates import IDCard, IDCardStatus, PersonType
from models.school import School
from models.student import Student
from models.user import User, UserRole
from models.payment import OnlineTransaction, TransactionStatus, TransactionType
from models.procurement import Supplier
from services.canteen_wallet_service import CanteenWalletService, build_canteen_wallet_snapshot
from services.online_payment_service import OnlinePaymentService
from routers.parent import get_parent_children_ids

router = APIRouter(prefix="/canteen-wallet", tags=["Canteen Wallet"])

ADMIN_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)
# Order fulfillment (status changes, pickup verification) is also open to
# canteen counter staff; menu/pricing (ADMIN_ROLES above) is not.
ORDER_MANAGE_ROLES = ADMIN_ROLES + (UserRole.CANTEEN_STAFF,)
# Biometric PIN enrollment is also open to gate/security staff — the PIN
# below is shared with routers/gate_attendance.py's student-attendance
# purpose, not canteen-exclusive (see set_student_biometric_pin).
BIOMETRIC_PIN_MANAGE_ROLES = ADMIN_ROLES + (UserRole.SECURITY_OFFICER, UserRole.REGISTRAR)


class CanteenItemCreateRequest(SQLModel):
    name: str
    item_type: str = "lunch"
    price: float
    info: Optional[str] = None
    code: Optional[str] = None
    stock_count: Optional[int] = None
    needs_prep: bool = False
    calories: Optional[int] = None
    protein_g: Optional[float] = None
    carbs_g: Optional[float] = None
    fat_g: Optional[float] = None
    allergens: Optional[str] = None
    supplier_id: Optional[str] = None


class CanteenTopUpRequest(SQLModel):
    student_id: str
    amount: float


class CanteenOrderLine(SQLModel):
    item_id: str
    quantity: int


class CanteenOrderCreateRequest(SQLModel):
    items: List[CanteenOrderLine]
    note: Optional[str] = None
    # Set only by counter staff (ORDER_MANAGE_ROLES) placing an order on a
    # student's behalf after scanning their ID card — see /scan/{card_number}.
    # A student caller always orders for themselves; this is ignored for them.
    student_id: Optional[str] = None


class CanteenOrderStatusRequest(SQLModel):
    status: CanteenOrderStatus
    note: Optional[str] = None


class CanteenPickupVerifyRequest(SQLModel):
    pickup_code: str


class StudentBiometricPinRequest(SQLModel):
    pin: str


class CanteenWalletControlsRequest(SQLModel):
    frozen: Optional[bool] = None
    daily_limit: Optional[float] = None
    clear_daily_limit: bool = False
    weekly_limit: Optional[float] = None
    clear_weekly_limit: bool = False
    blocked_categories: Optional[List[str]] = None
    low_balance_threshold: Optional[float] = None


def _item_dict(item: CanteenItem) -> dict:
    return {
        "id": item.id,
        "school_id": item.school_id,
        "name": item.name,
        "item_type": item.item_type,
        "price": item.price,
        "info": item.info,
        "code": item.code,
        "is_active": item.is_active,
        "stock_count": item.stock_count,
        "needs_prep": item.needs_prep,
        "calories": item.calories,
        "protein_g": item.protein_g,
        "carbs_g": item.carbs_g,
        "fat_g": item.fat_g,
        "allergens": item.allergens,
        "supplier_id": item.supplier_id,
        "created_at": item.created_at.isoformat() if item.created_at else None,
    }


def _vendor_dict(supplier: Supplier) -> dict:
    return {
        "id": supplier.id,
        "name": supplier.name,
        "contact_name": supplier.contact_name,
        "phone": supplier.phone,
        "email": supplier.email,
        "address": supplier.address,
        "tax_number": supplier.tax_number,
        "status": supplier.status.value,
    }


def _order_dict(order: CanteenOrder, lines: List[CanteenOrderItem]) -> dict:
    return {
        "id": order.id,
        "order_code": order.order_code,
        "school_id": order.school_id,
        "student_id": order.student_id,
        "status": order.status,
        "subtotal": order.subtotal,
        "total": order.total,
        "pickup_code": order.pickup_code,
        "note": order.note,
        "rejection_reason": order.rejection_reason,
        "created_at": order.created_at.isoformat() if order.created_at else None,
        "accepted_at": order.accepted_at.isoformat() if order.accepted_at else None,
        "preparing_at": order.preparing_at.isoformat() if order.preparing_at else None,
        "ready_at": order.ready_at.isoformat() if order.ready_at else None,
        "completed_at": order.completed_at.isoformat() if order.completed_at else None,
        "closed_at": order.closed_at.isoformat() if order.closed_at else None,
        "items": [
            {
                "item_id": line.item_id,
                "name": line.item_name,
                "unit_price": line.unit_price,
                "quantity": line.quantity,
                "line_total": line.line_total,
            }
            for line in lines
        ],
    }


async def _get_order_lines(session: AsyncSession, order_id: str) -> List[CanteenOrderItem]:
    result = await session.execute(select(CanteenOrderItem).where(CanteenOrderItem.order_id == order_id))
    return list(result.scalars().all())


async def _get_student_record(session: AsyncSession, current_user: User) -> Student:
    result = await session.execute(select(Student).where(Student.user_id == current_user.id))
    student_record = result.scalar_one_or_none()
    if not student_record:
        raise HTTPException(status_code=404, detail="Student record not found")
    return student_record


async def _assert_parent_owns_student(current_user: User, student_id: str, session: AsyncSession) -> None:
    child_ids = await get_parent_children_ids(current_user, session)
    if student_id not in child_ids:
        raise HTTPException(status_code=403, detail="Not authorized for this student")


async def _get_student_for_staff(session: AsyncSession, current_user: User, student_id: str) -> Student:
    """Same school-scoping shape as _get_order_for_admin — a school_admin/
    canteen_staff can only charge students at their own school; super_admin
    is unrestricted."""
    result = await session.execute(select(Student).where(Student.id == student_id))
    student = result.scalar_one_or_none()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    if current_user.role != UserRole.SUPER_ADMIN and student.school_id != current_user.school_id:
        raise HTTPException(status_code=403, detail="Access denied for this school")
    return student


async def _get_order_for_admin(session: AsyncSession, current_user: User, order_id: str) -> CanteenOrder:
    result = await session.execute(select(CanteenOrder).where(CanteenOrder.id == order_id))
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if current_user.role != UserRole.SUPER_ADMIN and order.school_id != current_user.school_id:
        raise HTTPException(status_code=403, detail="Access denied for this school")
    return order


# ── Menu (item catalog) ─────────────────────────────────────────────────────

@router.get("/items")
async def list_canteen_items(
    current_user: User = Depends(require_roles(
        UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.STUDENT,
        UserRole.PARENT, UserRole.TEACHER, UserRole.SECURITY_OFFICER,
        UserRole.CANTEEN_STAFF,
    )),
    session: AsyncSession = Depends(get_session),
):
    if not current_user.school_id:
        return []

    result = await session.execute(
        select(CanteenItem).where(
            CanteenItem.school_id == current_user.school_id,
            CanteenItem.is_active == True,
        ).order_by(CanteenItem.item_type, CanteenItem.name)
    )
    return [_item_dict(item) for item in result.scalars().all()]


@router.post("/items", status_code=status.HTTP_201_CREATED)
async def create_canteen_item(
    payload: CanteenItemCreateRequest,
    current_user: User = Depends(require_roles(*ADMIN_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    if not current_user.school_id:
        raise HTTPException(status_code=400, detail="School context is required")
    if not payload.name or payload.price < 0:
        raise HTTPException(status_code=400, detail="Item name and valid price are required")
    if payload.stock_count is not None and payload.stock_count < 0:
        raise HTTPException(status_code=400, detail="Stock count cannot be negative")

    if payload.supplier_id:
        supplier = (
            await session.execute(
                select(Supplier).where(Supplier.id == payload.supplier_id, Supplier.school_id == current_user.school_id)
            )
        ).scalar_one_or_none()
        if not supplier:
            raise HTTPException(status_code=400, detail="Supplier not found for this school")

    code = (payload.code or f"{payload.item_type.upper()[:4]}-{uuid.uuid4().hex[:4].upper()}").strip().upper()
    item = CanteenItem(
        school_id=current_user.school_id,
        name=payload.name,
        item_type=payload.item_type.lower(),
        price=payload.price,
        info=payload.info,
        code=code,
        stock_count=payload.stock_count,
        needs_prep=payload.needs_prep,
        calories=payload.calories,
        protein_g=payload.protein_g,
        carbs_g=payload.carbs_g,
        fat_g=payload.fat_g,
        allergens=payload.allergens,
        supplier_id=payload.supplier_id,
    )
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return _item_dict(item)


@router.get("/vendors")
async def list_vendors(
    current_user: User = Depends(require_roles(*ADMIN_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    """Thin passthrough to the procurement Supplier table, so the canteen
    admin UI can populate a supplier picker without knowing about
    /api/procurement/suppliers directly."""
    if not current_user.school_id:
        return []

    result = await session.execute(
        select(Supplier).where(Supplier.school_id == current_user.school_id).order_by(Supplier.name)
    )
    return [_vendor_dict(s) for s in result.scalars().all()]


@router.delete("/items/{item_id}", status_code=status.HTTP_200_OK)
async def delete_canteen_item(
    item_id: str,
    current_user: User = Depends(require_roles(*ADMIN_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(CanteenItem).where(
            CanteenItem.id == item_id,
            CanteenItem.school_id == current_user.school_id,
        )
    )
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Canteen item not found")

    item.is_active = False
    item.updated_at = datetime.utcnow()
    session.add(item)
    await session.commit()
    return {"success": True, "message": "Canteen item removed"}


# ── Wallet summary & history ────────────────────────────────────────────────

@router.get("/student/summary")
async def get_student_wallet_summary(
    current_user: User = Depends(require_roles(UserRole.STUDENT)),
    session: AsyncSession = Depends(get_session),
):
    student_record = await _get_student_record(session, current_user)
    service = CanteenWalletService(session)
    account = await service.get_or_create_account(
        session=session, school_id=current_user.school_id, student_id=student_record.id
    )
    await session.commit()
    return build_canteen_wallet_snapshot(account=account)


@router.get("/parent/summary")
async def get_parent_wallet_summary(
    student_id: str,
    current_user: User = Depends(require_roles(UserRole.PARENT)),
    session: AsyncSession = Depends(get_session),
):
    await _assert_parent_owns_student(current_user, student_id, session)
    service = CanteenWalletService(session)
    account = await service.get_or_create_account(
        session=session, school_id=current_user.school_id, student_id=student_id, parent_id=current_user.id
    )
    await session.commit()
    return build_canteen_wallet_snapshot(account=account)


@router.get("/student/history")
async def get_student_wallet_history(
    limit: int = 10,
    current_user: User = Depends(require_roles(UserRole.STUDENT)),
    session: AsyncSession = Depends(get_session),
):
    student_record = await _get_student_record(session, current_user)
    service = CanteenWalletService(session)
    return await service.list_ledger_entries(
        session=session, school_id=current_user.school_id, student_id=student_record.id, limit=limit
    )


@router.get("/parent/history")
async def get_parent_wallet_history(
    student_id: str,
    limit: int = 10,
    current_user: User = Depends(require_roles(UserRole.PARENT)),
    session: AsyncSession = Depends(get_session),
):
    await _assert_parent_owns_student(current_user, student_id, session)
    service = CanteenWalletService(session)
    return await service.list_ledger_entries(
        session=session, school_id=current_user.school_id, student_id=student_id, limit=limit
    )


# ── Spending controls (parent) ──────────────────────────────────────────────

@router.get("/parent/controls")
async def get_wallet_controls(
    student_id: str,
    current_user: User = Depends(require_roles(UserRole.PARENT)),
    session: AsyncSession = Depends(get_session),
):
    await _assert_parent_owns_student(current_user, student_id, session)
    service = CanteenWalletService(session)
    account = await service.get_or_create_account(
        session=session, school_id=current_user.school_id, student_id=student_id, parent_id=current_user.id
    )
    await session.commit()
    return build_canteen_wallet_snapshot(account=account)


@router.put("/parent/controls")
async def update_wallet_controls(
    student_id: str,
    payload: CanteenWalletControlsRequest,
    current_user: User = Depends(require_roles(UserRole.PARENT)),
    session: AsyncSession = Depends(get_session),
):
    await _assert_parent_owns_student(current_user, student_id, session)
    service = CanteenWalletService(session)
    account = await service.get_or_create_account(
        session=session, school_id=current_user.school_id, student_id=student_id, parent_id=current_user.id
    )

    result = await service.update_controls(
        session=session,
        account=account,
        frozen=payload.frozen,
        daily_limit=None if payload.clear_daily_limit else (payload.daily_limit if payload.daily_limit is not None else "__unset__"),
        weekly_limit=None if payload.clear_weekly_limit else (payload.weekly_limit if payload.weekly_limit is not None else "__unset__"),
        blocked_categories=payload.blocked_categories,
        low_balance_threshold=payload.low_balance_threshold,
    )
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "Could not update spending controls"))

    await session.commit()
    await session.refresh(account)
    return build_canteen_wallet_snapshot(account=account)


# ── Top-up (parent funds the wallet, real prepaid money) ───────────────────

@router.post("/parent/topup", status_code=status.HTTP_200_OK)
async def initiate_wallet_topup(
    payload: CanteenTopUpRequest,
    current_user: User = Depends(require_roles(UserRole.PARENT)),
    session: AsyncSession = Depends(get_session),
):
    if payload.amount <= 0:
        raise HTTPException(status_code=400, detail="Top-up amount must be greater than zero")
    if not current_user.school_id:
        raise HTTPException(status_code=400, detail="School context is required")
    await _assert_parent_owns_student(current_user, payload.student_id, session)

    paystack_secret_key = os.getenv("PAYSTACK_SECRET_KEY", "")
    if not paystack_secret_key:
        raise HTTPException(status_code=503, detail="Payment gateway not configured")

    payment_service = OnlinePaymentService(paystack_secret_key)
    transaction_id = f"TXN-{uuid.uuid4().hex[:12].upper()}"

    transaction = OnlineTransaction(
        school_id=current_user.school_id,
        fee_id=payload.student_id,
        student_id=payload.student_id,
        parent_id=current_user.id,
        amount=payload.amount,
        gateway="paystack",
        reference=transaction_id,
        transaction_type=TransactionType.CANTEEN_TOPUP,
        status=TransactionStatus.PENDING,
    )
    session.add(transaction)
    await session.flush()

    # Route straight to the school's own canteen Paystack subaccount — kept
    # independent of the fee-payment subaccount since a school's canteen is
    # often run against its own bank/MoMo account (e.g. a canteen
    # committee's account) — if it's been verified for direct settlement.
    school_result = await session.execute(select(School).where(School.id == current_user.school_id))
    school = school_result.scalar_one_or_none()
    subaccount = school.canteen_paystack_subaccount_code if school else None

    paystack_result = await payment_service.paystack.initialize_payment(
        amount_kobo=int(payload.amount * 100),
        email=current_user.email,
        reference=transaction_id,
        metadata={
            "student_id": payload.student_id,
            "canteen_topup": True,
        },
        subaccount=subaccount,
    )

    if not paystack_result.get("success"):
        transaction.status = TransactionStatus.FAILED
        transaction.failed_reason = paystack_result.get("error", "Payment initialization failed")
        session.add(transaction)
        await session.commit()
        raise HTTPException(status_code=500, detail="Payment initialization failed")

    transaction.payment_url = paystack_result["authorization_url"]
    transaction.access_code = paystack_result["access_code"]
    transaction.reference = paystack_result["reference"]
    transaction.status = TransactionStatus.PROCESSING
    session.add(transaction)
    await session.commit()

    return {
        "success": True,
        "transaction_id": str(transaction.id),
        "payment_url": paystack_result["authorization_url"],
        "reference": paystack_result["reference"],
        "amount": payload.amount,
        "message": "Wallet top-up payment initialized",
    }


# ── Counter staff: scan-to-identify ─────────────────────────────────────────
# Lets canteen staff identify a student by their existing ID-card QR
# (routers/id_cards.py's card_number) instead of the student needing to be
# logged in themselves — mirrors routers/kiosk.py's card lookup, but with no
# PIN step: a staff member is physically present watching the transaction,
# so the card alone is enough to know who to charge.

@router.get("/scan/{card_number}")
async def scan_student_card(
    card_number: str,
    current_user: User = Depends(require_roles(*ORDER_MANAGE_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    invalid_card = HTTPException(status_code=404, detail="Card not recognized or inactive.")

    query = select(IDCard).where(IDCard.card_number == card_number, IDCard.person_type == PersonType.STUDENT)
    if current_user.role != UserRole.SUPER_ADMIN:
        query = query.where(IDCard.school_id == current_user.school_id)
    result = await session.execute(query)
    card = result.scalar_one_or_none()
    if not card or card.status != IDCardStatus.ACTIVE:
        raise invalid_card

    result = await session.execute(select(Student).where(Student.id == card.person_id))
    student = result.scalar_one_or_none()
    if not student:
        raise invalid_card

    service = CanteenWalletService(session)
    account = await service.get_or_create_account(session=session, school_id=student.school_id, student_id=student.id)
    await session.commit()

    return {
        "student_id": student.id,
        "first_name": student.first_name,
        "last_name": student.last_name,
        "photo_url": student.photo_url,
        "wallet": build_canteen_wallet_snapshot(account=account),
    }


# ── Orders ───────────────────────────────────────────────────────────────────

@router.post("/orders", status_code=status.HTTP_201_CREATED)
async def place_order(
    payload: CanteenOrderCreateRequest,
    current_user: User = Depends(require_roles(UserRole.STUDENT, *ORDER_MANAGE_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    if current_user.role == UserRole.STUDENT:
        student = await _get_student_record(session, current_user)
    else:
        if not payload.student_id:
            raise HTTPException(status_code=400, detail="student_id is required")
        student = await _get_student_for_staff(session, current_user, payload.student_id)

    service = CanteenWalletService(session)
    result = await service.place_order(
        session=session,
        school_id=student.school_id,
        student_id=student.id,
        parent_id=None,
        cart=[{"item_id": line.item_id, "quantity": line.quantity} for line in payload.items],
        note=payload.note,
    )
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "Order could not be placed"))

    await session.commit()
    order = result["order"]
    lines = await _get_order_lines(session, order.id)
    return {"order": _order_dict(order, lines), "balance": result["balance"]}


@router.get("/orders")
async def list_orders(
    student_id: Optional[str] = None,
    order_status: Optional[str] = None,
    limit: int = 50,
    current_user: User = Depends(require_roles(UserRole.STUDENT, UserRole.PARENT, *ORDER_MANAGE_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    query = select(CanteenOrder)

    if current_user.role == UserRole.STUDENT:
        student_record = await _get_student_record(session, current_user)
        query = query.where(CanteenOrder.student_id == student_record.id)
    elif current_user.role == UserRole.PARENT:
        if not student_id:
            raise HTTPException(status_code=400, detail="student_id is required")
        await _assert_parent_owns_student(current_user, student_id, session)
        query = query.where(CanteenOrder.student_id == student_id)
    else:
        if current_user.role != UserRole.SUPER_ADMIN:
            query = query.where(CanteenOrder.school_id == current_user.school_id)
        if student_id:
            query = query.where(CanteenOrder.student_id == student_id)

    if order_status:
        statuses = [CanteenOrderStatus(s.strip()) for s in order_status.split(",") if s.strip()]
        if statuses:
            query = query.where(CanteenOrder.status.in_(statuses))

    query = query.order_by(CanteenOrder.created_at.desc()).limit(limit)
    result = await session.execute(query)
    orders = result.scalars().all()

    output = []
    for order in orders:
        lines = await _get_order_lines(session, order.id)
        output.append(_order_dict(order, lines))
    return output


@router.get("/orders/{order_id}")
async def get_order_detail(
    order_id: str,
    current_user: User = Depends(require_roles(UserRole.STUDENT, UserRole.PARENT, *ORDER_MANAGE_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(select(CanteenOrder).where(CanteenOrder.id == order_id))
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if current_user.role == UserRole.STUDENT:
        student_record = await _get_student_record(session, current_user)
        if order.student_id != student_record.id:
            raise HTTPException(status_code=403, detail="Not authorized for this order")
    elif current_user.role == UserRole.PARENT:
        await _assert_parent_owns_student(current_user, order.student_id, session)
    elif current_user.role != UserRole.SUPER_ADMIN and order.school_id != current_user.school_id:
        raise HTTPException(status_code=403, detail="Access denied for this school")

    lines = await _get_order_lines(session, order.id)
    return _order_dict(order, lines)


@router.post("/orders/{order_id}/cancel")
async def cancel_order(
    order_id: str,
    current_user: User = Depends(require_roles(UserRole.STUDENT)),
    session: AsyncSession = Depends(get_session),
):
    student_record = await _get_student_record(session, current_user)
    result = await session.execute(select(CanteenOrder).where(CanteenOrder.id == order_id))
    order = result.scalar_one_or_none()
    if not order or order.student_id != student_record.id:
        raise HTTPException(status_code=404, detail="Order not found")

    # A fast-tracked (no-prep) order reaches READY without ever being
    # accepted/prepared, so it's still safe for the student to back out of.
    # A made-to-order item that reached READY via the kitchen queue has
    # already consumed staff time and ingredients — self-cancel is only
    # offered while it's still PENDING for that one.
    if order.status == CanteenOrderStatus.READY and (order.accepted_at or order.preparing_at):
        raise HTTPException(status_code=400, detail="This order has already been prepared and can no longer be cancelled")

    service = CanteenWalletService(session)
    transition_result = await service.transition_status(
        session=session, order=order, new_status=CanteenOrderStatus.CANCELLED
    )
    if not transition_result.get("success"):
        raise HTTPException(status_code=400, detail=transition_result.get("error"))

    await session.commit()
    lines = await _get_order_lines(session, order.id)
    return _order_dict(transition_result["order"], lines)


@router.post("/orders/{order_id}/status")
async def update_order_status(
    order_id: str,
    payload: CanteenOrderStatusRequest,
    current_user: User = Depends(require_roles(*ORDER_MANAGE_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    order = await _get_order_for_admin(session, current_user, order_id)
    service = CanteenWalletService(session)
    result = await service.transition_status(
        session=session, order=order, new_status=payload.status, note=payload.note
    )
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))

    await session.commit()
    lines = await _get_order_lines(session, order.id)
    return _order_dict(result["order"], lines)


@router.post("/orders/{order_id}/verify-pickup")
async def verify_pickup(
    order_id: str,
    payload: CanteenPickupVerifyRequest,
    current_user: User = Depends(require_roles(*ORDER_MANAGE_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    order = await _get_order_for_admin(session, current_user, order_id)
    service = CanteenWalletService(session)
    result = await service.verify_pickup(session=session, order=order, pickup_code=payload.pickup_code)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))

    await session.commit()
    lines = await _get_order_lines(session, order.id)
    return _order_dict(result["order"], lines)


# ── Biometric device PIN mapping (face-scan counter payment + gate scans) ──
# The device registry itself is routers/integrations.py's existing
# biometric-devices CRUD (models.integrations.BiometricDevice) — reused
# as-is, not duplicated. This is the one genuinely new piece: recording
# which PIN a student was assigned at a terminal's own local enrollment
# step, so routers/biometric_adms.py's raw recognition-event push can
# resolve a scan to a student. Kept here (not in integrations.py) since it
# started as the canteen face-scan feature's join key — it's since become
# dual-purpose: the same PIN also resolves a gate/student-attendance
# device's scan (routers/gate_attendance.py's domain), which is why the
# role gate below reaches beyond canteen/school admins.

@router.put("/students/{student_id}/biometric-pin")
async def set_student_biometric_pin(
    student_id: str,
    payload: StudentBiometricPinRequest,
    current_user: User = Depends(require_roles(*BIOMETRIC_PIN_MANAGE_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(select(Student).where(Student.id == student_id))
    student = result.scalar_one_or_none()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    if current_user.role != UserRole.SUPER_ADMIN and student.school_id != current_user.school_id:
        raise HTTPException(status_code=403, detail="Access denied for this school")

    student.biometric_device_pin = payload.pin
    session.add(student)
    await session.commit()
    return {"ok": True}
