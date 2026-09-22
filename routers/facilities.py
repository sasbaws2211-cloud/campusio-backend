"""Facilities asset register, work requests, and preventive maintenance."""
from datetime import datetime, date
from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_current_user, require_roles
from database import get_session
from models.facilities import (
    FacilityAsset, FacilityAssetCreate, FacilityAssetUpdate, MaintenanceRequest, MaintenanceRequestCreate,
    MaintenanceSchedule, MaintenanceScheduleCreate, MaintenanceScheduleUpdate, MaintenanceUpdate, MaintenanceStatus,
    FacilityBuilding, FacilityBuildingCreate, FacilityBuildingUpdate, FacilityRoom, FacilityRoomCreate, FacilityRoomUpdate,
    FacilityEquipment, FacilityEquipmentCreate, FacilityEquipmentUpdate, FacilityContractor, FacilityContractorCreate,
    FacilityContractorUpdate, FacilityWorkOrder, FacilityWorkOrderCreate, FacilityWorkOrderUpdate, FacilityServiceRecord,
    FacilityServiceRecordCreate, UtilityMeter, UtilityMeterCreate, UtilityReading, UtilityReadingCreate, FacilityBooking,
    FacilityBookingCreate, FacilityBookingUpdate, SafetyInspection, SafetyInspectionCreate, SafetyInspectionUpdate,
)
from models.inventory import Asset
from models.procurement import Supplier
from models.user import User, UserRole
from services.facilities_maintenance_service import run_due_maintenance_schedules

router = APIRouter(prefix="/facilities", tags=["Facilities & Maintenance"])
WRITE_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.HR, UserRole.STOREKEEPER)
# Every write endpoint below is already gated to WRITE_ROLES, but every read
# used plain get_current_user -- a STUDENT/PARENT account got the exact same
# full result set as staff, including contractor pricing/contacts, safety
# inspection outcomes, and maintenance cost estimates. STAFF_ROLES is
# everything except STUDENT/PARENT, built from the enum so a future role
# doesn't silently fall through this gate the way it would with a hardcoded list.
STAFF_ROLES = tuple(r for r in UserRole if r not in (UserRole.STUDENT, UserRole.PARENT))


def school_id(user: User) -> str:
    if not user.school_id:
        raise HTTPException(status_code=403, detail="No school context")
    return user.school_id


@router.get("/assets", response_model=list[dict])
async def list_assets(user: User = Depends(require_roles(*STAFF_ROLES)), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(FacilityAsset).where(FacilityAsset.school_id == school_id(user)).order_by(FacilityAsset.name))
    return [asset.model_dump() for asset in result.scalars().all()]


@router.get("/summary", response_model=dict)
async def facilities_summary(user: User = Depends(require_roles(*STAFF_ROLES)), session: AsyncSession = Depends(get_session)):
    scope = school_id(user)
    assets = (await session.execute(select(FacilityAsset).where(FacilityAsset.school_id == scope))).scalars().all()
    requests = (await session.execute(select(MaintenanceRequest).where(MaintenanceRequest.school_id == scope))).scalars().all()
    schedules = (await session.execute(select(MaintenanceSchedule).where(MaintenanceSchedule.school_id == scope, MaintenanceSchedule.active == True))).scalars().all()
    today = date.today().isoformat()
    return {
        "assets_total": len(assets),
        "assets_active": sum(1 for asset in assets if asset.status == "active"),
        "requests_open": sum(1 for item in requests if item.status in [MaintenanceStatus.OPEN, MaintenanceStatus.SCHEDULED, MaintenanceStatus.IN_PROGRESS]),
        "requests_urgent": sum(1 for item in requests if item.priority == "urgent" and item.status != MaintenanceStatus.COMPLETED),
        "schedules_overdue": sum(1 for item in schedules if item.next_due_date < today),
        "condition_breakdown": {condition: sum(1 for asset in assets if asset.condition == condition) for condition in sorted({asset.condition for asset in assets})},
    }


