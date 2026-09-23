from __future__ import annotations

import random
import string
import uuid
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from models.canteen_wallet import (
    CanteenItem,
    CanteenOrder,
    CanteenOrderItem,
    CanteenOrderStatus,
    CanteenWalletAccount,
    CanteenWalletLedgerEntry,
)
from models.payment import OnlineTransaction

logger = logging.getLogger(__name__)

# Valid forward transitions for an order's lifecycle. Anything not listed
# here (including every transition out of a terminal state) is rejected.
ORDER_TRANSITIONS: Dict[CanteenOrderStatus, List[CanteenOrderStatus]] = {
    CanteenOrderStatus.PENDING: [CanteenOrderStatus.ACCEPTED, CanteenOrderStatus.REJECTED, CanteenOrderStatus.CANCELLED],
    CanteenOrderStatus.ACCEPTED: [CanteenOrderStatus.PREPARING, CanteenOrderStatus.CANCELLED],
    CanteenOrderStatus.PREPARING: [CanteenOrderStatus.READY],
    # A no-prep order starts life already READY (see place_order), so a
    # student backing out before collecting it needs READY -> CANCELLED too,
    # not just the PENDING -> CANCELLED path a prepared order would use.
    CanteenOrderStatus.READY: [CanteenOrderStatus.COMPLETED, CanteenOrderStatus.CANCELLED],
    CanteenOrderStatus.COMPLETED: [],
    CanteenOrderStatus.REJECTED: [],
    CanteenOrderStatus.CANCELLED: [],
}

REFUND_STATUSES = {CanteenOrderStatus.REJECTED, CanteenOrderStatus.CANCELLED}

STATUS_TIMESTAMP_FIELD = {
    CanteenOrderStatus.ACCEPTED: "accepted_at",
    CanteenOrderStatus.PREPARING: "preparing_at",
    CanteenOrderStatus.READY: "ready_at",
    CanteenOrderStatus.COMPLETED: "completed_at",
    CanteenOrderStatus.REJECTED: "closed_at",
    CanteenOrderStatus.CANCELLED: "closed_at",
}


def canTransition(current: CanteenOrderStatus, target: CanteenOrderStatus) -> bool:
    return target in ORDER_TRANSITIONS.get(current, [])


