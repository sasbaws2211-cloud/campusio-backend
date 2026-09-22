"""Hostel Management Service - Business logic for hostel operations

Only get_hostel_occupancy_report is live (called from
routers/operational_insights.py) -- every other method this class used to
have (create_hostel, allocate_room, allocate_student, create_hostel_fee,
mark_attendance, etc.) was dead code: routers/hostel.py never routed through
this service, instead duplicating (and diverging from) the same logic
inline, so nothing in the codebase ever called them. Deleted rather than
fixed/kept, per a 2026-09-08 audit decision -- see
project_campusio_hostel_service_dead_code_removal_fix.md.
"""
import logging
from typing import Optional, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from models.hostel import Hostel, Room

logger = logging.getLogger(__name__)


class HostelServiceError(Exception):
    """Base exception for Hostel service errors"""
    pass


class HostelService:
    """Service for hostel reporting. See module docstring: this used to be a
    full CRUD service, but every method except get_hostel_occupancy_report
    was unreachable dead code and was removed."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_hostel_occupancy_report(
        self,
        school_id: str,
        hostel_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Generate comprehensive occupancy report"""
        query_hostels = select(Hostel).where(Hostel.school_id == school_id)
        if hostel_id:
            query_hostels = query_hostels.where(Hostel.id == hostel_id)

        result = await self.session.execute(query_hostels)
        hostels = result.scalars().all()

        report = {
            "total_hostels": len(hostels),
            "total_capacity": 0,
            "total_occupied": 0,
            "hostels": []
        }

        for hostel in hostels:
            # Get rooms in hostel
            room_result = await self.session.execute(
                select(Room).where(Room.hostel_id == hostel.id)
            )
            rooms = room_result.scalars().all()

            total_capacity = sum(r.capacity for r in rooms)
            total_occupied = sum(r.current_occupancy for r in rooms)
            occupancy_rate = (total_occupied / total_capacity * 100) if total_capacity > 0 else 0

            report["total_capacity"] += total_capacity
            report["total_occupied"] += total_occupied

            report["hostels"].append({
                "hostel_id": hostel.id,
                "hostel_name": hostel.hostel_name,
                "hostel_type": hostel.hostel_type,
                "total_rooms": len(rooms),
                "capacity": total_capacity,
                "occupied": total_occupied,
                "available": total_capacity - total_occupied,
                "occupancy_rate": round(occupancy_rate, 2),
                "status": hostel.status.value
            })

        # Add overall occupancy rate
        overall_rate = (report["total_occupied"] / report["total_capacity"] * 100) \
            if report["total_capacity"] > 0 else 0
        report["overall_occupancy_rate"] = round(overall_rate, 2)

        return report
