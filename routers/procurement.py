"""Supplier, purchase-order, and stock-receiving workflow."""
from datetime import datetime
from typing import Optional
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import SQLModel, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_current_user, require_roles
from database import get_session
from models.inventory import StockItem
from models.procurement import (
    PurchaseOrder, PurchaseOrderCreate, PurchaseOrderLine, PurchaseOrderStatus,
    Supplier, SupplierCreate, SupplierStatus, SupplierUpdate, PurchaseRequisition,
    PurchaseRequisitionLine, PurchaseRequisitionCreate, RequisitionStatus,
    GoodsReceivedNote, GoodsReceivedLine, StockReturn, StockReturnCreate, StockReturnStatus,
    StockTransfer, StockTransferCreate, StockTransferStatus,
    SupplierInvoice, SupplierInvoiceCreate, SupplierInvoicePayment, SupplierInvoiceStatus,
)
from models.user import User, UserRole

router = APIRouter(prefix="/procurement", tags=["Procurement"])
WRITE_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.STOREKEEPER, UserRole.HR)
ADMIN_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)


class ReceiveLine(SQLModel):
    line_id: str
    quantity: float


class ReceiveRequest(SQLModel):
    lines: list[ReceiveLine]


class RequisitionDecision(SQLModel):
    rejection_reason: Optional[str] = None


class ConvertLineCost(SQLModel):
    stock_item_id: str
    unit_cost: float = 0


class ConvertRequisitionRequest(SQLModel):
    supplier_id: str
    order_date: str
    expected_date: Optional[str] = None
    line_costs: list[ConvertLineCost] = []


class RejectionRequest(SQLModel):
    rejection_reason: Optional[str] = None


def _school_id(user: User) -> str:
    if not user.school_id:
        raise HTTPException(status_code=403, detail="No school context")
    return user.school_id


def _supplier_dict(item: Supplier) -> dict:
    return {"id": item.id, "name": item.name, "contact_name": item.contact_name, "phone": item.phone, "email": item.email, "address": item.address, "tax_number": item.tax_number, "status": item.status.value}


async def _order_dict(order: PurchaseOrder, session: AsyncSession) -> dict:
    supplier = (await session.execute(select(Supplier).where(Supplier.id == order.supplier_id))).scalar_one()
    lines = (await session.execute(select(PurchaseOrderLine).where(PurchaseOrderLine.purchase_order_id == order.id))).scalars().all()
    return {"id": order.id, "order_number": order.order_number, "supplier": _supplier_dict(supplier), "order_date": order.order_date, "expected_date": order.expected_date, "status": order.status.value, "notes": order.notes, "created_by": order.created_by, "approved_by": order.approved_by, "lines": [{"id": line.id, "stock_item_id": line.stock_item_id, "quantity_ordered": line.quantity_ordered, "quantity_received": line.quantity_received, "unit_cost": line.unit_cost} for line in lines]}


async def _requisition_dict(item: PurchaseRequisition, session: AsyncSession) -> dict:
    lines = (await session.execute(select(PurchaseRequisitionLine).where(PurchaseRequisitionLine.requisition_id == item.id))).scalars().all()
    return {"id": item.id, "requisition_number": item.requisition_number, "requested_by": item.requested_by, "department": item.department, "purpose": item.purpose, "status": item.status.value, "approved_by": item.approved_by, "approved_at": item.approved_at, "rejection_reason": item.rejection_reason, "created_at": item.created_at, "lines": [{"id": line.id, "stock_item_id": line.stock_item_id, "quantity_requested": line.quantity_requested, "notes": line.notes} for line in lines]}


def _invoice_dict(item: SupplierInvoice) -> dict:
    return {"id": item.id, "supplier_id": item.supplier_id, "purchase_order_id": item.purchase_order_id, "invoice_number": item.invoice_number, "invoice_date": item.invoice_date, "due_date": item.due_date, "total_amount": item.total_amount, "amount_paid": item.amount_paid, "balance": round(item.total_amount - item.amount_paid, 2), "currency": item.currency, "status": item.status.value, "notes": item.notes, "created_by": item.created_by, "created_at": item.created_at}


@router.get("/requisitions", response_model=list[dict])
async def list_requisitions(current_user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(PurchaseRequisition).where(PurchaseRequisition.school_id == _school_id(current_user)).order_by(PurchaseRequisition.created_at.desc()))
    return [await _requisition_dict(item, session) for item in result.scalars().all()]


