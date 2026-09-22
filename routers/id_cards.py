"""ID Card Generation Router"""
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse
from sqlmodel import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime
from typing import Optional, List
import secrets

from models.certificates import (
    IDCard, IDCardCreate, IDCardUpdate, IDCardDistribute, BulkIDCardRequest,
    PersonType, IDCardStatus,
)
from models.student import Student
from models.staff import Staff
from models.security import AuthorizedPickupPerson
from models.user import User, UserRole
from database import get_session
from auth import get_current_user, require_roles
from services.certificate_pdf_service import CertificatePDFService
from services.plan_gating import require_plan_feature

router = APIRouter(
    prefix="/id-cards", tags=["ID Cards"],
    dependencies=[Depends(require_plan_feature("id_cards"))],
)

WRITE_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.HR)

_PERSON_MODEL = {
    PersonType.STUDENT: Student,
    PersonType.STAFF: Staff,
    PersonType.PICKUP_PERSON: AuthorizedPickupPerson,
}


async def _get_person(session: AsyncSession, school_id: str, person_type: PersonType, person_id: str):
    model = _PERSON_MODEL[person_type]
    result = await session.execute(select(model).where(and_(model.id == person_id, model.school_id == school_id)))
    return result.scalar_one_or_none()


def _person_name(person_type: PersonType, person) -> str:
    if person_type == PersonType.PICKUP_PERSON:
        return person.name
    return f"{person.first_name} {person.last_name}"


def _person_id_code(person_type: PersonType, person) -> Optional[str]:
    if person_type == PersonType.STUDENT:
        return person.student_id
    if person_type == PersonType.STAFF:
        return person.staff_id
    # Pickup persons have no admission/staff number — show the relationship
    # to the student instead (e.g. "Uncle", "Driver"), falling back to phone.
    return person.relationship or person.phone


def _person_photo(person_type: PersonType, person) -> Optional[str]:
    return getattr(person, "photo_url", None)


_PERSON_TYPE_LABEL = {
    PersonType.STUDENT: "Student",
    PersonType.STAFF: "Staff",
    PersonType.PICKUP_PERSON: "Pickup Person",
}


def _new_card(school_id: str, person_type: PersonType, person_id: str, expiry_date: Optional[str]) -> IDCard:
    card_number = f"ID-{person_type.value[:3].upper()}-{secrets.token_hex(4).upper()}"
    return IDCard(
        school_id=school_id,
        person_type=person_type,
        person_id=person_id,
        card_number=card_number,
        issue_date=datetime.utcnow().strftime("%Y-%m-%d"),
        expiry_date=expiry_date,
        status=IDCardStatus.ACTIVE,
        qr_payload=card_number,
    )


