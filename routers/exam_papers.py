"""Exam paper / question-bank management and moderation router — see
models/exam_papers.py. Setters (teachers + admins) build a paper from
question-bank items or attach an uploaded file, submit it, and only a
SCHOOL_ADMIN/SUPER_ADMIN can approve or reject it (maker-checker). Unlike
routers/discipline.py's suspension approval — the shape this was originally
modeled on, which is role-gated only — approve/reject here additionally
blocks the paper's own author from moderating it: a genuine maker vs.
checker split, not just "an admin signed off," since a wrong/leaked paper
reaching students is a harder-to-undo mistake than most disciplinary
actions."""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.encoders import jsonable_encoder
from sqlmodel import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_current_user, require_permission
from database import get_session
from models.exam_papers import (
    QuestionBankItem, QuestionBankItemCreate, QuestionBankItemUpdate,
    ExamPaper, ExamPaperCreate, ExamPaperUpdate, ExamPaperStatus, ExamPaperModerationDecision,
    ExamPaperQuestion, ExamPaperQuestionAdd,
)
from models.staff import Staff, TeacherAssignment
from models.user import User, UserRole

router = APIRouter(prefix="/exam-papers", tags=["Exam Papers"])


def _school_id(user: User) -> str:
    if not user.school_id:
        raise HTTPException(status_code=403, detail="No school context")
    return user.school_id


async def _assert_teaches_subject(user: User, session: AsyncSession, subject_id: str) -> None:
    """A TEACHER can only author/edit an exam paper for a subject they
    actually teach — mirrors routers/teacher/grades.py's TeacherAssignment
    check for grade recording. exam.paper.manage is a broad, school-wide
    RBAC grant (scripts/seed_permissions.py), so without this any teacher
    could create/edit a paper for a subject they've never been assigned to.
    ExamPaper has no class_id of its own (only an optional
    exam_schedule_id), so this checks "teaches this subject somewhere at
    this school" rather than a specific class+subject pairing — as far as
    the data model supports. No-op for admins."""
    if user.role != UserRole.TEACHER:
        return
    staff_result = await session.execute(select(Staff).where(Staff.school_id == _school_id(user), Staff.user_id == user.id))
    staff = staff_result.scalar_one_or_none()
    if not staff:
        staff_result = await session.execute(select(Staff).where(Staff.school_id == _school_id(user), Staff.email == user.email))
        staff = staff_result.scalar_one_or_none()
    if not staff:
        raise HTTPException(status_code=404, detail="Teacher staff profile not found")
    assignment_result = await session.execute(select(TeacherAssignment).where(
        TeacherAssignment.school_id == _school_id(user),
        TeacherAssignment.staff_id == staff.id,
        TeacherAssignment.subject_id == subject_id,
    ))
    # A teacher can be assigned this subject across multiple classes, so
    # more than one TeacherAssignment row can match here — this only needs
    # to know at least one exists, not which/how many.
    if not assignment_result.scalars().first():
        raise HTTPException(status_code=403, detail="You are not assigned to teach this subject")


# ── Question Bank ────────────────────────────────────────────────────────

