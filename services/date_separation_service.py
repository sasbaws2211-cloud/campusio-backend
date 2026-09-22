"""Date Separation Service - Entry Date vs Posting Date Logic

Handles accounting cutoff procedures:
- Entry Date: When the transaction occurred (business perspective)
- Posting Date: When posted to GL (accounting period perspective)

Supports:
- Accrual accounting with different entry/posting periods
- Month-end cutoff procedures
- Adjusting entries posted after period close
- Retroactive postings for prior periods
"""
import logging
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select, and_, or_

from models.finance import JournalEntry, PostingStatus, ReferenceType
from models.finance.fiscal_period import FiscalPeriod, FiscalPeriodStatus
from services.fiscal_period_service import FiscalPeriodService

logger = logging.getLogger(__name__)


class DateSeparationError(Exception):
    """Base exception for date separation operations"""
    pass


class DateSeparationService:
    """Service for managing entry date vs posting date separation
    
    **Key Concepts**:
    - entry_date: When the business transaction occurred
    - posted_date: When posted to GL (determines which fiscal period it affects)
    - cutoff_period: GL period this entry affects (based on posted_date)
    
    **Use Cases**:
    1. Month-end accruals: Record expense on 1/31 (entry_date), 
       post to January (posted_date = 1/31)
    
    2. Late posting: Record invoice on 2/3 (entry_date), 
       but post to January (posted_date = 1/31, posted_date before entry_date)
    
    3. Adjusting entries: Record at month-end after books close,
       post to GL with is_adjusting_entry=True flag
    
    4. Prior period adjustment: Discover error from January,
       record adjustment in February but posted_date = 1/31
    """
    
    def __init__(self, session: AsyncSession):
        """Initialize service
        
        Args:
            session: AsyncSession for database operations
        """
        self.session = session
        self.period_service = FiscalPeriodService(session)
    
    # ==================== Period Determination ====================
    
    async def determine_posting_period(
        self,
        school_id: str,
        posted_date: datetime,
    ) -> Optional[str]:
        """Determine which fiscal period a posting date falls into
        
        Args:
            school_id: School identifier
            posted_date: Date to find period for
            
        Returns:
            Period ID, or None if no period found
        """
        try:
            period = await self.period_service.get_period_by_date(school_id, posted_date)
            return period.id if period else None
        except Exception as e:
            logger.error(f"Error determining posting period: {str(e)}")
            raise DateSeparationError(f"Failed to determine posting period: {str(e)}")
    
    async def can_post_entry_to_period(
        self,
        school_id: str,
        posted_date: datetime,
        is_adjusting_entry: bool = False,
    ) -> Dict[str, Any]:
        """Check if an entry can be posted given its posted_date
        
        Rules:
        - Normal entries: Period must be OPEN
        - Adjusting entries: Period can be LOCKED (allows post-close adjustments)
        - Never post to CLOSED or ARCHIVED periods
        
        Args:
            school_id: School identifier
            posted_date: When entry would be posted
            is_adjusting_entry: Whether this is an adjusting entry
            
        Returns:
            Dictionary with can_post (bool) and reason
        """
        try:
            period = await self.period_service.get_period_by_date(school_id, posted_date)
            
            if not period:
                return {
                    "can_post": False,
                    "reason": f"No period found for date {posted_date.date()}",
                }
            
            if period.status == FiscalPeriodStatus.CLOSED:
                return {
                    "can_post": False,
                    "reason": f"Period {period.period_name} is CLOSED. No postings allowed.",
                }
            
            if period.status == FiscalPeriodStatus.ARCHIVED:
                return {
                    "can_post": False,
                    "reason": f"Period {period.period_name} is ARCHIVED. No postings allowed.",
                }
            
            if period.status == FiscalPeriodStatus.LOCKED:
                if is_adjusting_entry:
                    if not period.allow_adjustment_entries:
                        return {
                            "can_post": False,
                            "reason": f"Period {period.period_name} is LOCKED and does not allow adjusting entries.",
                        }
                    return {
                        "can_post": True,
                        "reason": f"Adjusting entries allowed in LOCKED period {period.period_name}",
                        "period_id": period.id,
                    }
                else:
                    return {
                        "can_post": False,
                        "reason": f"Period {period.period_name} is LOCKED. Only adjusting entries allowed.",
                    }
            
            # Period is OPEN
            return {
                "can_post": True,
                "reason": f"Period {period.period_name} is OPEN for postings",
                "period_id": period.id,
            }
            
        except Exception as e:
            logger.error(f"Error checking posting eligibility: {str(e)}")
            raise DateSeparationError(f"Failed to check posting eligibility: {str(e)}")
    
    # ==================== Cutoff Procedures ====================
    
    async def get_cutoff_entries(
        self,
        school_id: str,
        period_id: str,
    ) -> Dict[str, Any]:
        """Get entries with mismatched entry_date and posted_date (cutoff entries)
        
        These are entries where the transaction occurred in one period but
        was posted in another (e.g., accruals, late postings).
        
        Args:
            school_id: School identifier
            period_id: Period to analyze for cutoff entries
            
        Returns:
            Summary of cutoff entries by type
        """
        try:
            # Get period date range
            period = await self.period_service.get_period_by_id(school_id, period_id)
            if not period:
                raise DateSeparationError(f"Period {period_id} not found")
            
            # Entries posted in this period with entry_date before period start
            early_entries = await self.session.execute(
                select(JournalEntry).where(
                    and_(
                        JournalEntry.school_id == school_id,
                        JournalEntry.fiscal_period_id == period_id,
                        JournalEntry.posted_date >= period.start_date,
                        JournalEntry.posted_date <= period.end_date,
                        JournalEntry.entry_date < period.start_date,
                        # POSTED *and* REVERSED, not POSTED alone: this is an
                        # audit/compliance report of posting-timing behavior,
                        # so an entry that was later reversed still needs to
                        # show up here — it WAS posted with this cutoff
                        # mismatch, and that fact doesn't stop being true (or
                        # stop being worth investigating) just because it was
                        # subsequently reversed.
                        JournalEntry.posting_status.in_([PostingStatus.POSTED, PostingStatus.REVERSED]),
                    )
                )
            )
            early_entries_list = early_entries.scalars().all()
            
            # Entries posted in this period with entry_date after period end
            late_entries = await self.session.execute(
                select(JournalEntry).where(
                    and_(
                        JournalEntry.school_id == school_id,
                        JournalEntry.fiscal_period_id == period_id,
                        JournalEntry.posted_date >= period.start_date,
                        JournalEntry.posted_date <= period.end_date,
                        JournalEntry.entry_date > period.end_date,
                        # POSTED *and* REVERSED, not POSTED alone: see
                        # early_entries above for why.
                        JournalEntry.posting_status.in_([PostingStatus.POSTED, PostingStatus.REVERSED]),
                    )
                )
            )
            late_entries_list = late_entries.scalars().all()
            
            return {
                "period_id": period_id,
                "period_name": period.period_name,
                "early_entries": {
                    "count": len(early_entries_list),
                    "entries": [
                        {
                            "id": e.id,
                            "entry_date": e.entry_date.date(),
                            "posted_date": e.posted_date.date() if e.posted_date else None,
                            "description": e.description,
                            "total_debit": e.total_debit,
                        }
                        for e in early_entries_list[:10]  # Top 10
                    ],
                },
                "late_entries": {
                    "count": len(late_entries_list),
                    "entries": [
                        {
                            "id": e.id,
                            "entry_date": e.entry_date.date(),
                            "posted_date": e.posted_date.date() if e.posted_date else None,
                            "description": e.description,
                            "total_debit": e.total_debit,
                        }
                        for e in late_entries_list[:10]  # Top 10
                    ],
                },
            }
            
        except DateSeparationError:
            raise
        except Exception as e:
            logger.error(f"Error getting cutoff entries: {str(e)}")
            raise DateSeparationError(f"Failed to get cutoff entries: {str(e)}")
    
    async def get_accrual_entries(
        self,
        school_id: str,
        period_id: str,
    ) -> List[Dict[str, Any]]:
        """Get accrual entries (posted before entry_date)
        
        These are pre-accrual entries where the posting date is before the
        transaction actually occurred (e.g., accrued interest).
        
        Args:
            school_id: School identifier
            period_id: Period to analyze
            
        Returns:
            List of accrual entries
        """
        try:
            entries = await self.session.execute(
                select(JournalEntry).where(
                    and_(
                        JournalEntry.school_id == school_id,
                        JournalEntry.fiscal_period_id == period_id,
                        JournalEntry.posted_date < JournalEntry.entry_date,
                        # POSTED *and* REVERSED, not POSTED alone: same
                        # audit-trail reasoning as get_cutoff_entries.
                        JournalEntry.posting_status.in_([PostingStatus.POSTED, PostingStatus.REVERSED]),
                    )
                )
            )

            return [
                {
                    "id": e.id,
                    "entry_date": e.entry_date.isoformat(),
                    "posted_date": e.posted_date.isoformat() if e.posted_date else None,
                    "days_early": (e.entry_date - e.posted_date).days,
                    "description": e.description,
                    "total_debit": e.total_debit,
                    "reference_type": e.reference_type.value,
                }
                for e in entries.scalars().all()
            ]
            
        except Exception as e:
            logger.error(f"Error getting accrual entries: {str(e)}")
            return []
    
    # ==================== Adjusting Entries ====================
    
    async def get_adjusting_entries_for_period(
        self,
        school_id: str,
        period_id: str,
    ) -> Dict[str, Any]:
        """Get adjusting entries posted to a period
        
        Adjusting entries are posted after a period is locked/closed to
        record corrections or accruals specific to that period.
        
        Args:
            school_id: School identifier
            period_id: Period ID
            
        Returns:
            List of adjusting entries with details
        """
        try:
            entries = await self.session.execute(
                select(JournalEntry).where(
                    and_(
                        JournalEntry.school_id == school_id,
                        JournalEntry.fiscal_period_id == period_id,
                        JournalEntry.is_adjusting_entry == True,
                        # POSTED *and* REVERSED, not POSTED alone: same
                        # audit-trail reasoning as get_cutoff_entries.
                        JournalEntry.posting_status.in_([PostingStatus.POSTED, PostingStatus.REVERSED]),
                    )
                ).order_by(JournalEntry.posted_date.desc())
            )
            
            adjusting_entries = entries.scalars().all()
            
            return {
                "period_id": period_id,
                "count": len(adjusting_entries),
                "entries": [
                    {
                        "id": e.id,
                        "entry_date": e.entry_date.isoformat(),
                        "posted_date": e.posted_date.isoformat() if e.posted_date else None,
                        "description": e.description,
                        "total_debit": e.total_debit,
                        "reference_type": e.reference_type.value,
                        "posted_by": e.posted_by,
                    }
                    for e in adjusting_entries
                ],
            }
            
        except Exception as e:
            logger.error(f"Error getting adjusting entries: {str(e)}")
            raise DateSeparationError(f"Failed to get adjusting entries: {str(e)}")
    
    # ==================== Date Range Queries ====================
    
    async def get_entries_by_entry_date_range(
        self,
        school_id: str,
        start_date: datetime,
        end_date: datetime,
        posting_status: Optional[PostingStatus] = None,
    ) -> List[JournalEntry]:
        """Get entries by entry_date range (when transaction occurred)
        
        Args:
            school_id: School identifier
            start_date: Start of date range
            end_date: End of date range
            posting_status: Filter by posting status (optional)
            
        Returns:
            List of entries matching criteria
        """
        try:
            query = select(JournalEntry).where(
                and_(
                    JournalEntry.school_id == school_id,
                    JournalEntry.entry_date >= start_date,
                    JournalEntry.entry_date <= end_date,
                )
            )
            
            if posting_status:
                query = query.where(JournalEntry.posting_status == posting_status)
            
            result = await self.session.execute(query)
            return result.scalars().all()
            
        except Exception as e:
            logger.error(f"Error querying by entry date range: {str(e)}")
            return []
    
    async def get_entries_by_posting_date_range(
        self,
        school_id: str,
        start_date: datetime,
        end_date: datetime,
        posting_status: Optional[PostingStatus] = PostingStatus.POSTED,
    ) -> List[JournalEntry]:
        """Get entries by posted_date range (when posted to GL)
        
        Args:
            school_id: School identifier
            start_date: Start of date range
            end_date: End of date range
            posting_status: Filter by posting status (default: POSTED)
            
        Returns:
            List of entries matching criteria
        """
        try:
            query = select(JournalEntry).where(
                and_(
                    JournalEntry.school_id == school_id,
                    JournalEntry.posted_date >= start_date,
                    JournalEntry.posted_date <= end_date,
                )
            )
            
            if posting_status:
                query = query.where(JournalEntry.posting_status == posting_status)
            
            result = await self.session.execute(query)
            return result.scalars().all()
            
        except Exception as e:
            logger.error(f"Error querying by posting date range: {str(e)}")
            return []
    
    # ==================== Date Analysis ====================
    
    async def get_date_variance_report(
        self,
        school_id: str,
        period_id: str,
    ) -> Dict[str, Any]:
        """Analyze entry_date vs posted_date variance for a period
        
        Shows how many entries have entry_date different from posted_date,
        and the distribution of variances.
        
        Args:
            school_id: School identifier
            period_id: Period to analyze
            
        Returns:
            Report of date variances
        """
        try:
            period = await self.period_service.get_period_by_id(school_id, period_id)
            if not period:
                raise DateSeparationError(f"Period {period_id} not found")
            
            # Get all posted entries in this period
            entries = await self.session.execute(
                select(JournalEntry).where(
                    and_(
                        JournalEntry.school_id == school_id,
                        JournalEntry.fiscal_period_id == period_id,
                        # POSTED *and* REVERSED, not POSTED alone: same
                        # audit-trail reasoning as get_cutoff_entries.
                        JournalEntry.posting_status.in_([PostingStatus.POSTED, PostingStatus.REVERSED]),
                    )
                )
            )

            all_entries = entries.scalars().all()
            
            # Analyze variances
            same_day = 0
            early_posted = 0  # posted before entry date
            late_posted = 0   # posted after entry date
            same_month = 0
            different_month = 0
            
            variances_by_days = {}
            
            for entry in all_entries:
                if entry.posted_date and entry.entry_date:
                    variance = (entry.posted_date - entry.entry_date).days
                    
                    if variance == 0:
                        same_day += 1
                    elif variance < 0:
                        early_posted += 1
                    else:
                        late_posted += 1
                    
                    if entry.posted_date.month == entry.entry_date.month:
                        same_month += 1
                    else:
                        different_month += 1
                    
                    # Track variance distribution
                    days_key = f"{variance}_days"
                    variances_by_days[days_key] = variances_by_days.get(days_key, 0) + 1
            
            return {
                "period_id": period_id,
                "period_name": period.period_name,
                "total_entries": len(all_entries),
                "same_day": same_day,
                "early_posted": early_posted,
                "late_posted": late_posted,
                "same_month": same_month,
                "different_month": different_month,
                "variance_distribution": variances_by_days,
            }
            
        except DateSeparationError:
            raise
        except Exception as e:
            logger.error(f"Error generating date variance report: {str(e)}")
            raise DateSeparationError(f"Failed to generate report: {str(e)}")
