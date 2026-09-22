"""Accounting Integration models — GL-account-to-external-ledger/account
name mappings and export tracking for one-way sync to Tally and QuickBooks
Desktop.

Neither platform exposes a live API a cloud multi-tenant app can push into
directly: Tally has no public REST API (only an XML-over-HTTP interface to
a Tally instance running on the *same local network*, which a cloud
backend can't generally reach), and QuickBooks Desktop has no API at all —
only file-based import (IIF). So "integration" here means generating an
import file (Tally's XML voucher-import schema / QuickBooks' IIF format)
that the school's accountant brings into their own install. See
services/tally_export_service.py and services/qb_iif_export_service.py.
"""
from sqlmodel import SQLModel, Field
from typing import Optional
from datetime import datetime
from enum import Enum
import uuid


class AccountingProvider(str, Enum):
    TALLY = "tally"
    QUICKBOOKS_DESKTOP = "quickbooks_desktop"


class ExternalAccountMapping(SQLModel, table=True):
    """Maps one of our GL accounts to the ledger/account NAME the school
    uses for it in an external system. Both Tally (LEDGERNAME) and
    QuickBooks Desktop IIF (ACCNT) identify accounts by name, not by our
    internal id, so this is a simple name override per (account, provider).
    No mapping row means "use our account_name as-is" — export falls back
    to that, so it works immediately for schools that just create matching
    ledger/account names on the other side, without requiring setup first."""
    __tablename__ = "external_account_mappings"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    gl_account_id: str = Field(index=True)
    provider: AccountingProvider = Field(index=True)
    external_name: str
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    updated_by: str


class ExternalAccountMappingUpsert(SQLModel):
    gl_account_id: str
    provider: AccountingProvider
    external_name: str


class OpeningBalanceImport(SQLModel, table=True):
    """Audit record of a one-time opening-balance migration from Tally or
    QuickBooks Desktop (see services/opening_balance_import_service.py and
    the /opening-balance/import endpoint). Not a recurring sync — this
    exists to seed GL account balances once when a school first moves onto
    Campusio, from a Trial Balance export off their old system, the same
    way any accounting-system migration is normally done: one clean
    cutover with opening balances, not an import of every historical
    transaction."""
    __tablename__ = "opening_balance_imports"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    provider: AccountingProvider
    journal_entry_id: str
    as_of_date: str
    row_count: int
    total_debit: float
    total_credit: float
    imported_by: str
    imported_at: datetime = Field(default_factory=datetime.utcnow)


class JournalEntrySyncLog(SQLModel, table=True):
    """Idempotency record: which JournalEntry rows have already been
    included in a given provider's export, so re-running an export for an
    overlapping date range doesn't hand the accountant duplicate
    vouchers/transactions to import twice. This tracks "was included in a
    generated export file", not "confirmed imported into Tally/QuickBooks"
    — there's no feedback channel from either platform to know that."""
    __tablename__ = "journal_entry_sync_logs"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    journal_entry_id: str = Field(index=True)
    provider: AccountingProvider = Field(index=True)
    exported_at: datetime = Field(default_factory=datetime.utcnow)
    exported_by: str
