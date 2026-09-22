"""API Router for Bank Reconciliation

Endpoints for:
- Bank statement import
- Automatic GL matching
- Manual transaction matching
- Reconciliation analysis
- Variance reporting
- Reconciliation approval
"""
import logging
from typing import Optional, List
from sqlmodel import SQLModel
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime

from services.bank_reconciliation_service import (
    BankReconciliationService,
    BankReconciliationError,
)
from models.finance.bank_reconciliation import (
    BankStatementCreate,
    BankReconciliationCreate,
    BankTransactionType,
)
from models.finance.gl_audit_log import AuditActionType, AuditEntityType
from dependencies import get_current_school_id
from auth import get_current_user, require_permission
from database import get_session
from models.user import User
from services.gl_audit_log_service import GLAuditLogService
from services.plan_gating import require_plan_feature

# Importing statements, matching, and approving reconciliations all mutate
# GL-adjacent records — same role gate as journal.py's posting endpoints.

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/bank-reconciliation", tags=["Bank Reconciliation"],
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
            entity_type=AuditEntityType.BANK_RECONCILIATION,
            entity_id=entity_id,
            action=action,
            user_id=current_user.id,
            user_name=f"{current_user.first_name} {current_user.last_name}",
            user_role=current_user.role.value,
            old_values=old_values,
            new_values=new_values,
        )
    except Exception as e:
        logger.warning(f"Failed to write GL audit log for bank reconciliation {entity_id}: {e}")


# ── Request bodies (mutating endpoints must never take bare query params) ──

class ImportBankStatementRequest(SQLModel):
    account_id: str
    statement_date: str
    statement_beginning_balance: float
    statement_ending_balance: float
    transactions: Optional[List[BankStatementCreate]] = None


class AutoMatchRequest(SQLModel):
    matching_window_days: int = 5


class ManualMatchRequest(SQLModel):
    variance_reason: Optional[str] = None



# ==================== Bank Statement Import ====================

