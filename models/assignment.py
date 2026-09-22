"""Assignment, Submission, and Learning Materials models for Teacher Portal"""
from sqlmodel import SQLModel, Field
from sqlalchemy import Column, String, ForeignKey
from sqlalchemy import Index, Column
from sqlalchemy.types import Enum as SQLEnum
from typing import Optional, List
from datetime import datetime
from enum import Enum
import uuid
import json


# ============================================================================
# ENUMS - GES-Aligned Assessment Types
# ============================================================================

class AssignmentType(str, Enum):
    """GES-aligned assignment types (CA components)"""
    CLASSWORK = "classwork"      # CA1 - In-class activities
    HOMEWORK = "homework"        # CA2 - Take-home assignments
    QUIZ = "quiz"                # CA3 - Periodic tests
    PROJECT = "project"          # CA4 - Extended projects
    WORKSHEET = "worksheet"      # Supplementary work


class AssignmentStatus(str, Enum):
    """Assignment lifecycle status"""
    DRAFT = "draft"              # Not yet published
    PUBLISHED = "published"      # Available to students
    CLOSED = "closed"            # Submissions no longer accepted
    ARCHIVED = "archived"        # Historical/reference only


class SubmissionStatus(str, Enum):
    """Student submission status"""
    NOT_SUBMITTED = "not_submitted"  # Student hasn't submitted
    SUBMITTED = "submitted"          # Submitted on time
    GRADED = "graded"                # Teacher has graded
    LATE = "late"                    # Submitted after deadline
    EXCUSED = "excused"              # Exempted by teacher


class ProgressNoteType(str, Enum):
    """Student progress note categories"""
    BEHAVIORAL = "behavioral"      # Behavior/conduct
    ACADEMIC = "academic"          # Academic performance
    PARTICIPATION = "participation" # Class participation
    EFFORT = "effort"              # Effort level
    CONDUCT = "conduct"            # General conduct


class ResourceType(str, Enum):
    """Learning resource types"""
    LESSON_PLAN = "lesson_plan"
    WORKSHEET = "worksheet"
    DOCUMENT = "document"
    VIDEO = "video"
    LINK = "link"
    INTERACTIVE = "interactive"


class QuestionType(str, Enum):
    """Question types for assignments"""
    ESSAY = "essay"
    MULTIPLE_CHOICE = "multipleChoice"
    SHORT_ANSWER = "shortAnswer"
    MATCHING = "matching"


# ============================================================================
# MODEL 1: ASSIGNMENT - Main assignment/classwork model
# ============================================================================

class Assignment(SQLModel, table=True):
    """
    Assignment/Classwork model for Teacher Portal.
    Represents a task/assignment given to a class.
    Aligns with GES assessment types (CA components).
    """
    __tablename__ = "assignments"
    
    # Primary key & multi-tenancy
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)  # Multi-tenant isolation
    
    # Teacher & Class context
    teacher_id: str = Field(index=True)  # Staff.id
    class_id: str = Field(index=True)
    subject_id: str = Field(index=True)
    academic_term_id: str = Field(sa_column=Column(String, ForeignKey("academic_terms.id", ondelete="CASCADE"), index=True))
    
    # Assignment content
    title: str
    description: str
    assignment_type: AssignmentType = Field(
        sa_column=Column(SQLEnum('classwork', 'homework', 'quiz', 'project', 'worksheet', name='assignmenttype_portal', native_enum=False))
    )  # GES CA type: classwork, homework, quiz, project
    status: AssignmentStatus = Field(
        default=AssignmentStatus.DRAFT,
        sa_column=Column(SQLEnum('draft', 'published', 'closed', 'archived', name='assignmentstatus_portal', native_enum=False))
    )
    
    # Assignment details
    instructions: Optional[str] = None
    rubric: Optional[str] = None  # JSON string with rubric criteria
    points_possible: float = 100.0
    
    # Dates
    created_date: datetime = Field(default_factory=datetime.utcnow)
    published_date: Optional[datetime] = None
    due_date: Optional[datetime] = None
    due_time: Optional[str] = None  # HH:MM format (e.g., "14:30")
    
    # Attachments/Resources
    attachment_urls: Optional[str] = None  # JSON array: ["url1", "url2"]
    resource_links: Optional[str] = None   # JSON array: ["http://..."]
    
    # Audit trail
    recorded_by: str  # User.id of teacher who created
    updated_by: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


# ============================================================================
# MODEL 2: ASSIGNMENT QUESTION - Question bank for assignments
# ============================================================================

