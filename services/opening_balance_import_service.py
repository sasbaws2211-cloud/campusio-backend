"""Opening-balance import — one-time migration of a Trial Balance export
from Tally or QuickBooks Desktop into a single opening journal entry.

Not a recurring sync: this exists to seed GL account balances once when a
school first moves onto Campusio, matching how real accounting migrations
work (one clean cutover with opening balances, not an ongoing import of
every historical transaction — arbitrary externally-authored journal
entries have no way to link back to a Student/Fee/Payroll record the way
Campusio's own postings do, so importing full transaction history isn't a
safe operation to offer).

Expected input: a CSV with an account-name column and Debit/Credit columns
— exactly what a Trial Balance report exports as from either platform.
Column names are matched loosely/case-insensitively so minor export
variations don't require reformatting the file by hand.
"""
import csv
import io
from decimal import Decimal, InvalidOperation
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from models.finance.chart_of_accounts import GLAccount
from models.finance.accounting_integration import ExternalAccountMapping, AccountingProvider

NAME_HEADERS = {"account", "account name", "ledger", "ledger name", "name", "particulars"}
DEBIT_HEADERS = {"debit", "dr", "debit amount", "debit (ghs)"}
CREDIT_HEADERS = {"credit", "cr", "credit amount", "credit (ghs)"}


def _find_column(fieldnames, candidates) -> Optional[str]:
    for field in fieldnames or []:
        if field and field.strip().lower() in candidates:
            return field
    return None


def _parse_amount(raw: Optional[str]) -> Decimal:
    if not raw or not raw.strip():
        return Decimal("0")
    cleaned = raw.replace(",", "").replace("GHS", "").strip()
    if cleaned in ("-", ""):
        return Decimal("0")
    try:
        return abs(Decimal(cleaned))
    except InvalidOperation:
        return Decimal("0")


async def parse_opening_balance_csv(
    session: AsyncSession, school_id: str, provider: AccountingProvider, csv_text: str,
) -> dict:
    """Returns {"matched": [...], "unmatched": [...], "total_debit",
    "total_credit", "is_balanced"} — or {"error": "..."} if the file
    doesn't look like a trial balance export at all."""
    reader = csv.DictReader(io.StringIO(csv_text))
    name_col = _find_column(reader.fieldnames, NAME_HEADERS)
    debit_col = _find_column(reader.fieldnames, DEBIT_HEADERS)
    credit_col = _find_column(reader.fieldnames, CREDIT_HEADERS)

    if not name_col or (not debit_col and not credit_col):
        return {
            "error": (
                "Couldn't find an account name column and a Debit/Credit column in this file. "
                "Expected headers like 'Account Name', 'Debit', 'Credit' — export a Trial Balance "
                "report to CSV and upload that."
            ),
            "matched": [], "unmatched": [], "total_debit": 0, "total_credit": 0, "is_balanced": False,
        }

    accounts_result = await session.execute(
        select(GLAccount).where(GLAccount.school_id == school_id, GLAccount.is_active == True)  # noqa: E712
    )
    accounts = accounts_result.scalars().all()
    accounts_by_id = {a.id: a for a in accounts}
    by_lower_name = {a.account_name.strip().lower(): a for a in accounts}

    mappings_result = await session.execute(
        select(ExternalAccountMapping).where(
            ExternalAccountMapping.school_id == school_id,
            ExternalAccountMapping.provider == provider,
        )
    )
    by_external_name = {m.external_name.strip().lower(): m.gl_account_id for m in mappings_result.scalars().all()}

    matched, unmatched = [], []
    total_debit = Decimal("0")
    total_credit = Decimal("0")

    for row in reader:
        raw_name = (row.get(name_col) or "").strip()
        if not raw_name:
            continue
        debit = _parse_amount(row.get(debit_col)) if debit_col else Decimal("0")
        credit = _parse_amount(row.get(credit_col)) if credit_col else Decimal("0")
        if debit == 0 and credit == 0:
            continue

        lower_name = raw_name.lower()
        account = None
        if lower_name in by_external_name:
            account = accounts_by_id.get(by_external_name[lower_name])
        elif lower_name in by_lower_name:
            account = by_lower_name[lower_name]

        if account:
            matched.append({
                "external_name": raw_name,
                "gl_account_id": account.id,
                "account_code": account.account_code,
                "account_name": account.account_name,
                "debit": float(debit),
                "credit": float(credit),
            })
            total_debit += debit
            total_credit += credit
        else:
            unmatched.append({"external_name": raw_name, "debit": float(debit), "credit": float(credit)})

    return {
        "matched": matched,
        "unmatched": unmatched,
        "total_debit": float(total_debit),
        "total_credit": float(total_credit),
        "is_balanced": abs(total_debit - total_credit) < Decimal("0.01"),
    }
