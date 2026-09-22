"""Classes and Subjects router"""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlmodel import select, func, SQLModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import and_
from sqlalchemy.exc import IntegrityError
from datetime import datetime
from typing import Optional
from models.classroom import Class, ClassCreate, ClassUpdate, ClassLevel, Subject, SubjectCreate, SubjectCategory, ClassSubject, ClassWaitlistEntry
from models.student import Student, StudentStatus
from models.staff import Staff, TeacherAssignment
from models.school import AcademicTerm
from models.user import User, UserRole
from database import get_session
from auth import get_current_user, require_permission
from dependencies import resolve_campus_scope, resolve_write_campus_id, assert_campus_access

router = APIRouter(prefix="/classes", tags=["Classes & Subjects"])


class AssignSubjectRequest(SQLModel):
    academic_term_id: str


async def validate_academic_term(session: AsyncSession, school_id: str, academic_term_id: Optional[str]) -> None:
    """Reject a class/link pointing at an academic term that doesn't exist for this
    school, rather than silently creating a row with a dangling reference."""
    if not academic_term_id:
        return
    result = await session.execute(
        select(AcademicTerm).where(
            AcademicTerm.id == academic_term_id,
            AcademicTerm.school_id == school_id
        )
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="academic_term_id does not exist for this school")



@router.post("", response_model=dict)
async def create_class(
    class_data: ClassCreate,
    current_user: User = Depends(require_permission("academics.class.manage")),
    session: AsyncSession = Depends(get_session)
):
    """Create a new class"""
    school_id = current_user.school_id
    if not school_id and current_user.role != UserRole.SUPER_ADMIN:
        raise HTTPException(status_code=400, detail="No school context")

    await validate_academic_term(session, school_id, class_data.academic_term_id)

    class_data.campus_id = resolve_write_campus_id(current_user, class_data.campus_id)

    cls = Class(school_id=school_id, **class_data.model_dump())
    session.add(cls)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=400, detail="A class with this name, level, and section already exists")
    await session.refresh(cls)

    return {
        "id": cls.id,
        "school_id": cls.school_id,
        "name": cls.name,
        "level": cls.level,
        "section": cls.section,
        "capacity": cls.capacity,
        "room_number": cls.room_number,
        "is_active": cls.is_active,
        "created_at": cls.created_at.isoformat()
    }


@router.get("", response_model=list[dict])
async def list_classes(
    level: Optional[ClassLevel] = None,
    is_active: Optional[bool] = None,
    campus_id: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """List all classes"""
    school_id = current_user.school_id
    if not school_id and current_user.role != UserRole.SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="No school context")
    
    query = select(Class)
    
    if school_id:
        query = query.where(Class.school_id == school_id)
    
    if level:
        query = query.where(Class.level == level)
    
    if is_active is not None:
        query = query.where(Class.is_active == is_active)

    campus_id = resolve_campus_scope(current_user, campus_id)
    if campus_id:
        query = query.where(Class.campus_id == campus_id)

    query = query.order_by(Class.level, Class.name)
    
    result = await session.execute(query)
    classes = result.scalars().all()

    class_ids = [c.id for c in classes]
    class_student_counts = {}
    if class_ids:
        # active students only — a student's class_id is never cleared when
        # they exit (graduate/transfer/withdraw/get expelled), so an
        # unfiltered count here disagrees with every other "class size"
        # figure in the app (report-card rankings, dashboard per-class
        # summaries), which already filter this way.
        counts_result = await session.execute(
            select(Student.class_id, func.count(Student.id))
            .where(Student.class_id.in_(class_ids), Student.status == StudentStatus.ACTIVE)
            .group_by(Student.class_id)
        )
        class_student_counts = dict(counts_result.all())

    return [
        {
            "id": c.id,
            "school_id": c.school_id,
            "name": c.name,
            "level": c.level,
            "section": c.section,
            "capacity": c.capacity,
            "room_number": c.room_number,
            "campus_id": c.campus_id,
            "is_active": c.is_active,
            "student_count": class_student_counts.get(c.id, 0)
        }
        for c in classes
    ]


