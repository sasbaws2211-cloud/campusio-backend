"""Accounting Integrations Router — one-way export to Tally and QuickBooks
Desktop via generated import files (see services/tally_export_service.py
and services/qb_iif_export_service.py for why this is file-based, not a
live API push), plus a one-time opening-balance import in the reverse
direction (see services/opening_balance_import_service.py)."""
import logging
from datetime import datetime
from decimal import Decimal
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from models.finance.chart_of_accounts import GLAccount
from models.finance.accounting_integration import (
    ExternalAccountMapping, ExternalAccountMappingUpsert, AccountingProvider,
    JournalEntrySyncLog, OpeningBalanceImport,
)
from models.finance.journal_entries import JournalEntryCreate, JournalLineItemCreate, ReferenceType
from models.finance.gl_audit_log import AuditActionType, AuditEntityType
from models.integrations import (
    QuickBooksAccountMapping, QuickBooksAccountMappingUpsert, QuickBooksConnection,
    QuickBooksConnectionStatus, QuickBooksJournalPushResult, QuickBooksJournalSyncLog,
    QuickBooksSyncResult,
)
from models.user import User, UserRole
from config import get_settings
from database import get_session
from auth import require_roles
from services.tally_export_service import generate_tally_xml, mark_exported as mark_tally_exported, entries_to_export as tally_entries_to_export
from services.qb_iif_export_service import generate_qb_iif, mark_exported as mark_qb_exported, entries_to_export as qb_entries_to_export
from services.opening_balance_import_service import parse_opening_balance_csv
from services.journal_entry_service import JournalEntryService, JournalEntryError
from services.gl_audit_log_service import GLAuditLogService
from services import quickbooks_service
from services.plan_gating import require_plan_feature

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/accounting-integrations", tags=["Accounting Integrations"])

VIEW_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.HR)
WRITE_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)


async def _log_gl_audit(
    session: AsyncSession,
    school_id: str,
    entity_id: str,
    action: AuditActionType,
    current_user: User,
    old_values: Optional[dict] = None,
    new_values: Optional[dict] = None,
) -> None:
    """Best-effort GL audit log write — never blocks the actual mutation if it fails."""
    try:
        await GLAuditLogService(session).log_action(
            school_id=school_id,
            entity_type=AuditEntityType.JOURNAL_ENTRY,
            entity_id=entity_id,
            action=action,
            user_id=current_user.id,
            user_name=f"{current_user.first_name} {current_user.last_name}",
            user_role=current_user.role.value,
            old_values=old_values,
            new_values=new_values,
        )
    except Exception as e:
        logger.warning(f"Failed to write GL audit log for opening balance import {entity_id}: {e}")


