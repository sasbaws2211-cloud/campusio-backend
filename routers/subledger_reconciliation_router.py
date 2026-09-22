"""API Router for Sub-Ledger Reconciliation

Endpoints for:
- Sub-ledger reconciliation creation
- Detail record import
- Automatic GL matching
- Aging analysis
- Variance reporting
- Reconciliation approval
"""
import logging
from typing import Optional, List
from sqlmodel import SQLModel
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime

from services.subledger_reconciliation_service import (
    SubLedgerReconciliationService,
    SubLedgerReconciliationError,
)
from models.finance.subledger_reconciliation import (
    SubLedgerDetailCreate,
    SubLedgerType,
)
from dependencies import get_current_school_id
from auth import get_current_user, require_roles
from database import get_session
from models.user import User, UserRole

# Creating/matching/approving sub-ledger reconciliations mutates GL-adjacent
# records — same role gate as journal.py's posting endpoints.
FINANCE_ADMIN_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.HR)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/subledger-reconciliation", tags=["Sub-Ledger Reconciliation"])


class CreateSubledgerReconciliationRequest(SQLModel):
    subledger_type: SubLedgerType
    control_account_id: str
    detail_records: Optional[List[SubLedgerDetailCreate]] = None
    notes: Optional[str] = None



# ==================== Reconciliation Creation ====================

