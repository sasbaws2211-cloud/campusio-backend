"""Seed virtual extra class data for the School ERP System.

Creates:
- a published virtual extra class
- online session schedule
- assignments for the extra class
- a student enrollment and approved billing cycle
- a payment record and graded submission

This seeder is idempotent and re-runnable.
"""
import asyncio
import sys
from pathlib import Path
from datetime import datetime, timedelta

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent))

from sqlmodel import select
from database import async_session, init_db
from models.user import User, UserRole
from models.school import School
from models.staff import Staff, StaffType, StaffStatus
from models.student import Student, StudentParent, Parent
from models.classroom import Class as Classroom, ClassLevel, Subject
from models.extra_class import (
    ExtraClass, ExtraClassSession, ExtraClassAssignment,
    ExtraClassEnrollment, ExtraClassBillingCycle, ExtraClassPayment,
    ExtraClassSubmission, ExtraClassGrade,
    ExtraClassStatus, EnrollmentStatus, BillingInterval, SubmissionStatus
)
from auth import get_password_hash

EXTRA_CLASS_TITLE = "Virtual Extra Class - Computer Science Bootcamp"
TEACHER_EMAIL = "teacher@school.edu.gh"
STUDENT_EMAIL = "student@school.edu.gh"
PARENT_EMAIL = "parent@school.edu.gh"


async def get_school_and_admin(session):
    result = await session.exec(select(User).where(User.email == "admin@school.edu.gh"))
    admin = result.first()
    if not admin:
        return None, None

    result = await session.exec(select(School).where(School.id == admin.school_id))
    school = result.first()
    return school, admin


async def get_or_create_teacher(session, school):
    result = await session.exec(select(User).where(User.email == TEACHER_EMAIL))
    teacher_user = result.first()
    if not teacher_user:
        teacher_user = User(
            email=TEACHER_EMAIL,
            password_hash=get_password_hash("teacher123"),
            first_name="Kofi",
            last_name="Mensah",
            phone="+233 20 222 2222",
            role=UserRole.TEACHER,
            school_id=school.id,
        )
        session.add(teacher_user)
        await session.flush()

    result = await session.exec(select(Staff).where(Staff.user_id == teacher_user.id))
    staff = result.first()
    if not staff:
        staff = Staff(
            school_id=school.id,
            staff_id="STF3000",
            first_name=teacher_user.first_name,
            last_name=teacher_user.last_name,
            email=teacher_user.email,
            phone=teacher_user.phone or "+233 20 222 2222",
            date_of_birth="1990-01-01",
            gender="male",
            staff_type=StaffType.TEACHING,
            position="Virtual Class Teacher",
            department="Academic",
            qualification="B.Ed. (ICT)",
            date_joined="2024-01-01",
            status=StaffStatus.ACTIVE,
            user_id=teacher_user.id,
        )
        session.add(staff)
        await session.flush()

    return teacher_user, staff


async def get_or_create_class(session, school):
    result = await session.exec(select(Classroom).where(Classroom.school_id == school.id))
    classroom = result.first()
    if classroom:
        return classroom

    classroom = Classroom(
        school_id=school.id,
        name="Virtual Extra Class Group",
        level=ClassLevel.PRIMARY_6,
        section="V",
        capacity=25,
        room_number="V-01",
    )
    session.add(classroom)
    await session.flush()
    return classroom


async def get_or_create_subject(session, school):
    result = await session.exec(
        select(Subject).where(
            (Subject.school_id == school.id) &
            (Subject.name == "Computer Science")
        )
    )
    subject = result.first()
    if subject:
        return subject

    result = await session.exec(select(Subject).where(Subject.school_id == school.id))
    subject = result.first()
    if subject:
        return subject

    subject = Subject(
        school_id=school.id,
        name="Computer Science",
        code="COM101",
        category="core",
        credit_hours=3,
    )
    session.add(subject)
    await session.flush()
    return subject