@router.get("/mappings", response_model=List[dict])
async def list_account_mappings(
    provider: AccountingProvider,
    current_user: User = Depends(require_roles(*VIEW_ROLES)),
    _plan_check: User = Depends(require_plan_feature("fees_advanced")),
    session: AsyncSession = Depends(get_session),
):
    """Every active GL account for this school, with its current external
    name (an override if one is mapped, otherwise our own account_name)."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    accounts_result = await session.execute(
        select(GLAccount).where(GLAccount.school_id == school_id, GLAccount.is_active == True)  # noqa: E712
        .order_by(GLAccount.account_code)
    )
    accounts = accounts_result.scalars().all()

    mappings_result = await session.execute(
        select(ExternalAccountMapping).where(
            ExternalAccountMapping.school_id == school_id,
            ExternalAccountMapping.provider == provider,
        )
    )
    mapping_by_account = {m.gl_account_id: m for m in mappings_result.scalars().all()}

    return [
        {
            "gl_account_id": a.id,
            "account_code": a.account_code,
            "account_name": a.account_name,
            "account_type": a.account_type.value,
            "external_name": mapping_by_account[a.id].external_name if a.id in mapping_by_account else a.account_name,
            "is_override": a.id in mapping_by_account,
            "mapping_id": mapping_by_account[a.id].id if a.id in mapping_by_account else None,
        }
        for a in accounts
    ]


@router.put("/mappings", response_model=dict)
async def upsert_account_mapping(
    data: ExternalAccountMappingUpsert,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    _plan_check: User = Depends(require_plan_feature("fees_advanced")),
    session: AsyncSession = Depends(get_session),
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    account_result = await session.execute(
        select(GLAccount).where(GLAccount.id == data.gl_account_id, GLAccount.school_id == school_id)
    )
    if not account_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="GL account not found")

    existing_result = await session.execute(
        select(ExternalAccountMapping).where(
            ExternalAccountMapping.gl_account_id == data.gl_account_id,
            ExternalAccountMapping.provider == data.provider,
        )
    )
    mapping = existing_result.scalar_one_or_none()
    if mapping:
        mapping.external_name = data.external_name
        mapping.updated_at = datetime.utcnow()
        mapping.updated_by = current_user.email
    else:
        mapping = ExternalAccountMapping(
            school_id=school_id, gl_account_id=data.gl_account_id, provider=data.provider,
            external_name=data.external_name, updated_by=current_user.email,
        )
    session.add(mapping)
    await session.commit()
    await session.refresh(mapping)
    return jsonable_encoder(mapping)


@router.delete("/mappings/{mapping_id}", response_model=dict)
async def delete_account_mapping(
    mapping_id: str,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    _plan_check: User = Depends(require_plan_feature("fees_advanced")),
    session: AsyncSession = Depends(get_session),
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(ExternalAccountMapping).where(
            ExternalAccountMapping.id == mapping_id, ExternalAccountMapping.school_id == school_id,
        )
    )
    mapping = result.scalar_one_or_none()
    if not mapping:
        raise HTTPException(status_code=404, detail="Mapping not found")

    await session.delete(mapping)
    await session.commit()
    return {"success": True, "message": "Mapping removed — will use the account's own name"}


@router.get("/tally/export", response_class=StreamingResponse)
async def export_tally(
    start_date: datetime,
    end_date: datetime,
    only_new: bool = Query(True, description="Skip journal entries already included in a previous Tally export"),
    current_user: User = Depends(require_roles(*VIEW_ROLES)),
    _plan_check: User = Depends(require_plan_feature("fees_advanced")),
    session: AsyncSession = Depends(get_session),
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    if end_date < start_date:
        raise HTTPException(status_code=400, detail="end_date must be after start_date")

    entries = await tally_entries_to_export(session, school_id, start_date, end_date, AccountingProvider.TALLY, only_new)
    if not entries:
        raise HTTPException(status_code=404, detail="No new posted journal entries in this range to export")

    xml_content = await generate_tally_xml(session, school_id, start_date, end_date, only_new)
    await mark_tally_exported(session, school_id, entries, AccountingProvider.TALLY, current_user.email)

    filename = f"tally_export_{start_date.date()}_to_{end_date.date()}.xml"
    return StreamingResponse(
        iter([xml_content]), media_type="application/xml",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/quickbooks-desktop/export", response_class=StreamingResponse)
async def export_quickbooks_desktop(
    start_date: datetime,
    end_date: datetime,
    only_new: bool = Query(True, description="Skip journal entries already included in a previous QuickBooks export"),
    current_user: User = Depends(require_roles(*VIEW_ROLES)),
    _plan_check: User = Depends(require_plan_feature("fees_advanced")),
    session: AsyncSession = Depends(get_session),
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    if end_date < start_date:
        raise HTTPException(status_code=400, detail="end_date must be after start_date")

    entries = await qb_entries_to_export(session, school_id, start_date, end_date, AccountingProvider.QUICKBOOKS_DESKTOP, only_new)
    if not entries:
        raise HTTPException(status_code=404, detail="No new posted journal entries in this range to export")

    iif_content = await generate_qb_iif(session, school_id, start_date, end_date, only_new)
    await mark_qb_exported(session, school_id, entries, AccountingProvider.QUICKBOOKS_DESKTOP, current_user.email)

    filename = f"quickbooks_export_{start_date.date()}_to_{end_date.date()}.iif"
    return StreamingResponse(
        iter([iif_content]), media_type="text/plain",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/export-history", response_model=List[dict])
async def export_history(
    provider: AccountingProvider,
    limit: int = Query(50, ge=1, le=200),
    current_user: User = Depends(require_roles(*VIEW_ROLES)),
    _plan_check: User = Depends(require_plan_feature("fees_advanced")),
    session: AsyncSession = Depends(get_session),
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(JournalEntrySyncLog).where(
            JournalEntrySyncLog.school_id == school_id, JournalEntrySyncLog.provider == provider,
        ).order_by(JournalEntrySyncLog.exported_at.desc()).limit(limit)
    )
    return [jsonable_encoder(log) for log in result.scalars().all()]


# ── Opening balance import (Tally/QuickBooks Desktop -> Campusio) ──────────
# One-time migration only — see services/opening_balance_import_service.py's
# module docstring for why this doesn't attempt full transaction-history or
# ongoing two-way sync.

@router.post("/opening-balance/preview", response_model=dict)
async def preview_opening_balance_import(
    provider: AccountingProvider = Form(...),
    file: UploadFile = File(...),
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    _plan_check: User = Depends(require_plan_feature("fees_advanced")),
    session: AsyncSession = Depends(get_session),
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    raw = await file.read()
    content = raw.decode("utf-8-sig", errors="replace")
    return await parse_opening_balance_csv(session, school_id, provider, content)


@router.post("/opening-balance/import", response_model=dict)
async def confirm_opening_balance_import(
    provider: AccountingProvider = Form(...),
    as_of_date: str = Form(..., description="YYYY-MM-DD — the cutover date these balances are as of"),
    file: UploadFile = File(...),
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    _plan_check: User = Depends(require_plan_feature("fees_advanced")),
    session: AsyncSession = Depends(get_session),
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    raw = await file.read()
    content = raw.decode("utf-8-sig", errors="replace")
    result = await parse_opening_balance_csv(session, school_id, provider, content)

    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    if result["unmatched"]:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{len(result['unmatched'])} account(s) in this file couldn't be matched to a GL account — "
                "map them under Account Name Mapping first, or fix the names in the file, then try again."
            ),
        )
    if not result["matched"]:
        raise HTTPException(status_code=400, detail="No rows to import")
    if not result["is_balanced"]:
        raise HTTPException(status_code=400, detail="Total debits and credits don't match in this file — check the export before importing")

    try:
        entry_date = datetime.strptime(as_of_date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="as_of_date must be in YYYY-MM-DD format")

    line_items = [
        JournalLineItemCreate(
            gl_account_id=row["gl_account_id"],
            debit_amount=Decimal(str(row["debit"])),
            credit_amount=Decimal(str(row["credit"])),
            description=f"Opening balance — {row['account_name']}",
        )
        for row in result["matched"]
    ]

    journal_service = JournalEntryService(session)
    try:
        entry = await journal_service.create_entry(
            school_id=school_id,
            entry_data=JournalEntryCreate(
                entry_date=entry_date,
                reference_type=ReferenceType.ADJUSTMENT,
                description=f"Opening balances imported from {provider.value}",
                line_items=line_items,
                notes=f"Opening balance migration import — {len(line_items)} account(s), as of {as_of_date}",
            ),
            created_by=current_user.email,
        )
        await journal_service.post_entry(school_id=school_id, entry_id=entry.id, posted_by=current_user.email)
    except JournalEntryError as e:
        raise HTTPException(status_code=400, detail=str(e))

    import_record = OpeningBalanceImport(
        school_id=school_id,
        provider=provider,
        journal_entry_id=entry.id,
        as_of_date=as_of_date,
        row_count=len(result["matched"]),
        total_debit=result["total_debit"],
        total_credit=result["total_credit"],
        imported_by=current_user.email,
    )
    session.add(import_record)
    await session.commit()

    await _log_gl_audit(
        session, school_id, entry.id, AuditActionType.OPENING_BALANCE_IMPORTED, current_user,
        new_values={
            "provider": provider.value, "as_of_date": as_of_date,
            "accounts_imported": len(result["matched"]),
            "total_debit": result["total_debit"], "total_credit": result["total_credit"],
        },
    )

    return {
        "success": True,
        "journal_entry_id": entry.id,
        "accounts_imported": len(result["matched"]),
        "total_debit": result["total_debit"],
        "total_credit": result["total_credit"],
    }


@router.get("/opening-balance/history", response_model=List[dict])
async def opening_balance_import_history(
    current_user: User = Depends(require_roles(*VIEW_ROLES)),
    _plan_check: User = Depends(require_plan_feature("fees_advanced")),
    session: AsyncSession = Depends(get_session),
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(OpeningBalanceImport).where(OpeningBalanceImport.school_id == school_id)
        .order_by(OpeningBalanceImport.imported_at.desc())
    )
    return [jsonable_encoder(r) for r in result.scalars().all()]


# ==================== QuickBooks Online (live, OAuth2) ====================
# Distinct from the QuickBooks Desktop file export above — see
# services/quickbooks_service.py's module docstring.

def _quickbooks_redirect_uri(request: Request) -> str:
    """Must match exactly what's registered in the Intuit app's Redirect
    URIs list — built from the incoming request so it's correct whether
    this runs on localhost, a preview URL, or production."""
    return f"{str(request.base_url).rstrip('/')}/api/accounting-integrations/quickbooks/callback"


@router.get("/quickbooks/status", response_model=QuickBooksConnectionStatus)
async def quickbooks_connection_status(
    current_user: User = Depends(require_roles(*VIEW_ROLES)),
    _plan_check: User = Depends(require_plan_feature("fees_advanced")),
    session: AsyncSession = Depends(get_session),
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(QuickBooksConnection).where(QuickBooksConnection.school_id == school_id, QuickBooksConnection.is_active == True)  # noqa: E712
    )
    connection = result.scalar_one_or_none()
    if connection is None:
        return QuickBooksConnectionStatus(connected=False)

    return QuickBooksConnectionStatus(
        connected=True,
        realm_id=connection.realm_id,
        environment=connection.environment,
        last_synced_at=connection.last_synced_at,
    )


@router.get("/quickbooks/connect", response_model=dict)
async def quickbooks_connect(
    request: Request,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    _plan_check: User = Depends(require_plan_feature("fees_advanced")),
):
    """Returns the Intuit consent-screen URL for this school admin to
    click through — the browser does the redirect, not this endpoint,
    so it stays a plain JSON GET rather than an HTTP redirect response."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    if not get_settings().quickbooks_client_id:
        raise HTTPException(status_code=503, detail="QuickBooks integration is not configured for this deployment")

    state = quickbooks_service.sign_state(school_id, current_user.id)
    redirect_uri = _quickbooks_redirect_uri(request)
    return {"authorization_url": quickbooks_service.get_authorization_url(state, redirect_uri)}


