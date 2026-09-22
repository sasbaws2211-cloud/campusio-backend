"""Seed security module data for School ERP System

Creates:
- security officer user
- student security profile
- daily QR tokens (parent_pickup, transport_dispatch, collector_share)
- sample security scan log and arrival events
- a parent note

Idempotent: re-runnable without duplicating unique records.
"""
import asyncio
import sys
from pathlib import Path
from datetime import datetime, timedelta

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent))

from sqlmodel import select
from database import async_engine, async_session, init_db
from models.user import User, UserRole
from models.otp import OTPSettings
from models.school import School
from models.student import Student
from models.security import (
    StudentSecurityProfile,
    DailyQRToken,
    SecurityScanLog,
    ArrivalEvent,
    ParentNote,
    QRTokenType,
    ScanResult,
    ArrivalEventType,
)
from auth import get_password_hash


essential_password = "secOfficer123"


async def seed_security():
    await init_db()

    async with async_session() as session:
        # Use the same school and admin from seed_hostel_data.py
        result = await session.exec(select(User).where(User.email == "admin@school.edu.gh"))
        school_admin = result.first()
        if not school_admin:
            print("No school admin found. Run seed_data.py first.")
            return

        result = await session.exec(select(School).where(School.id == school_admin.school_id))
        school = result.first()
        if not school:
            print("School for admin not found.")
            return

        result = await session.exec(select(Student).where(Student.school_id == school.id).limit(1))
        student = result.first()
        if not student:
            print("No student found for the school. Run seed_data.py first.")
            return

        print(f"Seeding security data for school: {school.name} / student: {student.first_name} {student.last_name}")

        # Create or reuse a security officer for the same school
        sec_email = "security@school.edu.gh"
        result = await session.exec(select(User).where(User.email == sec_email))
        security_user = result.first()

        if not security_user:
            security_user = User(
                email=sec_email,
                password_hash=get_password_hash(essential_password),
                first_name="Security",
                last_name="Officer",
                phone="+233 20 999 9999",
                role=UserRole.SECURITY_OFFICER,
                school_id=school.id,
            )
            session.add(security_user)
            await session.flush()

            # optional OTP settings for security user
            sec_otp = OTPSettings(user_id=security_user.id, is_enabled=False, is_mandatory=False, method="sms")
            session.add(sec_otp)

        # Create or reuse student security profile
        result = await session.exec(select(StudentSecurityProfile).where(StudentSecurityProfile.student_id == student.id))
        profile = result.first()
        if not profile:
            profile = StudentSecurityProfile(
                student_id=student.id,
                school_id=school.id,
                ghana_post_address="",
                pickup_method="parent",
                authorized_pickup_name=student.guardian_name if hasattr(student, 'guardian_name') else None,
                authorized_pickup_phone=student.guardian_phone if hasattr(student, 'guardian_phone') else None,
                authorized_pickup_relationship="guardian",
            )
            session.add(profile)
            await session.flush()

        today = datetime.utcnow().date().isoformat()
        expires = datetime.utcnow() + timedelta(hours=24)

        # Create or reuse Daily QR tokens
        # Parent pickup token
        parent_token_str = f"PARENT-{student.id}-{today}"
        result = await session.exec(select(DailyQRToken).where(DailyQRToken.token == parent_token_str))
        parent_token = result.first()
        if not parent_token:
            parent_token = DailyQRToken(
                token=parent_token_str,
                token_type=QRTokenType.PARENT_PICKUP,
                school_id=school.id,
                student_id=student.id,
                issued_date=today,
                expires_at=expires,
            )
            session.add(parent_token)

        # Transport dispatch token
        dispatch_token_str = f"DISPATCH-{student.id}-{today}"
        result = await session.exec(select(DailyQRToken).where(DailyQRToken.token == dispatch_token_str))
        dispatch_token = result.first()
        if not dispatch_token:
            dispatch_token = DailyQRToken(
                token=dispatch_token_str,
                token_type=QRTokenType.TRANSPORT_DISPATCH,
                school_id=school.id,
                student_id=student.id,
                issued_date=today,
                expires_at=expires,
            )
            session.add(dispatch_token)

        # Collector share token (example)
        coll_token_str = f"COLLECTOR-{student.id}-{today}"
        result = await session.exec(select(DailyQRToken).where(DailyQRToken.token == coll_token_str))
        coll_token = result.first()
        if not coll_token:
            coll_token = DailyQRToken(
                token=coll_token_str,
                token_type=QRTokenType.COLLECTOR_SHARE,
                school_id=school.id,
                student_id=student.id,
                issued_date=today,
                expires_at=expires,
            )
            session.add(coll_token)

        await session.flush()

        # Create a sample security scan log (authorized)
        sample_scan_text = f"seed-scan-{student.id}-{today}"
        result = await session.exec(select(SecurityScanLog).where(SecurityScanLog.token_scanned == parent_token_str))
        existing_scan = result.first()
        if not existing_scan:
            scan = SecurityScanLog(
                school_id=school.id,
                token_scanned=parent_token_str,
                scanned_by_id=security_user.id,
                result=ScanResult.AUTHORIZED,
                student_id=student.id,
            )
            session.add(scan)

        # Create arrival events (dispatched -> parent_confirmed)
        result = await session.exec(select(ArrivalEvent).where(ArrivalEvent.student_id == student.id))
        existing_event = result.first()
        if not existing_event:
            evt1 = ArrivalEvent(
                school_id=school.id,
                student_id=student.id,
                event_type=ArrivalEventType.DISPATCHED,
                triggered_by_id=security_user.id,
                notes="Seeded: dispatched by transport",
            )
            session.add(evt1)

            evt2 = ArrivalEvent(
                school_id=school.id,
                student_id=student.id,
                event_type=ArrivalEventType.PARENT_CONFIRMED,
                triggered_by_id=security_user.id,
                notes="Seeded: parent confirmed pickup",
            )
            session.add(evt2)

        # Create a parent note example
        parent_email = getattr(student, 'guardian_email', None) or 'parent@school.edu.gh'
        result = await session.exec(select(User).where(User.email == parent_email))
        parent_user = result.first()
        if not parent_user:
            # fallback: find any parent user in the school
            result = await session.exec(select(User).where((User.school_id == school.id) & (User.role == UserRole.PARENT)))
            parent_user = result.first()

        if parent_user:
            result = await session.exec(select(ParentNote).where(ParentNote.student_id == student.id))
            note = result.first()
            if not note:
                note = ParentNote(
                    school_id=school.id,
                    student_id=student.id,
                    author_id=parent_user.id,
                    text="Seed note: Parent will pick up at gate.",
                    is_confirmation=True,
                )
                session.add(note)

        await session.commit()

        print("\n✓ Security seed completed")


if __name__ == "__main__":
    asyncio.run(seed_security())
