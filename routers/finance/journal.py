"""Journal Entry Router - Double-entry bookkeeping endpoints

ROLE-BASED ACCESS CONTROL:
- SUPER_ADMIN: Full access to all operations
- SCHOOL_ADMIN: Full access to their school's entries
- HR: Can create, post, and reverse entries (payroll integration)
- TEACHER/STUDENT/PARENT: Read-only access to entries

SCHOOL SCOPING:
- All endpoints enforce school_id scoping for multi-tenancy
- SUPER_ADMIN can access any school; others limited to their school_id
- All entries are automatically scoped to the current user's school

ENTRY LIFECYCLE:
- DRAFT: Entry is being prepared, can be updated or deleted
- POSTED: Entry has been posted to GL accounts (immutable)
- REVERSED: Entry was reversed with a contra-entry (maintains history)
- REJECTED: Entry was rejected and never posted (soft-deleted)

DOUBLE-ENTRY BOOKKEEPING:
- Every journal entry must have at least one debit and one credit
- Total debits must exactly equal total credits (±0.01 tolerance)
- All GL accounts must be active and belong to the school
"""
import logging
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional, List
from datetime import datetime

from models.finance import (
    JournalEntry,
    JournalLineItem,
    JournalEntryCreate,
    JournalEntryUpdate,
    JournalEntryResponse,
    JournalEntryPostRequest,
    JournalEntryReverseRequest,
    JournalEntrySummary,
    PostingStatus,
    ReferenceType,
)
from models.finance.gl_audit_log import AuditActionType, AuditEntityType
from models.user import User
from database import get_session
from auth import get_current_user, require_permission
from services.journal_entry_service import JournalEntryService, JournalEntryError, JournalEntryValidationError
from services.gl_audit_log_service import GLAuditLogService
from services.plan_gating import require_plan_feature

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/journal-entries", tags=["Finance - Journal Entries"],
    dependencies=[Depends(require_plan_feature("finance_advanced"))],
)


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
        logger.warning(f"Failed to write GL audit log for journal entry {entity_id}: {e}")


# ==================== Entry Creation ====================

@router.post("", response_model=JournalEntryResponse, status_code=status.HTTP_201_CREATED)
async def create_entry(
    entry_data: JournalEntryCreate,
    current_user: User = Depends(require_permission("finance.journal.create")),
    session: AsyncSession = Depends(get_session),
):
    """Create a new journal entry (in DRAFT status)
    
    **Access:** SUPER_ADMIN, SCHOOL_ADMIN, HR
    
    Required fields:
    - entry_date: Date of the transaction (datetime)
    - reference_type: Type of transaction [payroll_run, fee_payment, expense, manual, adjustment, depreciation, period_closing, bank_reconciliation]
    - description: Human-readable description of the transaction
    - line_items: List of GL entries (≥2 items, ≥1 debit + ≥1 credit)
    
    Optional fields:
    - reference_id: Link to source transaction (payroll_run_id, fee_payment_id, etc.)
    - notes: Additional context or explanation
    
    Validation:
    - Minimum 2 line items required
    - At least 1 debit line + at least 1 credit line required
    - Total debits must equal total credits
    - All GL accounts must exist and be active
    
    Returns:
    - New entry in DRAFT status
    - Can be updated or deleted while in DRAFT
    - Must be posted to affect GL accounts
    
    Example:
    ```json
    {
        "entry_date": "2026-04-01T10:00:00",
        "reference_type": "payroll_run",
        "reference_id": "payroll_run_2026_01",
        "description": "Payroll for January 2026",
        "line_items": [
            {
                "gl_account_id": "5100",
                "debit_amount": 50000.00,
                "description": "Salaries Expense"
            },
            {
                "gl_account_id": "2100",
                "credit_amount": 50000.00,
                "description": "Salaries Payable"
            }
        ],
        "notes": "January payroll run"
    }
    ```
    """
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No school context - contact administrator"
        )
    
    service = JournalEntryService(session)
    
    try:
        entry = await service.create_entry(
            school_id=school_id,
            entry_data=entry_data,
            created_by=current_user.id,
        )
        await _log_gl_audit(
            session, school_id, entry.id, AuditActionType.ENTRY_CREATED, current_user,
            new_values={"description": entry_data.description, "reference_type": entry_data.reference_type,
                        "line_item_count": len(entry_data.line_items)},
        )
        return JournalEntryResponse.model_validate(entry)
    except JournalEntryValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(e)
        )
    except JournalEntryError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error creating entry: {str(e)}"
        )