class AssignmentQuestion(SQLModel, table=True):
    """
    Question model for assignments.
    Supports multiple question types: essay, MCQ, short answer, matching.
    """
    __tablename__ = "assignment_questions"
    
    # Primary key
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    
    # Foreign key to assignment
    assignment_id: str = Field(index=True)  # Assignment.id
    
    # Question content
    question_text: str  # The question/prompt
    question_type: QuestionType = Field(
        sa_column=Column(SQLEnum('essay', 'multipleChoice', 'shortAnswer', 'matching', name='questiontype_enum', native_enum=False))
    )
    
    # Options & Answer (varies by type)
    options: Optional[str] = None  # JSON array for MCQ/matching: ["option1", "option2"]
    correct_answer: Optional[str] = None  # Correct answer (for auto-grading)
    
    # Grading
    points: float = 1.0  # Points for this question
    
    # Audit
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class AssignmentCreate(SQLModel):
    """Request model for creating assignment"""
    title: str
    description: str
    class_id: str
    subject_id: str
    academic_term_id: str
    assignment_type: AssignmentType
    instructions: Optional[str] = None
    rubric: Optional[str] = None
    points_possible: float = 100.0
    due_date: Optional[datetime] = None
    due_time: Optional[str] = None
    attachment_urls: Optional[List[str]] = None
    resource_links: Optional[List[str]] = None
    # NEW: Questions and grading settings
    questions: Optional[List[dict]] = None  # List of {question_text, question_type, options, correct_answer, points}
    grading_settings: Optional[dict] = None  # {allow_auto_grade, question_types, rubric_enabled, rubric_criteria}


class AssignmentUpdate(SQLModel):
    """Request model for updating assignment (draft only)"""
    title: Optional[str] = None
    description: Optional[str] = None
    instructions: Optional[str] = None
    rubric: Optional[str] = None
    points_possible: Optional[float] = None
    due_date: Optional[datetime] = None
    due_time: Optional[str] = None
    attachment_urls: Optional[List[str]] = None
    resource_links: Optional[List[str]] = None
    questions: Optional[List[dict]] = None  # Updated questions
    grading_settings: Optional[dict] = None  # Updated grading settings


class AssignmentResponse(SQLModel):
    """Response model for assignment"""
    id: str
    title: str
    description: str
    assignment_type: AssignmentType
    status: AssignmentStatus
    points_possible: float
    due_date: Optional[datetime]
    due_time: Optional[str]
    created_date: datetime
    published_date: Optional[datetime]
    submission_count: Optional[int] = None
    graded_count: Optional[int] = None


# ============================================================================
# MODEL 2: SUBMISSION - Student submissions
# ============================================================================

class Submission(SQLModel, table=True):
    """
    Student submission for an assignment.
    Tracks submission status, content, and grading.
    """
    __tablename__ = "submissions"
    
    # Primary key & multi-tenancy
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    
    # Assignment & Student reference
    assignment_id: str = Field(index=True)
    student_id: str = Field(index=True)
    class_id: str = Field(index=True)
    subject_id: str = Field(index=True)
    
    # Submission status
    status: SubmissionStatus = Field(
        default=SubmissionStatus.NOT_SUBMITTED,
        sa_column=Column(SQLEnum('not_submitted', 'submitted', 'graded', 'late', 'excused', name='submissionstatus_portal', native_enum=False))
    )
    
    # Submission content
    submission_text: Optional[str] = None  # Text response
    submission_urls: Optional[str] = None  # JSON array of file URLs
    submission_date: Optional[datetime] = None
    
    # Grading
    score: Optional[float] = None
    max_score: Optional[float] = None  # From Assignment.points_possible
    feedback: Optional[str] = None
    rubric_scores: Optional[str] = None  # JSON: {"criteria1": 85, "criteria2": 90}
    graded_answers: Optional[str] = None  # JSON: {"question_id": score, ...} for per-question grading
    graded_by: Optional[str] = None  # User.id of teacher who graded
    graded_date: Optional[datetime] = None
    
    # Audit trail
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class SubmissionCreate(SQLModel):
    """Request model for student submission"""
    submission_text: Optional[str] = None
    submission_urls: Optional[List[str]] = None


class SubmissionGrade(SQLModel):
    """Request model for grading submission"""
    score: float
    feedback: Optional[str] = None
    rubric_scores: Optional[dict] = None  # {"criteria1": 85, ...}
    graded_answers: Optional[dict] = None  # {"question_id": score, ...}


class SubmissionResponse(SQLModel):
    """Response model for submission"""
    id: str
    student_id: str
    status: SubmissionStatus
    submission_date: Optional[datetime]
    score: Optional[float]
    feedback: Optional[str]
    graded_date: Optional[datetime]
    graded_answers: Optional[dict] = None


# ============================================================================
# MODEL 3: TEACHER RESOURCE - Lesson materials and resources
# ============================================================================

class TeacherResource(SQLModel, table=True):
    """
    Learning materials/resources uploaded by teacher.
    Can be class-specific or general/shared resources.
    """
    __tablename__ = "teacher_resources"
    
    # Primary key & multi-tenancy
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    teacher_id: str = Field(index=True)
    
    # Scope (class/subject specific or general)
    class_id: Optional[str] = Field(default=None, index=True)
    subject_id: Optional[str] = Field(default=None, index=True)
    
    # Resource details
    title: str
    description: Optional[str] = None
    resource_type: str  # From ResourceType enum
    file_url: str  # S3 or storage URL
    file_size_bytes: Optional[int] = None
    file_mime_type: Optional[str] = None
    
    # Sharing & Discovery
    is_public: bool = False  # Available to other teachers in school
    tags: Optional[str] = None  # Comma-separated tags for search
    
    # Audit trail
    recorded_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class TeacherResourceCreate(SQLModel):
    """Request model for creating resource"""
    title: str
    description: Optional[str] = None
    resource_type: str
    file_url: str
    file_size_bytes: Optional[int] = None
    file_mime_type: Optional[str] = None
    class_id: Optional[str] = None
    subject_id: Optional[str] = None
    is_public: bool = False
    tags: Optional[List[str]] = None


# ============================================================================
# MODEL 4: LEARNING MATERIAL - Curriculum content and lessons
# ============================================================================

class LearningMaterial(SQLModel, table=True):
    """
    Curriculum-aligned learning materials for students.
    Lesson content, supplementary materials, links.
    """
    __tablename__ = "learning_materials"
    
    # Primary key & multi-tenancy
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    
    # Teacher & Class context
    teacher_id: str = Field(index=True)
    class_id: str = Field(index=True)
    subject_id: str = Field(index=True)
    academic_term_id: str = Field(sa_column=Column(String, ForeignKey("academic_terms.id", ondelete="CASCADE"), index=True))
    
    # Content
    title: str
    content_type: str  # "text", "pdf", "video", "link", "interactive"
    content: Optional[str] = None  # For text-based content
    external_url: Optional[str] = None  # For links/videos
    material_urls: Optional[str] = None  # JSON array of file URLs
    
    # Organization
    week_number: Optional[int] = None  # Week of term (1-12)
    topic_name: Optional[str] = None  # Curriculum topic
    is_published: bool = False  # Available to students
    
    # Audit trail
    recorded_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class LearningMaterialCreate(SQLModel):
    """Request model for creating learning material"""
    title: str
    class_id: str
    subject_id: str
    academic_term_id: str
    content_type: str
    content: Optional[str] = None
    external_url: Optional[str] = None
    material_urls: Optional[List[str]] = None
    week_number: Optional[int] = None
    topic_name: Optional[str] = None
    is_published: bool = False


# ============================================================================
# MODEL 5: STUDENT PROGRESS NOTE - Behavioral and progress tracking
# ============================================================================

class StudentProgressNote(SQLModel, table=True):
    """
    Teacher notes on student progress, behavior, conduct.
    Used for formative assessment and parent communication.
    """
    __tablename__ = "student_progress_notes"
    
    # Primary key & multi-tenancy
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    
    # Teacher & Student context
    teacher_id: str = Field(index=True)
    student_id: str = Field(index=True)
    class_id: str = Field(index=True)
    
    # Note content
    note_type: ProgressNoteType
    note: str  # The actual note text
    is_positive: bool = True  # Sentiment: positive or needs improvement
    
    # Visibility
    visible_to_parents: bool = True  # Parents can see this note
    
    # Audit trail
    recorded_by: str  # User.id of teacher
    created_at: datetime = Field(default_factory=datetime.utcnow)


class StudentProgressNoteCreate(SQLModel):
    """Request model for creating progress note"""
    student_id: str
    class_id: str
    note_type: ProgressNoteType
    note: str
    is_positive: bool = True
    visible_to_parents: bool = True


class StudentProgressNoteResponse(SQLModel):
    """Response model for progress note"""
    id: str
    note_type: ProgressNoteType
    note: str
    is_positive: bool
    created_at: datetime
    recorded_by: str


# ============================================================================
# UTILITY: Summary Models for Dashboard/Analytics
# ============================================================================

class AssignmentStats(SQLModel):
    """Statistics for an assignment"""
    total_students: int
    submitted: int
    not_submitted: int
    graded: int
    pending_grading: int
    late_submissions: int
    submission_percentage: float


