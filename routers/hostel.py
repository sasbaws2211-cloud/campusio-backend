"""Hostel Management Router"""
from fastapi import APIRouter, Depends, HTTPException, status, Query
from fastapi.encoders import jsonable_encoder
from sqlmodel import select, func, and_, SQLModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime, timedelta
from typing import Optional, List
import uuid
import logging
from models.hostel import (
    Hostel, HostelCreate, HostelUpdate, HostelStatus,
    Room, RoomCreate, RoomUpdate, RoomStatus, RoomType,
    StudentHostel, StudentHostelCreate, StudentHostelUpdate, StudentHostelStatus,
    RoomAllocation, RoomAllocationCreate,
    HostelAttendance, HostelAttendanceCreate, CheckInStatus,
    HostelFee, HostelFeeCreate, HostelFeeUpdate, HostelFeeType,
    HostelFeeStructure, HostelFeeStructureCreate, HostelFeeStructureUpdate,
    HostelMaintenance, HostelMaintenanceCreate,
    RoomInventoryItem, RoomInventoryItemCreate, RoomInventoryItemUpdate,
    HostelVisitor, HostelVisitorCreate, HostelVisitorUpdate,
    HostelComplaint, HostelComplaintCreate
)
from models.student import Student, Parent, StudentParent
from models.user import User, UserRole
from models.facilities import FacilityContractor, FacilityWorkOrder
from database import get_session
from auth import get_current_user, require_roles

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/hostel", tags=["Hostel Management"])


async def _has_student_access(session: AsyncSession, current_user: User, student: Student) -> bool:
    """Ownership rules for viewing a specific student's hostel
    accommodation/fee data: admins and teachers see any student in their
    school; parents only their own children; students only themselves.
    Previously every student-specific read endpoint below only checked
    same-school, not that a PARENT/STUDENT caller actually owns/is related
    to the student in question. Same shape as
    routers/fees.py::_has_fee_access / routers/transport.py::_has_student_access,
    applied here since hostel.py never had an equivalent ownership check.
    Also enforces campus scoping for a campus-restricted SCHOOL_ADMIN/
    TEACHER — hostel.py never called assert_campus_access anywhere, so
    such an admin could act on students in every campus of the school,
    not just their assigned one."""
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


class UpdateComplaintRequest(SQLModel):
    status: str
    resolution_notes: Optional[str] = None



# ============================================================================
# HOSTEL ENDPOINTS
# ============================================================================

@router.get("/hostels", response_model=List[dict])
async def list_hostels(
    status: Optional[HostelStatus] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """List all hostels for the school"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    query = select(Hostel).where(Hostel.school_id == school_id)
    
    if status:
        query = query.where(Hostel.status == status)
    
    query = query.offset(skip).limit(limit)
    result = await session.execute(query)
    hostels = result.scalars().all()
    
    return [
        {
            **jsonable_encoder(h),
            "status": h.status.value
        }
        for h in hostels
    ]


@router.post("/hostels", response_model=dict)
async def create_hostel(
    hostel_data: HostelCreate,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Create a new hostel"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    # Check if hostel code already exists in this school -- hostel codes are
    # only unique per-school (see uq_hostels_school_hostel_code)
    result = await session.execute(
        select(Hostel).where(Hostel.hostel_code == hostel_data.hostel_code, Hostel.school_id == school_id)
    )
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Hostel with this code already exists")
    
    hostel = Hostel(**hostel_data.dict(), school_id=school_id)
    session.add(hostel)
    await session.commit()
    await session.refresh(hostel)
    
    return {**jsonable_encoder(hostel), "status": hostel.status.value}


@router.get("/hostels/{hostel_id}", response_model=dict)
async def get_hostel(
    hostel_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Get hostel details"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    result = await session.execute(
        select(Hostel).where(and_(Hostel.id == hostel_id, Hostel.school_id == school_id))
    )
    hostel = result.scalar_one_or_none()
    if not hostel:
        raise HTTPException(status_code=404, detail="Hostel not found")
    
    return {**jsonable_encoder(hostel), "status": hostel.status.value}


@router.put("/hostels/{hostel_id}", response_model=dict)
async def update_hostel(
    hostel_id: str,
    hostel_data: HostelUpdate,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Update hostel information"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    result = await session.execute(
        select(Hostel).where(and_(Hostel.id == hostel_id, Hostel.school_id == school_id))
    )
    hostel = result.scalar_one_or_none()
    if not hostel:
        raise HTTPException(status_code=404, detail="Hostel not found")
    
    update_data = hostel_data.dict(exclude_unset=True)
    for key, value in update_data.items():
        setattr(hostel, key, value)
    
    hostel.updated_at = datetime.utcnow()
    session.add(hostel)
    await session.commit()
    await session.refresh(hostel)
    
    return {**jsonable_encoder(hostel), "status": hostel.status.value}


HOSTEL_CASCADE_TABLES = [
    ("rooms", "rooms"),
    ("student_hostels", "student accommodations"),
    ("room_allocations", "room allocations"),
    ("hostel_attendance", "attendance records"),
    ("hostel_maintenance", "maintenance records"),
    ("hostel_visitors", "visitor logs"),
    ("hostel_complaints", "complaints"),
]

HOSTEL_PRESERVED_TABLES = [
    ("hostel_fees", "hostel fee records"),
    ("hostel_fee_structures", "hostel fee structures"),
]


@router.get("/hostels/{hostel_id}/delete-impact", response_model=dict)
async def get_hostel_delete_impact(
    hostel_id: str,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Preview what deleting this hostel would affect, before actually deleting it."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(Hostel).where(and_(Hostel.id == hostel_id, Hostel.school_id == school_id))
    )
    hostel = result.scalar_one_or_none()
    if not hostel:
        raise HTTPException(status_code=404, detail="Hostel not found")

    will_be_deleted = {}
    for table, label in HOSTEL_CASCADE_TABLES:
        count_result = await session.execute(
            text(f"SELECT count(*) FROM {table} WHERE hostel_id = :hid"),
            {"hid": hostel_id}
        )
        count = count_result.scalar()
        if count:
            will_be_deleted[label] = count

    will_be_preserved = {}
    for table, label in HOSTEL_PRESERVED_TABLES:
        count_result = await session.execute(
            text(f"SELECT count(*) FROM {table} WHERE hostel_id = :hid"),
            {"hid": hostel_id}
        )
        count = count_result.scalar()
        if count:
            will_be_preserved[label] = count

    return {
        "hostel_id": hostel_id,
        "will_be_deleted": will_be_deleted,
        "total_to_be_deleted": sum(will_be_deleted.values()),
        "will_be_preserved": will_be_preserved,
    }


@router.delete("/hostels/{hostel_id}", response_model=dict)
async def delete_hostel(
    hostel_id: str,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Delete a hostel.

    Occupancy/operational records (rooms, accommodations, allocations, attendance,
    maintenance, visitors, complaints) are permanently deleted along with it via
    DB-level CASCADE. Financial records (hostel fees, fee structures) are never
    deleted — preserved with their hostel reference cleared via DB-level SET NULL.
    Call GET .../delete-impact first to show the user what this will affect.
    """
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(Hostel).where(and_(Hostel.id == hostel_id, Hostel.school_id == school_id))
    )
    hostel = result.scalar_one_or_none()
    if not hostel:
        raise HTTPException(status_code=404, detail="Hostel not found")

    await session.delete(hostel)
    await session.commit()
    
    return {"message": "Hostel deleted successfully", "id": hostel_id}