@router.get("/{class_id}", response_model=dict)
async def get_class(
    class_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Get class details with students and teachers"""
    result = await session.execute(select(Class).where(Class.id == class_id))
    cls = result.scalar_one_or_none()
    
    if not cls:
        raise HTTPException(status_code=404, detail="Class not found")
    
    if current_user.role != UserRole.SUPER_ADMIN and current_user.school_id != cls.school_id:
        raise HTTPException(status_code=403, detail="Access denied")
    assert_campus_access(current_user, cls.campus_id)

    # active only — see the same note on the class-list endpoint above.
    student_result = await session.execute(
        select(Student).where(Student.class_id == class_id, Student.status == StudentStatus.ACTIVE).order_by(Student.first_name)
    )
    students = student_result.scalars().all()
    
    subject_links = await session.execute(
        select(ClassSubject).where(ClassSubject.class_id == class_id)
    )
    class_subjects_list = subject_links.scalars().all()
    
    class_subjects = []
    if class_subjects_list:
        for cs in class_subjects_list:
            subject_result = await session.execute(select(Subject).where(Subject.id == cs.subject_id))
            subject = subject_result.scalar_one_or_none()
            if subject:
                class_subjects.append({
                    "subject_id": subject.id,
                    "subject_name": subject.name,
                    "subject_code": subject.code,
                    "subject_category": subject.category,
                    "academic_term_id": cs.academic_term_id
                })
    
    # Legacy subjects list for backward compatibility
    subjects = []
    if class_subjects_list:
        subject_result = await session.execute(select(Subject).where(Subject.id.in_([cs.subject_id for cs in class_subjects_list])))
        subjects = [
            {"id": s.id, "name": s.name, "code": s.code, "category": s.category}
            for s in subject_result.scalars().all()
        ]
    
    # Get teacher assignments
    assignments_result = await session.execute(
        select(TeacherAssignment).where(TeacherAssignment.class_id == class_id)
    )
    assignments = []
    for assignment in assignments_result.scalars().all():
        staff_result = await session.execute(select(Staff).where(Staff.id == assignment.staff_id))
        staff = staff_result.scalar_one_or_none()
        
        subject_result = await session.execute(select(Subject).where(Subject.id == assignment.subject_id))
        subject = subject_result.scalar_one_or_none()
        
        if staff and subject:
            assignments.append({
                "id": assignment.id,
                "staff_id": assignment.staff_id,
                "teacher_name": f"{staff.first_name} {staff.last_name}",
                "subject_id": assignment.subject_id,
                "subject_name": subject.name,
                "academic_term_id": assignment.academic_term_id,
                "is_class_teacher": assignment.is_class_teacher
            })
    
    return {
        "id": cls.id,
        "school_id": cls.school_id,
        "name": cls.name,
        "level": cls.level,
        "section": cls.section,
        "capacity": cls.capacity,
        "room_number": cls.room_number,
        "is_active": cls.is_active,
        "students": [
            {
                "id": s.id,
                "student_id": s.student_id,
                "first_name": s.first_name,
                "last_name": s.last_name,
                "gender": s.gender,
                "photo_url": s.photo_url
            }
            for s in students
        ],
        "class_subjects": class_subjects,
        "subjects": subjects,
        "teachers": assignments,
        "student_count": len(students)
    }


@router.get("/{class_id}/students", response_model=dict)
async def get_class_students(
    class_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Get all students in a specific class"""
    # Verify class exists
    class_result = await session.execute(select(Class).where(Class.id == class_id))
    cls = class_result.scalar_one_or_none()
    
    if not cls:
        raise HTTPException(status_code=404, detail="Class not found")

    if current_user.role != UserRole.SUPER_ADMIN and current_user.school_id != cls.school_id:
        raise HTTPException(status_code=403, detail="Access denied")
    assert_campus_access(current_user, cls.campus_id)

    # Get students in this class (active only — see the note on the class-list endpoint above)
    student_result = await session.execute(
        select(Student).where(Student.class_id == class_id, Student.status == StudentStatus.ACTIVE)
    )
    students = student_result.scalars().all()
    
    return {
        "items": [
            {
                "id": s.id,
                "student_id": s.student_id,
                "first_name": s.first_name,
                "last_name": s.last_name,
                "photo_url": s.photo_url,
                "status": s.status
            }
            for s in students
        ],
        "total": len(students)
    }


@router.put("/{class_id}", response_model=dict)
async def update_class(
    class_id: str,
    class_data: ClassUpdate,
    current_user: User = Depends(require_permission("academics.class.manage")),
    session: AsyncSession = Depends(get_session)
):
    """Update class details. Partial update — only fields present in the request are changed."""
    result = await session.execute(select(Class).where(Class.id == class_id))
    cls = result.scalar_one_or_none()

    if not cls:
        raise HTTPException(status_code=404, detail="Class not found")

    if current_user.role == UserRole.SCHOOL_ADMIN and current_user.school_id != cls.school_id:
        raise HTTPException(status_code=403, detail="Access denied")
    assert_campus_access(current_user, cls.campus_id)

    update_data = class_data.model_dump(exclude_unset=True)
    if current_user.campus_id:
        update_data["campus_id"] = current_user.campus_id

    if "academic_term_id" in update_data:
        await validate_academic_term(session, cls.school_id, update_data["academic_term_id"])

    if "capacity" in update_data:
        active_count = (await session.execute(
            select(func.count(Student.id)).where(Student.class_id == class_id, Student.status == StudentStatus.ACTIVE)
        )).scalar() or 0
        if update_data["capacity"] < active_count:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot set capacity below the class's current active enrollment ({active_count} students)",
            )

    for key, value in update_data.items():
        setattr(cls, key, value)

    cls.updated_at = datetime.utcnow()
    session.add(cls)
    await session.commit()
    
    return {"message": "Class updated successfully"}