@router.get("/quickbooks/callback")
async def quickbooks_callback(
    request: Request,
    code: str,
    realmId: str,
    state: str,
    session: AsyncSession = Depends(get_session),
):
    """Hit directly by Intuit's redirect after the school admin authorizes
    — no require_roles here, since this request carries no Campusio JWT.
    `state` (see services/quickbooks_service.py:verify_state) is what
    proves this callback corresponds to a connection this school actually
    initiated, and hasn't been replayed past its 10-minute window."""
    try:
        school_id, user_id = quickbooks_service.verify_state(state)
    except quickbooks_service.QuickBooksError as e:
        raise HTTPException(status_code=400, detail=str(e))

    redirect_uri = _quickbooks_redirect_uri(request)
    try:
        tokens = await quickbooks_service.exchange_code_for_tokens(code, redirect_uri)
        await quickbooks_service.upsert_connection(
            session, school_id, realmId, tokens,
            environment=get_settings().quickbooks_environment,
            connected_by=user_id,
        )
    except quickbooks_service.QuickBooksError as e:
        raise HTTPException(status_code=502, detail=str(e))

    # /finance?tab=integrations is where AccountingIntegrationsManager (and
    # this QuickBooks section) actually lives — not /settings/integrations,
    # which is the unrelated API-key/webhook IntegrationsPage. Built from
    # settings.frontend_url, not a relative path — the backend and frontend
    # are different origins in dev.
    frontend_base = get_settings().frontend_url.rstrip("/")
    return RedirectResponse(url=f"{frontend_base}/finance?tab=integrations&quickbooks=connected")


