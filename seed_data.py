"""Seed initial data for School ERP System"""
import asyncio
import sys
from pathlib import Path
from datetime import date, datetime

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent))

from sqlmodel import select
from database import async_engine, async_session, init_db
from models.user import User, UserRole
from models.otp import OTPSettings
from models.school import School
from models.classroom import Class as Classroom
from models.student import Student
from models.staff import Staff
from auth import get_password_hash


async def seed_data():
    """Seed initial data"""
    # Initialize database tables
    await init_db()
    
    async with async_session() as session:
        # Check if data already exists
        result = await session.execute(select(User).where(User.email == "admin@school.edu.gh"))
        if result.scalar_one_or_none():
            print("Seed data already exists. Skipping...")
            return
        
        # Create a test school
        school = School(
            name="Test Primary School",
            code="TPS001",
            address="123 Education Street",
            city="Accra",
            phone="+233 20 123 4567",
            email="info@testprimaryschool.edu.gh",
            school_type="basic",
            region="Greater Accra"
        )
        session.add(school)
        await session.flush()
        
        print(f"Created school: {school.name} (ID: {school.id})")
        
        # Create super admin user
        super_admin = User(
            email="superadmin@schoolerp.com",
            password_hash=get_password_hash("admin123"),
            first_name="Super",
            last_name="Admin",
            phone="+233 20 000 0000",
            role=UserRole.SUPER_ADMIN,
            school_id=None
        )
        session.add(super_admin)
        await session.flush()
        
        # Create OTP settings for super admin (mandatory OTP)
        super_admin_otp = OTPSettings(
            user_id=super_admin.id,
            is_enabled=False,
            is_mandatory=False,
            method="sms"
        )
        session.add(super_admin_otp)
        
        # Create school admin user
        school_admin = User(
            email="admin@school.edu.gh",
            password_hash=get_password_hash("admin123"),
            first_name="School",
            last_name="Admin",
            phone="+233 20 111 1111",
            role=UserRole.SCHOOL_ADMIN,
            school_id=school.id
        )
        session.add(school_admin)
        await session.flush()
        
        # Create OTP settings for school admin (mandatory OTP)
        school_admin_otp = OTPSettings(
            user_id=school_admin.id,
            is_enabled=False,
            is_mandatory=False,
            method="sms"
        )
        session.add(school_admin_otp)
        
        # Create teacher user
        teacher = User(
            email="teacher@school.edu.gh",
            password_hash=get_password_hash("teacher123"),
            first_name="Kwame",
            last_name="Asante",
            phone="+233 20 222 2222",
            role=UserRole.TEACHER,
            school_id=school.id
        )
        session.add(teacher)
        await session.flush()
        
        # Create OTP settings for teacher (optional OTP, disabled by default)
        teacher_otp = OTPSettings(
            user_id=teacher.id,
            is_enabled=False,
            is_mandatory=False,
            method="sms"
        )
        session.add(teacher_otp)
        
        # Create some classes
        classes_data = [
            {"name": "Primary 1A", "level": "primary_1", "section": "A", "capacity": 35, "room_number": "101"},
            {"name": "Primary 1B", "level": "primary_1", "section": "B", "capacity": 35, "room_number": "102"},
            {"name": "Primary 2A", "level": "primary_2", "section": "A", "capacity": 35, "room_number": "201"},
            {"name": "JHS 1A", "level": "jhs_1", "section": "A", "capacity": 40, "room_number": "301"},
        ]
        
        for class_data in classes_data:
            classroom = Classroom(
                school_id=school.id,
                **class_data
            )
            session.add(classroom)
        
        await session.flush()
        
        # Get the first class for student assignment
        class_result = await session.execute(select(Classroom).where(Classroom.school_id == school.id))
        first_class = class_result.scalars().first()
        
        # Create student user
        student_user = User(
            email="student@school.edu.gh",
            password_hash=get_password_hash("student123"),
            first_name="Kofi",
            last_name="Mensah",
            phone="+233 20 444 4444",
            role=UserRole.STUDENT,
            school_id=school.id
        )
        session.add(student_user)
        await session.flush()
        
        # Create OTP settings for student (optional OTP, disabled by default)
        student_otp = OTPSettings(
            user_id=student_user.id,
            is_enabled=False,
            is_mandatory=False,
            method="sms"
        )
        session.add(student_otp)
        
        # Create student record linked to user
        student = Student(
            school_id=school.id,
            student_id="TPS2024001",
            first_name="Kofi",
            last_name="Mensah",
            date_of_birth="2012-05-15",
            gender="male",
            class_id=first_class.id if first_class else None,
            admission_date="2024-01-08",
            guardian_name="Ama Mensah",
            guardian_phone="+233 20 555 5555",
            guardian_email="parent@school.edu.gh",
            user_id=student_user.id,
            status="active"
        )
        session.add(student)
        
        # Create parent user
        parent_user = User(
            email="parent@school.edu.gh",
            password_hash=get_password_hash("parent123"),
            first_name="Ama",
            last_name="Mensah",
            phone="+233 20 555 5555",
            role=UserRole.PARENT,
            school_id=school.id
        )
        session.add(parent_user)
        await session.flush()
        
        # Create OTP settings for parent (optional OTP, disabled by default)
        parent_otp = OTPSettings(
            user_id=parent_user.id,
            is_enabled=False,
            is_mandatory=False,
            method="sms"
        )
        session.add(parent_otp)
        
        await session.flush()
        
        await session.commit()
        
        print("Seed data created successfully!")
        print("\n=== Database Summary ===")
        print("✓ 1 School created")
        print("✓ 4 Classes created")
        print("✓ 5 User accounts created (Super Admin, Admin, Teacher, Student, Parent)")
        print("\nNote: Run seed_transport.py to seed transport data separately")
        print("\n=== Test Credentials ===")
        print("Super Admin:")
        print("  Email: superadmin@schoolerp.com")
        print("  Password: admin123")
        print("\nSchool Admin:")
        print("  Email: admin@school.edu.gh")
        print("  Password: admin123")
        print("\nTeacher:")
        print("  Email: teacher@school.edu.gh")
        print("  Password: teacher123")
        print("\nStudent:")
        print("  Email: student@school.edu.gh")
        print("  Password: student123")
        print("\nParent:")
        print("  Email: parent@school.edu.gh")
        print("  Password: parent123")


if __name__ == "__main__":
    asyncio.run(seed_data())
