"""Transport Management Router"""
from fastapi import APIRouter, Depends, HTTPException, status, Query
from fastapi.encoders import jsonable_encoder
from sqlmodel import select, func, and_, SQLModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError
from datetime import datetime, timedelta
from typing import Optional, List
import uuid
import json
import logging
from models.transport import (
    Vehicle, VehicleCreate, VehicleUpdate, VehicleStatus, VehicleType,
    Route, RouteCreate, RouteUpdate, RouteStatus,
    RouteStop, RouteStopCreate, RouteStopUpdate, BulkRouteStopsRequest,
    StudentTransport, StudentTransportCreate, StudentTransportUpdate,
    TransportAttendance, TransportAttendanceCreate, TransportAttendanceBulk, AttendanceStatus,
    TransportFee, TransportFeeCreate, TransportFeeUpdate, TransportFeeType,
    VehicleMaintenance, VehicleMaintenanceCreate,
    DriverStaff, DriverStaffCreate, DriverStaffUpdate
)
from models.student import Student, Parent, StudentParent
from models.staff import Staff
from models.user import User, UserRole
from models.facilities import FacilityContractor, FacilityWorkOrder
from database import get_session
from auth import get_current_user, require_roles
from services.plan_gating import require_plan_feature
from services.transport_security_bridge import sync_dropoff_to_arrival_status

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/transport", tags=["Transport Management"])


async def _has_student_access(session: AsyncSession, current_user: User, student: Student) -> bool:
    """Ownership rules for viewing a specific student's transport
    route/fee data: admins and teachers see any student in their school;
    parents only their own children; students only themselves. Previously
    every student-specific read endpoint below only checked same-school,
    not that a PARENT/STUDENT caller actually owns/is related to the
    student in question — any parent, student, teacher, or other
    same-school account could view any other student's transport route,
    pickup point, or fee/payment status. Same shape as
    routers/fees.py::_has_fee_access, applied here since transport.py never
    had an equivalent ownership check at all. Also enforces campus scoping
    for a campus-restricted SCHOOL_ADMIN/TEACHER — transport.py never
    called assert_campus_access anywhere, so such an admin could act on
    students in every campus of the school, not just their assigned one."""
    if current_user.role == UserRole.SUPER_ADMIN:
        return True
    if current_user.role in (UserRole.SCHOOL_ADMIN, UserRole.TEACHER):
        if current_user.school_id != student.school_id:
            return False
        return not current_user.campus_id or current_user.campus_id == student.campus_id
    if current_user.role == UserRole.PARENT:
        parent_result = await session.execute(select(Parent).where(Parent.user_id == current_user.id))
        parent = parent_result.scalar_one_or_none()
        if not parent:
            return False
        sp_result = await session.execute(
            select(StudentParent).where(
                StudentParent.parent_id == parent.id,
                StudentParent.student_id == student.id
            )
        )
        return sp_result.scalar_one_or_none() is not None
    if current_user.role == UserRole.STUDENT:
        return current_user.id == student.user_id
    return False


class VerifyDriverRequest(SQLModel):
    verification_notes: str = ""


async def get_driver_route_ids(current_user: User, session: AsyncSession) -> Optional[List[str]]:
    """Resolve a DRIVER user's assigned route IDs via User -> Staff -> DriverStaff -> Vehicle -> Route.

    Returns None for non-driver roles (no restriction applies to them).
    Returns a list (possibly empty) for drivers — empty means no vehicle/route
    is assigned to them yet, so they're restricted to seeing nothing.
    """
    if current_user.role != UserRole.DRIVER:
        return None

    staff_result = await session.execute(select(Staff).where(Staff.user_id == current_user.id))
    staff = staff_result.scalar_one_or_none()
    if not staff:
        return []

    driver_staff_result = await session.execute(select(DriverStaff).where(DriverStaff.staff_id == staff.id))
    driver_staff = driver_staff_result.scalar_one_or_none()
    if not driver_staff:
        return []

    vehicle_result = await session.execute(select(Vehicle).where(Vehicle.driver_id == driver_staff.id))
    vehicle_ids = [v.id for v in vehicle_result.scalars().all()]
    if not vehicle_ids:
        return []

    route_result = await session.execute(select(Route.id).where(Route.vehicle_id.in_(vehicle_ids)))
    return list(route_result.scalars().all())



# ============================================================================
# VEHICLE TYPE ENDPOINT
# ============================================================================

@router.get("/vehicle-types", response_model=List[dict])
async def get_vehicle_types():
    """Get all available vehicle types"""
    vehicle_type_labels = {
        "bus": "Bus",
        "van": "Van",
        "minibus": "Mini Bus",
        "shuttle": "Shuttle"
    }
    return [
        {"value": vtype.value, "label": vehicle_type_labels.get(vtype.value, vtype.value.capitalize())}
        for vtype in VehicleType
    ]


# ============================================================================
# VEHICLE ENDPOINTS
# ============================================================================

@router.get("/vehicles", response_model=List[dict])
async def list_vehicles(
    status: Optional[VehicleStatus] = None,
    vehicle_type: Optional[VehicleType] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    _plan_check: User = Depends(require_plan_feature("transport_module")),
    session: AsyncSession = Depends(get_session)
):
    """List all vehicles for the school"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    query = select(Vehicle).where(Vehicle.school_id == school_id)
    
    if status:
        query = query.where(Vehicle.status == status)
    if vehicle_type:
        query = query.where(Vehicle.vehicle_type == vehicle_type)
    
    query = query.offset(skip).limit(limit)
    result = await session.execute(query)
    vehicles = result.scalars().all()
    
    return [
        {
            **jsonable_encoder(v),
            "vehicle_type": v.vehicle_type.value,
            "status": v.status.value
        }
        for v in vehicles
    ]


@router.post("/vehicles", response_model=dict)
async def create_vehicle(
    vehicle_data: VehicleCreate,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    _plan_check: User = Depends(require_plan_feature("transport_module")),
    session: AsyncSession = Depends(get_session)
):
    """Create a new vehicle"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    # Check if registration number already exists in this school -- plates
    # are only unique per-school (see uq_vehicles_school_registration)
    result = await session.execute(
        select(Vehicle).where(Vehicle.registration_number == vehicle_data.registration_number, Vehicle.school_id == school_id)
    )
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Vehicle with this registration number already exists")
    
    vehicle = Vehicle(
        **vehicle_data.dict(),
        school_id=school_id
    )
    session.add(vehicle)
    await session.commit()
    await session.refresh(vehicle)
    
    return {
        **jsonable_encoder(vehicle),
        "vehicle_type": vehicle.vehicle_type.value,
        "status": vehicle.status.value
    }


@router.get("/vehicles/{vehicle_id}", response_model=dict)
async def get_vehicle(
    vehicle_id: str,
    current_user: User = Depends(get_current_user),
    _plan_check: User = Depends(require_plan_feature("transport_module")),
    session: AsyncSession = Depends(get_session)
):
    """Get vehicle details"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    result = await session.execute(
        select(Vehicle).where(
            and_(Vehicle.id == vehicle_id, Vehicle.school_id == school_id)
        )
    )
    vehicle = result.scalar_one_or_none()
    if not vehicle:
        raise HTTPException(status_code=404, detail="Vehicle not found")
    
    return {
        **jsonable_encoder(vehicle),
        "vehicle_type": vehicle.vehicle_type.value,
        "status": vehicle.status.value
    }


@router.put("/vehicles/{vehicle_id}", response_model=dict)
async def update_vehicle(
    vehicle_id: str,
    vehicle_data: VehicleUpdate,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    _plan_check: User = Depends(require_plan_feature("transport_module")),
    session: AsyncSession = Depends(get_session)
):
    """Update vehicle information"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    result = await session.execute(
        select(Vehicle).where(
            and_(Vehicle.id == vehicle_id, Vehicle.school_id == school_id)
        )
    )
    vehicle = result.scalar_one_or_none()
    if not vehicle:
        raise HTTPException(status_code=404, detail="Vehicle not found")
    
    update_data = vehicle_data.dict(exclude_unset=True)
    for key, value in update_data.items():
        setattr(vehicle, key, value)
    
    vehicle.updated_at = datetime.utcnow()
    session.add(vehicle)
    await session.commit()
    await session.refresh(vehicle)
    
    return {
        **jsonable_encoder(vehicle),
        "vehicle_type": vehicle.vehicle_type.value,
        "status": vehicle.status.value
    }