@router.post("/{person_type}/{person_id}/generate", response_model=dict)
async def generate_id_card(
    person_type: PersonType,
    person_id: str,
    data: IDCardCreate = IDCardCreate(),
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    """Issue a new ID card for a student, staff member, or authorized pickup person"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    person = await _get_person(session, school_id, person_type, person_id)
    if not person:
        raise HTTPException(status_code=404, detail=f"{_PERSON_TYPE_LABEL[person_type]} not found")

    card = _new_card(school_id, person_type, person_id, data.expiry_date)
    session.add(card)
    await session.commit()
    await session.refresh(card)

    return jsonable_encoder(card)


@router.post("/bulk-generate", response_model=dict)
async def bulk_generate_id_cards(
    data: BulkIDCardRequest,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    """Issue cards for many people of the same type in one call — e.g. a
    whole class at enrollment, or every currently-active staff member.
    A person who already holds an ACTIVE card for their type is skipped
    rather than double-issued; use the reissue-via-status-update flow
    (PUT /{card_id} → status=REISSUED, then generate again) for a
    deliberate replacement instead."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    if not data.person_ids:
        raise HTTPException(status_code=400, detail="person_ids cannot be empty")

    existing_result = await session.execute(
        select(IDCard.person_id).where(
            IDCard.school_id == school_id,
            IDCard.person_type == data.person_type,
            IDCard.person_id.in_(data.person_ids),
            IDCard.status == IDCardStatus.ACTIVE,
        )
    )
    already_has_card = set(existing_result.scalars().all())

    created: List[dict] = []
    skipped: List[str] = []
    not_found: List[str] = []
    for person_id in data.person_ids:
        if person_id in already_has_card:
            skipped.append(person_id)
            continue
        person = await _get_person(session, school_id, data.person_type, person_id)
        if not person:
            not_found.append(person_id)
            continue
        card = _new_card(school_id, data.person_type, person_id, data.expiry_date)
        session.add(card)
        created.append(card)

    await session.commit()
    for card in created:
        await session.refresh(card)

    return {
        "created": [jsonable_encoder(c) for c in created],
        "created_count": len(created),
        "skipped_already_has_card": skipped,
        "not_found": not_found,
    }


@router.get("/", response_model=List[dict])
async def list_id_cards(
    person_type: Optional[PersonType] = None,
    status: Optional[IDCardStatus] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    query = select(IDCard).where(IDCard.school_id == school_id)
    if person_type:
        query = query.where(IDCard.person_type == person_type)
    if status:
        query = query.where(IDCard.status == status)
    query = query.order_by(IDCard.created_at.desc()).offset(skip).limit(limit)

    result = await session.execute(query)
    return [jsonable_encoder(c) for c in result.scalars().all()]


@router.get("/verify/{card_number}", response_model=dict)
async def verify_id_card(
    card_number: str,
    current_user: User = Depends(require_roles(*WRITE_ROLES, UserRole.SECURITY_OFFICER)),
    session: AsyncSession = Depends(get_session)
):
    """Gate lookup: resolve a card number to person details, checking status"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(IDCard).where(and_(IDCard.card_number == card_number, IDCard.school_id == school_id))
    )
    card = result.scalar_one_or_none()
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")

    person = await _get_person(session, school_id, card.person_type, card.person_id)
    if not person:
        raise HTTPException(status_code=404, detail="Card holder not found")

    return {
        "card_number": card.card_number,
        "status": card.status.value,
        "is_active": card.status == IDCardStatus.ACTIVE,
        "person_type": card.person_type.value,
        "person_name": _person_name(card.person_type, person),
        "person_id_code": _person_id_code(card.person_type, person),
    }


@router.get("/{card_id}", response_model=dict)
async def get_id_card(
    card_id: str,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(select(IDCard).where(and_(IDCard.id == card_id, IDCard.school_id == school_id)))
    card = result.scalar_one_or_none()
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")
    return jsonable_encoder(card)


@router.put("/{card_id}", response_model=dict)
async def update_id_card(
    card_id: str,
    data: IDCardUpdate,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    """Update card status (e.g. LOST, REVOKED, REISSUED) or expiry"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(select(IDCard).where(and_(IDCard.id == card_id, IDCard.school_id == school_id)))
    card = result.scalar_one_or_none()
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")

    update_data = data.dict(exclude_unset=True)
    for key, value in update_data.items():
        setattr(card, key, value)
    card.updated_at = datetime.utcnow()

    session.add(card)
    await session.commit()
    await session.refresh(card)
    return jsonable_encoder(card)


@router.post("/{card_id}/distribute", response_model=dict)
async def distribute_id_card(
    card_id: str,
    data: IDCardDistribute = IDCardDistribute(),
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    """Mark a printed card as handed to its holder — a dedicated action
    (not a generic IDCardUpdate field) so distribution stays an auditable
    event: who marked it, when, and any note (e.g. "collected by mother")."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(select(IDCard).where(and_(IDCard.id == card_id, IDCard.school_id == school_id)))
    card = result.scalar_one_or_none()
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")

    card.distributed_at = datetime.utcnow()
    card.distributed_by = current_user.id
    card.distributed_note = data.note
    card.updated_at = datetime.utcnow()

    session.add(card)
    await session.commit()
    await session.refresh(card)
    return jsonable_encoder(card)


@router.delete("/{card_id}", response_model=dict)
async def delete_id_card(
    card_id: str,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(select(IDCard).where(and_(IDCard.id == card_id, IDCard.school_id == school_id)))
    card = result.scalar_one_or_none()
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")

    await session.delete(card)
    await session.commit()
    return {"message": "Card deleted successfully", "id": card_id}


@router.get("/{card_id}/pdf")
async def get_id_card_pdf(
    card_id: str,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    """Download a printable version of the ID card"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(select(IDCard).where(and_(IDCard.id == card_id, IDCard.school_id == school_id)))
    card = result.scalar_one_or_none()
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")

    person = await _get_person(session, school_id, card.person_type, card.person_id)
    if not person:
        raise HTTPException(status_code=404, detail="Card holder not found")

    from models.school import School
    school_result = await session.execute(select(School).where(School.id == school_id))
    school = school_result.scalar_one_or_none()

    service = CertificatePDFService()

    data = {
        "school_name": school.name if school else "School",
        "person_type": _PERSON_TYPE_LABEL[card.person_type],
        "person_name": _person_name(card.person_type, person),
        "person_id_code": _person_id_code(card.person_type, person),
        "issue_date": card.issue_date,
        "expiry_date": card.expiry_date,
        "status": card.status.value.capitalize(),
        "card_number": card.card_number,
        "qr_image_data_uri": service.generate_qr_data_uri(card.qr_payload),
        "photo_data_uri": service.resolve_local_image_data_uri(_person_photo(card.person_type, person)),
    }

    try:
        pdf_bytes = service.generate_pdf("id_card.html", data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to generate ID card PDF: {str(e)}")

    safe_name = data["person_name"].replace(" ", "_")
    filename = f"id_card_{safe_name}.pdf"

    return StreamingResponse(
        iter([pdf_bytes]),
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )
