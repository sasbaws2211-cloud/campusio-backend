"""Grades and Report Cards router"""
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status, Query
from fastapi.responses import StreamingResponse
from sqlmodel import select, func, SQLModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import and_
from sqlalchemy.exc import IntegrityError
from datetime import datetime
from typing import Optional, List
from io import BytesIO
import logging
import zipfile

logger = logging.getLogger(__name__)
from models.grade import Grade, GradeCreate, AssessmentType, GradeScale, GradingScheme, CreateGradingSchemeRequest, UpdateGradingSchemeRequest, ReportCard, ReportCardStatus, ReportCardRecall, RecallReportCardRequest, StandardMasteryRecord, StandardMasteryRecordCreate, MASTERY_LEVELS
from models.curriculum import CurriculumStandard
from models.student import Student,StudentParent,Parent
from models.classroom import Class, ClassLevel, Subject, SubjectCreate, SubjectCategory, ClassSubject
from models.school import AcademicTerm, School
from models.attendance import Attendance, AttendanceStatus
from models.user import User, UserRole
from models.staff import Staff
from models.report_template import ReportTemplate
from models.fee import Fee
from models.communication import MessageType
from database import get_session
from auth import get_current_user, require_permission, require_roles
from dependencies import assert_campus_access
from services.audit_service import log_event
from services.report_card_pdf_service import ReportCardPDFService, compute_overall_ges_score, compute_subject_ges_totals
from services import grading_service
from services import parent_notification_service
from utils import grade_scale as shared_ges_scale
from services.plan_gating import require_plan_feature

router = APIRouter(prefix="/grades", tags=["Grades & Report Cards"])

# get_class_grades is the one endpoint in this file that returns a whole
# class's roster + every student's grades in one call (contrast with
# get_student_grades, scoped to a single student a parent/student may
# legitimately view their own record via) — staff-only, no legitimate
# student/parent use case for a bulk class view.
STAFF_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.TEACHER, UserRole.REGISTRAR)


async def _recorded_by_id(current_user: User, session: AsyncSession) -> str:
    """Grade.recorded_by is keyed on Staff.id, not User.id — same convention
    routers/teacher/grades.py::resolve_staff documents and already follows.
    Falls back to current_user.id when no Staff row exists (e.g. a platform
    SUPER_ADMIN with no staff profile) so this endpoint keeps working for
    non-staff callers exactly as before; TEACHER callers (the only role the
    ownership check below actually gates on) always resolve a real Staff.id."""
    result = await session.execute(select(Staff).where(Staff.user_id == current_user.id, Staff.school_id == current_user.school_id))
    staff = result.scalar_one_or_none()
    return staff.id if staff else current_user.id


async def _resolve_subject_weights(session: AsyncSession, school_id: str, class_id: Optional[str], grades) -> dict:
    """{subject_id: (ca_weight, exam_weight)} for every subject in `grades`,
    via the class's level -> the school's configured GradingScheme(s)
    (services/grading_service.py::match_weights) — falls back to 50/50 for
    any subject with nothing configured. Every report-card compute/preview/
    download site in this file uses this so they can never disagree with
    each other over which split applies."""
    class_level = None
    if class_id:
        the_class = await session.get(Class, class_id)
        class_level = the_class.level if the_class else None
    schemes = await grading_service.get_school_schemes(session, school_id)
    return grading_service.build_subject_weights(schemes, class_level, {g.subject_id for g in grades})


class CreateGradeScaleRequest(SQLModel):
    grade: str
    min_score: float
    max_score: float
    description: str
    gpa_point: float


class GenerateReportCardRequest(SQLModel):
    student_id: str
    academic_term_id: str
    class_teacher_remarks: Optional[str] = None
    head_teacher_remarks: Optional[str] = None
    # GES SBA terminal-report fields
    attitude: Optional[str] = None
    conduct: Optional[str] = None
    interest: Optional[str] = None
    vacation_date: Optional[str] = None
    reopening_date: Optional[str] = None
    promoted_to: Optional[str] = None
    promotion_decision: Optional[str] = None  # "promoted" | "repeated" | "graduated" — see models.grade.PromotionDecision



# ============ TEMPLATE HELPER FUNCTIONS ============
async def get_school_template(session: AsyncSession, school_id: str) -> Optional[str]:
    """
    Fetch school's default report template from database.
    Returns None if no template exists (will use file-based fallback).
    """
    try:
        result = await session.execute(
            select(ReportTemplate).where(
                ReportTemplate.school_id == school_id,
                ReportTemplate.is_default == True,
                ReportTemplate.is_active == True
            )
        )
        template = result.scalar_one_or_none()
        return template.html_content if template else None
    except Exception as e:
        logger.warning(f"Failed to fetch template for school {school_id}: {str(e)}")
        return None  # Fallback to file-based template


# GES Grading Scale (single shared source — see utils/grade_scale.py)
GES_GRADE_SCALE = shared_ges_scale.GES_GRADE_SCALE
get_letter_grade = shared_ges_scale.get_letter_grade


async def get_report_grading_context(session: AsyncSession, school_id: str, class_id: Optional[str]) -> tuple:
    """(schemes, class_level) for report-card/gradebook grade formatting —
    resolve once per request (or once per bulk operation, reused across every
    student in it) and pass into services.grading_service.match_scale() /
    ReportCardPDFService.format_grade_data()'s grading_schemes/class_level args."""
    schemes = await grading_service.get_school_schemes(session, school_id)
    class_level = None
    if class_id:
        classroom = await session.get(Class, class_id)
        class_level = classroom.level if classroom else None
    return schemes, class_level


def validate_grade_score(score: float, max_score: float) -> None:
    """Guard against out-of-range or zero-division scores before they hit the DB."""
    if max_score is None or max_score <= 0:
        raise HTTPException(status_code=400, detail="max_score must be greater than 0")
    if score is None or score < 0:
        raise HTTPException(status_code=400, detail="score cannot be negative")
    if score > max_score:
        raise HTTPException(status_code=400, detail="score cannot exceed max_score")


async def compute_class_rankings(session: AsyncSession, class_id: str, academic_term_id: str) -> tuple:
    """Rank every active student in a class by their GES overall average for the
    term (descending; ties share a rank — standard competition ranking, e.g.
    1, 2, 2, 4). Returns ({student_id: rank}, class_average). ReportCard.position
    was previously never set by anything, so every report card rendered the
    literal text "None" where the class rank should be; class_average is
    likewise computed fresh here rather than stored, same as everything else
    report cards derive live from Grade records."""
    students_result = await session.execute(
        select(Student.id).where(Student.class_id == class_id, Student.status == "active")
    )
    student_ids = students_result.scalars().all()
    if not student_ids:
        return {}, 0.0

    grades_result = await session.execute(
        select(Grade).where(
            Grade.student_id.in_(student_ids),
            Grade.academic_term_id == academic_term_id
        )
    )
    all_grades = grades_result.scalars().all()
    grades_by_student: dict = {}
    for g in all_grades:
        grades_by_student.setdefault(g.student_id, []).append(g)

    # Same per-subject CA:exam split every other ranking-adjacent computation
    # uses (generate_report_card, etc.) — resolved once for the whole class
    # rather than once per student, since every student here shares the same
    # class_level and the subject set is the same across all of them.
    subject_weights = None
    the_class = await session.get(Class, class_id)
    if the_class:
        schemes = await grading_service.get_school_schemes(session, the_class.school_id)
        subject_weights = grading_service.build_subject_weights(
            schemes, the_class.level, {g.subject_id for g in all_grades}
        )

    averages = [
        (sid, compute_overall_ges_score(grades_by_student.get(sid, []), weights=subject_weights)[1])
        for sid in student_ids
    ]

    # class_average already excludes students with no grades at all (avg == 0)
    # from the pool it's computed over -- rankings previously did not, so a
    # student with zero grades tied for last place (avg == 0) still occupied
    # a numeric competition rank, shifting every graded student's position
    # below them upward by however many ungraded students are in the class.
    # Excluding them here means an ungraded student has no entry in the
    # returned dict at all; every caller already does rankings.get(student_id),
    # which resolves to None ("not yet ranked") rather than a misleading number.
    scored_pairs = [(sid, avg) for sid, avg in averages if avg > 0]
    scored_pairs.sort(key=lambda pair: pair[1], reverse=True)

    rankings: dict = {}
    current_rank = 0
    previous_score = None
    for idx, (sid, avg) in enumerate(scored_pairs):
        if avg != previous_score:
            current_rank = idx + 1
            previous_score = avg
        rankings[sid] = current_rank

    scored = [avg for _, avg in scored_pairs]
    class_average = round(sum(scored) / len(scored), 1) if scored else 0.0
    return rankings, class_average


async def validate_grade_references(session: AsyncSession, school_id: str, student_id: str, class_id: str, subject_id: str, academic_term_id: str, current_user: Optional[User] = None) -> None:
    """Grade.student_id/class_id/subject_id have no DB-level foreign keys, so without
    this check a grade could be recorded against a student/class/subject from a
    different school (or an id that doesn't exist at all) and would be silently
    persisted as a cross-tenant orphan record.

    current_user is optional so existing internal callers that don't have one
    keep working unchanged; when supplied, also enforces campus scoping —
    grades.py previously had none anywhere, letting a campus-scoped
    SCHOOL_ADMIN/TEACHER record a grade against a student/class in a
    different campus of the same school."""
    student_result = await session.execute(
        select(Student).where(Student.id == student_id, Student.school_id == school_id)
    )
    student = student_result.scalar_one_or_none()
    if not student:
        raise HTTPException(status_code=400, detail="student_id does not exist for this school")
    if current_user is not None:
        assert_campus_access(current_user, student.campus_id)

    class_result = await session.execute(
        select(Class).where(Class.id == class_id, Class.school_id == school_id)
    )
    cls = class_result.scalar_one_or_none()
    if not cls:
        raise HTTPException(status_code=400, detail="class_id does not exist for this school")
    if current_user is not None:
        assert_campus_access(current_user, cls.campus_id)

    subject_result = await session.execute(
        select(Subject).where(Subject.id == subject_id, Subject.school_id == school_id)
    )
    if not subject_result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="subject_id does not exist for this school")

    term_result = await session.execute(
        select(AcademicTerm).where(AcademicTerm.id == academic_term_id, AcademicTerm.school_id == school_id)
    )
    term = term_result.scalar_one_or_none()
    if not term:
        raise HTTPException(status_code=400, detail="academic_term_id does not exist for this school")
    if term.is_locked:
        raise HTTPException(status_code=423, detail="This academic term is locked and no longer accepts new grades")


@router.get("/ges-scale", response_model=List[dict])
async def get_ges_grade_scale():
    """Get the GES (Ghana Education Service) grading scale"""
    return GES_GRADE_SCALE


# ============ STANDARDS-BASED (MASTERY) GRADING ============
# Optional, parallel record — see models.grade.StandardMasteryRecord's
# docstring. Purely additive alongside the numeric Grade/percentage flow
# above; nothing here changes how a Grade is recorded or how a report
# card's percentage-based rendering works.

@router.post("/mastery-records", response_model=dict)
async def create_mastery_record(
    payload: StandardMasteryRecordCreate,
    current_user: User = Depends(require_permission("academics.mastery_record.create")),
    session: AsyncSession = Depends(get_session)
):
    """Record a standards-based mastery assessment for a student."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    if payload.mastery_level not in MASTERY_LEVELS:
        raise HTTPException(status_code=422, detail=f"mastery_level must be one of: {', '.join(MASTERY_LEVELS)}")

    standard_result = await session.execute(
        select(CurriculumStandard).where(CurriculumStandard.id == payload.standard_id, CurriculumStandard.school_id == school_id)
    )
    if not standard_result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="standard_id does not exist for this school")

    student_result = await session.execute(select(Student).where(Student.id == payload.student_id, Student.school_id == school_id))
    if not student_result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="student_id does not exist for this school")

    record = StandardMasteryRecord(school_id=school_id, assessed_by=current_user.id, **payload.model_dump())
    session.add(record)
    await session.commit()
    await session.refresh(record)
    return record.model_dump()


@router.post("", response_model=dict)
async def record_grade(
    grade_data: GradeCreate,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(require_permission("academics.grade.create")),
    session: AsyncSession = Depends(get_session)
):
    """Record a grade for a student"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    validate_grade_score(grade_data.score, grade_data.max_score)
    await validate_grade_references(
        session, school_id, grade_data.student_id, grade_data.class_id,
        grade_data.subject_id, grade_data.academic_term_id, current_user
    )

    grade = Grade(
        school_id=school_id,
        recorded_by=await _recorded_by_id(current_user, session),
        **grade_data.model_dump()
    )
    session.add(grade)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=400,
            detail="This student already has a grade recorded for this subject, term, and assessment type — edit the existing grade instead"
        )
    await session.refresh(grade)

    from services.webhook_service import emit_event
    await emit_event(
        session, background_tasks, school_id, "grade.recorded",
        {
            "id": grade.id,
            "student_id": grade.student_id,
            "subject_id": grade.subject_id,
            "assessment_type": grade.assessment_type,
            "score": grade.score,
            "max_score": grade.max_score,
        },
    )

    return {
        "grade_id": grade.id,
        "id": grade.id,
        "student_id": grade.student_id,
        "subject_id": grade.subject_id,
        "class_id": grade.class_id,
        "assessment_type": grade.assessment_type,
        "score": grade.score,
        "max_score": grade.max_score,
        "weight": grade.weight,
        "percentage": round(grade.score / grade.max_score * 100, 1),
        "recorded_at": grade.created_at.isoformat(),
        "message": "Grade recorded"
    }


@router.patch("/{grade_id}", response_model=dict)
async def update_grade(
    grade_id: str,
    grade_data: GradeCreate,
    current_user: User = Depends(require_permission("academics.grade.update")),
    session: AsyncSession = Depends(get_session)
):
    """Update a grade"""
    result = await session.execute(select(Grade).where(Grade.id == grade_id))
    grade = result.scalar_one_or_none()
    
    if not grade:
        raise HTTPException(status_code=404, detail="Grade not found")

    if current_user.role != UserRole.SUPER_ADMIN and current_user.school_id != grade.school_id:
        raise HTTPException(status_code=403, detail="Access denied")

    if current_user.role == UserRole.TEACHER and grade.recorded_by != await _recorded_by_id(current_user, session):
        raise HTTPException(status_code=403, detail="You can only edit grades you recorded yourself")

    # Update grade fields
    update_data = grade_data.model_dump(exclude_unset=True)

    await validate_grade_references(
        session, grade.school_id,
        update_data.get("student_id", grade.student_id),
        update_data.get("class_id", grade.class_id),
        update_data.get("subject_id", grade.subject_id),
        update_data.get("academic_term_id", grade.academic_term_id),
        current_user,
    )

    for key, value in update_data.items():
        setattr(grade, key, value)

    validate_grade_score(grade.score, grade.max_score)

    grade.updated_at = datetime.utcnow()
    session.add(grade)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=400,
            detail="Another grade already exists for this student, subject, term, and assessment type"
        )
    await session.refresh(grade)

    return {
        "grade_id": grade.id,
        "id": grade.id,
        "student_id": grade.student_id,
        "subject_id": grade.subject_id,
        "class_id": grade.class_id,
        "assessment_type": grade.assessment_type,
        "score": grade.score,
        "max_score": grade.max_score,
        "weight": grade.weight,
        "percentage": round(grade.score / grade.max_score * 100, 1),
        "recorded_at": grade.created_at.isoformat(),
        "updated_at": grade.updated_at.isoformat(),
        "message": "Grade updated"
    }


@router.delete("/{grade_id}", response_model=dict)
async def delete_grade(
    grade_id: str,
    current_user: User = Depends(require_permission("academics.grade.delete")),
    session: AsyncSession = Depends(get_session)
):
    """Delete a grade"""
    result = await session.execute(select(Grade).where(Grade.id == grade_id))
    grade = result.scalar_one_or_none()
    
    if not grade:
        raise HTTPException(status_code=404, detail="Grade not found")

    if current_user.role != UserRole.SUPER_ADMIN and current_user.school_id != grade.school_id:
        raise HTTPException(status_code=403, detail="Access denied")

    if current_user.role == UserRole.TEACHER and grade.recorded_by != await _recorded_by_id(current_user, session):
        raise HTTPException(status_code=403, detail="You can only delete grades you recorded yourself")

    deleted_summary = {"student_id": grade.student_id, "subject_id": grade.subject_id, "score": grade.score, "max_score": grade.max_score}
    await session.delete(grade)
    await session.commit()

    await log_event(
        session, actor=current_user, action="grade.deleted", entity_type="grade",
        entity_id=grade_id, school_id=grade.school_id,
        summary=f"{current_user.email} deleted a grade for student {grade.student_id}",
        old_values=deleted_summary,
    )

    return {"message": "Grade deleted successfully"}


@router.get("/student/{student_id}", response_model=dict)
async def get_student_grades(
    student_id: str,
    academic_term_id: Optional[str] = None,
    subject_id: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Get all grades for a student"""
    student_result = await session.execute(select(Student).where(Student.id == student_id))
    student = student_result.scalar_one_or_none()
    
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    if current_user.role != UserRole.SUPER_ADMIN and current_user.school_id != student.school_id:
        raise HTTPException(status_code=403, detail="Access denied")
    # Only same-school was checked above -- any PARENT/STUDENT account could
    # pull any other student's raw grades just by supplying their student_id.
    # _has_report_card_access (defined later in this file) is the same
    # ownership check every report-card view/preview/download endpoint
    # already uses for the identical PARENT/STUDENT ownership question.
    if not await _has_report_card_access(session, current_user, student):
        raise HTTPException(status_code=403, detail="Access denied")

    query = select(Grade).where(Grade.student_id == student_id)

    if academic_term_id:
        query = query.where(Grade.academic_term_id == academic_term_id)
    if subject_id:
        query = query.where(Grade.subject_id == subject_id)

    result = await session.execute(query)
    grades = result.scalars().all()
    
    subject_ids = list(set(g.subject_id for g in grades))
    subject_names = {}
    if subject_ids:
        subject_result = await session.execute(select(Subject).where(Subject.id.in_(subject_ids)))
        for s in subject_result.scalars().all():
            subject_names[s.id] = s.name
    
    return {
        "student_id": student_id,
        "student_name": f"{student.first_name} {student.last_name}",
        "grades": [
            {
                "id": g.id,
                "subject_id": g.subject_id,
                "subject_name": subject_names.get(g.subject_id, "Unknown"),
                "assessment_type": g.assessment_type,
                "score": g.score,
                "max_score": g.max_score,
                "percentage": round(g.score / g.max_score * 100, 1),
                "weight": g.weight,
                "remarks": g.remarks,
                "created_at": g.created_at.isoformat()
            }
            for g in grades
        ]
    }


@router.post("/scales", response_model=dict)
async def create_grade_scale(
    body: CreateGradeScaleRequest,
    current_user: User = Depends(require_permission("academics.grade_scale.manage")),
    _plan_check: User = Depends(require_plan_feature("academic_reports")),
    session: AsyncSession = Depends(get_session)
):
    """Create a grade scale entry"""
    grade = body.grade
    min_score = body.min_score
    max_score = body.max_score
    description = body.description
    gpa_point = body.gpa_point
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    scale = GradeScale(
        school_id=school_id,
        grade=grade,
        min_score=min_score,
        max_score=max_score,
        description=description,
        gpa_point=gpa_point
    )
    session.add(scale)
    await session.commit()
    
    return {"message": "Grade scale created"}


# Subject Management Endpoints
@router.get("/subjects", response_model=List[dict])
async def get_subjects(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Get all subjects for school"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    result = await session.execute(
        select(Subject).where(
            and_(Subject.school_id == school_id, Subject.is_active == True)
        )
        .order_by(Subject.category, Subject.name)
    )
    subjects = result.scalars().all()
    
    return [
        {
            "id": s.id,
            "name": s.name,
            "code": s.code,
            "category": s.category,
            "credit_hours": s.credit_hours,
            "description": s.description
        }
        for s in subjects
    ]


@router.post("/subjects", response_model=dict)
async def create_subject(
    subject_data: SubjectCreate,
    current_user: User = Depends(require_permission("academics.subject.manage")),
    session: AsyncSession = Depends(get_session)
):
    """Create a new subject"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    subject = Subject(
        school_id=school_id,
        **subject_data.model_dump()
    )
    session.add(subject)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=400, detail="A subject with this code already exists")
    await session.refresh(subject)

    return {
        "id": subject.id,
        "name": subject.name,
        "code": subject.code,
        "message": "Subject created successfully"
    }


@router.post("/subjects/seed-defaults", response_model=dict)
async def seed_default_subjects(
    current_user: User = Depends(require_permission("academics.subject.manage")),
    session: AsyncSession = Depends(get_session)
):
    """Seed default GES subjects for the school"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    # Check if subjects already exist
    existing = await session.execute(
        select(Subject).where(Subject.school_id == school_id).limit(1)
    )
    if existing.scalar_one_or_none():
        return {"message": "Subjects already exist", "created": 0}
    
    default_subjects = [
        # Core Subjects
        {"name": "English Language", "code": "ENG", "category": "core", "credit_hours": 5},
        {"name": "Mathematics", "code": "MATH", "category": "core", "credit_hours": 5},
        {"name": "Integrated Science", "code": "SCI", "category": "core", "credit_hours": 4},
        {"name": "Social Studies", "code": "SOC", "category": "core", "credit_hours": 4},
        {"name": "Computing/ICT", "code": "ICT", "category": "core", "credit_hours": 2},
        {"name": "Ghanaian Language", "code": "GHL", "category": "core", "credit_hours": 2},
        {"name": "Religious & Moral Education", "code": "RME", "category": "core", "credit_hours": 2},
        {"name": "Creative Arts", "code": "CRA", "category": "core", "credit_hours": 2},
        {"name": "Physical Education", "code": "PE", "category": "core", "credit_hours": 2},
        # Elective Subjects
        {"name": "French", "code": "FRE", "category": "elective", "credit_hours": 2},
        {"name": "Agriculture", "code": "AGR", "category": "elective", "credit_hours": 2},
        {"name": "Home Economics", "code": "HME", "category": "elective", "credit_hours": 2},
        {"name": "Basic Design & Technology", "code": "BDT", "category": "elective", "credit_hours": 2},
    ]
    
    created_count = 0
    for subj in default_subjects:
        subject = Subject(
            school_id=school_id,
            name=subj["name"],
            code=subj["code"],
            category=SubjectCategory(subj["category"]),
            credit_hours=subj["credit_hours"]
        )
        session.add(subject)
        created_count += 1
    
    await session.commit()
    
    return {"message": f"Created {created_count} default subjects", "created": created_count}


# Grade Recording by Class
@router.get("/class/{class_id}", response_model=dict)
async def get_class_grades(
    class_id: str,
    subject_id: Optional[str] = None,
    assessment_type: Optional[str] = None,
    academic_term_id: Optional[str] = None,
    current_user: User = Depends(require_roles(*STAFF_ROLES)),
    session: AsyncSession = Depends(get_session)
):
    """Get all grades for a class. Without academic_term_id, grades from every term
    the class has are returned mixed together — pass it to scope to one term, which
    the teacher gradebook UI always does to avoid mixing terms in an average."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    # Get class info — school_id filter is load-bearing: without it, any
    # staff member at any school could pull another school's entire class
    # roster + every grade by supplying that school's class_id.
    class_result = await session.execute(select(Class).where(Class.id == class_id, Class.school_id == school_id))
    classroom = class_result.scalar_one_or_none()
    if not classroom:
        raise HTTPException(status_code=404, detail="Class not found")

    # Get students in class
    students_result = await session.execute(
        select(Student).where(
            and_(Student.class_id == class_id, Student.school_id == school_id, Student.status == "active")
        ).order_by(Student.last_name, Student.first_name)
    )
    students = students_result.scalars().all()

    # Get grades for these students
    student_ids = [s.id for s in students]
    query = select(Grade).where(Grade.student_id.in_(student_ids))

    if subject_id:
        query = query.where(Grade.subject_id == subject_id)
    if assessment_type:
        query = query.where(Grade.assessment_type == assessment_type)
    if academic_term_id:
        query = query.where(Grade.academic_term_id == academic_term_id)

    grades_result = await session.execute(query)
    grades = grades_result.scalars().all()
    
    # Build grades lookup
    grades_by_student = {}
    for g in grades:
        if g.student_id not in grades_by_student:
            grades_by_student[g.student_id] = []
        grades_by_student[g.student_id].append(g)
    
    # Get subjects
    subject_ids = list(set(g.subject_id for g in grades))
    subject_names = {}
    if subject_ids:
        subj_result = await session.execute(select(Subject).where(Subject.id.in_(subject_ids)))
        for s in subj_result.scalars().all():
            subject_names[s.id] = s.name

    # Resolve once for the whole class instead of once per grade — every
    # grade below shares the same class_level; only the subject varies.
    schemes = await grading_service.get_school_schemes(session, school_id)
    overall_scale = grading_service.match_scale(schemes, classroom.level, subject_id=None)
    subject_weights = grading_service.build_subject_weights(schemes, classroom.level, subject_ids)

    # Build response
    students_data = []
    for student in students:
        student_grades = grades_by_student.get(student.id, [])
        # GES-split (SBA/exam 50/50), weighted average via compute_overall_ges_score
        # — the same function the report-card endpoints below in this file
        # use, so a teacher grading here sees the number that will actually
        # end up on the report card (previously weighted but not GES-split,
        # which could disagree with the report card for the same student).
        avg_percentage = compute_overall_ges_score(student_grades, weights=subject_weights)[1] if student_grades else 0
        letter_grade = get_letter_grade(avg_percentage, scale=overall_scale)

        students_data.append({
            "student_id": student.id,
            "student_name": f"{student.first_name} {student.last_name}",
            "student_number": student.student_id,
            "grades": [
                {
                    "id": g.id,
                    "grade_id": g.id,
                    "subject_id": g.subject_id,
                    "subject_name": subject_names.get(g.subject_id, "Unknown"),
                    "academic_term_id": g.academic_term_id,
                    "assessment_type": g.assessment_type,
                    "score": g.score,
                    "max_score": g.max_score,
                    "weight": g.weight,
                    "percentage": round(g.score / g.max_score * 100, 1),
                    "recorded_at": g.created_at.isoformat(),
                    "letter_grade": get_letter_grade(
                        g.score / g.max_score * 100,
                        scale=grading_service.match_scale(schemes, classroom.level, g.subject_id)
                    )["grade"]
                }
                for g in student_grades
            ],
            "average_percentage": round(avg_percentage, 1),
            "letter_grade": letter_grade["grade"],
            "grade_description": letter_grade["description"]
        })
    
    return {
        "class_id": class_id,
        "class_name": classroom.name,
        "subject_id": subject_id,
        "student_count": len(students),
        "students": students_data
    }


