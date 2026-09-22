"""Web Push subscription management + per-user notification preferences.
See services/push_notification_service.py for the actual send path."""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_current_user
from database import get_session
from models.push_notification import (
    PushSubscription, PushSubscriptionCreate, NotificationPreference, NotificationPreferenceUpdate,
)
from models.user import User
from services.push_notification_service import push_notification_service

router = APIRouter(prefix="/push", tags=["Push Notifications"])


def _school_id(user: User) -> str:
    if not user.school_id:
        raise HTTPException(status_code=403, detail="No school context")
    return user.school_id


@router.get("/vapid-public-key", response_model=dict)
async def get_vapid_public_key():
    """No auth required — the frontend needs this before the user has
    necessarily done anything else, to pass as applicationServerKey to
    PushManager.subscribe()."""
    if not push_notification_service.configured:
        raise HTTPException(status_code=503, detail="Push notifications are not configured on this server")
    return {"public_key": push_notification_service.public_key}


@router.post("/subscribe", response_model=dict)
async def subscribe(payload: PushSubscriptionCreate, current_user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    school_id = _school_id(current_user)
    existing = (await session.execute(select(PushSubscription).where(PushSubscription.endpoint == payload.endpoint))).scalar_one_or_none()
    if existing:
        existing.user_id = current_user.id
        existing.school_id = school_id
        existing.p256dh_key = payload.p256dh_key
        existing.auth_key = payload.auth_key
        existing.user_agent = payload.user_agent
        session.add(existing)
        await session.commit()
        return {"success": True, "message": "Subscription updated"}

    sub = PushSubscription(
        school_id=school_id, user_id=current_user.id, endpoint=payload.endpoint,
        p256dh_key=payload.p256dh_key, auth_key=payload.auth_key, user_agent=payload.user_agent,
    )
    session.add(sub)
    await session.commit()
    return {"success": True, "message": "Subscribed to push notifications"}


@router.post("/unsubscribe", response_model=dict)
async def unsubscribe(payload: dict, current_user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    endpoint = payload.get("endpoint")
    if not endpoint:
        raise HTTPException(status_code=422, detail="endpoint is required")
    sub = (await session.execute(select(PushSubscription).where(PushSubscription.endpoint == endpoint, PushSubscription.user_id == current_user.id))).scalar_one_or_none()
    if sub:
        await session.delete(sub)
        await session.commit()
    return {"success": True, "message": "Unsubscribed"}


@router.get("/subscriptions/mine", response_model=list[dict])
async def my_subscriptions(current_user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    """So the frontend can tell whether THIS device/browser is already
    subscribed (matches by endpoint, not just "any subscription exists")."""
    result = await session.execute(select(PushSubscription).where(PushSubscription.user_id == current_user.id))
    return [{"id": s.id, "endpoint": s.endpoint, "user_agent": s.user_agent, "created_at": s.created_at} for s in result.scalars().all()]


@router.post("/test", response_model=dict)
async def send_test_push(current_user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    """Sends a test push to every subscription the current user has — lets
    the settings UI offer a "send me a test notification" button."""
    subs = (await session.execute(select(PushSubscription).where(PushSubscription.user_id == current_user.id))).scalars().all()
    if not subs:
        raise HTTPException(status_code=400, detail="You have no active push subscriptions")
    sent, expired_ids = 0, []
    for sub in subs:
        result = await push_notification_service.send(
            {"endpoint": sub.endpoint, "keys": {"p256dh": sub.p256dh_key, "auth": sub.auth_key}},
            "Campusio", "This is a test notification.", "/dashboard",
        )
        if result["success"]:
            sent += 1
        elif result["expired"]:
            expired_ids.append(sub.id)
    for sub_id in expired_ids:
        sub = await session.get(PushSubscription, sub_id)
        if sub:
            await session.delete(sub)
    if expired_ids:
        await session.commit()
    return {"sent": sent, "total": len(subs), "expired_removed": len(expired_ids)}


# ── Notification preferences ─────────────────────────────────────────────

DEFAULT_PREFS = {"push_new_message": True, "push_announcements": True, "in_app_new_message": True, "in_app_announcements": True}


async def _get_or_create_preferences(session: AsyncSession, school_id: str, user_id: str) -> NotificationPreference:
    result = await session.execute(select(NotificationPreference).where(NotificationPreference.user_id == user_id))
    prefs = result.scalar_one_or_none()
    if prefs:
        return prefs
    prefs = NotificationPreference(school_id=school_id, user_id=user_id)
    session.add(prefs)
    await session.commit()
    await session.refresh(prefs)
    return prefs


@router.get("/preferences/me", response_model=dict)
async def get_my_preferences(current_user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    prefs = await _get_or_create_preferences(session, _school_id(current_user), current_user.id)
    return {
        "push_new_message": prefs.push_new_message, "push_announcements": prefs.push_announcements,
        "in_app_new_message": prefs.in_app_new_message, "in_app_announcements": prefs.in_app_announcements,
    }


@router.put("/preferences/me", response_model=dict)
async def update_my_preferences(payload: NotificationPreferenceUpdate, current_user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    prefs = await _get_or_create_preferences(session, _school_id(current_user), current_user.id)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(prefs, key, value)
    prefs.updated_at = datetime.utcnow()
    session.add(prefs)
    await session.commit()
    await session.refresh(prefs)
    return {
        "push_new_message": prefs.push_new_message, "push_announcements": prefs.push_announcements,
        "in_app_new_message": prefs.in_app_new_message, "in_app_announcements": prefs.in_app_announcements,
    }
