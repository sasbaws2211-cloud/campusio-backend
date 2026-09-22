"""QuickBooks Desktop IIF export — one-way, file-based sync of posted
journal entries into QuickBooks' IIF (Intuit Interchange Format) import
format. The school's accountant imports the generated file via
File → Utilities → Import → IIF Files in QuickBooks Desktop.

Sign convention: debit amounts are written POSITIVE and credit amounts
NEGATIVE on the AMOUNT column, for a "GENERAL JOURNAL" transaction type —
this is the convention most commonly documented for IIF general journal
entries. IIF's own structural requirement (that a transaction's TRNS +
SPL amounts sum to exactly zero) is satisfied regardless of which absolute
polarity is "correct" on QuickBooks' side, since our debit/credit totals
are always equal by construction — so a wrong guess here would produce a
file QuickBooks accepts and imports, just with debits and credits mirrored.
This has not been validated against a real QuickBooks Desktop import (no
QuickBooks Desktop instance available to test against) — run one small
export through a real "Import IIF" pass and check the resulting journal
entry's debit/credit sides before relying on this for production data.
"""
import csv
import io
import logging
from datetime import datetime
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from models.finance.journal_entries import JournalEntry, JournalLineItem, PostingStatus
from models.finance.chart_of_accounts import GLAccount
from models.finance.accounting_integration import (
    ExternalAccountMapping, AccountingProvider, JournalEntrySyncLog,
)

logger = logging.getLogger(__name__)


async def _resolve_account_names(session: AsyncSession, school_id: str, provider: AccountingProvider) -> dict:
    accounts_result = await session.execute(select(GLAccount).where(GLAccount.school_id == school_id))
    names = {a.id: a.account_name for a in accounts_result.scalars().all()}

    mappings_result = await session.execute(
        select(ExternalAccountMapping).where(
            ExternalAccountMapping.school_id == school_id,
            ExternalAccountMapping.provider == provider,
        )
    )
    for m in mappings_result.scalars().all():
        names[m.gl_account_id] = m.external_name

    return names


async def entries_to_export(
    session: AsyncSession, school_id: str, start_date: datetime, end_date: datetime,
    provider: AccountingProvider, only_new: bool,
):
    """Same POSTED + REVERSED inclusion logic as the Tally exporter — see
    that module's docstring for why reversed entries still appear."""
    start_date = start_date.replace(tzinfo=None) if start_date.tzinfo else start_date
    end_date = end_date.replace(tzinfo=None) if end_date.tzinfo else end_date

    query = select(JournalEntry).where(
        JournalEntry.school_id == school_id,
        JournalEntry.posting_status.in_([PostingStatus.POSTED, PostingStatus.REVERSED]),
        JournalEntry.entry_date >= start_date,
        JournalEntry.entry_date <= end_date,
    ).order_by(JournalEntry.entry_date)
    result = await session.execute(query)
    entries = list(result.scalars().all())

    if only_new and entries:
        synced_result = await session.execute(
            select(JournalEntrySyncLog.journal_entry_id).where(
                JournalEntrySyncLog.school_id == school_id,
                JournalEntrySyncLog.provider == provider,
                JournalEntrySyncLog.journal_entry_id.in_([e.id for e in entries]),
            )
        )
        already_synced = {row[0] for row in synced_result.all()}
        entries = [e for e in entries if e.id not in already_synced]

    return entries


async def generate_qb_iif(
    session: AsyncSession, school_id: str, start_date: datetime, end_date: datetime,
    only_new: bool = True,
) -> Optional[str]:
    """Returns the IIF text, or None if there's nothing to export."""
    entries = await entries_to_export(session, school_id, start_date, end_date, AccountingProvider.QUICKBOOKS_DESKTOP, only_new)
    if not entries:
        return None

    account_names = await _resolve_account_names(session, school_id, AccountingProvider.QUICKBOOKS_DESKTOP)

    entry_ids = [e.id for e in entries]
    line_items_result = await session.execute(
        select(JournalLineItem).where(JournalLineItem.journal_entry_id.in_(entry_ids)).order_by(JournalLineItem.line_number)
    )
    line_items_by_entry: dict = {}
    for li in line_items_result.scalars().all():
        line_items_by_entry.setdefault(li.journal_entry_id, []).append(li)

    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter="\t", lineterminator="\n", quoting=csv.QUOTE_NONE, escapechar="\\")

    writer.writerow(["!TRNS", "TRNSID", "TRNSTYPE", "DATE", "ACCNT", "NAME", "CLASS", "AMOUNT", "DOCNUM", "MEMO"])
    writer.writerow(["!SPL", "SPLID", "TRNSTYPE", "DATE", "ACCNT", "NAME", "CLASS", "AMOUNT", "DOCNUM", "MEMO"])
    writer.writerow(["!ENDTRNS"])

    for entry in entries:
        lines = line_items_by_entry.get(entry.id, [])
        if len(lines) < 2:
            continue
        date_str = entry.entry_date.strftime("%m/%d/%Y")
        memo = entry.description or ""
        docnum = entry.reference_id or entry.id[:8]

        first, rest = lines[0], lines[1:]
        writer.writerow([
            "TRNS", entry.id, "GENERAL JOURNAL", date_str,
            account_names.get(first.gl_account_id, first.gl_account_id), "", "",
            f"{_signed_amount(first):.2f}", docnum, memo,
        ])
        for li in rest:
            writer.writerow([
                "SPL", f"{entry.id}-{li.line_number}", "GENERAL JOURNAL", date_str,
                account_names.get(li.gl_account_id, li.gl_account_id), "", "",
                f"{_signed_amount(li):.2f}", docnum, memo,
            ])
        writer.writerow(["ENDTRNS"])

    return buffer.getvalue()


def _signed_amount(line_item: JournalLineItem):
    """Debit positive, credit negative — see module docstring for the
    caveat on this polarity."""
    if line_item.debit_amount and line_item.debit_amount > 0:
        return line_item.debit_amount
    return -line_item.credit_amount


async def mark_exported(session: AsyncSession, school_id: str, entries, provider: AccountingProvider, exported_by: str) -> None:
    """Idempotent: an only_new=False re-export deliberately re-includes
    entries that already have a sync-log row (that's the whole point of
    "re-export"), so this must skip re-inserting for those rather than
    hitting the (journal_entry_id, provider) unique constraint."""
    if not entries:
        return
    existing_result = await session.execute(
        select(JournalEntrySyncLog.journal_entry_id).where(
            JournalEntrySyncLog.school_id == school_id,
            JournalEntrySyncLog.provider == provider,
            JournalEntrySyncLog.journal_entry_id.in_([e.id for e in entries]),
        )
    )
    already_logged = {row[0] for row in existing_result.all()}

    for entry in entries:
        if entry.id in already_logged:
            continue
        session.add(JournalEntrySyncLog(
            school_id=school_id, journal_entry_id=entry.id, provider=provider, exported_by=exported_by,
        ))
    await session.commit()
