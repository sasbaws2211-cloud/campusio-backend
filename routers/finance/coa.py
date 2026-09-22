"""Chart of Accounts Router - GL account management endpoints

ROLE-BASED ACCESS CONTROL:
- SUPER_ADMIN: Full access to all operations
- SCHOOL_ADMIN: Full access to their school's accounts
- HR: Can read accounts and create new ones (for payroll setup)
- TEACHER/STUDENT/PARENT: Read-only access to active accounts

SCHOOL SCOPING:
- All endpoints enforce school_id scoping for multi-tenancy
- SUPER_ADMIN can access any school; others limited to their school_id
"""
import logging
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional, List

from models.finance import (
    GLAccount,
    GLAccountCreate,
    GLAccountUpdate,
    GLAccountResponse,
    AccountType,
    AccountCategory,
)
from models.finance.gl_audit_log import AuditActionType, AuditEntityType
from models.user import User
from database import get_session
from auth import get_current_user, require_permission
from services.coa_service import CoaService, CoaServiceError
from services.coa_initialization import validate_school_chart_of_accounts
from services.gl_audit_log_service import GLAuditLogService
from services.plan_gating import require_plan_feature

logger = logging.getLogger(__name__)

# Gated at the router level (all endpoints), not per-endpoint like
# routers/integrations.py — same effect, fewer lines across a file this size.
router = APIRouter(
    prefix="/chart-of-accounts", tags=["Finance - Chart of Accounts"],
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
            entity_type=AuditEntityType.GL_ACCOUNT,
            entity_id=entity_id,
            action=action,
            user_id=current_user.id,
            user_name=f"{current_user.first_name} {current_user.last_name}",
            user_role=current_user.role.value,
            old_values=old_values,
            new_values=new_values,
        )
    except Exception as e:
        logger.warning(f"Failed to write GL audit log for account {entity_id}: {e}")


# ==================== Account Creation ====================

@router.post("", response_model=GLAccountResponse, status_code=status.HTTP_201_CREATED)
async def create_account(
    account_data: GLAccountCreate,
    current_user: User = Depends(require_permission("finance.coa.create")),
    session: AsyncSession = Depends(get_session),
):
    """Create a new GL account
    
    **Access:** SUPER_ADMIN, SCHOOL_ADMIN, HR
    
    Required fields:
    - account_code: Unique code (e.g., "1010", "5100")
    - account_name: Descriptive name
    - account_type: One of [asset, liability, equity, revenue, expense]
    - account_category: One of 14 predefined categories
    
    Optional fields:
    - description: Longer explanation of the account
    - normal_balance: "debit" or "credit" (default: "debit")
    - parent_account_id: For hierarchical account structures
    """
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No school context - contact administrator"
        )
    
    service = CoaService(session)
    
    try:
        account = await service.create_account(
            school_id=school_id,
            account_data=account_data,
            created_by=current_user.id,
        )
        await _log_gl_audit(
            session, school_id, account.id, AuditActionType.ACCOUNT_CREATED, current_user,
            new_values={"account_code": account.account_code, "account_name": account.account_name,
                        "account_type": account.account_type.value if hasattr(account.account_type, "value") else account.account_type},
        )
        return GLAccountResponse.model_validate(account)
    except CoaServiceError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error creating account: {str(e)}"
        )


# ==================== Account Retrieval ====================

@router.get("/{account_id}", response_model=GLAccountResponse)
async def get_account(
    account_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Get a specific GL account by ID
    
    **Access:** All authenticated users (read-only for non-admin)
    """
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No school context"
        )
    
    service = CoaService(session)
    account = await service.get_account_by_id(school_id, account_id)
    
    if not account:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Account {account_id} not found"
        )
    
    return GLAccountResponse.model_validate(account)


@router.get("/code/{account_code}", response_model=GLAccountResponse)
async def get_account_by_code(
    account_code: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Get a GL account by account code
    
    **Access:** All authenticated users (read-only for non-admin)
    
    Example: GET /api/finance/coa/code/1010
    Returns the Business Checking Account for the school
    """
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No school context"
        )
    
    service = CoaService(session)
    account = await service.get_account_by_code(school_id, account_code)
    
    if not account:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Account code {account_code} not found"
        )
    
    return GLAccountResponse.model_validate(account)