class CanteenWalletService:
    """Service layer for the canteen prepaid wallet and order lifecycle."""

    def __init__(self, session=None):
        self.session = session

    def build_snapshot(self, *, account: CanteenWalletAccount, currency: str = "GHS") -> Dict[str, Any]:
        return {
            "balance": round(account.balance, 2),
            "currency": currency,
            "frozen": account.frozen,
            "daily_limit": account.daily_limit,
            "weekly_limit": account.weekly_limit,
            "blocked_categories": self._parse_categories(account.blocked_categories),
            "low_balance_threshold": account.low_balance_threshold,
            "low_balance": account.balance <= account.low_balance_threshold,
        }

    @staticmethod
    def _parse_categories(raw: Optional[str]) -> List[str]:
        if not raw:
            return []
        return [c.strip() for c in raw.split(",") if c.strip()]

    @staticmethod
    def _day_start(now: datetime) -> datetime:
        return now.replace(hour=0, minute=0, second=0, microsecond=0)

    @staticmethod
    def _week_start(now: datetime) -> datetime:
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return day_start - timedelta(days=day_start.weekday())

    async def get_or_create_account(
        self, *, session: AsyncSession, school_id: str, student_id: str, parent_id: Optional[str] = None
    ) -> CanteenWalletAccount:
        result = await session.execute(
            select(CanteenWalletAccount).where(
                CanteenWalletAccount.school_id == school_id,
                CanteenWalletAccount.student_id == student_id,
            ).order_by(CanteenWalletAccount.created_at, CanteenWalletAccount.id)
        )
        accounts = result.scalars().all()
        account = accounts[0] if accounts else None
        if len(accounts) > 1:
            logger.warning(
                "Duplicate canteen wallet accounts found for school=%s student=%s; "
                "using oldest account=%s",
                school_id,
                student_id,
                account.id,
            )
        if account is None:
            account = CanteenWalletAccount(school_id=school_id, student_id=student_id, parent_id=parent_id)
            session.add(account)
            await session.flush()
        if parent_id and not account.parent_id:
            account.parent_id = parent_id
        return account

    async def update_controls(
        self,
        *,
        session: AsyncSession,
        account: CanteenWalletAccount,
        frozen: Optional[bool] = None,
        daily_limit: Any = "__unset__",
        weekly_limit: Any = "__unset__",
        blocked_categories: Optional[List[str]] = None,
        low_balance_threshold: Optional[float] = None,
    ) -> Dict[str, Any]:
        if frozen is not None:
            account.frozen = frozen
        if daily_limit != "__unset__":
            account.daily_limit = daily_limit
        if weekly_limit != "__unset__":
            account.weekly_limit = weekly_limit
        if blocked_categories is not None:
            account.blocked_categories = ",".join(sorted(set(blocked_categories))) if blocked_categories else None
        if low_balance_threshold is not None:
            account.low_balance_threshold = low_balance_threshold

        if account.daily_limit is not None and account.weekly_limit is not None:
            if account.weekly_limit < account.daily_limit:
                return {"success": False, "error": "Weekly limit cannot be lower than the daily limit"}

        account.updated_at = datetime.utcnow()
        session.add(account)
        await session.flush()
        return {"success": True}

    async def list_ledger_entries(
        self, *, session: AsyncSession, school_id: str, student_id: str, limit: int = 10
    ) -> List[Dict[str, Any]]:
        account = await self.get_or_create_account(session=session, school_id=school_id, student_id=student_id)
        result = await session.execute(
            select(CanteenWalletLedgerEntry)
            .where(CanteenWalletLedgerEntry.wallet_id == account.id)
            .order_by(CanteenWalletLedgerEntry.created_at.desc())
            .limit(limit)
        )
        entries = result.scalars().all()
        return [
            {
                "id": entry.id,
                "event_type": entry.event_type,
                "amount": round(entry.amount, 2),
                "reference": entry.reference,
                "description": entry.description,
                "created_at": entry.created_at.isoformat() if entry.created_at else None,
            }
            for entry in entries
        ]

    async def apply_topup(
        self,
        *,
        session: AsyncSession,
        transaction: OnlineTransaction,
        amount: float,
        description: str = "Wallet top-up",
    ) -> Dict[str, Any]:
        if amount <= 0:
            return {"success": False, "error": "Amount must be positive"}

        account = await self.get_or_create_account(
            session=session,
            school_id=transaction.school_id,
            student_id=transaction.student_id,
            parent_id=transaction.parent_id,
        )

        account.balance += amount
        account.updated_at = datetime.utcnow()
        session.add(account)

        ledger_entry = CanteenWalletLedgerEntry(
            wallet_id=account.id,
            event_type="topup",
            amount=amount,
            reference=transaction.reference,
            description=description,
        )
        session.add(ledger_entry)
        await session.flush()

        return {"success": True, "wallet_id": account.id, "balance": round(account.balance, 2)}

    async def _spent_since(self, *, session: AsyncSession, school_id: str, student_id: str, since: datetime) -> float:
        result = await session.execute(
            select(func.coalesce(func.sum(CanteenOrder.total), 0.0)).where(
                CanteenOrder.school_id == school_id,
                CanteenOrder.student_id == student_id,
                CanteenOrder.status.not_in(list(REFUND_STATUSES)),
                CanteenOrder.created_at >= since,
            )
        )
        return float(result.scalar() or 0.0)

    async def place_order(
        self,
        *,
        session: AsyncSession,
        school_id: str,
        student_id: str,
        parent_id: Optional[str],
        cart: List[Dict[str, Any]],  # [{"item_id": str, "quantity": int}, ...]
        note: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not cart:
            return {"success": False, "code": "empty_cart", "error": "Your cart is empty"}

        # Resolve every line against the live catalog — never trust client-supplied prices.
        # Locked (FOR UPDATE) since stock_count is decremented below; without
        # this, two concurrent orders for the last unit of an item can both
        # pass the stock_count check off the same stale read and oversell.
        item_ids = [line["item_id"] for line in cart]
        result = await session.execute(
            select(CanteenItem).where(CanteenItem.id.in_(item_ids), CanteenItem.school_id == school_id).with_for_update()
        )
        items_by_id = {item.id: item for item in result.scalars().all()}

        resolved_lines: List[Tuple[CanteenItem, int]] = []
        for line in cart:
            quantity = int(line.get("quantity") or 0)
            item = items_by_id.get(line["item_id"])
            if quantity <= 0:
                return {"success": False, "code": "invalid_quantity", "error": "Quantity must be at least 1"}
            if not item or not item.is_active:
                return {"success": False, "code": "item_unavailable", "error": "One of the items is no longer available"}
            if item.stock_count is not None and item.stock_count < quantity:
                return {"success": False, "code": "out_of_stock", "error": f"{item.name} is out of stock"}
            resolved_lines.append((item, quantity))

        account = await self.get_or_create_account(
            session=session, school_id=school_id, student_id=student_id, parent_id=parent_id
        )
        # Re-select with a row lock now that the account definitely exists —
        # get_or_create_account itself stays unlocked since most of its
        # other callers only read the balance, and locking there would add
        # unnecessary contention for those paths. balance is decremented
        # below, so without this a concurrent order (or a counter charge via
        # canteen_wallet.py) can read the same stale balance and both pass
        # the sufficiency check, driving the wallet negative.
        # .populate_existing() is required here (unlike every OTHER lock in
        # this fix, which is the first select of its row): get_or_create_account
        # already loaded this row into the session's identity map, so a plain
        # re-select would hand back that same Python object with its stale
        # cached balance instead of the freshly-locked row's value.
        locked_result = await session.execute(
            select(CanteenWalletAccount).where(CanteenWalletAccount.id == account.id).with_for_update().execution_options(populate_existing=True)
        )
        account = locked_result.scalar_one()

        if account.frozen:
            return {"success": False, "code": "wallet_frozen", "error": "Wallet is frozen. Ask a parent to unfreeze it."}

        blocked = set(self._parse_categories(account.blocked_categories))
        for item, _ in resolved_lines:
            if item.item_type in blocked:
                return {
                    "success": False,
                    "code": "blocked_category",
                    "error": f"{item.name} ({item.item_type}) is blocked by a parent spending control",
                }

        subtotal = sum(item.price * quantity for item, quantity in resolved_lines)
        total = round(subtotal, 2)

        now = datetime.utcnow()
        if account.daily_limit is not None:
            spent_today = await self._spent_since(session=session, school_id=school_id, student_id=student_id, since=self._day_start(now))
            if spent_today + total > account.daily_limit:
                remaining = max(account.daily_limit - spent_today, 0)
                return {
                    "success": False,
                    "code": "daily_limit",
                    "error": f"Daily limit reached. GHS {remaining:.2f} left today.",
                }

        if account.weekly_limit is not None:
            spent_week = await self._spent_since(session=session, school_id=school_id, student_id=student_id, since=self._week_start(now))
            if spent_week + total > account.weekly_limit:
                remaining = max(account.weekly_limit - spent_week, 0)
                return {
                    "success": False,
                    "code": "weekly_limit",
                    "error": f"Weekly limit reached. GHS {remaining:.2f} left this week.",
                }

        if account.balance < total:
            return {
                "success": False,
                "code": "insufficient_funds",
                "error": f"Insufficient balance. GHS {total - account.balance:.2f} more needed.",
            }

        # All checks passed — commit the debit, stock decrement, and order atomically.
        account.balance -= total
        account.updated_at = now
        session.add(account)

        for item, quantity in resolved_lines:
            if item.stock_count is not None:
                item.stock_count -= quantity
                session.add(item)

        # Grab-and-go carts (no item needs cooking/prep) skip the kitchen
        # queue entirely and land straight in READY — a packaged juice or a
        # notebook doesn't need a canteen staff member to "accept" it.
        needs_prep = any(item.needs_prep for item, _ in resolved_lines)
        initial_status = CanteenOrderStatus.PENDING if needs_prep else CanteenOrderStatus.READY

        order = CanteenOrder(
            school_id=school_id,
            student_id=student_id,
            parent_id=account.parent_id,
            order_code=f"CT-{uuid.uuid4().hex[:6].upper()}",
            status=initial_status,
            subtotal=round(subtotal, 2),
            total=total,
            pickup_code="".join(random.choices(string.digits, k=6)),
            note=note,
            ready_at=now if not needs_prep else None,
        )
        session.add(order)
        await session.flush()

        for item, quantity in resolved_lines:
            session.add(
                CanteenOrderItem(
                    order_id=order.id,
                    item_id=item.id,
                    item_name=item.name,
                    unit_price=item.price,
                    quantity=quantity,
                    line_total=round(item.price * quantity, 2),
                )
            )

        session.add(
            CanteenWalletLedgerEntry(
                wallet_id=account.id,
                event_type="purchase",
                amount=-total,
                reference=order.order_code,
                description=f"Order {order.order_code}",
            )
        )
        await session.flush()

        return {"success": True, "order": order, "balance": round(account.balance, 2)}

    async def transition_status(
        self,
        *,
        session: AsyncSession,
        order: CanteenOrder,
        new_status: CanteenOrderStatus,
        note: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not canTransition(order.status, new_status):
            return {"success": False, "error": f"Cannot move an order from {order.status.value} to {new_status.value}"}

        now = datetime.utcnow()
        order.status = new_status
        timestamp_field = STATUS_TIMESTAMP_FIELD.get(new_status)
        if timestamp_field:
            setattr(order, timestamp_field, now)
        if new_status == CanteenOrderStatus.REJECTED and note:
            order.rejection_reason = note
        order.updated_at = now
        session.add(order)

        if new_status in REFUND_STATUSES:
            account = await self.get_or_create_account(
                session=session, school_id=order.school_id, student_id=order.student_id
            )
            account.balance += order.total
            account.updated_at = now
            session.add(account)

            lines_result = await session.execute(
                select(CanteenOrderItem).where(CanteenOrderItem.order_id == order.id)
            )
            for line in lines_result.scalars().all():
                if not line.item_id:
                    continue
                item_result = await session.execute(select(CanteenItem).where(CanteenItem.id == line.item_id))
                item = item_result.scalar_one_or_none()
                if item and item.stock_count is not None:
                    item.stock_count += line.quantity
                    session.add(item)

            session.add(
                CanteenWalletLedgerEntry(
                    wallet_id=account.id,
                    event_type="refund",
                    amount=order.total,
                    reference=order.order_code,
                    description=f"Refund for {new_status.value} order {order.order_code}",
                )
            )

        await session.flush()
        return {"success": True, "order": order}

    async def verify_pickup(
        self, *, session: AsyncSession, order: CanteenOrder, pickup_code: str
    ) -> Dict[str, Any]:
        if order.status != CanteenOrderStatus.READY:
            return {"success": False, "error": f"Order is {order.status.value}, not ready for pickup"}
        if order.pickup_code != pickup_code.strip():
            return {"success": False, "error": "Pickup code does not match"}
        return await self.transition_status(session=session, order=order, new_status=CanteenOrderStatus.COMPLETED)


def build_canteen_wallet_snapshot(*, account: CanteenWalletAccount, currency: str = "GHS") -> Dict[str, Any]:
    return CanteenWalletService().build_snapshot(account=account, currency=currency)
