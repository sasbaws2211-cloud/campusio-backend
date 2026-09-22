"""Finance Reports router - Schools view payment data"""
import logging
from datetime import datetime, timedelta
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func, and_, or_
from sqlmodel import select
from pydantic import BaseModel

from database import get_session
from auth import get_current_user
from models.user import User, UserRole
from models.payment import OnlineTransaction, TransactionStatus, PaymentVerification
from models.finance import GLAccount, JournalLineItem, JournalEntry, PostingStatus, AccountType
from models.fee import Fee, FeeStructure, PaymentStatus as FeeStatus
from models.student import Student
from services.finance_dashboard_service import compute_financial_snapshot

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/finance", tags=["Finance Reports"])


# ============================================================================
# DASHBOARD ENDPOINTS (NEW)
# ============================================================================

@router.get("/dashboard-metrics", status_code=200)
async def get_dashboard_metrics(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
) -> dict:
    """
    Get financial dashboard metrics summary.
    
    Calculates metrics from posted journal entries to GL accounts.
    Uses proper accounting balance calculations:
    - Assets/Expenses: Debit - Credit
    - Liabilities/Equity/Revenue: Credit - Debit
    
    **Auth Required:** Authenticated users (finance staff, admin, principal)
    
    **Returns:**
    ```json
    {
        "cashBalance": 50000.00,
        "accountsReceivable": 25000.00,
        "totalAssets": 150000.00,
        "accountsPayable": 15000.00,
        "netProfit": 35000.00,
        "profitMargin": 23.3,
        "revenueBySource": [
            {
                "name": "Tuition Fees",
                "amount": 100000.00,
                "percentage": 66.7
            }
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

    # GL-balance calculation lives in services/finance_dashboard_service.py —
    # the same function backs routers/dashboard.py's real-time overview, so
    # there's exactly one implementation of this math, not two. That service
    # never raises (returns an all-zero snapshot on any error), which is why
    # this endpoint no longer needs its own try/except around the calculation.
    snapshot = await compute_financial_snapshot(session, school_id)
    return {
        "cashBalance": snapshot["cash_balance"],
        "accountsReceivable": snapshot["accounts_receivable"],
        "totalAssets": snapshot["total_assets"],
        "accountsPayable": snapshot["accounts_payable"],
        "netProfit": snapshot["net_profit"],
        "profitMargin": snapshot["profit_margin"],
        "revenueBySource": snapshot["revenue_by_source"],
    }


@router.get("/recent-transactions", status_code=200)
async def get_recent_transactions(
    limit: int = Query(4, ge=1, le=50),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
) -> dict:
    """
    Get recent financial transactions.
    
    **Auth Required:** Authenticated users (finance staff, admin, principal)
    
    **Query Parameters:**
    - limit: Number of transactions to return (max 50)
    
    **Returns:**
    ```json
    {
        "transactions": [
            {
                "id": "txn-001",
                "description": "Tuition Payment - John Doe",
                "amount": 500.00,
                "date": "2026-04-08T10:30:00",
                "type": "payment"
            }
        ]
    }
    ```
    """
    try:
        school_id = current_user.school_id
        if not school_id:
            return {"transactions": []}
        
        # Get recent online transactions
        from models.payment import OnlineTransaction, TransactionStatus
        from models.payment import TransactionType
        from sqlalchemy import cast, String
        
        query = select(
            OnlineTransaction.id,
            OnlineTransaction.reference,
            OnlineTransaction.amount,
            OnlineTransaction.completed_at,
            OnlineTransaction.status
        ).where(
            OnlineTransaction.school_id == school_id,
            OnlineTransaction.status == TransactionStatus.SUCCESS
        ).order_by(
            OnlineTransaction.completed_at.desc()
        ).limit(limit)
        
        results = await session.execute(query)
        transactions_data = results.all()
        
        transactions = []
        for txn_id, reference, amount, completed_at, status in transactions_data:
            transactions.append({
                "id": str(txn_id),
                "description": f"Payment - {reference or 'Online Payment'}",
                "amount": float(amount or 0),
                "date": completed_at.isoformat() if completed_at else datetime.utcnow().isoformat(),
                "type": "payment"
            })
        
        return {"transactions": transactions}
    
    except Exception as e:
        logger.error(f"Error fetching recent transactions: {str(e)}")
        return {"transactions": []}


@router.get("/health-indicators", status_code=200)
async def get_health_indicators(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
) -> dict:
    """
    Get financial health indicators.
    
    **Auth Required:** Authenticated users (finance staff, admin, principal)
    
    **Returns:**
    ```json
    {
        "indicators": [
            {
                "label": "Current Ratio",
                "value": "2.5",
                "status": "healthy"
            },
            {
                "label": "Debt-to-Equity",
                "value": "0.5",
                "status": "healthy"
            }
        ]
    }
    ```
    """
    try:
        school_id = current_user.school_id
        if not school_id:
            return {"indicators": []}
        
        # Import models
        from models.finance import GLAccount, JournalLineItem, JournalEntry, PostingStatus, AccountType
        
        # Get all GL accounts for this school
        account_query = select(GLAccount).where(
            GLAccount.school_id == school_id,
            GLAccount.is_active == True
        )
        account_results = await session.execute(account_query)
        gl_accounts = {acc.id: acc for acc in account_results.scalars().all()}
        
        if not gl_accounts:
            return {"indicators": []}
        
        # Get all posted journal entries. POSTED *and* REVERSED, not POSTED
        # alone: see get_dashboard_metrics for why.
        posted_entries_query = select(JournalEntry.id).where(
            JournalEntry.school_id == school_id,
            JournalEntry.posting_status.in_([PostingStatus.POSTED, PostingStatus.REVERSED])
        )
        posted_entries = await session.execute(posted_entries_query)
        posted_entry_ids = [e for e in posted_entries.scalars().all()]
        
        if not posted_entry_ids:
            return {"indicators": []}
        
        # Get line items aggregated by account
        line_items_query = select(
            JournalLineItem.gl_account_id,
            func.sum(JournalLineItem.debit_amount).label('total_debit'),
            func.sum(JournalLineItem.credit_amount).label('total_credit')
        ).where(
            JournalLineItem.journal_entry_id.in_(posted_entry_ids)
        ).group_by(JournalLineItem.gl_account_id)
        
        line_items_results = await session.execute(line_items_query)
        account_balances = {}
        
        for gl_account_id, total_debit, total_credit in line_items_results.all():
            account_balances[gl_account_id] = {
                'debit': total_debit or 0,
                'credit': total_credit or 0
            }
        
        # Calculate key metrics
        current_assets = 0
        current_liabilities = 0
        total_assets = 0
        total_liabilities = 0
        total_equity = 0
        inventory = 0
        revenue = 0
        expenses = 0

        for account_id, account in gl_accounts.items():
            balance_data = account_balances.get(account_id, {'debit': 0, 'credit': 0})
            debit = balance_data['debit']
            credit = balance_data['credit']

            if account.account_type == AccountType.ASSET:
                balance = debit - credit
                total_assets += balance
                if "current" in account.account_name.lower() or any(x in account.account_name.lower() for x in ["cash", "receivable", "inventory"]):
                    current_assets += balance
                if "inventory" in account.account_name.lower():
                    inventory += balance

            elif account.account_type == AccountType.LIABILITY:
                balance = credit - debit
                total_liabilities += balance
                if "current" in account.account_name.lower() or "payable" in account.account_name.lower():
                    current_liabilities += balance

            elif account.account_type == AccountType.EQUITY:
                balance = credit - debit
                total_equity += balance

            elif account.account_type == AccountType.REVENUE:
                revenue += credit - debit

            elif account.account_type == AccountType.EXPENSE:
                expenses += debit - credit

        net_profit = revenue - expenses

        # Calculate ratios — all from the same account-balance aggregation
        # above, no additional queries needed.
        current_ratio = (current_assets / current_liabilities) if current_liabilities > 0 else 0
        quick_ratio = ((current_assets - inventory) / current_liabilities) if current_liabilities > 0 else 0
        working_capital = current_assets - current_liabilities
        debt_to_equity = (total_liabilities / total_equity) if total_equity > 0 else 0
        debt_ratio = (total_liabilities / total_assets) if total_assets > 0 else 0
        net_profit_margin = (net_profit / revenue * 100) if revenue > 0 else 0
        return_on_assets = (net_profit / total_assets * 100) if total_assets > 0 else 0
        return_on_equity = (net_profit / total_equity * 100) if total_equity > 0 else 0
        asset_turnover = (revenue / total_assets) if total_assets > 0 else 0

        # Determine health status
        def get_status(value, metric):
            if metric == "current_ratio":
                return "healthy" if value >= 1.5 else "warning" if value >= 1.0 else "critical"
            elif metric == "quick_ratio":
                return "healthy" if value >= 1.0 else "warning" if value >= 0.5 else "critical"
            elif metric == "working_capital":
                return "healthy" if value >= 0 else "critical"
            elif metric == "debt_to_equity":
                return "healthy" if value <= 0.5 else "warning" if value <= 1.0 else "critical"
            elif metric == "debt_ratio":
                return "healthy" if value <= 0.4 else "warning" if value <= 0.6 else "critical"
            elif metric == "net_profit_margin":
                return "healthy" if value >= 10 else "warning" if value >= 0 else "critical"
            elif metric == "roa":
                return "healthy" if value >= 5 else "warning" if value >= 0 else "critical"
            elif metric == "roe":
                return "healthy" if value >= 10 else "warning" if value >= 0 else "critical"
            return "normal"

        indicators = [
            {
                "label": "Current Ratio",
                "value": f"{current_ratio:.2f}",
                "status": get_status(current_ratio, "current_ratio")
            },
            {
                "label": "Quick Ratio",
                "value": f"{quick_ratio:.2f}",
                "status": get_status(quick_ratio, "quick_ratio")
            },
            {
                "label": "Working Capital",
                "value": f"{working_capital:,.2f}",
                "status": get_status(working_capital, "working_capital")
            },
            {
                "label": "Debt-to-Equity",
                "value": f"{debt_to_equity:.2f}",
                "status": get_status(debt_to_equity, "debt_to_equity")
            },
            {
                "label": "Debt Ratio",
                "value": f"{debt_ratio:.2f}",
                "status": get_status(debt_ratio, "debt_ratio")
            },
            {
                "label": "Net Profit Margin",
                "value": f"{net_profit_margin:.1f}%",
                "status": get_status(net_profit_margin, "net_profit_margin")
            },
            {
                "label": "Return on Assets (ROA)",
                "value": f"{return_on_assets:.1f}%",
                "status": get_status(return_on_assets, "roa")
            },
            {
                "label": "Return on Equity (ROE)",
                "value": f"{return_on_equity:.1f}%",
                "status": get_status(return_on_equity, "roe")
            },
            {
                "label": "Asset Turnover",
                "value": f"{asset_turnover:.2f}",
                "status": "normal"
            }
        ]

        return {"indicators": indicators}
    
    except Exception as e:
        logger.error(f"Error fetching health indicators: {str(e)}")
        return {"indicators": []}


# ============================================================================
# RESPONSE MODELS
# ============================================================================

class PaymentSummary(BaseModel):
    """Summary of a payment"""
    transaction_id: str
    reference: str
    student_name: str
    parent_name: str
    parent_email: str
    parent_phone: str
    fee_type: str
    amount: float
    status: str
    initiated_at: datetime
    completed_at: Optional[datetime] = None
    verified_at: Optional[datetime] = None


class PaymentStatistics(BaseModel):
    """Payment statistics for dashboard"""
    total_transactions: int
    successful_payments: int
    failed_payments: int
    pending_payments: int
    total_amount_collected: float
    total_amount_pending: float
    period_start: datetime
    period_end: datetime


class ParentPaymentHistory(BaseModel):
    """Payment history for a specific parent"""
    parent_id: str
    parent_name: str
    parent_email: str
    total_payments: int
    total_amount_paid: float
    last_payment_date: Optional[datetime]
    transactions: List[PaymentSummary]


# ============================================================================
# AUTHENTICATION & AUTHORIZATION
# ============================================================================

async def verify_school_financial_access(
    current_user: User = Depends(get_current_user)
) -> str:
    """
    Verify user has access to school financial data.
    Only admins and HR (who handle payroll/finance) can view.
    """
    allowed_roles = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.HR)

    if current_user.role not in allowed_roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Role '{current_user.role}' cannot access financial reports"
        )

    return current_user.school_id


# ============================================================================
# ENDPOINTS
# ============================================================================

@router.get("/payments", status_code=200)
async def get_payments(
    skip: int = Query(0, gte=0),
    limit: int = Query(50, gte=1, le=500),
    status_filter: Optional[str] = Query(None),  # pending, success, failed, all
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(None),
    search: Optional[str] = Query(None),  # Search by parent name/email/student name
    school_id: str = Depends(verify_school_financial_access),
    session: AsyncSession = Depends(get_session)
) -> dict:
    """
    Get list of online payments for the school.
    
    **Auth Required:** Admin, Accountant, Finance Officer
    
    **Query Parameters:**
    - skip: Number of records to skip (pagination)
    - limit: Number of records to return (max 500)
    - status_filter: Filter by status (pending, success, failed, all)
    - start_date: Filter payments from this date
    - end_date: Filter payments to this date
    - search: Search by parent name, email, or student name
    
    **Returns:**
    ```json
    {
        "total": 150,
        "skip": 0,
        "limit": 50,
        "payments": [
            {
                "transaction_id": "txn-001",
                "reference": "ref-xyz",
                "student_name": "John Doe",
                "parent_name": "Jane Doe",
                "parent_email": "jane@example.com",
                "parent_phone": "+233123456789",
                "fee_type": "Tuition",
                "amount": 500.00,
                "status": "success",
                "initiated_at": "2026-04-08T10:30:00",
                "completed_at": "2026-04-08T10:35:00",
                "verified_at": "2026-04-08T10:35:30"
            }
        ]
    }
    ```
    """
    try:
        # Build query
        query = select(
            OnlineTransaction.id,
            OnlineTransaction.reference,
            Student.name,
            User.first_name,
            User.last_name,
            User.email,
            User.phone,
            Fee.fee_type,
            OnlineTransaction.amount,
            OnlineTransaction.status,
            OnlineTransaction.initiated_at,
            OnlineTransaction.completed_at,
            OnlineTransaction.verified_at
        ).join(
            Fee, OnlineTransaction.fee_id == Fee.id
        ).join(
            Student, OnlineTransaction.student_id == Student.id
        ).join(
            User, OnlineTransaction.parent_id == User.id
        ).where(
            OnlineTransaction.school_id == school_id
        )
        
        # Status filter
        if status_filter and status_filter != 'all':
            query = query.where(OnlineTransaction.status == status_filter)
        
        # Date range filter
        if start_date:
            query = query.where(OnlineTransaction.initiated_at >= start_date)
        if end_date:
            # Add 1 day to include full end date
            end_date_inclusive = end_date + timedelta(days=1)
            query = query.where(OnlineTransaction.initiated_at < end_date_inclusive)
        
        # Search filter
        if search:
            search_pattern = f"%{search}%"
            query = query.where(
                or_(
                    Student.name.ilike(search_pattern),
                    User.first_name.ilike(search_pattern),
                    User.email.ilike(search_pattern)
                )
            )
        
        # Get total count
        count_query = select(func.count()).select_from(OnlineTransaction).where(
            OnlineTransaction.school_id == school_id
        )
        if status_filter and status_filter != 'all':
            count_query = count_query.where(OnlineTransaction.status == status_filter)
        if start_date:
            count_query = count_query.where(OnlineTransaction.initiated_at >= start_date)
        if end_date:
            end_date_inclusive = end_date + timedelta(days=1)
            count_query = count_query.where(OnlineTransaction.initiated_at < end_date_inclusive)
        
        total = await session.exec(count_query)
        total = total.one()
        
        # Execute query with pagination
        query = query.order_by(OnlineTransaction.initiated_at.desc()).offset(skip).limit(limit)
        results = await session.exec(query)
        payments = results.all()
        
        return {
            "total": total,
            "skip": skip,
            "limit": limit,
            "payments": [
                {
                    "transaction_id": p[0],
                    "reference": p[1],
                    "student_name": p[2],
                    "parent_name": f"{p[3]} {p[4] or ''}" if p[3] else 'Unknown',
                    "parent_email": p[5],
                    "parent_phone": p[6],
                    "fee_type": p[7],
                    "amount": float(p[8]),
                    "status": p[9],
                    "initiated_at": p[10],
                    "completed_at": p[11],
                    "verified_at": p[12]
                }
                for p in payments
            ]
        }
    
    except Exception as e:
        logger.error(f"Error fetching payments: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error fetching payments: {str(e)}")


@router.get("/payments/statistics", status_code=200)
async def get_payment_statistics(
    period_days: int = Query(30, ge=1, le=365),
    school_id: str = Depends(verify_school_financial_access),
    session: AsyncSession = Depends(get_session)
) -> PaymentStatistics:
    """
    Get payment statistics for the school.
    
    **Auth Required:** Admin, Accountant, Finance Officer
    
    **Query Parameters:**
    - period_days: Number of days to look back (default 30)
    
    **Returns:**
    ```json
    {
        "total_transactions": 150,
        "successful_payments": 120,
        "failed_payments": 15,
        "pending_payments": 15,
        "total_amount_collected": 45000.00,
        "total_amount_pending": 7500.00,
        "period_start": "2026-03-08T00:00:00",
        "period_end": "2026-04-08T00:00:00"
    }
    ```
    """
    try:
        period_start = datetime.utcnow() - timedelta(days=period_days)
        period_end = datetime.utcnow()
        
        # Get totals
        query = select(OnlineTransaction).where(
            and_(
                OnlineTransaction.school_id == school_id,
                OnlineTransaction.initiated_at >= period_start,
                OnlineTransaction.initiated_at <= period_end
            )
        )
        
        results = await session.exec(query)
        transactions = results.all()
        
        total_transactions = len(transactions)
        successful = sum(1 for t in transactions if t.status == TransactionStatus.SUCCESS)
        failed = sum(1 for t in transactions if t.status == TransactionStatus.FAILED)
        pending = sum(1 for t in transactions if t.status in [
            TransactionStatus.PENDING, 
            TransactionStatus.PROCESSING,
            TransactionStatus.WEBHOOK_RECEIVED
        ])
        
        total_collected = sum(
            t.amount_paid for t in transactions 
            if t.status == TransactionStatus.SUCCESS
        )
        total_pending = sum(
            t.amount for t in transactions 
            if t.status in [TransactionStatus.PENDING, TransactionStatus.PROCESSING]
        )
        
        return {
            "total_transactions": total_transactions,
            "successful_payments": successful,
            "failed_payments": failed,
            "pending_payments": pending,
            "total_amount_collected": float(total_collected),
            "total_amount_pending": float(total_pending),
            "period_start": period_start,
            "period_end": period_end
        }
    
    except Exception as e:
        logger.error(f"Error fetching statistics: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error fetching statistics: {str(e)}")


@router.get("/payments/by-parent", status_code=200)
async def get_payments_by_parent(
    skip: int = Query(0, gte=0),
    limit: int = Query(50, gte=1, le=500),
    school_id: str = Depends(verify_school_financial_access),
    session: AsyncSession = Depends(get_session)
) -> dict:
    """
    Get payment history grouped by parent.
    
    **Auth Required:** Admin, Accountant, Finance Officer
    
    **Returns:**
    ```json
    {
        "total": 75,
        "parents": [
            {
                "parent_id": "parent-001",
                "parent_name": "Jane Doe",
                "parent_email": "jane@example.com",
                "total_payments": 5,
                "total_amount_paid": 2500.00,
                "last_payment_date": "2026-04-08T10:30:00",
                "transactions": [...]
            }
        ]
    }
    ```
    """
    try:
        # Get unique parents with their payment summaries
        parent_query = select(
            User.id,
            User.first_name,
            User.email
        ).join(
            OnlineTransaction, User.id == OnlineTransaction.parent_id
        ).where(
            OnlineTransaction.school_id == school_id
        ).distinct().offset(skip).limit(limit)
        
        parent_results = await session.exec(parent_query)
        parents = parent_results.all()
        
        parent_data = []
        for parent_id, parent_first, parent_email in parents:
            # Get transactions for this parent
            txn_query = select(OnlineTransaction).where(
                and_(
                    OnlineTransaction.parent_id == parent_id,
                    OnlineTransaction.school_id == school_id,
                    OnlineTransaction.status == TransactionStatus.SUCCESS
                )
            ).order_by(OnlineTransaction.initiated_at.desc())
            
            txn_results = await session.exec(txn_query)
            transactions = txn_results.all()
            
            total_paid = sum(t.amount_paid for t in transactions)
            last_payment = transactions[0].completed_at if transactions else None
            
            parent_data.append({
                "parent_id": parent_id,
                "parent_name": parent_first,
                "parent_email": parent_email,
                "total_payments": len(transactions),
                "total_amount_paid": float(total_paid),
                "last_payment_date": last_payment,
                "transactions": len(transactions)  # Simplified for listing
            })
        
        return {
            "total": len(parent_data),
            "parents": parent_data
        }
    
    except Exception as e:
        logger.error(f"Error fetching parent payments: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error fetching parent payments: {str(e)}")


@router.get("/payments/by-fee-type", status_code=200)
async def get_payments_by_fee_type(
    school_id: str = Depends(verify_school_financial_access),
    session: AsyncSession = Depends(get_session)
) -> dict:
    """
    Get payment summary grouped by fee type.
    
    **Auth Required:** Admin, Accountant, Finance Officer
    
    **Returns:**
    ```json
    {
        "fee_types": [
            {
                "fee_type": "Tuition",
                "total_due": 150000.00,
                "total_collected": 120000.00,
                "total_pending": 30000.00,
                "collection_rate": 80.0,
                "transaction_count": 45
            }
        ]
    }
    ```
    """
    try:
        # fee_type lives on FeeStructure, not Fee — the previous query
        # selected Fee.fee_type directly, which doesn't exist on that model
        # and raised an AttributeError on every call (caught below and
        # turned into a 500 with no useful detail, so this endpoint has
        # never actually returned data). Joined to FeeStructure to resolve
        # fee_type properly, and grouped straight off Fee rather than
        # through OnlineTransaction — that inner join silently excluded
        # every fee paid by cash, bank transfer, or mobile money
        # (FeePayment, the general payment path most fees actually use).
        # "transaction_count" below is the fee count in this group, not a
        # literal transaction count, matching what the response returns.
        query = select(
            FeeStructure.fee_type,
            func.sum(Fee.amount_due).label('total_due'),
            func.sum(Fee.amount_paid).label('total_paid'),
            func.sum(Fee.discount).label('total_discount'),
            func.count(Fee.id).label('fee_count')
        ).select_from(Fee).join(
            FeeStructure, Fee.fee_structure_id == FeeStructure.id
        ).where(
            Fee.school_id == school_id
        ).group_by(FeeStructure.fee_type)

        results = await session.exec(query)
        fee_types = results.all()

        summary = []
        for fee_type, total_due, total_paid, total_discount, count in fee_types:
            total_due = float(total_due or 0)
            total_paid = float(total_paid or 0)
            total_discount = float(total_discount or 0)
            total_pending = max(0, total_due - total_paid - total_discount)
            net_due = total_due - total_discount
            collection_rate = (total_paid / net_due * 100) if net_due > 0 else 0

            summary.append({
                "fee_type": fee_type,
                "total_due": total_due,
                "total_collected": total_paid,
                "total_pending": total_pending,
                "collection_rate": round(collection_rate, 2),
                "transaction_count": count
            })
        
        return {"fee_types": sorted(summary, key=lambda x: x['total_collected'], reverse=True)}
    
    except Exception as e:
        logger.error(f"Error fetching fee type summary: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error fetching fee type summary: {str(e)}")


@router.get("/payments/reconciliation", status_code=200)
async def get_payment_reconciliation(
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(None),
    school_id: str = Depends(verify_school_financial_access),
    session: AsyncSession = Depends(get_session)
) -> dict:
    """
    Get reconciliation report comparing payments to GL entries.
    
    **Auth Required:** Admin, Accountant, Finance Officer
    
    **Returns:**
    ```json
    {
        "total_transactions": 150,
        "posted_to_gl": 145,
        "pending_gl_posting": 5,
        "discrepancies": [
            {
                "transaction_id": "txn-001",
                "issue": "GL entry not created",
                "amount": 500.00
            }
        ]
    }
    ```
    """
    try:
        if not start_date:
            start_date = datetime.utcnow() - timedelta(days=30)
        if not end_date:
            end_date = datetime.utcnow()
        
        query = select(OnlineTransaction).where(
            and_(
                OnlineTransaction.school_id == school_id,
                OnlineTransaction.status == TransactionStatus.SUCCESS,
                OnlineTransaction.initiated_at >= start_date,
                OnlineTransaction.initiated_at <= end_date
            )
        )
        
        results = await session.exec(query)
        transactions = results.all()
        
        discrepancies = []
        posted_count = 0
        
        for txn in transactions:
            if txn.journal_entry_id:
                posted_count += 1
            else:
                discrepancies.append({
                    "transaction_id": txn.id,
                    "reference": txn.reference,
                    "issue": "GL entry not created",
                    "amount": float(txn.amount_paid),
                    "completed_at": txn.completed_at
                })
        
        return {
            "total_transactions": len(transactions),
            "posted_to_gl": posted_count,
            "pending_gl_posting": len(transactions) - posted_count,
            "discrepancies": discrepancies
        }
    
    except Exception as e:
        logger.error(f"Error fetching reconciliation: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error fetching reconciliation: {str(e)}")


@router.get("/payments/{transaction_id}", status_code=200)
async def get_payment_detail(
    transaction_id: str,
    school_id: str = Depends(verify_school_financial_access),
    session: AsyncSession = Depends(get_session)
) -> dict:
    """
    Get detailed view of a specific payment transaction.
    
    **Auth Required:** Admin, Accountant, Finance Officer
    
    **Path Parameters:**
    - transaction_id: UUID of the transaction
    
    **Returns:**
    ```json
    {
        "transaction": {
            "id": "txn-001",
            "reference": "ref-xyz",
            "student_name": "John Doe",
            "parent_name": "Jane Doe",
            "parent_email": "jane@example.com",
            "fee_type": "Tuition",
            "amount": 500.00,
            "amount_paid": 500.00,
            "status": "success",
            "initiated_at": "2026-04-08T10:30:00",
            "completed_at": "2026-04-08T10:35:00",
            "verified_at": "2026-04-08T10:35:30",
            "journal_entry_id": "jne-001"
        }
    }
    ```
    """
    try:
        query = select(OnlineTransaction).where(
            and_(
                OnlineTransaction.id == transaction_id,
                OnlineTransaction.school_id == school_id
            )
        )
        
        result = await session.exec(query)
        transaction = result.first()
        
        if not transaction:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Transaction not found"
            )
        
        # Get related data
        fee_query = select(Fee).where(Fee.id == transaction.fee_id)
        fee = await session.exec(fee_query)
        fee = fee.first()
        
        student_query = select(Student).where(Student.id == transaction.student_id)
        student = await session.exec(student_query)
        student = student.first()
        
        parent_query = select(User).where(User.id == transaction.parent_id)
        parent = await session.exec(parent_query)
        parent = parent.first()
        
        return {
            "transaction": {
                "id": transaction.id,
                "reference": transaction.reference,
                "student_name": student.name if student else "Unknown",
                "student_id": transaction.student_id,
                "parent_name": f"{parent.first_name} {parent.last_name}" if parent else "Unknown",
                "parent_email": parent.email if parent else "Unknown",
                "parent_phone": parent.phone if parent else "Unknown",
                "fee_type": fee.fee_type if fee else "Unknown",
                "amount": float(transaction.amount),
                "amount_paid": float(transaction.amount_paid),
                "status": transaction.status,
                "gateway": transaction.gateway,
                "initiated_at": transaction.initiated_at,
                "completed_at": transaction.completed_at,
                "verified_at": transaction.verified_at,
                "journal_entry_id": transaction.journal_entry_id,
                "failed_reason": transaction.failed_reason
            }
        }
    
    except Exception as e:
        logger.error(f"Error fetching transaction detail: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error fetching transaction: {str(e)}")


# ============================================================================
# EXPORT ENDPOINTS (CSV/PDF)
# ============================================================================

@router.get("/payments/export/csv", status_code=200)
async def export_payments_csv(
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(None),
    school_id: str = Depends(verify_school_financial_access),
    session: AsyncSession = Depends(get_session)
) -> dict:
    """
    Export payments as CSV. Returns URL to download.
    
    **Auth Required:** Admin, Accountant, Finance Officer
    """
    # Implementation would export to S3 or generate CSV file
    return {
        "message": "Export functionality to be implemented",
        "status": "pending"
    }