@router.post("/requisitions", response_model=dict)
async def create_requisition(payload: PurchaseRequisitionCreate, current_user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    school_id = _school_id(current_user)
    if not payload.lines or any(line.quantity_requested <= 0 for line in payload.lines):
        raise HTTPException(status_code=400, detail="A requisition needs positive quantities")
    stock_ids = [line.stock_item_id for line in payload.lines]
    stock_count = (await session.execute(select(StockItem.id).where(StockItem.school_id == school_id, StockItem.id.in_(stock_ids)))).all()
    if len(stock_count) != len(set(stock_ids)):
        raise HTTPException(status_code=400, detail="Every requisition item must exist in this school")
    item = PurchaseRequisition(school_id=school_id, requisition_number=f"PR-{datetime.utcnow().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}", requested_by=current_user.id, department=payload.department, purpose=payload.purpose)
    session.add(item)
    await session.flush()
    for line in payload.lines:
        session.add(PurchaseRequisitionLine(requisition_id=item.id, **line.model_dump()))
    await session.commit()
    return await _requisition_dict(item, session)


@router.post("/requisitions/{requisition_id}/submit", response_model=dict)
async def submit_requisition(requisition_id: str, current_user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(PurchaseRequisition).where(PurchaseRequisition.id == requisition_id, PurchaseRequisition.school_id == _school_id(current_user)))
    item = result.scalar_one_or_none()
    if not item or item.status != RequisitionStatus.DRAFT:
        raise HTTPException(status_code=400, detail="Only draft requisitions can be submitted")
    item.status = RequisitionStatus.SUBMITTED
    item.updated_at = datetime.utcnow()
    session.add(item)
    await session.commit()
    return await _requisition_dict(item, session)


@router.post("/requisitions/{requisition_id}/approve", response_model=dict)
async def approve_requisition(requisition_id: str, decision: RequisitionDecision = RequisitionDecision(), current_user: User = Depends(require_roles(*ADMIN_ROLES)), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(PurchaseRequisition).where(PurchaseRequisition.id == requisition_id, PurchaseRequisition.school_id == _school_id(current_user)))
    item = result.scalar_one_or_none()
    if not item or item.status != RequisitionStatus.SUBMITTED:
        raise HTTPException(status_code=400, detail="Only submitted requisitions can be approved")
    item.status = RequisitionStatus.APPROVED
    item.approved_by = current_user.id
    item.approved_at = datetime.utcnow()
    session.add(item)
    await session.commit()
    return await _requisition_dict(item, session)


@router.post("/requisitions/{requisition_id}/reject", response_model=dict)
async def reject_requisition(requisition_id: str, decision: RequisitionDecision = RequisitionDecision(), current_user: User = Depends(require_roles(*ADMIN_ROLES)), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(PurchaseRequisition).where(PurchaseRequisition.id == requisition_id, PurchaseRequisition.school_id == _school_id(current_user)))
    item = result.scalar_one_or_none()
    if not item or item.status != RequisitionStatus.SUBMITTED:
        raise HTTPException(status_code=400, detail="Only submitted requisitions can be rejected")
    item.status = RequisitionStatus.REJECTED
    item.approved_by = current_user.id
    item.approved_at = datetime.utcnow()
    item.rejection_reason = decision.rejection_reason
    session.add(item)
    await session.commit()
    return await _requisition_dict(item, session)


@router.post("/requisitions/{requisition_id}/convert", response_model=dict)
async def convert_requisition(requisition_id: str, payload: ConvertRequisitionRequest, current_user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    """Turn an approved requisition into a draft purchase order — the missing
    link between "someone asked for this" and "we ordered it from a
    supplier." The requisition only carries stock items + quantities (no
    supplier or cost), so the caller supplies those here; anything not
    priced defaults to 0 and can be edited before the order is submitted."""
    school_id = _school_id(current_user)
    result = await session.execute(select(PurchaseRequisition).where(PurchaseRequisition.id == requisition_id, PurchaseRequisition.school_id == school_id))
    item = result.scalar_one_or_none()
    if not item or item.status != RequisitionStatus.APPROVED:
        raise HTTPException(status_code=400, detail="Only approved requisitions can be converted to a purchase order")
    supplier = (await session.execute(select(Supplier).where(Supplier.id == payload.supplier_id, Supplier.school_id == school_id, Supplier.status == SupplierStatus.ACTIVE))).scalar_one_or_none()
    if not supplier:
        raise HTTPException(status_code=400, detail="Active supplier not found for this school")
    req_lines = (await session.execute(select(PurchaseRequisitionLine).where(PurchaseRequisitionLine.requisition_id == item.id))).scalars().all()
    if not req_lines:
        raise HTTPException(status_code=400, detail="Requisition has no lines to convert")
    cost_by_item = {line.stock_item_id: line.unit_cost for line in payload.line_costs}
    order = PurchaseOrder(school_id=school_id, supplier_id=supplier.id, order_number=f"PO-{datetime.utcnow().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}", order_date=payload.order_date, expected_date=payload.expected_date, notes=f"Converted from requisition {item.requisition_number}", created_by=current_user.id)
    session.add(order)
    await session.flush()
    for line in req_lines:
        session.add(PurchaseOrderLine(purchase_order_id=order.id, stock_item_id=line.stock_item_id, quantity_ordered=line.quantity_requested, unit_cost=cost_by_item.get(line.stock_item_id, 0)))
    item.status = RequisitionStatus.CONVERTED
    session.add(item)
    await session.commit()
    result = await _order_dict(order, session)
    result["converted_from_requisition"] = item.requisition_number
    return result


@router.get("/suppliers", response_model=list[dict])
async def list_suppliers(current_user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(Supplier).where(Supplier.school_id == _school_id(current_user)).order_by(Supplier.name))
    return [_supplier_dict(item) for item in result.scalars().all()]


@router.get("/supplier-invoices", response_model=list[dict])
async def list_supplier_invoices(current_user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    records = (await session.execute(select(SupplierInvoice).where(SupplierInvoice.school_id == _school_id(current_user)).order_by(SupplierInvoice.invoice_date.desc()))).scalars().all()
    return [_invoice_dict(record) for record in records]


@router.post("/supplier-invoices", response_model=dict)
async def create_supplier_invoice(payload: SupplierInvoiceCreate, current_user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    school_id = _school_id(current_user)
    if payload.total_amount <= 0:
        raise HTTPException(status_code=400, detail="Invoice amount must be positive")
    supplier = (await session.execute(select(Supplier).where(Supplier.id == payload.supplier_id, Supplier.school_id == school_id))).scalar_one_or_none()
    if not supplier:
        raise HTTPException(status_code=404, detail="Supplier not found")
    if payload.purchase_order_id:
        order = (await session.execute(select(PurchaseOrder).where(PurchaseOrder.id == payload.purchase_order_id, PurchaseOrder.school_id == school_id))).scalar_one_or_none()
        if not order or order.supplier_id != supplier.id or order.status not in (PurchaseOrderStatus.PARTIALLY_RECEIVED, PurchaseOrderStatus.RECEIVED):
            raise HTTPException(status_code=400, detail="Invoice must reference a received purchase order for matching")
    duplicate = (await session.execute(select(SupplierInvoice).where(SupplierInvoice.school_id == school_id, SupplierInvoice.supplier_id == supplier.id, SupplierInvoice.invoice_number == payload.invoice_number))).scalar_one_or_none()
    if duplicate:
        raise HTTPException(status_code=409, detail="Invoice number already exists for this supplier")
    record = SupplierInvoice(school_id=school_id, created_by=current_user.id, **payload.model_dump())
    session.add(record)
    await session.commit()
    await session.refresh(record)
    return _invoice_dict(record)


@router.post("/supplier-invoices/{invoice_id}/payments", response_model=dict)
async def pay_supplier_invoice(invoice_id: str, payload: SupplierInvoicePayment, current_user: User = Depends(require_roles(*ADMIN_ROLES)), session: AsyncSession = Depends(get_session)):
    record = (await session.execute(select(SupplierInvoice).where(SupplierInvoice.id == invoice_id, SupplierInvoice.school_id == _school_id(current_user)))).scalar_one_or_none()
    if not record or record.status == SupplierInvoiceStatus.VOID:
        raise HTTPException(status_code=404, detail="Open supplier invoice not found")
    if payload.amount <= 0 or record.amount_paid + payload.amount > record.total_amount:
        raise HTTPException(status_code=400, detail="Payment exceeds the invoice balance")
    record.amount_paid += payload.amount
    record.status = SupplierInvoiceStatus.PAID if record.amount_paid == record.total_amount else SupplierInvoiceStatus.PARTIALLY_PAID
    record.updated_at = datetime.utcnow()
    session.add(record)
    await session.commit()
    await session.refresh(record)
    return _invoice_dict(record)


@router.get("/supplier-balances", response_model=list[dict])
async def supplier_balances(current_user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    scope = _school_id(current_user)
    suppliers = (await session.execute(select(Supplier).where(Supplier.school_id == scope).order_by(Supplier.name))).scalars().all()
    invoices = (await session.execute(select(SupplierInvoice).where(SupplierInvoice.school_id == scope, SupplierInvoice.status != SupplierInvoiceStatus.VOID))).scalars().all()
    return [{"supplier_id": supplier.id, "supplier_name": supplier.name, "invoice_count": sum(1 for invoice in invoices if invoice.supplier_id == supplier.id), "total_invoiced": sum(invoice.total_amount for invoice in invoices if invoice.supplier_id == supplier.id), "total_paid": sum(invoice.amount_paid for invoice in invoices if invoice.supplier_id == supplier.id), "balance": sum(invoice.total_amount - invoice.amount_paid for invoice in invoices if invoice.supplier_id == supplier.id)} for supplier in suppliers]


@router.get("/reorder-suggestions", response_model=list[dict])
async def reorder_suggestions(current_user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    items = (await session.execute(select(StockItem).where(StockItem.school_id == _school_id(current_user), StockItem.reorder_level != None, StockItem.quantity_on_hand <= StockItem.reorder_level).order_by(StockItem.quantity_on_hand))).scalars().all()
    return [{"stock_item_id": item.id, "name": item.name, "location": item.location, "quantity_on_hand": item.quantity_on_hand, "reorder_level": item.reorder_level, "suggested_quantity": max((item.reorder_level or 0) * 2 - item.quantity_on_hand, 1)} for item in items]


@router.get("/transfers", response_model=list[dict])
async def list_transfers(current_user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    records = (await session.execute(select(StockTransfer).where(StockTransfer.school_id == _school_id(current_user)).order_by(StockTransfer.created_at.desc()))).scalars().all()
    return [record.model_dump() for record in records]


@router.post("/transfers", response_model=dict)
async def create_transfer(payload: StockTransferCreate, current_user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    school_id = _school_id(current_user)
    if payload.quantity <= 0 or payload.source_location == payload.destination_location:
        raise HTTPException(status_code=400, detail="Transfer quantity must be positive and locations must differ")
    item = (await session.execute(select(StockItem).where(StockItem.id == payload.stock_item_id, StockItem.school_id == school_id))).scalar_one_or_none()
    if not item or (item.location and item.location != payload.source_location):
        raise HTTPException(status_code=400, detail="Stock item is not held at the requested source location")
    if payload.quantity > item.quantity_on_hand:
        raise HTTPException(status_code=400, detail="Transfer quantity exceeds available stock")
    record = StockTransfer(school_id=school_id, requested_by=current_user.id, **payload.model_dump())
    session.add(record)
    await session.commit()
    await session.refresh(record)
    return record.model_dump()


@router.post("/transfers/{transfer_id}/approve", response_model=dict)
async def approve_transfer(transfer_id: str, current_user: User = Depends(require_roles(*ADMIN_ROLES)), session: AsyncSession = Depends(get_session)):
    school_id = _school_id(current_user)
    record = (await session.execute(select(StockTransfer).where(StockTransfer.id == transfer_id, StockTransfer.school_id == school_id).with_for_update())).scalar_one_or_none()
    if not record or record.status != StockTransferStatus.PENDING:
        raise HTTPException(status_code=400, detail="Only pending transfers can be approved")
    item = (await session.execute(select(StockItem).where(StockItem.id == record.stock_item_id, StockItem.school_id == school_id).with_for_update())).scalar_one_or_none()
    if not item or record.quantity > item.quantity_on_hand or (item.location and item.location != record.source_location):
        raise HTTPException(status_code=409, detail="Stock is no longer available at the source location")
    if record.quantity != item.quantity_on_hand:
        raise HTTPException(status_code=409, detail="Partial transfers require location-level stock balances")
    item.location = record.destination_location
    item.updated_at = datetime.utcnow()
    record.status = StockTransferStatus.APPROVED
    record.approved_by = current_user.id
    record.approved_at = datetime.utcnow()
    session.add(item)
    session.add(record)
    await session.commit()
    await session.refresh(record)
    return record.model_dump()


@router.post("/transfers/{transfer_id}/reject", response_model=dict)
async def reject_transfer(transfer_id: str, current_user: User = Depends(require_roles(*ADMIN_ROLES)), session: AsyncSession = Depends(get_session)):
    school_id = _school_id(current_user)
    record = (await session.execute(select(StockTransfer).where(StockTransfer.id == transfer_id, StockTransfer.school_id == school_id))).scalar_one_or_none()
    if not record or record.status != StockTransferStatus.PENDING:
        raise HTTPException(status_code=400, detail="Only pending transfers can be rejected")
    record.status = StockTransferStatus.REJECTED
    record.approved_by = current_user.id
    record.approved_at = datetime.utcnow()
    session.add(record)
    await session.commit()
    await session.refresh(record)
    return record.model_dump()


@router.post("/suppliers", response_model=dict)
async def create_supplier(payload: SupplierCreate, current_user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    school_id = _school_id(current_user)
    existing = await session.execute(select(Supplier).where(Supplier.school_id == school_id, Supplier.name == payload.name))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="A supplier with this name already exists")
    supplier = Supplier(school_id=school_id, **payload.model_dump())
    session.add(supplier)
    await session.commit()
    await session.refresh(supplier)
    return _supplier_dict(supplier)


@router.put("/suppliers/{supplier_id}", response_model=dict)
async def update_supplier(supplier_id: str, payload: SupplierUpdate, current_user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(Supplier).where(Supplier.id == supplier_id, Supplier.school_id == _school_id(current_user)))
    supplier = result.scalar_one_or_none()
    if not supplier:
        raise HTTPException(status_code=404, detail="Supplier not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(supplier, key, value)
    supplier.updated_at = datetime.utcnow()
    session.add(supplier)
    await session.commit()
    await session.refresh(supplier)
    return _supplier_dict(supplier)


@router.get("/purchase-orders", response_model=list[dict])
async def list_purchase_orders(current_user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(PurchaseOrder).where(PurchaseOrder.school_id == _school_id(current_user)).order_by(PurchaseOrder.created_at.desc()))
    return [await _order_dict(order, session) for order in result.scalars().all()]


@router.post("/purchase-orders", response_model=dict)
async def create_purchase_order(payload: PurchaseOrderCreate, current_user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    school_id = _school_id(current_user)
    if not payload.lines:
        raise HTTPException(status_code=400, detail="At least one purchase-order line is required")
    supplier = (await session.execute(select(Supplier).where(Supplier.id == payload.supplier_id, Supplier.school_id == school_id, Supplier.status == SupplierStatus.ACTIVE))).scalar_one_or_none()
    if not supplier:
        raise HTTPException(status_code=400, detail="Active supplier not found for this school")
    stock_ids = [line.stock_item_id for line in payload.lines]
    stock_items = (await session.execute(select(StockItem).where(StockItem.school_id == school_id, StockItem.id.in_(stock_ids)))).scalars().all()
    stock_by_id = {item.id: item for item in stock_items}
    if len(stock_by_id) != len(set(stock_ids)):
        raise HTTPException(status_code=400, detail="Every stock item must exist in this school")
    if any(line.quantity_ordered <= 0 or line.unit_cost < 0 for line in payload.lines):
        raise HTTPException(status_code=400, detail="Order quantities must be positive and costs cannot be negative")
    order = PurchaseOrder(school_id=school_id, supplier_id=supplier.id, order_number=f"PO-{datetime.utcnow().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}", order_date=payload.order_date, expected_date=payload.expected_date, notes=payload.notes, created_by=current_user.id)
    session.add(order)
    await session.flush()
    for line in payload.lines:
        session.add(PurchaseOrderLine(purchase_order_id=order.id, stock_item_id=line.stock_item_id, quantity_ordered=line.quantity_ordered, unit_cost=line.unit_cost))
    await session.commit()
    return await _order_dict(order, session)


@router.post("/purchase-orders/{order_id}/submit", response_model=dict)
async def submit_purchase_order(order_id: str, current_user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(PurchaseOrder).where(PurchaseOrder.id == order_id, PurchaseOrder.school_id == _school_id(current_user)))
    order = result.scalar_one_or_none()
    if not order or order.status != PurchaseOrderStatus.DRAFT:
        raise HTTPException(status_code=400, detail="Only draft purchase orders can be submitted")
    order.status = PurchaseOrderStatus.SUBMITTED
    order.updated_at = datetime.utcnow()
    session.add(order)
    await session.commit()
    return await _order_dict(order, session)


@router.post("/purchase-orders/{order_id}/cancel", response_model=dict)
async def cancel_purchase_order(order_id: str, current_user: User = Depends(require_roles(*ADMIN_ROLES)), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(PurchaseOrder).where(PurchaseOrder.id == order_id, PurchaseOrder.school_id == _school_id(current_user)))
    order = result.scalar_one_or_none()
    if not order or order.status not in (PurchaseOrderStatus.DRAFT, PurchaseOrderStatus.SUBMITTED, PurchaseOrderStatus.APPROVED):
        raise HTTPException(status_code=400, detail="Only orders that haven't started receiving can be cancelled")
    order.status = PurchaseOrderStatus.CANCELLED
    order.updated_at = datetime.utcnow()
    session.add(order)
    await session.commit()
    return await _order_dict(order, session)


@router.post("/purchase-orders/{order_id}/approve", response_model=dict)
async def approve_purchase_order(order_id: str, current_user: User = Depends(require_roles(*ADMIN_ROLES)), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(PurchaseOrder).where(PurchaseOrder.id == order_id, PurchaseOrder.school_id == _school_id(current_user)))
    order = result.scalar_one_or_none()
    if not order or order.status != PurchaseOrderStatus.SUBMITTED:
        raise HTTPException(status_code=400, detail="Only submitted purchase orders can be approved")
    order.status = PurchaseOrderStatus.APPROVED
    order.approved_by = current_user.id
    order.approved_at = datetime.utcnow()
    order.updated_at = datetime.utcnow()
    session.add(order)
    await session.commit()
    return await _order_dict(order, session)


@router.post("/purchase-orders/{order_id}/receive", response_model=dict)
async def receive_purchase_order(order_id: str, payload: ReceiveRequest, current_user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    school_id = _school_id(current_user)
    if not payload.lines:
        raise HTTPException(status_code=400, detail="At least one received line is required")
    result = await session.execute(select(PurchaseOrder).where(PurchaseOrder.id == order_id, PurchaseOrder.school_id == school_id).with_for_update())
    order = result.scalar_one_or_none()
    if not order or order.status not in (PurchaseOrderStatus.APPROVED, PurchaseOrderStatus.PARTIALLY_RECEIVED):
        raise HTTPException(status_code=400, detail="Only approved orders can be received")
    lines = (await session.execute(select(PurchaseOrderLine).where(PurchaseOrderLine.purchase_order_id == order.id).with_for_update())).scalars().all()
    line_by_id = {line.id: line for line in lines}
    stock_items = (await session.execute(select(StockItem).where(StockItem.school_id == school_id, StockItem.id.in_([line.stock_item_id for line in lines])).with_for_update())).scalars().all()
    stock_by_id = {item.id: item for item in stock_items}
    grn = GoodsReceivedNote(school_id=school_id, grn_number=f"GRN-{datetime.utcnow().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}", purchase_order_id=order.id, received_by=current_user.id)
    session.add(grn)
    await session.flush()
    for received in payload.lines:
        line = line_by_id.get(received.line_id)
        if not line or received.quantity <= 0 or received.quantity > line.quantity_ordered - line.quantity_received:
            raise HTTPException(status_code=400, detail="Received quantity exceeds the outstanding order quantity")
        line.quantity_received += received.quantity
        stock_by_id[line.stock_item_id].quantity_on_hand += received.quantity
        stock_by_id[line.stock_item_id].updated_at = datetime.utcnow()
        session.add(GoodsReceivedLine(grn_id=grn.id, purchase_order_line_id=line.id, quantity_received=received.quantity))
        session.add(line)
        session.add(stock_by_id[line.stock_item_id])
    order.status = PurchaseOrderStatus.RECEIVED if all(line.quantity_received >= line.quantity_ordered for line in lines) else PurchaseOrderStatus.PARTIALLY_RECEIVED
    order.updated_at = datetime.utcnow()
    session.add(order)
    await session.commit()
    result = await _order_dict(order, session)
    result["grn_number"] = grn.grn_number
    return result


@router.get("/goods-received-notes", response_model=list[dict])
async def list_goods_received_notes(current_user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    notes = (await session.execute(select(GoodsReceivedNote).where(GoodsReceivedNote.school_id == _school_id(current_user)).order_by(GoodsReceivedNote.received_at.desc()))).scalars().all()
    response = []
    for note in notes:
        lines = (await session.execute(select(GoodsReceivedLine).where(GoodsReceivedLine.grn_id == note.id))).scalars().all()
        response.append({"id": note.id, "grn_number": note.grn_number, "purchase_order_id": note.purchase_order_id, "received_by": note.received_by, "received_at": note.received_at, "notes": note.notes, "lines": [line.model_dump() for line in lines]})
    return response


@router.post("/returns", response_model=dict)
async def create_stock_return(payload: StockReturnCreate, current_user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    school_id = _school_id(current_user)
    item = (await session.execute(select(StockItem).where(StockItem.id == payload.stock_item_id, StockItem.school_id == school_id))).scalar_one_or_none()
    if not item or payload.quantity <= 0 or payload.quantity > item.quantity_on_hand:
        raise HTTPException(status_code=400, detail="Return quantity exceeds available stock")
    record = StockReturn(school_id=school_id, requested_by=current_user.id, **payload.model_dump())
    session.add(record)
    await session.commit()
    await session.refresh(record)
    return record.model_dump()


@router.post("/returns/{return_id}/approve", response_model=dict)
async def approve_stock_return(return_id: str, current_user: User = Depends(require_roles(*ADMIN_ROLES)), session: AsyncSession = Depends(get_session)):
    school_id = _school_id(current_user)
    record = (await session.execute(select(StockReturn).where(StockReturn.id == return_id, StockReturn.school_id == school_id).with_for_update())).scalar_one_or_none()
    if not record or record.status != StockReturnStatus.PENDING:
        raise HTTPException(status_code=400, detail="Only pending returns can be approved")
    item = (await session.execute(select(StockItem).where(StockItem.id == record.stock_item_id, StockItem.school_id == school_id).with_for_update())).scalar_one_or_none()
    if not item or record.quantity > item.quantity_on_hand:
        raise HTTPException(status_code=409, detail="Stock is no longer available for this return")
    item.quantity_on_hand -= record.quantity
    item.updated_at = datetime.utcnow()
    record.status = StockReturnStatus.APPROVED
    record.approved_by = current_user.id
    record.approved_at = datetime.utcnow()
    session.add(item)
    session.add(record)
    await session.commit()
    await session.refresh(record)
    return record.model_dump()


@router.post("/returns/{return_id}/reject", response_model=dict)
async def reject_stock_return(return_id: str, current_user: User = Depends(require_roles(*ADMIN_ROLES)), session: AsyncSession = Depends(get_session)):
    school_id = _school_id(current_user)
    record = (await session.execute(select(StockReturn).where(StockReturn.id == return_id, StockReturn.school_id == school_id))).scalar_one_or_none()
    if not record or record.status != StockReturnStatus.PENDING:
        raise HTTPException(status_code=400, detail="Only pending returns can be rejected")
    record.status = StockReturnStatus.REJECTED
    record.approved_by = current_user.id
    record.approved_at = datetime.utcnow()
    session.add(record)
    await session.commit()
    await session.refresh(record)
    return record.model_dump()


@router.get("/returns", response_model=list[dict])
async def list_stock_returns(current_user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    records = (await session.execute(select(StockReturn).where(StockReturn.school_id == _school_id(current_user)).order_by(StockReturn.created_at.desc()))).scalars().all()
    return [record.model_dump() for record in records]


@router.post("/supplier-invoices/{invoice_id}/void", response_model=dict)
async def void_supplier_invoice(invoice_id: str, current_user: User = Depends(require_roles(*ADMIN_ROLES)), session: AsyncSession = Depends(get_session)):
    record = (await session.execute(select(SupplierInvoice).where(SupplierInvoice.id == invoice_id, SupplierInvoice.school_id == _school_id(current_user)))).scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=404, detail="Supplier invoice not found")
    if record.status == SupplierInvoiceStatus.VOID:
        raise HTTPException(status_code=400, detail="Invoice is already void")
    if record.amount_paid > 0:
        raise HTTPException(status_code=400, detail="An invoice with payments recorded against it cannot be voided")
    record.status = SupplierInvoiceStatus.VOID
    record.updated_at = datetime.utcnow()
    session.add(record)
    await session.commit()
    await session.refresh(record)
    return _invoice_dict(record)