class ClassPerformanceMetrics(SQLModel):
    """Performance metrics for a class"""
    class_id: str
    class_name: str
    subject_id: str
    subject_name: str
    average_score: float
    highest_score: float
    lowest_score: float
    students_count: int
    pass_percentage: float  # % scoring >= 50 (GES pass mark)
    excellent_percentage: float  # % scoring >= 80 (GES grade 1)


class SubmissionSummary(SQLModel):
    """Summary of submissions for export"""
    student_name: str
    student_id: str
    submission_status: str
    submission_date: Optional[datetime]
    score: Optional[float]
    feedback: Optional[str]


# ============================================================================
# MODEL 6: COURSE MODULE - Structured LMS module container
# ============================================================================

class CourseModule(SQLModel, table=True):
    """
    Ordered container of learning items (embedded video links and/or
    existing LearningMaterial rows) for a class/subject/term — the
    structural unit of the lightweight LMS. Publishing gates visibility to
    students, same idiom as LearningMaterial.is_published.
    """
    __tablename__ = "course_modules"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)

    teacher_id: str = Field(index=True)  # Staff.id
    class_id: str = Field(index=True)
    subject_id: str = Field(index=True)
    academic_term_id: str = Field(sa_column=Column(String, ForeignKey("academic_terms.id", ondelete="CASCADE"), index=True))

    title: str
    description: Optional[str] = None
    order_index: int = 0
    is_published: bool = False

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class CourseModuleCreate(SQLModel):
    class_id: str
    subject_id: str
    academic_term_id: str
    title: str
    description: Optional[str] = None
    order_index: int = 0


class CourseModuleUpdate(SQLModel):
    title: Optional[str] = None
    description: Optional[str] = None
    order_index: Optional[int] = None
    is_published: Optional[bool] = None


class CourseModuleItem(SQLModel, table=True):
    """
    A single ordered entry in a CourseModule — an embedded video link, an
    uploaded/hosted video file, a pointer to an existing LearningMaterial
    row, or a pointer to an existing Assignment (a "quiz/assessment" item —
    reuses the teacher portal's full Assignment/AssignmentQuestion/
    Submission/AutoGrader machinery rather than a parallel quiz engine).
    Exactly one of video_url/learning_material_id/assignment_id is
    expected, enforced at the router layer (see
    routers/teacher/assignments.py::add_course_module_item).

    video_source distinguishes the two video_url flavors: "link" (an
    external URL — YouTube, Vimeo, etc. — opened in a new tab, never
    embedded) vs "upload" (a file saved under uploads/course-videos/ and
    served from this app's own /uploads static mount, played inline via
    an HTML5 <video> element). See
    routers/teacher/assignments.py::upload_course_module_video.
    """
    __tablename__ = "course_module_items"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    module_id: str = Field(sa_column=Column(String, ForeignKey("course_modules.id", ondelete="CASCADE"), index=True))

    title: str
    video_url: Optional[str] = None
    video_source: Optional[str] = None  # "link" | "upload" — see class docstring
    learning_material_id: Optional[str] = Field(
        default=None,
        sa_column=Column(String, ForeignKey("learning_materials.id", ondelete="SET NULL"), index=True, nullable=True)
    )
    # A "quiz/assessment" item — points at an existing Assignment (any
    # AssignmentType, not just QUIZ; a teacher may want a homework or
    # project attached to a module too). Completion for this item type is
    # derived from Submission.status (see student_portal.py), never
    # manually toggled the way video/material items are.
    assignment_id: Optional[str] = Field(
        default=None,
        sa_column=Column(String, ForeignKey("assignments.id", ondelete="SET NULL"), index=True, nullable=True)
    )
    order_index: int = 0

    created_at: datetime = Field(default_factory=datetime.utcnow)


class CourseModuleItemCreate(SQLModel):
    title: str
    video_url: Optional[str] = None
    learning_material_id: Optional[str] = None
    assignment_id: Optional[str] = None
    order_index: int = 0


class StudentModuleProgress(SQLModel, table=True):
    """A completion checkmark — no quiz/scoring engine. One row per
    (student, module_item) that the student has marked complete;
    unmarking deletes the row rather than tracking history."""
    __tablename__ = "student_module_progress"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    school_id: str = Field(index=True)
    student_id: str = Field(index=True)
    module_item_id: str = Field(sa_column=Column(String, ForeignKey("course_module_items.id", ondelete="CASCADE"), index=True))

    completed_at: datetime = Field(default_factory=datetime.utcnow)
