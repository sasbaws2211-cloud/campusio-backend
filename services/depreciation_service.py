"""Depreciation Service - straight-line depreciation schedules

generate_due_depreciation() posts, for each active schedule due to run:
  Dr. Depreciation Expense
  Cr. Accumulated Depreciation
for the schedule's monthly amount, then advances it one month and stops it
once useful_life_months periods have run (crediting any final rounding
remainder in the last period so the total posted exactly equals
asset_cost - salvage_value).

Invoked periodically by services/scheduler.py's real APScheduler-based job
(this codebase does have a scheduler — every other finance/HR/ops job
already runs through it), so this stays a plain callable service function
rather than owning its own background-task machinery. Still callable
on-demand too (e.g. a manual catch-up run).
"""
import logging
from typing import Optional, List
from decimal import Decimal
from datetime import datetime
from dateutil.relativedelta import relativedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select, and_

from models.finance.depreciation import DepreciationSchedule, DepreciationScheduleCreate
from models.finance.journal_entries import JournalEntryCreate, JournalLineItemCreate, ReferenceType
from models.inventory import Asset, AssetStatus, AssetCondition
from services.coa_service import CoaService
from services.journal_entry_service import JournalEntryService, JournalEntryError
from services.gl_audit_log_service import GLAuditLogService
from models.finance.gl_audit_log import AuditActionType, AuditEntityType

logger = logging.getLogger(__name__)


class DepreciationError(Exception):
    """Base exception for depreciation service errors"""
    pass