@router.get("/vehicles/{vehicle_id}/delete-impact", response_model=dict)
async def get_vehicle_delete_impact(
    vehicle_id: str,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    _plan_check: User = Depends(require_plan_feature("transport_module")),
    session: AsyncSession = Depends(get_session)
):
    """Preview what deleting this vehicle would affect, before actually deleting it."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(Vehicle).where(and_(Vehicle.id == vehicle_id, Vehicle.school_id == school_id))
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Vehicle not found")

    will_be_deleted = {}
    for table, label in [("vehicle_maintenance", "maintenance records"), ("transport_attendance", "attendance records")]:
        col = "vehicle_id"
        count = (await session.execute(text(f"SELECT count(*) FROM {table} WHERE {col} = :vid"), {"vid": vehicle_id})).scalar()
        if count:
            will_be_deleted[label] = count

    will_be_preserved = {}
    count = (await session.execute(text("SELECT count(*) FROM routes WHERE vehicle_id = :vid"), {"vid": vehicle_id})).scalar()
    if count:
        will_be_preserved["routes (will lose their assigned vehicle)"] = count

    return {
        "vehicle_id": vehicle_id,
        "will_be_deleted": will_be_deleted,
        "total_to_be_deleted": sum(will_be_deleted.values()),
        "will_be_preserved": will_be_preserved,
    }


@router.delete("/vehicles/{vehicle_id}", status_code=204)
async def delete_vehicle(
    vehicle_id: str,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    _plan_check: User = Depends(require_plan_feature("transport_module")),
    session: AsyncSession = Depends(get_session)
):
    """Delete a vehicle.

    Maintenance and attendance records tied to this vehicle are permanently
    deleted via DB-level CASCADE. Routes that had it assigned just lose the
    assignment (SET NULL) — the route itself isn't touched.
    """
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(Vehicle).where(
            and_(Vehicle.id == vehicle_id, Vehicle.school_id == school_id)
        )
    )
    vehicle = result.scalar_one_or_none()
    if not vehicle:
        raise HTTPException(status_code=404, detail="Vehicle not found")

    await session.delete(vehicle)
    await session.commit()


# ============================================================================
# ROUTE ENDPOINTS
# ============================================================================

@router.get("/routes", response_model=List[dict])
async def list_routes(
    status: Optional[RouteStatus] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    _plan_check: User = Depends(require_plan_feature("transport_module")),
    session: AsyncSession = Depends(get_session)
):
    """List transport routes. Drivers see only routes assigned to their vehicle."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    query = select(Route).where(Route.school_id == school_id)

    driver_route_ids = await get_driver_route_ids(current_user, session)
    if driver_route_ids is not None:
        if not driver_route_ids:
            return []
        query = query.where(Route.id.in_(driver_route_ids))

    if status:
        query = query.where(Route.status == status)

    query = query.offset(skip).limit(limit)
    result = await session.execute(query)
    routes = result.scalars().all()

    return [
        {
            **jsonable_encoder(r),
            "status": r.status.value,
            "pickup_days": json.loads(r.pickup_days) if isinstance(r.pickup_days, str) else r.pickup_days,
            "intermediate_stops": json.loads(r.intermediate_stops) if r.intermediate_stops and isinstance(r.intermediate_stops, str) else r.intermediate_stops
        }
        for r in routes
    ]


@router.post("/routes", response_model=dict)
async def create_route(
    route_data: RouteCreate,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    _plan_check: User = Depends(require_plan_feature("transport_module")),
    session: AsyncSession = Depends(get_session)
):
    """Create a new transport route"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    # Check if route code already exists in this school -- route codes are
    # only unique per-school (see uq_routes_school_route_code)
    result = await session.execute(
        select(Route).where(Route.route_code == route_data.route_code, Route.school_id == school_id)
    )
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Route with this code already exists")
    
    # Ensure pickup_days is stored as JSON string if it's a list
    pickup_days = route_data.pickup_days
    if isinstance(pickup_days, list):
        pickup_days = json.dumps(pickup_days)
    
    intermediate_stops = route_data.intermediate_stops
    if isinstance(intermediate_stops, list):
        intermediate_stops = json.dumps(intermediate_stops)
    
    route = Route(
        **route_data.dict(exclude={"pickup_days", "intermediate_stops"}),
        school_id=school_id,
        pickup_days=pickup_days,
        intermediate_stops=intermediate_stops
    )
    session.add(route)
    await session.commit()
    await session.refresh(route)
    
    return {
        **jsonable_encoder(route),
        "status": route.status.value,
        "pickup_days": json.loads(route.pickup_days) if isinstance(route.pickup_days, str) else route.pickup_days,
        "intermediate_stops": json.loads(route.intermediate_stops) if route.intermediate_stops and isinstance(route.intermediate_stops, str) else route.intermediate_stops
    }


@router.get("/routes/{route_id}", response_model=dict)
async def get_route(
    route_id: str,
    current_user: User = Depends(get_current_user),
    _plan_check: User = Depends(require_plan_feature("transport_module")),
    session: AsyncSession = Depends(get_session)
):
    """Get route details"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    result = await session.execute(
        select(Route).where(
            and_(Route.id == route_id, Route.school_id == school_id)
        )
    )
    route = result.scalar_one_or_none()
    if not route:
        raise HTTPException(status_code=404, detail="Route not found")

    driver_route_ids = await get_driver_route_ids(current_user, session)
    if driver_route_ids is not None and route_id not in driver_route_ids:
        raise HTTPException(status_code=403, detail="Not authorized for this route")

    return {
        **jsonable_encoder(route),
        "status": route.status.value,
        "pickup_days": json.loads(route.pickup_days) if isinstance(route.pickup_days, str) else route.pickup_days,
        "intermediate_stops": json.loads(route.intermediate_stops) if route.intermediate_stops and isinstance(route.intermediate_stops, str) else route.intermediate_stops
    }


