"""Shared "notify a student's parent" helper — in-app Message + best-effort
SMS, via the Student -> StudentParent -> Parent join used throughout this
codebase. Originally written inline in routers/grades.py for report-card
recall notifications; extracted here once a second feature (gate
attendance) needed the identical pattern, since this codebase has no
generic multi-channel notification dispatcher (see routers/sms.py's fee
reminder for the original template this follows).
"""
import logging
from datetime import datetime
from typing import Optional

from fastapi import BackgroundTasks
from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.communication import Message, MessageType, SMSNotification
from models.student import Parent, StudentParent
from models.user import User
from services.sms_service import sms_service
from services.broadcaster import broadcaster

logger = logging.getLogger(__name__)


async def notify_parent(
    session: AsyncSession,
    student,
    actor: Optional[User],
    background_tasks: BackgroundTasks,
    sms_message: str,
    in_app_subject: str,
    in_app_content: str,
    notification_type: str,
    message_type: MessageType = MessageType.GENERAL,
) -> None:
    """Best-effort notify a student's parent of some event concerning their
    child. Never raises: a notification failure must never block the action
    it's attached to (recalling a report card, logging a gate check-in,
    etc.) — errors are logged and swallowed.

    student: a models.student.Student instance (needs .id/.school_id).
    actor: the staff user triggering the notification (used as Message.sender_id).
        None for a system/device-triggered event (e.g. a biometric gate
        scan) — Message.sender_id is a required FK, so the in-app message
        is skipped in that case, but SMS still fires (it needs no actor).
    sms_message: full SMS text.
    in_app_subject/in_app_content: the in-app Message's subject/body.
    notification_type: free-text label stored on SMSNotification for filtering/history.
    message_type: models.communication.MessageType — categorizes the in-app Message.
    """
    try:
        parent_result = await session.execute(
            select(Parent).join(StudentParent, StudentParent.parent_id == Parent.id)
            .where(StudentParent.student_id == student.id)
        )
        # Every linked parent, not just the first — a two-parent household
        # previously had its second parent silently miss every gate/pickup/
        # report-card notification this helper sends.
        parents = parent_result.scalars().all()
        if not parents:
            return

        for parent in parents:
            if actor and parent.user_id:
                message = Message(
                    school_id=student.school_id,
                    sender_id=actor.id,
                    receiver_id=parent.user_id,
                    subject=in_app_subject,
                    content=in_app_content,
                    student_id=student.id,
                    message_type=message_type,
                )
                session.add(message)
                await session.commit()
                try:
                    await broadcaster.publish({
                        "type": "new_message",
                        "school_id": student.school_id,
                        "sender_id": actor.id,
                        "receiver_id": parent.user_id,
                    })
                except Exception:
                    logger.exception("Failed to publish new_message event for parent notification")

            if parent.phone and sms_service.validate_phone_number(parent.phone):
                formatted_phone = sms_service.format_phone_number(parent.phone)
                recipient_name = f"{parent.first_name} {parent.last_name}"

                async def send_task(formatted_phone=formatted_phone, recipient_name=recipient_name):
                    try:
                        result = await sms_service.send_sms([formatted_phone], sms_message)
                        sms_record = SMSNotification(
                            school_id=student.school_id,
                            recipient_phone=formatted_phone,
                            recipient_name=recipient_name,
                            message=sms_message,
                            notification_type=notification_type,
                            status="sent" if result.get("success") else "failed",
                            message_id=result.get("message_ids", [None])[0] if result.get("message_ids") else None,
                            error_message=result.get("error") if not result.get("success") else None,
                            sent_at=datetime.utcnow() if result.get("success") else None,
                        )
                        session.add(sms_record)
                        await session.commit()
                    except Exception:
                        logger.exception("Failed to send parent SMS notification")

                background_tasks.add_task(send_task)
    except Exception:
        logger.exception("Failed to notify parent")
