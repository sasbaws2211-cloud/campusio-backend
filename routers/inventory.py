"""Inventory / Asset Management Router"""
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from sqlmodel import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime
from typing import Optional, List
import logging

from models.inventory import (
    AssetCategory, AssetCategoryCreate, AssetCategoryUpdate,
    Asset, AssetCreate, AssetUpdate,
    StockItem, StockItemCreate, StockItemUpdate,
    StockIssuance, StockIssuanceCreate, IssuanceApprovalStatus, RejectIssuanceRequest,
)
from models.school import School
from models.user import User, UserRole
from database import get_session
from auth import get_current_user, require_roles

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/inventory", tags=["Inventory"])


async def _requires_maker_checker(session: AsyncSession, school_id: str) -> bool:
    """Whether this school has segregation-of-duties enabled

    Off by default (School.require_maker_checker) — small schools with a
    single storekeeper can't otherwise use the approval workflow. Mirrors
    services/journal_entry_service.py::requires_maker_checker exactly.
    """
    result = await session.execute(
        select(School.require_maker_checker).where(School.id == school_id)
    )
    return bool(result.scalar_one_or_none())


async def _apply_issuance(
    session: AsyncSession,
    item: StockItem,
    issuance: StockIssuance,
    approved_by: str,
) -> None:
    """Decrements stock and attempts GL posting — the actual physical/
    financial effect of an issuance, deferred until approval when
    maker-checker is on. Never raises on GL failure (see
    _post_stock_issuance_to_gl's docstring)."""
    item.quantity_on_hand -= issuance.quantity
    item.updated_at = datetime.utcnow()
    session.add(item)

    issuance.approval_status = IssuanceApprovalStatus.APPROVED
    issuance.approved_by = approved_by
    issuance.approved_at = datetime.utcnow()
    session.add(issuance)

    await session.commit()
    await session.refresh(issuance)
    await session.refresh(item)

    if item.gl_expense_account_code and item.gl_inventory_asset_account_code and item.unit_cost:
        try:
            journal_entry_id = await _post_stock_issuance_to_gl(
                session=session,
                school_id=item.school_id,
                issuance_id=issuance.id,
                expense_account_code=item.gl_expense_account_code,
                asset_account_code=item.gl_inventory_asset_account_code,
                amount=item.unit_cost * issuance.quantity,
                created_by=approved_by,
            )
            if journal_entry_id:
                issuance.gl_journal_entry_id = journal_entry_id
                session.add(issuance)
                await session.commit()
                await session.refresh(issuance)
                logger.info(f"Created journal entry {journal_entry_id} for stock issuance {issuance.id}")
            else:
                logger.warning(f"GL posting returned None for stock issuance {issuance.id} (GL accounts may not be configured)")
        except Exception as e:
            import traceback
            logger.error(f"Exception in GL posting for stock issuance {issuance.id}: {type(e).__name__}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")


