"""Fee <-> GL posting — the accrual-accounting bridge that was previously
entirely missing. Before this: a fee payment posted Dr Bank / Cr Revenue
directly, with nothing ever posted at invoice time — Accounts Receivable
(GL 1100) was seeded but never used by any code path, so the GL's revenue
figure and a bursar's "fees outstanding" figure were structurally two
different numbers with no way to reconcile them.

Now: a Fee is recognized as revenue (Dr Accounts Receivable / Cr Revenue)
the moment it's invoiced; a payment against it clears that receivable
(Dr Bank-or-Clearing / Cr Accounts Receivable) instead of crediting Revenue
a second time; a discount changed after the invoice entry posts adjusts
AR/Revenue by the delta; and an uncollectable balance can be formally
written off (Dr Bad Debt Expense / Cr Accounts Receivable) instead of
sitting as a phantom receivable forever.

Shared by routers/fees.py (manual/in-person payments -> GL 1010) and
services/online_payment_service.py (Paystack -> GL 1040, the clearing
account, since that cash hasn't reached the school's real bank yet) so both
paths post identically-shaped entries.
"""
import logging
from datetime import datetime
from typing import Optional

from models.fee import Fee, FeeStructure, FeeType, FeePayment
from models.finance import JournalEntryCreate, JournalLineItemCreate, ReferenceType
from models.student import Student
from services.gl_account_helpers import get_or_create_system_account
from services.journal_entry_service import JournalEntryService

logger = logging.getLogger(__name__)

FEE_TYPE_TO_REVENUE_ACCOUNT = {
    FeeType.TUITION: "4100",
    FeeType.EXAMINATION: "4110",
    FeeType.SPORTS: "4120",
    FeeType.ICT: "4130",
    FeeType.LIBRARY: "4140",
    FeeType.PTA: "4150",
    FeeType.MAINTENANCE: "4160",
    FeeType.OTHER: "4100",
}

AR_ACCOUNT_CODE = "1100"
BAD_DEBT_ACCOUNT_CODE = "5910"
REFUNDS_PAYABLE_ACCOUNT_CODE = "2150"
CHECKING_ACCOUNT_CODE = "1010"
PAYSTACK_CLEARING_ACCOUNT_CODE = "1040"
BANK_FEE_ACCOUNT_CODE = "5850"


async def _student_name(session, student_id: str) -> str:
    student = await session.get(Student, student_id)
    return f"{student.first_name} {student.last_name}" if student else "Unknown"


async def _post(session, school_id: str, entry_data: JournalEntryCreate, actor: str) -> str:
    journal_service = JournalEntryService(session)
    entry = await journal_service.create_entry(school_id=school_id, entry_data=entry_data, created_by=actor)
    posted = await journal_service.post_entry(
        school_id=school_id, entry_id=entry.id, posted_by=actor, approval_notes=entry_data.notes or "Auto-posted"
    )
    return posted.id


async def post_fee_invoice(
    session, school_id: str, fee: Fee, fee_structure: FeeStructure, net_amount: float, actor: str = "SYSTEM",
) -> Optional[str]:
    """Dr Accounts Receivable / Cr Revenue for `net_amount` (amount_due
    minus any discount already known at invoice time). A fully-discounted
    fee (net_amount <= 0) posts nothing — there's no revenue to recognize."""
    if net_amount <= 0:
        return None
    revenue_code = FEE_TYPE_TO_REVENUE_ACCOUNT.get(fee_structure.fee_type, "4100")
    ar_account = await get_or_create_system_account(session, school_id, AR_ACCOUNT_CODE)
    revenue_account = await get_or_create_system_account(session, school_id, revenue_code)
    student_name = await _student_name(session, fee.student_id)

    entry_data = JournalEntryCreate(
        entry_date=datetime.utcnow(),
        reference_type=ReferenceType.FEE_INVOICE,
        reference_id=fee.id,
        description=f"Fee invoiced to {student_name} - {fee_structure.fee_type}",
        line_items=[
            JournalLineItemCreate(gl_account_id=ar_account.id, debit_amount=float(net_amount), credit_amount=0.0, description=f"Fee invoiced - {fee_structure.fee_type}"),
            JournalLineItemCreate(gl_account_id=revenue_account.id, debit_amount=0.0, credit_amount=float(net_amount), description=f"Fee revenue recognized - {student_name}"),
        ],
        notes=f"Auto-posted at fee invoice time for fee {fee.id}",
    )
    entry_id = await _post(session, school_id, entry_data, actor)
    fee.invoice_journal_entry_id = entry_id
    session.add(fee)
    return entry_id