# ==================== Entry Retrieval ====================

@router.get("/{entry_id}", response_model=JournalEntryResponse)
async def get_entry(
    entry_id: str,
    include_line_items: bool = Query(True, description="Include line items in response"),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Get a specific journal entry by ID
    
    **Access:** All authenticated users (read-only for non-admin)
    
    Query parameters:
    - include_line_items: True/False (default: True) - Include GL line details
    
    Returns:
    - Journal entry with full details
    - If include_line_items=False, returns entry header only (faster for lists)
    
    Note: Returns 404 if entry not found or user lacks permission for school
    """
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No school context"
        )
    
    service = JournalEntryService(session)
    
    try:
        entry = await service.get_entry_by_id(
            school_id=school_id,
            entry_id=entry_id,
            include_line_items=include_line_items,
        )
        if not entry:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Entry {entry_id} not found"
            )
        return JournalEntryResponse.model_validate(entry)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error retrieving entry: {str(e)}"
        )


@router.get("", response_model=List[JournalEntryResponse])
async def list_entries(
    status: Optional[str] = Query(None, description="Filter by posting status"),
    reference_type: Optional[str] = Query(None, description="Filter by reference type"),
    start_date: Optional[datetime] = Query(None, description="Filter by start date"),
    end_date: Optional[datetime] = Query(None, description="Filter by end date"),
    skip: int = Query(0, ge=0, description="Number of entries to skip"),
    limit: int = Query(50, ge=1, le=1000, description="Number of entries to return"),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """List journal entries with optional filtering
    
    **Access:** All authenticated users
    
    Query parameters:
    - status: Filter by status [draft, posted, reversed, rejected]
    - reference_type: Filter by type [payroll_run, fee_payment, expense, manual, adjustment, depreciation, period_closing, bank_reconciliation]
    - start_date: Filter entries from this date onwards (ISO format)
    - end_date: Filter entries up to this date (ISO format)
    - skip: Pagination offset (default: 0)
    - limit: Page size (default: 50, max: 1000)
    
    Returns:
    - List of journal entries matching filters
    - Results ordered by entry_date descending
    - Line items not included in list (use get_entry for full details)
    
    Examples:
    - GET /api/finance/journal - All entries, recent first
    - GET /api/finance/journal?status=posted - All posted entries
    - GET /api/finance/journal?reference_type=payroll_run - All payroll entries
    - GET /api/finance/journal?start_date=2026-01-01&end_date=2026-03-31 - Q1 entries
    - GET /api/finance/journal?status=draft&limit=25 - First 25 draft entries
    """
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No school context"
        )
    
    # Parse optional filter parameters
    posting_status = None
    if status:
        try:
            posting_status = PostingStatus(status)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid status: {status}"
            )
    
    reference_enum = None
    if reference_type:
        try:
            reference_enum = ReferenceType(reference_type)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid reference_type: {reference_type}"
            )
    
    service = JournalEntryService(session)
    
    try:
        entries = await service.get_entries_filtered(
            school_id=school_id,
            posting_status=posting_status,
            reference_type=reference_enum,
            start_date=start_date,
            end_date=end_date,
            skip=skip,
            limit=limit,
            include_line_items=False,  # Optimize for list view
        )
        return [JournalEntryResponse.model_validate(entry) for entry in entries]
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error retrieving entries: {str(e)}"
        )


@router.get("/reference/{reference_type}/{reference_id}", response_model=List[JournalEntryResponse])
async def get_entries_by_reference(
    reference_type: str,
    reference_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Get all journal entries for a specific source transaction
    
    **Access:** All authenticated users (read-only for non-admin)
    
    Path parameters:
    - reference_type: Type of source [payroll_run, fee_payment, expense, etc.]
    - reference_id: ID of the source transaction
    
    Returns:
    - All entries linked to this source
    - Ordered by entry_date
    - Includes line items for full GL detail
    
    Use case: Find all GL postings created by a specific payroll run or fee payment
    
    Example:
    - GET /api/finance/journal/reference/payroll_run/payroll_2026_01
      Returns all entries created from January 2026 payroll
    
    - GET /api/finance/journal/reference/fee_payment/fee_12345
      Returns all entries created from fee payment #12345
    """
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No school context"
        )
    
    try:
        ref_type = ReferenceType(reference_type)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid reference_type: {reference_type}"
        )
    
    service = JournalEntryService(session)
    
    try:
        entries = await service.get_entries_by_reference(
            school_id=school_id,
            reference_type=ref_type,
            reference_id=reference_id,
        )
        return [JournalEntryResponse.model_validate(entry) for entry in entries]
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error retrieving entries: {str(e)}"
        )


