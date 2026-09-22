"""Billing reporting and analytics service (Phase 2)"""
import ast
import logging
from datetime import datetime
from typing import Dict, List, Optional
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from models.billing import (
    PlatformSubscription, SubscriptionInvoice, LateFeeCharge, DiscountRule,
    BillingReport, SubscriptionStatus
)
from models.payment import OnlineTransaction, TransactionStatus, TransactionType
from services.platform_billing_service import subscription_outstanding

logger = logging.getLogger(__name__)


class BillingReportingService:
    """Generates billing reports and analytics"""
    
    async def get_revenue_report(
        self,
        session: AsyncSession,
        school_id: Optional[str] = None,
        academic_year: Optional[str] = None
    ) -> Dict:
        """
        Generate comprehensive revenue and collection report
        
        Returns all metrics for dashboard/analytics
        """
        try:
            # Build base query
            query = select(PlatformSubscription)
            if school_id:
                query = query.where(PlatformSubscription.school_id == school_id)
            if academic_year:
                # academic_year isn't a column on PlatformSubscription itself —
                # it lives on the invoice generated alongside it. This used to
                # be accepted and echoed back in the response without ever
                # being applied, so the filter was a silent no-op.
                query = query.join(
                    SubscriptionInvoice, SubscriptionInvoice.id == PlatformSubscription.invoice_id
                ).where(SubscriptionInvoice.academic_year == academic_year)

            result = await session.execute(query)
            subscriptions = result.scalars().all()
            
            # Calculate metrics
            total_subscriptions = len(subscriptions)
            total_expected = sum(s.final_amount_due for s in subscriptions)
            total_collected = sum(s.amount_paid for s in subscriptions)
            collection_rate = (
                (total_collected / total_expected * 100)
                if total_expected > 0 else 0
            )
            
            # Aging analysis
            now = datetime.utcnow()
            current_subs = sum(
                1 for s in subscriptions 
                if s.due_date > now and s.status != SubscriptionStatus.CANCELLED
            )
            overdue_subs = sum(
                1 for s in subscriptions 
                if s.due_date <= now and s.status != SubscriptionStatus.CANCELLED
            )
            # subscription_outstanding() includes late fees — final_amount_due
            # alone doesn't, so overdue amounts were understated by exactly
            # whatever late fee had accrued (same bug class fixed elsewhere
            # this session in suspension checks and reminder messages).
            overdue_amount = sum(
                subscription_outstanding(s)
                for s in subscriptions
                if s.due_date <= now and s.status != SubscriptionStatus.CANCELLED
            )
            
            # Late fees analysis
            late_fees_charged = sum(1 for s in subscriptions if s.late_fee_amount > 0)
            late_fees_total = sum(s.late_fee_amount for s in subscriptions)
            
            # Discounts analysis
            total_discounts = sum(s.discount_amount for s in subscriptions)
            discount_rate = (
                (total_discounts / total_expected * 100)
                if total_expected > 0 else 0
            )
            
            # Payment method breakdown
            mobile_money = 0
            card_payment = 0
            other_payment = 0
            
            # Get transaction details for payment method analysis. This
            # previously crashed unconditionally: TransactionStatus.SUCCESSFUL
            # doesn't exist (the real member is SUCCESS) and
            # OnlineTransaction has no payment_method column, so the whole
            # revenue report failed before either bug could even be reached.
            txn_query = select(OnlineTransaction).where(
                OnlineTransaction.status == TransactionStatus.SUCCESS,
                OnlineTransaction.transaction_type == TransactionType.SUBSCRIPTION,
            )
            if school_id:
                txn_query = txn_query.where(OnlineTransaction.school_id == school_id)
            txn_result = await session.execute(txn_query)
            transactions = txn_result.scalars().all()

            # There's no dedicated payment-channel column; best-effort parse
            # Paystack's "channel" out of the raw gateway_response (stored as
            # a Python dict repr via str(), not JSON — ast.literal_eval reads
            # that safely). Falls back to "other" if unavailable/unparsable.
            for txn in transactions:
                channel = None
                if txn.gateway_response:
                    try:
                        parsed = ast.literal_eval(txn.gateway_response)
                        if isinstance(parsed, dict):
                            channel = parsed.get("channel")
                    except (ValueError, SyntaxError):
                        pass
                if channel == "mobile_money":
                    mobile_money += 1
                elif channel == "card":
                    card_payment += 1
                else:
                    other_payment += 1
            
            report = BillingReport(
                school_id=school_id or "ALL",
                academic_year=academic_year or self._get_current_academic_year(),
                
                # Revenue metrics
                total_subscriptions=total_subscriptions,
                total_revenue_expected=round(total_expected, 2),
                total_revenue_collected=round(total_collected, 2),
                collection_rate=round(collection_rate, 2),
                
                # Aging analysis
                current_subscriptions=current_subs,
                overdue_subscriptions=overdue_subs,
                overdue_amount=round(overdue_amount, 2),
                
                # Late fees
                late_fees_charged=late_fees_charged,
                late_fees_total=round(late_fees_total, 2),
                
                # Discounts
                total_discounts_given=round(total_discounts, 2),
                discount_rate=round(discount_rate, 2),
                
                # Payment methods
                mobile_money_count=mobile_money,
                card_payment_count=card_payment,
                other_payment_count=other_payment
            )
            
            return {
                "success": True,
                "report": report.dict() if hasattr(report, 'dict') else {
                    "school_id": report.school_id,
                    "academic_year": report.academic_year,
                    "total_subscriptions": report.total_subscriptions,
                    "total_revenue_expected": report.total_revenue_expected,
                    "total_revenue_collected": report.total_revenue_collected,
                    "collection_rate": report.collection_rate,
                    "current_subscriptions": report.current_subscriptions,
                    "overdue_subscriptions": report.overdue_subscriptions,
                    "overdue_amount": report.overdue_amount,
                    "late_fees_charged": report.late_fees_charged,
                    "late_fees_total": report.late_fees_total,
                    "total_discounts_given": report.total_discounts_given,
                    "discount_rate": report.discount_rate,
                    "mobile_money_count": report.mobile_money_count,
                    "card_payment_count": report.card_payment_count,
                    "other_payment_count": report.other_payment_count
                }
            }
            
        except Exception as e:
            logger.error(f"Error generating revenue report: {str(e)}")
            return {"success": False, "error": str(e)}
    
    async def get_aging_analysis(
        self,
        session: AsyncSession,
        school_id: Optional[str] = None
    ) -> Dict:
        """
        Get aging analysis of overdue accounts
        
        Bucket: Current, 1-30 days, 31-60 days, 61+ days
        """
        try:
            query = select(PlatformSubscription).where(
                PlatformSubscription.status != SubscriptionStatus.CANCELLED
            )
            if school_id:
                query = query.where(PlatformSubscription.school_id == school_id)
            
            result = await session.execute(query)
            subscriptions = result.scalars().all()
            
            now = datetime.utcnow()
            buckets = {
                "current": {"count": 0, "amount": 0},
                "1_30_days": {"count": 0, "amount": 0},
                "31_60_days": {"count": 0, "amount": 0},
                "61_plus_days": {"count": 0, "amount": 0}
            }
            
            for sub in subscriptions:
                outstanding = subscription_outstanding(sub)

                if outstanding <= 0:
                    continue
                
                days_overdue = (now - sub.due_date).days
                
                if days_overdue < 0:
                    bucket = "current"
                elif days_overdue <= 30:
                    bucket = "1_30_days"
                elif days_overdue <= 60:
                    bucket = "31_60_days"
                else:
                    bucket = "61_plus_days"
                
                buckets[bucket]["count"] += 1
                buckets[bucket]["amount"] += outstanding
            
            # Calculate percentages
            total_overdue = sum(b["amount"] for b in buckets.values())
            
            for bucket in buckets:
                amount = buckets[bucket]["amount"]
                buckets[bucket]["percentage"] = (
                    (amount / total_overdue * 100) if total_overdue > 0 else 0
                )
                buckets[bucket]["amount"] = round(buckets[bucket]["amount"], 2)
                buckets[bucket]["percentage"] = round(
                    buckets[bucket]["percentage"], 2
                )
            
            return {
                "success": True,
                "total_overdue": round(total_overdue, 2),
                "aging_buckets": buckets
            }
            
        except Exception as e:
            logger.error(f"Error generating aging analysis: {str(e)}")
            return {"success": False, "error": str(e)}
    
    async def get_top_paying_schools(
        self,
        session: AsyncSession,
        limit: int = 10
    ) -> List[Dict]:
        """
        Get top paying schools by collection rate
        """
        try:
            result = await session.execute(
                select(PlatformSubscription).where(
                    PlatformSubscription.status != SubscriptionStatus.CANCELLED
                )
            )
            subscriptions = result.scalars().all()
            
            # Group by school
            school_stats = {}
            for sub in subscriptions:
                if sub.school_id not in school_stats:
                    school_stats[sub.school_id] = {
                        "expected": 0,
                        "collected": 0,
                        "count": 0
                    }
                
                school_stats[sub.school_id]["expected"] += sub.final_amount_due
                school_stats[sub.school_id]["collected"] += sub.amount_paid
                school_stats[sub.school_id]["count"] += 1
            
            # Calculate rates and sort
            schools = []
            for school_id, stats in school_stats.items():
                rate = (
                    (stats["collected"] / stats["expected"] * 100)
                    if stats["expected"] > 0 else 0
                )
                
                schools.append({
                    "school_id": school_id,
                    "subscriptions": stats["count"],
                    "expected": round(stats["expected"], 2),
                    "collected": round(stats["collected"], 2),
                    "collection_rate": round(rate, 2)
                })
            
            # Sort by collection rate descending
            schools.sort(key=lambda x: x["collection_rate"], reverse=True)
            
            return schools[:limit]
            
        except Exception as e:
            logger.error(f"Error getting top paying schools: {str(e)}")
            return []
    
    async def get_bottom_paying_schools(
        self,
        session: AsyncSession,
        limit: int = 10
    ) -> List[Dict]:
        """
        Get bottom paying schools by collection rate (priority for follow-up)
        """
        try:
            result = await session.execute(
                select(PlatformSubscription).where(
                    PlatformSubscription.status != SubscriptionStatus.CANCELLED
                )
            )
            subscriptions = result.scalars().all()
            
            # Group by school
            school_stats = {}
            for sub in subscriptions:
                if sub.school_id not in school_stats:
                    school_stats[sub.school_id] = {
                        "expected": 0,
                        "collected": 0,
                        "overdue": 0,
                        "count": 0,
                        "late_fees": 0
                    }
                
                school_stats[sub.school_id]["expected"] += sub.final_amount_due
                school_stats[sub.school_id]["collected"] += sub.amount_paid
                school_stats[sub.school_id]["count"] += 1
                school_stats[sub.school_id]["late_fees"] += sub.late_fee_amount
                
                if sub.due_date <= datetime.utcnow():
                    school_stats[sub.school_id]["overdue"] += subscription_outstanding(sub)
            
            # Calculate rates and sort
            schools = []
            for school_id, stats in school_stats.items():
                rate = (
                    (stats["collected"] / stats["expected"] * 100)
                    if stats["expected"] > 0 else 0
                )
                
                schools.append({
                    "school_id": school_id,
                    "subscriptions": stats["count"],
                    "expected": round(stats["expected"], 2),
                    "collected": round(stats["collected"], 2),
                    "overdue": round(stats["overdue"], 2),
                    "late_fees": round(stats["late_fees"], 2),
                    "collection_rate": round(rate, 2)
                })
            
            # Sort by collection rate ascending
            schools.sort(key=lambda x: x["collection_rate"])
            
            return schools[:limit]
            
        except Exception as e:
            logger.error(f"Error getting bottom paying schools: {str(e)}")
            return []
    
    # ========================================================================
    # UTILITY METHODS
    # ========================================================================
    
    def _get_current_academic_year(self) -> str:
        """
        Get current academic year in format YYYY/YYYY
        
        Assumes academic year starts in September
        """
        today = datetime.utcnow()
        year = today.year
        
        # If before September, academic year started last year
        if today.month < 9:
            return f"{year - 1}/{year}"
        else:
            return f"{year}/{year + 1}"
