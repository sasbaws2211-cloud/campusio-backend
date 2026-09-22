"""Satisfaction surveys, feedback forms, and evaluation responses."""
import json
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_current_user, require_roles
from database import get_session
from models.survey import Survey, SurveyCreate, SurveyUpdate, SurveyResponse, SurveyResponseCreate
from models.user import User, UserRole
from services.plan_gating import require_plan_feature

router = APIRouter(
    prefix="/surveys", tags=["Surveys & Feedback"],
    dependencies=[Depends(require_plan_feature("comms_plus"))],
)
ADMIN_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.HR, UserRole.TEACHER)


def school_id(user: User) -> str:
    if not user.school_id:
        raise HTTPException(status_code=403, detail="No school context")
    return user.school_id


@router.get("", response_model=list[dict])
async def list_surveys(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(Survey).where(Survey.school_id == school_id(user)).order_by(Survey.created_at.desc()))
    surveys = result.scalars().all()
    response = []
    for survey in surveys:
        count = (await session.execute(select(func.count(SurveyResponse.id)).where(SurveyResponse.survey_id == survey.id))).scalar() or 0
        item = survey.model_dump()
        item["response_count"] = count
        response.append(item)
    return response


@router.get("/summary", response_model=dict)
async def surveys_summary(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    scope = school_id(user)
    surveys = (await session.execute(select(Survey).where(Survey.school_id == scope))).scalars().all()
    responses = (await session.execute(select(SurveyResponse).where(SurveyResponse.school_id == scope))).scalars().all()
    ratings = [item.rating for item in responses if item.rating is not None]
    return {
        "surveys_total": len(surveys),
        "surveys_published": sum(1 for survey in surveys if survey.status == "published"),
        "responses_total": len(responses),
        "average_rating": sum(ratings) / len(ratings) if ratings else None,
    }


@router.post("", response_model=dict)
async def create_survey(payload: SurveyCreate, user: User = Depends(require_roles(*ADMIN_ROLES)), session: AsyncSession = Depends(get_session)):
    try:
        questions = json.loads(payload.questions_json)
    except json.JSONDecodeError:
        raise HTTPException(status_code=422, detail="questions_json must be valid JSON")
    if not isinstance(questions, list):
        raise HTTPException(status_code=422, detail="questions_json must be a JSON array")
    survey = Survey(school_id=school_id(user), created_by=user.id, **payload.model_dump())
    session.add(survey)
    await session.commit()
    await session.refresh(survey)
    return survey.model_dump()


@router.patch("/{survey_id}", response_model=dict)
async def update_survey(survey_id: str, payload: SurveyUpdate, user: User = Depends(require_roles(*ADMIN_ROLES)), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(Survey).where(Survey.id == survey_id, Survey.school_id == school_id(user)))
    survey = result.scalar_one_or_none()
    if not survey:
        raise HTTPException(status_code=404, detail="Survey not found")
    changes = payload.model_dump(exclude_unset=True)
    if "questions_json" in changes:
        try:
            questions = json.loads(changes["questions_json"])
        except json.JSONDecodeError:
            raise HTTPException(status_code=422, detail="questions_json must be valid JSON")
        if not isinstance(questions, list):
            raise HTTPException(status_code=422, detail="questions_json must be a JSON array")
    for key, value in changes.items():
        setattr(survey, key, value)
    survey.updated_at = datetime.utcnow()
    session.add(survey)
    await session.commit()
    await session.refresh(survey)
    return survey.model_dump()


@router.post("/{survey_id}/responses", response_model=dict)
async def submit_response(survey_id: str, payload: SurveyResponseCreate, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(Survey).where(Survey.id == survey_id, Survey.school_id == school_id(user)))
    survey = result.scalar_one_or_none()
    if not survey or survey.status != "published":
        raise HTTPException(status_code=404, detail="Published survey not found")
    if not survey.anonymous:
        existing = await session.execute(select(SurveyResponse).where(SurveyResponse.survey_id == survey.id, SurveyResponse.respondent_id == user.id))
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="You have already submitted this survey")
    response = SurveyResponse(school_id=school_id(user), survey_id=survey.id, respondent_id=None if survey.anonymous else user.id, **payload.model_dump())
    session.add(response)
    await session.commit()
    await session.refresh(response)
    return response.model_dump()


@router.get("/{survey_id}/results", response_model=dict)
async def survey_results(survey_id: str, user: User = Depends(require_roles(*ADMIN_ROLES)), session: AsyncSession = Depends(get_session)):
    survey_result = await session.execute(select(Survey).where(Survey.id == survey_id, Survey.school_id == school_id(user)))
    if not survey_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Survey not found")
    result = await session.execute(select(SurveyResponse).where(SurveyResponse.survey_id == survey_id, SurveyResponse.school_id == school_id(user)).order_by(SurveyResponse.submitted_at.desc()))
    responses = result.scalars().all()
    ratings = [item.rating for item in responses if item.rating is not None]
    return {"survey_id": survey_id, "response_count": len(responses), "average_rating": sum(ratings) / len(ratings) if ratings else None, "responses": [item.model_dump() for item in responses]}