@router.post("/bulk", response_model=dict)
async def record_bulk_grades(
    grades_data: List[GradeCreate],
    current_user: User = Depends(require_permission("academics.grade.create")),
    session: AsyncSession = Depends(get_session)
):
    """Record multiple grades at once (e.g., entire class for an assessment)"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    for grade_data in grades_data:
        validate_grade_score(grade_data.score, grade_data.max_score)

    # Batch-validate referenced ids rather than one query per grade per field —
    # a bulk submission is typically the whole class against one class/subject/term,
    # so this is a handful of .in_() queries instead of up to 4×N.
    if grades_data:
        student_ids = {g.student_id for g in grades_data}
        class_ids = {g.class_id for g in grades_data}
        subject_ids = {g.subject_id for g in grades_data}
        term_ids = {g.academic_term_id for g in grades_data}

        found_students = (await session.execute(
            select(Student.id).where(Student.id.in_(student_ids), Student.school_id == school_id)
        )).scalars().all()
        if set(found_students) != student_ids:
            raise HTTPException(status_code=400, detail="One or more student_id values do not exist for this school")

        found_classes = (await session.execute(
            select(Class.id).where(Class.id.in_(class_ids), Class.school_id == school_id)
        )).scalars().all()
        if set(found_classes) != class_ids:
            raise HTTPException(status_code=400, detail="One or more class_id values do not exist for this school")

        found_subjects = (await session.execute(
            select(Subject.id).where(Subject.id.in_(subject_ids), Subject.school_id == school_id)
        )).scalars().all()
        if set(found_subjects) != subject_ids:
            raise HTTPException(status_code=400, detail="One or more subject_id values do not exist for this school")

        found_terms = (await session.execute(
            select(AcademicTerm).where(AcademicTerm.id.in_(term_ids), AcademicTerm.school_id == school_id)
        )).scalars().all()
        if {t.id for t in found_terms} != term_ids:
            raise HTTPException(status_code=400, detail="One or more academic_term_id values do not exist for this school")
        locked_terms = [t.id for t in found_terms if t.is_locked]
        if locked_terms:
            raise HTTPException(status_code=423, detail=f"One or more academic terms are locked: {', '.join(locked_terms)}")

    # Skip any grade already recorded for the same (student, subject, term, assessment
    # type) instead of erroring the whole batch — a double-click on "Save Grades"
    # (there's no submit-guard in the UI) would otherwise duplicate every grade in
    # the class, or abort the entire commit on the first repeat submission.
    existing_result = await session.execute(
        select(Grade.student_id, Grade.subject_id, Grade.academic_term_id, Grade.assessment_type)
        .where(
            Grade.student_id.in_(student_ids),
            Grade.subject_id.in_(subject_ids),
            Grade.academic_term_id.in_(term_ids),
        )
    ) if grades_data else None
    existing_keys = set(existing_result.all()) if existing_result else set()

    recorded_by = await _recorded_by_id(current_user, session)
    created_count = 0
    skipped_count = 0
    for grade_data in grades_data:
        key = (grade_data.student_id, grade_data.subject_id, grade_data.academic_term_id, grade_data.assessment_type)
        if key in existing_keys:
            skipped_count += 1
            continue
        grade = Grade(
            school_id=school_id,
            recorded_by=recorded_by,
            **grade_data.model_dump()
        )
        session.add(grade)
        existing_keys.add(key)
        created_count += 1

    await session.commit()

    return {
        "message": f"Recorded {created_count} grades successfully"
        + (f", skipped {skipped_count} already-recorded" if skipped_count else ""),
        "count": created_count,
        "skipped": skipped_count
    }


@router.get("/scales", response_model=list[dict])
async def get_grade_scales(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Get grade scales for school"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    result = await session.execute(
        select(GradeScale).where(GradeScale.school_id == school_id).order_by(GradeScale.min_score.desc())
    )
    scales = result.scalars().all()
    
    return [
        {
            "id": s.id,
            "grade": s.grade,
            "min_score": s.min_score,
            "max_score": s.max_score,
            "description": s.description,
            "gpa_point": s.gpa_point
        }
        for s in scales
    ]


# ============ GRADING SCHEMES (multiple configurable grading systems) ============
# A school can define several named GradingScheme rows — e.g. "Primary Grading
# Scale", "JHS Grading Scale", "Sciences Grading Scale" — each scoped to a
# class level and/or subject via GradingScheme.class_level/subject_id (either
# left unset means "every level"/"every subject"). services/grading_service.py
# picks the most specific match at grading time; a school with none configured
# keeps using the built-in GES_GRADE_SCALE exactly as before.

def _validate_scheme_scope(class_level: Optional[str], subject_id: Optional[str], valid_subject_ids: set) -> None:
    if class_level is not None and class_level not in {lvl.value for lvl in ClassLevel}:
        raise HTTPException(status_code=400, detail=f"Invalid class_level: {class_level}")
    if subject_id is not None and subject_id not in valid_subject_ids:
        raise HTTPException(status_code=400, detail=f"Invalid subject_id: {subject_id}")


def _validate_scheme_bands(bands: List) -> None:
    if not bands:
        raise HTTPException(status_code=400, detail="A grading scheme needs at least one grade band")
    for b in bands:
        if b.min_score < 0:
            raise HTTPException(status_code=400, detail=f"Band '{b.grade}': min_score cannot be negative")
        if b.min_score > b.max_score:
            raise HTTPException(status_code=400, detail=f"Band '{b.grade}': min_score cannot exceed max_score")
    # utils/grade_scale.py::get_letter_grade resolves a score to "the highest
    # band whose min_score it clears" — so the only way a real coverage gap
    # can exist is if nothing starts at 0: a score below the lowest band's
    # min_score would clear no band at all and silently fall back to the
    # scale's last entry, exactly the failure mode this validation exists to
    # rule out at configuration time rather than discovering it on a report
    # card later.
    if min(b.min_score for b in bands) > 0:
        raise HTTPException(
            status_code=400,
            detail="This scheme's bands must cover every score down to 0 — the lowest band's min_score should be 0",
        )


def _validate_scheme_weights(ca_weight: float, exam_weight: float) -> None:
    if ca_weight < 0 or exam_weight < 0:
        raise HTTPException(status_code=400, detail="ca_weight and exam_weight cannot be negative")
    if round(ca_weight + exam_weight, 2) != 100:
        raise HTTPException(
            status_code=400,
            detail=f"ca_weight and exam_weight must sum to 100 (got {ca_weight} + {exam_weight} = {ca_weight + exam_weight})",
        )


def _scheme_response(scheme: GradingScheme, bands: List[GradeScale]) -> dict:
    return {
        "id": scheme.id,
        "name": scheme.name,
        "class_level": scheme.class_level,
        "subject_id": scheme.subject_id,
        "is_active": scheme.is_active,
        "bands": [
            {"id": b.id, "grade": b.grade, "min_score": b.min_score, "max_score": b.max_score,
             "description": b.description, "gpa_point": b.gpa_point}
            for b in sorted(bands, key=lambda b: b.min_score, reverse=True)
        ],
    }


@router.get("/grading-schemes", response_model=List[dict])
async def list_grading_schemes(
    current_user: User = Depends(get_current_user),
    _plan_check: User = Depends(require_plan_feature("academic_reports")),
    session: AsyncSession = Depends(get_session),
):
    """List every grading scheme configured for this school, each with its bands."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    schemes = (await session.execute(
        select(GradingScheme).where(GradingScheme.school_id == school_id).order_by(GradingScheme.name)
    )).scalars().all()
    if not schemes:
        return []

    scheme_ids = [s.id for s in schemes]
    bands = (await session.execute(
        select(GradeScale).where(GradeScale.scheme_id.in_(scheme_ids))
    )).scalars().all()
    bands_by_scheme: dict = {}
    for b in bands:
        bands_by_scheme.setdefault(b.scheme_id, []).append(b)

    return [_scheme_response(s, bands_by_scheme.get(s.id, [])) for s in schemes]


@router.post("/grading-schemes", response_model=dict)
async def create_grading_scheme(
    body: CreateGradingSchemeRequest,
    current_user: User = Depends(require_permission("academics.grading_scheme.manage")),
    _plan_check: User = Depends(require_plan_feature("academic_reports")),
    session: AsyncSession = Depends(get_session),
):
    """Create a new grading scheme (e.g. one for JHS, one for a department/
    subject) with its full set of grade bands in one call."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    valid_subject_ids = set((await session.execute(
        select(Subject.id).where(Subject.school_id == school_id)
    )).scalars().all())
    _validate_scheme_scope(body.class_level, body.subject_id, valid_subject_ids)
    _validate_scheme_bands(body.bands)
    _validate_scheme_weights(body.ca_weight, body.exam_weight)

    # Resolution is ambiguous if two active schemes share the same scope —
    # block that at creation time rather than silently picking one later.
    existing = (await session.execute(
        select(GradingScheme).where(
            GradingScheme.school_id == school_id,
            GradingScheme.class_level == body.class_level,
            GradingScheme.subject_id == body.subject_id,
            GradingScheme.is_active == True,
        )
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=400,
            detail=f"An active grading scheme ('{existing.name}') already covers this class level/subject combination",
        )

    scheme = GradingScheme(
        school_id=school_id, name=body.name, class_level=body.class_level, subject_id=body.subject_id,
        ca_weight=body.ca_weight, exam_weight=body.exam_weight,
    )
    session.add(scheme)
    await session.flush()

    bands = [
        GradeScale(
            school_id=school_id, scheme_id=scheme.id, grade=b.grade,
            min_score=b.min_score, max_score=b.max_score, description=b.description, gpa_point=b.gpa_point,
        )
        for b in body.bands
    ]
    session.add_all(bands)
    await session.commit()

    await log_event(
        session, actor=current_user, action="grading_scheme.created", entity_type="grading_scheme",
        entity_id=scheme.id, school_id=school_id,
        summary=f"{current_user.email} created grading scheme '{scheme.name}'",
    )

    return _scheme_response(scheme, bands)


@router.put("/grading-schemes/{scheme_id}", response_model=dict)
async def update_grading_scheme(
    scheme_id: str,
    body: UpdateGradingSchemeRequest,
    current_user: User = Depends(require_permission("academics.grading_scheme.manage")),
    _plan_check: User = Depends(require_plan_feature("academic_reports")),
    session: AsyncSession = Depends(get_session),
):
    """Update a grading scheme's name/scope/active flag, and — if `bands` is
    supplied — replace its entire set of grade bands."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    scheme = await session.get(GradingScheme, scheme_id)
    if not scheme or scheme.school_id != school_id:
        raise HTTPException(status_code=404, detail="Grading scheme not found")

    new_class_level = body.class_level if body.class_level is not None else scheme.class_level
    new_subject_id = body.subject_id if body.subject_id is not None else scheme.subject_id
    new_is_active = body.is_active if body.is_active is not None else scheme.is_active

    valid_subject_ids = set((await session.execute(
        select(Subject.id).where(Subject.school_id == school_id)
    )).scalars().all())
    _validate_scheme_scope(new_class_level, new_subject_id, valid_subject_ids)

    if new_is_active:
        existing = (await session.execute(
            select(GradingScheme).where(
                GradingScheme.school_id == school_id,
                GradingScheme.class_level == new_class_level,
                GradingScheme.subject_id == new_subject_id,
                GradingScheme.is_active == True,
                GradingScheme.id != scheme_id,
            )
        )).scalar_one_or_none()
        if existing:
            raise HTTPException(
                status_code=400,
                detail=f"An active grading scheme ('{existing.name}') already covers this class level/subject combination",
            )

    new_ca_weight = body.ca_weight if body.ca_weight is not None else scheme.ca_weight
    new_exam_weight = body.exam_weight if body.exam_weight is not None else scheme.exam_weight
    if body.ca_weight is not None or body.exam_weight is not None:
        _validate_scheme_weights(new_ca_weight, new_exam_weight)

    if body.name is not None:
        scheme.name = body.name
    scheme.class_level = new_class_level
    scheme.subject_id = new_subject_id
    scheme.ca_weight = new_ca_weight
    scheme.exam_weight = new_exam_weight
    scheme.is_active = new_is_active
    scheme.updated_at = datetime.utcnow()
    session.add(scheme)

    if body.bands is not None:
        _validate_scheme_bands(body.bands)
        existing_bands = (await session.execute(
            select(GradeScale).where(GradeScale.scheme_id == scheme_id)
        )).scalars().all()
        for b in existing_bands:
            await session.delete(b)
        await session.flush()
        new_bands = [
            GradeScale(
                school_id=school_id, scheme_id=scheme.id, grade=b.grade,
                min_score=b.min_score, max_score=b.max_score, description=b.description, gpa_point=b.gpa_point,
            )
            for b in body.bands
        ]
        session.add_all(new_bands)

    await session.commit()

    bands = (await session.execute(select(GradeScale).where(GradeScale.scheme_id == scheme_id))).scalars().all()
    return _scheme_response(scheme, bands)


@router.delete("/grading-schemes/{scheme_id}", response_model=dict)
async def delete_grading_scheme(
    scheme_id: str,
    current_user: User = Depends(require_permission("academics.grading_scheme.manage")),
    _plan_check: User = Depends(require_plan_feature("academic_reports")),
    session: AsyncSession = Depends(get_session),
):
    """Delete a grading scheme and its bands. Grades already recorded/graded
    under it are unaffected — the letter grade shown was computed at the time,
    not stored as a live reference to the scheme."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    scheme = await session.get(GradingScheme, scheme_id)
    if not scheme or scheme.school_id != school_id:
        raise HTTPException(status_code=404, detail="Grading scheme not found")

    bands = (await session.execute(select(GradeScale).where(GradeScale.scheme_id == scheme_id))).scalars().all()
    for b in bands:
        await session.delete(b)
    await session.delete(scheme)
    await session.commit()

    await log_event(
        session, actor=current_user, action="grading_scheme.deleted", entity_type="grading_scheme",
        entity_id=scheme_id, school_id=school_id,
        summary=f"{current_user.email} deleted grading scheme '{scheme.name}'",
    )

    return {"message": "Grading scheme deleted"}


@router.post("/report-cards/generate", response_model=dict)
async def generate_report_card(
    body: GenerateReportCardRequest,
    current_user: User = Depends(require_permission("academics.report_card.generate")),
    _plan_check: User = Depends(require_plan_feature("academic_reports")),
    session: AsyncSession = Depends(get_session)
):
    """Generate a report card for a student"""
    student_id = body.student_id
    academic_term_id = body.academic_term_id
    class_teacher_remarks = body.class_teacher_remarks
    head_teacher_remarks = body.head_teacher_remarks
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    # school_id scoped, without which a School A teacher could generate a
    # report card for a School B student -- pulling and returning their real
    # grades/attendance (a cross-tenant data leak) and persisting a
    # ReportCard row mixing School A's school_id with a foreign student.
    # Every sibling report-card endpoint in this file (get/preview/download)
    # already checks this via _has_report_card_access; this one hadn't.
    student_result = await session.execute(select(Student).where(Student.id == student_id, Student.school_id == school_id))
    student = student_result.scalar_one_or_none()

    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    if not await _has_report_card_access(session, current_user, student):
        raise HTTPException(status_code=403, detail="Access denied")

    # Get grades
    grades_result = await session.execute(
        select(Grade).where(
            Grade.student_id == student_id,
            Grade.academic_term_id == academic_term_id
        )
    )
    grades = grades_result.scalars().all()

    # Resolve each subject's CA:exam split from the school's configured
    # GradingScheme(s) (falls back to 50/50 for any subject with nothing
    # configured) — the same split the PDF/preview renders below, so the
    # total/average shown here always matches the downloaded report card.
    # Also used just below to check whether this average needs a promotion
    # review flag, so resolved once here rather than via the shared
    # _resolve_subject_weights helper (which doesn't expose class_level/
    # schemes back to the caller).
    student_class = await session.get(Class, student.class_id) if student.class_id else None
    class_level = student_class.level if student_class else None
    schemes = await grading_service.get_school_schemes(session, school_id)
    subject_weights = grading_service.build_subject_weights(schemes, class_level, {g.subject_id for g in grades})
    total_score, average_score = compute_overall_ges_score(grades, weights=subject_weights)

    # A subject with grades on only one side of the SBA/exam split (e.g. the
    # end-of-term exam hasn't been entered yet) has its total_score capped
    # near the graded half's weight above rather than reflecting only the
    # half that's actually graded (see compute_subject_ges_totals's
    # data_complete comment) — flag it here so whoever generates the report
    # card notices before approving it, rather than a parent seeing a
    # misleadingly low score.
    incomplete_subject_ids = [sid for sid, t in compute_subject_ges_totals(grades, weights=subject_weights).items() if not t["data_complete"]]
    incomplete_subjects = []
    if incomplete_subject_ids:
        subjects_result = await session.execute(select(Subject).where(Subject.id.in_(incomplete_subject_ids)))
        subject_name_by_id = {s.id: s.name for s in subjects_result.scalars().all()}
        incomplete_subjects = [
            {"subject_id": sid, "subject_name": subject_name_by_id.get(sid, "Unknown")}
            for sid in incomplete_subject_ids
        ]

    # Get class size
    class_count_result = await session.execute(
        select(func.count(Student.id)).where(Student.class_id == student.class_id, Student.status == "active")
    )
    class_size = class_count_result.scalar() or 0
    
    # Get attendance percentage for this term only
    attendance_result = await session.execute(
        select(Attendance).where(
            Attendance.student_id == student_id,
            Attendance.academic_term_id == academic_term_id
        )
    )
    attendance_records = attendance_result.scalars().all()
    total_days = len(attendance_records)
    present_days = sum(1 for a in attendance_records if a.status in [AttendanceStatus.PRESENT, AttendanceStatus.LATE])
    attendance_percentage = round(present_days / total_days * 100, 1) if total_days > 0 else 0

    rankings, class_average = await compute_class_rankings(session, student.class_id, academic_term_id)
    position = rankings.get(student_id)

    # Promotion-decision review flag: year-rollover (routers/academic_calendar.py)
    # defaults an unset promotion_decision to PROMOTED and only surfaces that
    # after the fact, at rollover time, via students_defaulted_without_decision
    # — by then the school year is already ending. This gives the same signal
    # much earlier, right when the report card is generated: if the student's
    # overall grade falls in the scale's lowest/fail band (generic across any
    # school-configured scheme, not just the built-in GES one — the worst
    # band is always whichever has the lowest min_score) and nobody has
    # recorded a promotion_decision for them yet, flag it so a human notices
    # before the term ends instead of only at rollover, when it's too late to
    # act on for this student's actual performance this year.
    overall_scale = grading_service.match_scale(schemes, class_level, subject_id=None)
    overall_letter = get_letter_grade(average_score, scale=overall_scale)
    is_lowest_band = overall_letter["min_score"] == min(band["min_score"] for band in overall_scale)
    requires_promotion_review = is_lowest_band and not body.promotion_decision

    # Create or update the report card for this student/term (upsert — this
    # endpoint may be called more than once, e.g. remarks were edited and
    # re-submitted before preview/download).
    existing_result = await session.execute(
        select(ReportCard).where(
            ReportCard.student_id == student_id,
            ReportCard.academic_term_id == academic_term_id
        )
    )
    report_card = existing_result.scalars().first()

    field_values = dict(
        school_id=school_id,
        student_id=student_id,
        class_id=student.class_id,
        academic_term_id=academic_term_id,
        total_score=total_score,
        average_score=average_score,
        position=position,
        class_size=class_size,
        attendance_percentage=attendance_percentage,
        days_present=present_days,
        days_total=total_days,
        class_teacher_remarks=class_teacher_remarks,
        head_teacher_remarks=head_teacher_remarks,
        attitude=body.attitude,
        conduct=body.conduct,
        interest=body.interest,
        vacation_date=body.vacation_date,
        reopening_date=body.reopening_date,
        promoted_to=body.promoted_to,
        promotion_decision=body.promotion_decision,
        generated_by=current_user.id
    )

    if report_card:
        for field, value in field_values.items():
            setattr(report_card, field, value)
        # Content changed — any prior head-teacher sign-off no longer covers
        # this version, so it goes back to DRAFT and needs re-approval.
        report_card.status = ReportCardStatus.DRAFT.value
        report_card.approved_by = None
        report_card.approved_at = None
    else:
        report_card = ReportCard(**field_values)
        session.add(report_card)

    try:
        await session.commit()
    except IntegrityError:
        # Two concurrent generate-report-card calls for the same student+term
        # both saw "no existing row" and both tried to insert — the
        # uq_report_cards_student_term constraint catches the loser here
        # instead of leaving a duplicate row that would crash every later
        # single-row lookup with MultipleResultsFound.
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="This report card was just generated by another request — reload and try again",
        )
    await session.refresh(report_card)

    await log_event(
        session, actor=current_user, action="grade.report_card_generated", entity_type="report_card",
        entity_id=report_card.id, school_id=school_id,
        summary=f"{current_user.email} generated a report card for student {student_id}",
    )

    return {
        "id": report_card.id,
        "student_id": student_id,
        "student_name": f"{student.first_name} {student.last_name}",
        "total_score": round(total_score, 1),
        "average_score": round(average_score, 1),
        "class_average": class_average,
        "class_size": class_size,
        "attendance_percentage": attendance_percentage,
        "status": report_card.status,
        "incomplete_subjects": incomplete_subjects,
        # True when the overall grade falls in the scale's lowest/fail band
        # and no promotion_decision has been recorded yet for this report
        # card — a signal to record one deliberately (promoted/repeated/
        # graduated) rather than letting rollover silently default this
        # student to "promoted" months from now.
        "requires_promotion_review": requires_promotion_review,
        "message": "Report card generated as draft — pending approval before it's visible to parents/students"
    }


async def _notify_parent_report_card_status(
    session: AsyncSession,
    student: Student,
    current_user: User,
    background_tasks: BackgroundTasks,
    sms_message: str,
    in_app_subject: str,
    in_app_content: str,
    notification_type: str,
) -> None:
    """Report-card-flavored thin wrapper around the shared
    services.parent_notification_service.notify_parent — kept here so every
    existing call site in this file stays unchanged."""
    await parent_notification_service.notify_parent(
        session, student, current_user, background_tasks,
        sms_message=sms_message, in_app_subject=in_app_subject, in_app_content=in_app_content,
        notification_type=notification_type, message_type=MessageType.GRADE,
    )


@router.post("/report-cards/{student_id}/{academic_term_id}/approve", response_model=dict)
async def approve_report_card(
    student_id: str,
    academic_term_id: str,
    background_tasks: BackgroundTasks,
    override_fee_hold: bool = Query(False, description="Approve anyway despite an outstanding fee balance, when the school has hold_report_cards_for_fee_defaulters enabled"),
    current_user: User = Depends(require_permission("academics.report_card.approve")),
    _plan_check: User = Depends(require_plan_feature("academic_reports")),
    session: AsyncSession = Depends(get_session)
):
    """Head-teacher sign-off (there's no separate head_teacher role in this
    system, so school admins stand in for it): a DRAFT report card only
    becomes visible to its student's parent/student account once approved
    here. Teachers can generate/edit drafts but cannot approve their own
    output — approval is deliberately restricted to admin roles.

    If this report card was previously recalled for correction (see
    recall_report_card below) and hasn't been resent yet, approving it here
    also stamps that recall as resent and notifies the parent a corrected
    version is now available — first-time approvals stay silent, only
    corrections trigger a notification.
    """
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    result = await session.execute(
        select(ReportCard).where(
            ReportCard.student_id == student_id,
            ReportCard.academic_term_id == academic_term_id
        )
    )
    report_card = result.scalar_one_or_none()

    if not report_card:
        raise HTTPException(status_code=404, detail="Report card not found")
    if report_card.school_id != school_id:
        raise HTTPException(status_code=403, detail="Access denied")
    if report_card.status == ReportCardStatus.APPROVED:
        raise HTTPException(status_code=409, detail="Report card is already approved")

    # Fee-balance hold — opt-in, mirrors _compute_exit_clearance's outstanding
    # balance math (routers/students.py) but only gates the sign-off step,
    # not draft generation/editing, and is per-card overridable.
    if not override_fee_hold:
        school_result = await session.execute(select(School).where(School.id == school_id))
        school = school_result.scalar_one_or_none()
        if school and school.hold_report_cards_for_fee_defaulters:
            fee_result = await session.execute(
                select(Fee).where(Fee.school_id == school_id, Fee.student_id == student_id)
            )
            outstanding_balance = round(
                sum(f.amount_due - f.discount - f.amount_paid for f in fee_result.scalars().all()), 2
            )
            if outstanding_balance > 0:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Student has an outstanding fee balance of {outstanding_balance:.2f} — "
                        "report card approval is on hold. Pass override_fee_hold=true to approve anyway."
                    ),
                )

    report_card.status = ReportCardStatus.APPROVED.value
    report_card.approved_by = current_user.id
    report_card.approved_at = datetime.utcnow()
    session.add(report_card)
    await session.commit()
    await session.refresh(report_card)

    await log_event(
        session, actor=current_user, action="grade.report_card_approved", entity_type="report_card",
        entity_id=report_card.id, school_id=school_id,
        summary=f"{current_user.email} approved the report card for student {student_id}",
    )

    open_recall = (await session.execute(
        select(ReportCardRecall).where(
            ReportCardRecall.report_card_id == report_card.id,
            ReportCardRecall.resent_at.is_(None),
        ).order_by(ReportCardRecall.recalled_at.desc())
    )).scalars().first()

    resent = False
    if open_recall:
        open_recall.resent_at = datetime.utcnow()
        open_recall.resent_by = current_user.id
        session.add(open_recall)
        await session.commit()
        resent = True

        student = await session.get(Student, student_id)
        if student:
            await _notify_parent_report_card_status(
                session, student, current_user, background_tasks,
                sms_message=f"Update: the report card for {student.first_name} {student.last_name} has been corrected and is now available. Please check the school portal. -School",
                in_app_subject="Corrected Report Card Available",
                in_app_content=f"The report card for {student.first_name} {student.last_name} has been corrected and is now available to view.",
                notification_type="report_card_resent",
            )

    return {
        "id": report_card.id,
        "student_id": student_id,
        "status": report_card.status,
        "approved_by": report_card.approved_by,
        "approved_at": report_card.approved_at.isoformat(),
        "resent_after_recall": resent,
        "message": (
            "Corrected report card approved, and the parent has been notified"
            if resent else
            "Report card approved and now visible to the student/parent"
        )
    }


@router.post("/report-cards/{student_id}/{academic_term_id}/recall", response_model=dict)
async def recall_report_card(
    student_id: str,
    academic_term_id: str,
    body: RecallReportCardRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(require_permission("academics.report_card.recall")),
    _plan_check: User = Depends(require_plan_feature("academic_reports")),
    session: AsyncSession = Depends(get_session)
):
    """Call back an already-approved (parent-visible) report card for
    correction: pulls it back to DRAFT — immediately hiding it from the
    parent/student — and notifies the parent it's being corrected, without
    requiring the admin to resubmit the whole report first (unlike
    generate_report_card's implicit DRAFT reversion). A reason is required
    so both the recall history and the parent notification say *why*.
    Re-approve via the endpoint above once corrected; that step notifies the
    parent again that the corrected version is ready."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    if not body.reason or not body.reason.strip():
        raise HTTPException(status_code=400, detail="A reason is required to recall a report card")

    result = await session.execute(
        select(ReportCard).where(
            ReportCard.student_id == student_id,
            ReportCard.academic_term_id == academic_term_id
        )
    )
    report_card = result.scalar_one_or_none()

    if not report_card:
        raise HTTPException(status_code=404, detail="Report card not found")
    if report_card.school_id != school_id:
        raise HTTPException(status_code=403, detail="Access denied")
    if report_card.status != ReportCardStatus.APPROVED:
        raise HTTPException(status_code=400, detail="Only an approved (parent-visible) report card can be recalled")

    recall = ReportCardRecall(
        school_id=school_id,
        report_card_id=report_card.id,
        student_id=student_id,
        academic_term_id=academic_term_id,
        reason=body.reason.strip(),
        recalled_by=current_user.id,
        snapshot_total_score=report_card.total_score,
        snapshot_average_score=report_card.average_score,
        snapshot_class_teacher_remarks=report_card.class_teacher_remarks,
        snapshot_head_teacher_remarks=report_card.head_teacher_remarks,
    )
    session.add(recall)

    report_card.status = ReportCardStatus.DRAFT.value
    report_card.approved_by = None
    report_card.approved_at = None
    session.add(report_card)
    await session.commit()
    await session.refresh(recall)

    await log_event(
        session, actor=current_user, action="grade.report_card_recalled", entity_type="report_card",
        entity_id=report_card.id, school_id=school_id,
        summary=f"{current_user.email} recalled the report card for student {student_id}: {recall.reason}",
    )

    student = await session.get(Student, student_id)
    if student:
        await _notify_parent_report_card_status(
            session, student, current_user, background_tasks,
            sms_message=f"Notice: the report card for {student.first_name} {student.last_name} has been withdrawn for correction. An updated version will be available soon. -School",
            in_app_subject="Report Card Withdrawn for Correction",
            in_app_content=f"The report card for {student.first_name} {student.last_name} has been withdrawn for correction. Reason: {recall.reason}",
            notification_type="report_card_recalled",
        )

    return {
        "id": report_card.id,
        "student_id": student_id,
        "status": report_card.status,
        "recall_id": recall.id,
        "reason": recall.reason,
        "recalled_at": recall.recalled_at.isoformat(),
        "message": "Report card recalled — hidden from the parent/student and marked for correction"
    }


@router.get("/report-cards/{student_id}/{academic_term_id}/recall-history", response_model=List[dict])
async def get_report_card_recall_history(
    student_id: str,
    academic_term_id: str,
    current_user: User = Depends(require_permission("academics.report_card.generate")),
    _plan_check: User = Depends(require_plan_feature("academic_reports")),
    session: AsyncSession = Depends(get_session)
):
    """Every recall (and, if resent, resend) recorded for this student's
    report card in this term, most recent first — lets an admin see what
    was corrected and when without digging through the generic audit log."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    recalls = (await session.execute(
        select(ReportCardRecall).where(
            ReportCardRecall.student_id == student_id,
            ReportCardRecall.academic_term_id == academic_term_id,
            ReportCardRecall.school_id == school_id,
        ).order_by(ReportCardRecall.recalled_at.desc())
    )).scalars().all()

    return [
        {
            "id": r.id,
            "reason": r.reason,
            "recalled_by": r.recalled_by,
            "recalled_at": r.recalled_at.isoformat(),
            "snapshot_total_score": r.snapshot_total_score,
            "snapshot_average_score": r.snapshot_average_score,
            "resent_at": r.resent_at.isoformat() if r.resent_at else None,
            "resent_by": r.resent_by,
        }
        for r in recalls
    ]


@router.get("/report-cards/recalls", response_model=List[dict])
async def list_report_card_recalls(
    resolved: Optional[bool] = Query(None, description="true = already corrected and resent; false = still outstanding; omit for both"),
    current_user: User = Depends(require_permission("academics.report_card.recall")),
    _plan_check: User = Depends(require_plan_feature("academic_reports")),
    session: AsyncSession = Depends(get_session)
):
    """School-wide list of every report-card recall, most recent first — the
    admin-facing 'what's been called back for correction' view, as opposed
    to get_report_card_recall_history's one-student-at-a-time history."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    query = select(ReportCardRecall).where(ReportCardRecall.school_id == school_id)
    if resolved is True:
        query = query.where(ReportCardRecall.resent_at.is_not(None))
    elif resolved is False:
        query = query.where(ReportCardRecall.resent_at.is_(None))
    query = query.order_by(ReportCardRecall.recalled_at.desc())

    recalls = (await session.execute(query)).scalars().all()
    if not recalls:
        return []

    student_ids = {r.student_id for r in recalls}
    term_ids = {r.academic_term_id for r in recalls}
    report_card_ids = {r.report_card_id for r in recalls}
    user_ids = {r.recalled_by for r in recalls} | {r.resent_by for r in recalls if r.resent_by}

    students = {s.id: s for s in (await session.execute(select(Student).where(Student.id.in_(student_ids)))).scalars().all()}
    terms = {t.id: t for t in (await session.execute(select(AcademicTerm).where(AcademicTerm.id.in_(term_ids)))).scalars().all()}
    report_cards = {rc.id: rc for rc in (await session.execute(select(ReportCard).where(ReportCard.id.in_(report_card_ids)))).scalars().all()}
    users = {}
    if user_ids:
        users = {u.id: u for u in (await session.execute(select(User).where(User.id.in_(user_ids)))).scalars().all()}

    def user_name(user_id: Optional[str]) -> Optional[str]:
        if not user_id:
            return None
        u = users.get(user_id)
        return f"{u.first_name} {u.last_name}" if u else None

    def term_label(term_id: str) -> str:
        t = terms.get(term_id)
        if not t:
            return "Unknown Term"
        term_name = t.term.value if hasattr(t.term, "value") else str(t.term)
        return f"{t.academic_year} — {term_name.capitalize()} Term"

    result = []
    for r in recalls:
        student = students.get(r.student_id)
        report_card = report_cards.get(r.report_card_id)
        result.append({
            "id": r.id,
            "report_card_id": r.report_card_id,
            "student_id": r.student_id,
            "student_name": f"{student.first_name} {student.last_name}" if student else "Unknown",
            "class_id": student.class_id if student else None,
            "academic_term_id": r.academic_term_id,
            "term_label": term_label(r.academic_term_id),
            "reason": r.reason,
            "recalled_by": r.recalled_by,
            "recalled_by_name": user_name(r.recalled_by),
            "recalled_at": r.recalled_at.isoformat(),
            "current_status": report_card.status if report_card else None,
            "resent_at": r.resent_at.isoformat() if r.resent_at else None,
            "resent_by": r.resent_by,
            "resent_by_name": user_name(r.resent_by),
        })
    return result


async def _has_report_card_access(session: AsyncSession, current_user: User, student: Student) -> bool:
    """Ownership rules shared by the get/preview/download report-card
    endpoints: admins and teachers see any student in their school; parents
    only their own children; students only themselves."""
    if current_user.role == UserRole.SUPER_ADMIN:
        return True
    if current_user.role in (UserRole.SCHOOL_ADMIN, UserRole.TEACHER):
        return current_user.school_id == student.school_id
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


def _report_card_visible_to_viewer(current_user: User, report_card: ReportCard) -> bool:
    """Parents/students only ever see an approved report card — a DRAFT
    (unapproved) card is invisible to them even if they'd otherwise have
    ownership access; staff can still see drafts to review/edit them."""
    if current_user.role in (UserRole.PARENT, UserRole.STUDENT):
        return report_card.status == ReportCardStatus.APPROVED
    return True


@router.get("/report-cards/{student_id}/{academic_term_id}", response_model=dict)
async def get_report_card(
    student_id: str,
    academic_term_id: str,
    current_user: User = Depends(get_current_user),
    _plan_check: User = Depends(require_plan_feature("academic_reports")),
    session: AsyncSession = Depends(get_session)
):
    """Get a student's report card"""
    student_result = await session.execute(select(Student).where(Student.id == student_id))
    student = student_result.scalar_one_or_none()

    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    if not await _has_report_card_access(session, current_user, student):
        raise HTTPException(status_code=403, detail="Access denied")

    result = await session.execute(
        select(ReportCard).where(
            ReportCard.student_id == student_id,
            ReportCard.academic_term_id == academic_term_id
        )
    )
    report_card = result.scalar_one_or_none()

    if not report_card:
        raise HTTPException(status_code=404, detail="Report card not found")

    if not _report_card_visible_to_viewer(current_user, report_card):
        raise HTTPException(status_code=403, detail="Report card has not been approved for release yet")

    return {
        "id": report_card.id,
        "student_id": student_id,
        "student_name": f"{student.first_name} {student.last_name}",
        "total_score": report_card.total_score,
        "average_score": report_card.average_score,
        "position": report_card.position,
        "class_size": report_card.class_size,
        "attendance_percentage": report_card.attendance_percentage,
        "class_teacher_remarks": report_card.class_teacher_remarks,
        "head_teacher_remarks": report_card.head_teacher_remarks,
        "status": report_card.status,
        "generated_at": report_card.generated_at.isoformat()
    }


@router.get("/report-cards/{student_id}/{academic_term_id}/preview")
async def preview_report_card_html(
    student_id: str,
    academic_term_id: str,
    current_user: User = Depends(get_current_user),
    _plan_check: User = Depends(require_plan_feature("academic_reports")),
    session: AsyncSession = Depends(get_session)
):
    """Preview a student's report card as HTML (for modal display)"""
    try:
        # Verify student access
        student_result = await session.execute(select(Student).where(Student.id == student_id))
        student = student_result.scalar_one_or_none()
        
        if not student:
            raise HTTPException(status_code=404, detail="Student not found")
        
        # Check access control based on user role
        if not await _has_report_card_access(session, current_user, student):
            raise HTTPException(status_code=403, detail="Access denied")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Access control error in preview_report_card_html: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Access control error: {str(e)}")
    
    # Get grades first (may be empty - that's ok for preview)
    grades_result = await session.execute(
        select(Grade).where(
            Grade.student_id == student_id,
            Grade.academic_term_id == academic_term_id
        )
    )
    grades = grades_result.scalars().all()
    
    # Allow preview even without grades - show empty/template report card
    
    # Get report card OR generate on demand if not found
    report_card_result = await session.execute(
        select(ReportCard).where(
            ReportCard.student_id == student_id,
            ReportCard.academic_term_id == academic_term_id
        )
    )
    report_card = report_card_result.scalar_one_or_none()

    if not report_card and current_user.role in (UserRole.PARENT, UserRole.STUDENT):
        # Don't auto-generate a report card under a parent/student's own
        # visit — that would create a DRAFT record "generated_by" someone
        # who isn't staff, and one they still can't see until it's approved.
        raise HTTPException(status_code=404, detail="Report card not yet available")

    # Needed for the format_grade_data call below regardless of whether the
    # report card already existed or gets auto-generated just below.
    rankings, class_average = await compute_class_rankings(session, student.class_id, academic_term_id)

    # Auto-generate report card if it doesn't exist
    if not report_card:
        # Same CA:exam split the PDF renders, so the number shown here and
        # the number on the downloaded PDF always agree.
        subject_weights = await _resolve_subject_weights(session, student.school_id, student.class_id, grades)
        total_score, average_score = compute_overall_ges_score(grades, weights=subject_weights)

        # Get class size
        class_count_result = await session.execute(
            select(func.count(Student.id)).where(Student.class_id == student.class_id, Student.status == "active")
        )
        class_size = class_count_result.scalar() or 0

        # Get attendance percentage for this term only
        attendance_result = await session.execute(
            select(Attendance).where(
                Attendance.student_id == student_id,
                Attendance.academic_term_id == academic_term_id
            )
        )
        attendance_records = attendance_result.scalars().all()
        total_days = len(attendance_records)
        present_days = sum(1 for a in attendance_records if a.status in [AttendanceStatus.PRESENT, AttendanceStatus.LATE])
        attendance_percentage = round(present_days / total_days * 100, 1) if total_days > 0 else 0

        # Create report card on the fly
        report_card = ReportCard(
            school_id=student.school_id,
            student_id=student_id,
            class_id=student.class_id,
            academic_term_id=academic_term_id,
            total_score=total_score,
            average_score=average_score,
            position=rankings.get(student_id),
            class_size=class_size,
            attendance_percentage=attendance_percentage,
            generated_by=current_user.id
        )
        session.add(report_card)
        try:
            await session.commit()
        except IntegrityError:
            # Another concurrent preview request for the same student+term
            # won the race and already created it — this is a passive
            # "show me the report card" view, not an explicit submit, so
            # recover by using the one that now exists instead of erroring.
            await session.rollback()
            report_card_result = await session.execute(
                select(ReportCard).where(
                    ReportCard.student_id == student_id,
                    ReportCard.academic_term_id == academic_term_id,
                )
            )
            report_card = report_card_result.scalar_one()
        else:
            await session.refresh(report_card)

    if not _report_card_visible_to_viewer(current_user, report_card):
        raise HTTPException(status_code=403, detail="Report card has not been approved for release yet")

    # Get subjects
    subject_ids = list(set(g.subject_id for g in grades))
    subject_result = await session.execute(
        select(Subject).where(Subject.id.in_(subject_ids))
    )
    subjects = {s.id: s for s in subject_result.scalars().all()}
    
    # Get academic term name
    academic_term_result = await session.execute(
        select(AcademicTerm).where(AcademicTerm.id == academic_term_id)
    )
    academic_term = academic_term_result.scalar_one_or_none()
    # term_name = academic_term.id if academic_term else f"Term {academic_term_id}"
    
    # Get class name
    class_name = "Not Assigned"
    class_obj = None
    if student.class_id:
        class_result = await session.execute(select(Class).where(Class.id == student.class_id))
        class_obj = class_result.scalar_one_or_none()
        if class_obj:
            class_name = class_obj.name

    # Get school name
    school_result = await session.execute(select(School).where(School.id == student.school_id))
    school = school_result.scalar_one_or_none()
    school_name = school.name if school else "School Name"

    # Create student data object with all required fields
    student_data = {
        "id": student.id,
        "student_id": student.student_id,
        "first_name": student.first_name,
        "last_name": student.last_name,
        "school_id": student.school_id,
        "class_id": student.class_id,
        "class_name": class_name,
        "school_name": school_name,
    }

    grading_schemes = await grading_service.get_school_schemes(session, student.school_id)

    # Format data for rendering
    report_data = ReportCardPDFService.format_grade_data(
        report_card=report_card,
        grades=grades,
        subjects_map=subjects,
        student=student_data,
        academic_term_name=None,
        class_average=class_average,
        grading_schemes=grading_schemes,
        class_level=class_obj.level if class_obj else None,
    )
    
    # Fetch school's custom template (or use file fallback)
    custom_template = await get_school_template(session, student.school_id)
    pdf_service = ReportCardPDFService()
    try:
        html_content = pdf_service.render_html(report_data, template_html=custom_template)
    except Exception as e:
        error_msg = str(e)
        raise HTTPException(status_code=500, detail=f"Failed to render report card: {error_msg}")
    
    # Return HTML response with report data
    return {
        "html": html_content,
        "student_name": f"{student.first_name} {student.last_name}",
        "academic_term": None,
        "can_download": True,
        "report_card_id": report_card.id,
        "status": report_card.status,
        "report_data": report_data,
        "subjects": report_data.get("subjects", []),
        "overall_average": report_data.get("overall_average", 0),
        "overall_grade": report_data.get("overall_grade", "N/A"),
        "overall_description": report_data.get("overall_description", "")
    }


async def _compute_report_preview_data(
    student_id: str, academic_term_id: str, current_user: User, session: AsyncSession,
):
    """Read-only version of preview_report_card_html's data computation —
    same access control and same stats, but never writes a ReportCard row.
    Used by the free and AI remarks-suggestion endpoints below."""
    student_result = await session.execute(select(Student).where(Student.id == student_id))
    student = student_result.scalar_one_or_none()

    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    try:
        # Check access control based on user role
        has_access = False

        if current_user.role == UserRole.SUPER_ADMIN or current_user.role == UserRole.SCHOOL_ADMIN:
            # Admins can view any student in their school
            has_access = current_user.school_id == student.school_id
        elif current_user.role == UserRole.TEACHER:
            # Teachers can view students they teach
            has_access = current_user.school_id == student.school_id
        elif current_user.role == UserRole.PARENT:
            # Parents can only view their own children
            parent_result = await session.execute(
                select(Parent).where(Parent.user_id == current_user.id)
            )
            parent = parent_result.scalar_one_or_none()

            if parent:
                student_parent_result = await session.execute(
                    select(StudentParent).where(
                        StudentParent.parent_id == parent.id,
                        StudentParent.student_id == student_id
                    )
                )
                has_access = student_parent_result.scalar_one_or_none() is not None
        elif current_user.role == UserRole.STUDENT:
            # Students can only view their own report card
            has_access = current_user.id == student.user_id

        if not has_access:
            raise HTTPException(status_code=403, detail="Access denied")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Access control error in _compute_report_preview_data: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Access control error: {str(e)}")

    grades_result = await session.execute(
        select(Grade).where(
            Grade.student_id == student_id,
            Grade.academic_term_id == academic_term_id
        )
    )
    grades = grades_result.scalars().all()

    attendance_result = await session.execute(
        select(Attendance).where(
            Attendance.student_id == student_id,
            Attendance.academic_term_id == academic_term_id
        )
    )
    attendance_records = attendance_result.scalars().all()
    total_days = len(attendance_records)
    present_days = sum(1 for a in attendance_records if a.status in [AttendanceStatus.PRESENT, AttendanceStatus.LATE])
    attendance_percentage = round(present_days / total_days * 100, 1) if total_days > 0 else 0

    subject_ids = list(set(g.subject_id for g in grades))
    subject_result = await session.execute(select(Subject).where(Subject.id.in_(subject_ids)))
    subjects = {s.id: s for s in subject_result.scalars().all()}

    class_name = "Not Assigned"
    class_obj = None
    if student.class_id:
        class_result = await session.execute(select(Class).where(Class.id == student.class_id))
        class_obj = class_result.scalar_one_or_none()
        if class_obj:
            class_name = class_obj.name

    school_result = await session.execute(select(School).where(School.id == student.school_id))
    school = school_result.scalar_one_or_none()
    school_name = school.name if school else "School Name"

    student_data = {
        "id": student.id, "student_id": student.student_id,
        "first_name": student.first_name, "last_name": student.last_name,
        "school_id": student.school_id, "class_id": student.class_id,
        "class_name": class_name, "school_name": school_name,
    }
    # A plain dict works here because ReportCardPDFService.format_grade_data's
    # get_value() helper supports dict OR attribute access — no DB row needed.
    report_card_stub = {"attendance_percentage": attendance_percentage}
    grading_schemes = await grading_service.get_school_schemes(session, student.school_id)

    report_data = ReportCardPDFService.format_grade_data(
        report_card=report_card_stub, grades=grades, subjects_map=subjects,
        student=student_data, academic_term_name=None,
        grading_schemes=grading_schemes, class_level=class_obj.level if class_obj else None,
    )
    return student, report_data


@router.get("/report-cards/{student_id}/{academic_term_id}/suggest-remarks")
async def suggest_report_card_remarks(
    student_id: str,
    academic_term_id: str,
    current_user: User = Depends(require_permission("academics.report_card.generate")),
    _plan_check: User = Depends(require_plan_feature("academic_reports")),
    session: AsyncSession = Depends(get_session)
):
    """Free, rule-based remarks suggestion. Always available, no external cost.
    Staff-only (same permission as generate_report_card, the only endpoint
    that actually persists class_teacher_remarks/head_teacher_remarks) —
    this is purely a drafting aid for whoever fills in that form, with no
    parent/student use case. Restricting it here also closes a report-card
    approval-gate bypass: unlike get_report_card/download_report_card_pdf,
    this endpoint computed live grade averages straight from Grade rows
    with no ReportCard.status check, so a parent/student could previously
    see overall_average/overall_grade before the report card was approved
    for release."""
    student, report_data = await _compute_report_preview_data(student_id, academic_term_id, current_user, session)
    from services.comment_generator_service import generate_template_remarks
    suggestion = generate_template_remarks(student.first_name, report_data)
    return {
        **suggestion,
        "overall_average": report_data.get("overall_average"),
        "overall_grade": report_data.get("overall_grade"),
        "attendance_display": report_data.get("attendance_display"),
    }


@router.post("/report-cards/{student_id}/{academic_term_id}/ai-suggest-remarks")
async def ai_suggest_report_card_remarks(
    student_id: str,
    academic_term_id: str,
    current_user: User = Depends(require_permission("academics.report_card.generate")),
    _plan_check: User = Depends(require_plan_feature("academic_reports")),
    session: AsyncSession = Depends(get_session)
):
    """Optional BYOK AI remarks. Requires the school to have configured its own
    provider API key via /api/ai-settings; otherwise returns a clear 400 telling
    the caller to use the free suggestion instead.

    Staff-only (see suggest_report_card_remarks docstring) — this also closes
    an AI-cost-abuse gap: this endpoint calls the school's own paid AI
    provider key with no rate limiting, so leaving it open to every
    authenticated parent/student in the school (as it previously was) meant
    any of them could run up the school's AI bill by spamming it."""
    student, report_data = await _compute_report_preview_data(student_id, academic_term_id, current_user, session)

    from models.ai_settings import SchoolAISettings
    from services.ai_key_crypto import decrypt_api_key
    from services.ai_comment_service import generate_ai_remarks, AIProviderError

    settings_result = await session.execute(
        select(SchoolAISettings).where(SchoolAISettings.school_id == student.school_id)
    )
    ai_settings = settings_result.scalar_one_or_none()
    if not ai_settings or not ai_settings.enabled:
        raise HTTPException(
            status_code=400,
            detail="AI comment generation is not configured for this school. "
                   "Ask your school admin to add an API key in Settings, or use the free suggested remarks instead."
        )

    try:
        api_key = decrypt_api_key(ai_settings.api_key_encrypted)
        suggestion = await generate_ai_remarks(
            provider=ai_settings.provider, api_key=api_key, model=ai_settings.model,
            student_first_name=student.first_name, report_data=report_data,
        )
    except AIProviderError as e:
        raise HTTPException(status_code=502, detail=str(e))

    return suggestion


@router.get("/report-cards/{student_id}/{academic_term_id}/download")
async def download_report_card_pdf(
    student_id: str,
    academic_term_id: str,
    current_user: User = Depends(get_current_user),
    _plan_check: User = Depends(require_plan_feature("academic_reports")),
    session: AsyncSession = Depends(get_session)
):
    """Download a student's report card as PDF"""
    # Verify student access
    student_result = await session.execute(select(Student).where(Student.id == student_id))
    student = student_result.scalar_one_or_none()
    
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    
    # Check access control
    if not await _has_report_card_access(session, current_user, student):
        raise HTTPException(status_code=403, detail="Access denied")

    # Get report card
    report_card_result = await session.execute(
        select(ReportCard).where(
            ReportCard.student_id == student_id,
            ReportCard.academic_term_id == academic_term_id
        )
    )
    report_card = report_card_result.scalar_one_or_none()

    if not report_card:
        raise HTTPException(status_code=404, detail="Report card not found for this student and term")

    if not _report_card_visible_to_viewer(current_user, report_card):
        raise HTTPException(status_code=403, detail="Report card has not been approved for release yet")

    _rankings, class_average = await compute_class_rankings(session, student.class_id, academic_term_id)

    # Get grades (may be empty - that's ok)
    grades_result = await session.execute(
        select(Grade).where(
            Grade.student_id == student_id,
            Grade.academic_term_id == academic_term_id
        )
    )
    grades = grades_result.scalars().all()

    # Get subjects
    subject_ids = list(set(g.subject_id for g in grades))
    subject_result = await session.execute(
        select(Subject).where(Subject.id.in_(subject_ids))
    )
    subjects = {s.id: s for s in subject_result.scalars().all()}

    # Get academic term name
    academic_term_result = await session.execute(
        select(AcademicTerm).where(AcademicTerm.id == academic_term_id)
    )
    academic_term = academic_term_result.scalar_one_or_none()
    term_name = (
        f"{academic_term.academic_year} — {academic_term.term.value.capitalize()} Term"
        if academic_term else f"Term {academic_term_id}"
    )
    
    # Get class name
    class_name = "Not Assigned"
    class_obj = None
    if student.class_id:
        class_result = await session.execute(select(Class).where(Class.id == student.class_id))
        class_obj = class_result.scalar_one_or_none()
        class_name = class_obj.name if class_obj else "Not Assigned"

    # Get school name
    school_result = await session.execute(select(School).where(School.id == student.school_id))
    school = school_result.scalar_one_or_none()
    school_name = school.name if school else "School Name"

    # format_grade_data reads student as a dict-or-object; a SQLModel/Pydantic v2
    # instance can't have ad-hoc attributes assigned onto it (unlike a plain
    # object), so build a dict with the extra class_name/school_name fields
    # instead of mutating the ORM instance — mutating it raised a ValueError on
    # every single download.
    student_data = {
        "id": student.id,
        "first_name": student.first_name,
        "last_name": student.last_name,
        "school_id": student.school_id,
        "class_id": student.class_id,
        "class_name": class_name,
        "school_name": school_name,
    }

    grading_schemes = await grading_service.get_school_schemes(session, student.school_id)

    # Format data for PDF
    report_data = ReportCardPDFService.format_grade_data(
        report_card=report_card,
        grades=grades,
        subjects_map=subjects,
        student=student_data,
        academic_term_name=term_name,
        class_average=class_average,
        grading_schemes=grading_schemes,
        class_level=class_obj.level if class_obj else None,
    )

    # Fetch school's custom template (or use file fallback)
    custom_template = await get_school_template(session, student.school_id)
    pdf_service = ReportCardPDFService()
    try:
        pdf_bytes = pdf_service.generate_pdf(report_data, template_html=custom_template)
        if not pdf_bytes or len(pdf_bytes) == 0:
            raise HTTPException(status_code=500, detail="PDF generation produced empty output")
    except Exception as e:
        error_msg = str(e)
        raise HTTPException(status_code=500, detail=f"Failed to generate PDF: {error_msg}")
    
    # Return as downloadable PDF
    filename = f"reportcard_{student.student_id}_{academic_term_id}.pdf"
    return StreamingResponse(
        BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@router.post("/report-cards/{student_id}/{academic_term_id}/regenerate-pdf")
async def regenerate_report_card_pdf(
    student_id: str,
    academic_term_id: str,
    current_user: User = Depends(require_permission("academics.report_card.generate")),
    _plan_check: User = Depends(require_plan_feature("academic_reports")),
    session: AsyncSession = Depends(get_session)
):
    """Regenerate and download a report card PDF (teacher action)"""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    # Verify student
    student_result = await session.execute(select(Student).where(Student.id == student_id))
    student = student_result.scalar_one_or_none()
    
    if not student or student.school_id != school_id:
        raise HTTPException(status_code=404, detail="Student not found in your school")
    
    # Get or create report card
    report_card_result = await session.execute(
        select(ReportCard).where(
            ReportCard.student_id == student_id,
            ReportCard.academic_term_id == academic_term_id
        )
    )
    report_card = report_card_result.scalar_one_or_none()
    
    if not report_card:
        # Create new report card (grades may be empty, that's ok)
        grades_result = await session.execute(
            select(Grade).where(
                Grade.student_id == student_id,
                Grade.academic_term_id == academic_term_id
            )
        )
        grades = grades_result.scalars().all()

        subject_weights = await _resolve_subject_weights(session, school_id, student.class_id, grades)
        total_score, average_score = compute_overall_ges_score(grades, weights=subject_weights)

        # Get class size
        class_count_result = await session.execute(
            select(func.count(Student.id)).where(Student.class_id == student.class_id, Student.status == "active")
        )
        class_size = class_count_result.scalar() or 0

        # Get attendance for this term only
        attendance_result = await session.execute(
            select(Attendance).where(
                Attendance.student_id == student_id,
                Attendance.academic_term_id == academic_term_id
            )
        )
        attendance_records = attendance_result.scalars().all()
        total_days = len(attendance_records)
        present_days = sum(1 for a in attendance_records if a.status in [AttendanceStatus.PRESENT, AttendanceStatus.LATE])
        attendance_percentage = round(present_days / total_days * 100, 1) if total_days > 0 else 0

        rankings, _class_average = await compute_class_rankings(session, student.class_id, academic_term_id)

        report_card = ReportCard(
            school_id=school_id,
            student_id=student_id,
            class_id=student.class_id,
            academic_term_id=academic_term_id,
            total_score=total_score,
            average_score=average_score,
            position=rankings.get(student_id),
            class_size=class_size,
            attendance_percentage=attendance_percentage,
            generated_by=current_user.id
        )
        session.add(report_card)
        try:
            await session.commit()
        except IntegrityError:
            # A concurrent request for the same student+term already
            # created it — this is a "get or create" download action, so
            # use the row that now exists instead of erroring.
            await session.rollback()
            report_card_result = await session.execute(
                select(ReportCard).where(
                    ReportCard.student_id == student_id,
                    ReportCard.academic_term_id == academic_term_id,
                )
            )
            report_card = report_card_result.scalar_one()
        else:
            await session.refresh(report_card)
    
    return {
        "id": report_card.id,
        "message": "Report card ready for PDF download",
        "download_url": f"/api/grades/report-cards/{student_id}/{academic_term_id}/download"
    }


# ============ BULK REPORT ENDPOINTS ============

@router.get("/report-cards/bulk-preview")
async def bulk_preview_report_cards(
    class_id: str = Query(..., description="Class ID"),
    term_id: str = Query(..., description="Academic Term ID"),
    current_user: User = Depends(require_permission("academics.report_card.generate")),
    _plan_check: User = Depends(require_plan_feature("academic_reports")),
    session: AsyncSession = Depends(get_session)
):
    """
    Generate HTML previews for all students in a class for a given academic term.
    Returns array of student report cards with HTML content for bulk preview.
    """
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    # Verify class belongs to user's school
    class_result = await session.execute(select(Class).where(Class.id == class_id))
    class_obj = class_result.scalar_one_or_none()
    
    if not class_obj or class_obj.school_id != school_id:
        raise HTTPException(status_code=404, detail="Class not found")
    
    # Verify academic term exists
    term_result = await session.execute(select(AcademicTerm).where(AcademicTerm.id == term_id))
    academic_term = term_result.scalar_one_or_none()
    
    if not academic_term:
        raise HTTPException(status_code=404, detail="Academic term not found")
    
    # Get all students in the class
    students_result = await session.execute(
        select(Student).where(Student.class_id == class_id, Student.status == "active")
    )
    students = students_result.scalars().all()
    
    if not students:
        raise HTTPException(status_code=404, detail="No students found in this class")
    
    # Get school name
    school_result = await session.execute(select(School).where(School.id == school_id))
    school = school_result.scalar_one_or_none()
    school_name = school.name if school else "School Name"
    
    # Generate previews for each student
    student_reports = []
    pdf_service = ReportCardPDFService()
    custom_template = await get_school_template(session, school_id)

    # Batch-fetch everything for the whole class up front instead of one query per
    # student per data type (grades/report-card/attendance/subjects) — the previous
    # version issued 4-6 sequential queries per student in this loop.
    student_ids = [s.id for s in students]
    class_size = len(students)
    rankings, class_average = await compute_class_rankings(session, class_id, term_id)

    all_grades = (await session.execute(
        select(Grade).where(Grade.student_id.in_(student_ids), Grade.academic_term_id == term_id)
    )).scalars().all()
    grades_by_student: dict = {}
    for g in all_grades:
        grades_by_student.setdefault(g.student_id, []).append(g)

    all_subject_ids = list({g.subject_id for g in all_grades})
    subjects = {
        s.id: s for s in (await session.execute(
            select(Subject).where(Subject.id.in_(all_subject_ids))
        )).scalars().all()
    } if all_subject_ids else {}

    all_report_cards = (await session.execute(
        select(ReportCard).where(ReportCard.student_id.in_(student_ids), ReportCard.academic_term_id == term_id)
    )).scalars().all()
    report_cards_by_student = {rc.student_id: rc for rc in all_report_cards}

    all_attendance = (await session.execute(
        select(Attendance).where(Attendance.student_id.in_(student_ids), Attendance.academic_term_id == term_id)
    )).scalars().all()
    attendance_by_student: dict = {}
    for a in all_attendance:
        attendance_by_student.setdefault(a.student_id, []).append(a)

    # Every student in this class shares the same class level — resolve the
    # school's configured grading schemes (and derived CA:exam weights) once,
    # not once per student.
    grading_schemes = await grading_service.get_school_schemes(session, school_id)
    subject_weights = grading_service.build_subject_weights(grading_schemes, class_obj.level, all_subject_ids)

    for student in students:
        try:
            grades = grades_by_student.get(student.id, [])
            report_card = report_cards_by_student.get(student.id)

            # Auto-generate report card if it doesn't exist
            if not report_card:
                total_score, average_score = compute_overall_ges_score(grades, weights=subject_weights)

                attendance_records = attendance_by_student.get(student.id, [])
                total_days = len(attendance_records)
                present_days = sum(1 for a in attendance_records if a.status in [AttendanceStatus.PRESENT, AttendanceStatus.LATE])
                attendance_percentage = round(present_days / total_days * 100, 1) if total_days > 0 else 0

                report_card = ReportCard(
                    school_id=school_id,
                    student_id=student.id,
                    class_id=class_id,
                    academic_term_id=term_id,
                    total_score=total_score,
                    average_score=average_score,
                    position=rankings.get(student.id),
                    class_size=class_size,
                    attendance_percentage=attendance_percentage,
                    generated_by=current_user.id
                )
                session.add(report_card)
                await session.commit()
                await session.refresh(report_card)
                report_cards_by_student[student.id] = report_card

            # Create student data object
            student_data = {
                "id": student.id,
                "student_id": student.student_id,
                "first_name": student.first_name,
                "last_name": student.last_name,
                "school_id": student.school_id,
                "class_id": student.class_id,
                "class_name": class_obj.name,
                "school_name": school_name,
            }

            # Format data for rendering
            report_data = ReportCardPDFService.format_grade_data(
                report_card=report_card,
                grades=grades,
                subjects_map=subjects,
                student=student_data,
                academic_term_name=None,
                class_average=class_average,
                grading_schemes=grading_schemes,
                class_level=class_obj.level,
            )

            # Render HTML
            html_content = pdf_service.render_html(report_data, template_html=custom_template)
            
            student_reports.append({
                "student_id": student.id,
                "student_name": f"{student.first_name} {student.last_name}",
                "student_number": student.student_id,
                "html": html_content,
                "report_data": report_data
            })
        except Exception as e:
            logger.error(f"Error generating preview for student {student.id}: {str(e)}")
            # Continue with next student instead of failing entire bulk operation
            continue
    
    return {
        "class_id": class_id,
        "class_name": class_obj.name,
        "term_id": term_id,
        "academic_year": academic_term.academic_year,
        "term": academic_term.term,
        "total_students": len(students),
        "students": student_reports
    }


@router.post("/report-cards/bulk-download")
async def bulk_download_report_cards(
    request_data: dict,
    current_user: User = Depends(require_permission("academics.report_card.generate")),
    _plan_check: User = Depends(require_plan_feature("academic_reports")),
    session: AsyncSession = Depends(get_session)
):
    """
    Download all student report cards for a class/term as a ZIP file.
    Expects: { class_id, term_id, format: "zip" }
    """
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")
    
    class_id = request_data.get("class_id")
    term_id = request_data.get("term_id")
    
    if not class_id or not term_id:
        raise HTTPException(status_code=400, detail="class_id and term_id are required")
    
    # Verify class belongs to user's school
    class_result = await session.execute(select(Class).where(Class.id == class_id))
    class_obj = class_result.scalar_one_or_none()
    
    if not class_obj or class_obj.school_id != school_id:
        raise HTTPException(status_code=404, detail="Class not found")
    
    # Verify academic term exists
    term_result = await session.execute(select(AcademicTerm).where(AcademicTerm.id == term_id))
    academic_term = term_result.scalar_one_or_none()
    
    if not academic_term:
        raise HTTPException(status_code=404, detail="Academic term not found")
    
    # Get all students in the class
    students_result = await session.execute(
        select(Student).where(Student.class_id == class_id, Student.status == "active")
    )
    students = students_result.scalars().all()

    if not students:
        raise HTTPException(status_code=404, detail="No students found in this class")

    # Create ZIP file in memory
    zip_buffer = BytesIO()
    pdf_service = ReportCardPDFService()
    custom_template = await get_school_template(session, school_id)

    # Batch-fetch everything for the whole class up front — see the identical
    # comment in bulk_preview_report_cards for why.
    student_ids = [s.id for s in students]
    class_size = len(students)
    rankings, class_average = await compute_class_rankings(session, class_id, term_id)

    all_grades = (await session.execute(
        select(Grade).where(Grade.student_id.in_(student_ids), Grade.academic_term_id == term_id)
    )).scalars().all()
    grades_by_student: dict = {}
    for g in all_grades:
        grades_by_student.setdefault(g.student_id, []).append(g)

    all_subject_ids = list({g.subject_id for g in all_grades})
    subjects = {
        s.id: s for s in (await session.execute(
            select(Subject).where(Subject.id.in_(all_subject_ids))
        )).scalars().all()
    } if all_subject_ids else {}

    all_report_cards = (await session.execute(
        select(ReportCard).where(ReportCard.student_id.in_(student_ids), ReportCard.academic_term_id == term_id)
    )).scalars().all()
    report_cards_by_student = {rc.student_id: rc for rc in all_report_cards}

    all_attendance = (await session.execute(
        select(Attendance).where(Attendance.student_id.in_(student_ids), Attendance.academic_term_id == term_id)
    )).scalars().all()
    attendance_by_student: dict = {}
    for a in all_attendance:
        attendance_by_student.setdefault(a.student_id, []).append(a)

    # Get school name
    school_result = await session.execute(select(School).where(School.id == school_id))
    school = school_result.scalar_one_or_none()
    school_name = school.name if school else "School Name"

    # Every student in this class shares the same class level — resolve the
    # school's configured grading schemes (and derived CA:exam weights) once,
    # not once per student.
    grading_schemes = await grading_service.get_school_schemes(session, school_id)
    subject_weights = grading_service.build_subject_weights(grading_schemes, class_obj.level, all_subject_ids)

    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        successful_count = 0

        for student in students:
            try:
                grades = grades_by_student.get(student.id, [])
                report_card = report_cards_by_student.get(student.id)

                # Auto-generate if not exists
                if not report_card:
                    total_score, average_score = compute_overall_ges_score(grades, weights=subject_weights)

                    attendance_records = attendance_by_student.get(student.id, [])
                    total_days = len(attendance_records)
                    present_days = sum(1 for a in attendance_records if a.status in [AttendanceStatus.PRESENT, AttendanceStatus.LATE])
                    attendance_percentage = round(present_days / total_days * 100, 1) if total_days > 0 else 0

                    report_card = ReportCard(
                        school_id=school_id,
                        student_id=student.id,
                        class_id=class_id,
                        academic_term_id=term_id,
                        total_score=total_score,
                        average_score=average_score,
                        position=rankings.get(student.id),
                        class_size=class_size,
                        attendance_percentage=attendance_percentage,
                        generated_by=current_user.id
                    )
                    session.add(report_card)
                    await session.commit()
                    await session.refresh(report_card)
                    report_cards_by_student[student.id] = report_card

                # Create student data
                student_data = {
                    "id": student.id,
                    "student_id": student.student_id,
                    "first_name": student.first_name,
                    "last_name": student.last_name,
                    "school_id": student.school_id,
                    "class_id": student.class_id,
                    "class_name": class_obj.name,
                    "school_name": school_name,
                }
                
                # Format data for PDF
                report_data = ReportCardPDFService.format_grade_data(
                    report_card=report_card,
                    grades=grades,
                    subjects_map=subjects,
                    student=student_data,
                    academic_term_name=(
                        f"{academic_term.academic_year} — {academic_term.term.value.capitalize()} Term"
                        if academic_term else None
                    ),
                    class_average=class_average,
                    grading_schemes=grading_schemes,
                    class_level=class_obj.level,
                )

                # Generate PDF
                pdf_bytes = pdf_service.generate_pdf(report_data, template_html=custom_template)
                
                if pdf_bytes and len(pdf_bytes) > 0:
                    # Add to ZIP with sanitized filename
                    filename = f"{student.student_id}_{student.first_name}_{student.last_name}.pdf"
                    zip_file.writestr(filename, pdf_bytes)
                    successful_count += 1
            except Exception as e:
                logger.error(f"Error generating PDF for student {student.id}: {str(e)}")
                # Continue with next student
                continue
        
        if successful_count == 0:
            raise HTTPException(status_code=500, detail="Failed to generate any report cards")
    
    zip_buffer.seek(0)

    # Return ZIP file as download
    filename = f"reportcards_{class_id}_{term_id}.zip"
    return StreamingResponse(
        iter([zip_buffer.getvalue()]),
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@router.post("/report-cards/bulk-approve", response_model=dict)
async def bulk_approve_report_cards(
    background_tasks: BackgroundTasks,
    class_id: str = Query(..., description="Class ID"),
    term_id: str = Query(..., description="Academic Term ID"),
    override_fee_hold: bool = Query(False, description="Approve fee-defaulting students anyway, when the school has hold_report_cards_for_fee_defaulters enabled"),
    current_user: User = Depends(require_permission("academics.report_card.approve")),
    _plan_check: User = Depends(require_plan_feature("academic_reports")),
    session: AsyncSession = Depends(get_session)
):
    """Approve every DRAFT report card for a class/term in one action —
    the bulk-preview counterpart of the single-student approve endpoint, so
    an admin reviewing a whole class doesn't have to open each student
    individually just to sign off. Only touches cards that already exist
    (bulk-preview auto-creates bare ones); it doesn't generate anything.

    Mirrors approve_report_card's recall-resolution: a card that was
    recalled for correction and hasn't been resent yet gets its recall
    stamped resent and its parent notified here too — without this, bulk-
    approving a class left every recalled-then-corrected card in that class
    permanently stuck "unresolved" with the parent never told the fix
    shipped, even though the card itself was visibly re-approved."""
    school_id = current_user.school_id
    if not school_id:
        raise HTTPException(status_code=403, detail="No school context")

    class_result = await session.execute(select(Class).where(Class.id == class_id))
    class_obj = class_result.scalar_one_or_none()
    if not class_obj or class_obj.school_id != school_id:
        raise HTTPException(status_code=404, detail="Class not found")

    students_result = await session.execute(
        select(Student.id).where(Student.class_id == class_id, Student.status == "active")
    )
    student_ids = students_result.scalars().all()
    if not student_ids:
        raise HTTPException(status_code=404, detail="No students found in this class")

    report_cards = (await session.execute(
        select(ReportCard).where(
            ReportCard.student_id.in_(student_ids), ReportCard.academic_term_id == term_id
        )
    )).scalars().all()
    if not report_cards:
        raise HTTPException(status_code=404, detail="No report cards found for this class/term — preview them first")

    fee_holds_by_student = {}
    if not override_fee_hold:
        school_result = await session.execute(select(School).where(School.id == school_id))
        school = school_result.scalar_one_or_none()
        if school and school.hold_report_cards_for_fee_defaulters:
            fee_result = await session.execute(
                select(Fee).where(Fee.school_id == school_id, Fee.student_id.in_(student_ids))
            )
            for fee in fee_result.scalars().all():
                fee_holds_by_student[fee.student_id] = fee_holds_by_student.get(fee.student_id, 0.0) + (
                    fee.amount_due - fee.discount - fee.amount_paid
                )

    approved_count = 0
    already_approved_count = 0
    fee_held_count = 0
    resent_student_ids = []
    now = datetime.utcnow()
    for report_card in report_cards:
        if report_card.status == ReportCardStatus.APPROVED:
            already_approved_count += 1
            continue
        if round(fee_holds_by_student.get(report_card.student_id, 0.0), 2) > 0:
            fee_held_count += 1
            continue
        report_card.status = ReportCardStatus.APPROVED.value
        report_card.approved_by = current_user.id
        report_card.approved_at = now
        session.add(report_card)
        approved_count += 1

        open_recall = (await session.execute(
            select(ReportCardRecall).where(
                ReportCardRecall.report_card_id == report_card.id,
                ReportCardRecall.resent_at.is_(None),
            ).order_by(ReportCardRecall.recalled_at.desc())
        )).scalars().first()
        if open_recall:
            open_recall.resent_at = now
            open_recall.resent_by = current_user.id
            session.add(open_recall)
            resent_student_ids.append(report_card.student_id)

    await session.commit()

    for student_id in resent_student_ids:
        student = await session.get(Student, student_id)
        if student:
            await _notify_parent_report_card_status(
                session, student, current_user, background_tasks,
                sms_message=f"Update: the report card for {student.first_name} {student.last_name} has been corrected and is now available. Please check the school portal. -School",
                in_app_subject="Corrected Report Card Available",
                in_app_content=f"The report card for {student.first_name} {student.last_name} has been corrected and is now available to view.",
                notification_type="report_card_resent",
            )

    await log_event(
        session, actor=current_user, action="grade.report_card_bulk_approved", entity_type="report_card",
        entity_id=class_id, school_id=school_id,
        summary=f"{current_user.email} bulk-approved {approved_count} report card(s) for class {class_id}, term {term_id}",
    )

    return {
        "class_id": class_id,
        "term_id": term_id,
        "approved_count": approved_count,
        "already_approved_count": already_approved_count,
        "fee_held_count": fee_held_count,
        "resent_after_recall_count": len(resent_student_ids),
        "total_report_cards": len(report_cards),
        "message": f"Approved {approved_count} report card(s); {already_approved_count} were already approved"
        + (f"; {fee_held_count} on hold for outstanding fees" if fee_held_count else "")
        + (f"; {len(resent_student_ids)} corrected recall(s) resolved and parents notified" if resent_student_ids else ""),
    }