@router.get("/routes/{route_id}/students", response_model=List[dict])
async def get_route_students(
    route_id: str,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    _plan_check: User = Depends(require_plan_feature("transport_module")),
    session: AsyncSession = Depends(get_session)
):
    """Get all students enrolled in a specific route"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    # Verify route exists
    result = await session.execute(
        select(Route).where(
            and_(Route.id == route_id, Route.school_id == school_id)
        )
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Route not found")

    driver_route_ids = await get_driver_route_ids(current_user, session)
    if driver_route_ids is not None and route_id not in driver_route_ids:
        raise HTTPException(status_code=403, detail="Not authorized for this route")

    # Get students enrolled in this route
    query = select(StudentTransport).where(
        and_(
            StudentTransport.route_id == route_id,
            StudentTransport.school_id == school_id
        )
    ).order_by(StudentTransport.created_at).offset(skip).limit(limit)
    
    result = await session.execute(query)
    enrollments = result.scalars().all()
    
    # Fetch student data for each enrollment
    enrollment_responses = []
    for e in enrollments:
        # Get student data
        student_result = await session.execute(
            select(Student).where(Student.id == e.student_id)
        )
        student = student_result.scalar_one_or_none()
        
        enrollment_dict = jsonable_encoder(e)
        if student:
            enrollment_dict["first_name"] = student.first_name
            enrollment_dict["last_name"] = student.last_name
            enrollment_dict["student_id"] = student.student_id
        
        enrollment_responses.append(enrollment_dict)
    
    return enrollment_responses


@router.put("/routes/{route_id}", response_model=dict)
async def update_route(
    route_id: str,
    route_data: RouteUpdate,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    _plan_check: User = Depends(require_plan_feature("transport_module")),
    session: AsyncSession = Depends(get_session)
):
    """Update route information"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    result = await session.execute(
        select(Route).where(
            and_(Route.id == route_id, Route.school_id == school_id)
        )
    )
    route = result.scalar_one_or_none()
    if not route:
        raise HTTPException(status_code=404, detail="Route not found")
    
    update_data = route_data.dict(exclude_unset=True)
    
    # Handle JSON fields
    if "pickup_days" in update_data and isinstance(update_data["pickup_days"], list):
        update_data["pickup_days"] = json.dumps(update_data["pickup_days"])
    if "intermediate_stops" in update_data and isinstance(update_data["intermediate_stops"], list):
        update_data["intermediate_stops"] = json.dumps(update_data["intermediate_stops"])
    
    for key, value in update_data.items():
        setattr(route, key, value)
    
    route.updated_at = datetime.utcnow()
    session.add(route)
    await session.commit()
    await session.refresh(route)
    
    return {
        **jsonable_encoder(route),
        "status": route.status.value,
        "pickup_days": json.loads(route.pickup_days) if isinstance(route.pickup_days, str) else route.pickup_days,
        "intermediate_stops": json.loads(route.intermediate_stops) if route.intermediate_stops and isinstance(route.intermediate_stops, str) else route.intermediate_stops
    }


ROUTE_CASCADE_TABLES = [
    ("student_transport", "student enrollments"),
    ("transport_attendance", "attendance records"),
    ("live_bus_locations", "live location pings"),
    ("route_stops", "route stops"),
]

ROUTE_PRESERVED_TABLES = [
    ("daily_qr_tokens", "daily QR tokens"),
    ("student_security_profiles", "student pickup/safety profiles"),
    ("transport_fees", "transport fee records"),
]


@router.get("/routes/{route_id}/delete-impact", response_model=dict)
async def get_route_delete_impact(
    route_id: str,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    _plan_check: User = Depends(require_plan_feature("transport_module")),
    session: AsyncSession = Depends(get_session)
):
    """Preview what deleting this route would affect, before actually deleting it."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(Route).where(and_(Route.id == route_id, Route.school_id == school_id))
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Route not found")

    will_be_deleted = {}
    for table, label in ROUTE_CASCADE_TABLES:
        count = (await session.execute(text(f"SELECT count(*) FROM {table} WHERE route_id = :rid"), {"rid": route_id})).scalar()
        if count:
            will_be_deleted[label] = count

    will_be_preserved = {}
    for table, label in ROUTE_PRESERVED_TABLES:
        col = "transport_route_id" if table == "student_security_profiles" else "route_id"
        count = (await session.execute(text(f"SELECT count(*) FROM {table} WHERE {col} = :rid"), {"rid": route_id})).scalar()
        if count:
            will_be_preserved[label] = count

    return {
        "route_id": route_id,
        "will_be_deleted": will_be_deleted,
        "total_to_be_deleted": sum(will_be_deleted.values()),
        "will_be_preserved": will_be_preserved,
    }


@router.delete("/routes/{route_id}", status_code=204)
async def delete_route(
    route_id: str,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    _plan_check: User = Depends(require_plan_feature("transport_module")),
    session: AsyncSession = Depends(get_session)
):
    """Delete a route.

    Student enrollments, attendance, and live-location pings tied to this route
    are permanently deleted via DB-level CASCADE. QR tokens, student safety
    profiles, and transport fees are never deleted — preserved with their route
    reference cleared via DB-level SET NULL.
    """
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(Route).where(
            and_(Route.id == route_id, Route.school_id == school_id)
        )
    )
    route = result.scalar_one_or_none()
    if not route:
        raise HTTPException(status_code=404, detail="Route not found")

    await session.delete(route)
    await session.commit()


# ============================================================================
# ROUTE STOPS — GPS-coordinate-bearing stops, additive alongside the
# existing plain-name Route.intermediate_stops field (see models/transport.py's
# RouteStop docstring for why this is a separate table). Powers
# routers/security.py's per-stop distance/ETA endpoint.
# ============================================================================

@router.get("/routes/{route_id}/stops", response_model=List[RouteStop])
async def list_route_stops(
    route_id: str,
    current_user: User = Depends(get_current_user),
    _plan_check: User = Depends(require_plan_feature("transport_module")),
    session: AsyncSession = Depends(get_session),
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    route = (await session.execute(select(Route).where(Route.id == route_id, Route.school_id == school_id))).scalar_one_or_none()
    if not route:
        raise HTTPException(status_code=404, detail="Route not found")
    result = await session.execute(select(RouteStop).where(RouteStop.route_id == route_id).order_by(RouteStop.sequence))
    return result.scalars().all()


@router.put("/routes/{route_id}/stops", response_model=List[RouteStop])
async def replace_route_stops(
    route_id: str,
    body: BulkRouteStopsRequest,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    _plan_check: User = Depends(require_plan_feature("transport_module")),
    session: AsyncSession = Depends(get_session),
):
    """Replaces this route's ENTIRE stop list in one call — the natural
    shape for a "define this route's stops" admin action (draw pins on a
    map, save once) rather than one create call per stop. Pass an empty
    list to clear all stops."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    route = (await session.execute(select(Route).where(Route.id == route_id, Route.school_id == school_id))).scalar_one_or_none()
    if not route:
        raise HTTPException(status_code=404, detail="Route not found")

    existing = await session.execute(select(RouteStop).where(RouteStop.route_id == route_id))
    for stop in existing.scalars().all():
        await session.delete(stop)
    await session.flush()

    new_stops = [
        RouteStop(school_id=school_id, route_id=route_id, **s.model_dump())
        for s in sorted(body.stops, key=lambda s: s.sequence)
    ]
    session.add_all(new_stops)
    await session.commit()
    for stop in new_stops:
        await session.refresh(stop)
    return new_stops


@router.delete("/routes/{route_id}/stops/{stop_id}", status_code=204)
async def delete_route_stop(
    route_id: str,
    stop_id: str,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    _plan_check: User = Depends(require_plan_feature("transport_module")),
    session: AsyncSession = Depends(get_session),
):
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    stop = (await session.execute(
        select(RouteStop).where(RouteStop.id == stop_id, RouteStop.route_id == route_id, RouteStop.school_id == school_id)
    )).scalar_one_or_none()
    if not stop:
        raise HTTPException(status_code=404, detail="Stop not found")
    await session.delete(stop)
    await session.commit()


# ============================================================================
# STUDENT TRANSPORT ENROLLMENT
# ============================================================================

async def _active_vehicle_occupancy(session: AsyncSession, vehicle_id: str) -> int:
    """Count active enrollments across every route that vehicle serves."""
    result = await session.execute(
        select(func.count(StudentTransport.id))
        .join(Route, Route.id == StudentTransport.route_id)
        .where(Route.vehicle_id == vehicle_id, StudentTransport.is_active == True)
    )
    return result.scalar() or 0


async def _check_route_capacity(session: AsyncSession, route: Route) -> None:
    """A route's seat capacity is its assigned vehicle's seating_capacity.
    A route with no vehicle assigned has nothing to enforce against.

    Locks the Vehicle row FOR UPDATE before counting occupancy: without it,
    two concurrent enrollment requests could both read the same occupancy
    count before either commits their INSERT, and both pass this check even
    though enrolling both would push the vehicle over capacity. Locking
    makes the second request block until the first's enrollment is visible,
    so its count reflects the first one's seat."""
    if not route.vehicle_id:
        return
    vehicle_result = await session.execute(select(Vehicle).where(Vehicle.id == route.vehicle_id).with_for_update())
    vehicle = vehicle_result.scalar_one_or_none()
    if not vehicle:
        return
    current = await _active_vehicle_occupancy(session, route.vehicle_id)
    if current >= vehicle.seating_capacity:
        raise HTTPException(
            status_code=400,
            detail=f"Vehicle {vehicle.registration_number} on this route is at full capacity ({vehicle.seating_capacity} seats)"
        )


