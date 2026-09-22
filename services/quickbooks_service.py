"""QuickBooks Online integration: per-school OAuth2 connection and a
chart-of-accounts pull (see models/integrations.py:QuickBooksConnection).

Distinct from services/qb_iif_export_service.py, which is a one-way,
offline file export to **QuickBooks Desktop** — this talks to the live
QuickBooks **Online** REST API instead.

Token storage reuses services/ai_key_crypto.py's Fernet scheme, the same
one already used for webhook_endpoints.secret_encrypted. HTTP calls follow
services/webhook_service.py's pattern (timeout set once on the
AsyncClient constructor). GL account creation reuses services/coa_service.py
CoaService.create_account as-is, including its existing account-code
uniqueness check — a sync that hits an already-imported account code is
treated as a skip, not an error, so re-running the sync is idempotent.
"""
import hashlib
import hmac
import logging
import time
from datetime import datetime, timedelta
from typing import Optional, Tuple
from urllib.parse import urlencode

import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from config import get_settings
from models.finance.chart_of_accounts import AccountCategory, AccountType, GLAccountCreate
from models.finance.journal_entries import JournalEntry, JournalLineItem, PostingStatus
from models.integrations import (
    QuickBooksAccountMapping, QuickBooksConnection, QuickBooksJournalSyncLog,
)
from services.ai_key_crypto import decrypt_api_key, encrypt_api_key
from services.coa_service import CoaService, CoaServiceError

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 15.0
STATE_MAX_AGE_SECONDS = 600  # 10 minutes — how long a /connect link stays valid before /callback

AUTHORIZATION_URL = "https://appcenter.intuit.com/connect/oauth2"
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
SCOPE = "com.intuit.quickbooks.accounting"


class QuickBooksError(Exception):
    """Base exception for QuickBooks service errors."""
    pass


# ==================== OAuth state signing ====================
# The callback is hit directly by Intuit's redirect — it can't carry our
# own JWT, so `state` is how we know which school/user initiated the
# connection and that the request wasn't forged. Raw HMAC via stdlib,
# matching services/paystack_service.py's existing HMAC usage rather than
# adding a new dependency (e.g. itsdangerous) for one signed string.