# Subjects
@router.post("/subjects", response_model=dict)
async def create_subject(
    subject_data: SubjectCreate,
    current_user: User = Depends(require_permission("academics.subject.manage")),
    session: AsyncSession = Depends(get_session)
):
    """Create a new subject"""
    school_id = current_user.school_id
    if not school_id and current_user.role != UserRole.SUPER_ADMIN:
        raise HTTPException(status_code=400, detail="No school context")
    
    subject = Subject(school_id=school_id, **subject_data.model_dump())
    session.add(subject)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=400, detail="A subject with this code already exists")
    await session.refresh(subject)

    return {
        "id": subject.id,
        "school_id": subject.school_id,
        "name": subject.name,
        "code": subject.code,
        "category": subject.category,
        "description": subject.description,
        "credit_hours": subject.credit_hours,
        "is_active": subject.is_active
    }


@router.get("/subjects/all", response_model=list[dict])
async def list_subjects(
    category: Optional[SubjectCategory] = None,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """List all subjects"""
    school_id = current_user.school_id
    if not school_id and current_user.role != UserRole.SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="No school context")
    
    query = select(Subject).where(
        and_(Subject.school_id == school_id, Subject.is_active == True)
    )
    
    if category:
        query = query.where(Subject.category == category)
    
    query = query.order_by(Subject.name)
    
    result = await session.execute(query)
    subjects = result.scalars().all()
    
    return [
        {
            "id": s.id,
            "school_id": s.school_id,
            "name": s.name,
            "code": s.code,
            "category": s.category,
            "description": s.description,
            "credit_hours": s.credit_hours,
            "is_active": s.is_active
        }
        for s in subjects
    ]