async def _sync_transport_occupancy(session: AsyncSession, route_id: str) -> None:
    """Route.student_count and Vehicle.current_occupancy are persisted counters
    that no write path ever kept in sync (they're 0 for every school that didn't
    run the one-off seed script) -- recompute them fresh from active enrollments
    rather than mirror them with +=1/-=1, so a missed call site can't desync them."""
    route = await session.get(Route, route_id)
    if not route:
        return

    count_result = await session.execute(
        select(func.count(StudentTransport.id)).where(
            StudentTransport.route_id == route_id,
            StudentTransport.is_active == True
        )
    )
    route.student_count = count_result.scalar() or 0
    session.add(route)

    if route.vehicle_id:
        vehicle = await session.get(Vehicle, route.vehicle_id)
        if vehicle:
            vehicle.current_occupancy = await _active_vehicle_occupancy(session, route.vehicle_id)
            session.add(vehicle)


@router.post("/enrollments", response_model=dict)
async def enroll_student_transport(
    enrollment_data: StudentTransportCreate,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.PARENT)),
    _plan_check: User = Depends(require_plan_feature("transport_module")),
    session: AsyncSession = Depends(get_session)
):
    """Enroll a student in transport"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    # Verify student exists and belongs to school
    result = await session.execute(
        select(Student).where(
            and_(Student.id == enrollment_data.student_id, Student.school_id == school_id)
        )
    )
    student = result.scalar_one_or_none()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    if not await _has_student_access(session, current_user, student):
        raise HTTPException(status_code=403, detail="You can only enroll your own child in transport")

    # Verify route exists
    result = await session.execute(
        select(Route).where(
            and_(Route.id == enrollment_data.route_id, Route.school_id == school_id)
        )
    )
    route = result.scalar_one_or_none()
    if not route:
        raise HTTPException(status_code=404, detail="Route not found")

    # Check if already enrolled in this route
    result = await session.execute(
        select(StudentTransport).where(
            and_(
                StudentTransport.student_id == enrollment_data.student_id,
                StudentTransport.route_id == enrollment_data.route_id
            )
        )
    )
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Student is already enrolled in this route")

    await _check_route_capacity(session, route)

    enrollment = StudentTransport(
        **enrollment_data.dict(),
        school_id=school_id
    )
    session.add(enrollment)
    await session.commit()
    await session.refresh(enrollment)

    await _sync_transport_occupancy(session, enrollment.route_id)
    await session.commit()

    return jsonable_encoder(enrollment)


@router.get("/enrollments", response_model=List[dict])
async def get_all_enrollments(
    current_user: User = Depends(get_current_user),
    _plan_check: User = Depends(require_plan_feature("transport_module")),
    session: AsyncSession = Depends(get_session)
):
    """Get all student transport enrollments for the school"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    result = await session.execute(
        select(StudentTransport).where(
            StudentTransport.school_id == school_id
        )
    )
    enrollments = result.scalars().all()
    
    # Fetch student and route data for each enrollment
    enrollment_responses = []
    for e in enrollments:
        # Get student data
        student_result = await session.execute(
            select(Student).where(Student.id == e.student_id)
        )
        student = student_result.scalar_one_or_none()
        
        # Get route data
        route_result = await session.execute(
            select(Route).where(Route.id == e.route_id)
        )
        route = route_result.scalar_one_or_none()
        
        enrollment_dict = jsonable_encoder(e)
        if student:
            enrollment_dict["student"] = jsonable_encoder(student)
        if route:
            enrollment_dict["route"] = {
                "id": route.id,
                "route_name": route.route_name,
                "route_code": route.route_code,
                "status": route.status.value
            }
        
        enrollment_responses.append(enrollment_dict)
    
    return enrollment_responses


@router.get("/enrollment/{student_id}", response_model=List[dict])
async def get_student_routes(
    student_id: str,
    current_user: User = Depends(get_current_user),
    _plan_check: User = Depends(require_plan_feature("transport_module")),
    session: AsyncSession = Depends(get_session)
):
    """Get all routes for a student"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    student_result = await session.execute(select(Student).where(Student.id == student_id, Student.school_id == school_id))
    student = student_result.scalar_one_or_none()
    if not student or not await _has_student_access(session, current_user, student):
        raise HTTPException(status_code=403, detail="Access denied")

    result = await session.execute(
        select(StudentTransport).where(
            and_(
                StudentTransport.student_id == student_id,
                StudentTransport.school_id == school_id
            )
        )
    )
    enrollments = result.scalars().all()
    
    return [jsonable_encoder(e) for e in enrollments]


@router.put("/enrollments/{enrollment_id}", response_model=dict)
async def update_enrollment(
    enrollment_id: str,
    enrollment_data: StudentTransportUpdate,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    _plan_check: User = Depends(require_plan_feature("transport_module")),
    session: AsyncSession = Depends(get_session)
):
    """Update student transport enrollment"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    result = await session.execute(
        select(StudentTransport).where(
            and_(
                StudentTransport.id == enrollment_id,
                StudentTransport.school_id == school_id
            )
        )
    )
    enrollment = result.scalar_one_or_none()
    if not enrollment:
        raise HTTPException(status_code=404, detail="Enrollment not found")

    student_result = await session.execute(
        select(Student).where(Student.id == enrollment.student_id, Student.school_id == school_id)
    )
    student = student_result.scalar_one_or_none()
    if student and not await _has_student_access(session, current_user, student):
        raise HTTPException(status_code=403, detail="Access denied")

    old_route_id = enrollment.route_id
    old_is_active = enrollment.is_active

    update_data = enrollment_data.dict(exclude_unset=True)
    new_route_id = update_data.get("route_id", old_route_id)
    new_is_active = update_data.get("is_active", old_is_active)

    # A capacity check is only needed when this update would newly occupy a
    # seat: switching to a different route, or reactivating a dropped enrollment.
    if new_is_active and (new_route_id != old_route_id or not old_is_active):
        route_result = await session.execute(
            select(Route).where(
                and_(Route.id == new_route_id, Route.school_id == school_id)
            )
        )
        new_route = route_result.scalar_one_or_none()
        if not new_route:
            raise HTTPException(status_code=404, detail="Route not found")
        await _check_route_capacity(session, new_route)

    for key, value in update_data.items():
        setattr(enrollment, key, value)

    enrollment.updated_at = datetime.utcnow()
    session.add(enrollment)
    await session.commit()
    await session.refresh(enrollment)

    await _sync_transport_occupancy(session, old_route_id)
    if new_route_id != old_route_id:
        await _sync_transport_occupancy(session, new_route_id)
    await session.commit()

    return jsonable_encoder(enrollment)


