"""Health / Clinic Router"""
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from sqlmodel import select, and_, func
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime
from typing import Optional, List

from models.health import (
    StudentHealthProfile, StudentHealthProfileCreate, StudentHealthProfileUpdate,
    ClinicVisit, ClinicVisitCreate, ClinicVisitUpdate, VisitApprovalStatus, RejectVisitRequest,
    ImmunizationRecord, ImmunizationRecordCreate, ImmunizationRecordUpdate,
    MedicationAdministration, MedicationAdministrationCreate, MedicationAdministrationUpdate,
    HealthIncident, HealthIncidentCreate, HealthIncidentUpdate,
    HealthScreeningCampaign, HealthScreeningCampaignCreate, HealthScreeningCampaignUpdate,
    HealthScreeningResult, HealthScreeningResultCreate, HealthScreeningResultBulkCreate,
)
from models.school import School
from models.student import Student
from models.user import User, UserRole
from database import get_session
from auth import get_current_user, require_roles

router = APIRouter(prefix="/health", tags=["Health"])

WRITE_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.HR, UserRole.NURSE)


async def _requires_maker_checker(session: AsyncSession, school_id: str) -> bool:
    """Whether this school has segregation-of-duties enabled

    Off by default (School.require_maker_checker) — mirrors
    services/journal_entry_service.py::requires_maker_checker exactly.
    """
    result = await session.execute(
        select(School.require_maker_checker).where(School.id == school_id)
    )
    return bool(result.scalar_one_or_none())


# ============================================================================
# HEALTH PROFILES
# ============================================================================

@router.get("/profiles", response_model=List[dict])
async def list_health_profiles(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    query = select(StudentHealthProfile).where(StudentHealthProfile.school_id == school_id)
    query = query.offset(skip).limit(limit)
    result = await session.execute(query)
    return [jsonable_encoder(r) for r in result.scalars().all()]


@router.post("/profiles", response_model=dict)
async def create_health_profile(
    data: StudentHealthProfileCreate,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    existing = await session.execute(
        select(StudentHealthProfile).where(
            and_(StudentHealthProfile.student_id == data.student_id, StudentHealthProfile.school_id == school_id)
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="A health profile already exists for this student")

    profile = StudentHealthProfile(**data.dict(), school_id=school_id)
    session.add(profile)
    await session.commit()
    await session.refresh(profile)
    return jsonable_encoder(profile)


@router.get("/profiles/by-student/{student_id}", response_model=dict)
async def get_health_profile_by_student(
    student_id: str,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(StudentHealthProfile).where(
            and_(StudentHealthProfile.student_id == student_id, StudentHealthProfile.school_id == school_id)
        )
    )
    profile = result.scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=404, detail="No health profile found for this student")
    return jsonable_encoder(profile)


@router.put("/profiles/{profile_id}", response_model=dict)
async def update_health_profile(
    profile_id: str,
    data: StudentHealthProfileUpdate,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(StudentHealthProfile).where(
            and_(StudentHealthProfile.id == profile_id, StudentHealthProfile.school_id == school_id)
        )
    )
    profile = result.scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=404, detail="Health profile not found")

    update_data = data.dict(exclude_unset=True)
    for key, value in update_data.items():
        setattr(profile, key, value)
    profile.updated_at = datetime.utcnow()

    session.add(profile)
    await session.commit()
    await session.refresh(profile)
    return jsonable_encoder(profile)


@router.delete("/profiles/{profile_id}", response_model=dict)
async def delete_health_profile(
    profile_id: str,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(StudentHealthProfile).where(
            and_(StudentHealthProfile.id == profile_id, StudentHealthProfile.school_id == school_id)
        )
    )
    profile = result.scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=404, detail="Health profile not found")

    await session.delete(profile)
    await session.commit()
    return {"message": "Health profile deleted successfully", "id": profile_id}


# ============================================================================
# CLINIC VISITS
# ============================================================================