@router.post("/{class_id}/subjects/{subject_id}", response_model=dict)
async def assign_subject_to_class(
    class_id: str,
    subject_id: str,
    body: AssignSubjectRequest,
    current_user: User = Depends(require_permission("academics.class_subject.manage")),
    session: AsyncSession = Depends(get_session)
):
    """Assign a subject to a class"""
    academic_term_id = body.academic_term_id

    class_result = await session.execute(select(Class).where(Class.id == class_id))
    cls = class_result.scalar_one_or_none()
    if not cls:
        raise HTTPException(status_code=404, detail="Class not found")
    if current_user.role != UserRole.SUPER_ADMIN and current_user.school_id != cls.school_id:
        raise HTTPException(status_code=403, detail="Access denied")
    assert_campus_access(current_user, cls.campus_id)

    subject_result = await session.execute(select(Subject).where(Subject.id == subject_id))
    subject = subject_result.scalar_one_or_none()
    if not subject or (current_user.role != UserRole.SUPER_ADMIN and subject.school_id != current_user.school_id):
        raise HTTPException(status_code=404, detail="Subject not found")

    await validate_academic_term(session, cls.school_id, academic_term_id)

    existing = await session.execute(
        select(ClassSubject).where(
            ClassSubject.class_id == class_id,
            ClassSubject.subject_id == subject_id,
            ClassSubject.academic_term_id == academic_term_id
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Subject already assigned to this class")
    
    link = ClassSubject(
        school_id=cls.school_id,
        class_id=class_id,
        subject_id=subject_id,
        academic_term_id=academic_term_id
    )
    session.add(link)
    try:
        await session.commit()
    except IntegrityError:
        # Backstop for the race between the existence check above and this
        # insert — two concurrent requests could both pass the check.
        await session.rollback()
        raise HTTPException(status_code=400, detail="Subject already assigned to this class")

    return {"message": "Subject assigned to class"}


@router.delete("/{class_id}/subjects/{subject_id}", response_model=dict)
async def remove_subject_from_class(
    class_id: str,
    subject_id: str,
    academic_term_id: str = Query(..., description="Which term's class-subject link to remove — the same subject can be assigned to a class across multiple terms, so this disambiguates which one"),
    current_user: User = Depends(require_permission("academics.class_subject.manage")),
    session: AsyncSession = Depends(get_session)
):
    """Remove a subject from a class for a specific term. Required, not
    inferred — assign_subject_to_class always scopes creation by term too
    (a class+subject can legitimately have a separate link per term), so
    there's no single "the" link to fall back to without one."""
    result = await session.execute(
        select(ClassSubject).where(
            ClassSubject.class_id == class_id,
            ClassSubject.subject_id == subject_id,
            ClassSubject.academic_term_id == academic_term_id,
        )
    )
    link = result.scalar_one_or_none()

    if not link:
        raise HTTPException(status_code=404, detail="Subject not assigned to this class for that term")

    if current_user.role != UserRole.SUPER_ADMIN and current_user.school_id != link.school_id:
        raise HTTPException(status_code=403, detail="Access denied")

    # ClassSubject carries no campus_id of its own -- check via the Class it links.
    class_result = await session.execute(select(Class).where(Class.id == link.class_id))
    cls = class_result.scalar_one_or_none()
    if cls:
        assert_campus_access(current_user, cls.campus_id)

    await session.delete(link)
    await session.commit()

    return {"message": "Subject removed from class"}


# ── Waitlist ────────────────────────────────────────────────────────────
# Entries are created by routers.students.check_class_capacity(auto_waitlist=True)
# and the bulk-promotion except-branch when a class is full — see that
# module for how entries land here in the first place.

@router.get("/{class_id}/waitlist", response_model=list[dict])
async def list_class_waitlist(
    class_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """The class's waitlist queue, ordered by position (earliest request first)."""
    class_result = await session.execute(select(Class).where(Class.id == class_id))
    cls = class_result.scalar_one_or_none()
    if not cls:
        raise HTTPException(status_code=404, detail="Class not found")
    if current_user.role != UserRole.SUPER_ADMIN and current_user.school_id != cls.school_id:
        raise HTTPException(status_code=403, detail="Access denied")
    assert_campus_access(current_user, cls.campus_id)

    result = await session.execute(
        select(ClassWaitlistEntry).where(ClassWaitlistEntry.class_id == class_id).order_by(ClassWaitlistEntry.position)
    )
    entries = result.scalars().all()

    student_ids = [e.student_id for e in entries]
    student_names: dict = {}
    if student_ids:
        student_result = await session.execute(select(Student).where(Student.id.in_(student_ids)))
        student_names = {s.id: f"{s.first_name} {s.last_name}" for s in student_result.scalars().all()}

    return [{**e.model_dump(), "student_name": student_names.get(e.student_id, "Unknown")} for e in entries]


@router.post("/{class_id}/waitlist/{entry_id}/offer", response_model=dict)
async def offer_waitlist_seat(
    class_id: str,
    entry_id: str,
    current_user: User = Depends(require_permission("academics.waitlist.manage")),
    session: AsyncSession = Depends(get_session)
):
    """Enroll the waitlisted student into the class (a seat has freed up) and
    mark their entry "enrolled". This is a deliberate admin action offering a
    specific freed seat, so it enrolls directly rather than re-checking
    capacity / re-waitlisting."""
    class_result = await session.execute(select(Class).where(Class.id == class_id))
    cls = class_result.scalar_one_or_none()
    if not cls:
        raise HTTPException(status_code=404, detail="Class not found")
    if current_user.role != UserRole.SUPER_ADMIN and current_user.school_id != cls.school_id:
        raise HTTPException(status_code=403, detail="Access denied")
    assert_campus_access(current_user, cls.campus_id)

    entry_result = await session.execute(
        select(ClassWaitlistEntry).where(ClassWaitlistEntry.id == entry_id, ClassWaitlistEntry.class_id == class_id)
    )
    entry = entry_result.scalar_one_or_none()
    if not entry:
        raise HTTPException(status_code=404, detail="Waitlist entry not found")
    if entry.status != "waiting":
        raise HTTPException(status_code=400, detail=f"This entry is already {entry.status}, not waiting")

    student_result = await session.execute(select(Student).where(Student.id == entry.student_id))
    student = student_result.scalar_one_or_none()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    student.class_id = class_id
    student.updated_at = datetime.utcnow()
    session.add(student)

    entry.status = "enrolled"
    session.add(entry)

    from routers.students import _record_enrollment
    await _record_enrollment(session, cls.school_id, student.id, class_id, reason="waitlist_offer")

    await session.commit()

    return {"message": f"{student.first_name} {student.last_name} enrolled from the waitlist", "student_id": student.id, "entry_id": entry.id}


@router.delete("/{class_id}/waitlist/{entry_id}", response_model=dict)
async def cancel_waitlist_entry(
    class_id: str,
    entry_id: str,
    current_user: User = Depends(require_permission("academics.waitlist.manage")),
    session: AsyncSession = Depends(get_session)
):
    """Cancel a waitlist entry (student no longer wants the seat, or an admin
    is clearing a stale request)."""
    class_result = await session.execute(select(Class).where(Class.id == class_id))
    cls = class_result.scalar_one_or_none()
    if not cls:
        raise HTTPException(status_code=404, detail="Class not found")
    if current_user.role != UserRole.SUPER_ADMIN and current_user.school_id != cls.school_id:
        raise HTTPException(status_code=403, detail="Access denied")
    assert_campus_access(current_user, cls.campus_id)

    entry_result = await session.execute(
        select(ClassWaitlistEntry).where(ClassWaitlistEntry.id == entry_id, ClassWaitlistEntry.class_id == class_id)
    )
    entry = entry_result.scalar_one_or_none()
    if not entry:
        raise HTTPException(status_code=404, detail="Waitlist entry not found")

    entry.status = "cancelled"
    session.add(entry)
    await session.commit()

    return {"message": "Waitlist entry cancelled", "entry_id": entry.id}
