"""Reports Router - Financial statement generation endpoints

ACCESS CONTROL:
- All authenticated users can VIEW financial reports (read-only)
- Reports are generated from posted GL data only
- SUPER_ADMIN/SCHOOL_ADMIN get full access
- Other roles can view (no reporting modifications exist)

SCHOOL SCOPING:
- All endpoints enforce school_id scoping for multi-tenancy
- All reports automatically scoped to current user's school

REPORT TYPES:
1. Trial Balance - Verification report (debits must equal credits)
2. Balance Sheet - Financial position at point in time (Assets = Liabilities + Equity)
3. Profit & Loss - Income statement for period (Revenue - Expenses = Net Income)
4. Cash Flow - Cash movement for period
"""
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime
from typing import Optional

from models.finance import (
    TrialBalanceReport,
    BalanceSheetReport,
    ProfitLossReport,
    CashFlowReport,
    ReportDateRangeRequest,
    ReportAsOfDateRequest,
)
from models.user import User
from database import get_session
from auth import get_current_user
from services.reports_service import ReportsService, ReportsServiceError
from services.plan_gating import require_plan_feature

router = APIRouter(
    prefix="/finance-reports", tags=["Finance - Reports"],
    dependencies=[Depends(require_plan_feature("finance_advanced"))],
)


# ==================== Metadata Endpoint ====================

@router.get("", response_model=dict)
async def get_reports_info(
    current_user: User = Depends(get_current_user),
):
    """Get available financial reports metadata
    
    **Access:** All authenticated users
    
    Returns:
    - List of available reports
    - Description of each report
    - Required parameters
    - Available date/period options
    
    Use this endpoint to discover what reports are available.
    """
    return {
        "available_reports": [
            {
                "name": "Trial Balance",
                "key": "trial-balance",
                "endpoint": "/api/finance/reports/trial-balance",
                "method": "GET",
                "description": "Verification report - all GL accounts with debit/credit balances",
                "verification": "Total Debits must equal Total Credits",
                "parameters": {
                    "as_of_date": {
                        "type": "datetime (ISO format)",
                        "required": True,
                        "description": "Report date - shows balances as of this date"
                    }
                },
                "use_case": "Verify posting accuracy before generating other reports",
            },
            {
                "name": "Balance Sheet",
                "key": "balance-sheet",
                "endpoint": "/api/finance/reports/balance-sheet",
                "method": "GET",
                "description": "Financial position - Assets = Liabilities + Equity",
                "verification": "Assets must equal Liabilities + Equity",
                "parameters": {
                    "as_of_date": {
                        "type": "datetime (ISO format)",
                        "required": True,
                        "description": "Report date - shows financial position at this date"
                    }
                },
                "use_case": "View school's net worth/financial position at specific date",
            },
            {
                "name": "Profit & Loss Statement",
                "key": "profit-loss",
                "endpoint": "/api/finance/reports/profit-loss",
                "method": "GET",
                "description": "Income statement - Revenue minus Expenses for a period",
                "verification": "Net Income = Revenue - Operating Expenses ± Other Items",
                "parameters": {
                    "start_date": {
                        "type": "datetime (ISO format)",
                        "required": True,
                        "description": "Period start date"
                    },
                    "end_date": {
                        "type": "datetime (ISO format)",
                        "required": True,
                        "description": "Period end date"
                    }
                },
                "use_case": "Analyze school profitability for a specific period (month, quarter, year)",
            },
            {
                "name": "Cash Flow Statement",
                "key": "cash-flow",
                "endpoint": "/api/finance/reports/cash-flow",
                "method": "GET",
                "description": "Cash movement - shows cash inflows and outflows for a period",
                "verification": "Ending Cash = Beginning Cash + Net Change",
                "parameters": {
                    "start_date": {
                        "type": "datetime (ISO format)",
                        "required": True,
                        "description": "Period start date"
                    },
                    "end_date": {
                        "type": "datetime (ISO format)",
                        "required": True,
                        "description": "Period end date"
                    }
                },
                "use_case": "Track cash position changes and understand cash flow sources/uses",
            },
        ]
    }


# ==================== Trial Balance Report ====================