def sign_state(school_id: str, user_id: str) -> str:
    settings = get_settings()
    timestamp = str(int(time.time()))
    payload = f"{school_id}:{user_id}:{timestamp}"
    signature = hmac.new(settings.secret_key.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{signature}"


def verify_state(state: str) -> Tuple[str, str]:
    """Returns (school_id, user_id) or raises QuickBooksError."""
    settings = get_settings()
    try:
        school_id, user_id, timestamp, signature = state.split(":")
    except ValueError:
        raise QuickBooksError("Malformed state parameter")

    payload = f"{school_id}:{user_id}:{timestamp}"
    expected = hmac.new(settings.secret_key.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise QuickBooksError("Invalid state signature")

    if time.time() - int(timestamp) > STATE_MAX_AGE_SECONDS:
        raise QuickBooksError("State has expired — please restart the connection")

    return school_id, user_id


# ==================== Authorization URL ====================

def get_authorization_url(state: str, redirect_uri: str) -> str:
    settings = get_settings()
    params = {
        "client_id": settings.quickbooks_client_id,
        "response_type": "code",
        "scope": SCOPE,
        "redirect_uri": redirect_uri,
        "state": state,
    }
    return f"{AUTHORIZATION_URL}?{urlencode(params)}"


# ==================== Token exchange / refresh ====================

async def exchange_code_for_tokens(code: str, redirect_uri: str) -> dict:
    """POST the authorization code for the first access/refresh token pair."""
    settings = get_settings()
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        response = await client.post(
            TOKEN_URL,
            auth=(settings.quickbooks_client_id, settings.quickbooks_client_secret),
            headers={"Accept": "application/json"},
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
            },
        )
    if not response.is_success:
        logger.error(f"QuickBooks token exchange failed: {response.status_code} {response.text}")
        raise QuickBooksError("Failed to exchange authorization code for tokens")
    return response.json()


async def refresh_access_token(connection: QuickBooksConnection, session: AsyncSession) -> QuickBooksConnection:
    """Refreshes an expired access token. QBO refresh tokens rotate on
    every use — both stored tokens and both expiry fields are overwritten,
    never appended, matching the model's docstring."""
    settings = get_settings()
    raw_refresh_token = decrypt_api_key(connection.refresh_token_encrypted)

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        response = await client.post(
            TOKEN_URL,
            auth=(settings.quickbooks_client_id, settings.quickbooks_client_secret),
            headers={"Accept": "application/json"},
            data={"grant_type": "refresh_token", "refresh_token": raw_refresh_token},
        )
    if not response.is_success:
        logger.error(f"QuickBooks token refresh failed: {response.status_code} {response.text}")
        raise QuickBooksError("Failed to refresh QuickBooks access token — the connection may need to be re-authorized")

    tokens = response.json()
    now = datetime.utcnow()
    connection.access_token_encrypted = encrypt_api_key(tokens["access_token"])
    connection.refresh_token_encrypted = encrypt_api_key(tokens["refresh_token"])
    connection.access_token_expires_at = now + timedelta(seconds=tokens["expires_in"])
    connection.refresh_token_expires_at = now + timedelta(seconds=tokens["x_refresh_token_expires_in"])
    session.add(connection)
    await session.commit()
    await session.refresh(connection)
    return connection


async def _get_valid_access_token(connection: QuickBooksConnection, session: AsyncSession) -> str:
    if connection.access_token_expires_at <= datetime.utcnow():
        connection = await refresh_access_token(connection, session)
    return decrypt_api_key(connection.access_token_encrypted)


def _api_base_url(environment: str) -> str:
    host = "sandbox-quickbooks.api.intuit.com" if environment == "sandbox" else "quickbooks.api.intuit.com"
    return f"https://{host}"


# ==================== Chart of accounts ====================

async def get_chart_of_accounts(connection: QuickBooksConnection, session: AsyncSession) -> list:
    access_token = await _get_valid_access_token(connection, session)
    base_url = _api_base_url(connection.environment)

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        response = await client.get(
            f"{base_url}/v3/company/{connection.realm_id}/query",
            params={"query": "SELECT * FROM Account MAXRESULTS 1000"},
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
        )
    if not response.is_success:
        logger.error(f"QuickBooks chart-of-accounts fetch failed: {response.status_code} {response.text}")
        raise QuickBooksError("Failed to fetch chart of accounts from QuickBooks")

    return response.json().get("QueryResponse", {}).get("Account", [])


# QuickBooks Classification -> Campusio AccountType. The primary mapping —
# always present on a QBO Account, unlike AccountType/AccountSubType below.
CLASSIFICATION_TO_ACCOUNT_TYPE = {
    "Asset": AccountType.ASSET,
    "Liability": AccountType.LIABILITY,
    "Equity": AccountType.EQUITY,
    "Revenue": AccountType.REVENUE,
    "Expense": AccountType.EXPENSE,
}

# QuickBooks AccountType (finer-grained than Classification) -> Campusio
# AccountCategory. Not exhaustive — QBO has more AccountType values than
# Campusio has categories; anything unmapped falls back to a generic
# category for its Classification (see _default_category_for).
QB_ACCOUNT_TYPE_TO_CATEGORY = {
    "Bank": AccountCategory.BANK_ACCOUNTS,
    "Accounts Receivable": AccountCategory.ACCOUNTS_RECEIVABLE,
    "Other Current Asset": AccountCategory.PREPAID_EXPENSES,
    "Fixed Asset": AccountCategory.FIXED_ASSETS,
    "Accounts Payable": AccountCategory.ACCOUNTS_PAYABLE,
    "Credit Card": AccountCategory.SHORT_TERM_DEBT,
    "Other Current Liability": AccountCategory.SHORT_TERM_DEBT,
    "Long Term Liability": AccountCategory.LONG_TERM_DEBT,
    "Equity": AccountCategory.ACCUMULATED_SURPLUS,
    "Income": AccountCategory.OTHER_INCOME,
    "Other Income": AccountCategory.OTHER_INCOME,
    "Cost of Goods Sold": AccountCategory.OTHER_EXPENSES,
    "Expense": AccountCategory.OTHER_EXPENSES,
    "Other Expense": AccountCategory.OTHER_EXPENSES,
}

_DEFAULT_CATEGORY_FOR_TYPE = {
    AccountType.ASSET: AccountCategory.FIXED_ASSETS,
    AccountType.LIABILITY: AccountCategory.SHORT_TERM_DEBT,
    AccountType.EQUITY: AccountCategory.ACCUMULATED_SURPLUS,
    AccountType.REVENUE: AccountCategory.OTHER_INCOME,
    AccountType.EXPENSE: AccountCategory.OTHER_EXPENSES,
}

# QuickBooks AccountSubType values that are contra accounts — their
# Classification/AccountType alone would map to the WRONG normal_balance
# (CoaService.create_account defaults purely off account_type, e.g. every
# ASSET defaults to "debit"). Without this override, "Accumulated
# Depreciation" (Classification=Asset, credit-normal) synced from QBO gets
# defaulted to "debit" — every depreciation entry that credits it then
# moves its balance in the wrong direction, so Fixed Assets on the Balance
# Sheet is silently wrong from the very first sync.
_CONTRA_SUBTYPE_TO_NORMAL_BALANCE = {
    "AccumulatedDepletion": "credit",
    "AccumulatedDepreciation": "credit",
    "AllowanceForBadDebts": "credit",
    "OwnersDraw": "debit",
    "TreasuryStock": "debit",
}


def _map_qb_account(qb_account: dict) -> Optional[GLAccountCreate]:
    """None if the QuickBooks account's Classification isn't one Campusio
    recognizes (rare — QBO always sets Classification in practice)."""
    classification = qb_account.get("Classification")
    account_type = CLASSIFICATION_TO_ACCOUNT_TYPE.get(classification)
    if account_type is None:
        return None

    category = QB_ACCOUNT_TYPE_TO_CATEGORY.get(qb_account.get("AccountType"), _DEFAULT_CATEGORY_FOR_TYPE[account_type])
    account_code = qb_account.get("AcctNum") or f"QB-{qb_account['Id']}"
    normal_balance = _CONTRA_SUBTYPE_TO_NORMAL_BALANCE.get(qb_account.get("AccountSubType"))

    return GLAccountCreate(
        account_code=account_code,
        account_name=qb_account["Name"],
        account_type=account_type,
        account_category=category,
        description=f"Imported from QuickBooks Online (Id {qb_account['Id']})",
        normal_balance=normal_balance,
    )


async def sync_chart_of_accounts(connection: QuickBooksConnection, session: AsyncSession, synced_by: str) -> dict:
    """Pulls every QuickBooks account and creates the ones that don't
    already exist by account_code. Returns {"created": N, "skipped": N}.

    Every account created here also gets a QuickBooksAccountMapping row
    recorded (local GL account -> the QBO Account.Id it came from) — this
    is what lets the push direction (sync_journal_entries) post back to the
    right QBO account without a separate manual mapping step for anything
    that was pulled in through this sync."""
    qb_accounts = await get_chart_of_accounts(connection, session)
    coa_service = CoaService(session)

    created = 0
    skipped = 0
    for qb_account in qb_accounts:
        mapped = _map_qb_account(qb_account)
        if mapped is None:
            skipped += 1
            continue
        try:
            gl_account = await coa_service.create_account(connection.school_id, mapped, created_by=synced_by)
            session.add(QuickBooksAccountMapping(
                school_id=connection.school_id, gl_account_id=gl_account.id,
                qb_account_id=qb_account["Id"], updated_by=synced_by,
            ))
            created += 1
        except CoaServiceError:
            skipped += 1

    connection.last_synced_at = datetime.utcnow()
    session.add(connection)
    await session.commit()

    return {"created": created, "skipped": skipped}


# ==================== Connection storage ====================

async def upsert_connection(
    session: AsyncSession,
    school_id: str,
    realm_id: str,
    tokens: dict,
    environment: str,
    connected_by: str,
) -> QuickBooksConnection:
    now = datetime.utcnow()
    result = await session.execute(select(QuickBooksConnection).where(QuickBooksConnection.school_id == school_id))
    connection = result.scalar_one_or_none()

    if connection is None:
        connection = QuickBooksConnection(school_id=school_id, realm_id=realm_id, environment=environment, connected_by=connected_by)

    connection.realm_id = realm_id
    connection.environment = environment
    connection.is_active = True
    connection.access_token_encrypted = encrypt_api_key(tokens["access_token"])
    connection.refresh_token_encrypted = encrypt_api_key(tokens["refresh_token"])
    connection.access_token_expires_at = now + timedelta(seconds=tokens["expires_in"])
    connection.refresh_token_expires_at = now + timedelta(seconds=tokens["x_refresh_token_expires_in"])

    session.add(connection)
    await session.commit()
    await session.refresh(connection)
    return connection


# ==================== Journal entry push (the two-way half) ====================
# Chart-of-accounts sync above only ever pulls FROM QuickBooks. This is the
# other direction: posted Campusio journal entries pushed INTO QuickBooks
# Online as QBO JournalEntry objects, making the integration a genuine
# two-way sync rather than a one-time/one-direction import.

async def get_account_mapping(session: AsyncSession, school_id: str) -> dict[str, str]:
    """gl_account_id -> QuickBooks Account.Id, for every GL account this
    school has mapped so far (via the pull-sync auto-mapping or the manual
    mapping endpoint)."""
    result = await session.execute(select(QuickBooksAccountMapping).where(QuickBooksAccountMapping.school_id == school_id))
    return {m.gl_account_id: m.qb_account_id for m in result.scalars().all()}


async def entries_to_push(
    session: AsyncSession, school_id: str, start_date: datetime, end_date: datetime, only_new: bool,
) -> list[JournalEntry]:
    start_date = start_date.replace(tzinfo=None) if start_date.tzinfo else start_date
    end_date = end_date.replace(tzinfo=None) if end_date.tzinfo else end_date

    query = select(JournalEntry).where(
        JournalEntry.school_id == school_id,
        JournalEntry.posting_status == PostingStatus.POSTED,
        JournalEntry.entry_date >= start_date,
        JournalEntry.entry_date <= end_date,
    ).order_by(JournalEntry.entry_date)
    result = await session.execute(query)
    entries = list(result.scalars().all())

    if only_new and entries:
        synced_result = await session.execute(
            select(QuickBooksJournalSyncLog.journal_entry_id).where(
                QuickBooksJournalSyncLog.school_id == school_id,
                QuickBooksJournalSyncLog.journal_entry_id.in_([e.id for e in entries]),
            )
        )
        already_synced = {row[0] for row in synced_result.all()}
        entries = [e for e in entries if e.id not in already_synced]

    return entries


async def push_journal_entry(
    connection: QuickBooksConnection, session: AsyncSession, entry: JournalEntry,
    line_items: list[JournalLineItem], account_mapping: dict[str, str],
) -> dict:
    """POSTs one journal entry to QBO as a JournalEntry object. Every line's
    gl_account_id must already be a key in account_mapping — callers check
    this before calling, so a missing mapping surfaces as a per-entry error
    the caller can report, not an exception here."""
    access_token = await _get_valid_access_token(connection, session)
    base_url = _api_base_url(connection.environment)

    qb_lines = []
    for line in line_items:
        is_debit = line.debit_amount and line.debit_amount > 0
        amount = float(line.debit_amount if is_debit else line.credit_amount)
        if amount == 0:
            continue
        qb_lines.append({
            "Amount": amount,
            "DetailType": "JournalEntryLineDetail",
            "Description": (line.description or entry.description)[:4000],
            "JournalEntryLineDetail": {
                "PostingType": "Debit" if is_debit else "Credit",
                "AccountRef": {"value": account_mapping[line.gl_account_id]},
            },
        })

    payload = {
        "TxnDate": entry.entry_date.strftime("%Y-%m-%d"),
        "PrivateNote": (entry.notes or entry.description or "")[:4000],
        "Line": qb_lines,
    }

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        response = await client.post(
            f"{base_url}/v3/company/{connection.realm_id}/journalentry",
            json=payload,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
    if not response.is_success:
        logger.error(f"QuickBooks journal entry push failed for {entry.id}: {response.status_code} {response.text}")
        raise QuickBooksError(f"QuickBooks rejected the entry: {response.text[:300]}")

    return response.json().get("JournalEntry", {})


async def sync_journal_entries(
    connection: QuickBooksConnection, session: AsyncSession, school_id: str,
    start_date: datetime, end_date: datetime, only_new: bool, synced_by: str,
) -> dict:
    """Pushes every posted journal entry in range to QuickBooks Online.
    Returns {"pushed": N, "skipped": N, "errors": [...]}. An entry with a
    line referencing an unmapped GL account is skipped with an error
    message rather than aborting the whole batch — the rest still go."""
    entries = await entries_to_push(session, school_id, start_date, end_date, only_new)
    if not entries:
        return {"pushed": 0, "skipped": 0, "errors": []}

    account_mapping = await get_account_mapping(session, school_id)

    entry_ids = [e.id for e in entries]
    line_items_result = await session.execute(
        select(JournalLineItem).where(JournalLineItem.journal_entry_id.in_(entry_ids)).order_by(JournalLineItem.line_number)
    )
    line_items_by_entry: dict[str, list[JournalLineItem]] = {}
    for li in line_items_result.scalars().all():
        line_items_by_entry.setdefault(li.journal_entry_id, []).append(li)

    pushed = 0
    skipped = 0
    errors: list[str] = []

    for entry in entries:
        lines = line_items_by_entry.get(entry.id, [])
        unmapped = sorted({li.gl_account_id for li in lines if li.gl_account_id not in account_mapping})
        if unmapped:
            skipped += 1
            errors.append(f"Entry {entry.id} ({entry.description}): {len(unmapped)} GL account(s) not mapped to QuickBooks — map them first")
            continue

        try:
            qb_result = await push_journal_entry(connection, session, entry, lines, account_mapping)
        except QuickBooksError as e:
            skipped += 1
            errors.append(f"Entry {entry.id} ({entry.description}): {e}")
            continue

        session.add(QuickBooksJournalSyncLog(
            school_id=school_id, journal_entry_id=entry.id,
            qb_journal_entry_id=qb_result.get("Id", ""), synced_by=synced_by,
        ))
        pushed += 1

    connection.last_synced_at = datetime.utcnow()
    session.add(connection)
    await session.commit()

    return {"pushed": pushed, "skipped": skipped, "errors": errors}