@router.get("/visits", response_model=List[dict])
async def list_clinic_visits(
    student_id: Optional[str] = None,
    approval_status: Optional[VisitApprovalStatus] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    query = select(ClinicVisit).where(ClinicVisit.school_id == school_id)
    if student_id:
        query = query.where(ClinicVisit.student_id == student_id)
    if approval_status:
        query = query.where(ClinicVisit.approval_status == approval_status)
    query = query.order_by(ClinicVisit.created_at.desc()).offset(skip).limit(limit)

    result = await session.execute(query)
    return [jsonable_encoder(r) for r in result.scalars().all()]


@router.post("/visits", response_model=dict)
async def create_clinic_visit(
    data: ClinicVisitCreate,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    """Log a clinic visit. If the school has maker-checker enabled
    (School.require_maker_checker), the visit is logged PENDING and needs
    sign-off from a different staff member via POST /visits/{id}/approve.
    Off by default, in which case it's auto-approved immediately — same as
    this module's original behavior."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    maker_checker = await _requires_maker_checker(session, school_id)
    visit = ClinicVisit(
        **data.dict(),
        school_id=school_id,
        created_by=current_user.id,
        approval_status=VisitApprovalStatus.PENDING if maker_checker else VisitApprovalStatus.APPROVED,
        approved_by=None if maker_checker else current_user.id,
        approved_at=None if maker_checker else datetime.utcnow(),
    )
    session.add(visit)
    await session.commit()
    await session.refresh(visit)
    return jsonable_encoder(visit)


@router.post("/visits/{visit_id}/approve", response_model=dict)
async def approve_clinic_visit(
    visit_id: str,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(ClinicVisit).where(and_(ClinicVisit.id == visit_id, ClinicVisit.school_id == school_id))
    )
    visit = result.scalar_one_or_none()
    if not visit:
        raise HTTPException(status_code=404, detail="Clinic visit not found")
    if visit.approval_status != VisitApprovalStatus.PENDING:
        raise HTTPException(status_code=400, detail=f"Cannot approve a visit with status {visit.approval_status.value}")

    if visit.created_by == current_user.id and await _requires_maker_checker(session, school_id):
        raise HTTPException(status_code=403, detail="Segregation of duties: you logged this visit and cannot also approve it")

    visit.approval_status = VisitApprovalStatus.APPROVED
    visit.approved_by = current_user.id
    visit.approved_at = datetime.utcnow()
    session.add(visit)
    await session.commit()
    await session.refresh(visit)
    return jsonable_encoder(visit)


@router.post("/visits/{visit_id}/reject", response_model=dict)
async def reject_clinic_visit(
    visit_id: str,
    data: RejectVisitRequest,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(ClinicVisit).where(and_(ClinicVisit.id == visit_id, ClinicVisit.school_id == school_id))
    )
    visit = result.scalar_one_or_none()
    if not visit:
        raise HTTPException(status_code=404, detail="Clinic visit not found")
    if visit.approval_status != VisitApprovalStatus.PENDING:
        raise HTTPException(status_code=400, detail=f"Cannot reject a visit with status {visit.approval_status.value}")

    if visit.created_by == current_user.id and await _requires_maker_checker(session, school_id):
        raise HTTPException(status_code=403, detail="Segregation of duties: you logged this visit and cannot also reject it")

    visit.approval_status = VisitApprovalStatus.REJECTED
    visit.approved_by = current_user.id
    visit.approved_at = datetime.utcnow()
    visit.rejection_reason = data.rejection_reason
    session.add(visit)
    await session.commit()
    await session.refresh(visit)
    return jsonable_encoder(visit)


@router.get("/visits/{visit_id}", response_model=dict)
async def get_clinic_visit(
    visit_id: str,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(ClinicVisit).where(and_(ClinicVisit.id == visit_id, ClinicVisit.school_id == school_id))
    )
    visit = result.scalar_one_or_none()
    if not visit:
        raise HTTPException(status_code=404, detail="Clinic visit not found")
    return jsonable_encoder(visit)


@router.put("/visits/{visit_id}", response_model=dict)
async def update_clinic_visit(
    visit_id: str,
    data: ClinicVisitUpdate,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(ClinicVisit).where(and_(ClinicVisit.id == visit_id, ClinicVisit.school_id == school_id))
    )
    visit = result.scalar_one_or_none()
    if not visit:
        raise HTTPException(status_code=404, detail="Clinic visit not found")

    update_data = data.dict(exclude_unset=True)
    for key, value in update_data.items():
        setattr(visit, key, value)

    session.add(visit)
    await session.commit()
    await session.refresh(visit)
    return jsonable_encoder(visit)


@router.delete("/visits/{visit_id}", response_model=dict)
async def delete_clinic_visit(
    visit_id: str,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(ClinicVisit).where(and_(ClinicVisit.id == visit_id, ClinicVisit.school_id == school_id))
    )
    visit = result.scalar_one_or_none()
    if not visit:
        raise HTTPException(status_code=404, detail="Clinic visit not found")

    await session.delete(visit)
    await session.commit()
    return {"message": "Clinic visit deleted successfully", "id": visit_id}


# ============================================================================
# IMMUNIZATION RECORDS
# ============================================================================

@router.get("/immunizations", response_model=List[dict])
async def list_immunization_records(
    student_id: Optional[str] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    query = select(ImmunizationRecord).where(ImmunizationRecord.school_id == school_id)
    if student_id:
        query = query.where(ImmunizationRecord.student_id == student_id)
    query = query.order_by(ImmunizationRecord.created_at.desc()).offset(skip).limit(limit)

    result = await session.execute(query)
    return [jsonable_encoder(r) for r in result.scalars().all()]


@router.post("/immunizations", response_model=dict)
async def create_immunization_record(
    data: ImmunizationRecordCreate,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    record = ImmunizationRecord(**data.dict(), school_id=school_id)
    session.add(record)
    await session.commit()
    await session.refresh(record)
    return jsonable_encoder(record)


@router.get("/immunizations/{record_id}", response_model=dict)
async def get_immunization_record(
    record_id: str,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(ImmunizationRecord).where(
            and_(ImmunizationRecord.id == record_id, ImmunizationRecord.school_id == school_id)
        )
    )
    record = result.scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=404, detail="Immunization record not found")
    return jsonable_encoder(record)


@router.put("/immunizations/{record_id}", response_model=dict)
async def update_immunization_record(
    record_id: str,
    data: ImmunizationRecordUpdate,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(ImmunizationRecord).where(
            and_(ImmunizationRecord.id == record_id, ImmunizationRecord.school_id == school_id)
        )
    )
    record = result.scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=404, detail="Immunization record not found")

    update_data = data.dict(exclude_unset=True)
    for key, value in update_data.items():
        setattr(record, key, value)

    session.add(record)
    await session.commit()
    await session.refresh(record)
    return jsonable_encoder(record)


@router.delete("/immunizations/{record_id}", response_model=dict)
async def delete_immunization_record(
    record_id: str,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(ImmunizationRecord).where(
            and_(ImmunizationRecord.id == record_id, ImmunizationRecord.school_id == school_id)
        )
    )
    record = result.scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=404, detail="Immunization record not found")

    await session.delete(record)
    await session.commit()
    return {"message": "Immunization record deleted successfully", "id": record_id}


# ============================================================================
# MEDICATION ADMINISTRATION RECORD (MAR)
# ============================================================================

@router.get("/visits/{visit_id}/medications", response_model=List[dict])
async def list_medication_administrations(
    visit_id: str,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    visit_result = await session.execute(
        select(ClinicVisit).where(and_(ClinicVisit.id == visit_id, ClinicVisit.school_id == school_id))
    )
    if not visit_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Clinic visit not found")

    query = select(MedicationAdministration).where(
        and_(MedicationAdministration.clinic_visit_id == visit_id, MedicationAdministration.school_id == school_id)
    ).order_by(MedicationAdministration.created_at.desc())
    result = await session.execute(query)
    return [jsonable_encoder(m) for m in result.scalars().all()]


@router.post("/visits/{visit_id}/medications", response_model=dict)
async def create_medication_administration(
    visit_id: str,
    data: MedicationAdministrationCreate,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    visit_result = await session.execute(
        select(ClinicVisit).where(and_(ClinicVisit.id == visit_id, ClinicVisit.school_id == school_id))
    )
    visit = visit_result.scalar_one_or_none()
    if not visit:
        raise HTTPException(status_code=404, detail="Clinic visit not found")

    record = MedicationAdministration(
        **data.dict(),
        school_id=school_id,
        clinic_visit_id=visit_id,
        student_id=visit.student_id,
    )
    session.add(record)
    await session.commit()
    await session.refresh(record)
    return jsonable_encoder(record)


@router.put("/medications/{medication_id}", response_model=dict)
async def update_medication_administration(
    medication_id: str,
    data: MedicationAdministrationUpdate,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(MedicationAdministration).where(
            and_(MedicationAdministration.id == medication_id, MedicationAdministration.school_id == school_id)
        )
    )
    record = result.scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=404, detail="Medication administration record not found")

    update_data = data.dict(exclude_unset=True)
    for key, value in update_data.items():
        setattr(record, key, value)

    session.add(record)
    await session.commit()
    await session.refresh(record)
    return jsonable_encoder(record)


@router.delete("/medications/{medication_id}", response_model=dict)
async def delete_medication_administration(
    medication_id: str,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(MedicationAdministration).where(
            and_(MedicationAdministration.id == medication_id, MedicationAdministration.school_id == school_id)
        )
    )
    record = result.scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=404, detail="Medication administration record not found")

    await session.delete(record)
    await session.commit()
    return {"message": "Medication administration record deleted successfully", "id": medication_id}


# ============================================================================
# HEALTH INCIDENT REPORTING
# ============================================================================

@router.post("/incidents", response_model=dict)
async def create_health_incident(
    data: HealthIncidentCreate,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    incident_data = data.dict()
    parent_notified = incident_data.pop("parent_notified", False)

    incident = HealthIncident(
        **incident_data,
        school_id=school_id,
        reporter_staff_id=current_user.id,
        parent_notified=parent_notified,
        parent_notified_at=datetime.utcnow() if parent_notified else None,
    )
    session.add(incident)
    await session.commit()
    await session.refresh(incident)
    return jsonable_encoder(incident)


@router.get("/incidents", response_model=List[dict])
async def list_health_incidents(
    student_id: Optional[str] = None,
    status: Optional[str] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    query = select(HealthIncident).where(HealthIncident.school_id == school_id)
    if student_id:
        query = query.where(HealthIncident.student_id == student_id)
    if status:
        query = query.where(HealthIncident.status == status)
    query = query.order_by(HealthIncident.created_at.desc()).offset(skip).limit(limit)

    result = await session.execute(query)
    return [jsonable_encoder(i) for i in result.scalars().all()]


@router.get("/incidents/{incident_id}", response_model=dict)
async def get_health_incident(
    incident_id: str,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(HealthIncident).where(and_(HealthIncident.id == incident_id, HealthIncident.school_id == school_id))
    )
    incident = result.scalar_one_or_none()
    if not incident:
        raise HTTPException(status_code=404, detail="Health incident not found")
    return jsonable_encoder(incident)


@router.put("/incidents/{incident_id}", response_model=dict)
async def update_health_incident(
    incident_id: str,
    data: HealthIncidentUpdate,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(HealthIncident).where(and_(HealthIncident.id == incident_id, HealthIncident.school_id == school_id))
    )
    incident = result.scalar_one_or_none()
    if not incident:
        raise HTTPException(status_code=404, detail="Health incident not found")

    update_data = data.dict(exclude_unset=True)
    for key, value in update_data.items():
        setattr(incident, key, value)

    # Auto-stamp parent_notified_at when parent_notified flips true and the
    # caller didn't explicitly supply a timestamp of their own.
    if update_data.get("parent_notified") and "parent_notified_at" not in update_data:
        incident.parent_notified_at = datetime.utcnow()

    incident.updated_at = datetime.utcnow()

    session.add(incident)
    await session.commit()
    await session.refresh(incident)
    return jsonable_encoder(incident)


@router.delete("/incidents/{incident_id}", response_model=dict)
async def delete_health_incident(
    incident_id: str,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(HealthIncident).where(and_(HealthIncident.id == incident_id, HealthIncident.school_id == school_id))
    )
    incident = result.scalar_one_or_none()
    if not incident:
        raise HTTPException(status_code=404, detail="Health incident not found")

    await session.delete(incident)
    await session.commit()
    return {"message": "Health incident deleted successfully", "id": incident_id}


# ============================================================================
# HEALTH SCREENING CAMPAIGNS
# ============================================================================

@router.post("/campaigns", response_model=dict)
async def create_screening_campaign(
    data: HealthScreeningCampaignCreate,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    campaign = HealthScreeningCampaign(
        **data.dict(),
        school_id=school_id,
        status="planned",
        created_by=current_user.id,
    )
    session.add(campaign)
    await session.commit()
    await session.refresh(campaign)
    return jsonable_encoder(campaign)


@router.get("/campaigns", response_model=List[dict])
async def list_screening_campaigns(
    status: Optional[str] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    query = select(HealthScreeningCampaign).where(HealthScreeningCampaign.school_id == school_id)
    if status:
        query = query.where(HealthScreeningCampaign.status == status)
    query = query.order_by(HealthScreeningCampaign.created_at.desc()).offset(skip).limit(limit)

    result = await session.execute(query)
    return [jsonable_encoder(c) for c in result.scalars().all()]


@router.get("/campaigns/{campaign_id}", response_model=dict)
async def get_screening_campaign(
    campaign_id: str,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(HealthScreeningCampaign).where(
            and_(HealthScreeningCampaign.id == campaign_id, HealthScreeningCampaign.school_id == school_id)
        )
    )
    campaign = result.scalar_one_or_none()
    if not campaign:
        raise HTTPException(status_code=404, detail="Screening campaign not found")
    return jsonable_encoder(campaign)


@router.put("/campaigns/{campaign_id}", response_model=dict)
async def update_screening_campaign(
    campaign_id: str,
    data: HealthScreeningCampaignUpdate,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(HealthScreeningCampaign).where(
            and_(HealthScreeningCampaign.id == campaign_id, HealthScreeningCampaign.school_id == school_id)
        )
    )
    campaign = result.scalar_one_or_none()
    if not campaign:
        raise HTTPException(status_code=404, detail="Screening campaign not found")

    update_data = data.dict(exclude_unset=True)
    for key, value in update_data.items():
        setattr(campaign, key, value)
    campaign.updated_at = datetime.utcnow()

    session.add(campaign)
    await session.commit()
    await session.refresh(campaign)
    return jsonable_encoder(campaign)


@router.post("/campaigns/{campaign_id}/results", response_model=List[dict])
async def bulk_create_screening_results(
    campaign_id: str,
    data: HealthScreeningResultBulkCreate,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    """Batch-enter screening results for a campaign — one row per student,
    committed once at the end."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    campaign_result = await session.execute(
        select(HealthScreeningCampaign).where(
            and_(HealthScreeningCampaign.id == campaign_id, HealthScreeningCampaign.school_id == school_id)
        )
    )
    if not campaign_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Screening campaign not found")

    new_results = []
    for entry in data.results:
        result = HealthScreeningResult(
            **entry.dict(),
            school_id=school_id,
            campaign_id=campaign_id,
            screened_by=current_user.id,
        )
        session.add(result)
        new_results.append(result)

    await session.commit()
    for result in new_results:
        await session.refresh(result)
    return [jsonable_encoder(r) for r in new_results]


@router.get("/campaigns/{campaign_id}/results", response_model=List[dict])
async def list_screening_results(
    campaign_id: str,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    campaign_result = await session.execute(
        select(HealthScreeningCampaign).where(
            and_(HealthScreeningCampaign.id == campaign_id, HealthScreeningCampaign.school_id == school_id)
        )
    )
    if not campaign_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Screening campaign not found")

    query = select(HealthScreeningResult).where(
        and_(HealthScreeningResult.campaign_id == campaign_id, HealthScreeningResult.school_id == school_id)
    ).order_by(HealthScreeningResult.screened_at.desc())
    result = await session.execute(query)
    return [jsonable_encoder(r) for r in result.scalars().all()]


@router.get("/campaigns/{campaign_id}/coverage", response_model=dict)
async def get_screening_campaign_coverage(
    campaign_id: str,
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    campaign_result = await session.execute(
        select(HealthScreeningCampaign).where(
            and_(HealthScreeningCampaign.id == campaign_id, HealthScreeningCampaign.school_id == school_id)
        )
    )
    campaign = campaign_result.scalar_one_or_none()
    if not campaign:
        raise HTTPException(status_code=404, detail="Screening campaign not found")

    target_count = 0
    if campaign.target_class_id:
        count_result = await session.execute(
            select(func.count(Student.id)).where(
                and_(
                    Student.school_id == school_id,
                    Student.class_id == campaign.target_class_id,
                    Student.status == "active",
                )
            )
        )
        target_count = count_result.scalar() or 0

    screened_result = await session.execute(
        select(func.count(func.distinct(HealthScreeningResult.student_id))).where(
            and_(HealthScreeningResult.campaign_id == campaign_id, HealthScreeningResult.school_id == school_id)
        )
    )
    screened_count = screened_result.scalar() or 0

    return {"target_count": target_count, "screened_count": screened_count}
