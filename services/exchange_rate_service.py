"""Exchange Rate Service - manual FX rates for converting expenses in a
non-base currency into the school's base currency (School.base_currency)
before posting to GL.
"""
import logging
from typing import Optional, List
from decimal import Decimal
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select, and_

from models.finance.exchange_rate import ExchangeRate, ExchangeRateCreate
from models.school import School

logger = logging.getLogger(__name__)


class ExchangeRateError(Exception):
    """Base exception for exchange rate service errors"""
    pass


class ExchangeRateService:
    """Service for recording and looking up manual FX rates"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_rate(
        self,
        school_id: str,
        rate_data: ExchangeRateCreate,
        created_by: str,
    ) -> ExchangeRate:
        """Record a new exchange rate

        Args:
            school_id: School identifier
            rate_data: from/to currency, rate, effective_date
            created_by: User recording the rate

        Returns:
            Created ExchangeRate

        Raises:
            ExchangeRateError: If validation fails
        """
        if rate_data.rate <= 0:
            raise ExchangeRateError("Exchange rate must be positive")
        if rate_data.from_currency == rate_data.to_currency:
            raise ExchangeRateError("from_currency and to_currency must differ")

        rate = ExchangeRate(
            school_id=school_id,
            from_currency=rate_data.from_currency.upper(),
            to_currency=rate_data.to_currency.upper(),
            rate=rate_data.rate,
            effective_date=rate_data.effective_date,
            notes=rate_data.notes,
            created_by=created_by,
        )
        self.session.add(rate)
        await self.session.commit()
        await self.session.refresh(rate)

        logger.info(
            f"Recorded exchange rate for school {school_id}: "
            f"1 {rate.from_currency} = {rate.rate} {rate.to_currency} (effective {rate.effective_date.date()})"
        )
        return rate

    async def list_rates(
        self,
        school_id: str,
        from_currency: Optional[str] = None,
        to_currency: Optional[str] = None,
    ) -> List[ExchangeRate]:
        """List exchange rates, most recent first"""
        query = select(ExchangeRate).where(ExchangeRate.school_id == school_id)
        if from_currency:
            query = query.where(ExchangeRate.from_currency == from_currency.upper())
        if to_currency:
            query = query.where(ExchangeRate.to_currency == to_currency.upper())
        query = query.order_by(ExchangeRate.effective_date.desc())

        result = await self.session.execute(query)
        return result.scalars().all()

    async def get_rate(
        self,
        school_id: str,
        from_currency: str,
        to_currency: str,
        as_of_date: datetime,
    ) -> Optional[Decimal]:
        """Get the applicable exchange rate as of a given date

        The most recent rate with effective_date <= as_of_date wins, so
        updating rates going forward never rewrites the conversion used by
        already-posted transactions.

        Args:
            school_id: School identifier
            from_currency: Source currency (e.g. the expense's currency)
            to_currency: Target currency (the school's base_currency)
            as_of_date: Date the rate should apply as of (e.g. expense_date)

        Returns:
            The rate (1 from_currency = rate to_currency), or None if no
            rate has been recorded on or before that date
        """
        if from_currency.upper() == to_currency.upper():
            return Decimal("1")

        result = await self.session.execute(
            select(ExchangeRate)
            .where(
                and_(
                    ExchangeRate.school_id == school_id,
                    ExchangeRate.from_currency == from_currency.upper(),
                    ExchangeRate.to_currency == to_currency.upper(),
                    ExchangeRate.effective_date <= as_of_date,
                )
            )
            .order_by(ExchangeRate.effective_date.desc())
        )
        rate = result.scalars().first()
        return rate.rate if rate else None

    async def convert(
        self,
        school_id: str,
        amount: Decimal,
        from_currency: str,
        to_currency: str,
        as_of_date: datetime,
    ) -> Decimal:
        """Convert an amount from one currency to another as of a given date

        Raises:
            ExchangeRateError: If no applicable rate has been recorded
        """
        rate = await self.get_rate(school_id, from_currency, to_currency, as_of_date)
        if rate is None:
            raise ExchangeRateError(
                f"No exchange rate found for {from_currency} -> {to_currency} "
                f"on or before {as_of_date.date()}. Record one first."
            )
        return (amount * rate).quantize(Decimal("0.01"))

    async def get_school_base_currency(self, school_id: str) -> str:
        """Get the school's base currency (defaults to GHS if unset)"""
        result = await self.session.execute(
            select(School.base_currency).where(School.id == school_id)
        )
        return result.scalar_one_or_none() or "GHS"