async def post_fee_discount_adjustment(
    session, school_id: str, fee: Fee, fee_structure: FeeStructure, delta: float, actor: str,
) -> Optional[str]:
    """Adjust AR/Revenue for a CHANGE in discount applied after the invoice
    entry was already posted. delta > 0 = discount increased (net amount
    owed went down) -> Dr Revenue / Cr AR. delta < 0 = discount decreased
    (net amount owed went up) -> Dr AR / Cr Revenue. No-op at delta == 0."""
    if delta == 0 or fee.invoice_journal_entry_id is None:
        return None
    revenue_code = FEE_TYPE_TO_REVENUE_ACCOUNT.get(fee_structure.fee_type, "4100")
    ar_account = await get_or_create_system_account(session, school_id, AR_ACCOUNT_CODE)
    revenue_account = await get_or_create_system_account(session, school_id, revenue_code)
    amount = abs(delta)

    if delta > 0:
        lines = [
            JournalLineItemCreate(gl_account_id=revenue_account.id, debit_amount=amount, credit_amount=0.0, description="Discount increase - revenue reduction"),
            JournalLineItemCreate(gl_account_id=ar_account.id, debit_amount=0.0, credit_amount=amount, description="Discount increase - receivable reduction"),
        ]
    else:
        lines = [
            JournalLineItemCreate(gl_account_id=ar_account.id, debit_amount=amount, credit_amount=0.0, description="Discount decrease - receivable restored"),
            JournalLineItemCreate(gl_account_id=revenue_account.id, debit_amount=0.0, credit_amount=amount, description="Discount decrease - revenue restored"),
        ]

    entry_data = JournalEntryCreate(
        entry_date=datetime.utcnow(), reference_type=ReferenceType.ADJUSTMENT, reference_id=fee.id,
        description=f"Discount adjustment for fee {fee.id}", line_items=lines,
        notes=f"Auto-posted discount adjustment (delta={delta}) for fee {fee.id}",
    )
    return await _post(session, school_id, entry_data, actor)


async def post_fee_write_off(session, school_id: str, fee: Fee, amount: float, actor: str) -> Optional[str]:
    """Dr Bad Debt Expense / Cr Accounts Receivable — closes the fee-side
    write-off gap: previously an uncollectable balance had no resolution
    path at all and sat as a phantom receivable forever."""
    if amount <= 0:
        return None
    ar_account = await get_or_create_system_account(session, school_id, AR_ACCOUNT_CODE)
    bad_debt_account = await get_or_create_system_account(session, school_id, BAD_DEBT_ACCOUNT_CODE)
    student_name = await _student_name(session, fee.student_id)

    entry_data = JournalEntryCreate(
        entry_date=datetime.utcnow(), reference_type=ReferenceType.WRITE_OFF, reference_id=fee.id,
        description=f"Fee write-off for {student_name}",
        line_items=[
            JournalLineItemCreate(gl_account_id=bad_debt_account.id, debit_amount=float(amount), credit_amount=0.0, description=f"Bad debt - {student_name}"),
            JournalLineItemCreate(gl_account_id=ar_account.id, debit_amount=0.0, credit_amount=float(amount), description=f"Receivable written off - {student_name}"),
        ],
        notes=f"Auto-posted write-off for fee {fee.id}",
    )
    return await _post(session, school_id, entry_data, actor)


async def post_fee_payment(
    session, school_id: str, payment: FeePayment, amount: float, cash_account_code: str = CHECKING_ACCOUNT_CODE, actor: str = "SYSTEM",
) -> Optional[str]:
    """Dr Bank-or-Clearing / Cr Accounts Receivable — clears the receivable
    as cash actually arrives. cash_account_code lets a caller choose 1010
    (manual/in-person payments — already real cash in hand) vs 1040
    (Paystack Clearing — online payments, not yet in the school's real
    bank until a settlement withdrawal clears)."""
    if amount <= 0:
        return None
    ar_account = await get_or_create_system_account(session, school_id, AR_ACCOUNT_CODE)
    cash_account = await get_or_create_system_account(session, school_id, cash_account_code)
    student_name = await _student_name(session, payment.student_id)

    entry_data = JournalEntryCreate(
        entry_date=datetime.fromisoformat(payment.payment_date) if isinstance(payment.payment_date, str) else payment.payment_date,
        reference_type=ReferenceType.FEE_PAYMENT,
        reference_id=payment.id,
        description=f"Fee payment received from {student_name} ({payment.receipt_number})",
        line_items=[
            JournalLineItemCreate(gl_account_id=cash_account.id, debit_amount=float(amount), credit_amount=0.0, description=f"Fee payment received from {student_name}"),
            JournalLineItemCreate(gl_account_id=ar_account.id, debit_amount=0.0, credit_amount=float(amount), description=f"Receivable cleared - {student_name} ({payment.receipt_number})"),
        ],
        notes=f"Auto-posted from fee payment {payment.id} - Method: {payment.payment_method}",
    )
    return await _post(session, school_id, entry_data, actor)