@router.get("", response_model=List[GLAccountResponse])
async def list_accounts(
    active_only: bool = Query(True, description="Filter to active accounts only"),
    account_type: Optional[str] = Query(None, description="Filter by account type"),
    account_category: Optional[str] = Query(None, description="Filter by category"),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """List all GL accounts with optional filtering
    
    **Access:** All authenticated users
    
    Query parameters:
    - active_only: True/False (default: True)
    - account_type: One of [asset, liability, equity, revenue, expense]
    - account_category: One of 14 predefined categories
    
    Examples:
    - GET /api/finance/coa - All active accounts
    - GET /api/finance/coa?account_type=asset - All active assets
    - GET /api/finance/coa?account_category=bank_accounts - All bank accounts
    - GET /api/finance/coa?active_only=false - All accounts (active + inactive)
    """
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No school context"
        )
    
    # Parse optional filter parameters
    acc_type = None
    if account_type:
        # Allow friendly aliases like BANK -> bank_accounts
        type_mapping = {
            "BANK": "bank_accounts",
            "BANK_ACCOUNTS": "bank_accounts",
            "AR": "accounts_receivable",
            "ACCOUNTS_RECEIVABLE": "accounts_receivable",
            "AP": "accounts_payable",
            "ACCOUNTS_PAYABLE": "accounts_payable",
        }
        
        mapped_value = type_mapping.get(account_type.upper())
        if mapped_value:
            # User passed a category alias, use it as category instead
            if not account_category:
                account_category = mapped_value
        else:
            # Try as account type
            try:
                acc_type = AccountType(account_type.lower())
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid account_type: {account_type}. Use: asset, liability, equity, revenue, expense. Or category aliases: BANK, AR, AP, etc."
                )
    
    acc_category = None
    if account_category:
        try:
            acc_category = AccountCategory(account_category)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid account_category: {account_category}"
            )
    
    service = CoaService(session)
    
    try:
        accounts = await service.get_all_accounts(
            school_id=school_id,
            active_only=active_only,
            account_type=acc_type,
            account_category=acc_category,
        )
        return [GLAccountResponse.model_validate(acc) for acc in accounts]
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error retrieving accounts: {str(e)}"
        )


@router.get("/type/{account_type}", response_model=List[GLAccountResponse])
async def get_accounts_by_type(
    account_type: str,
    active_only: bool = Query(True),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Get all accounts of a specific type
    
    **Access:** All authenticated users
    
    Account types:
    - asset: Bank, receivables, fixed assets
    - liability: Payables, debt, salaries payable
    - equity: Accumulated surplus, retained earnings
    - revenue: Tuition, donations, grants
    - expense: Salaries, utilities, supplies
    
    Example: GET /api/finance/coa/type/expense
    Returns all operating expenses accounts
    """
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No school context"
        )
    
    try:
        account_type_enum = AccountType(account_type)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid account_type: {account_type}"
        )
    
    service = CoaService(session)
    accounts = await service.get_accounts_by_type(
        school_id=school_id,
        account_type=account_type_enum,
        active_only=active_only,
    )
    
    return [GLAccountResponse.model_validate(acc) for acc in accounts]


@router.get("/category/{account_category}", response_model=List[GLAccountResponse])
async def get_accounts_by_category(
    account_category: str,
    active_only: bool = Query(True),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Get all accounts in a specific category
    
    **Access:** All authenticated users
    
    Categories include (examples):
    - bank_accounts: Checking, savings, petty cash
    - accounts_receivable: Student fees, outstanding payments
    - salaries_payable: Staff compensation accruals
    - student_fees: Tuition, exams, sports, ICT, library
    - utilities: Electricity, water, internet
    - supplies: Office, classroom, lab materials
    
    Example: GET /api/finance/coa/category/bank_accounts
    Returns all bank accounts (checking, savings, petty cash)
    """
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No school context"
        )
    
    try:
        category_enum = AccountCategory(account_category)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid account_category: {account_category}"
        )
    
    service = CoaService(session)
    accounts = await service.get_accounts_by_category(
        school_id=school_id,
        account_category=category_enum,
        active_only=active_only,
    )
    
    return [GLAccountResponse.model_validate(acc) for acc in accounts]


# ==================== Account Updates ====================

@router.put("/{account_id}", response_model=GLAccountResponse)
async def update_account(
    account_id: str,
    update_data: GLAccountUpdate,
    current_user: User = Depends(require_permission("finance.coa.update")),
    session: AsyncSession = Depends(get_session),
):
    """Update an existing GL account
    
    **Access:** SUPER_ADMIN, SCHOOL_ADMIN only
    
    Can update:
    - account_name: Descriptive name
    - account_category: Category classification
    - description: Longer explanation
    - normal_balance: "debit" or "credit"
    - is_active: Activation status
    
    Note: account_code and account_type cannot be changed after creation
    (this maintains historical integrity in journal entries)
    """
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No school context"
        )
    
    service = CoaService(session)

    # Verify account exists
    account = await service.get_account_by_id(school_id, account_id)
    if not account:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Account {account_id} not found"
        )

    old_values = update_data.model_dump(exclude_unset=True, exclude_none=True)
    old_values = {k: getattr(account, k, None) for k in old_values.keys()}

    try:
        updated_account = await service.update_account(
            school_id=school_id,
            account_id=account_id,
            update_data=update_data,
        )
        await _log_gl_audit(
            session, school_id, account_id, AuditActionType.ACCOUNT_UPDATED, current_user,
            old_values=old_values,
            new_values=update_data.model_dump(exclude_unset=True),
        )
        return GLAccountResponse.model_validate(updated_account)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error updating account: {str(e)}"
        )


# ==================== Account Deactivation ====================

@router.delete("/{account_id}", response_model=dict)
async def deactivate_account(
    account_id: str,
    current_user: User = Depends(require_permission("finance.coa.delete")),
    session: AsyncSession = Depends(get_session),
):
    """Deactivate (soft delete) a GL account
    
    **Access:** SUPER_ADMIN, SCHOOL_ADMIN only
    
    Note: Accounts are deactivated, not permanently deleted, to preserve:
    - Historical integrity of journal entries
    - Audit trail of all transactions
    - Data consistency across the system
    
    Deactivated accounts cannot be used for new transactions but remain
    visible in reports for historical reference.
    """
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No school context"
        )
    
    service = CoaService(session)
    
    # Verify account exists
    account = await service.get_account_by_id(school_id, account_id)
    if not account:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Account {account_id} not found"
        )
    
    try:
        updated_account = await service.deactivate_account(school_id, account_id)
    except CoaServiceError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    await _log_gl_audit(
        session, school_id, account_id, AuditActionType.ACCOUNT_DEACTIVATED, current_user,
        old_values={"is_active": True},
        new_values={"account_code": account.account_code, "is_active": False},
    )

    return {
        "message": f"Account {account.account_code} deactivated",
        "account_id": account_id,
        "is_active": updated_account.is_active,
    }


# ==================== Balance Operations ====================

@router.get("/{account_id}/balance", response_model=dict)
async def get_account_balance(
    account_id: str,
    use_cached: bool = Query(True, description="Use the denormalized balance; False recalculates from journal entries"),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Get the current balance of a GL account

    **Access:** All authenticated users

    Query parameters:
    - use_cached: True (default) returns the fast denormalized current_balance;
      False recalculates it from posted journal entries for verification.
    """
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No school context"
        )

    service = CoaService(session)
    account = await service.get_account_by_id(school_id, account_id)
    if not account:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Account {account_id} not found"
        )

    balance = await service.get_account_balance(school_id, account_id, use_cached=use_cached)

    return {
        "account_id": account_id,
        "account_code": account.account_code,
        "account_name": account.account_name,
        "balance": balance,
        "use_cached": use_cached,
    }


@router.post("/recalculate-balances", response_model=dict)
async def recalculate_all_balances(
    current_user: User = Depends(require_permission("finance.coa.manage")),
    session: AsyncSession = Depends(get_session),
):
    """Recalculate every GL account's balance from posted journal entries

    **Access:** SUPER_ADMIN, SCHOOL_ADMIN only

    Heavy operation intended for audit/correction procedures, not routine use —
    sums all posted journal-entry activity per account and overwrites the
    denormalized current_balance. Run during low-traffic periods.
    """
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No school context"
        )

    service = CoaService(session)
    summary = await service.recalculate_all_balances(school_id)

    if "error" in summary:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=summary["error"],
        )

    return summary


# ==================== Account Analysis & Summary ====================

@router.get("/summary/stats", response_model=dict)
async def get_account_summary(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Get Chart of Accounts summary statistics
    
    **Access:** All authenticated users
    
    Returns:
    - total_accounts: Total active accounts
    - by_type: Count of accounts by type (asset, liability, etc.)
    - by_category: Count of accounts by category
    
    Useful for dashboards and analytics
    """
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No school context"
        )
    
    service = CoaService(session)
    summary = await service.get_account_balance_summary(school_id)
    
    return summary


@router.post("/validate", response_model=dict)
async def validate_accounts(
    current_user: User = Depends(require_permission("finance.coa.manage")),
    session: AsyncSession = Depends(get_session),
):
    """Validate that required GL accounts are configured
    
    **Access:** SUPER_ADMIN, SCHOOL_ADMIN only
    
    Checks that critical accounts exist:
    - Bank accounts (1010)
    - Accounts receivable (1100)
    - Salaries payable (2100)
    - Student fees revenue (4100)
    - Salaries expense (5100)
    
    Returns validation results with any missing or recommended accounts.
    """
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No school context"
        )
    
    validation = await validate_school_chart_of_accounts(session, school_id)
    
    if not validation.get("is_valid"):
        return {
            "status": "invalid",
            "is_valid": False,
            "missing_accounts": validation.get("missing_accounts", []),
            "warnings": validation.get("warnings", []),
        }
    
    return {
        "status": "valid",
        "is_valid": True,
        "message": "All required GL accounts are configured",
        "warnings": validation.get("warnings", []),
    }
