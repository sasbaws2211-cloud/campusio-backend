"""Admin surface for the third-party integration layer: API keys and
webhook endpoints (see models/integrations.py). Managing these is gated by
require_roles, same reasoning as routers/roles.py — this is an admin
bootstrap concern, not something to gate behind the RBAC/scope system it
configures.
"""
import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from auth import require_roles
from database import get_session
from models.integrations import (
    ApiKey,
    ApiKeyCreate,
    ApiKeyCreateResponse,
    ApiKeyResponse,
    BiometricDevice,
    BiometricDeviceCreate,
    BiometricDeviceResponse,
    BiometricDeviceUpdate,
    BiometricRejectedScan,
    WebhookDelivery,
    WebhookDeliveryResponse,
    WebhookEndpoint,
    WebhookEndpointCreate,
    WebhookEndpointCreateResponse,
    WebhookEndpointResponse,
    WebhookEndpointUpdate,
)
from models.user import User, UserRole
from services.ai_key_crypto import encrypt_api_key
from services.api_key_service import generate_key, hash_key
from services.plan_gating import require_plan_feature
from services.biometric_device_health_service import is_device_stale

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/integrations", tags=["Integrations"])

INTEGRATIONS_ADMIN_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)

# The event catalog third parties can subscribe to. Extend this list as
# more trigger points are wired up (see services/webhook_service.py's
# emit_event call sites) — a school can only select events that exist here.
SUPPORTED_WEBHOOK_EVENTS = [
    "payment.completed",
    "student.created",
    "expense.approved",
    "fee.invoice.created",
    "grade.recorded",
    "attendance.marked",
    "staff.created",
    "staff.exited",
    "class.created",
    "announcement.published",
    "timetable.created",
    "discipline.incident.reported",
    "library.loan.issued",
    "ticket.created",
    "transport.enrollment.created",
    "hostel.allocation.created",
    "hostel.allocation.deallocated",
    "admissions.applicant.created",
    "alumni.record.created",
    "staff_attendance.marked",
    "lms.assignment.synced",
    "lms.submission.synced",
    "curriculum.lesson_note.recorded",
    "curriculum.topic.coverage_updated",
    "tracks.student.enrolled",
    "academic_calendar.event.created",
    "exams.schedule.created",
    "exam_papers.question.created",
    "exam_marks.recorded",
    "exam_malpractice.case.reported",
    "exam_board.result.recorded",
    "exam_board.registration.index_issued",
]


@router.get("/events", response_model=List[str])
async def list_supported_events(
    current_user: User = Depends(require_roles(*INTEGRATIONS_ADMIN_ROLES)),
    _plan_check: User = Depends(require_plan_feature("integrations")),
):
    """The event types available to subscribe a webhook endpoint to."""
    return SUPPORTED_WEBHOOK_EVENTS


# ==================== API Keys ====================