async def _post_stock_issuance_to_gl(
    session: AsyncSession,
    school_id: str,
    issuance_id: str,
    expense_account_code: str,
    asset_account_code: str,
    amount: float,
    created_by: str,
) -> Optional[str]:
    """Dr. the stock item's expense account / Cr. its inventory asset
    account, for the cost of stock issued out. Both account codes come from
    the StockItem itself (school-configurable, not a hardcoded constant —
    see models/inventory.py). Returns the journal entry id, or None if GL
    accounts aren't configured or posting fails — never raises, since a GL
    failure must not block the physical stock issuance that already happened."""
    try:
        from services.journal_entry_service import JournalEntryService
        from models.finance import JournalEntryCreate, JournalLineItemCreate, ReferenceType
        from models.finance.chart_of_accounts import GLAccount

        expense_result = await session.execute(
            select(GLAccount).where(
                and_(GLAccount.account_code == expense_account_code, GLAccount.school_id == school_id)
            )
        )
        expense_account = expense_result.scalar_one_or_none()

        asset_result = await session.execute(
            select(GLAccount).where(
                and_(GLAccount.account_code == asset_account_code, GLAccount.school_id == school_id)
            )
        )
        asset_account = asset_result.scalar_one_or_none()

        if not expense_account or not asset_account:
            missing = []
            if not expense_account:
                missing.append(f"Expense account {expense_account_code}")
            if not asset_account:
                missing.append(f"Inventory asset account {asset_account_code}")
            logger.warning(
                f"GL accounts not found for stock issuance posting (school_id={school_id}). "
                f"Missing: {', '.join(missing)}. Please create these GL accounts first."
            )
            return None

        journal_entry = JournalEntryCreate(
            entry_date=datetime.utcnow().isoformat().split('T')[0],
            description=f"Stock issuance {issuance_id}",
            remarks="Auto-posted from inventory stock issuance",
            reference_type=ReferenceType.EXPENSE,
            reference_id=issuance_id,
            line_items=[
                JournalLineItemCreate(
                    gl_account_id=expense_account.id,
                    debit_amount=amount,
                    credit_amount=0.0,
                    description="Stock consumed",
                ),
                JournalLineItemCreate(
                    gl_account_id=asset_account.id,
                    debit_amount=0.0,
                    credit_amount=amount,
                    description="Inventory drawn down",
                ),
            ],
        )

        service = JournalEntryService(session)
        entry = await service.create_entry(school_id, journal_entry, created_by)
        return entry.id if entry else None

    except Exception as e:
        import traceback
        logger.error(f"Error posting stock issuance to GL: {type(e).__name__}: {str(e)}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        return None

WRITE_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.HR, UserRole.STOREKEEPER)


# ============================================================================
# ASSET CATEGORIES
# ============================================================================

