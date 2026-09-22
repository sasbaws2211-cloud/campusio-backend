"""Settlement service for school wallet management via Paystack MoMo"""
import logging
import uuid
from datetime import datetime
from typing import Dict, Optional, List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import String, cast, func
from sqlmodel import select

from services.paystack_service import PaystackService
from models.payment import OnlineTransaction, TransactionStatus, TransactionType
from models.settlement import Withdrawal, WithdrawalStatus
from models.fee import FeePayment

logger = logging.getLogger(__name__)


class SettlementService:
    """Handles school wallet settlements via Paystack MoMo transfers"""
    
    def __init__(self, paystack_secret_key: str):
        self.paystack = PaystackService(paystack_secret_key)
    
    @staticmethod
    def detect_momo_provider(momo_number: str) -> tuple:
        """
        Detect mobile money provider from MoMo number prefix
        Returns tuple of (provider_name, bank_code) for Paystack API
        
        Ghana Paystack bank codes:
        - MTN: 024, 054, 053, 023 → bank_code="MTN"
        - Vodafone: 025, 055, 020, 050 → bank_code="VOD"
        - AirtelTigo: 027, 026, 056, 057 → bank_code="ATL"
        
        Args:
            momo_number: MoMo number (e.g., "0245782101")
        
        Returns:
            tuple: (provider_name, bank_code)
        """
        # Get first 3 digits for provider detection
        prefix_3digit = momo_number[:3]
        
        # MTN codes: 024, 054, 053, 023
        if prefix_3digit in ["024", "054", "053", "023"]:
            return ("MTN", "MTN")
        # Vodafone codes: 025, 055, 020, 050
        elif prefix_3digit in ["025", "055", "020", "050"]:
            return ("Vodafone", "VOD")
        # AirtelTigo codes: 027, 026, 056, 057
        elif prefix_3digit in ["027", "026", "056", "057"]:
            return ("AirtelTigo", "ATL")
        else:
            # Default to MTN for unknown prefixes
            logger.warning(f"Unknown MoMo prefix {prefix_3digit}, defaulting to MTN")
            return ("MTN", "MTN")
    
    async def get_balance(self) -> Dict:
        """
        Get Paystack account balance
        
        Returns:
        {
            "success": True,
            "balance": 50000.00,
            "currency": "GHS"
        }
        """
        try:
            result = await self.paystack.get_account_balance()
            return result
        except Exception as e:
            logger.error(f"Error getting balance: {str(e)}")
            return {
                "success": False,
                "error": f"Failed to get balance: {str(e)}"
            }
    
    async def create_momo_recipient(
        self,
        momo_number: str,
        recipient_name: str,
        school_id: str
    ) -> Dict:
        """
        Create a MoMo recipient in Paystack for transfers
        
        Args:
            momo_number: MoMo number (e.g., "0244123456")
            recipient_name: Name for this recipient
            school_id: School ID for tracking
        
        Returns:
        {
            "success": True,
            "recipient_code": "RCP_xxx",
            "momo_number": "0244123456",
            "recipient_name": "School Account"
        }
        """
        try:
            # Format MoMo number (Ghana: should be 10 digits)
            momo = momo_number.replace("+", "").replace(" ", "")
            if momo.startswith("233"):
                momo = "0" + momo[3:]  # Convert +233 to 0
            
            # Validate format
            if not momo.startswith("0") or len(momo) != 10:
                return {
                    "success": False,
                    "error": "Invalid MoMo number format (expected 10 digits starting with 0)"
                }
            
            # Detect provider and get bank code
            provider_name, bank_code = self.detect_momo_provider(momo)
            logger.info(f"Detected MoMo provider - Number: {momo}, Provider: {provider_name}, Bank Code: {bank_code}")
            
            # Create recipient via Paystack with mobile_money type and bank code
            result = await self.paystack.create_transfer_recipient(
                type_="mobile_money",
                account_number=momo,
                account_name=recipient_name,
                currency="GHS",
                bank_code=bank_code  # Use correct bank code for Ghana mobile money
            )
            
            return result
        
        except Exception as e:
            logger.error(f"Error creating MoMo recipient: {str(e)}")
            return {
                "success": False,
                "error": f"Failed to create recipient: {str(e)}"
            }
    
    async def initiate_momo_withdrawal(
        self,
        session: AsyncSession,
        momo_number: str,
        recipient_name: str,
        amount: float,
        school_id: str,
        initiated_by: str
    ) -> Dict:
        """
        Initiate MoMo withdrawal from Paystack
        
        Args:
            momo_number: MoMo number to receive funds
            recipient_name: Name for this recipient
            amount: Amount in GHS
            school_id: School ID
            initiated_by: Admin user ID who initiated
        
        Returns:
        {
            "success": True,
            "transfer_code": "TRF_xxx",
            "amount": 5000.00,
            "momo_number": "0244123456",
            "status": "pending"
        }
        """
        try:
            # Validate amount
            if amount <= 0:
                return {
                    "success": False,
                    "error": "Withdrawal amount must be greater than zero"
                }
            
            # Check minimum withdrawal (Paystack minimum is usually GHS 50)
            if amount < 0.5:
                return {
                     "success": False,
                     "error": "Minimum withdrawal amount is GHS 50"
                }
            
            # Step 1: Create recipient
            recipient_result = await self.create_momo_recipient(
                momo_number=momo_number,
                recipient_name=recipient_name,
                school_id=school_id
            )
            
            if not recipient_result["success"]:
                return recipient_result
            
            recipient_code = recipient_result["recipient_code"]
            
            # Step 2: Initiate transfer
            transfer_code = f"TRF-{uuid.uuid4().hex[:12].upper()}"
            amount_kobo = int(amount * 100)  # Paystack uses kobo for GHS
            
            transfer_result = await self.paystack.initiate_transfer(
                source="balance",
                amount=amount_kobo,
                recipient_code=recipient_code,
                reason=f"School fee settlement - {school_id}",
                reference=transfer_code
            )
            
            if not transfer_result["success"]:
                return {
                    "success": False,
                    "error": transfer_result.get("error", "Transfer initiation failed")
                }
            
            # Save withdrawal to database
            withdrawal = Withdrawal(
                school_id=school_id,
                amount=amount,
                momo_number=momo_number,
                recipient_name=recipient_name,
                transfer_code=transfer_code,
                recipient_code=recipient_code,
                status=WithdrawalStatus.PENDING,
                initiated_by=initiated_by
            )
            session.add(withdrawal)
            await session.commit()
            
            logger.info(f"Withdrawal initiated: {transfer_code} - GHS {amount} to {momo_number}")
            
            return {
                "success": True,
                "transfer_code": transfer_code,
                "recipient_code": recipient_code,
                "amount": amount,
                "momo_number": momo_number,
                "recipient_name": recipient_name,
                "status": "pending",
                "initiated_at": datetime.utcnow().isoformat()
            }
        
        except Exception as e:
            logger.error(f"Error initiating MoMo withdrawal: {str(e)}")
            return {
                "success": False,
                "error": f"Withdrawal failed: {str(e)}"
            }
    
    async def get_recent_transactions(
        self,
        session: AsyncSession,
        school_id: str,
        days: int = 7,
        limit: int = 20
    ) -> Dict:
        """
        Get recent online transactions (fees paid)
        
        Helps admins see what payments came in
        """
        try:
            from datetime import timedelta
            from models.payment import OnlineTransaction
            
            cutoff_date = datetime.utcnow() - timedelta(days=days)
            
            query = select(OnlineTransaction).where(
                OnlineTransaction.school_id == school_id,
                OnlineTransaction.created_at >= cutoff_date,
                cast(OnlineTransaction.status, String) == "SUCCESS",
                OnlineTransaction.transaction_type == TransactionType.FEE  # Only show fee payments, not subscriptions
            ).order_by(OnlineTransaction.created_at.desc()).limit(limit)
            
            result = await session.execute(query)
            online_transactions = result.scalars().all()
            
            transactions = []
            for t in online_transactions:
                transactions.append({
                    "id": str(t.id),
                    "reference": t.reference or "N/A",
                    "amount": float(t.amount) if t.amount else 0,
                    "date": t.completed_at.isoformat() if t.completed_at else t.created_at.isoformat(),
                    "student_name": f"Student {t.student_id[:8]}" if t.student_id else "Unknown",
                    "fee_type": "Online Payment"
                })
            
            logger.info(f"Fetched {len(transactions)} recent transactions for school {school_id}")
            
            return {
                "success": True,
                "count": len(transactions),
                "transactions": transactions
            }
        
        except Exception as e:
            logger.error(f"Error getting recent transactions: {str(e)}")
            logger.error(f"Exception type: {type(e).__name__}")
            import traceback
            logger.error(traceback.format_exc())
            return {
                "success": True,
                "count": 0,
                "transactions": []
            }
    
    async def get_withdrawal_history(
        self,
        session: AsyncSession,
        school_id: str,
        limit: int = 20
    ) -> Dict:
        """
        Get historical withdrawals for this school from database
        
        Returns structured withdrawal history sorted by date
        """
        try:
            query = select(Withdrawal).where(
                Withdrawal.school_id == school_id
            ).order_by(Withdrawal.initiated_at.desc()).limit(limit)
            
            result = await session.execute(query)
            withdrawals = result.scalars().all()
            
            withdrawal_list = []
            for w in withdrawals:
                withdrawal_list.append({
                    "id": w.id,
                    "transfer_code": w.transfer_code,
                    "amount": float(w.amount),
                    "momo_number": w.momo_number,
                    "status": w.status,
                    "initiated_at": w.initiated_at.isoformat(),
                    "completed_at": w.completed_at.isoformat() if w.completed_at else None
                })
            
            logger.info(f"Fetched {len(withdrawal_list)} withdrawals for school {school_id}")
            
            return {
                "success": True,
                "count": len(withdrawal_list),
                "withdrawals": withdrawal_list
            }
        
        except Exception as e:
            logger.error(f"Error getting withdrawal history: {str(e)}")
            return {
                "success": True,
                "count": 0,
                "withdrawals": []
            }
    
    async def calculate_school_balance(
        self,
        session: AsyncSession,
        school_id: str
    ) -> float:
        """
        Calculate school's settlement balance from database — the amount
        actually withdrawable via this Paystack-transfer flow.

        Formula: Total ONLINE (Paystack) Fees Collected - Total Withdrawals

        Previously this summed EVERY FeePayment regardless of method,
        including cash/cheque/bank-transfer payments a bursar records at
        the counter — money that never touches Paystack and therefore
        cannot be "withdrawn from Paystack" by this flow at all. Scoped to
        PaymentMethod.ONLINE_PAYMENT_PAYSTACK only, matching what a MoMo
        transfer here can actually draw against.

        Args:
            session: Database session
            school_id: School ID

        Returns:
            float: Balance in GHS (can be negative if over-withdrawn)
        """
        try:
            from models.fee import PaymentMethod
            fee_payment_result = await session.execute(
                select(func.sum(FeePayment.amount)).where(
                    FeePayment.school_id == school_id,
                    FeePayment.payment_method == PaymentMethod.ONLINE_PAYMENT_PAYSTACK,
                    FeePayment.voided == False,  # noqa: E712
                )
            )


            total_collected = fee_payment_result.scalar() or 0


            # Get total withdrawn (completed + pending withdrawals count as out)
            withdrawal_result = await session.execute(
                select(func.sum(Withdrawal.amount)).where(
                    Withdrawal.school_id == school_id,
                    Withdrawal.status.in_([WithdrawalStatus.COMPLETED, WithdrawalStatus.PENDING])
                )
            )
            total_withdrawn = withdrawal_result.scalar() or 0

            # Calculate balance
            balance = float(total_collected) - float(total_withdrawn)

            logger.info(
                f"Balance calculation for {school_id}: "
                f"Collected={total_collected}, Withdrawn={total_withdrawn}, Balance={balance}"
            )

            return balance

        except Exception as e:
            logger.error(f"Error calculating school balance: {str(e)}")
            return 0.0
    
    async def verify_transfer_status(
        self,
        transfer_code: str
    ) -> Dict:
        """
        Check status of a specific transfer from Paystack
        
        Returns:
        {
            "success": True,
            "transfer_code": "TRF-xxx",
            "status": "success|pending|failed",
            "amount": 5000.00,
            "recipient": {"name": "School MoMo", "number": "..."}
        }
        """
        try:
            result = await self.paystack.verify_transfer(transfer_code)
            return result
        except Exception as e:
            logger.error(f"Error verifying transfer: {str(e)}")
            return {
                "success": False,
                "error": str(e)
            }
    
    async def update_transfer_status(
        self,
        session: AsyncSession,
        transfer_code: str,
        status: str,
        paystack_data: Dict = None
    ) -> Dict:
        """
        Update withdrawal record with new status from Paystack
        
        Args:
            session: Database session
            transfer_code: Paystack transfer code
            status: New status (completed, failed, pending)
            paystack_data: Data from Paystack verification
        
        Returns:
            {
                "success": True/False,
                "updated": True/False,
                "transfer_code": "TRF-xxx",
                "status": "completed|failed|pending"
            }
        """
        try:
            logger.info(f"💾 [UPDATE] Looking for withdrawal with code: {transfer_code}")
            
            # Find withdrawal record by transfer code
            statement = select(Withdrawal).where(
                Withdrawal.transfer_code == transfer_code
            )
            result = await session.execute(statement)
            withdrawal = result.scalars().first()
            
            if not withdrawal:
                logger.warning(f"❌ [UPDATE] Withdrawal NOT FOUND for code: {transfer_code}")
                return {
                    "success": False,
                    "updated": False,
                    "message": "Withdrawal not found"
                }
            
            logger.info(f"✓ [UPDATE] Found withdrawal for {transfer_code}, current status: {withdrawal.status}")
            
            # Map status
            withdrawal_status_map = {
                "completed": WithdrawalStatus.COMPLETED,
                "success": WithdrawalStatus.COMPLETED,
                "failed": WithdrawalStatus.FAILED,
                "pending": WithdrawalStatus.PENDING
            }
            
            new_status = withdrawal_status_map.get(status, WithdrawalStatus.PENDING)
            
            logger.info(f"🔄 [UPDATE] Status mapping: '{status}' → {new_status}")
            
            # Update if status actually changed
            if withdrawal.status != new_status:
                old_status = withdrawal.status
                withdrawal.status = new_status

                journal_entry_id = None
                if new_status == WithdrawalStatus.COMPLETED:
                    withdrawal.completed_at = datetime.utcnow()
                    logger.info(f"⏰ [UPDATE] Set completed_at timestamp")

                    if paystack_data:
                        fee_value = paystack_data.get("fees") or paystack_data.get("fee")
                        if fee_value is not None:
                            try:
                                withdrawal.transfer_fee = round(float(fee_value) / 100, 2)  # Paystack amounts are in kobo/pesewas
                            except (TypeError, ValueError):
                                pass

                    # Post the withdrawal to the GL — previously a
                    # completed withdrawal never touched the ledger at
                    # all, so GL 1010 (Business Checking) stayed
                    # permanently overstated by every completed withdrawal
                    # forever, and Paystack's transfer fee was invisible.
                    try:
                        from services import fee_gl_service
                        session.add(withdrawal)
                        await session.flush()
                        journal_entry_id = await fee_gl_service.post_settlement_withdrawal(session, withdrawal.school_id, withdrawal)
                        withdrawal.journal_entry_id = journal_entry_id
                    except Exception as e:
                        logger.error(f"Error posting settlement journal entry for withdrawal {withdrawal.id}: {str(e)}")

                await session.commit()
                logger.info(
                    f"✅ [UPDATE] SUCCESS: Updated withdrawal {transfer_code} "
                    f"from {old_status} → {new_status}"
                )
                
                return {
                    "success": True,
                    "updated": True,
                    "transfer_code": transfer_code,
                    "status": status
                }
            else:
                logger.info(
                    f"⚠️ [UPDATE] Status unchanged for {transfer_code}: {withdrawal.status} (no update needed)"
                )
                return {
                    "success": True,
                    "updated": False,
                    "transfer_code": transfer_code,
                    "status": status
                }
        
        except Exception as e:
            logger.error(
                f"❌ [UPDATE] Error updating transfer status: {str(e)}", 
                exc_info=True
            )
            await session.rollback()
            return {
                "success": False,
                "updated": False,
                "error": str(e)
            }