async def get_or_create_student_and_parent(session, school, classroom):
    result = await session.exec(select(User).where(User.email == STUDENT_EMAIL))
    student_user = result.first()
    if not student_user:
        student_user = User(
            email=STUDENT_EMAIL,
            password_hash=get_password_hash("student123"),
            first_name="Esi",
            last_name="Boateng",
            phone="+233 20 444 4444",
            role=UserRole.STUDENT,
            school_id=school.id,
        )
        session.add(student_user)
        await session.flush()

    result = await session.exec(select(Student).where(Student.user_id == student_user.id))
    student = result.first()
    if not student:
        student = Student(
            school_id=school.id,
            student_id="EXTRA001",
            first_name=student_user.first_name,
            last_name=student_user.last_name,
            date_of_birth="2011-04-01",
            gender="female",
            admission_date=datetime.utcnow().strftime("%Y-%m-%d"),
            class_id=classroom.id,
            address="Virtual Campus",
            nationality="Ghanaian",
            status="active",
            user_id=student_user.id,
        )
        session.add(student)
        await session.flush()

    result = await session.exec(select(User).where(User.email == PARENT_EMAIL))
    parent_user = result.first()
    if not parent_user:
        parent_user = User(
            email=PARENT_EMAIL,
            password_hash=get_password_hash("parent123"),
            first_name="Ama",
            last_name="Boateng",
            phone="+233 20 555 5555",
            role=UserRole.PARENT,
            school_id=school.id,
        )
        session.add(parent_user)
        await session.flush()

    result = await session.exec(select(Parent).where(Parent.user_id == parent_user.id))
    parent = result.first()
    if not parent:
        parent = Parent(
            school_id=school.id,
            first_name=parent_user.first_name,
            last_name=parent_user.last_name,
            relationship="mother",
            phone=parent_user.phone or "+233 20 555 5555",
            email=parent_user.email,
            user_id=parent_user.id,
        )
        session.add(parent)
        await session.flush()

    result = await session.exec(
        select(StudentParent).where(
            (StudentParent.student_id == student.id) &
            (StudentParent.parent_id == parent.id)
        )
    )
    link = result.first()
    if not link:
        link = StudentParent(student_id=student.id, parent_id=parent.id)
        session.add(link)
        await session.flush()

    return student, parent


async def create_virtual_extra_class(session, school, teacher_staff, subject, classroom):
    result = await session.exec(
        select(ExtraClass).where(
            (ExtraClass.school_id == school.id) &
            (ExtraClass.title == EXTRA_CLASS_TITLE)
        )
    )
    extra_class = result.first()
    if extra_class:
        return extra_class

    extra_class = ExtraClass(
        school_id=school.id,
        teacher_id=teacher_staff.id,
        subject_id=subject.id,
        class_id=classroom.id,
        title=EXTRA_CLASS_TITLE,
        description="A virtual after-school course for students who want to build practical computer science skills.",
        pricing_type="hourly",
        price=30.0,
        billing_interval=BillingInterval.WEEKLY,
        payout_frequency=BillingInterval.MONTHLY,
        session_duration_hours=2.0,
        frequency_per_week=1,
        max_students_per_session=15,
        contact_phone=teacher_staff.phone,
        contact_email=teacher_staff.email,
        meeting_link="https://meet.school.edu.gh/virtual-extra-class",
        schedule="Saturdays 10:00 - 12:00",
        start_date=datetime.utcnow(),
        end_date=datetime.utcnow() + timedelta(weeks=8),
        status=ExtraClassStatus.PUBLISHED,
    )
    session.add(extra_class)
    await session.flush()
    return extra_class


async def create_sessions(session, school, extra_class):
    topics = [
        "Intro to Coding and Online Safety",
        "Algorithms and Problem Solving",
        "Web Development Fundamentals",
        "Project Showcase and Review",
    ]
    for idx, topic in enumerate(topics):
        result = await session.exec(
            select(ExtraClassSession).where(
                (ExtraClassSession.extra_class_id == extra_class.id) &
                (ExtraClassSession.topic == topic)
            )
        )
        if result.first():
            continue

        session_date = datetime.utcnow() + timedelta(days=7 * idx)
        session.add(
            ExtraClassSession(
                school_id=school.id,
                extra_class_id=extra_class.id,
                session_date=session_date,
                topic=topic,
                notes="Virtual session seeded for extra class.",
                attendance_count=5,
            )
        )
    await session.flush()


async def create_assignments(session, school, extra_class):
    assignments = [
        {
            "title": "Build a Simple Web Page",
            "description": "Create a small HTML page with a heading, paragraph, and image.",
            "due_days": 7,
        },
        {
            "title": "Algorithm Practice",
            "description": "Solve three simple algorithm puzzles and submit the answers.",
            "due_days": 14,
        },
        {
            "title": "Final Project Plan",
            "description": "Submit a short plan for the final virtual extra-class project.",
            "due_days": 21,
        },
    ]
    for item in assignments:
        result = await session.exec(
            select(ExtraClassAssignment).where(
                (ExtraClassAssignment.extra_class_id == extra_class.id) &
                (ExtraClassAssignment.title == item["title"])
            )
        )
        if result.first():
            continue

        session.add(
            ExtraClassAssignment(
                school_id=school.id,
                extra_class_id=extra_class.id,
                teacher_id=extra_class.teacher_id,
                title=item["title"],
                description=item["description"],
                instructions="Submit your work through the extra class portal.",
                due_date=datetime.utcnow() + timedelta(days=item["due_days"]),
                max_score=100.0,
            )
        )
    await session.flush()


