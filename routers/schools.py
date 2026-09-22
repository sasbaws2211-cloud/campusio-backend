"""Schools router"""
import logging
import os
import uuid
from pathlib import Path
from fastapi import APIRouter, Depends, File, HTTPException, status, Request, UploadFile
from sqlmodel import select, SQLModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime, timezone
from typing import Optional
from models.school import School, SchoolCreate, SchoolUpdate, SchoolType, AcademicTerm, AcademicTermCreate, AcademicTermUpdate, AcademicYear
from models.staff import PayoutVerificationStatus
from models.user import User, UserRole
from database import get_session
from auth import get_current_user, require_roles
from services.coa_initialization import seed_default_chart_of_accounts
from services.fiscal_period_initialization import seed_default_fiscal_periods
from services.audit_service import log_event
from services.paystack_service import PaystackService
from services.plan_gating import require_plan_feature

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/schools", tags=["Schools"])

LOGO_UPLOAD_DIR = Path("uploads/school_logos")
LOGO_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
ALLOWED_LOGO_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp", "image/svg+xml"}
MAX_LOGO_SIZE_BYTES = 2 * 1024 * 1024  # 2 MB


def sanitize_svg(content: bytes) -> bytes:
    """Strip active content from an uploaded SVG before it's written to disk
    and served back verbatim via the /uploads static mount. SVG can embed
    <script>, event-handler attributes (onload, onclick, ...), and
    javascript: URIs in href/xlink:href -- if a viewer ever opens the logo
    URL directly (a new tab, or an <object>/<iframe> embed) rather than via
    an <img> tag, that content executes in their session. Raises
    HTTPException(400) if the file isn't parseable XML at all (rejected,
    not silently passed through)."""
    from lxml import etree

    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)
    try:
        root = etree.fromstring(content, parser=parser)
    except etree.XMLSyntaxError:
        raise HTTPException(status_code=400, detail="Uploaded file is not a valid SVG")

    JAVASCRIPT_URI_ATTRS = {"href", "{http://www.w3.org/1999/xlink}href"}
    to_remove = []
    for element in root.iter():
        if not isinstance(element.tag, str):
            continue  # skip comments/processing instructions
        tag = etree.QName(element).localname.lower()
        if tag in ("script", "foreignobject") and element.getparent() is not None:
            to_remove.append(element)
            continue
        for attr in list(element.attrib):
            attr_local = etree.QName(attr).localname.lower() if "}" in attr else attr.lower()
            if attr_local.startswith("on"):
                del element.attrib[attr]
            elif attr in JAVASCRIPT_URI_ATTRS and element.attrib[attr].strip().lower().startswith("javascript:"):
                del element.attrib[attr]

    # Removed in a second pass -- mutating the tree while root.iter() is
    # still walking it can skip a removed element's next sibling.
    for element in to_remove:
        parent = element.getparent()
        if parent is not None:
            parent.remove(element)

    return etree.tostring(root, xml_declaration=False)