@router.delete("/enrollments/{enrollment_id}", status_code=204)
async def remove_enrollment(
    enrollment_id: str,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    _plan_check: User = Depends(require_plan_feature("transport_module")),
    session: AsyncSession = Depends(get_session)
):
    """Remove student from transport"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    result = await session.execute(
        select(StudentTransport).where(
            and_(
                StudentTransport.id == enrollment_id,
                StudentTransport.school_id == school_id
            )
        )
    )
    enrollment = result.scalar_one_or_none()
    if not enrollment:
        raise HTTPException(status_code=404, detail="Enrollment not found")

    route_id = enrollment.route_id
    await session.delete(enrollment)
    await session.commit()

    await _sync_transport_occupancy(session, route_id)
    await session.commit()


# ============================================================================
# TRANSPORT ATTENDANCE
# ============================================================================

@router.post("/attendance", response_model=dict)
async def mark_attendance(
    attendance_data: TransportAttendanceCreate,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.TEACHER)),
    _plan_check: User = Depends(require_plan_feature("transport_module")),
    session: AsyncSession = Depends(get_session)
):
    """Mark student attendance for transport"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    # Verify route exists — school_id scoped, without which a teacher could
    # mark attendance referencing another school's route.
    result = await session.execute(
        select(Route).where(Route.id == attendance_data.route_id, Route.school_id == school_id)
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Route not found")

    # Verify vehicle exists — same school_id scoping rationale as above.
    result = await session.execute(
        select(Vehicle).where(Vehicle.id == attendance_data.vehicle_id, Vehicle.school_id == school_id)
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Vehicle not found")
    
    attendance = TransportAttendance(
        **attendance_data.dict(),
        school_id=school_id
    )
    session.add(attendance)
    await session.commit()
    await session.refresh(attendance)

    if attendance.trip_type == "dropoff" and attendance.status == AttendanceStatus.PRESENT:
        await sync_dropoff_to_arrival_status(session, school_id, attendance.student_id, current_user.id)

    return {
        **jsonable_encoder(attendance),
        "status": attendance.status.value,
        "trip_type": attendance.trip_type
    }


@router.post("/attendance/bulk", response_model=dict)
async def mark_bulk_attendance(
    bulk_data: TransportAttendanceBulk,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.TEACHER)),
    _plan_check: User = Depends(require_plan_feature("transport_module")),
    session: AsyncSession = Depends(get_session)
):
    """Mark attendance for multiple students"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    records = []
    for att_data in bulk_data.attendance_records:
        attendance = TransportAttendance(
            **att_data.dict(),
            school_id=school_id
        )
        session.add(attendance)
        records.append(attendance)

    await session.commit()

    for attendance in records:
        if attendance.trip_type == "dropoff" and attendance.status == AttendanceStatus.PRESENT:
            await sync_dropoff_to_arrival_status(session, school_id, attendance.student_id, current_user.id)

    return {
        "total_records": len(records),
        "created_at": datetime.utcnow().isoformat(),
        "message": f"Successfully marked attendance for {len(records)} student(s)"
    }


@router.get("/attendance/{route_id}", response_model=List[dict])
async def get_route_attendance(
    route_id: str,
    attendance_date: Optional[str] = None,
    trip_type: Optional[str] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    current_user: User = Depends(get_current_user),
    _plan_check: User = Depends(require_plan_feature("transport_module")),
    session: AsyncSession = Depends(get_session)
):
    """Get attendance records for a route"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    query = select(TransportAttendance).where(
        and_(
            TransportAttendance.route_id == route_id,
            TransportAttendance.school_id == school_id
        )
    )
    
    if attendance_date:
        query = query.where(TransportAttendance.attendance_date == attendance_date)
    if trip_type:
        query = query.where(TransportAttendance.trip_type == trip_type)
    
    query = query.offset(skip).limit(limit)
    result = await session.execute(query)
    records = result.scalars().all()
    
    return [
        {
            **jsonable_encoder(r),
            "status": r.status.value
        }
        for r in records
    ]


# ============================================================================
# TRANSPORT FEES
# ============================================================================

@router.post("/fees", response_model=dict)
async def create_transport_fee(
    fee_data: TransportFeeCreate,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    _plan_check: User = Depends(require_plan_feature("transport_module")),
    session: AsyncSession = Depends(get_session)
):
    """Create transport fee for a student"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    # Verify student exists — school_id scoped, without which a School A
    # admin could reference a School B student and create a cross-tenant
    # transport fee (and, if paid on creation, a cross-tenant GL posting).
    result = await session.execute(
        select(Student).where(Student.id == fee_data.student_id, Student.school_id == school_id)
    )
    student = result.scalar_one_or_none()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    if not await _has_student_access(session, current_user, student):
        raise HTTPException(status_code=403, detail="Access denied")

    # Verify route exists — same school_id scoping rationale as above.
    result = await session.execute(
        select(Route).where(Route.id == fee_data.route_id, Route.school_id == school_id)
    )
    route = result.scalar_one_or_none()
    if not route:
        raise HTTPException(status_code=404, detail="Route not found")

    # Double-submit guard: reject an identical fee (same student, route,
    # term, type and amount) created in the last 30 seconds, mirroring the
    # fee-payment guard in routers/fees.py::record_payment.
    dupe_cutoff = datetime.utcnow() - timedelta(seconds=30)
    dupe_result = await session.execute(
        select(TransportFee).where(
            and_(
                TransportFee.school_id == school_id,
                TransportFee.student_id == fee_data.student_id,
                TransportFee.route_id == fee_data.route_id,
                TransportFee.academic_term_id == fee_data.academic_term_id,
                TransportFee.fee_type == fee_data.fee_type,
                TransportFee.amount_due == route.fee_amount,
                TransportFee.created_at >= dupe_cutoff,
            )
        )
    )
    if dupe_result.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="An identical transport fee was just created — avoid double-submitting")

    # Create fee and calculate is_paid (amount_paid + discount >= amount_due).
    # amount_due is derived from the route's own fee_amount, not trusted from
    # the client -- Route has a single fee_amount field with no per-fee_type
    # rate breakdown, so it's the only real source of truth for what this
    # route actually costs, the same "derive from the real record, don't
    # trust the caller's number" fix already applied to
    # routers/fees.py::create_student_fee.
    fee_dict = fee_data.dict()
    fee_dict['amount_due'] = route.fee_amount
    amount_paid = fee_dict.get('amount_paid', 0.0)
    amount_due = fee_dict['amount_due']
    discount = fee_dict.get('discount', 0.0)
    if discount > amount_due:
        raise HTTPException(status_code=400, detail="discount cannot exceed the route's fee amount")
    total_covered = amount_paid + discount
    is_paid = total_covered >= amount_due
    
    fee = TransportFee(
        **fee_dict,
        school_id=school_id,
        is_paid=is_paid
    )
    session.add(fee)
    await session.commit()
    await session.refresh(fee)
    
    # Auto-post to GL if any payment is made on creation (partial or full)
    journal_entry_id = None
    if amount_paid > 0:
        try:
            journal_entry_id = await _create_transport_journal_entry(
                session=session,
                school_id=school_id,
                fee_id=fee.id,
                student_id=fee.student_id,
                route_id=fee.route_id,
                payment_amount=amount_paid,
                payment_method=fee_dict.get("payment_method", "cash")
            )
            
            # Update fee with GL posting info
            fee.gl_journal_entry_id = journal_entry_id
            fee.gl_posted_date = datetime.utcnow().isoformat().split('T')[0]
            session.add(fee)
            await session.commit()
            await session.refresh(fee)
            
            logger.info(f"Created journal entry {journal_entry_id} for transport fee {fee.id} (payment: GHS {amount_paid})")
        except Exception as e:
            logger.error(f"Error posting transport fee to GL on creation: {str(e)}")
            # Continue anyway - fee is recorded even if GL posting fails
    
    response = {
        **jsonable_encoder(fee),
        "fee_type": fee.fee_type.value
    }
    
    if journal_entry_id:
        response["journal_entry_id"] = journal_entry_id
    
    return response


@router.get("/fees", response_model=List[dict])
async def get_all_transport_fees(
    is_paid: Optional[bool] = None,
    needs_gl_reconciliation: Optional[bool] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    _plan_check: User = Depends(require_plan_feature("transport_module")),
    session: AsyncSession = Depends(get_session)
):
    """Get all transport fees for the school with pagination, including student and route details

    needs_gl_reconciliation=true surfaces fees that received a payment but
    have no gl_journal_entry_id -- GL posting is best-effort (see
    _create_transport_journal_entry) and its failures were previously only
    logged, with no reconciliation job covering transport fees at all, so
    a failed post was otherwise permanently invisible to admins.
    """
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    query = select(TransportFee).where(TransportFee.school_id == school_id)

    if is_paid is not None:
        query = query.where(TransportFee.is_paid == is_paid)
    if needs_gl_reconciliation:
        query = query.where(TransportFee.amount_paid > 0, TransportFee.gl_journal_entry_id.is_(None))

    query = query.order_by(TransportFee.created_at.desc()).offset(skip).limit(limit)
    result = await session.execute(query)
    fees = result.scalars().all()
    
    # Fetch student and route details for each fee
    response = []
    for f in fees:
        fee_data = {
            **jsonable_encoder(f),
            "fee_type": f.fee_type.value
        }
        
        # Get student details
        student_result = await session.execute(
            select(Student).where(Student.id == f.student_id)
        )
        student = student_result.scalar_one_or_none()
        if student:
            fee_data["student"] = {
                "id": student.id,
                "first_name": student.first_name,
                "last_name": student.last_name
            }
        
        # Get route details
        route_result = await session.execute(
            select(Route).where(Route.id == f.route_id)
        )
        route = route_result.scalar_one_or_none()
        if route:
            fee_data["route"] = {
                "id": route.id,
                "route_name": route.route_name,
                "route_code": route.route_code,
                "fee_amount": route.fee_amount
            }
        
        response.append(fee_data)
    
    return response


@router.get("/fees/{student_id}", response_model=List[dict])
async def get_student_transport_fees(
    student_id: str,
    is_paid: Optional[bool] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    _plan_check: User = Depends(require_plan_feature("transport_module")),
    session: AsyncSession = Depends(get_session)
):
    """Get transport fees for a student with route details"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    student_result = await session.execute(select(Student).where(Student.id == student_id, Student.school_id == school_id))
    student = student_result.scalar_one_or_none()
    if not student or not await _has_student_access(session, current_user, student):
        raise HTTPException(status_code=403, detail="Access denied")

    query = select(TransportFee).where(
        and_(
            TransportFee.student_id == student_id,
            TransportFee.school_id == school_id
        )
    )
    
    if is_paid is not None:
        query = query.where(TransportFee.is_paid == is_paid)
    
    query = query.offset(skip).limit(limit)
    result = await session.execute(query)
    fees = result.scalars().all()
    
    # Fetch route details for each fee
    response = []
    for f in fees:
        fee_data = {
            **jsonable_encoder(f),
            "fee_type": f.fee_type.value
        }
        
        # Get route details
        route_result = await session.execute(
            select(Route).where(Route.id == f.route_id)
        )
        route = route_result.scalar_one_or_none()
        if route:
            fee_data["route"] = {
                "id": route.id,
                "route_name": route.route_name,
                "route_code": route.route_code,
                "fee_amount": route.fee_amount
            }
        
        response.append(fee_data)
    
    return response


