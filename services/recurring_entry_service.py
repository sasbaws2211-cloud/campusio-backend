"""Recurring Journal Entry Service

Generates real DRAFT journal entries from RecurringJournalEntryTemplate rows
whose next_run_date has arrived, and auto-reverses posted accrual entries
whose auto_reverse_date has arrived.

Invoked periodically by services/scheduler.py's real APScheduler-based job
(this codebase does have a scheduler — every other finance/HR/ops job
already runs through it), so this stays a plain callable service function
rather than owning its own background-task machinery.
"""
import logging
from typing import Optional, List
from datetime import datetime
from dateutil.relativedelta import relativedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select, and_

from models.finance.recurring_entry import (
    RecurringJournalEntryTemplate,
    RecurringEntryLineTemplate,
    RecurringJournalEntryTemplateCreate,
    RecurrenceFrequency,
)
from models.finance.journal_entries import (
    JournalEntryCreate,
    JournalLineItemCreate,
    JournalEntry,
    PostingStatus,
    ReferenceType,
)
from services.journal_entry_service import JournalEntryService, JournalEntryError
from services.gl_audit_log_service import GLAuditLogService
from models.finance.gl_audit_log import AuditActionType, AuditEntityType

logger = logging.getLogger(__name__)

_FREQUENCY_MONTHS = {
    RecurrenceFrequency.MONTHLY: 1,
    RecurrenceFrequency.QUARTERLY: 3,
    RecurrenceFrequency.SEMI_ANNUAL: 6,
    RecurrenceFrequency.ANNUAL: 12,
}


class RecurringEntryError(Exception):
    """Base exception for recurring entry service errors"""
    pass


def _advance(date: datetime, frequency: RecurrenceFrequency) -> datetime:
    return date + relativedelta(months=_FREQUENCY_MONTHS[frequency])