@router.get("/trial-balance", response_model=TrialBalanceReport)
async def get_trial_balance(
    as_of_date: datetime = Query(
        ...,
        description="Report date (ISO format, e.g., 2026-04-01T00:00:00)"
    ),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Generate Trial Balance report
    
    **Access:** All authenticated users (read-only)
    
    Trial Balance shows all GL accounts with their debit and credit balances.
    Serves as verification that posting was correct (Total Debits = Total Credits).
    
    Query parameters:
    - as_of_date: Report date in ISO format (cumulative from inception through this date)
    
    Returns:
    - trial_balance: TrialBalanceReport with all accounts and verification
    - is_balanced: True if total_debits == total_credits
    - difference: Should be 0 if balanced
    
    Accounting Principle:
    In all GL postings, debits must equal credits. If they don't match on the
    trial balance, there's an error in posting that must be corrected.
    
    Example:
    ```
    GET /api/finance/reports/trial-balance?as_of_date=2026-04-01T00:00:00
    
    Response (200):
    {
      "school_id": "school-uuid",
      "as_of_date": "2026-04-01T00:00:00",
      "line_items": [
        {
          "account_code": "1010",
          "account_name": "Business Checking Account",
          "account_type": "asset",
          "normal_balance": "debit",
          "debit_amount": 50000,
          "credit_amount": 0,
          "balance": 50000,
          "closing_balance": 50000
        },
        ...
      ],
      "total_debits": 125000,
      "total_credits": 125000,
      "difference": 0,
      "is_balanced": true
    }
    ```
    """
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No school context - contact administrator"
        )
    
    service = ReportsService(session)
    
    try:
        report = await service.generate_trial_balance(
            school_id=school_id,
            as_of_date=as_of_date,
        )
        return report
    except ReportsServiceError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error generating trial balance: {str(e)}"
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Unexpected error: {str(e)}"
        )


# ==================== Balance Sheet Report ====================

@router.get("/balance-sheet", response_model=BalanceSheetReport)
async def get_balance_sheet(
    as_of_date: datetime = Query(
        ...,
        description="Report date (ISO format, e.g., 2026-04-01T00:00:00)"
    ),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Generate Balance Sheet report
    
    **Access:** All authenticated users (read-only)
    
    Balance Sheet shows financial position: Assets = Liabilities + Equity
    
    Query parameters:
    - as_of_date: Report date in ISO format (point-in-time snapshot)
    
    Returns:
    - balance_sheet: BalanceSheetReport organized into three sections
    - is_balanced: True if total_assets == total_liabilities + total_equity
    - balance_difference: Should be 0 if balanced
    
    Structure:
    ASSETS (including current and fixed)
      Cash, Receivables, Equipment, etc.
      Total: X
    
    LIABILITIES (current and long-term)
      Payables, Accruals, Long-term Debt, etc.
      Total: Y
    
    EQUITY (accumulated capital and earnings)
      School Fund, Retained Earnings, etc.
      Total: Z
    
    Verification: X must equal Y + Z
    
    Example:
    ```
    GET /api/finance/reports/balance-sheet?as_of_date=2026-04-01T00:00:00
    
    Response (200):
    {
      "school_id": "school-uuid",
      "as_of_date": "2026-04-01T00:00:00",
      "assets": {
        "section_name": "Assets",
        "section_type": "assets",
        "items": [
          {
            "account_code": "1010",
            "account_name": "Business Checking Account",
            "amount": 50000
          }
        ],
        "section_total": 65000
      },
      "liabilities": { ... },
      "equity": { ... },
      "total_assets": 65000,
      "total_liabilities": 10000,
      "total_equity": 55000,
      "is_balanced": true,
      "balance_difference": 0
    }
    ```
    """
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No school context - contact administrator"
        )
    
    service = ReportsService(session)
    
    try:
        report = await service.generate_balance_sheet(
            school_id=school_id,
            as_of_date=as_of_date,
        )
        return report
    except ReportsServiceError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error generating balance sheet: {str(e)}"
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Unexpected error: {str(e)}"
        )


# ==================== Profit & Loss Report ====================

