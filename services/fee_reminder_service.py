"""Automated fee-due reminders to parents — the student-fee counterpart to
services/payment_reminder_service.py (which only covers a school's own
Campusio platform-subscription billing, not student fees at all). Mirrors
that service's shape: per-school config, a same-day dedupe log, SMS+email."""
import logging
from datetime import date, datetime
from typing import Dict, Optional

from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import async_session
from models.fee import Fee, FeeStructure, FeeInstallment, PaymentStatus, InstallmentStatus
from models.student import Student, StudentParent, Parent
from models.fee_reminders import FeeReminderSettings, FeeReminder
from services.sms_service import sms_service
from services.email_service import email_service

logger = logging.getLogger(__name__)


async def get_or_create_settings(session: AsyncSession, school_id: str) -> FeeReminderSettings:
    result = await session.execute(select(FeeReminderSettings).where(FeeReminderSettings.school_id == school_id))
    settings = result.scalar_one_or_none()
    if not settings:
        settings = FeeReminderSettings(school_id=school_id)
        session.add(settings)
        await session.flush()
    return settings


async def _effective_due_date(session: AsyncSession, fee: Fee) -> Optional[str]:
    """The next date this fee actually owes money by — the earliest
    not-yet-fully-paid installment's due date if the fee has installments,
    else the fee structure's own due date."""
    installments = (
        await session.execute(
            select(FeeInstallment)
            .where(FeeInstallment.fee_id == fee.id, FeeInstallment.status.in_([InstallmentStatus.PENDING.value, InstallmentStatus.PARTIAL.value, InstallmentStatus.OVERDUE.value]))
            .order_by(FeeInstallment.due_date)
        )
    ).scalars().first()
    if installments:
        return installments.due_date
    structure = (await session.execute(select(FeeStructure).where(FeeStructure.id == fee.fee_structure_id))).scalar_one_or_none()
    return structure.due_date if structure else None


async def is_fee_overdue(session: AsyncSession, fee: Fee) -> bool:
    """Whether this fee is currently past its effective due date with a
    balance still owing — computed fresh from date math every time it's
    asked, the same way send_pending_reminders decides whether to text a
    parent, rather than trusted from Fee.status == OVERDUE. That status
    field is only ever flipped by two narrow triggers (services.fee_late_fee_service's
    nightly sweep, gated behind a per-school "enable_late_fees" setting that
    defaults off; and routers/fees.py's POST /fees/refresh-overdue, a manual
    admin action nothing schedules) and can silently disagree with reality —
    a parent could get "12 days overdue" by SMS from this same date math
    while a report reading Fee.status still says zero overdue fees.
    Reporting/risk consumers should call this, not read the status field."""
    if fee.amount_due - fee.amount_paid - fee.discount <= 0:
        return False
    due_date_str = await _effective_due_date(session, fee)
    if not due_date_str:
        return False
    try:
        due_date = date.fromisoformat(due_date_str)
    except ValueError:
        return False
    return date.today() > due_date


def _message(days_until_due: int, outstanding: float, student_name: str) -> str:
    if days_until_due < 0:
        return f"{student_name}'s school fee is {abs(days_until_due)} day(s) overdue. Outstanding: GHS {outstanding:.2f}. Please settle this as soon as possible."
    if days_until_due == 0:
        return f"{student_name}'s school fee of GHS {outstanding:.2f} is due today."
    return f"Reminder: {student_name}'s school fee of GHS {outstanding:.2f} is due in {days_until_due} day(s)."


async def _send_for_fee(session: AsyncSession, fee: Fee, student: Student, days_until_due: int) -> int:
    outstanding = fee.amount_due - fee.amount_paid - fee.discount
    if outstanding <= 0:
        return 0

    # Dedupe: don't resend the same day's reminder twice for this fee.
    existing = await session.execute(
        select(FeeReminder).where(FeeReminder.fee_id == fee.id, FeeReminder.days_until_due == days_until_due, FeeReminder.sent == True)  # noqa: E712
    )
    if existing.scalar_one_or_none():
        return 0

    parents = (
        await session.execute(
            select(Parent).join(StudentParent, StudentParent.parent_id == Parent.id).where(StudentParent.student_id == student.id)
        )
    ).scalars().all()
    if not parents:
        return 0

    student_name = f"{student.first_name} {student.last_name}"
    message = _message(days_until_due, outstanding, student_name)
    sent_count = 0

    for parent in parents:
        if parent.phone:
            sms_result = await sms_service.send_sms([parent.phone], message)
            session.add(FeeReminder(
                school_id=fee.school_id, fee_id=fee.id, student_id=student.id, channel="sms",
                days_until_due=days_until_due, message=message, recipient=parent.phone,
                sent=bool(sms_result.get("success")), sent_at=datetime.utcnow() if sms_result.get("success") else None,
                status="sent" if sms_result.get("success") else "failed",
                error_message=None if sms_result.get("success") else sms_result.get("error"),
            ))
            if sms_result.get("success"):
                sent_count += 1
        if parent.email:
            email_result = await email_service.send_email([parent.email], "School Fee Payment Reminder", message)
            session.add(FeeReminder(
                school_id=fee.school_id, fee_id=fee.id, student_id=student.id, channel="email",
                days_until_due=days_until_due, message=message, recipient=parent.email,
                sent=bool(email_result.get("success")), sent_at=datetime.utcnow() if email_result.get("success") else None,
                status="sent" if email_result.get("success") else "failed",
                error_message=None if email_result.get("success") else email_result.get("error"),
            ))
            if email_result.get("success"):
                sent_count += 1

    return sent_count


async def send_pending_reminders(session: AsyncSession, school_id: Optional[str] = None) -> Dict:
    today = date.today()
    settings_query = select(FeeReminderSettings)
    if school_id:
        settings_query = settings_query.where(FeeReminderSettings.school_id == school_id)
    settings_rows = (await session.execute(settings_query)).scalars().all()

    # A school with no settings row yet still gets defaults via get_or_create.
    school_ids = {school_id} if school_id else set()
    if not school_id:
        fee_school_ids = (await session.execute(select(Fee.school_id).distinct())).scalars().all()
        school_ids.update(fee_school_ids)

    settings_by_school = {s.school_id: s for s in settings_rows}
    total_sent = 0
    fees_checked = 0

    for sid in school_ids:
        settings = settings_by_school.get(sid) or await get_or_create_settings(session, sid)
        if not settings.enabled:
            continue

        fees = (
            await session.execute(
                select(Fee).where(Fee.school_id == sid, Fee.status.in_([PaymentStatus.PENDING.value, PaymentStatus.PARTIAL.value, PaymentStatus.OVERDUE.value]))
            )
        ).scalars().all()

        for fee in fees:
            if fee.amount_due - fee.amount_paid - fee.discount <= 0:
                continue
            due_date_str = await _effective_due_date(session, fee)
            if not due_date_str:
                continue
            try:
                due_date = date.fromisoformat(due_date_str)
            except ValueError:
                continue
            days_until_due = (due_date - today).days
            if days_until_due > settings.reminder_days_before_due:
                continue  # not due soon enough yet

            fees_checked += 1
            student = (await session.execute(select(Student).where(Student.id == fee.student_id))).scalar_one_or_none()
            if not student:
                continue
            total_sent += await _send_for_fee(session, fee, student, days_until_due)

    await session.commit()
    return {"fees_checked": fees_checked, "reminders_sent": total_sent}


async def run_fee_reminder_sweep() -> None:
    """Called by services/scheduler.py — every school, every day."""
    async with async_session() as session:
        result = await send_pending_reminders(session, school_id=None)
        logger.info(f"Fee reminder sweep: {result['reminders_sent']} reminder(s) sent across {result['fees_checked']} due fee(s)")