class DepreciationService:
    """Service for managing fixed-asset depreciation schedules"""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.coa_service = CoaService(session)
        self.journal_service = JournalEntryService(session)
        self.audit_service = GLAuditLogService(session)

    async def create_schedule(
        self,
        school_id: str,
        schedule_data: DepreciationScheduleCreate,
        created_by: str,
    ) -> DepreciationSchedule:
        """Create a depreciation schedule, computing the monthly amount

        Raises:
            DepreciationError: If any referenced GL account doesn't exist,
                or salvage_value >= asset_cost
        """
        if schedule_data.salvage_value >= schedule_data.asset_cost:
            raise DepreciationError("salvage_value must be less than asset_cost")

        for account_id in (
            schedule_data.asset_account_id,
            schedule_data.accumulated_depreciation_account_id,
            schedule_data.depreciation_expense_account_id,
        ):
            account = await self.coa_service.get_account_by_id(school_id, account_id)
            if not account:
                raise DepreciationError(f"GL account {account_id} not found")

        depreciable_base = schedule_data.asset_cost - schedule_data.salvage_value
        monthly_amount = (depreciable_base / schedule_data.useful_life_months).quantize(Decimal("0.01"))

        schedule = DepreciationSchedule(
            school_id=school_id,
            asset_description=schedule_data.asset_description,
            asset_id=schedule_data.asset_id,
            asset_account_id=schedule_data.asset_account_id,
            accumulated_depreciation_account_id=schedule_data.accumulated_depreciation_account_id,
            depreciation_expense_account_id=schedule_data.depreciation_expense_account_id,
            method=schedule_data.method,
            asset_cost=schedule_data.asset_cost,
            salvage_value=schedule_data.salvage_value,
            useful_life_months=schedule_data.useful_life_months,
            monthly_depreciation_amount=monthly_amount,
            start_date=schedule_data.start_date,
            next_run_date=schedule_data.start_date,
            created_by=created_by,
        )
        self.session.add(schedule)
        await self.session.commit()
        await self.session.refresh(schedule)

        logger.info(
            f"Created depreciation schedule for '{schedule.asset_description}': "
            f"{monthly_amount}/month over {schedule.useful_life_months} months"
        )
        return schedule

    async def list_schedules(self, school_id: str, active_only: bool = True) -> List[DepreciationSchedule]:
        query = select(DepreciationSchedule).where(DepreciationSchedule.school_id == school_id)
        if active_only:
            query = query.where(DepreciationSchedule.is_active == True)
        result = await self.session.execute(query.order_by(DepreciationSchedule.next_run_date))
        return result.scalars().all()

    async def deactivate_schedule(self, school_id: str, schedule_id: str) -> DepreciationSchedule:
        result = await self.session.execute(
            select(DepreciationSchedule).where(
                and_(DepreciationSchedule.id == schedule_id, DepreciationSchedule.school_id == school_id)
            )
        )
        schedule = result.scalar_one_or_none()
        if not schedule:
            raise DepreciationError(f"Schedule {schedule_id} not found")

        schedule.is_active = False
        schedule.updated_at = datetime.utcnow()
        self.session.add(schedule)
        await self.session.commit()
        await self.session.refresh(schedule)
        return schedule

    async def generate_due_depreciation(
        self,
        school_id: str,
        created_by: str,
        as_of_date: Optional[datetime] = None,
    ) -> List[dict]:
        """Post the monthly depreciation entry for every active schedule due to run

        Args:
            school_id: School identifier
            created_by: User/system identity to attribute generated entries to
            as_of_date: Generate/post depreciation due on or before this date (default: now)

        Returns:
            List of {"schedule_id", "entry_id", "status"} for each schedule processed
        """
        as_of = as_of_date or datetime.utcnow()

        result = await self.session.execute(
            select(DepreciationSchedule).where(
                and_(
                    DepreciationSchedule.school_id == school_id,
                    DepreciationSchedule.is_active == True,
                    DepreciationSchedule.next_run_date <= as_of,
                )
            )
        )
        due_schedules = result.scalars().all()

        results = []
        for schedule in due_schedules:
            if schedule.periods_run >= schedule.useful_life_months:
                schedule.is_active = False
                self.session.add(schedule)
                await self.session.commit()
                continue

            is_final_period = schedule.periods_run == schedule.useful_life_months - 1
            if is_final_period:
                # Credit whatever remains of the depreciable base, so
                # rounding across all periods never over/under-depreciates
                # the asset relative to (asset_cost - salvage_value).
                already_posted = schedule.monthly_depreciation_amount * schedule.periods_run
                amount = (schedule.asset_cost - schedule.salvage_value) - already_posted
            else:
                amount = schedule.monthly_depreciation_amount

            entry_data = JournalEntryCreate(
                entry_date=schedule.next_run_date,
                reference_type=ReferenceType.DEPRECIATION,
                reference_id=schedule.id,
                description=f"Depreciation: {schedule.asset_description} (period {schedule.periods_run + 1}/{schedule.useful_life_months})",
                line_items=[
                    JournalLineItemCreate(
                        gl_account_id=schedule.depreciation_expense_account_id,
                        debit_amount=amount,
                        credit_amount=Decimal("0"),
                        description=f"Depreciation expense: {schedule.asset_description}",
                    ),
                    JournalLineItemCreate(
                        gl_account_id=schedule.accumulated_depreciation_account_id,
                        debit_amount=Decimal("0"),
                        credit_amount=amount,
                        description=f"Accumulated depreciation: {schedule.asset_description}",
                    ),
                ],
                notes=f"Auto-generated from depreciation schedule {schedule.id}",
            )

            try:
                entry = await self.journal_service.create_entry(
                    school_id=school_id, entry_data=entry_data, created_by=created_by,
                )
                posted = await self.journal_service.post_entry(
                    school_id=school_id, entry_id=entry.id, posted_by=created_by,
                    approval_notes="Auto-posted depreciation entry",
                )

                schedule.periods_run += 1
                schedule.next_run_date = schedule.next_run_date + relativedelta(months=1)
                if schedule.periods_run >= schedule.useful_life_months:
                    schedule.is_active = False
                schedule.updated_at = datetime.utcnow()
                self.session.add(schedule)
                await self.session.commit()

                # Previously depreciation postings never wrote to the GL
                # audit log at all — a real gap against this log's own
                # stated purpose ("every change to GL accounts, journal
                # entries... is logged"), leaving an external auditor
                # unable to trace who/what triggered a month's
                # depreciation from the audit trail alone.
                try:
                    await self.audit_service.log_action(
                        school_id=school_id, entity_type=AuditEntityType.JOURNAL_ENTRY, entity_id=posted.id,
                        action=AuditActionType.ENTRY_POSTED, user_id=created_by, user_name="System (depreciation schedule)",
                        user_role="system",
                        new_values={"schedule_id": schedule.id, "amount": float(amount), "period": schedule.periods_run},
                    )
                except Exception as e:
                    logger.warning(f"Failed to write GL audit log for depreciation entry {posted.id}: {e}")

                results.append({"schedule_id": schedule.id, "entry_id": posted.id, "status": "posted"})
                logger.info(f"Posted depreciation entry {posted.id} for schedule {schedule.id}")
            except (JournalEntryError, Exception) as e:
                logger.error(f"Failed to post depreciation for schedule {schedule.id}: {str(e)}")
                results.append({"schedule_id": schedule.id, "entry_id": None, "status": f"error: {str(e)}"})

        return results

    async def dispose_asset(
        self,
        school_id: str,
        schedule_id: str,
        disposal_date: datetime,
        proceeds: Decimal,
        gain_loss_account_id: str,
        cash_account_id: Optional[str],
        created_by: str,
    ) -> dict:
        """Write off a depreciation schedule's asset: remove its cost and
        accumulated depreciation from the books, recognize any gain/loss
        on disposal (proceeds vs. net book value), and stop further
        depreciation. If the schedule is linked to a fixed-asset register
        row (asset_id), that Asset is flipped to DISPOSED too.

        Journal entry (double-entry validated by JournalEntryService):
          Dr. Accumulated Depreciation      (removes the contra-asset balance)
          Dr. Cash/Bank                     (if proceeds > 0)
          Cr. Fixed Asset (at cost)
          Dr./Cr. Gain/Loss on Disposal      (plug to balance)
        """
        result = await self.session.execute(
            select(DepreciationSchedule).where(
                and_(DepreciationSchedule.id == schedule_id, DepreciationSchedule.school_id == school_id)
            )
        )
        schedule = result.scalar_one_or_none()
        if not schedule:
            raise DepreciationError(f"Schedule {schedule_id} not found")
        if not schedule.is_active:
            raise DepreciationError("Schedule is already inactive/disposed")
        if proceeds > 0 and not cash_account_id:
            raise DepreciationError("cash_account_id is required when proceeds > 0")

        for account_id in filter(None, (gain_loss_account_id, cash_account_id)):
            account = await self.coa_service.get_account_by_id(school_id, account_id)
            if not account:
                raise DepreciationError(f"GL account {account_id} not found")

        depreciable_base = schedule.asset_cost - schedule.salvage_value
        accumulated_depreciation = min(schedule.monthly_depreciation_amount * schedule.periods_run, depreciable_base)
        net_book_value = schedule.asset_cost - accumulated_depreciation
        gain_or_loss = proceeds - net_book_value  # positive = gain, negative = loss

        line_items = [
            JournalLineItemCreate(
                gl_account_id=schedule.accumulated_depreciation_account_id,
                debit_amount=accumulated_depreciation,
                credit_amount=Decimal("0"),
                description=f"Remove accumulated depreciation: {schedule.asset_description}",
            ),
            JournalLineItemCreate(
                gl_account_id=schedule.asset_account_id,
                debit_amount=Decimal("0"),
                credit_amount=schedule.asset_cost,
                description=f"Remove asset at cost: {schedule.asset_description}",
            ),
        ]
        if proceeds > 0:
            line_items.append(JournalLineItemCreate(
                gl_account_id=cash_account_id,
                debit_amount=proceeds,
                credit_amount=Decimal("0"),
                description=f"Disposal proceeds: {schedule.asset_description}",
            ))
        if gain_or_loss > 0:
            line_items.append(JournalLineItemCreate(
                gl_account_id=gain_loss_account_id,
                debit_amount=Decimal("0"),
                credit_amount=gain_or_loss,
                description=f"Gain on disposal: {schedule.asset_description}",
            ))
        elif gain_or_loss < 0:
            line_items.append(JournalLineItemCreate(
                gl_account_id=gain_loss_account_id,
                debit_amount=-gain_or_loss,
                credit_amount=Decimal("0"),
                description=f"Loss on disposal: {schedule.asset_description}",
            ))

        entry_data = JournalEntryCreate(
            entry_date=disposal_date,
            reference_type=ReferenceType.ASSET_DISPOSAL,
            reference_id=schedule.id,
            description=f"Asset disposal: {schedule.asset_description}",
            line_items=line_items,
            notes=f"Disposal of asset tracked by depreciation schedule {schedule.id}",
        )

        entry = await self.journal_service.create_entry(school_id=school_id, entry_data=entry_data, created_by=created_by)
        posted = await self.journal_service.post_entry(
            school_id=school_id, entry_id=entry.id, posted_by=created_by, approval_notes="Asset disposal",
        )

        schedule.is_active = False
        schedule.disposed_at = disposal_date
        schedule.disposal_journal_entry_id = posted.id
        schedule.updated_at = datetime.utcnow()
        self.session.add(schedule)

        if schedule.asset_id:
            asset_result = await self.session.execute(
                select(Asset).where(and_(Asset.id == schedule.asset_id, Asset.school_id == school_id))
            )
            asset = asset_result.scalar_one_or_none()
            if asset:
                asset.status = AssetStatus.DISPOSED
                asset.condition = AssetCondition.DISPOSED
                asset.updated_at = datetime.utcnow()
                self.session.add(asset)

        await self.session.commit()

        try:
            await self.audit_service.log_action(
                school_id=school_id, entity_type=AuditEntityType.JOURNAL_ENTRY, entity_id=posted.id,
                action=AuditActionType.ENTRY_POSTED, user_id=created_by, user_name="Asset disposal", user_role="finance",
                new_values={"schedule_id": schedule.id, "net_book_value": float(net_book_value), "gain_or_loss": float(gain_or_loss)},
            )
        except Exception as e:
            logger.warning(f"Failed to write GL audit log for asset disposal entry {posted.id}: {e}")

        logger.info(
            f"Disposed asset for schedule {schedule.id}: NBV {net_book_value}, "
            f"proceeds {proceeds}, gain/loss {gain_or_loss}"
        )
        return {
            "schedule_id": schedule.id,
            "journal_entry_id": posted.id,
            "net_book_value": net_book_value,
            "accumulated_depreciation": accumulated_depreciation,
            "gain_or_loss": gain_or_loss,
        }