# ============================================================================
# ROOM ENDPOINTS
# ============================================================================

@router.get("/rooms", response_model=List[dict])
async def list_rooms(
    hostel_id: Optional[str] = None,
    room_type: Optional[RoomType] = None,
    status: Optional[RoomStatus] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """List all rooms"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    query = select(Room).where(Room.school_id == school_id)
    
    if hostel_id:
        query = query.where(Room.hostel_id == hostel_id)
    if room_type:
        query = query.where(Room.room_type == room_type)
    if status:
        query = query.where(Room.status == status)
    
    query = query.offset(skip).limit(limit)
    result = await session.execute(query)
    rooms = result.scalars().all()
    
    return [
        {
            **jsonable_encoder(r),
            "room_type": r.room_type.value,
            "status": r.status.value
        }
        for r in rooms
    ]


@router.post("/rooms", response_model=dict)
async def create_room(
    room_data: RoomCreate,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Create a new room"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    # Verify hostel exists — school_id scoped, without which a room could
    # be attached to another school's hostel (with the room's own school_id
    # set to the caller's school, producing an inconsistent cross-tenant
    # Room).
    result = await session.execute(
        select(Hostel).where(Hostel.id == room_data.hostel_id, Hostel.school_id == school_id)
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Hostel not found")

    room = Room(**room_data.dict(), school_id=school_id)
    session.add(room)
    await session.commit()
    await session.refresh(room)
    
    return {
        **jsonable_encoder(room),
        "room_type": room.room_type.value,
        "status": room.status.value
    }


@router.get("/rooms/{room_id}", response_model=dict)
async def get_room(
    room_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Get room details"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    result = await session.execute(
        select(Room).where(and_(Room.id == room_id, Room.school_id == school_id))
    )
    room = result.scalar_one_or_none()
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    
    return {
        **jsonable_encoder(room),
        "room_type": room.room_type.value,
        "status": room.status.value
    }


@router.put("/rooms/{room_id}", response_model=dict)
async def update_room(
    room_id: str,
    room_data: RoomUpdate,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Update room information"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    result = await session.execute(
        select(Room).where(and_(Room.id == room_id, Room.school_id == school_id))
    )
    room = result.scalar_one_or_none()
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    
    update_data = room_data.dict(exclude_unset=True)
    for key, value in update_data.items():
        setattr(room, key, value)
    
    room.updated_at = datetime.utcnow()
    session.add(room)
    await session.commit()
    await session.refresh(room)
    
    return {
        **jsonable_encoder(room),
        "room_type": room.room_type.value,
        "status": room.status.value
    }


ROOM_CASCADE_TABLES = [
    ("room_allocations", "room allocations"),
]

ROOM_PRESERVED_TABLES = [
    ("student_hostels", "student accommodations (will lose this specific room assignment)"),
    ("hostel_maintenance", "maintenance records"),
    ("hostel_complaints", "complaints"),
]


@router.get("/rooms/{room_id}/delete-impact", response_model=dict)
async def get_room_delete_impact(
    room_id: str,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Preview what deleting this room would affect, before actually deleting it."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(Room).where(and_(Room.id == room_id, Room.school_id == school_id))
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Room not found")

    will_be_deleted = {}
    for table, label in ROOM_CASCADE_TABLES:
        count = (await session.execute(text(f"SELECT count(*) FROM {table} WHERE room_id = :rid"), {"rid": room_id})).scalar()
        if count:
            will_be_deleted[label] = count

    will_be_preserved = {}
    for table, label in ROOM_PRESERVED_TABLES:
        count = (await session.execute(text(f"SELECT count(*) FROM {table} WHERE room_id = :rid"), {"rid": room_id})).scalar()
        if count:
            will_be_preserved[label] = count

    return {
        "room_id": room_id,
        "will_be_deleted": will_be_deleted,
        "total_to_be_deleted": sum(will_be_deleted.values()),
        "will_be_preserved": will_be_preserved,
    }


@router.delete("/rooms/{room_id}", response_model=dict)
async def delete_room(
    room_id: str,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Delete a room.

    Room allocations tied to this room are permanently deleted via DB-level
    CASCADE. Student accommodations, maintenance records, and complaints
    referencing this room are never deleted — preserved with their room
    reference cleared via DB-level SET NULL.
    """
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(Room).where(and_(Room.id == room_id, Room.school_id == school_id))
    )
    room = result.scalar_one_or_none()
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")

    await session.delete(room)
    await session.commit()

    return {"message": "Room deleted successfully", "id": room_id}


# ============================================================================
# ROOM INVENTORY (condition tracking)
# ============================================================================