@router.post("/assets", response_model=dict)
async def create_asset(payload: FacilityAssetCreate, user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    scope = school_id(user)
    await _related(session, Asset, payload.inventory_asset_id, scope, "Inventory asset")
    asset = FacilityAsset(school_id=scope, **payload.model_dump())
    session.add(asset)
    await session.commit()
    await session.refresh(asset)
    return asset.model_dump()


@router.patch("/assets/{asset_id}", response_model=dict)
async def update_asset(asset_id: str, payload: FacilityAssetUpdate, user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    scope = school_id(user)
    result = await session.execute(select(FacilityAsset).where(FacilityAsset.id == asset_id, FacilityAsset.school_id == scope))
    asset = result.scalar_one_or_none()
    if not asset:
        raise HTTPException(status_code=404, detail="Facility asset not found")
    await _related(session, Asset, payload.inventory_asset_id, scope, "Inventory asset")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(asset, key, value)
    asset.updated_at = datetime.utcnow()
    session.add(asset)
    await session.commit()
    await session.refresh(asset)
    return asset.model_dump()


@router.delete("/assets/{asset_id}", response_model=dict)
async def archive_asset(asset_id: str, user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(FacilityAsset).where(FacilityAsset.id == asset_id, FacilityAsset.school_id == school_id(user)))
    asset = result.scalar_one_or_none()
    if not asset:
        raise HTTPException(status_code=404, detail="Facility asset not found")
    asset.status = "archived"
    asset.updated_at = datetime.utcnow()
    session.add(asset)
    await session.commit()
    return {"success": True, "id": asset_id, "status": "archived"}


@router.get("/requests", response_model=list[dict])
async def list_requests(user: User = Depends(require_roles(*STAFF_ROLES)), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(MaintenanceRequest).where(MaintenanceRequest.school_id == school_id(user)).order_by(MaintenanceRequest.created_at.desc()))
    return [item.model_dump() for item in result.scalars().all()]


@router.post("/requests", response_model=dict)
async def create_request(payload: MaintenanceRequestCreate, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    request = MaintenanceRequest(school_id=school_id(user), requested_by=user.id, **payload.model_dump())
    session.add(request)
    await session.commit()
    await session.refresh(request)
    return request.model_dump()


@router.patch("/requests/{request_id}", response_model=dict)
async def update_request(request_id: str, payload: MaintenanceUpdate, user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(MaintenanceRequest).where(MaintenanceRequest.id == request_id, MaintenanceRequest.school_id == school_id(user)))
    request = result.scalar_one_or_none()
    if not request:
        raise HTTPException(status_code=404, detail="Maintenance request not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(request, key, value)
    if request.status == MaintenanceStatus.COMPLETED and not request.completed_date:
        request.completed_date = datetime.utcnow().date().isoformat()
    session.add(request)
    await session.commit()
    await session.refresh(request)
    return request.model_dump()


@router.post("/requests/{request_id}/convert-to-work-order", response_model=dict)
async def convert_request_to_work_order(request_id: str, user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    """Turns a reported fault into an actual work order — the missing link
    between "someone reported this is broken" and "this is now being
    worked." Closes the loop MaintenanceRequest/FacilityWorkOrder used to
    leave open: two near-identical tables with no way to tell that one
    caused the other."""
    scope = school_id(user)
    result = await session.execute(select(MaintenanceRequest).where(MaintenanceRequest.id == request_id, MaintenanceRequest.school_id == scope))
    request = result.scalar_one_or_none()
    if not request:
        raise HTTPException(status_code=404, detail="Maintenance request not found")
    if request.work_order_id:
        raise HTTPException(status_code=400, detail="This request already has a work order")

    work_order = FacilityWorkOrder(
        school_id=scope,
        asset_id=request.asset_id,
        title=request.title,
        description=request.description,
        priority=request.priority,
        requested_by=request.requested_by,
        assigned_to=request.assigned_to,
        requested_date=request.requested_date,
        scheduled_date=request.scheduled_date,
        estimated_cost=request.estimated_cost,
    )
    session.add(work_order)
    await session.flush()

    request.work_order_id = work_order.id
    request.status = MaintenanceStatus.SCHEDULED
    session.add(request)
    await session.commit()
    await session.refresh(work_order)
    return work_order.model_dump()


@router.get("/schedules", response_model=list[dict])
async def list_schedules(user: User = Depends(require_roles(*STAFF_ROLES)), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(MaintenanceSchedule).where(MaintenanceSchedule.school_id == school_id(user)).order_by(MaintenanceSchedule.next_due_date))
    return [item.model_dump() for item in result.scalars().all()]


@router.post("/schedules", response_model=dict)
async def create_schedule(payload: MaintenanceScheduleCreate, user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    scope = school_id(user)
    await _related(session, FacilityAsset, payload.asset_id, scope, "Asset")
    await _related(session, FacilityRoom, payload.room_id, scope, "Room")
    await _related(session, FacilityContractor, payload.contractor_id, scope, "Contractor")
    schedule = MaintenanceSchedule(school_id=scope, **payload.model_dump())
    session.add(schedule)
    await session.commit()
    await session.refresh(schedule)
    return schedule.model_dump()


@router.patch("/schedules/{schedule_id}", response_model=dict)
async def update_schedule(schedule_id: str, payload: MaintenanceScheduleUpdate, user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(MaintenanceSchedule).where(MaintenanceSchedule.id == schedule_id, MaintenanceSchedule.school_id == school_id(user)))
    schedule = result.scalar_one_or_none()
    if not schedule:
        raise HTTPException(status_code=404, detail="Maintenance schedule not found")
    scope = school_id(user)
    await _related(session, FacilityRoom, payload.room_id, scope, "Room")
    await _related(session, FacilityContractor, payload.contractor_id, scope, "Contractor")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(schedule, key, value)
    session.add(schedule)
    await session.commit()
    await session.refresh(schedule)
    return schedule.model_dump()


@router.post("/schedules/run-due-check", response_model=dict)
async def run_schedule_due_check(user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    """Manual trigger for the same sweep services/scheduler.py runs
    nightly across every school — lets an admin generate this school's due
    preventive-maintenance work orders on demand instead of waiting for
    03:00 UTC (also how this is tested/verified)."""
    return await run_due_maintenance_schedules(session, school_id=school_id(user))


async def _record(session: AsyncSession, model, record_id: str, scope: str):
    result = await session.execute(select(model).where(model.id == record_id, model.school_id == scope))
    return result.scalar_one_or_none()


async def _related(session: AsyncSession, model, record_id: str | None, scope: str, label: str):
    if not record_id:
        return None
    item = await _record(session, model, record_id, scope)
    if not item:
        raise HTTPException(status_code=400, detail=f"{label} not found in this school")
    return item


@router.get("/buildings", response_model=list[dict])
async def list_buildings(user: User = Depends(require_roles(*STAFF_ROLES)), session: AsyncSession = Depends(get_session)):
    records = (await session.execute(select(FacilityBuilding).where(FacilityBuilding.school_id == school_id(user)).order_by(FacilityBuilding.name))).scalars().all()
    return [item.model_dump() for item in records]


@router.post("/buildings", response_model=dict)
async def create_building(payload: FacilityBuildingCreate, user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    item = FacilityBuilding(school_id=school_id(user), **payload.model_dump())
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item.model_dump()


@router.patch("/buildings/{building_id}", response_model=dict)
async def update_building(building_id: str, payload: FacilityBuildingUpdate, user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    item = await _record(session, FacilityBuilding, building_id, school_id(user))
    if not item:
        raise HTTPException(status_code=404, detail="Facility building not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(item, key, value)
    item.updated_at = datetime.utcnow()
    await session.commit()
    await session.refresh(item)
    return item.model_dump()


@router.get("/rooms", response_model=list[dict])
async def list_facility_rooms(user: User = Depends(require_roles(*STAFF_ROLES)), session: AsyncSession = Depends(get_session)):
    records = (await session.execute(select(FacilityRoom).where(FacilityRoom.school_id == school_id(user)).order_by(FacilityRoom.name))).scalars().all()
    return [item.model_dump() for item in records]


@router.post("/rooms", response_model=dict)
async def create_facility_room(payload: FacilityRoomCreate, user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    scope = school_id(user)
    await _related(session, FacilityBuilding, payload.building_id, scope, "Building")
    item = FacilityRoom(school_id=scope, **payload.model_dump())
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item.model_dump()


@router.patch("/rooms/{room_id}", response_model=dict)
async def update_facility_room(room_id: str, payload: FacilityRoomUpdate, user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    scope = school_id(user)
    item = await _record(session, FacilityRoom, room_id, scope)
    if not item:
        raise HTTPException(status_code=404, detail="Facility room not found")
    if payload.building_id:
        await _related(session, FacilityBuilding, payload.building_id, scope, "Building")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(item, key, value)
    item.updated_at = datetime.utcnow()
    await session.commit()
    await session.refresh(item)
    return item.model_dump()


@router.get("/equipment", response_model=list[dict])
async def list_equipment(user: User = Depends(require_roles(*STAFF_ROLES)), session: AsyncSession = Depends(get_session)):
    records = (await session.execute(select(FacilityEquipment).where(FacilityEquipment.school_id == school_id(user)).order_by(FacilityEquipment.name))).scalars().all()
    return [item.model_dump() for item in records]


@router.post("/equipment", response_model=dict)
async def create_equipment(payload: FacilityEquipmentCreate, user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    scope = school_id(user)
    await _related(session, FacilityRoom, payload.room_id, scope, "Room")
    await _related(session, FacilityAsset, payload.asset_id, scope, "Asset")
    item = FacilityEquipment(school_id=scope, **payload.model_dump())
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item.model_dump()


@router.patch("/equipment/{equipment_id}", response_model=dict)
async def update_equipment(equipment_id: str, payload: FacilityEquipmentUpdate, user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    scope = school_id(user)
    item = await _record(session, FacilityEquipment, equipment_id, scope)
    if not item:
        raise HTTPException(status_code=404, detail="Facility equipment not found")
    await _related(session, FacilityRoom, payload.room_id, scope, "Room")
    await _related(session, FacilityAsset, payload.asset_id, scope, "Asset")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(item, key, value)
    item.updated_at = datetime.utcnow()
    await session.commit()
    await session.refresh(item)
    return item.model_dump()


@router.get("/contractors", response_model=list[dict])
async def list_contractors(user: User = Depends(require_roles(*STAFF_ROLES)), session: AsyncSession = Depends(get_session)):
    records = (await session.execute(select(FacilityContractor).where(FacilityContractor.school_id == school_id(user)).order_by(FacilityContractor.name))).scalars().all()
    return [item.model_dump() for item in records]


@router.post("/contractors", response_model=dict)
async def create_contractor(payload: FacilityContractorCreate, user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    scope = school_id(user)
    await _related(session, Supplier, payload.supplier_id, scope, "Supplier")
    item = FacilityContractor(school_id=scope, **payload.model_dump())
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item.model_dump()


@router.patch("/contractors/{contractor_id}", response_model=dict)
async def update_contractor(contractor_id: str, payload: FacilityContractorUpdate, user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    scope = school_id(user)
    item = await _record(session, FacilityContractor, contractor_id, scope)
    if not item:
        raise HTTPException(status_code=404, detail="Facility contractor not found")
    await _related(session, Supplier, payload.supplier_id, scope, "Supplier")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(item, key, value)
    item.updated_at = datetime.utcnow()
    await session.commit()
    await session.refresh(item)
    return item.model_dump()


@router.get("/work-orders", response_model=list[dict])
async def list_work_orders(user: User = Depends(require_roles(*STAFF_ROLES)), session: AsyncSession = Depends(get_session)):
    records = (await session.execute(select(FacilityWorkOrder).where(FacilityWorkOrder.school_id == school_id(user)).order_by(FacilityWorkOrder.created_at.desc()))).scalars().all()
    return [item.model_dump() for item in records]


@router.post("/work-orders", response_model=dict)
async def create_work_order(payload: FacilityWorkOrderCreate, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    scope = school_id(user)
    await _related(session, FacilityAsset, payload.asset_id, scope, "Asset")
    await _related(session, FacilityRoom, payload.room_id, scope, "Room")
    await _related(session, FacilityContractor, payload.contractor_id, scope, "Contractor")
    item = FacilityWorkOrder(school_id=scope, requested_by=user.id, **payload.model_dump())
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item.model_dump()


@router.patch("/work-orders/{work_order_id}", response_model=dict)
async def update_work_order(work_order_id: str, payload: FacilityWorkOrderUpdate, user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    scope = school_id(user)
    item = await _record(session, FacilityWorkOrder, work_order_id, scope)
    if not item:
        raise HTTPException(status_code=404, detail="Facility work order not found")
    await _related(session, FacilityAsset, payload.asset_id, scope, "Asset")
    await _related(session, FacilityRoom, payload.room_id, scope, "Room")
    await _related(session, FacilityContractor, payload.contractor_id, scope, "Contractor")
    values = payload.model_dump(exclude_unset=True)
    for key, value in values.items():
        setattr(item, key, value)
    if item.status == "completed" and not item.completed_date:
        item.completed_date = date.today().isoformat()
    item.updated_at = datetime.utcnow()
    await session.commit()
    await session.refresh(item)
    return item.model_dump()


@router.get("/service-history", response_model=list[dict])
async def list_service_history(user: User = Depends(require_roles(*STAFF_ROLES)), session: AsyncSession = Depends(get_session)):
    records = (await session.execute(select(FacilityServiceRecord).where(FacilityServiceRecord.school_id == school_id(user)).order_by(FacilityServiceRecord.service_date.desc()))).scalars().all()
    return [item.model_dump() for item in records]


@router.post("/service-history", response_model=dict)
async def create_service_record(payload: FacilityServiceRecordCreate, user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    scope = school_id(user)
    await _related(session, FacilityWorkOrder, payload.work_order_id, scope, "Work order")
    await _related(session, FacilityAsset, payload.asset_id, scope, "Asset")
    await _related(session, FacilityRoom, payload.room_id, scope, "Room")
    await _related(session, FacilityContractor, payload.contractor_id, scope, "Contractor")
    item = FacilityServiceRecord(school_id=scope, created_by=user.id, **payload.model_dump())
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item.model_dump()


@router.get("/meters", response_model=list[dict])
async def list_meters(user: User = Depends(require_roles(*STAFF_ROLES)), session: AsyncSession = Depends(get_session)):
    records = (await session.execute(select(UtilityMeter).where(UtilityMeter.school_id == school_id(user)).order_by(UtilityMeter.utility_type, UtilityMeter.meter_number))).scalars().all()
    return [item.model_dump() for item in records]


@router.post("/meters", response_model=dict)
async def create_meter(payload: UtilityMeterCreate, user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    scope = school_id(user)
    await _related(session, FacilityBuilding, payload.building_id, scope, "Building")
    await _related(session, FacilityRoom, payload.room_id, scope, "Room")
    item = UtilityMeter(school_id=scope, **payload.model_dump())
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item.model_dump()


@router.get("/meter-readings", response_model=list[dict])
async def list_meter_readings(user: User = Depends(require_roles(*STAFF_ROLES)), session: AsyncSession = Depends(get_session)):
    records = (await session.execute(select(UtilityReading).where(UtilityReading.school_id == school_id(user)).order_by(UtilityReading.reading_date.desc()))).scalars().all()
    return [item.model_dump() for item in records]


@router.post("/meter-readings", response_model=dict)
async def create_meter_reading(payload: UtilityReadingCreate, user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    scope = school_id(user)
    await _related(session, UtilityMeter, payload.meter_id, scope, "Utility meter")
    item = UtilityReading(school_id=scope, recorded_by=user.id, **payload.model_dump())
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item.model_dump()


@router.get("/bookings", response_model=list[dict])
async def list_bookings(user: User = Depends(require_roles(*STAFF_ROLES)), session: AsyncSession = Depends(get_session)):
    records = (await session.execute(select(FacilityBooking).where(FacilityBooking.school_id == school_id(user)).order_by(FacilityBooking.start_at))).scalars().all()
    return [item.model_dump() for item in records]


@router.post("/bookings", response_model=dict)
async def create_booking(payload: FacilityBookingCreate, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    scope = school_id(user)
    await _related(session, FacilityRoom, payload.room_id, scope, "Room")
    if payload.end_at <= payload.start_at:
        raise HTTPException(status_code=400, detail="Booking end must be after its start")
    overlap = (await session.execute(select(FacilityBooking).where(FacilityBooking.school_id == scope, FacilityBooking.room_id == payload.room_id, FacilityBooking.status != "cancelled", FacilityBooking.start_at < payload.end_at, FacilityBooking.end_at > payload.start_at))).first()
    if overlap:
        raise HTTPException(status_code=409, detail="Room is already booked for that time")
    item = FacilityBooking(school_id=scope, booked_by=user.id, **payload.model_dump())
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item.model_dump()


@router.patch("/bookings/{booking_id}", response_model=dict)
async def update_booking(booking_id: str, payload: FacilityBookingUpdate, user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    item = await _record(session, FacilityBooking, booking_id, school_id(user))
    if not item:
        raise HTTPException(status_code=404, detail="Facility booking not found")
    values = payload.model_dump(exclude_unset=True)
    start_at = values.get("start_at", item.start_at)
    end_at = values.get("end_at", item.end_at)
    if end_at <= start_at:
        raise HTTPException(status_code=400, detail="Booking end must be after its start")
    for key, value in values.items():
        setattr(item, key, value)
    await session.commit()
    await session.refresh(item)
    return item.model_dump()


@router.get("/inspections", response_model=list[dict])
async def list_inspections(user: User = Depends(require_roles(*STAFF_ROLES)), session: AsyncSession = Depends(get_session)):
    records = (await session.execute(select(SafetyInspection).where(SafetyInspection.school_id == school_id(user)).order_by(SafetyInspection.inspection_date.desc()))).scalars().all()
    return [item.model_dump() for item in records]


@router.post("/inspections", response_model=dict)
async def create_inspection(payload: SafetyInspectionCreate, user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    scope = school_id(user)
    await _related(session, FacilityBuilding, payload.building_id, scope, "Building")
    await _related(session, FacilityRoom, payload.room_id, scope, "Room")
    item = SafetyInspection(school_id=scope, inspector_id=user.id, **payload.model_dump())
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item.model_dump()


@router.patch("/inspections/{inspection_id}", response_model=dict)
async def update_inspection(inspection_id: str, payload: SafetyInspectionUpdate, user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    item = await _record(session, SafetyInspection, inspection_id, school_id(user))
    if not item:
        raise HTTPException(status_code=404, detail="Safety inspection not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(item, key, value)
    await session.commit()
    await session.refresh(item)
    return item.model_dump()