@router.post("", response_model=dict)
async def create_school(
    school_data: SchoolCreate,
    request: Request,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Create a new school (super admin only)"""
    result = await session.execute(select(School).where(School.code == school_data.code))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="School code already exists")
    
    school = School(**school_data.model_dump())
    session.add(school)
    await session.commit()
    await session.refresh(school)

    # Capture the response before seeding — seed_default_chart_of_accounts commits once per
    # account, which expires this ORM object's attributes and would otherwise force a refetch.
    response = {
        "id": school.id,
        "name": school.name,
        "code": school.code,
        "school_type": school.school_type,
        "address": school.address,
        "city": school.city,
        "region": school.region,
        "phone": school.phone,
        "email": school.email,
        "logo_url": school.logo_url,
        "motto": school.motto,
        "is_active": school.is_active,
        "enable_hostel": school.enable_hostel,
        "require_maker_checker": school.require_maker_checker,
        "created_at": school.created_at.isoformat()
    }

    # Best-effort: a missing/partial Chart of Accounts shouldn't block school creation,
    # but without it the Finance module has nothing to post fee payments against.
    try:
        seed_result = await seed_default_chart_of_accounts(session, response["id"], created_by=current_user.id)
        if not seed_result["success"]:
            logger.warning(f"CoA seeding had failures for new school {response['id']}: {seed_result['errors']}")
    except Exception as e:
        logger.error(f"Failed to seed default Chart of Accounts for new school {response['id']}: {e}")

    # Best-effort: without an open fiscal period, postings have nowhere to land until
    # a school_admin manually creates one via the Fiscal Periods tab.
    try:
        period_result = await seed_default_fiscal_periods(session, response["id"], created_by=current_user.id)
        if not period_result["success"]:
            logger.warning(f"Fiscal period seeding had failures for new school {response['id']}: {period_result['errors']}")
    except Exception as e:
        logger.error(f"Failed to seed default fiscal periods for new school {response['id']}: {e}")

    await log_event(
        session, actor=current_user, action="school.created", entity_type="school",
        entity_id=response["id"], school_id=response["id"],
        summary=f"{current_user.email} created school: {school.name} ({school.code})",
        ip_address=request.client.host if request.client else None,
    )

    return response


@router.get("/terms/list", response_model=dict)
async def list_all_academic_terms(
    academic_year: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """List academic terms for current user's school"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    query = select(AcademicTerm).where(AcademicTerm.school_id == school_id)
    
    if academic_year:
        query = query.where(AcademicTerm.academic_year == academic_year)
    
    query = query.order_by(AcademicTerm.start_date.desc())
    
    result = await session.execute(query)
    terms = result.scalars().all()
    
    return {
        "items": [
            {
                "id": t.id,
                "school_id": t.school_id,
                "academic_year": t.academic_year,
                "term": t.term,
                "start_date": t.start_date,
                "end_date": t.end_date,
                "is_current": t.is_current
            }
            for t in terms
        ]
    }


@router.get("", response_model=list[dict])
async def list_schools(
    is_active: Optional[bool] = None,
    school_type: Optional[SchoolType] = None,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """List all schools"""
    query = select(School)
    
    if is_active is not None:
        query = query.where(School.is_active == is_active)
    if school_type:
        query = query.where(School.school_type == school_type)
    
    if current_user.role != UserRole.SUPER_ADMIN:
        query = query.where(School.id == current_user.school_id)
    
    result = await session.execute(query)
    schools = result.scalars().all()
    
    return [
        {
            "id": s.id,
            "name": s.name,
            "code": s.code,
            "school_type": s.school_type,
            "address": s.address,
            "city": s.city,
            "region": s.region,
            "phone": s.phone,
            "email": s.email,
            "logo_url": s.logo_url,
            "motto": s.motto,
            "is_active": s.is_active,
            "enable_hostel": s.enable_hostel,
            "created_at": s.created_at.isoformat()
        }
        for s in schools
    ]


# ── Direct-settlement payout (Paystack subaccount) ──────────────────────────
# Where parent fee payments are sent. A school admin submits their own
# school's bank/MoMo details; a super admin verifies once, which creates a
# real Paystack subaccount — after that, payments route straight to the
# school's own bank/MoMo account with no /transfer step (and therefore no
# Paystack dashboard approval) on the platform's side at all.
# Deliberately verified by a super admin rather than the school itself
# (unlike staff, who a school's own admin verifies) — this is the platform
# deciding to route money away from its own pooled balance.
# Canteen top-ups use a second, independent subaccount — see the
# "Canteen direct-settlement payout" section further down.

def _serialize_school_payout_details(school: School) -> dict:
    return {
        "payout_account_type": school.payout_account_type,
        "payout_bank_code": school.payout_bank_code,
        "payout_account_number": school.payout_account_number,
        "payout_account_name": school.payout_account_name,
        "payout_verification_status": school.payout_verification_status.value if hasattr(school.payout_verification_status, "value") else str(school.payout_verification_status),
        "payout_submitted_at": school.payout_submitted_at.isoformat() if school.payout_submitted_at else None,
        "payout_verified_at": school.payout_verified_at.isoformat() if school.payout_verified_at else None,
        "payout_rejection_reason": school.payout_rejection_reason,
        "has_subaccount": bool(school.paystack_subaccount_code),
    }


@router.get("/payout-details", response_model=dict)
async def get_my_school_payout_details(
    current_user: User = Depends(require_roles(UserRole.SCHOOL_ADMIN)),
    _plan_check: User = Depends(require_plan_feature("fees_plus")),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(select(School).where(School.id == current_user.school_id))
    school = result.scalar_one_or_none()
    if not school:
        raise HTTPException(status_code=404, detail="School not found")
    return _serialize_school_payout_details(school)


@router.put("/payout-details", response_model=dict)
async def submit_my_school_payout_details(
    payload: dict,
    current_user: User = Depends(require_roles(UserRole.SCHOOL_ADMIN)),
    _plan_check: User = Depends(require_plan_feature("fees_plus")),
    session: AsyncSession = Depends(get_session),
):
    account_type = payload.get("account_type")
    bank_code = payload.get("bank_code")
    account_number = payload.get("account_number")
    if account_type not in ("bank", "mobile_money") or not bank_code or not account_number:
        raise HTTPException(status_code=400, detail="account_type (bank/mobile_money), bank_code, and account_number are required")

    result = await session.execute(select(School).where(School.id == current_user.school_id))
    school = result.scalar_one_or_none()
    if not school:
        raise HTTPException(status_code=404, detail="School not found")

    paystack_secret_key = os.getenv("PAYSTACK_SECRET_KEY", "")
    if not paystack_secret_key:
        raise HTTPException(status_code=503, detail="Payment gateway not configured")

    # The actual anti-fraud check — Paystack confirms who really owns this
    # account before we ever store it or show it to a super admin to "verify".
    paystack = PaystackService(paystack_secret_key)
    resolved = await paystack.resolve_account_number(account_number=account_number, bank_code=bank_code)
    if not resolved.get("success"):
        raise HTTPException(status_code=400, detail=resolved.get("error", "Could not verify this account number"))

    school.payout_account_type = account_type
    school.payout_bank_code = bank_code
    school.payout_account_number = account_number
    school.payout_account_name = resolved["account_name"]
    school.payout_verification_status = PayoutVerificationStatus.PENDING
    school.payout_submitted_at = datetime.utcnow()
    school.payout_rejection_reason = None
    school.paystack_subaccount_code = None  # re-submitting invalidates any prior subaccount
    school.updated_at = datetime.utcnow()
    session.add(school)
    await session.commit()
    await session.refresh(school)
    return _serialize_school_payout_details(school)


@router.get("/payout-details/pending", response_model=dict)
async def list_pending_school_payout_details(
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN)),
    _plan_check: User = Depends(require_plan_feature("fees_plus")),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(School).where(School.payout_verification_status == PayoutVerificationStatus.PENDING)
    )
    schools = result.scalars().all()
    return {
        "schools": [
            {"id": s.id, "name": s.name, "code": s.code, **_serialize_school_payout_details(s)}
            for s in schools
        ]
    }


@router.get("/{school_id}/payout-details", response_model=dict)
async def get_school_payout_details(
    school_id: str,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    _plan_check: User = Depends(require_plan_feature("fees_plus")),
    session: AsyncSession = Depends(get_session),
):
    if current_user.role == UserRole.SCHOOL_ADMIN and current_user.school_id != school_id:
        raise HTTPException(status_code=403, detail="Access denied")
    result = await session.execute(select(School).where(School.id == school_id))
    school = result.scalar_one_or_none()
    if not school:
        raise HTTPException(status_code=404, detail="School not found")
    return _serialize_school_payout_details(school)


@router.post("/{school_id}/payout-details/verify", response_model=dict)
async def verify_school_payout_details(
    school_id: str,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(select(School).where(School.id == school_id))
    school = result.scalar_one_or_none()
    if not school:
        raise HTTPException(status_code=404, detail="School not found")
    if school.payout_verification_status != PayoutVerificationStatus.PENDING:
        raise HTTPException(status_code=400, detail=f"Nothing to verify — status is {school.payout_verification_status}")

    paystack_secret_key = os.getenv("PAYSTACK_SECRET_KEY", "")
    if not paystack_secret_key:
        raise HTTPException(status_code=503, detail="Payment gateway not configured")

    paystack = PaystackService(paystack_secret_key)
    result_sub = await paystack.create_subaccount(
        business_name=school.name,
        settlement_bank=school.payout_bank_code,
        account_number=school.payout_account_number,
        percentage_charge=0,  # platform takes no per-transaction cut — the school gets the full amount
    )
    if not result_sub.get("success"):
        raise HTTPException(status_code=502, detail=result_sub.get("error", "Failed to create Paystack subaccount"))

    school.paystack_subaccount_code = result_sub["subaccount_code"]
    school.payout_verification_status = PayoutVerificationStatus.VERIFIED
    school.payout_verified_at = datetime.utcnow()
    school.payout_verified_by = current_user.id
    school.updated_at = datetime.utcnow()
    session.add(school)
    await session.commit()
    await session.refresh(school)

    await log_event(
        session, actor=current_user, action="school.payout_verified", entity_type="school",
        entity_id=school_id, school_id=school_id,
        summary=f"{current_user.email} verified direct-settlement payout for {school.name} ({school.code})",
    )

    return _serialize_school_payout_details(school)


@router.post("/{school_id}/payout-details/reject", response_model=dict)
async def reject_school_payout_details(
    school_id: str,
    payload: dict,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(select(School).where(School.id == school_id))
    school = result.scalar_one_or_none()
    if not school:
        raise HTTPException(status_code=404, detail="School not found")
    if school.payout_verification_status != PayoutVerificationStatus.PENDING:
        raise HTTPException(status_code=400, detail=f"Nothing to reject — status is {school.payout_verification_status}")

    school.payout_verification_status = PayoutVerificationStatus.REJECTED
    school.payout_rejection_reason = payload.get("reason")
    school.payout_verified_by = current_user.id
    school.updated_at = datetime.utcnow()
    session.add(school)
    await session.commit()
    await session.refresh(school)
    return _serialize_school_payout_details(school)


# ── Canteen direct-settlement payout (independent Paystack subaccount) ──────
# Same submit-once/verify-once mechanics as the fee payout above, but for a
# second, independent subaccount — a school's canteen is often run against
# its own bank/MoMo account (e.g. a canteen committee's account), distinct
# from the one that collects tuition. Canteen top-ups fall back to the
# pooled main balance until this is verified, same as fee payments do for
# the other subaccount.

def _serialize_canteen_payout_details(school: School) -> dict:
    return {
        "payout_account_type": school.canteen_payout_account_type,
        "payout_bank_code": school.canteen_payout_bank_code,
        "payout_account_number": school.canteen_payout_account_number,
        "payout_account_name": school.canteen_payout_account_name,
        "payout_verification_status": school.canteen_payout_verification_status.value if hasattr(school.canteen_payout_verification_status, "value") else str(school.canteen_payout_verification_status),
        "payout_submitted_at": school.canteen_payout_submitted_at.isoformat() if school.canteen_payout_submitted_at else None,
        "payout_verified_at": school.canteen_payout_verified_at.isoformat() if school.canteen_payout_verified_at else None,
        "payout_rejection_reason": school.canteen_payout_rejection_reason,
        "has_subaccount": bool(school.canteen_paystack_subaccount_code),
    }


@router.get("/canteen-payout-details", response_model=dict)
async def get_my_canteen_payout_details(
    current_user: User = Depends(require_roles(UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(select(School).where(School.id == current_user.school_id))
    school = result.scalar_one_or_none()
    if not school:
        raise HTTPException(status_code=404, detail="School not found")
    return _serialize_canteen_payout_details(school)


@router.put("/canteen-payout-details", response_model=dict)
async def submit_my_canteen_payout_details(
    payload: dict,
    current_user: User = Depends(require_roles(UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session),
):
    account_type = payload.get("account_type")
    bank_code = payload.get("bank_code")
    account_number = payload.get("account_number")
    if account_type not in ("bank", "mobile_money") or not bank_code or not account_number:
        raise HTTPException(status_code=400, detail="account_type (bank/mobile_money), bank_code, and account_number are required")

    result = await session.execute(select(School).where(School.id == current_user.school_id))
    school = result.scalar_one_or_none()
    if not school:
        raise HTTPException(status_code=404, detail="School not found")

    paystack_secret_key = os.getenv("PAYSTACK_SECRET_KEY", "")
    if not paystack_secret_key:
        raise HTTPException(status_code=503, detail="Payment gateway not configured")

    paystack = PaystackService(paystack_secret_key)
    resolved = await paystack.resolve_account_number(account_number=account_number, bank_code=bank_code)
    if not resolved.get("success"):
        raise HTTPException(status_code=400, detail=resolved.get("error", "Could not verify this account number"))

    school.canteen_payout_account_type = account_type
    school.canteen_payout_bank_code = bank_code
    school.canteen_payout_account_number = account_number
    school.canteen_payout_account_name = resolved["account_name"]
    school.canteen_payout_verification_status = PayoutVerificationStatus.PENDING
    school.canteen_payout_submitted_at = datetime.utcnow()
    school.canteen_payout_rejection_reason = None
    school.canteen_paystack_subaccount_code = None  # re-submitting invalidates any prior subaccount
    school.updated_at = datetime.utcnow()
    session.add(school)
    await session.commit()
    await session.refresh(school)
    return _serialize_canteen_payout_details(school)


@router.get("/canteen-payout-details/pending", response_model=dict)
async def list_pending_canteen_payout_details(
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(School).where(School.canteen_payout_verification_status == PayoutVerificationStatus.PENDING)
    )
    schools = result.scalars().all()
    return {
        "schools": [
            {"id": s.id, "name": s.name, "code": s.code, **_serialize_canteen_payout_details(s)}
            for s in schools
        ]
    }


@router.get("/{school_id}/canteen-payout-details", response_model=dict)
async def get_school_canteen_payout_details(
    school_id: str,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session),
):
    if current_user.role == UserRole.SCHOOL_ADMIN and current_user.school_id != school_id:
        raise HTTPException(status_code=403, detail="Access denied")
    result = await session.execute(select(School).where(School.id == school_id))
    school = result.scalar_one_or_none()
    if not school:
        raise HTTPException(status_code=404, detail="School not found")
    return _serialize_canteen_payout_details(school)


@router.post("/{school_id}/canteen-payout-details/verify", response_model=dict)
async def verify_school_canteen_payout_details(
    school_id: str,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(select(School).where(School.id == school_id))
    school = result.scalar_one_or_none()
    if not school:
        raise HTTPException(status_code=404, detail="School not found")
    if school.canteen_payout_verification_status != PayoutVerificationStatus.PENDING:
        raise HTTPException(status_code=400, detail=f"Nothing to verify — status is {school.canteen_payout_verification_status}")

    paystack_secret_key = os.getenv("PAYSTACK_SECRET_KEY", "")
    if not paystack_secret_key:
        raise HTTPException(status_code=503, detail="Payment gateway not configured")

    paystack = PaystackService(paystack_secret_key)
    result_sub = await paystack.create_subaccount(
        business_name=f"{school.name} Canteen",
        settlement_bank=school.canteen_payout_bank_code,
        account_number=school.canteen_payout_account_number,
        percentage_charge=0,
    )
    if not result_sub.get("success"):
        raise HTTPException(status_code=502, detail=result_sub.get("error", "Failed to create Paystack subaccount"))

    school.canteen_paystack_subaccount_code = result_sub["subaccount_code"]
    school.canteen_payout_verification_status = PayoutVerificationStatus.VERIFIED
    school.canteen_payout_verified_at = datetime.utcnow()
    school.canteen_payout_verified_by = current_user.id
    school.updated_at = datetime.utcnow()
    session.add(school)
    await session.commit()
    await session.refresh(school)

    await log_event(
        session, actor=current_user, action="school.canteen_payout_verified", entity_type="school",
        entity_id=school_id, school_id=school_id,
        summary=f"{current_user.email} verified canteen direct-settlement payout for {school.name} ({school.code})",
    )

    return _serialize_canteen_payout_details(school)


@router.post("/{school_id}/canteen-payout-details/reject", response_model=dict)
async def reject_school_canteen_payout_details(
    school_id: str,
    payload: dict,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(select(School).where(School.id == school_id))
    school = result.scalar_one_or_none()
    if not school:
        raise HTTPException(status_code=404, detail="School not found")
    if school.canteen_payout_verification_status != PayoutVerificationStatus.PENDING:
        raise HTTPException(status_code=400, detail=f"Nothing to reject — status is {school.canteen_payout_verification_status}")

    school.canteen_payout_verification_status = PayoutVerificationStatus.REJECTED
    school.canteen_payout_rejection_reason = payload.get("reason")
    school.canteen_payout_verified_by = current_user.id
    school.updated_at = datetime.utcnow()
    session.add(school)
    await session.commit()
    await session.refresh(school)
    return _serialize_canteen_payout_details(school)


@router.get("/{school_id}", response_model=dict)
async def get_school(
    school_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Get school details"""
    if current_user.role != UserRole.SUPER_ADMIN and current_user.school_id != school_id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    result = await session.execute(select(School).where(School.id == school_id))
    school = result.scalar_one_or_none()
    
    if not school:
        raise HTTPException(status_code=404, detail="School not found")
    
    return {
        "id": school.id,
        "name": school.name,
        "code": school.code,
        "school_type": school.school_type,
        "address": school.address,
        "city": school.city,
        "region": school.region,
        "phone": school.phone,
        "email": school.email,
        "logo_url": school.logo_url,
        "motto": school.motto,
        "is_active": school.is_active,
        "enable_hostel": school.enable_hostel,
        "require_maker_checker": school.require_maker_checker,
        "require_application_fee": school.require_application_fee,
        "application_fee_amount": school.application_fee_amount,
        "created_at": school.created_at.isoformat()
    }


@router.get("/{school_id}/detail", response_model=dict)
async def get_school_detail(
    school_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """School info plus every user account at that school, with roles.
    Super admin can view any school; a school admin can only view their own."""
    if current_user.role != UserRole.SUPER_ADMIN and current_user.school_id != school_id:
        raise HTTPException(status_code=403, detail="Access denied")

    result = await session.execute(select(School).where(School.id == school_id))
    school = result.scalar_one_or_none()
    if not school:
        raise HTTPException(status_code=404, detail="School not found")

    users_result = await session.execute(
        select(User).where(User.school_id == school_id).order_by(User.role, User.first_name)
    )
    users = users_result.scalars().all()

    role_counts: dict[str, int] = {}
    for u in users:
        role_value = u.role.value if hasattr(u.role, "value") else str(u.role)
        role_counts[role_value] = role_counts.get(role_value, 0) + 1

    return {
        "school": {
            "id": school.id,
            "name": school.name,
            "code": school.code,
            "school_type": school.school_type,
            "address": school.address,
            "city": school.city,
            "region": school.region,
            "phone": school.phone,
            "email": school.email,
            "motto": school.motto,
            "is_active": school.is_active,
            "enable_hostel": school.enable_hostel,
            "created_at": school.created_at.isoformat(),
        },
        "role_counts": role_counts,
        "users": [
            {
                "id": u.id,
                "email": u.email,
                "first_name": u.first_name,
                "last_name": u.last_name,
                "phone": u.phone,
                "role": u.role.value if hasattr(u.role, "value") else str(u.role),
                "is_active": u.is_active,
                "created_at": u.created_at.isoformat(),
                "last_login": u.last_login.isoformat() if u.last_login else None,
            }
            for u in users
        ],
    }


@router.delete("/{school_id}", response_model=dict)
async def delete_school(
    school_id: str,
    request: Request,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Remove a school. Super admin only. This is a soft delete (is_active=False)
    — the school and every record referencing it (students, staff, fees,
    grades, ...) stay in place, since there's no DB-level cascade to safely
    hard-delete against. A deactivated school disappears from active lists
    but its historical data remains intact."""
    result = await session.execute(select(School).where(School.id == school_id))
    school = result.scalar_one_or_none()
    if not school:
        raise HTTPException(status_code=404, detail="School not found")

    if not school.is_active:
        raise HTTPException(status_code=400, detail="School is already removed")

    school.is_active = False
    school.updated_at = datetime.utcnow()
    session.add(school)
    await session.commit()

    await log_event(
        session, actor=current_user, action="school.deleted", entity_type="school",
        entity_id=school_id, school_id=school_id,
        summary=f"{current_user.email} removed school: {school.name} ({school.code})",
        old_values={"is_active": True},
        ip_address=request.client.host if request.client else None,
    )

    return {"message": f"School {school.name} removed"}


class UpdateSchoolAccessRequest(SQLModel):
    suspended: bool
    reason: Optional[str] = None


@router.put("/{school_id}/access", response_model=dict)
async def update_school_access(
    school_id: str,
    body: UpdateSchoolAccessRequest,
    request: Request,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Suspend or restore a school's access to every module (billing enforcement).
    Enforced centrally in auth.py::get_current_user — every non-super-admin
    request for this school's users gets blocked while suspended is True."""
    result = await session.execute(select(School).where(School.id == school_id))
    school = result.scalar_one_or_none()
    if not school:
        raise HTTPException(status_code=404, detail="School not found")

    was_suspended = school.access_suspended
    school.access_suspended = body.suspended
    school.access_suspended_reason = body.reason if body.suspended else None
    school.access_suspended_at = datetime.utcnow() if body.suspended else None
    school.updated_at = datetime.utcnow()
    session.add(school)
    await session.commit()

    action = "school.access_suspended" if body.suspended else "school.access_restored"
    summary = (
        f"{current_user.email} suspended access for {school.name} ({school.code})"
        + (f" — {body.reason}" if body.reason else "")
    ) if body.suspended else f"{current_user.email} restored access for {school.name} ({school.code})"

    await log_event(
        session, actor=current_user, action=action, entity_type="school",
        entity_id=school_id, school_id=school_id, summary=summary,
        old_values={"access_suspended": was_suspended}, new_values={"access_suspended": body.suspended},
        ip_address=request.client.host if request.client else None,
    )

    return {
        "message": f"School {school.name} access {'suspended' if body.suspended else 'restored'}",
        "access_suspended": school.access_suspended,
    }


# Academic Terms

def require_unlocked_term(term: AcademicTerm) -> None:
    """Raise 423 if this term is locked. Shared by every write path that
    touches a term or a record scoped to one (grades, attendance, fees,
    assignments), plus the term CRUD itself, so a historical term stays
    genuinely historical."""
    if term.is_locked:
        raise HTTPException(status_code=423, detail="This academic term is locked")


async def _validate_academic_year(session: AsyncSession, school_id: str, academic_year_id: Optional[str]) -> None:
    """If an academic_year_id is provided, it must belong to this school —
    mirrors the same check in routers/academic_calendar.py's create_calendar_event."""
    if not academic_year_id:
        return
    result = await session.execute(
        select(AcademicYear).where(AcademicYear.id == academic_year_id, AcademicYear.school_id == school_id)
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="academic_year_id does not belong to this school")


@router.post("/{school_id}/terms", response_model=dict)
async def create_academic_term(
    school_id: str,
    term_data: AcademicTermCreate,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Create a new academic term"""
    if current_user.role == UserRole.SCHOOL_ADMIN and current_user.school_id != school_id:
        raise HTTPException(status_code=403, detail="Access denied")

    await _validate_academic_year(session, school_id, term_data.academic_year_id)

    if term_data.is_current:
        result = await session.execute(
            select(AcademicTerm).where(
                AcademicTerm.school_id == school_id,
                AcademicTerm.is_current == True
            )
        )
        for term in result.scalars().all():
            term.is_current = False
            session.add(term)
    
    term = AcademicTerm(school_id=school_id, **term_data.model_dump())
    session.add(term)
    await session.commit()
    await session.refresh(term)
    
    return {
        "id": term.id,
        "school_id": term.school_id,
        "academic_year_id": term.academic_year_id,
        "academic_year": term.academic_year,
        "term": term.term,
        "start_date": term.start_date,
        "end_date": term.end_date,
        "is_current": term.is_current,
        "is_locked": term.is_locked
    }


@router.get("/{school_id}/terms", response_model=list[dict])
async def list_academic_terms(
    school_id: str,
    academic_year: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """List academic terms for a school"""
    if current_user.role != UserRole.SUPER_ADMIN and current_user.school_id != school_id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    query = select(AcademicTerm).where(AcademicTerm.school_id == school_id)
    
    if academic_year:
        query = query.where(AcademicTerm.academic_year == academic_year)
    
    query = query.order_by(AcademicTerm.start_date.desc())
    
    result = await session.execute(query)
    terms = result.scalars().all()
    
    return [
        {
            "id": t.id,
            "school_id": t.school_id,
            "academic_year_id": t.academic_year_id,
            "academic_year": t.academic_year,
            "term": t.term,
            "start_date": t.start_date,
            "end_date": t.end_date,
            "is_current": t.is_current,
            "is_locked": t.is_locked
        }
        for t in terms
    ]


@router.get("/{school_id}/terms/current", response_model=dict)
async def get_current_term(
    school_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Get current academic term"""
    if current_user.role != UserRole.SUPER_ADMIN and current_user.school_id != school_id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    result = await session.execute(
        select(AcademicTerm).where(
            AcademicTerm.school_id == school_id,
            AcademicTerm.is_current == True
        )
    )
    term = result.scalar_one_or_none()
    
    if not term:
        raise HTTPException(status_code=404, detail="No current term set")

    return {
        "id": term.id,
        "school_id": term.school_id,
        "academic_year_id": term.academic_year_id,
        "academic_year": term.academic_year,
        "term": term.term,
        "start_date": term.start_date,
        "end_date": term.end_date,
        "is_current": term.is_current,
        "is_locked": term.is_locked
    }


@router.put("/{school_id}/terms/{term_id}/set-current", response_model=dict)
async def set_current_term(
    school_id: str,
    term_id: str,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Set a term as current"""
    if current_user.role == UserRole.SCHOOL_ADMIN and current_user.school_id != school_id:
        raise HTTPException(status_code=403, detail="Access denied")

    # Validate the target term before mutating anything else.
    result = await session.execute(
        select(AcademicTerm).where(
            AcademicTerm.id == term_id,
            AcademicTerm.school_id == school_id
        )
    )
    term = result.scalar_one_or_none()

    if not term:
        raise HTTPException(status_code=404, detail="Term not found")
    require_unlocked_term(term)

    # Unset all current terms
    result = await session.execute(
        select(AcademicTerm).where(
            AcademicTerm.school_id == school_id,
            AcademicTerm.is_current == True
        )
    )
    for other in result.scalars().all():
        other.is_current = False
        session.add(other)

    term.is_current = True
    session.add(term)
    await session.commit()

    return {"message": "Current term updated successfully"}


# School Update
@router.put("/{school_id}", response_model=dict)
async def update_school(
    school_id: str,
    school_data: SchoolUpdate,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Update school details"""
    if current_user.role == UserRole.SCHOOL_ADMIN and current_user.school_id != school_id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    result = await session.execute(select(School).where(School.id == school_id))
    school = result.scalar_one_or_none()
    
    if not school:
        raise HTTPException(status_code=404, detail="School not found")
    
    # Update only provided fields
    update_data = school_data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(school, field, value)
    
    school.updated_at = datetime.utcnow()
    session.add(school)
    await session.commit()
    await session.refresh(school)
    
    return {
        "id": school.id,
        "name": school.name,
        "code": school.code,
        "school_type": school.school_type,
        "address": school.address,
        "city": school.city,
        "region": school.region,
        "phone": school.phone,
        "email": school.email,
        "logo_url": school.logo_url,
        "motto": school.motto,
        "is_active": school.is_active,
        "enable_hostel": school.enable_hostel,
        "require_maker_checker": school.require_maker_checker,
        "require_application_fee": school.require_application_fee,
        "application_fee_amount": school.application_fee_amount,
        "created_at": school.created_at.isoformat(),
        "updated_at": school.updated_at.isoformat()
    }


@router.post("/{school_id}/logo", response_model=dict)
async def upload_school_logo(
    school_id: str,
    file: UploadFile = File(...),
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Upload a school logo image, replacing any existing logo_url (which
    used to be a plain paste-a-URL text field) with a locally-stored file
    served back via the /uploads static mount."""
    if current_user.role == UserRole.SCHOOL_ADMIN and current_user.school_id != school_id:
        raise HTTPException(status_code=403, detail="Access denied")

    result = await session.execute(select(School).where(School.id == school_id))
    school = result.scalar_one_or_none()
    if not school:
        raise HTTPException(status_code=404, detail="School not found")

    if file.content_type not in ALLOWED_LOGO_CONTENT_TYPES:
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {file.content_type}. Allowed: JPEG, PNG, WEBP, SVG.")

    content = await file.read()
    if len(content) > MAX_LOGO_SIZE_BYTES:
        raise HTTPException(status_code=400, detail="Logo file exceeds the 2 MB limit.")

    if file.content_type == "image/svg+xml":
        content = sanitize_svg(content)

    filename = f"{uuid.uuid4()}_{Path(file.filename or 'logo').name}"
    target_dir = LOGO_UPLOAD_DIR / school.id
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / filename).write_bytes(content)

    school.logo_url = f"/uploads/school_logos/{school.id}/{filename}"
    school.updated_at = datetime.utcnow()
    session.add(school)
    await session.commit()

    return {"logo_url": school.logo_url}


# Academic Term Update
@router.put("/{school_id}/terms/{term_id}", response_model=dict)
async def update_academic_term(
    school_id: str,
    term_id: str,
    term_data: AcademicTermUpdate,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Update an academic term"""
    if current_user.role == UserRole.SCHOOL_ADMIN and current_user.school_id != school_id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    result = await session.execute(
        select(AcademicTerm).where(
            AcademicTerm.id == term_id,
            AcademicTerm.school_id == school_id
        )
    )
    term = result.scalar_one_or_none()

    if not term:
        raise HTTPException(status_code=404, detail="Term not found")
    require_unlocked_term(term)
    await _validate_academic_year(session, school_id, term_data.academic_year_id)

    # If setting is_current to True, unset other current terms
    if term_data.is_current:
        result = await session.execute(
            select(AcademicTerm).where(
                AcademicTerm.school_id == school_id,
                AcademicTerm.is_current == True
            )
        )
        for t in result.scalars().all():
            t.is_current = False
            session.add(t)
    
    # Update only provided fields
    update_data = term_data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(term, field, value)
    
    session.add(term)
    await session.commit()
    await session.refresh(term)
    
    return {
        "id": term.id,
        "school_id": term.school_id,
        "academic_year_id": term.academic_year_id,
        "academic_year": term.academic_year,
        "term": term.term,
        "start_date": term.start_date,
        "end_date": term.end_date,
        "is_current": term.is_current,
        "is_locked": term.is_locked
    }


# Tables that get permanently deleted (CASCADE) when an academic term is deleted.
TERM_CASCADE_TABLES = [
    ("assignments", "assignments"),
    ("learning_materials", "learning materials"),
    ("attendance", "attendance records"),
    ("class_subjects", "class-subject assignments"),
    ("grades", "grades"),
    ("report_cards", "report cards"),
    ("teacher_assignments", "teacher assignments"),
    ("timetables", "timetable entries"),
]

# Tables that just get unlinked (SET NULL) — the records themselves are kept.
TERM_PRESERVED_TABLES = [
    ("classes", "classes"),
    ("fees", "fee records"),
    ("fee_structures", "fee structures"),
    ("platform_subscriptions", "billing/subscription records"),
]


@router.get("/{school_id}/terms/{term_id}/delete-impact", response_model=dict)
async def get_term_delete_impact(
    school_id: str,
    term_id: str,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Preview what deleting this academic term would affect, before actually deleting it.

    Academic/operational data (grades, attendance, assignments, etc.) is permanently
    deleted along with the term. Financial data (fees, billing records) is never deleted —
    it's preserved with its term reference cleared.
    """
    if current_user.role == UserRole.SCHOOL_ADMIN and current_user.school_id != school_id:
        raise HTTPException(status_code=403, detail="Access denied")

    result = await session.execute(
        select(AcademicTerm).where(
            AcademicTerm.id == term_id,
            AcademicTerm.school_id == school_id
        )
    )
    term = result.scalar_one_or_none()
    if not term:
        raise HTTPException(status_code=404, detail="Term not found")

    will_be_deleted = {}
    for table, label in TERM_CASCADE_TABLES:
        count_result = await session.execute(
            text(f"SELECT count(*) FROM {table} WHERE academic_term_id = :tid"),
            {"tid": term_id}
        )
        count = count_result.scalar()
        if count:
            will_be_deleted[label] = count

    will_be_preserved = {}
    for table, label in TERM_PRESERVED_TABLES:
        count_result = await session.execute(
            text(f"SELECT count(*) FROM {table} WHERE academic_term_id = :tid"),
            {"tid": term_id}
        )
        count = count_result.scalar()
        if count:
            will_be_preserved[label] = count

    return {
        "term_id": term_id,
        "will_be_deleted": will_be_deleted,
        "total_to_be_deleted": sum(will_be_deleted.values()),
        "will_be_preserved": will_be_preserved,
    }


# Academic Term Delete
@router.delete("/{school_id}/terms/{term_id}", response_model=dict)
async def delete_academic_term(
    school_id: str,
    term_id: str,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Delete an academic term.

    Academic/operational records tied to this term (grades, attendance, assignments,
    report cards, timetables, teacher assignments, class-subject links) are permanently
    deleted along with it via DB-level CASCADE. Financial records (fees, fee structures,
    billing/subscription records) are never deleted — they're preserved with their term
    reference cleared via DB-level SET NULL. Call GET .../delete-impact first to show the
    user what this will affect before they confirm.
    """
    if current_user.role == UserRole.SCHOOL_ADMIN and current_user.school_id != school_id:
        raise HTTPException(status_code=403, detail="Access denied")

    result = await session.execute(
        select(AcademicTerm).where(
            AcademicTerm.id == term_id,
            AcademicTerm.school_id == school_id
        )
    )
    term = result.scalar_one_or_none()

    if not term:
        raise HTTPException(status_code=404, detail="Term not found")
    if term.is_locked:
        raise HTTPException(status_code=423, detail="This academic term is locked and cannot be deleted — unlock it first if you're certain")

    await session.delete(term)
    await session.commit()

    return {"message": "Academic term deleted successfully"}


@router.put("/{school_id}/terms/{term_id}/lock", response_model=dict)
async def lock_academic_term(
    school_id: str,
    term_id: str,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Lock a term: its dates/is_current can no longer be edited, it can't be
    deleted, and grades/attendance/assignments/fees can no longer be written
    against it. Historical data protection — see require_unlocked_term above."""
    if current_user.role == UserRole.SCHOOL_ADMIN and current_user.school_id != school_id:
        raise HTTPException(status_code=403, detail="Access denied")

    result = await session.execute(
        select(AcademicTerm).where(AcademicTerm.id == term_id, AcademicTerm.school_id == school_id)
    )
    term = result.scalar_one_or_none()
    if not term:
        raise HTTPException(status_code=404, detail="Term not found")

    term.is_locked = True
    session.add(term)
    await session.commit()

    return {"message": "Academic term locked", "id": term.id, "is_locked": True}


@router.put("/{school_id}/terms/{term_id}/unlock", response_model=dict)
async def unlock_academic_term(
    school_id: str,
    term_id: str,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Reopen a locked term for corrections. Deliberately as easy to call as
    lock — this is a judgment call for the admin, not something the system
    should get in the way of, but it's a separate explicit action so it's
    never accidental."""
    if current_user.role == UserRole.SCHOOL_ADMIN and current_user.school_id != school_id:
        raise HTTPException(status_code=403, detail="Access denied")

    result = await session.execute(
        select(AcademicTerm).where(AcademicTerm.id == term_id, AcademicTerm.school_id == school_id)
    )
    term = result.scalar_one_or_none()
    if not term:
        raise HTTPException(status_code=404, detail="Term not found")

    term.is_locked = False
    session.add(term)
    await session.commit()

    return {"message": "Academic term unlocked", "id": term.id, "is_locked": False}


@router.post("/{school_id}/terms/{term_id}/close", response_model=dict)
async def close_academic_term(
    school_id: str,
    term_id: str,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Explicit term-closing workflow: stop it being the current term (if it
    was) and lock it in one step, distinct from set-current (opening) and
    from delete (which destroys data instead of preserving it)."""
    if current_user.role == UserRole.SCHOOL_ADMIN and current_user.school_id != school_id:
        raise HTTPException(status_code=403, detail="Access denied")

    result = await session.execute(
        select(AcademicTerm).where(AcademicTerm.id == term_id, AcademicTerm.school_id == school_id)
    )
    term = result.scalar_one_or_none()
    if not term:
        raise HTTPException(status_code=404, detail="Term not found")

    term.is_current = False
    term.is_locked = True
    session.add(term)
    await session.commit()

    return {"message": "Academic term closed", "id": term.id, "is_current": False, "is_locked": True}