@router.post("/question-bank", response_model=dict)
async def create_question(
    data: QuestionBankItemCreate,
    current_user: User = Depends(require_permission("exams.paper_question.manage")),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    item = QuestionBankItem(
        school_id=school_id, created_by=current_user.id,
        **{**data.model_dump(), "question_type": data.question_type.value},
    )
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return jsonable_encoder(item)


@router.get("/question-bank", response_model=List[dict])
async def list_questions(
    subject_id: Optional[str] = None,
    is_active: Optional[bool] = None,
    current_user: User = Depends(require_permission("exams.paper_question.view")),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    query = select(QuestionBankItem).where(QuestionBankItem.school_id == school_id)
    if subject_id:
        query = query.where(QuestionBankItem.subject_id == subject_id)
    if is_active is not None:
        query = query.where(QuestionBankItem.is_active == is_active)
    result = await session.execute(query.order_by(QuestionBankItem.created_at.desc()))
    return [jsonable_encoder(q) for q in result.scalars().all()]


@router.put("/question-bank/{question_id}", response_model=dict)
async def update_question(
    question_id: str,
    data: QuestionBankItemUpdate,
    current_user: User = Depends(require_permission("exams.paper_question.manage")),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    item = (await session.execute(select(QuestionBankItem).where(QuestionBankItem.id == question_id, QuestionBankItem.school_id == school_id))).scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Question not found")
    update_data = data.model_dump(exclude_unset=True)
    if "question_type" in update_data and update_data["question_type"] is not None:
        update_data["question_type"] = update_data["question_type"].value
    for key, value in update_data.items():
        setattr(item, key, value)
    item.updated_at = datetime.utcnow()
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return jsonable_encoder(item)


@router.delete("/question-bank/{question_id}", response_model=dict)
async def delete_question(
    question_id: str,
    current_user: User = Depends(require_permission("exams.paper_question.manage")),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    item = (await session.execute(select(QuestionBankItem).where(QuestionBankItem.id == question_id, QuestionBankItem.school_id == school_id))).scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Question not found")
    await session.delete(item)
    await session.commit()
    return {"success": True, "message": "Question deleted"}


# ── Exam Papers ──────────────────────────────────────────────────────────

async def _get_paper_or_404(session: AsyncSession, school_id: str, paper_id: str) -> ExamPaper:
    item = (await session.execute(select(ExamPaper).where(ExamPaper.id == paper_id, ExamPaper.school_id == school_id))).scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Exam paper not found")
    return item


async def _recompute_total_marks(session: AsyncSession, paper_id: str) -> float:
    result = await session.execute(select(func.coalesce(func.sum(ExamPaperQuestion.marks_allocated), 0)).where(ExamPaperQuestion.exam_paper_id == paper_id))
    return float(result.scalar_one())


@router.post("", response_model=dict)
async def create_paper(
    data: ExamPaperCreate,
    current_user: User = Depends(require_permission("exams.paper.manage")),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    await _assert_teaches_subject(current_user, session, data.subject_id)
    paper = ExamPaper(school_id=school_id, created_by=current_user.id, **data.model_dump())
    session.add(paper)
    await session.commit()
    await session.refresh(paper)
    return jsonable_encoder(paper)


@router.get("", response_model=List[dict])
async def list_papers(
    subject_id: Optional[str] = None,
    status_filter: Optional[str] = None,
    exam_schedule_id: Optional[str] = None,
    current_user: User = Depends(require_permission("exams.paper.view")),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    query = select(ExamPaper).where(ExamPaper.school_id == school_id)
    if subject_id:
        query = query.where(ExamPaper.subject_id == subject_id)
    if status_filter:
        query = query.where(ExamPaper.status == status_filter)
    if exam_schedule_id:
        query = query.where(ExamPaper.exam_schedule_id == exam_schedule_id)
    result = await session.execute(query.order_by(ExamPaper.created_at.desc()))
    return [jsonable_encoder(p) for p in result.scalars().all()]


@router.get("/{paper_id}", response_model=dict)
async def get_paper(
    paper_id: str,
    current_user: User = Depends(require_permission("exams.paper.view")),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    paper = await _get_paper_or_404(session, school_id, paper_id)
    questions_result = await session.execute(
        select(ExamPaperQuestion, QuestionBankItem)
        .join(QuestionBankItem, QuestionBankItem.id == ExamPaperQuestion.question_bank_item_id)
        .where(ExamPaperQuestion.exam_paper_id == paper_id)
        .order_by(ExamPaperQuestion.order_index)
    )
    questions = [
        {**jsonable_encoder(link), "question_text": q.question_text, "question_type": q.question_type, "topic": q.topic}
        for link, q in questions_result.all()
    ]
    return {**jsonable_encoder(paper), "questions": questions}


@router.put("/{paper_id}", response_model=dict)
async def update_paper(
    paper_id: str,
    data: ExamPaperUpdate,
    current_user: User = Depends(require_permission("exams.paper.manage")),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    paper = await _get_paper_or_404(session, school_id, paper_id)
    await _assert_teaches_subject(current_user, session, paper.subject_id)
    if paper.status not in (ExamPaperStatus.DRAFT.value, ExamPaperStatus.REJECTED.value):
        raise HTTPException(status_code=400, detail=f"Cannot edit a paper that is {paper.status}")
    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(paper, key, value)
    paper.updated_at = datetime.utcnow()
    session.add(paper)
    await session.commit()
    await session.refresh(paper)
    return jsonable_encoder(paper)


@router.delete("/{paper_id}", response_model=dict)
async def delete_paper(
    paper_id: str,
    current_user: User = Depends(require_permission("exams.paper.manage")),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    paper = await _get_paper_or_404(session, school_id, paper_id)
    await _assert_teaches_subject(current_user, session, paper.subject_id)
    if paper.status == ExamPaperStatus.APPROVED.value:
        raise HTTPException(status_code=400, detail="Cannot delete an approved paper")
    await session.delete(paper)
    await session.commit()
    return {"success": True, "message": "Exam paper deleted"}


@router.post("/{paper_id}/questions", response_model=dict)
async def add_question_to_paper(
    paper_id: str,
    data: ExamPaperQuestionAdd,
    current_user: User = Depends(require_permission("exams.paper.manage")),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    paper = await _get_paper_or_404(session, school_id, paper_id)
    await _assert_teaches_subject(current_user, session, paper.subject_id)
    if paper.status not in (ExamPaperStatus.DRAFT.value, ExamPaperStatus.REJECTED.value):
        raise HTTPException(status_code=400, detail=f"Cannot edit a paper that is {paper.status}")

    question = (await session.execute(select(QuestionBankItem).where(QuestionBankItem.id == data.question_bank_item_id, QuestionBankItem.school_id == school_id))).scalar_one_or_none()
    if not question:
        raise HTTPException(status_code=404, detail="Question not found")

    link = ExamPaperQuestion(exam_paper_id=paper_id, **data.model_dump())
    session.add(link)
    await session.flush()
    paper.total_marks = await _recompute_total_marks(session, paper_id)
    paper.updated_at = datetime.utcnow()
    session.add(paper)
    await session.commit()
    await session.refresh(link)
    return jsonable_encoder(link)


@router.delete("/{paper_id}/questions/{link_id}", response_model=dict)
async def remove_question_from_paper(
    paper_id: str,
    link_id: str,
    current_user: User = Depends(require_permission("exams.paper.manage")),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    paper = await _get_paper_or_404(session, school_id, paper_id)
    await _assert_teaches_subject(current_user, session, paper.subject_id)
    link = (await session.execute(select(ExamPaperQuestion).where(ExamPaperQuestion.id == link_id, ExamPaperQuestion.exam_paper_id == paper_id))).scalar_one_or_none()
    if not link:
        raise HTTPException(status_code=404, detail="Question is not on this paper")
    await session.delete(link)
    await session.flush()
    paper.total_marks = await _recompute_total_marks(session, paper_id)
    paper.updated_at = datetime.utcnow()
    session.add(paper)
    await session.commit()
    return {"success": True, "message": "Question removed from paper"}


@router.post("/{paper_id}/submit", response_model=dict)
async def submit_paper(
    paper_id: str,
    current_user: User = Depends(require_permission("exams.paper.submit")),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    paper = await _get_paper_or_404(session, school_id, paper_id)
    await _assert_teaches_subject(current_user, session, paper.subject_id)
    if paper.status not in (ExamPaperStatus.DRAFT.value, ExamPaperStatus.REJECTED.value):
        raise HTTPException(status_code=400, detail=f"Cannot submit a paper that is {paper.status}")
    paper.status = ExamPaperStatus.SUBMITTED.value
    paper.submitted_at = datetime.utcnow()
    paper.moderated_by = None
    paper.moderated_at = None
    paper.moderation_notes = None
    paper.updated_at = datetime.utcnow()
    session.add(paper)
    await session.commit()
    await session.refresh(paper)
    return jsonable_encoder(paper)


@router.post("/{paper_id}/approve", response_model=dict)
async def approve_paper(
    paper_id: str,
    data: ExamPaperModerationDecision,
    current_user: User = Depends(require_permission("exams.paper.moderate")),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    paper = await _get_paper_or_404(session, school_id, paper_id)
    if paper.status != ExamPaperStatus.SUBMITTED.value:
        raise HTTPException(status_code=400, detail=f"Only a submitted paper can be approved (currently {paper.status})")
    if paper.created_by == current_user.id:
        raise HTTPException(status_code=403, detail="You cannot approve a paper you authored — ask another admin to moderate it")
    paper.status = ExamPaperStatus.APPROVED.value
    paper.moderated_by = current_user.id
    paper.moderated_at = datetime.utcnow()
    paper.moderation_notes = data.notes
    paper.updated_at = datetime.utcnow()
    session.add(paper)
    await session.commit()
    await session.refresh(paper)
    return jsonable_encoder(paper)


@router.post("/{paper_id}/reject", response_model=dict)
async def reject_paper(
    paper_id: str,
    data: ExamPaperModerationDecision,
    current_user: User = Depends(require_permission("exams.paper.moderate")),
    session: AsyncSession = Depends(get_session),
):
    school_id = _school_id(current_user)
    paper = await _get_paper_or_404(session, school_id, paper_id)
    if paper.status != ExamPaperStatus.SUBMITTED.value:
        raise HTTPException(status_code=400, detail=f"Only a submitted paper can be rejected (currently {paper.status})")
    if paper.created_by == current_user.id:
        raise HTTPException(status_code=403, detail="You cannot reject a paper you authored — ask another admin to moderate it")
    paper.status = ExamPaperStatus.REJECTED.value
    paper.moderated_by = current_user.id
    paper.moderated_at = datetime.utcnow()
    paper.moderation_notes = data.notes
    paper.updated_at = datetime.utcnow()
    session.add(paper)
    await session.commit()
    await session.refresh(paper)
    return jsonable_encoder(paper)
