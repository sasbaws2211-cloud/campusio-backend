"""API Router for Exchange Rates

Endpoints for recording and browsing manual FX rates used to convert
non-base-currency expenses into the school's base currency at posting time.
"""
import logging
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime

from models.finance.exchange_rate import ExchangeRateCreate, ExchangeRateResponse
from models.user import User, UserRole
from dependencies import get_current_school_id
from auth import require_roles
from database import get_session
from services.exchange_rate_service import ExchangeRateService, ExchangeRateError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/exchange-rates", tags=["Finance - Exchange Rates"])

# Recording a rate affects how every subsequent foreign-currency expense
# posts to GL — same role gate as journal.py's posting endpoints.
FINANCE_ADMIN_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.HR)


@router.post("", response_model=ExchangeRateResponse, status_code=201)
async def create_exchange_rate(
    rate_data: ExchangeRateCreate,
    current_user: User = Depends(require_roles(*FINANCE_ADMIN_ROLES)),
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
):
    """Record a new exchange rate

    **Access:** SUPER_ADMIN, SCHOOL_ADMIN, HR

    Example: 1 USD = 12.50 GHS, effective 2026-08-01. Expenses recorded in
    USD dated on or after that date will use this rate when posted, until a
    newer rate is recorded.
    """
    service = ExchangeRateService(session)
    try:
        rate = await service.create_rate(school_id=school_id, rate_data=rate_data, created_by=current_user.id)
        return ExchangeRateResponse.model_validate(rate)
    except ExchangeRateError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error creating exchange rate: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to create exchange rate")


@router.get("", response_model=List[ExchangeRateResponse])
async def list_exchange_rates(
    from_currency: Optional[str] = Query(None),
    to_currency: Optional[str] = Query(None),
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
):
    """List recorded exchange rates, most recent first

    **Access:** All authenticated users
    """
    service = ExchangeRateService(session)
    rates = await service.list_rates(school_id, from_currency=from_currency, to_currency=to_currency)
    return [ExchangeRateResponse.model_validate(r) for r in rates]


@router.get("/convert", response_model=dict)
async def convert_amount(
    amount: float = Query(..., gt=0),
    from_currency: str = Query(...),
    to_currency: str = Query(...),
    as_of_date: Optional[str] = Query(None, description="ISO date; defaults to today"),
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
):
    """Convert an amount between currencies using the applicable recorded rate

    **Access:** All authenticated users

    Useful for previewing what an expense will post as before submitting it.
    """
    try:
        as_of = datetime.fromisoformat(as_of_date) if as_of_date else datetime.utcnow()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid as_of_date format. Use ISO format.")

    from decimal import Decimal

    service = ExchangeRateService(session)
    try:
        converted = await service.convert(
            school_id=school_id,
            amount=Decimal(str(amount)),
            from_currency=from_currency,
            to_currency=to_currency,
            as_of_date=as_of,
        )
        return {
            "amount": amount,
            "from_currency": from_currency.upper(),
            "to_currency": to_currency.upper(),
            "as_of_date": as_of.isoformat(),
            "converted_amount": float(converted),
        }
    except ExchangeRateError as e:
        raise HTTPException(status_code=400, detail=str(e))