@router.get("/profit-loss", response_model=ProfitLossReport)
async def get_profit_loss(
    start_date: datetime = Query(
        ...,
        description="Period start date (ISO format, e.g., 2026-01-01T00:00:00)"
    ),
    end_date: datetime = Query(
        ...,
        description="Period end date (ISO format, e.g., 2026-03-31T23:59:59)"
    ),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Generate Profit & Loss Statement (Income Statement)
    
    **Access:** All authenticated users (read-only)
    
    P&L Statement shows income and expenses for a period: Revenue - Expenses = Net Income
    
    Query parameters:
    - start_date: Period start date (ISO format)
    - end_date: Period end date (ISO format)
    
    Returns:
    - profit_loss: ProfitLossReport organized into revenue and expense sections
    - net_income: Bottom line (should be positive for profitable periods)
    
    Structure:
    REVENUE
      Tuition Fees, Exam Fees, Sports Fees, Sports Fees, PTA Fees, etc.
      Subtotal: A
    
    OPERATING EXPENSES
      Salaries, Utilities, Supplies, Maintenance, etc.
      Subtotal: B
    
    OTHER INCOME (if any)
      Interest Received, Grants, Donations
      Subtotal: C
    
    OTHER EXPENSES (if any)
      Interest Paid, Depreciation
      Subtotal: D
    
    Net Income = A - B + C - D
    
    Example:
    ```
    GET /api/finance/reports/profit-loss?start_date=2026-01-01T00:00:00&end_date=2026-03-31T23:59:59
    
    Response (200):
    {
      "school_id": "school-uuid",
      "period_start_date": "2026-01-01T00:00:00",
      "period_end_date": "2026-03-31T23:59:59",
      "revenue_section": {
        "section_name": "Revenue",
        "section_type": "revenue",
        "items": [
          {
            "account_code": "4100",
            "account_name": "Tuition Fees",
            "amount": 300000
          }
        ],
        "section_total": 375000
      },
      "operating_expenses_section": { ... },
      "total_revenue": 375000,
      "total_operating_expenses": 185000,
      "operating_income": 190000,
      "net_income": 190000
    }
    ```
    """
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No school context - contact administrator"
        )
    
    # Validate date range
    if end_date < start_date:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="end_date must be after or equal to start_date"
        )
    
    service = ReportsService(session)
    
    try:
        report = await service.generate_profit_loss(
            school_id=school_id,
            start_date=start_date,
            end_date=end_date,
        )
        return report
    except ReportsServiceError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error generating P&L statement: {str(e)}"
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Unexpected error: {str(e)}"
        )


# ==================== Cash Flow Report ====================

@router.get("/cash-flow", response_model=CashFlowReport)
async def get_cash_flow(
    start_date: datetime = Query(
        ...,
        description="Period start date (ISO format, e.g., 2026-01-01T00:00:00)"
    ),
    end_date: datetime = Query(
        ...,
        description="Period end date (ISO format, e.g., 2026-03-31T23:59:59)"
    ),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Generate Cash Flow Statement
    
    **Access:** All authenticated users (read-only)
    
    Cash Flow Statement tracks movement of cash during a period.
    Shows cash sources and uses across three activity categories.
    
    Query parameters:
    - start_date: Period start date (ISO format)
    - end_date: Period end date (ISO format)
    
    Returns:
    - cash_flow: CashFlowReport showing cash movements and reconciliation
    - net_change_in_cash: Total cash movement during period
    - ending_cash_balance: Cash position at period end
    
    Structure:
    OPERATING ACTIVITIES
      Net Income from operations
      Adjustments for non-cash items
      Subtotal: A (cash generated from operations)
    
    INVESTING ACTIVITIES (simplified)
      Purchase of equipment, property, etc.
      Sales of assets
      Subtotal: B (cash used/generated by investing)
    
    FINANCING ACTIVITIES (simplified)
      Borrowing, loan repayment
      Owner contributions
      Subtotal: C (cash from/to financing)
    
    Cash Flow Statement:
      Operating Cash Flow: A
      Investing Cash Flow: B
      Financing Cash Flow: C
      Net Change in Cash: A + B + C
      Beginning Cash: X
      Ending Cash: X + (A + B + C)
    
    Example:
    ```
    GET /api/finance/reports/cash-flow?start_date=2026-01-01T00:00:00&end_date=2026-03-31T23:59:59
    
    Response (200):
    {
      "school_id": "school-uuid",
      "period_start_date": "2026-01-01T00:00:00",
      "period_end_date": "2026-03-31T23:59:59",
      "operating_activities": {
        "activity_type": "operating",
        "activity_name": "Operating Activities",
        "items": [
          {
            "description": "Net Income",
            "amount": 190000
          }
        ],
        "activity_subtotal": 190000
      },
      "investing_activities": null,
      "financing_activities": null,
      "cash_from_operations": 190000,
      "cash_from_investing": 0,
      "cash_from_financing": 0,
      "net_change_in_cash": 190000,
      "beginning_cash_balance": 0,
      "ending_cash_balance": 190000
    }
    ```
    """
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No school context - contact administrator"
        )
    
    # Validate date range
    if end_date < start_date:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="end_date must be after or equal to start_date"
        )
    
    service = ReportsService(session)
    
    try:
        report = await service.generate_cash_flow(
            school_id=school_id,
            start_date=start_date,
            end_date=end_date,
        )
        return report
    except ReportsServiceError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error generating cash flow statement: {str(e)}"
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Unexpected error: {str(e)}"
        )