@router.get("/categories", response_model=List[dict])
async def list_categories(
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(select(AssetCategory).where(AssetCategory.school_id == school_id))
    return [jsonable_encoder(c) for c in result.scalars().all()]


@router.post("/categories", response_model=dict)
async def create_category(
    data: AssetCategoryCreate,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    category = AssetCategory(**data.dict(), school_id=school_id)
    session.add(category)
    await session.commit()
    await session.refresh(category)
    return jsonable_encoder(category)


@router.put("/categories/{category_id}", response_model=dict)
async def update_category(
    category_id: str,
    data: AssetCategoryUpdate,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(AssetCategory).where(and_(AssetCategory.id == category_id, AssetCategory.school_id == school_id))
    )
    category = result.scalar_one_or_none()
    if not category:
        raise HTTPException(status_code=404, detail="Category not found")

    update_data = data.dict(exclude_unset=True)
    for key, value in update_data.items():
        setattr(category, key, value)
    category.updated_at = datetime.utcnow()

    session.add(category)
    await session.commit()
    await session.refresh(category)
    return jsonable_encoder(category)


@router.delete("/categories/{category_id}", response_model=dict)
async def delete_category(
    category_id: str,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(AssetCategory).where(and_(AssetCategory.id == category_id, AssetCategory.school_id == school_id))
    )
    category = result.scalar_one_or_none()
    if not category:
        raise HTTPException(status_code=404, detail="Category not found")

    await session.delete(category)
    await session.commit()
    return {"message": "Category deleted successfully", "id": category_id}


# ============================================================================
# ASSETS
# ============================================================================

@router.get("/assets", response_model=List[dict])
async def list_assets(
    status: Optional[str] = None,
    category_id: Optional[str] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    query = select(Asset).where(Asset.school_id == school_id)
    if status:
        query = query.where(Asset.status == status)
    if category_id:
        query = query.where(Asset.category_id == category_id)
    query = query.order_by(Asset.created_at.desc()).offset(skip).limit(limit)

    result = await session.execute(query)
    return [jsonable_encoder(a) for a in result.scalars().all()]


@router.post("/assets", response_model=dict)
async def create_asset(
    data: AssetCreate,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    existing = await session.execute(
        select(Asset).where(and_(Asset.tag_number == data.tag_number, Asset.school_id == school_id))
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="An asset with this tag number already exists")

    asset = Asset(**data.dict(), school_id=school_id)
    session.add(asset)
    await session.commit()
    await session.refresh(asset)
    return jsonable_encoder(asset)


@router.get("/assets/{asset_id}", response_model=dict)
async def get_asset(
    asset_id: str,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(select(Asset).where(and_(Asset.id == asset_id, Asset.school_id == school_id)))
    asset = result.scalar_one_or_none()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    return jsonable_encoder(asset)


@router.put("/assets/{asset_id}", response_model=dict)
async def update_asset(
    asset_id: str,
    data: AssetUpdate,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(select(Asset).where(and_(Asset.id == asset_id, Asset.school_id == school_id)))
    asset = result.scalar_one_or_none()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")

    update_data = data.dict(exclude_unset=True)
    for key, value in update_data.items():
        setattr(asset, key, value)
    asset.updated_at = datetime.utcnow()

    session.add(asset)
    await session.commit()
    await session.refresh(asset)
    return jsonable_encoder(asset)


@router.delete("/assets/{asset_id}", response_model=dict)
async def delete_asset(
    asset_id: str,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(select(Asset).where(and_(Asset.id == asset_id, Asset.school_id == school_id)))
    asset = result.scalar_one_or_none()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")

    await session.delete(asset)
    await session.commit()
    return {"message": "Asset deleted successfully", "id": asset_id}


# ============================================================================
# STOCK ITEMS
# ============================================================================

@router.get("/stock-items", response_model=List[dict])
async def list_stock_items(
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(select(StockItem).where(StockItem.school_id == school_id))
    return [jsonable_encoder(s) for s in result.scalars().all()]


@router.post("/stock-items", response_model=dict)
async def create_stock_item(
    data: StockItemCreate,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    item = StockItem(**data.dict(), school_id=school_id)
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return jsonable_encoder(item)


@router.put("/stock-items/{item_id}", response_model=dict)
async def update_stock_item(
    item_id: str,
    data: StockItemUpdate,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(select(StockItem).where(and_(StockItem.id == item_id, StockItem.school_id == school_id)))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Stock item not found")

    update_data = data.dict(exclude_unset=True)
    for key, value in update_data.items():
        setattr(item, key, value)
    item.updated_at = datetime.utcnow()

    session.add(item)
    await session.commit()
    await session.refresh(item)
    return jsonable_encoder(item)


@router.delete("/stock-items/{item_id}", response_model=dict)
async def delete_stock_item(
    item_id: str,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(select(StockItem).where(and_(StockItem.id == item_id, StockItem.school_id == school_id)))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Stock item not found")

    await session.delete(item)
    await session.commit()
    return {"message": "Stock item deleted successfully", "id": item_id}


@router.post("/stock-items/{item_id}/issue", response_model=dict)
async def issue_stock(
    item_id: str,
    data: StockIssuanceCreate,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    """Issue stock out to a person, decrementing quantity_on_hand.

    If the school has maker-checker enabled (School.require_maker_checker),
    the issuance is created PENDING instead: the quantity deduction and GL
    posting are deferred until a different user approves it via
    POST /stock-items/{item_id}/issuances/{issuance_id}/approve. Off by
    default, in which case this behaves exactly as before — issued and
    posted immediately."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(select(StockItem).where(and_(StockItem.id == item_id, StockItem.school_id == school_id)).with_for_update())
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Stock item not found")

    if data.quantity <= 0:
        raise HTTPException(status_code=400, detail="Quantity must be positive")
    if data.quantity > item.quantity_on_hand:
        raise HTTPException(status_code=400, detail="Insufficient stock on hand")

    maker_checker = await _requires_maker_checker(session, school_id)

    issuance = StockIssuance(
        **data.dict(),
        stock_item_id=item_id,
        school_id=school_id,
        issued_by_staff_id=current_user.id,
        approval_status=IssuanceApprovalStatus.PENDING if maker_checker else IssuanceApprovalStatus.APPROVED,
    )
    session.add(issuance)
    await session.commit()
    await session.refresh(issuance)

    if maker_checker:
        return {"issuance": jsonable_encoder(issuance), "stock_item": jsonable_encoder(item)}

    await _apply_issuance(session, item, issuance, approved_by=current_user.id)
    return {"issuance": jsonable_encoder(issuance), "stock_item": jsonable_encoder(item)}


@router.post("/stock-items/{item_id}/issuances/{issuance_id}/approve", response_model=dict)
async def approve_stock_issuance(
    item_id: str,
    issuance_id: str,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(StockIssuance).where(and_(StockIssuance.id == issuance_id, StockIssuance.stock_item_id == item_id, StockIssuance.school_id == school_id))
    )
    issuance = result.scalar_one_or_none()
    if not issuance:
        raise HTTPException(status_code=404, detail="Issuance not found")
    if issuance.approval_status != IssuanceApprovalStatus.PENDING:
        raise HTTPException(status_code=400, detail=f"Cannot approve an issuance with status {issuance.approval_status.value}")

    if issuance.issued_by_staff_id == current_user.id and await _requires_maker_checker(session, school_id):
        raise HTTPException(status_code=403, detail="Segregation of duties: you created this issuance and cannot also approve it")

    item_result = await session.execute(select(StockItem).where(and_(StockItem.id == item_id, StockItem.school_id == school_id)).with_for_update())
    item = item_result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Stock item not found")
    if issuance.quantity > item.quantity_on_hand:
        raise HTTPException(status_code=400, detail="Insufficient stock on hand to approve this issuance")

    await _apply_issuance(session, item, issuance, approved_by=current_user.id)
    return {"issuance": jsonable_encoder(issuance), "stock_item": jsonable_encoder(item)}


@router.post("/stock-items/{item_id}/issuances/{issuance_id}/reject", response_model=dict)
async def reject_stock_issuance(
    item_id: str,
    issuance_id: str,
    data: RejectIssuanceRequest,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(StockIssuance).where(and_(StockIssuance.id == issuance_id, StockIssuance.stock_item_id == item_id, StockIssuance.school_id == school_id))
    )
    issuance = result.scalar_one_or_none()
    if not issuance:
        raise HTTPException(status_code=404, detail="Issuance not found")
    if issuance.approval_status != IssuanceApprovalStatus.PENDING:
        raise HTTPException(status_code=400, detail=f"Cannot reject an issuance with status {issuance.approval_status.value}")

    if issuance.issued_by_staff_id == current_user.id and await _requires_maker_checker(session, school_id):
        raise HTTPException(status_code=403, detail="Segregation of duties: you created this issuance and cannot also reject it")

    issuance.approval_status = IssuanceApprovalStatus.REJECTED
    issuance.approved_by = current_user.id
    issuance.approved_at = datetime.utcnow()
    issuance.rejection_reason = data.rejection_reason
    session.add(issuance)
    await session.commit()
    await session.refresh(issuance)

    return jsonable_encoder(issuance)


@router.get("/stock-issuances", response_model=List[dict])
async def list_stock_issuances(
    stock_item_id: Optional[str] = None,
    approval_status: Optional[IssuanceApprovalStatus] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    query = select(StockIssuance).where(StockIssuance.school_id == school_id)
    if stock_item_id:
        query = query.where(StockIssuance.stock_item_id == stock_item_id)
    if approval_status:
        query = query.where(StockIssuance.approval_status == approval_status)
    query = query.order_by(StockIssuance.created_at.desc()).offset(skip).limit(limit)

    result = await session.execute(query)
    return [jsonable_encoder(i) for i in result.scalars().all()]