# ==================== Entry Updates ====================

@router.put("/{entry_id}", response_model=JournalEntryResponse)
async def update_entry(
    entry_id: str,
    update_data: JournalEntryUpdate,
    current_user: User = Depends(require_permission("finance.journal.update")),
    session: AsyncSession = Depends(get_session),
):
    """Update a journal entry (only DRAFT entries can be updated)
    
    **Access:** SUPER_ADMIN, SCHOOL_ADMIN, HR only
    
    Can update:
    - description: Transaction description
    - notes: Additional context
    - line_items: GL postings (will re-validate balance)
    
    Restrictions:
    - Only DRAFT entries can be updated
    - Posted entries must be reversed to correct them
    - Updated entries must still satisfy double-entry rules
    
    Returns:
    - Updated entry in DRAFT status
    - Will re-validate debits = credits
    
    Note: Returns 400 if entry is already posted (must reverse instead)
    """
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No school context"
        )
    
    service = JournalEntryService(session)
    
    # Verify entry exists and is in DRAFT status
    try:
        entry = await service.get_entry_by_id(school_id, entry_id, include_line_items=False)
        if not entry:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Entry {entry_id} not found"
            )
        
        if entry.get("posting_status") != PostingStatus.DRAFT:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot update entry with status {entry.get('posting_status')} (only DRAFT entries can be updated)"
            )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error retrieving entry: {str(e)}"
        )
    
    try:
        updated_entry = await service.update_entry(
            school_id=school_id,
            entry_id=entry_id,
            update_data=update_data,
        )
        await _log_gl_audit(
            session, school_id, entry_id, AuditActionType.ENTRY_UPDATED, current_user,
            new_values=update_data.model_dump(exclude_unset=True, exclude={"line_items"}),
        )
        return JournalEntryResponse.model_validate(updated_entry)
    except JournalEntryValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(e)
        )
    except JournalEntryError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error updating entry: {str(e)}"
        )


# ==================== Entry Deletion ====================

@router.delete("/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_entry(
    entry_id: str,
    current_user: User = Depends(require_permission("finance.journal.delete")),
    session: AsyncSession = Depends(get_session),
):
    """Delete a journal entry (only DRAFT entries can be deleted)
    
    **Access:** SUPER_ADMIN, SCHOOL_ADMIN, HR only
    
    Restrictions:
    - Only DRAFT entries can be deleted
    - Posted entries must be reversed to correct them (maintains audit trail)
    - Deleted entries are permanently removed (cannot be recovered)
    
    Returns:
    - 204 No Content on success
    
    Note: Returns 400 if entry is already posted (must reverse instead)
    """
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No school context"
        )
    
    service = JournalEntryService(session)

    entry = await service.get_entry_by_id(school_id, entry_id, include_line_items=False)

    try:
        await service.delete_entry(
            school_id=school_id,
            entry_id=entry_id,
        )
        await _log_gl_audit(
            session, school_id, entry_id, AuditActionType.ENTRY_DELETED, current_user,
            old_values={"description": entry.get("description")} if entry else None,
        )
        return None  # 204 No Content
    except JournalEntryError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error deleting entry: {str(e)}"
        )


# ==================== Entry Posting ====================