async def create_enrollment_and_billing(session, school, extra_class, student, parent):
    result = await session.exec(
        select(ExtraClassEnrollment).where(
            (ExtraClassEnrollment.extra_class_id == extra_class.id) &
            (ExtraClassEnrollment.student_id == student.id)
        )
    )
    enrollment = result.first()
    if not enrollment:
        enrollment = ExtraClassEnrollment(
            school_id=school.id,
            extra_class_id=extra_class.id,
            student_id=student.id,
            parent_id=parent.id,
            status=EnrollmentStatus.APPROVED,
            approved_at=datetime.utcnow(),
        )
        session.add(enrollment)
        await session.flush()

    result = await session.exec(
        select(ExtraClassBillingCycle).where(ExtraClassBillingCycle.enrollment_id == enrollment.id)
    )
    billing_cycle = result.first()
    if not billing_cycle:
        billing_cycle = ExtraClassBillingCycle(
            school_id=school.id,
            extra_class_id=extra_class.id,
            teacher_id=extra_class.teacher_id,
            enrollment_id=enrollment.id,
            parent_id=parent.id,
            student_id=student.id,
            amount=extra_class.price,
            interval=extra_class.billing_interval,
            next_due_date=datetime.utcnow() + timedelta(days=7),
            status="paid",
        )
        session.add(billing_cycle)
        await session.flush()

    result = await session.exec(
        select(ExtraClassPayment).where(ExtraClassPayment.billing_cycle_id == billing_cycle.id)
    )
    payment = result.first()
    if not payment:
        session.add(
            ExtraClassPayment(
                school_id=school.id,
                billing_cycle_id=billing_cycle.id,
                amount=billing_cycle.amount,
                payment_method="card",
                reference=f"SEEDPAY-{billing_cycle.id[:8]}",
                status="paid",
                paid_at=datetime.utcnow(),
            )
        )
        await session.flush()

    return enrollment


async def create_submission_and_grade(session, school, extra_class, student):
    result = await session.exec(select(ExtraClassAssignment).where(ExtraClassAssignment.extra_class_id == extra_class.id))
    assignment = result.first()
    if not assignment:
        return

    result = await session.exec(
        select(ExtraClassSubmission).where(
            (ExtraClassSubmission.assignment_id == assignment.id) &
            (ExtraClassSubmission.student_id == student.id)
        )
    )
    submission = result.first()
    if not submission:
        submission = ExtraClassSubmission(
            school_id=school.id,
            extra_class_id=extra_class.id,
            assignment_id=assignment.id,
            student_id=student.id,
            status=SubmissionStatus.GRADED,
            submission_text="Completed the first virtual extra class assignment.",
            submitted_at=datetime.utcnow(),
        )
        session.add(submission)
        await session.flush()

    result = await session.exec(
        select(ExtraClassGrade).where(
            (ExtraClassGrade.assignment_id == assignment.id) &
            (ExtraClassGrade.student_id == student.id)
        )
    )
    grade = result.first()
    if not grade:
        session.add(
            ExtraClassGrade(
                school_id=school.id,
                extra_class_id=extra_class.id,
                assignment_id=assignment.id,
                submission_id=submission.id,
                student_id=student.id,
                teacher_id=extra_class.teacher_id,
                score=92.0,
                feedback="Excellent start to the virtual course.",
            )
        )
        await session.flush()


async def seed_virtual_extra_class_data():
    await init_db()

    async with async_session() as session:
        school, admin = await get_school_and_admin(session)
        if not school:
            print("No school admin found. Run seed_data.py first.")
            return

        teacher_user, teacher_staff = await get_or_create_teacher(session, school)
        classroom = await get_or_create_class(session, school)
        subject = await get_or_create_subject(session, school)
        student, parent = await get_or_create_student_and_parent(session, school, classroom)

        print(f"Seeding virtual extra class for school: {school.name}")
        print(f"Teacher: {teacher_user.email}")
        print(f"Student: {student.first_name} {student.last_name}")

        extra_class = await create_virtual_extra_class(session, school, teacher_staff, subject, classroom)
        await create_sessions(session, school, extra_class)
        await create_assignments(session, school, extra_class)
        enrollment = await create_enrollment_and_billing(session, school, extra_class, student, parent)
        await create_submission_and_grade(session, school, extra_class, student)

        await session.commit()

        print("\n✓ Virtual extra class seed completed")
        print(f"Extra Class ID: {extra_class.id}")
        print(f"Enrollment ID: {enrollment.id}")


if __name__ == "__main__":
    asyncio.run(seed_virtual_extra_class_data())