@router.post("/import", response_model=dict)
async def import_bank_statement(
    body: ImportBankStatementRequest,
    current_user: User = Depends(require_permission("finance.bank_reconciliation.manage")),
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Import a bank statement with transactions
    
    Creates a new bank reconciliation record and imports all transactions
    from the bank statement.
    
    Args:
        account_id: GL bank account ID
        statement_date: Date of bank statement (YYYY-MM-DD)
        statement_beginning_balance: Opening balance
        statement_ending_balance: Closing balance
        transactions: List of bank transactions
        current_user: Current user
        school_id: School identifier
        session: Database session
        
    Returns:
        Reconciliation ID and import summary
        
    Raises:
        HTTPException 400: If import fails
    """
    account_id = body.account_id
    statement_date = body.statement_date
    statement_beginning_balance = body.statement_beginning_balance
    statement_ending_balance = body.statement_ending_balance
    transactions = body.transactions
    try:
        statement_dt = datetime.fromisoformat(statement_date)
        
        service = BankReconciliationService(session)
        recon_id = await service.import_bank_statement(
            school_id=school_id,
            gl_account_id=account_id,
            statement_date=statement_dt,
            statement_beginning_balance=statement_beginning_balance,
            statement_ending_balance=statement_ending_balance,
            transactions=transactions or [],
            reconciled_by=current_user.id,
            notes=f"Imported by {current_user.first_name} {current_user.last_name}",
        )

        logger.info(
            f"Bank statement imported by {current_user.id} "
            f"for account {account_id} on {statement_date}"
        )
        
        return {
            "status": "success",
            "reconciliation_id": recon_id,
            "statement_date": statement_date,
            "transactions_imported": len(transactions or []),
            "next_step": "Run auto-matching or manually match transactions",
        }
    except BankReconciliationError as e:
        logger.warning(f"Error importing bank statement: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error in statement import: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to import bank statement")


# ==================== Automatic Matching ====================

@router.post("/auto-match/{reconciliation_id}", response_model=dict)
async def auto_match_transactions(
    reconciliation_id: str,
    body: AutoMatchRequest = AutoMatchRequest(),
    current_user: User = Depends(require_permission("finance.bank_reconciliation.manage")),
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    matching_window_days = body.matching_window_days
    """Automatically match bank transactions to GL entries
    
    Uses intelligent matching rules:
    1. Exact match: Same amount + date
    2. Within window: Same amount, within 5 days
    3. Outstanding: GL entry after bank date (not cleared)
    4. In transit: Bank deposit before GL entry (not yet recorded)
    
    Args:
        reconciliation_id: BankReconciliation ID
        matching_window_days: Days to match within
        school_id: School identifier
        session: Database session
        
    Returns:
        Matching summary (matched, unmatched_bank, unmatched_gl counts)
    """
    try:
        service = BankReconciliationService(session)
        result = await service.auto_match_transactions(
            school_id=school_id,
            reconciliation_id=reconciliation_id,
            matching_window_days=matching_window_days,
        )
        
        return {
            "status": "success",
            "reconciliation_id": reconciliation_id,
            "matched_transactions": result.get("matched"),
            "unmatched_bank_items": result.get("unmatched_bank"),
            "unmatched_gl_items": result.get("unmatched_gl"),
            "next_step": "Review unmatched items and calculate variance",
        }
    except BankReconciliationError as e:
        logger.warning(f"Error in auto-matching: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error in auto-match process: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to auto-match transactions")


# ==================== Manual Matching ====================

@router.post("/match/{bank_statement_id}/{journal_entry_id}", response_model=dict)
async def manually_match_transaction(
    bank_statement_id: str,
    journal_entry_id: str,
    body: ManualMatchRequest = ManualMatchRequest(),
    current_user: User = Depends(require_permission("finance.bank_reconciliation.manage")),
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    variance_reason = body.variance_reason
    """Manually match a bank transaction to GL entry
    
    Used for transactions that couldn't be auto-matched and require
    manual review (e.g., amount variance, timing differences).
    
    Args:
        bank_statement_id: Bank statement item ID
        journal_entry_id: Journal entry ID to match to
        variance_reason: Reason for any amount difference
        current_user: Current user
        school_id: School identifier
        session: Database session
        
    Returns:
        Match result with variance details
    """
    try:
        service = BankReconciliationService(session)
        match = await service.manually_match_transaction(
            school_id=school_id,
            bank_statement_id=bank_statement_id,
            journal_entry_id=journal_entry_id,
            matched_by=current_user.id,
            variance_reason=variance_reason,
        )
        
        return {
            "status": "success",
            "match_id": match.id,
            "match_status": match.match_status.value,
            "bank_amount": match.bank_amount,
            "gl_amount": match.gl_amount,
            "variance": match.variance_amount,
            "variance_reason": variance_reason,
        }
    except BankReconciliationError as e:
        logger.warning(f"Error in manual matching: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error in manual match: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to manually match transaction")


# ==================== Analysis & Reporting ====================

@router.get("/variance/{reconciliation_id}", response_model=dict)
async def calculate_variance(
    reconciliation_id: str,
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Calculate variance between GL balance and bank balance
    
    Bank Balance - GL Balance should = 0 if reconciled
    
    Args:
        reconciliation_id: BankReconciliation ID
        school_id: School identifier
        session: Database session
        
    Returns:
        Variance analysis showing if balanced
    """
    try:
        service = BankReconciliationService(session)
        result = await service.calculate_variance(school_id, reconciliation_id)
        
        return {
            "reconciliation_id": reconciliation_id,
            "bank_balance": result["bank_balance"],
            "gl_balance": result["gl_balance"],
            "variance": result["variance"],
            "is_balanced": result["is_balanced"],
            "status": "BALANCED" if result["is_balanced"] else "UNBALANCED",
            "next_step": "Reconciliation ready to complete" if result["is_balanced"] else "Review unmatched items",
        }
    except Exception as e:
        logger.error(f"Error calculating variance: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to calculate variance")


@router.get("/reconciling-items/{reconciliation_id}", response_model=dict)
async def get_reconciling_items(
    reconciliation_id: str,
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Get reconciling items (outstanding checks, deposits in transit)
    
    These are timing differences that explain GL vs bank variance.
    
    Args:
        reconciliation_id: BankReconciliation ID
        school_id: School identifier
        session: Database session
        
    Returns:
        List of reconciling items by type
    """
    try:
        service = BankReconciliationService(session)
        result = await service.calculate_reconciling_items(school_id, reconciliation_id)
        
        return {
            "reconciliation_id": reconciliation_id,
            "outstanding_checks": result["outstanding_checks"],
            "deposits_in_transit": result["deposits_in_transit"],
            "bank_fees": result["bank_fees"],
            "total_reconciling_items": result["total_reconciling_items"],
        }
    except BankReconciliationError as e:
        logger.warning(f"Error getting reconciling items: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error in reconciling items calculation: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to calculate reconciling items")


@router.post("/reconciling-items/{reconciliation_id}/create-adjustments", response_model=dict)
async def create_reconciling_adjustments(
    reconciliation_id: str,
    current_user: User = Depends(require_permission("finance.bank_reconciliation.manage")),
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Create GL adjustment records for unmatched bank fee/interest items

    Persists a BankReconciliationAdjustment (unposted) for each unmatched bank
    statement line flagged as a FEE or INTEREST transaction — the step that
    calculate_reconciling_items only ever summarized without recording.
    Safe to call more than once: items already adjusted are skipped.

    Args:
        reconciliation_id: BankReconciliation ID
        current_user: Current user (creator of the adjustments)
        school_id: School identifier
        session: Database session

    Returns:
        List of newly created adjustments (empty if there was nothing new)
    """
    try:
        service = BankReconciliationService(session)
        adjustments = await service.create_reconciliation_adjustments(
            school_id=school_id,
            reconciliation_id=reconciliation_id,
            created_by=current_user.id,
        )

        return {
            "status": "success",
            "reconciliation_id": reconciliation_id,
            "adjustments_created": len(adjustments),
            "adjustments": adjustments,
        }
    except BankReconciliationError as e:
        logger.warning(f"Error creating reconciliation adjustments: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error creating reconciliation adjustments: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to create reconciliation adjustments")


@router.get("/summary/{reconciliation_id}", response_model=dict)
async def get_reconciliation_summary(
    reconciliation_id: str,
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Get complete reconciliation summary
    
    Returns comprehensive reconciliation report with:
    - Bank vs GL balances
    - Matched/unmatched counts
    - Variance analysis
    - Reconciling items
    - Status and approval info
    
    Args:
        reconciliation_id: BankReconciliation ID
        school_id: School identifier
        session: Database session
        
    Returns:
        Complete reconciliation summary
    """
    try:
        service = BankReconciliationService(session)
        return await service.get_reconciliation_summary(school_id, reconciliation_id)
    except BankReconciliationError as e:
        logger.warning(f"Error getting reconciliation summary: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error in summary generation: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to get reconciliation summary")


# ==================== Reconciliation Completion ====================

@router.post("/complete/{reconciliation_id}", response_model=dict)
async def complete_reconciliation(
    reconciliation_id: str,
    current_user: User = Depends(require_permission("finance.bank_reconciliation.manage")),
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Mark reconciliation as completed and approved
    
    **CRITICAL**: Can only complete if variance is zero (or < $0.01).
    
    Args:
        reconciliation_id: BankReconciliation ID to complete
        current_user: Current user (becomes approver)
        school_id: School identifier
        session: Database session
        
    Returns:
        Completion confirmation with approval details
        
    Raises:
        HTTPException 400: If reconciliation not balanced
    """
    try:
        service = BankReconciliationService(session)
        result = await service.complete_reconciliation(
            school_id=school_id,
            reconciliation_id=reconciliation_id,
            approved_by=current_user.id,
        )

        logger.info(
            f"Bank reconciliation {reconciliation_id} completed and approved by "
            f"{current_user.id}"
        )

        await _log_gl_audit(
            session, school_id, reconciliation_id, AuditActionType.RECONCILIATION_COMPLETED, current_user,
            new_values={"completed_date": str(result["completed_date"]), "approved_by": result["approved_by"]},
        )

        return {
            "status": "success",
            "reconciliation_id": reconciliation_id,
            "completed_date": result["completed_date"],
            "approved_by": result["approved_by"],
            "message": "Reconciliation completed and approved",
        }
    except BankReconciliationError as e:
        logger.warning(f"Error completing reconciliation: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error in reconciliation completion: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to complete reconciliation")