@router.put("/fees/{fee_id}", response_model=dict)
async def update_transport_fee(
    fee_id: str,
    fee_data: TransportFeeUpdate,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    _plan_check: User = Depends(require_plan_feature("transport_module")),
    session: AsyncSession = Depends(get_session)
):
    """Update transport fee payment status and auto-post to GL"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    # Locked FOR UPDATE: this endpoint takes a client-supplied absolute
    # amount_paid and diffs it against the row's current value to decide
    # the GL posting amount. Without a lock, two concurrent submissions
    # (double-click, retry) both read the same stale old_amount_paid and
    # each post their own GL journal entry for the same payment delta —
    # the fee's amount_paid itself lands correctly (last write wins on the
    # same value) but the GL ends up double-booked. Locking makes the
    # second request block until the first's write is visible, so its
    # delta comes out as zero instead of a duplicate.
    result = await session.execute(
        select(TransportFee).where(
            and_(TransportFee.id == fee_id, TransportFee.school_id == school_id)
        ).with_for_update()
    )
    fee = result.scalar_one_or_none()
    if not fee:
        raise HTTPException(status_code=404, detail="Fee not found")

    update_data = fee_data.dict(exclude_unset=True)
    old_amount_paid = fee.amount_paid

    # Ensure amount_paid doesn't exceed amount_due — previously unclamped
    # here (unlike routers/hostel.py::update_hostel_fee's equivalent),
    # so an overpayment would be posted to GL in full, potentially
    # exceeding the fee actually owed.
    if "amount_paid" in update_data:
        new_amount_paid = update_data.get("amount_paid")
        amount_due_for_clamp = update_data.get("amount_due", fee.amount_due)
        if new_amount_paid > amount_due_for_clamp:
            update_data["amount_paid"] = amount_due_for_clamp

    # Auto-calculate is_paid based on amount_paid and amount_due
    if "amount_paid" in update_data or "amount_due" in update_data:
        amount_due = update_data.get("amount_due", fee.amount_due)
        amount_paid = update_data.get("amount_paid", fee.amount_paid)
        discount = update_data.get("discount", fee.discount)

        total_covered = amount_paid + discount
        update_data["is_paid"] = total_covered >= amount_due
    
    for key, value in update_data.items():
        setattr(fee, key, value)
    
    fee.updated_at = datetime.utcnow()
    session.add(fee)
    await session.commit()
    await session.refresh(fee)
    
    # Auto-post to GL if payment was recorded
    journal_entry_id = None
    if "amount_paid" in update_data and update_data["amount_paid"] > old_amount_paid:
        payment_amount = update_data["amount_paid"] - old_amount_paid
        try:
            journal_entry_id = await _create_transport_journal_entry(
                session=session,
                school_id=school_id,
                fee_id=fee_id,
                student_id=fee.student_id,
                route_id=fee.route_id,
                payment_amount=payment_amount,
                payment_method=update_data.get("payment_method", "cash")
            )
            
            # Update fee with GL posting info
            fee.gl_journal_entry_id = journal_entry_id
            fee.gl_posted_date = datetime.utcnow().isoformat().split('T')[0]
            session.add(fee)
            await session.commit()
            
            logger.info(f"Created journal entry {journal_entry_id} for transport fee payment {fee_id}")
        except Exception as e:
            logger.error(f"Error posting transport fee to GL: {str(e)}")
            # Continue anyway - payment is recorded even if GL posting fails
    
    response = {
        **jsonable_encoder(fee),
        "fee_type": fee.fee_type.value
    }
    
    if journal_entry_id:
        response["journal_entry_id"] = journal_entry_id
    
    return response


@router.delete("/fees/{fee_id}", status_code=204)
async def delete_transport_fee(
    fee_id: str,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    _plan_check: User = Depends(require_plan_feature("transport_module")),
    session: AsyncSession = Depends(get_session)
):
    """Delete a transport fee"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    result = await session.execute(
        select(TransportFee).where(
            and_(TransportFee.id == fee_id, TransportFee.school_id == school_id)
        )
    )
    fee = result.scalar_one_or_none()
    if not fee:
        raise HTTPException(status_code=404, detail="Fee not found")

    # Reverse the GL journal entry this fee was posted under (if any) before
    # deleting it — otherwise the ledger permanently overstates cash/revenue
    # for a fee that no longer even exists in fee records. Best-effort: a GL
    # problem here shouldn't block the delete itself, same trade-off as
    # routers/fees.py::void_payment's identical reversal-before-void step.
    if fee.gl_journal_entry_id:
        try:
            from services.journal_entry_service import JournalEntryService
            journal_service = JournalEntryService(session)
            await journal_service.reverse_entry(
                school_id=school_id,
                entry_id=fee.gl_journal_entry_id,
                reversed_by=current_user.id,
                reversal_reason=f"Transport fee {fee_id} deleted",
            )
        except Exception as e:
            logger.error(f"Error reversing journal entry {fee.gl_journal_entry_id} for deleted transport fee {fee_id}: {str(e)}")

    await session.delete(fee)
    await session.commit()
    logger.info(f"Deleted transport fee {fee_id}")