@router.post("/rooms/{room_id}/inventory", response_model=dict)
async def create_room_inventory_item(
    room_id: str,
    item_data: RoomInventoryItemCreate,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Add an inventory/condition item to a room"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(Room).where(and_(Room.id == room_id, Room.school_id == school_id))
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Room not found")

    item = RoomInventoryItem(**item_data.dict(), room_id=room_id, school_id=school_id)
    session.add(item)
    await session.commit()
    await session.refresh(item)

    return jsonable_encoder(item)


@router.get("/rooms/{room_id}/inventory", response_model=List[dict])
async def list_room_inventory(
    room_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """List inventory/condition items for a room"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    query = select(RoomInventoryItem).where(
        and_(
            RoomInventoryItem.room_id == room_id,
            RoomInventoryItem.school_id == school_id
        )
    ).order_by(RoomInventoryItem.item_name.asc())
    result = await session.execute(query)
    items = result.scalars().all()

    return [jsonable_encoder(i) for i in items]


@router.put("/inventory/{item_id}", response_model=dict)
async def update_room_inventory_item(
    item_id: str,
    update_data: RoomInventoryItemUpdate,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Update a room inventory/condition item"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(RoomInventoryItem).where(
            and_(RoomInventoryItem.id == item_id, RoomInventoryItem.school_id == school_id)
        )
    )
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Inventory item not found")

    update_data_dict = update_data.dict(exclude_unset=True)
    for key, value in update_data_dict.items():
        setattr(item, key, value)

    item.updated_at = datetime.utcnow()
    session.add(item)
    await session.commit()
    await session.refresh(item)

    return jsonable_encoder(item)


@router.delete("/inventory/{item_id}", response_model=dict)
async def delete_room_inventory_item(
    item_id: str,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Delete a room inventory/condition item"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(RoomInventoryItem).where(
            and_(RoomInventoryItem.id == item_id, RoomInventoryItem.school_id == school_id)
        )
    )
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Inventory item not found")

    await session.delete(item)
    await session.commit()

    return {"message": "Inventory item deleted successfully", "id": item_id}


# ============================================================================
# STUDENT ACCOMMODATION
# ============================================================================

async def _sync_room_occupancy(session: AsyncSession, room_id: str) -> None:
    """Room.current_occupancy is a persisted counter that every write path
    tried to maintain incrementally (+=1/-=1) across two independent,
    parallel occupancy mechanisms -- StudentHostel accommodation and
    RoomAllocation -- so a missed call site (e.g. RoomAllocation had no
    reachable deallocate endpoint until recently) desyncs it permanently.
    Recompute fresh from both sources instead, mirroring
    _sync_transport_occupancy in routers/transport.py."""
    room = await session.get(Room, room_id)
    if not room:
        return

    accommodation_count = await session.execute(
        select(func.count(StudentHostel.id)).where(
            StudentHostel.room_id == room_id,
            StudentHostel.status == StudentHostelStatus.ACTIVE,
        )
    )
    allocation_count = await session.execute(
        select(func.count(RoomAllocation.id)).where(
            RoomAllocation.room_id == room_id,
            RoomAllocation.deallocation_date.is_(None),
        )
    )
    room.current_occupancy = (accommodation_count.scalar() or 0) + (allocation_count.scalar() or 0)
    session.add(room)


@router.post("/accommodation", response_model=dict)
async def add_student_accommodation(
    accommodation_data: StudentHostelCreate,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Add student to hostel accommodation"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    # Verify student exists — school_id scoped, without which a school could
    # reference another school's student by guessing/reusing their roll
    # number (this business id is not unique across schools).
    result = await session.execute(
        select(Student).where(Student.student_id == accommodation_data.student_id, Student.school_id == school_id)
    )
    student = result.scalar_one_or_none()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    if not await _has_student_access(session, current_user, student):
        raise HTTPException(status_code=403, detail="Access denied")

    # Check if student already has accommodation
    result = await session.execute(
        select(StudentHostel).where(StudentHostel.student_id == accommodation_data.student_id)
    )
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Student already has hostel accommodation")

    # Enforce room capacity and keep occupancy accurate when a room is assigned up front.
    # school_id scoped, without which a school could allocate (and mutate the
    # occupancy of) another school's room by guessing/reusing its id. Locked
    # FOR UPDATE so two concurrent requests can't both pass the capacity
    # check before either commits and over-allocate the room.
    room = None
    if accommodation_data.room_id:
        room_result = await session.execute(
            select(Room).where(Room.id == accommodation_data.room_id, Room.school_id == school_id).with_for_update()
        )
        room = room_result.scalar_one_or_none()
        if not room:
            raise HTTPException(status_code=404, detail="Room not found")
        if room.current_occupancy >= room.capacity:
            raise HTTPException(status_code=400, detail="Room is at full capacity")

    accommodation = StudentHostel(**accommodation_data.dict(), school_id=school_id)
    session.add(accommodation)
    await session.commit()
    await session.refresh(accommodation)

    if room:
        await _sync_room_occupancy(session, room.id)
        await session.commit()

    return {**jsonable_encoder(accommodation), "status": accommodation.status.value}


@router.get("/accommodation", response_model=List[dict])
async def list_student_accommodations(
    hostel_id: Optional[str] = None,
    status: Optional[StudentHostelStatus] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """List student accommodations with optional filtering"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    query = select(StudentHostel).where(StudentHostel.school_id == school_id)
    
    if hostel_id:
        query = query.where(StudentHostel.hostel_id == hostel_id)
    if status:
        query = query.where(StudentHostel.status == status)
    
    query = query.offset(skip).limit(limit)
    result = await session.execute(query)
    accommodations = result.scalars().all()
    
    return [
        {
            **jsonable_encoder(acc),
            "status": acc.status.value
        }
        for acc in accommodations
    ]


@router.get("/accommodation/{student_id}", response_model=dict)
async def get_student_accommodation(
    student_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Get student's hostel accommodation details"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    # StudentHostel.student_id stores the roll number (Student.student_id),
    # not the UUID -- match that when resolving the Student for the
    # ownership check below.
    student_result = await session.execute(select(Student).where(Student.student_id == student_id, Student.school_id == school_id))
    student = student_result.scalar_one_or_none()
    if not student or not await _has_student_access(session, current_user, student):
        raise HTTPException(status_code=403, detail="Access denied")

    result = await session.execute(
        select(StudentHostel).where(
            and_(StudentHostel.student_id == student_id, StudentHostel.school_id == school_id)
        )
    )
    accommodation = result.scalar_one_or_none()
    if not accommodation:
        raise HTTPException(status_code=404, detail="Accommodation not found")
    
    return {**jsonable_encoder(accommodation), "status": accommodation.status.value}


@router.put("/accommodation/{accommodation_id}", response_model=dict)
async def update_student_accommodation(
    accommodation_id: str,
    accommodation_data: StudentHostelUpdate,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Update student's hostel accommodation"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    result = await session.execute(
        select(StudentHostel).where(
            and_(StudentHostel.id == accommodation_id, StudentHostel.school_id == school_id)
        )
    )
    accommodation = result.scalar_one_or_none()
    if not accommodation:
        raise HTTPException(status_code=404, detail="Accommodation not found")

    student_result = await session.execute(
        select(Student).where(Student.student_id == accommodation.student_id, Student.school_id == school_id)
    )
    student = student_result.scalar_one_or_none()
    if student and not await _has_student_access(session, current_user, student):
        raise HTTPException(status_code=403, detail="Access denied")

    old_room_id = accommodation.room_id
    old_was_active = accommodation.status == StudentHostelStatus.ACTIVE

    update_data = accommodation_data.dict(exclude_unset=True)
    for key, value in update_data.items():
        setattr(accommodation, key, value)

    accommodation.updated_at = datetime.utcnow()

    new_room_id = accommodation.room_id
    new_is_active = accommodation.status == StudentHostelStatus.ACTIVE

    # Enforce capacity on the new room before committing anything. school_id
    # scoped + locked FOR UPDATE for the same reasons as
    # add_student_accommodation above — new_room_id comes straight from the
    # client's request body.
    if new_room_id and new_room_id != old_room_id and new_is_active:
        new_room_result = await session.execute(
            select(Room).where(Room.id == new_room_id, Room.school_id == school_id).with_for_update()
        )
        new_room = new_room_result.scalar_one_or_none()
        if not new_room:
            raise HTTPException(status_code=404, detail="Room not found")
        if new_room.current_occupancy >= new_room.capacity:
            raise HTTPException(status_code=400, detail="Room is at full capacity")

    session.add(accommodation)
    await session.commit()
    await session.refresh(accommodation)

    # Resync both rooms' occupancy from source (old room freed if the
    # student moved rooms or is no longer an active resident; new room
    # occupied if a different room was assigned and the student is active).
    if old_room_id and old_was_active and (old_room_id != new_room_id or not new_is_active):
        await _sync_room_occupancy(session, old_room_id)
    if new_room_id and new_room_id != old_room_id and new_is_active:
        await _sync_room_occupancy(session, new_room_id)
    await session.commit()

    return {**jsonable_encoder(accommodation), "status": accommodation.status.value}


# ============================================================================
# ROOM ALLOCATION
# ============================================================================

@router.post("/allocation", response_model=dict)
async def allocate_room_to_student(
    allocation_data: RoomAllocationCreate,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Allocate a room to a student"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    # Verify student exists in this school — previously never checked at all.
    student_result = await session.execute(
        select(Student).where(Student.student_id == allocation_data.student_id, Student.school_id == school_id)
    )
    student = student_result.scalar_one_or_none()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    if not await _has_student_access(session, current_user, student):
        raise HTTPException(status_code=403, detail="Access denied")

    # A student can only hold one active (not yet deallocated) room
    # allocation at a time — previously never checked, so repeated calls
    # could allocate the same student multiple rooms simultaneously, each
    # only checked against the *target* room's own capacity.
    existing_result = await session.execute(
        select(RoomAllocation).where(
            RoomAllocation.student_id == allocation_data.student_id,
            RoomAllocation.school_id == school_id,
            RoomAllocation.deallocation_date.is_(None),
        )
    )
    if existing_result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="This student already has an active room allocation — deallocate it first")

    # Check room capacity — school_id scoped and locked FOR UPDATE, same
    # rationale as add_student_accommodation/update_student_accommodation.
    result = await session.execute(
        select(Room).where(Room.id == allocation_data.room_id, Room.school_id == school_id).with_for_update()
    )
    room = result.scalar_one_or_none()
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")

    if room.current_occupancy >= room.capacity:
        raise HTTPException(status_code=400, detail="Room is at full capacity")

    allocation = RoomAllocation(**allocation_data.dict(), school_id=school_id)
    session.add(allocation)
    await session.commit()
    await session.refresh(allocation)

    await _sync_room_occupancy(session, room.id)
    await session.commit()

    return jsonable_encoder(allocation)


@router.get("/allocation/student/{student_id}", response_model=List[dict])
async def get_student_room_allocations(
    student_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Get all room allocations for a student"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    # RoomAllocation.student_id stores the roll number (Student.student_id),
    # not the UUID -- match that when resolving the Student for the
    # ownership check below.
    student_result = await session.execute(select(Student).where(Student.student_id == student_id, Student.school_id == school_id))
    student = student_result.scalar_one_or_none()
    if not student or not await _has_student_access(session, current_user, student):
        raise HTTPException(status_code=403, detail="Access denied")

    result = await session.execute(
        select(RoomAllocation).where(
            and_(
                RoomAllocation.student_id == student_id,
                RoomAllocation.school_id == school_id
            )
        )
    )
    allocations = result.scalars().all()

    return [jsonable_encoder(a) for a in allocations]


@router.post("/allocation/{allocation_id}/deallocate", response_model=dict)
async def deallocate_room_from_student(
    allocation_id: str,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Free a student's room allocation — until this endpoint existed, a
    RoomAllocation made via POST /allocation had no reachable way to be
    released, so every allocation permanently consumed a slot in
    Room.current_occupancy with no way back through the API. (A correct but
    uncalled deallocate_room() existed in services/hostel_service.py at the
    time; that whole dead-code class was later removed, see
    project_campusio_hostel_service_dead_code_removal_fix.md.)"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(RoomAllocation).where(
            and_(RoomAllocation.id == allocation_id, RoomAllocation.school_id == school_id)
        )
    )
    allocation = result.scalar_one_or_none()
    if not allocation:
        raise HTTPException(status_code=404, detail="Allocation not found")
    if allocation.deallocation_date:
        raise HTTPException(status_code=400, detail="This allocation has already been deallocated")

    room_id = allocation.room_id
    allocation.deallocation_date = datetime.utcnow().strftime("%Y-%m-%d")
    session.add(allocation)
    await session.commit()
    await session.refresh(allocation)

    await _sync_room_occupancy(session, room_id)
    await session.commit()

    return jsonable_encoder(allocation)


# ============================================================================
# HOSTEL ATTENDANCE
# ============================================================================

@router.post("/attendance", response_model=dict)
async def mark_hostel_attendance(
    attendance_data: HostelAttendanceCreate,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Mark hostel check-in/check-out"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    attendance = HostelAttendance(**attendance_data.dict(), school_id=school_id)
    session.add(attendance)
    await session.commit()
    await session.refresh(attendance)
    
    return {**jsonable_encoder(attendance), "status": attendance.status.value}


@router.get("/attendance", response_model=List[dict])
async def get_all_attendance(
    hostel_id: Optional[str] = None,
    status: Optional[str] = None,
    attendance_date: Optional[str] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Get all attendance records, optionally filtered by hostel, status, or date"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    query = select(HostelAttendance).where(HostelAttendance.school_id == school_id)
    
    if hostel_id:
        query = query.where(HostelAttendance.hostel_id == hostel_id)
    if status:
        query = query.where(HostelAttendance.status == status)
    if attendance_date:
        query = query.where(HostelAttendance.attendance_date == attendance_date)
    
    query = query.order_by(HostelAttendance.attendance_date.desc()).offset(skip).limit(limit)
    result = await session.execute(query)
    records = result.scalars().all()
    
    return [{**jsonable_encoder(r), "status": r.status.value} for r in records]


@router.get("/attendance/{hostel_id}", response_model=List[dict])
async def get_hostel_attendance(
    hostel_id: str,
    attendance_date: Optional[str] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Get attendance records for a hostel"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    query = select(HostelAttendance).where(
        and_(
            HostelAttendance.hostel_id == hostel_id,
            HostelAttendance.school_id == school_id
        )
    )
    
    if attendance_date:
        query = query.where(HostelAttendance.attendance_date == attendance_date)
    
    query = query.offset(skip).limit(limit)
    result = await session.execute(query)
    records = result.scalars().all()
    
    return [{**jsonable_encoder(r), "status": r.status.value} for r in records]


@router.put("/attendance/{attendance_id}", response_model=dict)
async def update_hostel_attendance(
    attendance_id: str,
    update_data: HostelAttendanceCreate,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Update hostel attendance record"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    result = await session.execute(
        select(HostelAttendance).where(
            HostelAttendance.id == attendance_id,
            HostelAttendance.school_id == school_id
        )
    )
    attendance = result.scalar_one_or_none()
    
    if not attendance:
        raise HTTPException(status_code=404, detail="Attendance record not found")
    
    # Update fields
    update_data_dict = update_data.dict(exclude_unset=True)
    for key, value in update_data_dict.items():
        setattr(attendance, key, value)
    
    session.add(attendance)
    await session.commit()
    await session.refresh(attendance)
    
    return {**jsonable_encoder(attendance), "status": attendance.status.value}


# ============================================================================
# HOSTEL FEE STRUCTURES
# ============================================================================

@router.post("/fee-structures", response_model=dict)
async def create_fee_structure(
    structure_data: HostelFeeStructureCreate,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Create hostel fee structure for billing"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    # Verify hostel exists
    hostel_result = await session.execute(
        select(Hostel).where(and_(Hostel.id == structure_data.hostel_id, Hostel.school_id == school_id))
    )
    if not hostel_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Hostel not found")
    
    structure = HostelFeeStructure(**structure_data.dict(), school_id=school_id)
    session.add(structure)
    await session.commit()
    await session.refresh(structure)
    
    return {**jsonable_encoder(structure), "fee_type": structure.fee_type.value}


@router.get("/fee-structures", response_model=List[dict])
async def get_fee_structures(
    hostel_id: Optional[str] = None,
    academic_term_id: Optional[str] = None,
    is_active: Optional[bool] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Get hostel fee structures"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    query = select(HostelFeeStructure).where(HostelFeeStructure.school_id == school_id)
    
    if hostel_id:
        query = query.where(HostelFeeStructure.hostel_id == hostel_id)
    if academic_term_id:
        query = query.where(HostelFeeStructure.academic_term_id == academic_term_id)
    if is_active is not None:
        query = query.where(HostelFeeStructure.is_active == is_active)
    
    query = query.offset(skip).limit(limit)
    result = await session.execute(query)
    structures = result.scalars().all()
    
    return [
        {**jsonable_encoder(s), "fee_type": s.fee_type.value}
        for s in structures
    ]


@router.get("/fee-structures/{structure_id}", response_model=dict)
async def get_fee_structure(
    structure_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Get fee structure details"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    result = await session.execute(
        select(HostelFeeStructure).where(
            and_(HostelFeeStructure.id == structure_id, HostelFeeStructure.school_id == school_id)
        )
    )
    structure = result.scalar_one_or_none()
    if not structure:
        raise HTTPException(status_code=404, detail="Fee structure not found")
    
    return {**jsonable_encoder(structure), "fee_type": structure.fee_type.value}


@router.put("/fee-structures/{structure_id}", response_model=dict)
async def update_fee_structure(
    structure_id: str,
    structure_data: HostelFeeStructureUpdate,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Update hostel fee structure"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    result = await session.execute(
        select(HostelFeeStructure).where(
            and_(HostelFeeStructure.id == structure_id, HostelFeeStructure.school_id == school_id)
        )
    )
    structure = result.scalar_one_or_none()
    if not structure:
        raise HTTPException(status_code=404, detail="Fee structure not found")
    
    update_data = structure_data.dict(exclude_unset=True)
    for key, value in update_data.items():
        setattr(structure, key, value)
    
    structure.updated_at = datetime.utcnow()
    session.add(structure)
    await session.commit()
    await session.refresh(structure)
    
    return {**jsonable_encoder(structure), "fee_type": structure.fee_type.value}


@router.delete("/fee-structures/{structure_id}", response_model=dict)
async def delete_fee_structure(
    structure_id: str,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Delete fee structure"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    result = await session.execute(
        select(HostelFeeStructure).where(
            and_(HostelFeeStructure.id == structure_id, HostelFeeStructure.school_id == school_id)
        )
    )
    structure = result.scalar_one_or_none()
    if not structure:
        raise HTTPException(status_code=404, detail="Fee structure not found")
    
    await session.delete(structure)
    await session.commit()
    
    return {"message": "Fee structure deleted successfully", "id": structure_id}


# ============================================================================
# HOSTEL FEES
# ============================================================================

@router.post("/fees", response_model=dict)
async def create_hostel_fee(
    fee_data: HostelFeeCreate,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Create hostel accommodation fee"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    # Verify student exists in this school — previously never checked at
    # all, unlike routers/transport.py's equivalent create_transport_fee.
    student_result = await session.execute(
        select(Student).where(Student.id == fee_data.student_id, Student.school_id == school_id)
    )
    student = student_result.scalar_one_or_none()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    if not await _has_student_access(session, current_user, student):
        raise HTTPException(status_code=403, detail="Access denied")

    # If a fee_structure_id is given, it must be a real, same-school
    # structure, and amount_due is derived from its rate rather than
    # trusted from the client -- the same "derive from the real record"
    # fix already applied to routers/fees.py::create_student_fee and
    # routers/transport.py::create_transport_fee. fee_structure_id is
    # optional here (unlike a transport fee's mandatory route), so a fee
    # with none is a deliberately ad-hoc, one-off charge and its
    # caller-supplied amount_due is left as-is.
    amount_due = fee_data.amount_due
    if fee_data.fee_structure_id:
        structure_result = await session.execute(
            select(HostelFeeStructure).where(
                HostelFeeStructure.id == fee_data.fee_structure_id, HostelFeeStructure.school_id == school_id
            )
        )
        structure = structure_result.scalar_one_or_none()
        if not structure:
            raise HTTPException(status_code=404, detail="Fee structure not found")
        amount_due = structure.amount

    if fee_data.discount > amount_due:
        raise HTTPException(status_code=400, detail="discount cannot exceed the fee amount")

    # Double-submit guard: reject an identical fee (same student, hostel,
    # term, type and amount) created in the last 30 seconds, mirroring the
    # fee-payment guard in routers/fees.py::record_payment.
    dupe_cutoff = datetime.utcnow() - timedelta(seconds=30)
    dupe_result = await session.execute(
        select(HostelFee).where(
            and_(
                HostelFee.school_id == school_id,
                HostelFee.student_id == fee_data.student_id,
                HostelFee.hostel_id == fee_data.hostel_id,
                HostelFee.academic_term_id == fee_data.academic_term_id,
                HostelFee.fee_type == fee_data.fee_type,
                HostelFee.amount_due == amount_due,
                HostelFee.created_at >= dupe_cutoff,
            )
        )
    )
    if dupe_result.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="An identical hostel fee was just created — avoid double-submitting")

    fee_dict = fee_data.dict()
    fee_dict['amount_due'] = amount_due
    fee = HostelFee(**fee_dict, school_id=school_id)
    # Same formula as update_hostel_fee's auto-calc: a fee created fully
    # covered by its own discount (e.g. a scholarship waiver) must not sit
    # marked unpaid until some unrelated later update happens to touch it.
    fee.is_paid = (fee.amount_due - fee.amount_paid - fee.discount) <= 0
    session.add(fee)
    await session.commit()
    await session.refresh(fee)

    return {**jsonable_encoder(fee), "fee_type": fee.fee_type.value}


@router.get("/fees", response_model=List[dict])
async def get_hostel_fees(
    hostel_id: Optional[str] = None,
    is_paid: Optional[bool] = None,
    needs_gl_reconciliation: Optional[bool] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Get hostel fees, optionally filtered by hostel

    needs_gl_reconciliation=true surfaces fees that received a payment but
    have no gl_journal_entry_id -- GL posting is best-effort (see
    _post_hostel_fee_to_gl) and its failures were previously only logged,
    with no reconciliation job covering hostel fees at all, so a failed
    post was otherwise permanently invisible to admins.
    """
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    query = select(HostelFee).where(HostelFee.school_id == school_id)

    if hostel_id:
        query = query.where(HostelFee.hostel_id == hostel_id)
    if is_paid is not None:
        query = query.where(HostelFee.is_paid == is_paid)
    if needs_gl_reconciliation:
        query = query.where(HostelFee.amount_paid > 0, HostelFee.gl_journal_entry_id.is_(None))
    
    query = query.offset(skip).limit(limit)
    result = await session.execute(query)
    fees = result.scalars().all()
    
    return [
        {**jsonable_encoder(f), "fee_type": f.fee_type.value}
        for f in fees
    ]


@router.get("/fees/{student_id}", response_model=List[dict])
async def get_student_hostel_fees(
    student_id: str,
    is_paid: Optional[bool] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Get hostel fees for a student"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    # Unlike StudentHostel/RoomAllocation, HostelFee.student_id stores the
    # real Student UUID (confirmed against live data), matching Student.id.
    student_result = await session.execute(select(Student).where(Student.id == student_id, Student.school_id == school_id))
    student = student_result.scalar_one_or_none()
    if not student or not await _has_student_access(session, current_user, student):
        raise HTTPException(status_code=403, detail="Access denied")

    query = select(HostelFee).where(
        and_(
            HostelFee.student_id == student_id,
            HostelFee.school_id == school_id
        )
    )
    
    if is_paid is not None:
        query = query.where(HostelFee.is_paid == is_paid)
    
    query = query.offset(skip).limit(limit)
    result = await session.execute(query)
    fees = result.scalars().all()
    
    return [
        {**jsonable_encoder(f), "fee_type": f.fee_type.value}
        for f in fees
    ]


@router.put("/fees/{fee_id}", response_model=dict)
async def update_hostel_fee(
    fee_id: str,
    fee_data: HostelFeeUpdate,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Update hostel fee payment with GL auto-posting
    
    For partial payments:
    - amount_paid is cumulative total of all payments
    - outstanding_balance = amount_due - amount_paid - discount
    - is_paid = True when outstanding_balance <= 0
    """
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
        select(HostelFee).where(
            and_(HostelFee.id == fee_id, HostelFee.school_id == school_id)
        ).with_for_update()
    )
    fee = result.scalar_one_or_none()
    if not fee:
        raise HTTPException(status_code=404, detail="Fee not found")

    update_data = fee_data.dict(exclude_unset=True)
    old_amount_paid = fee.amount_paid
    
    # Handle partial payment logic
    if "amount_paid" in update_data:
        new_amount_paid = update_data.get("amount_paid")
        # Ensure amount_paid doesn't exceed amount_due
        if new_amount_paid > fee.amount_due:
            new_amount_paid = fee.amount_due
            update_data["amount_paid"] = new_amount_paid
    
    # Auto-calculate is_paid based on outstanding balance
    if "amount_paid" in update_data or "amount_due" in update_data or "discount" in update_data:
        amount_due = update_data.get("amount_due", fee.amount_due)
        amount_paid = update_data.get("amount_paid", fee.amount_paid)
        discount = update_data.get("discount", fee.discount)
        
        # Calculate outstanding balance
        outstanding_balance = amount_due - amount_paid - discount
        update_data["is_paid"] = outstanding_balance <= 0
    
    for key, value in update_data.items():
        setattr(fee, key, value)
    
    fee.updated_at = datetime.utcnow()
    session.add(fee)
    await session.commit()
    await session.refresh(fee)
    
    # Attempt GL posting if payment was recorded
    journal_entry_id = None
    if "amount_paid" in update_data and update_data["amount_paid"] > old_amount_paid:
        payment_amount = update_data["amount_paid"] - old_amount_paid
        try:
            journal_entry_id = await _post_hostel_fee_to_gl(
                session=session,
                school_id=school_id,
                fee_id=fee_id,
                hostel_id=fee.hostel_id,
                payment_amount=payment_amount,
                payment_method=update_data.get("payment_method", "cash"),
                created_by=current_user.id
            )
            if journal_entry_id:
                logger.info(f"Created journal entry {journal_entry_id} for hostel fee payment {fee_id}")
                fee.gl_journal_entry_id = journal_entry_id
                fee.gl_posted_date = datetime.utcnow().isoformat().split('T')[0]
                session.add(fee)
                await session.commit()
                await session.refresh(fee)
            else:
                logger.warning(f"GL posting returned None for hostel fee payment {fee_id} (GL accounts may not be configured)")
        except Exception as e:
            import traceback
            logger.error(f"Exception in GL posting for hostel fee {fee_id}: {type(e).__name__}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            # Continue anyway - payment is recorded even if GL posting fails
    
    response = {**jsonable_encoder(fee), "fee_type": fee.fee_type.value}
    
    # Calculate and include outstanding balance in response
    outstanding_balance = fee.amount_due - fee.amount_paid - fee.discount
    response["outstanding_balance"] = max(0, outstanding_balance)
    response["is_fully_paid"] = fee.is_paid
    
    if journal_entry_id:
        response["journal_entry_id"] = journal_entry_id
    
    return response


@router.get("/fees-summary", response_model=dict)
async def get_hostel_fees_summary(
    hostel_id: Optional[str] = None,
    academic_term_id: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Get hostel fees summary"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    query = select(HostelFee).where(HostelFee.school_id == school_id)
    
    if hostel_id:
        query = query.where(HostelFee.hostel_id == hostel_id)
    if academic_term_id:
        query = query.where(HostelFee.academic_term_id == academic_term_id)
    
    result = await session.execute(query)
    fees = result.scalars().all()
    
    total_due = sum(f.amount_due for f in fees)
    total_paid = sum(f.amount_paid for f in fees)
    total_discount = sum(f.discount for f in fees)
    total_outstanding = total_due - total_paid - total_discount
    
    return {
        "total_due": total_due,
        "total_paid": total_paid,
        "total_discount": total_discount,
        "total_outstanding": total_outstanding,
        "collection_rate": round((total_paid / total_due * 100) if total_due > 0 else 0, 2),
        "total_students": len(set(f.student_id for f in fees)),
        "fees_paid": sum(1 for f in fees if f.is_paid),
        "fees_unpaid": sum(1 for f in fees if not f.is_paid)
    }


# ============================================================================
# MAINTENANCE
# ============================================================================

async def _validate_facilities_links(session: AsyncSession, contractor_id: Optional[str], work_order_id: Optional[str], school_id: str) -> None:
    if contractor_id:
        result = await session.execute(select(FacilityContractor).where(FacilityContractor.id == contractor_id, FacilityContractor.school_id == school_id))
        if not result.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="Contractor not found in this school")
    if work_order_id:
        result = await session.execute(select(FacilityWorkOrder).where(FacilityWorkOrder.id == work_order_id, FacilityWorkOrder.school_id == school_id))
        if not result.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="Work order not found in this school")


@router.post("/maintenance", response_model=dict)
async def record_maintenance(
    maintenance_data: HostelMaintenanceCreate,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Record hostel maintenance"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    # hostel_id/room_id were previously never validated against school_id at
    # all — only contractor_id/work_order_id were checked via
    # _validate_facilities_links.
    hostel_result = await session.execute(select(Hostel).where(Hostel.id == maintenance_data.hostel_id, Hostel.school_id == school_id))
    if not hostel_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Hostel not found")
    if maintenance_data.room_id:
        room_result = await session.execute(select(Room).where(Room.id == maintenance_data.room_id, Room.school_id == school_id))
        if not room_result.scalar_one_or_none():
            raise HTTPException(status_code=404, detail="Room not found")

    await _validate_facilities_links(session, maintenance_data.contractor_id, maintenance_data.work_order_id, school_id)

    maintenance = HostelMaintenance(**maintenance_data.dict(), school_id=school_id)
    session.add(maintenance)
    await session.commit()
    await session.refresh(maintenance)
    
    return jsonable_encoder(maintenance)


@router.get("/maintenance", response_model=List[dict])
async def get_all_maintenance(
    hostel_id: Optional[str] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Get all maintenance records, optionally filtered by hostel"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    query = select(HostelMaintenance).where(HostelMaintenance.school_id == school_id)
    
    if hostel_id:
        query = query.where(HostelMaintenance.hostel_id == hostel_id)
    
    query = query.order_by(HostelMaintenance.maintenance_date.desc()).offset(skip).limit(limit)
    result = await session.execute(query)
    records = result.scalars().all()
    
    return [jsonable_encoder(r) for r in records]


@router.get("/maintenance/{hostel_id}", response_model=List[dict])
async def get_hostel_maintenance(
    hostel_id: str,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Get maintenance history for a hostel"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    query = select(HostelMaintenance).where(
        and_(
            HostelMaintenance.hostel_id == hostel_id,
            HostelMaintenance.school_id == school_id
        )
    ).order_by(HostelMaintenance.maintenance_date.desc())
    
    query = query.offset(skip).limit(limit)
    result = await session.execute(query)
    records = result.scalars().all()
    
    return [jsonable_encoder(r) for r in records]


# ============================================================================
# VISITORS
# ============================================================================

@router.post("/visitors", response_model=dict)
async def register_visitor(
    visitor_data: HostelVisitorCreate,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Register a hostel visitor"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    visitor = HostelVisitor(**visitor_data.dict(), school_id=school_id)
    session.add(visitor)
    await session.commit()
    await session.refresh(visitor)
    
    return jsonable_encoder(visitor)


@router.get("/visitors", response_model=List[dict])
async def get_all_visitors(
    hostel_id: Optional[str] = None,
    student_id: Optional[str] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Get all visitor records, optionally filtered by hostel or student"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    query = select(HostelVisitor).where(HostelVisitor.school_id == school_id)
    
    if hostel_id:
        query = query.where(HostelVisitor.hostel_id == hostel_id)
    if student_id:
        query = query.where(HostelVisitor.student_id == student_id)
    
    query = query.order_by(HostelVisitor.check_in_time.desc()).offset(skip).limit(limit)
    result = await session.execute(query)
    records = result.scalars().all()
    
    return [jsonable_encoder(r) for r in records]


@router.get("/visitors/{hostel_id}", response_model=List[dict])
async def get_hostel_visitors(
    hostel_id: str,
    student_id: Optional[str] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Get visitor records for a hostel"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    query = select(HostelVisitor).where(
        and_(
            HostelVisitor.hostel_id == hostel_id,
            HostelVisitor.school_id == school_id
        )
    )
    
    if student_id:
        query = query.where(HostelVisitor.student_id == student_id)
    
    query = query.offset(skip).limit(limit)
    result = await session.execute(query)
    records = result.scalars().all()

    return [jsonable_encoder(r) for r in records]


@router.put("/visitors/{visitor_id}", response_model=dict)
async def update_visitor(
    visitor_id: str,
    visitor_data: HostelVisitorUpdate,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Update a hostel visitor record — e.g. recording check-out time or
    correcting entry details, without the delete+recreate the frontend used
    to have to do in the absence of this endpoint."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(HostelVisitor).where(and_(HostelVisitor.id == visitor_id, HostelVisitor.school_id == school_id))
    )
    visitor = result.scalar_one_or_none()
    if not visitor:
        raise HTTPException(status_code=404, detail="Visitor record not found")

    for key, value in visitor_data.dict(exclude_unset=True).items():
        setattr(visitor, key, value)

    session.add(visitor)
    await session.commit()
    await session.refresh(visitor)
    return jsonable_encoder(visitor)


# ============================================================================
# COMPLAINTS
# ============================================================================

@router.post("/complaints", response_model=dict)
async def create_complaint(
    complaint_data: HostelComplaintCreate,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Create a hostel complaint"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    complaint = HostelComplaint(**complaint_data.dict(), school_id=school_id)
    session.add(complaint)
    await session.commit()
    await session.refresh(complaint)
    
    return jsonable_encoder(complaint)


@router.get("/complaints", response_model=List[dict])
async def get_all_complaints(
    hostel_id: Optional[str] = None,
    status: Optional[str] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Get all complaints, optionally filtered by hostel or status"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    query = select(HostelComplaint).where(HostelComplaint.school_id == school_id)
    
    if hostel_id:
        query = query.where(HostelComplaint.hostel_id == hostel_id)
    if status:
        query = query.where(HostelComplaint.status == status)
    
    query = query.offset(skip).limit(limit).order_by(HostelComplaint.created_at.desc())
    result = await session.execute(query)
    records = result.scalars().all()
    
    return [jsonable_encoder(r) for r in records]


@router.get("/complaints/{hostel_id}", response_model=List[dict])
async def get_hostel_complaints(
    hostel_id: str,
    status: Optional[str] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Get complaints for a hostel"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    query = select(HostelComplaint).where(
        and_(
            HostelComplaint.hostel_id == hostel_id,
            HostelComplaint.school_id == school_id
        )
    )
    
    if status:
        query = query.where(HostelComplaint.status == status)
    
    query = query.offset(skip).limit(limit)
    result = await session.execute(query)
    records = result.scalars().all()
    
    return [jsonable_encoder(r) for r in records]


@router.put("/complaints/{complaint_id}", response_model=dict)
async def update_complaint(
    complaint_id: str,
    body: UpdateComplaintRequest,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Update complaint status"""
    status = body.status
    resolution_notes = body.resolution_notes
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    result = await session.execute(
        select(HostelComplaint).where(
            and_(HostelComplaint.id == complaint_id, HostelComplaint.school_id == school_id)
        )
    )
    complaint = result.scalar_one_or_none()
    if not complaint:
        raise HTTPException(status_code=404, detail="Complaint not found")
    
    complaint.status = status
    if resolution_notes:
        complaint.resolution_notes = resolution_notes
    if status == "resolved":
        complaint.resolved_date = datetime.utcnow().isoformat().split('T')[0]
    
    complaint.updated_at = datetime.utcnow()
    session.add(complaint)
    await session.commit()
    await session.refresh(complaint)
    
    return jsonable_encoder(complaint)


@router.delete("/accommodation/{accommodation_id}", response_model=dict)
async def delete_accommodation(
    accommodation_id: str,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Delete student's hostel accommodation"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    result = await session.execute(
        select(StudentHostel).where(
            and_(StudentHostel.id == accommodation_id, StudentHostel.school_id == school_id)
        )
    )
    accommodation = result.scalar_one_or_none()
    if not accommodation:
        raise HTTPException(status_code=404, detail="Accommodation not found")

    # Free the room's bed before removing the accommodation record (only if
    # it was actively occupying one — inactive/graduated/transferred records
    # already had their bed freed when their status changed)
    room_id_to_sync = accommodation.room_id if accommodation.status == StudentHostelStatus.ACTIVE else None

    await session.delete(accommodation)
    await session.commit()

    if room_id_to_sync:
        await _sync_room_occupancy(session, room_id_to_sync)
        await session.commit()

    return {"message": "Accommodation deleted successfully", "id": accommodation_id}


@router.delete("/fees/{fee_id}", response_model=dict)
async def delete_fee(
    fee_id: str,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Delete hostel fee"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    result = await session.execute(
        select(HostelFee).where(
            and_(HostelFee.id == fee_id, HostelFee.school_id == school_id)
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
                reversal_reason=f"Hostel fee {fee_id} deleted",
            )
        except Exception as e:
            logger.error(f"Error reversing journal entry {fee.gl_journal_entry_id} for deleted hostel fee {fee_id}: {str(e)}")

    await session.delete(fee)
    await session.commit()

    return {"message": "Fee deleted successfully", "id": fee_id}


@router.put("/maintenance/{maintenance_id}", response_model=dict)
async def update_maintenance(
    maintenance_id: str,
    update_data: HostelMaintenanceCreate,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Update maintenance record"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    result = await session.execute(
        select(HostelMaintenance).where(
            and_(HostelMaintenance.id == maintenance_id, HostelMaintenance.school_id == school_id)
        )
    )
    maintenance = result.scalar_one_or_none()

    if not maintenance:
        raise HTTPException(status_code=404, detail="Maintenance record not found")
    await _validate_facilities_links(session, update_data.contractor_id, update_data.work_order_id, school_id)

    # Update fields
    update_data_dict = update_data.dict(exclude_unset=True)
    for key, value in update_data_dict.items():
        setattr(maintenance, key, value)
    
    session.add(maintenance)
    await session.commit()
    await session.refresh(maintenance)
    
    return {**jsonable_encoder(maintenance), "status": getattr(maintenance.status, 'value', maintenance.status) if hasattr(maintenance, 'status') else 'N/A'}


@router.delete("/maintenance/{maintenance_id}", response_model=dict)
async def delete_maintenance(
    maintenance_id: str,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Delete maintenance record"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    result = await session.execute(
        select(HostelMaintenance).where(
            and_(HostelMaintenance.id == maintenance_id, HostelMaintenance.school_id == school_id)
        )
    )
    maintenance = result.scalar_one_or_none()
    if not maintenance:
        raise HTTPException(status_code=404, detail="Maintenance record not found")
    
    await session.delete(maintenance)
    await session.commit()
    
    return {"message": "Maintenance record deleted successfully", "id": maintenance_id}


@router.delete("/visitors/{visitor_id}", response_model=dict)
async def delete_visitor(
    visitor_id: str,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Delete visitor record"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    result = await session.execute(
        select(HostelVisitor).where(
            and_(HostelVisitor.id == visitor_id, HostelVisitor.school_id == school_id)
        )
    )
    visitor = result.scalar_one_or_none()
    if not visitor:
        raise HTTPException(status_code=404, detail="Visitor record not found")
    
    await session.delete(visitor)
    await session.commit()
    
    return {"message": "Visitor record deleted successfully", "id": visitor_id}


@router.delete("/complaints/{complaint_id}", response_model=dict)
async def delete_complaint(
    complaint_id: str,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Delete complaint record"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    result = await session.execute(
        select(HostelComplaint).where(
            and_(HostelComplaint.id == complaint_id, HostelComplaint.school_id == school_id)
        )
    )
    complaint = result.scalar_one_or_none()
    if not complaint:
        raise HTTPException(status_code=404, detail="Complaint not found")
    
    await session.delete(complaint)
    await session.commit()
    
    return {"message": "Complaint deleted successfully", "id": complaint_id}


@router.delete("/attendance/{attendance_id}", response_model=dict)
async def delete_attendance(
    attendance_id: str,
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)),
    session: AsyncSession = Depends(get_session)
):
    """Delete attendance record"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    result = await session.execute(
        select(HostelAttendance).where(
            and_(HostelAttendance.id == attendance_id, HostelAttendance.school_id == school_id)
        )
    )
    attendance = result.scalar_one_or_none()
    if not attendance:
        raise HTTPException(status_code=404, detail="Attendance record not found")
    
    await session.delete(attendance)
    await session.commit()
    
    return {"message": "Attendance record deleted successfully", "id": attendance_id}


# ============================================================================
# GL AUTO-POSTING HELPER
# ============================================================================

async def _post_hostel_fee_to_gl(
    session: AsyncSession,
    school_id: str,
    fee_id: str,
    hostel_id: str,
    payment_amount: float,
    payment_method: str,
    created_by: str
) -> Optional[str]:
    """
    Create GL journal entries for hostel fee payment.
    
    Posts:
    - Dr. Cash/Bank GL account (based on payment method)
    - Cr. Hostel Fee Revenue GL account
    
    Args:
        session: Database session
        school_id: School ID for multi-tenancy
        fee_id: Fee ID for reference
        hostel_id: Hostel ID
        payment_amount: Amount paid
        payment_method: Payment method (cash, bank_transfer, mobile_money, cheque)
    
    Returns:
        Journal entry ID if successful, None if GL posting skipped/failed
    """
    try:
        from services.journal_entry_service import JournalEntryService
        from models.finance import JournalEntryCreate, JournalLineItemCreate, ReferenceType
        from models.finance.chart_of_accounts import GLAccount
        
        # GL Account mappings
        GL_BANK_ACCOUNTS = {
            "cash": "1001",  # Cash in Hand
            "bank_transfer": "1010",  # Bank Account
            "mobile_money": "1015",  # Mobile Money Account
            "cheque": "1010",  # Bank Account
        }
        GL_HOSTEL_REVENUE = "4150"  # Hostel Fee Revenue
        
        bank_account_code = GL_BANK_ACCOUNTS.get(payment_method, "1001")
        
        # Get GL accounts
        bank_result = await session.execute(
            select(GLAccount).where(
                and_(
                    GLAccount.account_code == bank_account_code,
                    GLAccount.school_id == school_id
                )
            )
        )
        bank_account = bank_result.scalar_one_or_none()
        
        revenue_result = await session.execute(
            select(GLAccount).where(
                and_(
                    GLAccount.account_code == GL_HOSTEL_REVENUE,
                    GLAccount.school_id == school_id
                )
            )
        )
        revenue_account = revenue_result.scalar_one_or_none()
        
        if not bank_account or not revenue_account:
            missing = []
            if not bank_account:
                missing.append(f"Bank account {bank_account_code}")
            if not revenue_account:
                missing.append(f"Revenue account {GL_HOSTEL_REVENUE}")
            logger.warning(
                f"GL accounts not found for hostel fee posting (school_id={school_id}). "
                f"Missing: {', '.join(missing)}. Please create these GL accounts first."
            )
            return None
        
        # Create journal entry
        journal_entry = JournalEntryCreate(
            entry_date=datetime.utcnow().isoformat().split('T')[0],
            description=f"Hostel accommodation fee payment for fee {fee_id}",
            remarks=f"Payment method: {payment_method}",
            reference_type=ReferenceType.FEE_PAYMENT,
            reference_id=fee_id,
            line_items=[
                JournalLineItemCreate(
                    gl_account_id=bank_account.id,
                    debit_amount=payment_amount,
                    credit_amount=0.0,
                    description=f"Cash received - Hostel fees ({payment_method})"
                ),
                JournalLineItemCreate(
                    gl_account_id=revenue_account.id,
                    debit_amount=0.0,
                    credit_amount=payment_amount,
                    description="Hostel accommodation fee revenue"
                )
            ]
        )
        
        service = JournalEntryService(session)
        entry = await service.create_entry(school_id, journal_entry, created_by)
        if not entry:
            return None

        # create_entry only ever leaves a new entry in DRAFT status -- GL
        # account balances aren't touched, and the entry stays invisible to
        # the trial balance/any POSTED-only report until this runs. Without
        # it, every hostel fee payment's journal entry was permanently
        # stuck in DRAFT (unlike routers/transport.py's equivalent, which
        # already calls post_entry after create_entry).
        posted = await service.post_entry(
            school_id=school_id, entry_id=entry.id, posted_by=created_by,
            approval_notes="Auto-posted from hostel fee payment",
        )

        return posted.id if posted else entry.id
        
    except Exception as e:
        import traceback
        logger.error(f"Error posting hostel fee to GL: {type(e).__name__}: {str(e)}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        return None