@router.post("/{entry_id}/post", response_model=JournalEntryResponse)
async def post_entry(
    entry_id: str,
    post_request: JournalEntryPostRequest,
    current_user: User = Depends(require_permission("finance.journal.post")),
    session: AsyncSession = Depends(get_session),
):
    """Post a journal entry to the General Ledger
    
    **Access:** SUPER_ADMIN, SCHOOL_ADMIN, HR only
    
    Request body:
    - approval_notes: Optional notes for approval audit trail
    
    Effect:
    - Changes entry status from DRAFT to POSTED
    - Finalizes GL account postings
    - Entry becomes immutable (can only be reversed, not edited)
    - Records posted_date and posted_by (current user)
    
    Preconditions:
    - Entry must be in DRAFT status
    - Entry must have valid double-entry (debits = credits)
    - All GL accounts must be active
    
    Returns:
    - Entry with POSTED status
    - posted_date and posted_by filled in
    
    Note: Posted entries can only be corrected via reversal (POST /journal/{id}/reverse)
    """
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No school context"
        )
    
    service = JournalEntryService(session)
    
    # Verify entry exists and is in DRAFT status
    try:
        entry = await service.get_entry_by_id(school_id, entry_id, include_line_items=False)
        if not entry:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Entry {entry_id} not found"
            )
        
        if entry.get("posting_status") != PostingStatus.DRAFT:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot post entry with status {entry.get('posting_status')} (only DRAFT entries can be posted)"
            )

        # Segregation of duties (School.require_maker_checker, off by
        # default): only gates the human "post this DRAFT entry" action here
        # at the API boundary — not service.post_entry() itself, which is
        # also called internally by reversal/period-close/expense-posting
        # flows where the same actor creating and posting in one step is
        # intentional and has no separate "checker" step to delegate to.
        if entry.get("created_by") == current_user.id and await service.requires_maker_checker(school_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Segregation of duties: you created this entry and cannot also post it"
            )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error retrieving entry: {str(e)}"
        )

    try:
        posted_entry = await service.post_entry(
            school_id=school_id,
            entry_id=entry_id,
            posted_by=current_user.id,
            approval_notes=post_request.approval_notes,
        )
        await _log_gl_audit(
            session, school_id, entry_id, AuditActionType.ENTRY_POSTED, current_user,
            new_values={"approval_notes": post_request.approval_notes},
        )
        return JournalEntryResponse.model_validate(posted_entry)
    except JournalEntryValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(e)
        )
    except JournalEntryError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error posting entry: {str(e)}"
        )


# ==================== Entry Reversal ====================

@router.post("/{entry_id}/reverse", response_model=dict)
async def reverse_entry(
    entry_id: str,
    reverse_request: JournalEntryReverseRequest,
    current_user: User = Depends(require_permission("finance.journal.reverse")),
    session: AsyncSession = Depends(get_session),
):
    """Reverse a posted journal entry with a contra-entry
    
    **Access:** SUPER_ADMIN, SCHOOL_ADMIN, HR only
    
    Request body:
    - reversal_reason: Required - reason for reversal (e.g., "Incorrect payroll date", "Duplicate entry")
    - reversal_notes: Optional - additional context for audit trail
    
    Effect:
    - Creates a new entry with exact opposite amounts
    - Original entry marked as REVERSED with link to contra-entry
    - Contra-entry marked as POSTED automatically
    - Both entries remain visible in history (immutable audit trail)
    - Net effect on GL accounts is zero (balances out)
    
    Preconditions:
    - Original entry must be in POSTED status
    - Cannot reverse an already-reversed entry
    - Cannot reverse a rejected entry
    
    Returns:
    - JSON with original and reversal entry details
    - Both entries' IDs and statuses
    - Reversal date and user who performed reversal
    
    Use case: Correcting an incorrectly posted transaction
    - Original incorrect posting remains in GL
    - Contra-entry zeros it out
    - Both visible for audit trail
    - Better than deletion for compliance
    
    Example response:
    ```json
    {
        "message": "Entry reversed successfully",
        "original_entry_id": "entry_123",
        "original_status": "reversed",
        "reversal_entry_id": "entry_124",
        "reversal_status": "posted",
        "reversed_by": "user_456",
        "reversed_date": "2026-04-01T10:30:00"
    }
    ```
    """
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No school context"
        )
    
    service = JournalEntryService(session)
    
    # Verify entry exists and is in POSTED status
    try:
        entry = await service.get_entry_by_id(school_id, entry_id, include_line_items=False)
        if not entry:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Entry {entry_id} not found"
            )
        
        if entry.get("posting_status") != PostingStatus.POSTED:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot reverse entry with status {entry.get('posting_status')} (only POSTED entries can be reversed)"
            )
        
        if entry.get("reversal_entry_id"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This entry has already been reversed"
            )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error retrieving entry: {str(e)}"
        )
    
    try:
        original_entry, reversal_entry = await service.reverse_entry(
            school_id=school_id,
            entry_id=entry_id,
            reversed_by=current_user.id,
            reversal_reason=reverse_request.reversal_reason,
            reversal_notes=reverse_request.reversal_notes,
        )
        await _log_gl_audit(
            session, school_id, entry_id, AuditActionType.ENTRY_REVERSED, current_user,
            new_values={"reversal_reason": reverse_request.reversal_reason,
                        "reversal_entry_id": reversal_entry.id},
        )

        return {
            "message": "Entry reversed successfully",
            "original_entry_id": original_entry.id,
            "original_status": original_entry.posting_status.value if hasattr(original_entry.posting_status, 'value') else str(original_entry.posting_status),
            "reversal_entry_id": reversal_entry.id,
            "reversal_status": reversal_entry.posting_status.value if hasattr(reversal_entry.posting_status, 'value') else str(reversal_entry.posting_status),
            "reversed_by": current_user.id,
            "reversed_date": datetime.utcnow().isoformat(),
        }
    except JournalEntryValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(e)
        )
    except JournalEntryError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error reversing entry: {str(e)}"
        )