async def post_refund_liability(session, school_id: str, transaction, amount: float, cash_account_code: str = PAYSTACK_CLEARING_ACCOUNT_CODE, actor: str = "SYSTEM") -> Optional[str]:
    """Dr Bank/Clearing / Cr Refunds Payable — recognizes that cash for an
    overpayment genuinely arrived but is owed back, instead of leaving it
    with zero GL representation until/unless someone remembers to refund
    it (previously: GL cash was silently understated for that window)."""
    if amount <= 0:
        return None
    cash_account = await get_or_create_system_account(session, school_id, cash_account_code)
    refunds_payable_account = await get_or_create_system_account(session, school_id, REFUNDS_PAYABLE_ACCOUNT_CODE)

    entry_data = JournalEntryCreate(
        entry_date=datetime.utcnow(), reference_type=ReferenceType.REFUND, reference_id=transaction.id,
        description=f"Fee overpayment flagged for refund (ref {getattr(transaction, 'reference', transaction.id)})",
        line_items=[
            JournalLineItemCreate(gl_account_id=cash_account.id, debit_amount=float(amount), credit_amount=0.0, description="Overpayment received, refund owed"),
            JournalLineItemCreate(gl_account_id=refunds_payable_account.id, debit_amount=0.0, credit_amount=float(amount), description="Refund payable recognized"),
        ],
        notes=f"Auto-posted refund liability for transaction {transaction.id}",
    )
    return await _post(session, school_id, entry_data, actor)


async def post_refund_payout(session, school_id: str, transaction, amount_paid_out: float, cash_account_code: str = PAYSTACK_CLEARING_ACCOUNT_CODE, actor: str = "SYSTEM") -> Optional[str]:
    """Dr Refunds Payable / Cr Bank/Clearing — the refund actually leaving
    the school's account. Posted for whatever was actually paid out (which
    may be less than what was originally flagged, for a partial refund),
    independent of the original refund-liability entry's exact amount —
    a forward-posting entry rather than a reversal, so it works uniformly
    for full and partial refunds."""
    if amount_paid_out <= 0:
        return None
    cash_account = await get_or_create_system_account(session, school_id, cash_account_code)
    refunds_payable_account = await get_or_create_system_account(session, school_id, REFUNDS_PAYABLE_ACCOUNT_CODE)

    entry_data = JournalEntryCreate(
        entry_date=datetime.utcnow(), reference_type=ReferenceType.REFUND, reference_id=transaction.id,
        description=f"Refund paid to parent (ref {getattr(transaction, 'reference', transaction.id)})",
        line_items=[
            JournalLineItemCreate(gl_account_id=refunds_payable_account.id, debit_amount=float(amount_paid_out), credit_amount=0.0, description="Refund payable settled"),
            JournalLineItemCreate(gl_account_id=cash_account.id, debit_amount=0.0, credit_amount=float(amount_paid_out), description="Refund paid out"),
        ],
        notes=f"Auto-posted refund payout for transaction {transaction.id}",
    )
    return await _post(session, school_id, entry_data, actor)


async def post_settlement_withdrawal(session, school_id: str, withdrawal, actor: str = "SYSTEM") -> Optional[str]:
    """Dr Business Checking (1010) / Cr Paystack Clearing (1040) for the net
    amount received into the school's real account, plus — when a transfer
    fee is known — Dr Bank & Payment Processing Fees / Cr Paystack Clearing
    for Paystack's cut. Previously this withdrawal never touched the GL at
    all: 1010 stayed overstated by every completed withdrawal forever, and
    the transfer fee was invisible."""
    checking_account = await get_or_create_system_account(session, school_id, CHECKING_ACCOUNT_CODE)
    clearing_account = await get_or_create_system_account(session, school_id, PAYSTACK_CLEARING_ACCOUNT_CODE)

    lines = [
        JournalLineItemCreate(gl_account_id=checking_account.id, debit_amount=float(withdrawal.amount), credit_amount=0.0, description=f"MoMo withdrawal settled ({withdrawal.transfer_code})"),
    ]
    total_credit = float(withdrawal.amount)
    if withdrawal.transfer_fee and withdrawal.transfer_fee > 0:
        fee_account = await get_or_create_system_account(session, school_id, BANK_FEE_ACCOUNT_CODE)
        lines.append(JournalLineItemCreate(gl_account_id=fee_account.id, debit_amount=float(withdrawal.transfer_fee), credit_amount=0.0, description="Paystack transfer fee"))
        total_credit += float(withdrawal.transfer_fee)
    lines.append(JournalLineItemCreate(gl_account_id=clearing_account.id, debit_amount=0.0, credit_amount=total_credit, description=f"Cleared to bank - withdrawal {withdrawal.transfer_code}"))

    entry_data = JournalEntryCreate(
        entry_date=datetime.utcnow(), reference_type=ReferenceType.SETTLEMENT_WITHDRAWAL, reference_id=withdrawal.id,
        description=f"MoMo settlement withdrawal {withdrawal.transfer_code}", line_items=lines,
        notes=f"Auto-posted settlement for withdrawal {withdrawal.id}",
    )
    return await _post(session, school_id, entry_data, actor)
