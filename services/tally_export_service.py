"""Tally XML voucher export — one-way, file-based sync of posted journal
entries into Tally's standard XML voucher-import schema. The school's
accountant imports the generated file via Tally's Gateway of Tally →
Import Data → Vouchers.

Sign convention (ALLLEDGERENTRIES.LIST): Tally represents a debit as
ISDEEMEDPOSITIVE=Yes with a NEGATIVE amount, and a credit as
ISDEEMEDPOSITIVE=No with a POSITIVE amount. This is the standard,
widely-documented convention for Tally's XML voucher import — but it has
not been validated against a real Tally import in this environment (no
Tally instance available to test against). Run one small export through a
real Tally "Import Data" pass before relying on this for production data,
and confirm debits/credits land on the correct side.
"""
import logging
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from models.finance.journal_entries import JournalEntry, JournalLineItem, PostingStatus
from models.finance.chart_of_accounts import GLAccount
from models.finance.accounting_integration import (
    ExternalAccountMapping, AccountingProvider, JournalEntrySyncLog,
)
from models.school import School

logger = logging.getLogger(__name__)


async def _resolve_account_names(session: AsyncSession, school_id: str, provider: AccountingProvider) -> dict:
    """gl_account_id -> external name, falling back to our own account_name
    when no override mapping exists."""
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
    """POSTED + REVERSED entries in range — a reversed entry *was* posted;
    its reversal is a separate, also-posted contra-entry, so Tally should
    see both vouchers (that's how double-entry systems represent "it
    happened, then was undone"), same reasoning as the trial balance."""
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


async def generate_tally_xml(
    session: AsyncSession, school_id: str, start_date: datetime, end_date: datetime,
    only_new: bool = True,
) -> Optional[str]:
    """Returns the XML string, or None if there's nothing to export."""
    entries = await entries_to_export(session, school_id, start_date, end_date, AccountingProvider.TALLY, only_new)
    if not entries:
        return None

    school_result = await session.execute(select(School).where(School.id == school_id))
    school = school_result.scalar_one_or_none()

    account_names = await _resolve_account_names(session, school_id, AccountingProvider.TALLY)

    entry_ids = [e.id for e in entries]
    line_items_result = await session.execute(
        select(JournalLineItem).where(JournalLineItem.journal_entry_id.in_(entry_ids)).order_by(JournalLineItem.line_number)
    )
    line_items_by_entry: dict = {}
    for li in line_items_result.scalars().all():
        line_items_by_entry.setdefault(li.journal_entry_id, []).append(li)

    envelope = ET.Element("ENVELOPE")
    header = ET.SubElement(envelope, "HEADER")
    ET.SubElement(header, "TALLYREQUEST").text = "Import Data"

    body = ET.SubElement(envelope, "BODY")
    import_data = ET.SubElement(body, "IMPORTDATA")
    request_desc = ET.SubElement(import_data, "REQUESTDESC")
    ET.SubElement(request_desc, "REPORTNAME").text = "Vouchers"
    static_vars = ET.SubElement(request_desc, "STATICVARIABLES")
    ET.SubElement(static_vars, "SVCURRENTCOMPANY").text = school.name if school else ""

    request_data = ET.SubElement(import_data, "REQUESTDATA")

    # One TALLYMESSAGE > VOUCHER per journal entry, containing one
    # ALLLEDGERENTRIES.LIST per line item.
    for entry in entries:
        lines = line_items_by_entry.get(entry.id, [])
        if not lines:
            continue

        message = ET.SubElement(request_data, "TALLYMESSAGE")
        message.set("xmlns:UDF", "TallyUDF")
        voucher = ET.SubElement(message, "VOUCHER")
        voucher.set("VCHTYPE", "Journal")
        voucher.set("ACTION", "Create")
        voucher.set("OBJVIEW", "Accounting Voucher View")

        ET.SubElement(voucher, "DATE").text = entry.entry_date.strftime("%Y%m%d")
        ET.SubElement(voucher, "NARRATION").text = entry.description or ""
        ET.SubElement(voucher, "VOUCHERTYPENAME").text = "Journal"
        ET.SubElement(voucher, "VOUCHERNUMBER").text = entry.reference_id or entry.id[:8]

        for li in lines:
            ledger_entry = ET.SubElement(voucher, "ALLLEDGERENTRIES.LIST")
            ET.SubElement(ledger_entry, "LEDGERNAME").text = account_names.get(li.gl_account_id, li.gl_account_id)
            is_debit = li.debit_amount and li.debit_amount > 0
            ET.SubElement(ledger_entry, "ISDEEMEDPOSITIVE").text = "Yes" if is_debit else "No"
            amount = -li.debit_amount if is_debit else li.credit_amount
            ET.SubElement(ledger_entry, "AMOUNT").text = f"{amount:.2f}"

    xml_bytes = ET.tostring(envelope, encoding="utf-8", xml_declaration=True)
    return xml_bytes.decode("utf-8")


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