# ==================== Entry Analysis & Summary ====================

@router.get("/summary/stats", response_model=JournalEntrySummary)
async def get_entry_summary(
    start_date: Optional[datetime] = Query(None, description="Start date for summary"),
    end_date: Optional[datetime] = Query(None, description="End date for summary"),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Get journal entry summary statistics
    
    **Access:** All authenticated users
    
    Query parameters:
    - start_date: Filter summary from this date (ISO format, optional)
    - end_date: Filter summary up to this date (ISO format, optional)
    
    Returns:
    - total_entries: Count of all entries in period
    - posted_entries: Count of POSTED entries
    - draft_entries: Count of DRAFT entries
    - reversed_entries: Count of REVERSED entries
    - rejected_entries: Count of REJECTED entries
    - total_postings: Total number of GL line items
    - total_amount: Sum of all posted amounts
    
    Use case: Dashboard analytics and period close verification
    
    Example response:
    ```json
    {
        "total_entries": 150,
        "posted_entries": 145,
        "draft_entries": 3,
        "reversed_entries": 2,
        "rejected_entries": 0,
        "total_postings": 450,
        "total_amount": 2850000.00
    }
    ```
    """
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No school context"
        )
    
    service = JournalEntryService(session)
    
    try:
        summary = await service.get_entry_summary(
            school_id=school_id,
            start_date=start_date,
            end_date=end_date,
        )
        return summary
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error generating summary: {str(e)}"
        )


@router.get("/trial-balance", response_model=dict)
async def get_trial_balance(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Get trial balance (verify debits = credits)
    
    **Access:** All authenticated users
    
    Returns:
    - debits_total: Sum of all debits across GL accounts
    - credits_total: Sum of all credits across GL accounts
    - is_balanced: Boolean indicating if debits = credits
    - tolerance: Rounding tolerance used (±0.01)
    - accounts: List of accounts with debit/credit balances
    
    Use case: Verify GL integrity and compliance with double-entry bookkeeping
    
    Warning: If is_balanced = false, there's an accounting error to investigate
    
    Example response:
    ```json
    {
        "debits_total": 2850000.00,
        "credits_total": 2850000.00,
        "is_balanced": true,
        "tolerance": 0.01,
        "accounts": [
            {
                "account_code": "1010",
                "account_name": "Business Checking",
                "debit_balance": 500000.00,
                "credit_balance": 0.00,
                "total_balance": 500000.00
            },
            ...
        ]
    }
    ```
    """
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No school context"
        )
    
    service = JournalEntryService(session)
    
    try:
        trial_balance = await service.get_trial_balance(school_id)
        return trial_balance
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error generating trial balance: {str(e)}"
        )
