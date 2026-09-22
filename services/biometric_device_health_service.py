"""Device-health check shared by the admin list endpoint
(routers/integrations.py) and the periodic alerting sweep
(services/scheduler.py) — one threshold, one definition of "stale", used in
both places so the dashboard flag and the proactive alert never disagree.
"""
from datetime import datetime, timedelta
from typing import Optional

# A device that's gone quiet for this long is flagged — chosen to comfortably
# exceed a normal gap between school sessions (morning gate rush, then quiet
# until afternoon pickup) without waiting a full day to notice a dead unit.
STALE_AFTER_HOURS = 4
# A brand-new device that has never once reported in gets a longer grace
# period before being flagged — it may simply not be installed/wired yet.
NEVER_SEEN_GRACE_HOURS = 24


def is_device_stale(is_active: bool, last_seen_at: Optional[datetime], created_at: datetime, now: Optional[datetime] = None) -> bool:
    if not is_active:
        return False
    now = now or datetime.utcnow()
    if last_seen_at is None:
        return (now - created_at) > timedelta(hours=NEVER_SEEN_GRACE_HOURS)
    return (now - last_seen_at) > timedelta(hours=STALE_AFTER_HOURS)


async def run_device_health_sweep() -> dict:
    """Periodic sweep (services/scheduler.py) — a dead device previously
    produced silent zero-attendance with no alert; this raises a persisted,
    admin-alerting SecurityIncident the first time a device is found stale,
    and stays quiet on every subsequent sweep until it either recovers
    (routers/biometric_adms.py clears stale_alerted_at on its next real
    ping) or someone acknowledges the incident. Not school-hours-aware —
    a device that's actually turned off overnight will alert once when it
    first crosses the threshold; a school can turn a device inactive
    (BiometricDevice.is_active=False) to silence one it knows is offline
    for a planned reason."""
    from sqlmodel import select
    from database import async_session
    from models.integrations import BiometricDevice
    from services import security_alert_service

    flagged = 0
    async with async_session() as session:
        result = await session.execute(select(BiometricDevice).where(BiometricDevice.is_active == True))  # noqa: E712
        devices = result.scalars().all()
        now = datetime.utcnow()
        for device in devices:
            if device.stale_alerted_at is not None:
                continue
            if not is_device_stale(device.is_active, device.last_seen_at, device.created_at, now=now):
                continue

            last_seen_label = device.last_seen_at.strftime("%Y-%m-%d %H:%M") if device.last_seen_at else "never"
            await security_alert_service.raise_incident(
                session, device.school_id, "biometric_device_stale",
                sms_message=f"Device '{device.name}' ({device.device_serial}) hasn't reported in (last seen: {last_seen_label}). Check it's powered on and connected.",
                details=f"purpose={device.purpose.value if hasattr(device.purpose, 'value') else device.purpose}, last_seen_at={last_seen_label}",
            )
            device.stale_alerted_at = now
            session.add(device)
            flagged += 1

        if flagged:
            await session.commit()

    return {"flagged": flagged}
