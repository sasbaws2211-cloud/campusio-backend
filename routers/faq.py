"""FAQ / knowledge-base articles. Any authenticated school user can browse
published articles; only admin/HR can author or unpublish."""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_current_user, require_roles
from database import get_session
from models.faq import FaqArticle, FaqArticleCreate, FaqArticleUpdate
from models.user import User, UserRole

router = APIRouter(prefix="/faq", tags=["FAQ"])
WRITE_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.HR)


def _school_id(user: User) -> str:
    if not user.school_id:
        raise HTTPException(status_code=403, detail="No school context")
    return user.school_id


def _serialize(item: FaqArticle) -> dict:
    return {
        "id": item.id, "category": item.category, "question": item.question, "answer": item.answer,
        "is_published": item.is_published, "view_count": item.view_count, "created_at": item.created_at,
    }


@router.get("", response_model=list[dict])
async def list_faq(category: str | None = None, current_user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    query = select(FaqArticle).where(FaqArticle.school_id == _school_id(current_user))
    if current_user.role not in WRITE_ROLES:
        query = query.where(FaqArticle.is_published == True)  # noqa: E712
    if category:
        query = query.where(FaqArticle.category == category)
    result = await session.execute(query.order_by(FaqArticle.category, FaqArticle.created_at.desc()))
    return [_serialize(item) for item in result.scalars().all()]


@router.get("/categories", response_model=list[str])
async def list_categories(current_user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    query = select(FaqArticle.category).where(FaqArticle.school_id == _school_id(current_user)).distinct()
    if current_user.role not in WRITE_ROLES:
        query = query.where(FaqArticle.is_published == True)  # noqa: E712
    result = await session.execute(query)
    return sorted(result.scalars().all())


@router.post("", response_model=dict)
async def create_faq(payload: FaqArticleCreate, current_user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    item = FaqArticle(school_id=_school_id(current_user), created_by=current_user.id, **payload.model_dump())
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return _serialize(item)


@router.patch("/{faq_id}", response_model=dict)
async def update_faq(faq_id: str, payload: FaqArticleUpdate, current_user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    item = (await session.execute(select(FaqArticle).where(FaqArticle.id == faq_id, FaqArticle.school_id == _school_id(current_user)))).scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="FAQ article not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(item, key, value)
    item.updated_at = datetime.utcnow()
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return _serialize(item)


@router.delete("/{faq_id}", response_model=dict)
async def delete_faq(faq_id: str, current_user: User = Depends(require_roles(*WRITE_ROLES)), session: AsyncSession = Depends(get_session)):
    item = (await session.execute(select(FaqArticle).where(FaqArticle.id == faq_id, FaqArticle.school_id == _school_id(current_user)))).scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="FAQ article not found")
    await session.delete(item)
    await session.commit()
    return {"success": True, "message": "FAQ article deleted"}


@router.post("/{faq_id}/view", response_model=dict)
async def record_view(faq_id: str, current_user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    item = (await session.execute(select(FaqArticle).where(FaqArticle.id == faq_id, FaqArticle.school_id == _school_id(current_user)))).scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="FAQ article not found")
    item.view_count += 1
    session.add(item)
    await session.commit()
    return {"view_count": item.view_count}