@router.get("/fees-summary", response_model=dict)
async def get_transport_fees_summary(
    academic_term_id: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    _plan_check: User = Depends(require_plan_feature("transport_module")),
    session: AsyncSession = Depends(get_session)
):
    """Get transport fees summary for the school"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    query = select(TransportFee).where(TransportFee.school_id == school_id)
    
    if academic_term_id:
        query = query.where(TransportFee.academic_term_id == academic_term_id)
    
    result = await session.execute(query)
    fees = result.scalars().all()
    
    total_due = sum(f.amount_due for f in fees)
    total_paid = sum(f.amount_paid for f in fees)
    total_discount = sum(f.discount for f in fees)
    total_outstanding = total_due - total_paid - total_discount
    
    paid_count = sum(1 for f in fees if f.is_paid)
    unpaid_count = len(fees) - paid_count
    
    return {
        "total_due": total_due,
        "total_paid": total_paid,
        "total_discount": total_discount,
        "total_outstanding": total_outstanding,
        "collection_rate": round((total_paid / total_due * 100) if total_due > 0 else 0, 2),
        "total_students": len(set(f.student_id for f in fees)),
        "fees_paid": paid_count,
        "fees_unpaid": unpaid_count
    }


# ============================================================================
# VEHICLE MAINTENANCE
# ============================================================================

@router.post("/maintenance", response_model=dict)
async def record_maintenance(
    maintenance_data: VehicleMaintenanceCreate,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    _plan_check: User = Depends(require_plan_feature("transport_module")),
    session: AsyncSession = Depends(get_session)
):
    """Record vehicle maintenance"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    # Verify vehicle exists — school_id scoped, without which an admin could
    # record maintenance against another school's vehicle.
    result = await session.execute(
        select(Vehicle).where(Vehicle.id == maintenance_data.vehicle_id, Vehicle.school_id == school_id)
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Vehicle not found")

    if maintenance_data.contractor_id:
        contractor = await session.execute(select(FacilityContractor).where(FacilityContractor.id == maintenance_data.contractor_id, FacilityContractor.school_id == school_id))
        if not contractor.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="Contractor not found in this school")
    if maintenance_data.work_order_id:
        work_order = await session.execute(select(FacilityWorkOrder).where(FacilityWorkOrder.id == maintenance_data.work_order_id, FacilityWorkOrder.school_id == school_id))
        if not work_order.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="Work order not found in this school")

    maintenance = VehicleMaintenance(
        **maintenance_data.dict(),
        school_id=school_id
    )
    session.add(maintenance)
    await session.commit()
    await session.refresh(maintenance)
    
    return jsonable_encoder(maintenance)


@router.get("/maintenance/{vehicle_id}", response_model=List[dict])
async def get_vehicle_maintenance_history(
    vehicle_id: str,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    _plan_check: User = Depends(require_plan_feature("transport_module")),
    session: AsyncSession = Depends(get_session)
):
    """Get maintenance history for a vehicle"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    query = select(VehicleMaintenance).where(
        and_(
            VehicleMaintenance.vehicle_id == vehicle_id,
            VehicleMaintenance.school_id == school_id
        )
    ).order_by(VehicleMaintenance.maintenance_date.desc())
    
    query = query.offset(skip).limit(limit)
    result = await session.execute(query)
    records = result.scalars().all()
    
    return [jsonable_encoder(r) for r in records]


# ============================================================================
# DRIVER/CONDUCTOR STAFF
# ============================================================================

@router.post("/drivers", response_model=dict)
async def register_driver(
    driver_data: DriverStaffCreate,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    _plan_check: User = Depends(require_plan_feature("transport_module")),
    session: AsyncSession = Depends(get_session)
):
    """Register a driver or conductor"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    # Check if license already registered in this school -- license numbers
    # are only unique per-school (see uq_driver_staff_school_license); the
    # same license can legitimately be registered at more than one school.
    result = await session.execute(
        select(DriverStaff).where(DriverStaff.license_number == driver_data.license_number, DriverStaff.school_id == school_id)
    )
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="License number already registered")
    
    driver = DriverStaff(
        **driver_data.dict(),
        school_id=school_id
    )
    session.add(driver)
    try:
        await session.commit()
    except IntegrityError as e:
        await session.rollback()
        if "ix_driver_staff_staff_id" in str(e):
            raise HTTPException(status_code=409, detail="This staff member is already registered as a driver")
        elif "license_number" in str(e):
            raise HTTPException(status_code=400, detail="License number already registered")
        else:
            raise HTTPException(status_code=422, detail="Duplicate entry detected")
    await session.refresh(driver)
    
    return jsonable_encoder(driver)


@router.get("/drivers", response_model=List[dict])
async def list_drivers(
    role: Optional[str] = None,
    is_active: Optional[bool] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    _plan_check: User = Depends(require_plan_feature("transport_module")),
    session: AsyncSession = Depends(get_session)
):
    """List all drivers and conductors"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    logger.info(f"List drivers called with role={role}, is_active={is_active}")
    
    query = select(DriverStaff).where(DriverStaff.school_id == school_id)
    
    if role:
        logger.info(f"Filtering by role: {role}")
        query = query.where(DriverStaff.role == role)
    if is_active is not None:
        logger.info(f"Filtering by is_active: {is_active}")
        query = query.where(DriverStaff.is_active == is_active)
    
    query = query.offset(skip).limit(limit)
    result = await session.execute(query)
    drivers = result.scalars().all()
    
    logger.info(f"Found {len(drivers)} drivers")
    
    return [jsonable_encoder(d) for d in drivers]


@router.get("/drivers/{driver_id}", response_model=dict)
async def get_driver(
    driver_id: str,
    current_user: User = Depends(get_current_user),
    _plan_check: User = Depends(require_plan_feature("transport_module")),
    session: AsyncSession = Depends(get_session)
):
    """Get driver details"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    result = await session.execute(
        select(DriverStaff).where(
            and_(DriverStaff.id == driver_id, DriverStaff.school_id == school_id)
        )
    )
    driver = result.scalar_one_or_none()
    if not driver:
        raise HTTPException(status_code=404, detail="Driver not found")
    
    return jsonable_encoder(driver)


