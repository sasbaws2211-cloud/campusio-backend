"""Library acquisitions — requesting new titles, linking them to a real
purchase order in the procurement module, and receiving them straight into
the library catalog (creating the LibraryItem + copies) instead of leaving
"the book arrived" and "the book is in the catalog" as two disconnected
manual steps."""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import require_roles
from database import get_session
from dependencies import get_current_school_id
from models.library import LibraryItem
from models.library_circulation import LibraryBookCopy, CopyStatus
from models.library_acquisitions import (
    LibraryAcquisitionRequest,
    LibraryAcquisitionRequestCreate,
    LibraryAcquisitionLinkPO,
    LibraryAcquisitionReceive,
    AcquisitionStatus,
)
from models.procurement import PurchaseOrder
from models.user import User, UserRole

router = APIRouter(prefix="/library/acquisitions", tags=["Library Acquisitions"])

REQUEST_ROLES = (UserRole.SCHOOL_ADMIN, UserRole.SUPER_ADMIN, UserRole.TEACHER)
MANAGE_ROLES = (UserRole.SCHOOL_ADMIN, UserRole.SUPER_ADMIN)


def _to_dict(acquisition: LibraryAcquisitionRequest) -> dict:
    return {
        "id": acquisition.id,
        "title": acquisition.title,
        "author": acquisition.author,
        "isbn": acquisition.isbn,
        "category_id": acquisition.category_id,
        "quantity_requested": acquisition.quantity_requested,
        "quantity_received": acquisition.quantity_received,
        "status": acquisition.status,
        "purchase_order_id": acquisition.purchase_order_id,
        "library_item_id": acquisition.library_item_id,
        "requested_by": acquisition.requested_by,
        "notes": acquisition.notes,
        "created_at": acquisition.created_at,
        "updated_at": acquisition.updated_at,
    }


async def _get_or_404(session: AsyncSession, acquisition_id: str, school_id: str) -> LibraryAcquisitionRequest:
    result = await session.execute(
        select(LibraryAcquisitionRequest).where(LibraryAcquisitionRequest.id == acquisition_id, LibraryAcquisitionRequest.school_id == school_id)
    )
    acquisition = result.scalar_one_or_none()
    if not acquisition:
        raise HTTPException(status_code=404, detail="Acquisition request not found")
    return acquisition


@router.get("", response_model=List[dict])
async def list_acquisitions(
    status_filter: Optional[str] = Query(default=None, alias="status"),
    current_user: User = Depends(require_roles(*REQUEST_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = await get_current_school_id(current_user)
    stmt = select(LibraryAcquisitionRequest).where(LibraryAcquisitionRequest.school_id == school_id)
    if status_filter:
        stmt = stmt.where(LibraryAcquisitionRequest.status == status_filter)
    result = await session.execute(stmt.order_by(LibraryAcquisitionRequest.created_at.desc()))
    return [_to_dict(a) for a in result.scalars().all()]


@router.post("", response_model=dict, status_code=status.HTTP_201_CREATED)
async def create_acquisition(
    payload: LibraryAcquisitionRequestCreate,
    current_user: User = Depends(require_roles(*REQUEST_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = await get_current_school_id(current_user)
    acquisition = LibraryAcquisitionRequest(school_id=school_id, requested_by=current_user.id, **payload.model_dump())
    session.add(acquisition)
    await session.commit()
    await session.refresh(acquisition)
    return _to_dict(acquisition)


@router.post("/{acquisition_id}/link-purchase-order", response_model=dict)
async def link_purchase_order(
    acquisition_id: str,
    payload: LibraryAcquisitionLinkPO,
    current_user: User = Depends(require_roles(*MANAGE_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = await get_current_school_id(current_user)
    acquisition = await _get_or_404(session, acquisition_id, school_id)
    if acquisition.status not in (AcquisitionStatus.REQUESTED.value, AcquisitionStatus.ORDERED.value):
        raise HTTPException(status_code=400, detail=f"Cannot link a purchase order to a {acquisition.status} request")

    po = (
        await session.execute(select(PurchaseOrder).where(PurchaseOrder.id == payload.purchase_order_id, PurchaseOrder.school_id == school_id))
    ).scalar_one_or_none()
    if not po:
        raise HTTPException(status_code=400, detail="Purchase order not found in this school")

    acquisition.purchase_order_id = po.id
    acquisition.status = AcquisitionStatus.ORDERED.value
    acquisition.updated_at = datetime.utcnow()
    await session.commit()
    await session.refresh(acquisition)
    return _to_dict(acquisition)


@router.post("/{acquisition_id}/receive", response_model=dict)
async def receive_acquisition(
    acquisition_id: str,
    payload: LibraryAcquisitionReceive,
    current_user: User = Depends(require_roles(*MANAGE_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    """Marks copies of this request as received and creates them straight
    in the circulation catalog — a new LibraryItem the first time anything
    against this request is received, then just more LibraryBookCopy rows
    on subsequent partial receipts of the same request."""
    school_id = await get_current_school_id(current_user)
    acquisition = await _get_or_404(session, acquisition_id, school_id)
    if acquisition.status in (AcquisitionStatus.RECEIVED.value, AcquisitionStatus.CANCELLED.value):
        raise HTTPException(status_code=400, detail=f"This request is already {acquisition.status}")

    remaining = acquisition.quantity_requested - acquisition.quantity_received
    if payload.quantity_received > remaining:
        raise HTTPException(status_code=422, detail=f"Only {remaining} more cop{'y' if remaining == 1 else 'ies'} were requested")

    item: Optional[LibraryItem] = None
    if acquisition.library_item_id:
        item = (await session.execute(select(LibraryItem).where(LibraryItem.id == acquisition.library_item_id))).scalar_one_or_none()
    if not item:
        item = LibraryItem(
            school_id=school_id,
            title=acquisition.title,
            material_type="book",
            content_type="pdf",
            category_id=acquisition.category_id,
            author=acquisition.author,
            isbn=acquisition.isbn,
            is_published=True,
            created_by=current_user.id,
            updated_by=current_user.id,
        )
        session.add(item)
        await session.flush()
        acquisition.library_item_id = item.id

    for i in range(payload.quantity_received):
        barcode = f"ACQ-{acquisition.id[:8].upper()}-{acquisition.quantity_received + i + 1:03d}"
        session.add(
            LibraryBookCopy(
                school_id=school_id,
                item_id=item.id,
                barcode=barcode,
                condition="new",
                status=CopyStatus.AVAILABLE.value,
                location=payload.location,
            )
        )

    acquisition.quantity_received += payload.quantity_received
    acquisition.status = (
        AcquisitionStatus.RECEIVED.value if acquisition.quantity_received >= acquisition.quantity_requested else AcquisitionStatus.ORDERED.value
    )
    acquisition.updated_at = datetime.utcnow()
    await session.commit()
    await session.refresh(acquisition)
    return _to_dict(acquisition)


@router.post("/{acquisition_id}/cancel", response_model=dict)
async def cancel_acquisition(
    acquisition_id: str,
    current_user: User = Depends(require_roles(*MANAGE_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = await get_current_school_id(current_user)
    acquisition = await _get_or_404(session, acquisition_id, school_id)
    if acquisition.status == AcquisitionStatus.RECEIVED.value:
        raise HTTPException(status_code=400, detail="Cannot cancel a request that's already been received")
    acquisition.status = AcquisitionStatus.CANCELLED.value
    acquisition.updated_at = datetime.utcnow()
    await session.commit()
    return _to_dict(acquisition)