@router.post("/quickbooks/sync/chart-of-accounts", response_model=QuickBooksSyncResult)
async def quickbooks_sync_chart_of_accounts(
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    _plan_check: User = Depends(require_plan_feature("fees_advanced")),
    session: AsyncSession = Depends(get_session),
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(QuickBooksConnection).where(QuickBooksConnection.school_id == school_id, QuickBooksConnection.is_active == True)  # noqa: E712
    )
    connection = result.scalar_one_or_none()
    if connection is None:
        raise HTTPException(status_code=400, detail="No active QuickBooks connection for this school — connect one first")

    try:
        sync_result = await quickbooks_service.sync_chart_of_accounts(connection, session, synced_by=current_user.email)
    except quickbooks_service.QuickBooksError as e:
        raise HTTPException(status_code=502, detail=str(e))

    return QuickBooksSyncResult(**sync_result)


# ── QuickBooks Online: journal entry push (the two-way half) ──────────────
# See services/quickbooks_service.py's "Journal entry push" section — the
# chart-of-accounts sync above only ever pulls; this pushes posted journal
# entries the other way, into QBO.

@router.get("/quickbooks/account-mappings", response_model=List[dict])
async def list_quickbooks_account_mappings(
    current_user: User = Depends(require_roles(*VIEW_ROLES)),
    _plan_check: User = Depends(require_plan_feature("fees_advanced")),
    session: AsyncSession = Depends(get_session),
):
    """Every active GL account for this school, with its mapped QBO
    Account.Id if one exists — accounts pulled in via the chart-of-accounts
    sync are mapped automatically; anything else needs mapping here before
    it can appear on a pushed journal entry."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    accounts_result = await session.execute(
        select(GLAccount).where(GLAccount.school_id == school_id, GLAccount.is_active == True)  # noqa: E712
        .order_by(GLAccount.account_code)
    )
    accounts = accounts_result.scalars().all()

    mappings_result = await session.execute(select(QuickBooksAccountMapping).where(QuickBooksAccountMapping.school_id == school_id))
    mapping_by_account = {m.gl_account_id: m for m in mappings_result.scalars().all()}

    return [
        {
            "gl_account_id": a.id,
            "account_code": a.account_code,
            "account_name": a.account_name,
            "account_type": a.account_type.value,
            "qb_account_id": mapping_by_account[a.id].qb_account_id if a.id in mapping_by_account else None,
            "mapping_id": mapping_by_account[a.id].id if a.id in mapping_by_account else None,
        }
        for a in accounts
    ]


@router.put("/quickbooks/account-mappings", response_model=dict)
async def upsert_quickbooks_account_mapping(
    data: QuickBooksAccountMappingUpsert,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    _plan_check: User = Depends(require_plan_feature("fees_advanced")),
    session: AsyncSession = Depends(get_session),
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    account_result = await session.execute(select(GLAccount).where(GLAccount.id == data.gl_account_id, GLAccount.school_id == school_id))
    if not account_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="GL account not found")

    existing_result = await session.execute(select(QuickBooksAccountMapping).where(QuickBooksAccountMapping.gl_account_id == data.gl_account_id))
    mapping = existing_result.scalar_one_or_none()
    if mapping:
        mapping.qb_account_id = data.qb_account_id
        mapping.updated_at = datetime.utcnow()
        mapping.updated_by = current_user.email
    else:
        mapping = QuickBooksAccountMapping(
            school_id=school_id, gl_account_id=data.gl_account_id,
            qb_account_id=data.qb_account_id, updated_by=current_user.email,
        )
    session.add(mapping)
    await session.commit()
    await session.refresh(mapping)
    return jsonable_encoder(mapping)


@router.delete("/quickbooks/account-mappings/{mapping_id}", response_model=dict)
async def delete_quickbooks_account_mapping(
    mapping_id: str,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    _plan_check: User = Depends(require_plan_feature("fees_advanced")),
    session: AsyncSession = Depends(get_session),
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(select(QuickBooksAccountMapping).where(QuickBooksAccountMapping.id == mapping_id, QuickBooksAccountMapping.school_id == school_id))
    mapping = result.scalar_one_or_none()
    if not mapping:
        raise HTTPException(status_code=404, detail="Mapping not found")

    await session.delete(mapping)
    await session.commit()
    return {"success": True, "message": "Mapping removed"}


@router.post("/quickbooks/sync/journal-entries", response_model=QuickBooksJournalPushResult)
async def quickbooks_push_journal_entries(
    start_date: datetime,
    end_date: datetime,
    only_new: bool = Query(True, description="Skip journal entries already pushed to QuickBooks in a previous sync"),
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    _plan_check: User = Depends(require_plan_feature("fees_advanced")),
    session: AsyncSession = Depends(get_session),
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    if end_date < start_date:
        raise HTTPException(status_code=400, detail="end_date must be after start_date")

    result = await session.execute(
        select(QuickBooksConnection).where(QuickBooksConnection.school_id == school_id, QuickBooksConnection.is_active == True)  # noqa: E712
    )
    connection = result.scalar_one_or_none()
    if connection is None:
        raise HTTPException(status_code=400, detail="No active QuickBooks connection for this school — connect one first")

    try:
        push_result = await quickbooks_service.sync_journal_entries(
            connection, session, school_id, start_date, end_date, only_new, synced_by=current_user.email,
        )
    except quickbooks_service.QuickBooksError as e:
        raise HTTPException(status_code=502, detail=str(e))

    return QuickBooksJournalPushResult(**push_result)


@router.get("/quickbooks/journal-sync-history", response_model=List[dict])
async def quickbooks_journal_sync_history(
    limit: int = Query(50, ge=1, le=200),
    current_user: User = Depends(require_roles(*VIEW_ROLES)),
    _plan_check: User = Depends(require_plan_feature("fees_advanced")),
    session: AsyncSession = Depends(get_session),
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(QuickBooksJournalSyncLog).where(QuickBooksJournalSyncLog.school_id == school_id)
        .order_by(QuickBooksJournalSyncLog.synced_at.desc()).limit(limit)
    )
    return [jsonable_encoder(log) for log in result.scalars().all()]


@router.delete("/quickbooks/disconnect", status_code=204)
async def quickbooks_disconnect(
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    _plan_check: User = Depends(require_plan_feature("fees_advanced")),
    session: AsyncSession = Depends(get_session),
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(select(QuickBooksConnection).where(QuickBooksConnection.school_id == school_id))
    connection = result.scalar_one_or_none()
    if connection is None:
        raise HTTPException(status_code=404, detail="No QuickBooks connection found")

    await session.delete(connection)
    await session.commit()
    return None