class RecurringEntryService:
    """Service for managing recurring journal entry templates"""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.journal_service = JournalEntryService(session)
        self.audit_service = GLAuditLogService(session)

    async def create_template(
        self,
        school_id: str,
        template_data: RecurringJournalEntryTemplateCreate,
        created_by: str,
    ) -> RecurringJournalEntryTemplate:
        """Create a recurring entry template

        Validates the template's line items balance (debits = credits),
        exactly like a normal journal entry, since generate_due_entries()
        will feed them straight into JournalEntryService.create_entry().
        """
        total_debit = sum((li.debit_amount for li in template_data.line_items), start=0)
        total_credit = sum((li.credit_amount for li in template_data.line_items), start=0)
        if abs(total_debit - total_credit) > 0.01:
            raise RecurringEntryError(
                f"Template line items don't balance: debits {total_debit} != credits {total_credit}"
            )
        if len(template_data.line_items) < 2:
            raise RecurringEntryError("Template must have at least 2 line items")

        template = RecurringJournalEntryTemplate(
            school_id=school_id,
            description=template_data.description,
            frequency=template_data.frequency,
            next_run_date=template_data.next_run_date,
            end_date=template_data.end_date,
            is_accrual=template_data.is_accrual,
            auto_reverse_after_days=template_data.auto_reverse_after_days,
            created_by=created_by,
        )
        self.session.add(template)
        await self.session.flush()

        for idx, line in enumerate(template_data.line_items):
            self.session.add(
                RecurringEntryLineTemplate(
                    template_id=template.id,
                    school_id=school_id,
                    gl_account_id=line.gl_account_id,
                    debit_amount=line.debit_amount,
                    credit_amount=line.credit_amount,
                    description=line.description,
                    line_number=line.line_number or idx + 1,
                )
            )

        await self.session.commit()
        await self.session.refresh(template)

        logger.info(
            f"Created recurring entry template '{template.description}' for school {school_id} "
            f"({template.frequency.value}, next run {template.next_run_date.date()})"
        )
        return template

    async def list_templates(self, school_id: str, active_only: bool = True) -> List[RecurringJournalEntryTemplate]:
        query = select(RecurringJournalEntryTemplate).where(
            RecurringJournalEntryTemplate.school_id == school_id
        )
        if active_only:
            query = query.where(RecurringJournalEntryTemplate.is_active == True)
        result = await self.session.execute(query.order_by(RecurringJournalEntryTemplate.next_run_date))
        return result.scalars().all()

    async def get_template_lines(self, template_id: str) -> List[RecurringEntryLineTemplate]:
        result = await self.session.execute(
            select(RecurringEntryLineTemplate)
            .where(RecurringEntryLineTemplate.template_id == template_id)
            .order_by(RecurringEntryLineTemplate.line_number)
        )
        return result.scalars().all()

    async def deactivate_template(self, school_id: str, template_id: str) -> RecurringJournalEntryTemplate:
        result = await self.session.execute(
            select(RecurringJournalEntryTemplate).where(
                and_(
                    RecurringJournalEntryTemplate.id == template_id,
                    RecurringJournalEntryTemplate.school_id == school_id,
                )
            )
        )
        template = result.scalar_one_or_none()
        if not template:
            raise RecurringEntryError(f"Template {template_id} not found")

        template.is_active = False
        template.updated_at = datetime.utcnow()
        self.session.add(template)
        await self.session.commit()
        await self.session.refresh(template)
        return template

    async def generate_due_entries(
        self,
        school_id: str,
        created_by: str,
        as_of_date: Optional[datetime] = None,
    ) -> List[dict]:
        """Create a DRAFT journal entry for every active template due to run

        Safe to call repeatedly: a template's next_run_date only advances
        after its entry is successfully created, so a template that fails
        (e.g. a line item's GL account was deactivated) stays due and will
        be retried on the next call rather than silently skipped forever.

        Args:
            school_id: School identifier
            created_by: User/system identity to attribute generated entries to
            as_of_date: Generate entries due on or before this date (default: now)

        Returns:
            List of {"template_id", "entry_id", "status"} for each template processed
        """
        as_of = as_of_date or datetime.utcnow()

        result = await self.session.execute(
            select(RecurringJournalEntryTemplate).where(
                and_(
                    RecurringJournalEntryTemplate.school_id == school_id,
                    RecurringJournalEntryTemplate.is_active == True,
                    RecurringJournalEntryTemplate.next_run_date <= as_of,
                )
            )
        )
        due_templates = result.scalars().all()

        results = []
        for template in due_templates:
            if template.end_date and template.next_run_date > template.end_date:
                template.is_active = False
                self.session.add(template)
                await self.session.commit()
                continue

            lines = await self.get_template_lines(template.id)
            auto_reverse_date = None
            if template.is_accrual and template.auto_reverse_after_days:
                auto_reverse_date = template.next_run_date + relativedelta(
                    days=template.auto_reverse_after_days
                )

            entry_data = JournalEntryCreate(
                entry_date=template.next_run_date,
                reference_type=ReferenceType.ADJUSTMENT if template.is_accrual else ReferenceType.MANUAL,
                reference_id=template.id,
                description=f"{template.description} (recurring — {template.frequency.value})",
                line_items=[
                    JournalLineItemCreate(
                        gl_account_id=li.gl_account_id,
                        debit_amount=li.debit_amount,
                        credit_amount=li.credit_amount,
                        description=li.description,
                        line_number=li.line_number,
                    )
                    for li in lines
                ],
                notes=f"Auto-generated from recurring template {template.id}",
                is_adjusting_entry=template.is_accrual,
                auto_reverse_date=auto_reverse_date,
            )

            try:
                entry = await self.journal_service.create_entry(
                    school_id=school_id, entry_data=entry_data, created_by=created_by,
                )
                template.next_run_date = _advance(template.next_run_date, template.frequency)
                if template.end_date and template.next_run_date > template.end_date:
                    template.is_active = False
                template.updated_at = datetime.utcnow()
                self.session.add(template)
                await self.session.commit()

                try:
                    await self.audit_service.log_action(
                        school_id=school_id, entity_type=AuditEntityType.JOURNAL_ENTRY, entity_id=entry.id,
                        action=AuditActionType.ENTRY_CREATED, user_id=created_by, user_name="System (recurring entry template)",
                        user_role="system",
                        new_values={"template_id": template.id, "description": template.description},
                    )
                except Exception as e:
                    logger.warning(f"Failed to write GL audit log for recurring entry {entry.id}: {e}")

                results.append({"template_id": template.id, "entry_id": entry.id, "status": "created"})
                logger.info(f"Generated entry {entry.id} from recurring template {template.id}")
            except (JournalEntryError, Exception) as e:
                logger.error(f"Failed to generate entry from template {template.id}: {str(e)}")
                results.append({"template_id": template.id, "entry_id": None, "status": f"error: {str(e)}"})

        return results

    async def reverse_due_accruals(
        self,
        school_id: str,
        reversed_by: str,
        as_of_date: Optional[datetime] = None,
    ) -> List[dict]:
        """Reverse every POSTED accrual entry whose auto_reverse_date has arrived

        Args:
            school_id: School identifier
            reversed_by: User/system identity performing the reversal
            as_of_date: Reverse accruals due on or before this date (default: now)

        Returns:
            List of {"entry_id", "reversal_entry_id", "status"} for each accrual processed
        """
        as_of = as_of_date or datetime.utcnow()

        result = await self.session.execute(
            select(JournalEntry).where(
                and_(
                    JournalEntry.school_id == school_id,
                    JournalEntry.posting_status == PostingStatus.POSTED,
                    JournalEntry.is_adjusting_entry == True,
                    JournalEntry.auto_reverse_date.isnot(None),
                    JournalEntry.auto_reverse_date <= as_of,
                )
            )
        )
        due_entries = result.scalars().all()

        results = []
        for entry in due_entries:
            try:
                original, reversal = await self.journal_service.reverse_entry(
                    school_id=school_id,
                    entry_id=entry.id,
                    reversed_by=reversed_by,
                    reversal_reason="Automatic accrual reversal",
                )
                try:
                    await self.audit_service.log_action(
                        school_id=school_id, entity_type=AuditEntityType.JOURNAL_ENTRY, entity_id=entry.id,
                        action=AuditActionType.ENTRY_REVERSED, user_id=reversed_by, user_name="System (auto-reverse accrual)",
                        user_role="system",
                        new_values={"reversal_entry_id": reversal.id, "reason": "Automatic accrual reversal"},
                    )
                except Exception as e:
                    logger.warning(f"Failed to write GL audit log for auto-reversal of {entry.id}: {e}")

                results.append({"entry_id": entry.id, "reversal_entry_id": reversal.id, "status": "reversed"})
                logger.info(f"Auto-reversed accrual entry {entry.id} with {reversal.id}")
            except Exception as e:
                logger.error(f"Failed to auto-reverse accrual {entry.id}: {str(e)}")
                results.append({"entry_id": entry.id, "reversal_entry_id": None, "status": f"error: {str(e)}"})

        return results