@router.post("/api-keys", response_model=ApiKeyCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_api_key(
    key_data: ApiKeyCreate,
    current_user: User = Depends(require_roles(*INTEGRATIONS_ADMIN_ROLES)),
    _plan_check: User = Depends(require_plan_feature("integrations")),
    session: AsyncSession = Depends(get_session),
):
    """Create a new API key. The raw key is returned exactly once — it is
    never recoverable after this response, only the prefix + scopes are
    kept for display in the list view."""
    if not current_user.school_id:
        raise HTTPException(status_code=400, detail="No school context")

    raw_key, prefix = generate_key()
    api_key = ApiKey(
        school_id=current_user.school_id,
        name=key_data.name,
        key_prefix=prefix,
        key_hash=hash_key(raw_key),
        scopes=key_data.scopes,
        expires_at=key_data.expires_at,
        created_by=current_user.id,
    )
    session.add(api_key)
    await session.commit()

    return ApiKeyCreateResponse(
        id=api_key.id,
        name=api_key.name,
        key_prefix=api_key.key_prefix,
        scopes=api_key.scopes,
        is_active=api_key.is_active,
        created_at=api_key.created_at,
        last_used_at=api_key.last_used_at,
        expires_at=api_key.expires_at,
        raw_key=raw_key,
    )


@router.get("/api-keys", response_model=List[ApiKeyResponse])
async def list_api_keys(
    current_user: User = Depends(require_roles(*INTEGRATIONS_ADMIN_ROLES)),
    _plan_check: User = Depends(require_plan_feature("integrations")),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(ApiKey).where(ApiKey.school_id == current_user.school_id).order_by(ApiKey.created_at.desc())
    )
    return [ApiKeyResponse.model_validate(k) for k in result.scalars().all()]


@router.delete("/api-keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_api_key(
    key_id: str,
    current_user: User = Depends(require_roles(*INTEGRATIONS_ADMIN_ROLES)),
    _plan_check: User = Depends(require_plan_feature("integrations")),
    session: AsyncSession = Depends(get_session),
):
    """Revoke (deactivate) an API key. Kept as a row, not deleted, so the
    delivery/usage history it's associated with stays intact."""
    key = (
        await session.execute(
            select(ApiKey).where(ApiKey.id == key_id, ApiKey.school_id == current_user.school_id)
        )
    ).scalar_one_or_none()
    if key is None:
        raise HTTPException(status_code=404, detail="API key not found")

    key.is_active = False
    session.add(key)
    await session.commit()
    return None


# ==================== Webhook Endpoints ====================

@router.post("/webhooks", response_model=WebhookEndpointCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_webhook_endpoint(
    endpoint_data: WebhookEndpointCreate,
    current_user: User = Depends(require_roles(*INTEGRATIONS_ADMIN_ROLES)),
    _plan_check: User = Depends(require_plan_feature("integrations")),
    session: AsyncSession = Depends(get_session),
):
    """Register a webhook endpoint. The raw signing secret is returned
    exactly once — the receiver needs it to verify X-Campusio-Signature on
    every delivery (see services/webhook_service.py:sign_payload)."""
    if not current_user.school_id:
        raise HTTPException(status_code=400, detail="No school context")

    unknown_events = set(endpoint_data.subscribed_events) - set(SUPPORTED_WEBHOOK_EVENTS)
    if unknown_events:
        raise HTTPException(status_code=400, detail=f"Unknown event types: {sorted(unknown_events)}")

    raw_secret, _ = generate_key()
    endpoint = WebhookEndpoint(
        school_id=current_user.school_id,
        url=endpoint_data.url,
        secret_encrypted=encrypt_api_key(raw_secret),
        subscribed_events=endpoint_data.subscribed_events,
        created_by=current_user.id,
    )
    session.add(endpoint)
    await session.commit()

    return WebhookEndpointCreateResponse(
        id=endpoint.id,
        url=endpoint.url,
        subscribed_events=endpoint.subscribed_events,
        is_active=endpoint.is_active,
        created_at=endpoint.created_at,
        raw_secret=raw_secret,
    )


@router.get("/webhooks", response_model=List[WebhookEndpointResponse])
async def list_webhook_endpoints(
    current_user: User = Depends(require_roles(*INTEGRATIONS_ADMIN_ROLES)),
    _plan_check: User = Depends(require_plan_feature("integrations")),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(WebhookEndpoint)
        .where(WebhookEndpoint.school_id == current_user.school_id)
        .order_by(WebhookEndpoint.created_at.desc())
    )
    return [WebhookEndpointResponse.model_validate(e) for e in result.scalars().all()]


@router.put("/webhooks/{endpoint_id}", response_model=WebhookEndpointResponse)
async def update_webhook_endpoint(
    endpoint_id: str,
    update_data: WebhookEndpointUpdate,
    current_user: User = Depends(require_roles(*INTEGRATIONS_ADMIN_ROLES)),
    _plan_check: User = Depends(require_plan_feature("integrations")),
    session: AsyncSession = Depends(get_session),
):
    endpoint = (
        await session.execute(
            select(WebhookEndpoint).where(
                WebhookEndpoint.id == endpoint_id, WebhookEndpoint.school_id == current_user.school_id
            )
        )
    ).scalar_one_or_none()
    if endpoint is None:
        raise HTTPException(status_code=404, detail="Webhook endpoint not found")

    if update_data.url is not None:
        endpoint.url = update_data.url
    if update_data.subscribed_events is not None:
        unknown_events = set(update_data.subscribed_events) - set(SUPPORTED_WEBHOOK_EVENTS)
        if unknown_events:
            raise HTTPException(status_code=400, detail=f"Unknown event types: {sorted(unknown_events)}")
        endpoint.subscribed_events = update_data.subscribed_events
    if update_data.is_active is not None:
        endpoint.is_active = update_data.is_active

    session.add(endpoint)
    await session.commit()
    return WebhookEndpointResponse.model_validate(endpoint)


@router.delete("/webhooks/{endpoint_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_webhook_endpoint(
    endpoint_id: str,
    current_user: User = Depends(require_roles(*INTEGRATIONS_ADMIN_ROLES)),
    _plan_check: User = Depends(require_plan_feature("integrations")),
    session: AsyncSession = Depends(get_session),
):
    endpoint = (
        await session.execute(
            select(WebhookEndpoint).where(
                WebhookEndpoint.id == endpoint_id, WebhookEndpoint.school_id == current_user.school_id
            )
        )
    ).scalar_one_or_none()
    if endpoint is None:
        raise HTTPException(status_code=404, detail="Webhook endpoint not found")

    await session.delete(endpoint)
    await session.commit()
    return None


@router.get("/webhooks/{endpoint_id}/deliveries", response_model=List[WebhookDeliveryResponse])
async def list_webhook_deliveries(
    endpoint_id: str,
    current_user: User = Depends(require_roles(*INTEGRATIONS_ADMIN_ROLES)),
    _plan_check: User = Depends(require_plan_feature("integrations")),
    session: AsyncSession = Depends(get_session),
):
    """Recent delivery attempts for one endpoint — for an integrator (or
    the school admin on their behalf) debugging why events aren't arriving."""
    endpoint = (
        await session.execute(
            select(WebhookEndpoint).where(
                WebhookEndpoint.id == endpoint_id, WebhookEndpoint.school_id == current_user.school_id
            )
        )
    ).scalar_one_or_none()
    if endpoint is None:
        raise HTTPException(status_code=404, detail="Webhook endpoint not found")

    result = await session.execute(
        select(WebhookDelivery)
        .where(WebhookDelivery.webhook_endpoint_id == endpoint_id)
        .order_by(WebhookDelivery.created_at.desc())
        .limit(50)
    )
    return [WebhookDeliveryResponse.model_validate(d) for d in result.scalars().all()]


# ==================== Biometric Devices ====================
# Registry of fingerprint/face-scan devices (or their vendor push
# middleware) allowed to post attendance punches — see
# models/integrations.py:BiometricDevice and routers/public_api.py's
# biometric punch endpoints, which check a device against this table
# before accepting a punch from it.

@router.get("/biometric-devices", response_model=List[BiometricDeviceResponse])
async def list_biometric_devices(
    current_user: User = Depends(require_roles(*INTEGRATIONS_ADMIN_ROLES)),
    _plan_check: User = Depends(require_plan_feature("integrations")),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(BiometricDevice).where(BiometricDevice.school_id == current_user.school_id).order_by(BiometricDevice.name)
    )
    responses = []
    for d in result.scalars().all():
        resp = BiometricDeviceResponse.model_validate(d)
        resp.is_stale = is_device_stale(d.is_active, d.last_seen_at, d.created_at)
        responses.append(resp)
    return responses


@router.get("/biometric-devices/rejected-scans", response_model=List[BiometricRejectedScan])
async def list_biometric_rejected_scans(
    days: int = 7,
    limit: int = 100,
    current_user: User = Depends(require_roles(*INTEGRATIONS_ADMIN_ROLES)),
    _plan_check: User = Depends(require_plan_feature("integrations")),
    session: AsyncSession = Depends(get_session),
):
    """PINs that scanned on one of this school's own registered devices but
    matched no staff/student — previously invisible outside application
    logs. Surfaces a repeated mismatch (e.g. a new student not yet
    PIN-enrolled) that a school would otherwise only discover by chance."""
    from datetime import datetime, timedelta
    cutoff = datetime.utcnow() - timedelta(days=max(1, min(days, 90)))
    result = await session.execute(
        select(BiometricRejectedScan).where(
            BiometricRejectedScan.school_id == current_user.school_id,
            BiometricRejectedScan.created_at >= cutoff,
        ).order_by(BiometricRejectedScan.created_at.desc()).limit(min(max(1, limit), 500))
    )
    return result.scalars().all()


@router.post("/biometric-devices", response_model=BiometricDeviceResponse, status_code=status.HTTP_201_CREATED)
async def create_biometric_device(
    data: BiometricDeviceCreate,
    current_user: User = Depends(require_roles(*INTEGRATIONS_ADMIN_ROLES)),
    _plan_check: User = Depends(require_plan_feature("integrations")),
    session: AsyncSession = Depends(get_session),
):
    if not current_user.school_id:
        raise HTTPException(status_code=400, detail="No school context")

    existing = await session.execute(
        select(BiometricDevice).where(
            BiometricDevice.school_id == current_user.school_id, BiometricDevice.device_serial == data.device_serial
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="A device with this serial is already registered")

    device = BiometricDevice(school_id=current_user.school_id, created_by=current_user.id, **data.model_dump())
    session.add(device)
    await session.commit()
    await session.refresh(device)
    return BiometricDeviceResponse.model_validate(device)


@router.put("/biometric-devices/{device_id}", response_model=BiometricDeviceResponse)
async def update_biometric_device(
    device_id: str,
    data: BiometricDeviceUpdate,
    current_user: User = Depends(require_roles(*INTEGRATIONS_ADMIN_ROLES)),
    _plan_check: User = Depends(require_plan_feature("integrations")),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(BiometricDevice).where(BiometricDevice.id == device_id, BiometricDevice.school_id == current_user.school_id)
    )
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(device, key, value)
    session.add(device)
    await session.commit()
    await session.refresh(device)
    return BiometricDeviceResponse.model_validate(device)


@router.delete("/biometric-devices/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_biometric_device(
    device_id: str,
    current_user: User = Depends(require_roles(*INTEGRATIONS_ADMIN_ROLES)),
    _plan_check: User = Depends(require_plan_feature("integrations")),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(BiometricDevice).where(BiometricDevice.id == device_id, BiometricDevice.school_id == current_user.school_id)
    )
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    await session.delete(device)
    await session.commit()
    return None