@router.post("/create", response_model=dict)
async def create_subledger_reconciliation(
    body: CreateSubledgerReconciliationRequest,
    current_user: User = Depends(require_roles(*FINANCE_ADMIN_ROLES)),
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    subledger_type = body.subledger_type
    control_account_id = body.control_account_id
    detail_records = body.detail_records
    notes = body.notes
    """Create a sub-ledger reconciliation and import detail records
    
    Creates a reconciliation record and imports all detail account records
    (e.g., individual student AR, vendor AP accounts).
    
    Args:
        subledger_type: Type of sub-ledger (ACCOUNTS_RECEIVABLE, ACCOUNTS_PAYABLE, etc.)
        control_account_id: GL control account ID
        detail_records: List of detail records with balances
        notes: Optional notes
        current_user: Current user
        school_id: School identifier
        session: Database session
        
    Returns:
        Reconciliation ID and import summary
        
    Raises:
        HTTPException 400: If creation fails
    """
    try:
        service = SubLedgerReconciliationService(session)
        
        recon_id = await service.create_subledger_reconciliation(
            school_id=school_id,
            subledger_type=subledger_type,
            control_account_id=control_account_id,
            detail_records=detail_records or [],
            reconciled_by=current_user.id,
            notes=notes,
        )

        # Calculate totals
        detail_total = sum(r.detail_balance for r in (detail_records or []))

        logger.info(
            f"Sub-ledger reconciliation created by {current_user.id} "
            f"({subledger_type.value}) with {len(detail_records or [])} detail records"
        )
        
        return {
            "status": "success",
            "reconciliation_id": recon_id,
            "subledger_type": subledger_type.value,
            "detail_records_imported": len(detail_records or []),
            "detail_total": detail_total,
            "next_step": "Run auto-matching",
        }
    except SubLedgerReconciliationError as e:
        logger.warning(f"Error creating reconciliation: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error in reconciliation creation: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to create reconciliation")


class AutoGenerateSubledgerReconciliationRequest(SQLModel):
    subledger_type: SubLedgerType
    control_account_id: str
    notes: Optional[str] = None


@router.post("/auto-generate", response_model=dict)
async def auto_generate_subledger_reconciliation(
    body: AutoGenerateSubledgerReconciliationRequest,
    current_user: User = Depends(require_roles(*FINANCE_ADMIN_ROLES)),
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Create a sub-ledger reconciliation with detail records auto-derived
    from the actual Fee (for ACCOUNTS_RECEIVABLE) or Expense (for
    ACCOUNTS_PAYABLE) tables, instead of requiring every student/vendor
    balance to be hand-keyed via /create's detail_records list — that
    manual path still exists for HOSTEL_DEPOSITS/EMPLOYEE_ADVANCES/OTHER,
    which have no single source table this can assume.
    """
    try:
        service = SubLedgerReconciliationService(session)
        detail_records = await service.build_detail_records_from_subledger(
            school_id=school_id, subledger_type=body.subledger_type,
        )

        recon_id = await service.create_subledger_reconciliation(
            school_id=school_id,
            subledger_type=body.subledger_type,
            control_account_id=body.control_account_id,
            detail_records=detail_records,
            reconciled_by=current_user.id,
            notes=body.notes,
        )

        detail_total = sum(r.detail_balance for r in detail_records)

        logger.info(
            f"Auto-generated sub-ledger reconciliation by {current_user.id} "
            f"({body.subledger_type.value}) with {len(detail_records)} detail records"
        )

        return {
            "status": "success",
            "reconciliation_id": recon_id,
            "subledger_type": body.subledger_type.value,
            "detail_records_imported": len(detail_records),
            "detail_total": detail_total,
            "next_step": "Run auto-matching",
        }
    except SubLedgerReconciliationError as e:
        logger.warning(f"Error auto-generating reconciliation: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error in auto-generate reconciliation: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to auto-generate reconciliation")


# ==================== Automatic Matching ====================

@router.post("/auto-match/{reconciliation_id}", response_model=dict)
async def auto_match_details(
    reconciliation_id: str,
    current_user: User = Depends(require_roles(*FINANCE_ADMIN_ROLES)),
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Automatically match detail records to GL postings
    
    Uses matching strategy:
    1. Exact match: Same amount + date
    2. Close match: Amount within $1.00, date within 5 days
    3. Unmatched: No GL posting found
    
    Args:
        reconciliation_id: SubLedgerReconciliation ID
        school_id: School identifier
        session: Database session
        
    Returns:
        Matching summary (matched, variance, unmatched counts)
    """
    try:
        service = SubLedgerReconciliationService(session)
        result = await service.auto_match_details(school_id, reconciliation_id)
        
        total = result["total"]
        matched = result["matched"]
        match_rate = (matched / total * 100) if total > 0 else 0
        
        return {
            "status": "success",
            "reconciliation_id": reconciliation_id,
            "matched_records": matched,
            "variance_records": result["variance"],
            "unmatched_records": result["unmatched"],
            "total_records": total,
            "match_rate": f"{match_rate:.1f}%",
            "next_step": "Calculate aging and variance",
        }
    except SubLedgerReconciliationError as e:
        logger.warning(f"Error in auto-matching: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error in auto-match process: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to auto-match details")


# ==================== Aging Analysis ====================

@router.get("/aging/{reconciliation_id}", response_model=dict)
async def get_aging_analysis(
    reconciliation_id: str,
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Get aging analysis for AR/AP reconciliation
    
    Breaks down detail record balances by age:
    - Current: 0-30 days
    - 30-60 days
    - 60-90 days
    - Over 90 days
    
    Args:
        reconciliation_id: SubLedgerReconciliation ID
        school_id: School identifier
        session: Database session
        
    Returns:
        Aging breakdown with balances by bucket
    """
    try:
        service = SubLedgerReconciliationService(session)
        result = await service.calculate_aging_analysis(school_id, reconciliation_id)
        
        return {
            "reconciliation_id": reconciliation_id,
            "current_0_30_days": result["current_0_30_days"],
            "thirty_to_60_days": result["thirty_to_60_days"],
            "sixty_to_90_days": result["sixty_to_90_days"],
            "over_90_days": result["over_90_days"],
            "total_balance": result["total_balance"],
            "current_percentage": (result["current_0_30_days"] / result["total_balance"] * 100) if result["total_balance"] > 0 else 0,
            "aged_percentage": ((result["thirty_to_60_days"] + result["sixty_to_90_days"] + result["over_90_days"]) / result["total_balance"] * 100) if result["total_balance"] > 0 else 0,
        }
    except SubLedgerReconciliationError as e:
        logger.warning(f"Error in aging analysis: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error calculating aging: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to calculate aging")


# ==================== Variance Analysis ====================

@router.get("/variance/{reconciliation_id}", response_model=dict)
async def calculate_variance(
    reconciliation_id: str,
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Calculate variance between detail total and GL control account
    
    Detail Total - GL Control Balance should = 0 if reconciled
    
    Args:
        reconciliation_id: SubLedgerReconciliation ID
        school_id: School identifier
        session: Database session
        
    Returns:
        Variance analysis showing if balanced
    """
    try:
        service = SubLedgerReconciliationService(session)
        result = await service.calculate_variance(school_id, reconciliation_id)
        
        return {
            "reconciliation_id": reconciliation_id,
            "detail_total": result["detail_total"],
            "gl_control_balance": result["gl_control_balance"],
            "variance": result["variance"],
            "is_balanced": result["is_balanced"],
            "status": "BALANCED" if result["is_balanced"] else "UNBALANCED",
            "next_step": "Reconciliation ready to complete" if result["is_balanced"] else "Review unmatched items",
        }
    except SubLedgerReconciliationError as e:
        logger.warning(f"Error calculating variance: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error in variance calculation: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to calculate variance")


@router.get("/unmatched/{reconciliation_id}", response_model=dict)
async def get_unmatched_details(
    reconciliation_id: str,
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Get unmatched detail records
    
    Returns list of detail records that couldn't be matched to GL postings
    or had amount variances.
    
    Args:
        reconciliation_id: SubLedgerReconciliation ID
        school_id: School identifier
        session: Database session
        
    Returns:
        List of unmatched/variance detail records
    """
    try:
        service = SubLedgerReconciliationService(session)
        unmatched = await service.get_unmatched_details(school_id, reconciliation_id)
        
        return {
            "reconciliation_id": reconciliation_id,
            "unmatched_records": unmatched,
            "count": len(unmatched),
        }
    except Exception as e:
        logger.error(f"Error getting unmatched details: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to get unmatched records")


# ==================== Summary & Reporting ====================

@router.get("/summary/{reconciliation_id}", response_model=dict)
async def get_reconciliation_summary(
    reconciliation_id: str,
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Get complete sub-ledger reconciliation summary
    
    Returns comprehensive reconciliation report with:
    - Detail vs GL balances
    - Matched/unmatched counts
    - Variance analysis
    - Aging breakdown
    - Status and approval info
    
    Args:
        reconciliation_id: SubLedgerReconciliation ID
        school_id: School identifier
        session: Database session
        
    Returns:
        Complete reconciliation summary
    """
    try:
        service = SubLedgerReconciliationService(session)
        return await service.get_reconciliation_summary(school_id, reconciliation_id)
    except SubLedgerReconciliationError as e:
        logger.warning(f"Error getting summary: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error in summary generation: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to get reconciliation summary")


# ==================== Reconciliation Completion ====================

@router.post("/complete/{reconciliation_id}", response_model=dict)
async def complete_reconciliation(
    reconciliation_id: str,
    current_user: User = Depends(require_roles(*FINANCE_ADMIN_ROLES)),
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Mark sub-ledger reconciliation as completed and approved
    
    **CRITICAL**: Can only complete if variance is zero (or < $0.01).
    
    Args:
        reconciliation_id: SubLedgerReconciliation ID to complete
        current_user: Current user (becomes approver)
        school_id: School identifier
        session: Database session
        
    Returns:
        Completion confirmation with approval details
        
    Raises:
        HTTPException 400: If reconciliation not balanced
    """
    try:
        service = SubLedgerReconciliationService(session)
        result = await service.complete_reconciliation(
            school_id=school_id,
            reconciliation_id=reconciliation_id,
            approved_by=current_user.id,
        )

        logger.info(
            f"Sub-ledger reconciliation {reconciliation_id} completed and approved by "
            f"{current_user.id}"
        )
        
        return {
            "status": "success",
            "reconciliation_id": reconciliation_id,
            "completed_date": result["completed_date"],
            "approved_by": result["approved_by"],
            "message": "Sub-ledger reconciliation completed and approved",
        }
    except SubLedgerReconciliationError as e:
        logger.warning(f"Error completing reconciliation: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error in reconciliation completion: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to complete reconciliation")