@router.put("/drivers/{driver_id}", response_model=dict)
async def update_driver(
    driver_id: str,
    driver_data: DriverStaffUpdate,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    _plan_check: User = Depends(require_plan_feature("transport_module")),
    session: AsyncSession = Depends(get_session)
):
    """Update driver information"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    result = await session.execute(
        select(DriverStaff).where(
            and_(DriverStaff.id == driver_id, DriverStaff.school_id == school_id)
        )
    )
    driver = result.scalar_one_or_none()
    if not driver:
        raise HTTPException(status_code=404, detail="Driver not found")
    
    update_data = driver_data.dict(exclude_unset=True)
    for key, value in update_data.items():
        setattr(driver, key, value)
    
    driver.updated_at = datetime.utcnow()
    session.add(driver)
    await session.commit()
    await session.refresh(driver)
    
    return jsonable_encoder(driver)


@router.put("/drivers/{driver_id}/verify", response_model=dict)
async def verify_driver(
    driver_id: str,
    body: VerifyDriverRequest = VerifyDriverRequest(),
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    _plan_check: User = Depends(require_plan_feature("transport_module")),
    session: AsyncSession = Depends(get_session)
):
    """Verify a driver"""
    verification_notes = body.verification_notes
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    result = await session.execute(
        select(DriverStaff).where(
            and_(DriverStaff.id == driver_id, DriverStaff.school_id == school_id)
        )
    )
    driver = result.scalar_one_or_none()
    if not driver:
        raise HTTPException(status_code=404, detail="Driver not found")
    
    driver.is_verified = True
    driver.verification_date = datetime.utcnow().isoformat()
    driver.notes = verification_notes or driver.notes
    driver.updated_at = datetime.utcnow()
    
    session.add(driver)
    await session.commit()
    await session.refresh(driver)
    
    logger.info(f"Driver {driver_id} verified by {current_user.id}")
    
    return jsonable_encoder(driver)


@router.get("/drivers/{driver_id}/delete-impact", response_model=dict)
async def get_driver_delete_impact(
    driver_id: str,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    _plan_check: User = Depends(require_plan_feature("transport_module")),
    session: AsyncSession = Depends(get_session)
):
    """Preview what deleting this driver would affect, before actually deleting it."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(DriverStaff).where(and_(DriverStaff.id == driver_id, DriverStaff.school_id == school_id))
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Driver not found")

    will_be_deleted = {}
    count = (await session.execute(text("SELECT count(*) FROM live_bus_locations WHERE driver_id = :did"), {"did": driver_id})).scalar()
    if count:
        will_be_deleted["live location pings"] = count

    will_be_preserved = {}
    count = (await session.execute(
        text("SELECT count(*) FROM vehicles WHERE driver_id = :did OR conductor_id = :did"), {"did": driver_id}
    )).scalar()
    if count:
        will_be_preserved["vehicles (will lose this driver/conductor assignment)"] = count

    return {
        "driver_id": driver_id,
        "will_be_deleted": will_be_deleted,
        "total_to_be_deleted": sum(will_be_deleted.values()),
        "will_be_preserved": will_be_preserved,
    }


@router.delete("/drivers/{driver_id}", status_code=204)
async def delete_driver(
    driver_id: str,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    _plan_check: User = Depends(require_plan_feature("transport_module")),
    session: AsyncSession = Depends(get_session)
):
    """Delete a driver.

    Live location pings tied to this driver are permanently deleted via DB-level
    CASCADE. Vehicles that had them assigned as driver/conductor just lose that
    assignment (SET NULL) — the vehicle itself isn't touched.
    """
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(DriverStaff).where(
            and_(DriverStaff.id == driver_id, DriverStaff.school_id == school_id)
        )
    )
    driver = result.scalar_one_or_none()
    if not driver:
        raise HTTPException(status_code=404, detail="Driver not found")

    await session.delete(driver)
    await session.commit()

    logger.info(f"Deleted driver {driver_id}")


# ============================================================================
# GL AUTO-POSTING HELPER FOR TRANSPORT FEES
# ============================================================================

async def _create_transport_journal_entry(
    session: AsyncSession,
    school_id: str,
    fee_id: str,
    student_id: str,
    route_id: str,
    payment_amount: float,
    payment_method: str
) -> str:
    """
    Create GL journal entry for transport fee payment.
    
    Posts:
    - Dr. GL account based on payment method (1001=Cash, 1010=Bank, 1015=Mobile)
    - Cr. 4155 (Transport Fee Revenue)
    
    Args:
        session: AsyncSession
        school_id: School identifier
        fee_id: Transport fee ID for reference
        student_id: Student ID
        route_id: Route ID
        payment_amount: Amount paid
        payment_method: Payment method (cash, bank_transfer, mobile_money, cheque)
        
    Returns:
        Journal entry ID
        
    Raises:
        Exception: If GL accounts not found or GL posting fails
    """
    from services.journal_entry_service import JournalEntryService
    from models.finance import JournalEntryCreate, JournalLineItemCreate, ReferenceType
    from models.finance.chart_of_accounts import GLAccount
    
    # Map payment method to GL bank account
    GL_BANK_ACCOUNTS = {
        "cash": "1001",              # Cash in Hand
        "bank_transfer": "1010",     # Business Checking Account
        "bank": "1010",              # Alias
        "mobile_money": "1015",      # Mobile Money Account
        "mobile": "1015",            # Alias
        "cheque": "1010",            # Bank Account
    }
    
    GL_TRANSPORT_REVENUE = "4155"   # Transport Fee Revenue
    
    # Get bank account code based on payment method
    bank_account_code = GL_BANK_ACCOUNTS.get(payment_method.lower(), "1001")
    
    try:
        # Get bank GL account
        bank_result = await session.execute(
            select(GLAccount).where(
                and_(
                    GLAccount.school_id == school_id,
                    GLAccount.account_code == bank_account_code,
                    GLAccount.is_active == True
                )
            )
        )
        bank_account = bank_result.scalar_one_or_none()
        
        if not bank_account:
            raise Exception(f"GL Account {bank_account_code} ({payment_method}) not found or inactive")
        
        # Get revenue GL account
        revenue_result = await session.execute(
            select(GLAccount).where(
                and_(
                    GLAccount.school_id == school_id,
                    GLAccount.account_code == GL_TRANSPORT_REVENUE,
                    GLAccount.is_active == True
                )
            )
        )
        revenue_account = revenue_result.scalar_one_or_none()
        
        if not revenue_account:
            raise Exception(f"GL Account {GL_TRANSPORT_REVENUE} (Transport Revenue) not found or inactive")
        
        # Get student for description
        student_result = await session.execute(
            select(Student).where(Student.id == student_id)
        )
        student = student_result.scalar_one_or_none()
        student_name = f"{student.first_name} {student.last_name}" if student else "Unknown"
        
        # Get route for description
        route_result = await session.execute(
            select(Route).where(Route.id == route_id)
        )
        route = route_result.scalar_one_or_none()
        route_name = route.route_name if route else "Unknown Route"
        
        # Create journal line items
        line_items = [
            # Debit: Bank account (payment received)
            JournalLineItemCreate(
                gl_account_id=bank_account.id,
                debit_amount=float(payment_amount),
                credit_amount=0.0,
                description=f"Transport fee payment from {student_name} ({route_name})"
            ),
            # Credit: Revenue account (fee income)
            JournalLineItemCreate(
                gl_account_id=revenue_account.id,
                debit_amount=0.0,
                credit_amount=float(payment_amount),
                description=f"Transport fee income from {student_name} ({route_name})"
            )
        ]
        
        # Create journal entry
        entry_data = JournalEntryCreate(
            entry_date=datetime.utcnow().isoformat().split('T')[0],
            reference_type=ReferenceType.FEE_PAYMENT,
            reference_id=fee_id,
            description=f"Transport fee payment from {student_name} ({route_name})",
            line_items=line_items,
            notes=f"Auto-posted from transport fee {fee_id} - Payment method: {payment_method}"
        )
        
        # Use JournalEntryService to create and post entry
        journal_service = JournalEntryService(session)
        entry = await journal_service.create_entry(
            school_id=school_id,
            entry_data=entry_data,
            created_by="SYSTEM"
        )
        
        # Post the entry immediately
        posted_entry = await journal_service.post_entry(
            school_id=school_id,
            entry_id=entry.id,
            posted_by="SYSTEM",
            approval_notes="Auto-posted from transport fee payment"
        )

        from services.gl_audit_log_service import GLAuditLogService
        from models.finance.gl_audit_log import AuditActionType, AuditEntityType
        try:
            await GLAuditLogService(session).log_action(
                school_id=school_id,
                entity_type=AuditEntityType.JOURNAL_ENTRY,
                entity_id=posted_entry.id,
                action=AuditActionType.ENTRY_POSTED,
                user_id="SYSTEM",
                user_name="System (transport fee auto-posting)",
                user_role="system",
                new_values={
                    "fee_id": fee_id, "student_id": student_id, "route_id": route_id,
                    "payment_amount": payment_amount, "payment_method": payment_method,
                },
            )
        except Exception as e:
            logger.warning(f"Failed to write GL audit log for transport fee journal entry {posted_entry.id}: {e}")

        return posted_entry.id

    except Exception as e:
        logger.error(f"Error creating transport fee journal entry: {str(e)}")
        raise
