"""E-library API routes: standard digital-library feature set.

Covers catalog CRUD (categories, tags, items), role-aware browsing with
pagination/search/sort, view/download analytics, favorites, and ratings.
Physical circulation (ISBN copies, borrow/return, fines) is out of scope —
this remains a digital resource library.
"""
from __future__ import annotations

import io
import uuid
from datetime import datetime, timedelta
from typing import List, Optional
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from sqlmodel import select
from sqlalchemy import delete, func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from PIL import Image

from auth import get_current_user, require_roles
from database import get_session
from dependencies import get_current_school_id
from models.library import (
    LibraryCategory,
    LibraryCategoryCreate,
    LibraryCategoryUpdate,
    LibraryInteractionType,
    LibraryItem,
    LibraryItemAccessSession,
    LibraryItemClass,
    LibraryItemFavorite,
    LibraryItemInteraction,
    LibraryItemRating,
    LibraryItemRatingCreate,
    LibraryItemTag,
    LibraryTag,
)
from models.classroom import Class as Classroom
from models.library_circulation import LibraryBookCopy, CopyStatus
from models.student import Student
from models.user import User, UserRole

router = APIRouter(prefix="/library", tags=["Library"])

UPLOAD_DIR = Path("uploads/library")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

LIBRARY_ADMIN_ROLES = (UserRole.SCHOOL_ADMIN, UserRole.SUPER_ADMIN)
LIBRARY_UPLOAD_ROLES = LIBRARY_ADMIN_ROLES + (UserRole.TEACHER,)
# Everyone except students/parents counts as "staff" for LibraryItem.staff_only
# — deliberately the whole staff-role roster (nurse, registrar, counselor,
# ...), not just teachers, since a staff-only resource (e.g. an HR policy
# PDF) isn't necessarily teaching material.
NON_STAFF_ROLES = (UserRole.STUDENT, UserRole.PARENT)
ACCESS_SESSION_TTL_MINUTES = 30

ALLOWED_ITEM_CONTENT_TYPES = {
    "application/pdf",
    "video/mp4", "video/webm", "video/quicktime", "video/x-msvideo",
    "audio/mpeg", "audio/mp3", "audio/wav", "audio/ogg", "audio/x-wav",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "text/plain",
}
MAX_ITEM_FILE_SIZE_BYTES = 200 * 1024 * 1024  # 200 MB

ALLOWED_THUMBNAIL_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_THUMBNAIL_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB
THUMBNAIL_MAX_DIMENSION = 480


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _save_validated_upload(file: UploadFile, school_id: str) -> str:
    if file.content_type not in ALLOWED_ITEM_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported file type: {file.content_type}.",
        )
    content = await file.read()
    if len(content) > MAX_ITEM_FILE_SIZE_BYTES:
        raise HTTPException(status_code=400, detail="File exceeds the 200 MB limit.")
    filename = f"{uuid.uuid4()}_{Path(file.filename or 'upload').name}"
    target_path = UPLOAD_DIR / school_id / filename
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_bytes(content)
    return f"/uploads/library/{school_id}/{filename}"


async def _save_thumbnail(file: UploadFile, school_id: str) -> str:
    if file.content_type not in ALLOWED_THUMBNAIL_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported thumbnail type: {file.content_type}. Allowed: JPEG, PNG, WEBP.",
        )
    content = await file.read()
    if len(content) > MAX_THUMBNAIL_SIZE_BYTES:
        raise HTTPException(status_code=400, detail="Thumbnail exceeds the 5 MB limit.")
    try:
        image = Image.open(io.BytesIO(content))
        image = image.convert("RGB")
        image.thumbnail((THUMBNAIL_MAX_DIMENSION, THUMBNAIL_MAX_DIMENSION))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=85)
        processed = buffer.getvalue()
    except Exception:
        raise HTTPException(status_code=400, detail="Uploaded thumbnail is not a valid image.")
    filename = f"{uuid.uuid4()}.jpg"
    target_path = UPLOAD_DIR / school_id / "thumbnails" / filename
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_bytes(processed)
    return f"/uploads/library/{school_id}/thumbnails/{filename}"


async def _get_student_class_id(session: AsyncSession, current_user: User) -> Optional[str]:
    if current_user.role != UserRole.STUDENT:
        return None
    result = await session.execute(select(Student).where(Student.user_id == current_user.id))
    student = result.scalar_one_or_none()
    return student.class_id if student else None


async def _get_item_or_404(session: AsyncSession, item_id: str, school_id: str) -> LibraryItem:
    result = await session.execute(
        select(LibraryItem).where(LibraryItem.id == item_id, LibraryItem.school_id == school_id)
    )
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Library item not found")
    return item


async def _ensure_visible(session: AsyncSession, item: LibraryItem, current_user: User) -> None:
    """Raise 404 (not 403, to avoid leaking existence) if the item isn't visible to this user."""
    if current_user.role in LIBRARY_ADMIN_ROLES:
        return
    if item.staff_only and current_user.role in NON_STAFF_ROLES:
        raise HTTPException(status_code=404, detail="Library item not found")
    if current_user.role == UserRole.TEACHER:
        if item.is_published or item.created_by == current_user.id:
            return
        raise HTTPException(status_code=404, detail="Library item not found")
    if not item.is_published:
        raise HTTPException(status_code=404, detail="Library item not found")
    class_ids = (
        await session.execute(select(LibraryItemClass.class_id).where(LibraryItemClass.item_id == item.id))
    ).scalars().all()
    if class_ids:
        student_class_id = await _get_student_class_id(session, current_user)
        if student_class_id not in class_ids:
            raise HTTPException(status_code=404, detail="Library item not found")


def _ensure_owner_or_admin(item: LibraryItem, current_user: User) -> None:
    if current_user.role in LIBRARY_ADMIN_ROLES:
        return
    if current_user.role == UserRole.TEACHER and item.created_by == current_user.id:
        return
    raise HTTPException(status_code=403, detail="You can only modify library items you created")


async def _sync_item_classes(session: AsyncSession, item_id: str, class_ids: List[str], school_id: str) -> None:
    await session.execute(delete(LibraryItemClass).where(LibraryItemClass.item_id == item_id))
    cleaned = list({c.strip() for c in class_ids if c and c.strip()})
    if not cleaned:
        return
    valid_ids = (
        await session.execute(
            select(Classroom.id).where(Classroom.id.in_(cleaned), Classroom.school_id == school_id)
        )
    ).scalars().all()
    for class_id in valid_ids:
        session.add(LibraryItemClass(item_id=item_id, class_id=class_id))


async def _sync_item_tags(session: AsyncSession, item_id: str, tag_names: List[str], school_id: str) -> None:
    await session.execute(delete(LibraryItemTag).where(LibraryItemTag.item_id == item_id))
    seen = set()
    names = []
    for raw in tag_names:
        name = (raw or "").strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        names.append(name)
    for name in names:
        tag = (
            await session.execute(
                select(LibraryTag).where(LibraryTag.school_id == school_id, func.lower(LibraryTag.name) == name.lower())
            )
        ).scalar_one_or_none()
        if not tag:
            tag = LibraryTag(school_id=school_id, name=name, slug=name.lower().replace(" ", "-"))
            session.add(tag)
            await session.flush()
        session.add(LibraryItemTag(item_id=item_id, tag_id=tag.id))


async def _serialize_items(session: AsyncSession, items: List[LibraryItem], current_user_id: Optional[str] = None) -> List[dict]:
    if not items:
        return []
    item_ids = [item.id for item in items]

    tag_rows = (
        await session.execute(
            select(LibraryItemTag.item_id, LibraryTag.name)
            .join(LibraryTag, LibraryTag.id == LibraryItemTag.tag_id)
            .where(LibraryItemTag.item_id.in_(item_ids))
        )
    ).all()
    tags_by_item: dict = {}
    for item_id, name in tag_rows:
        tags_by_item.setdefault(item_id, []).append(name)

    class_rows = (
        await session.execute(
            select(LibraryItemClass.item_id, LibraryItemClass.class_id).where(LibraryItemClass.item_id.in_(item_ids))
        )
    ).all()
    classes_by_item: dict = {}
    for item_id, class_id in class_rows:
        classes_by_item.setdefault(item_id, []).append(class_id)

    rating_rows = (
        await session.execute(
            select(LibraryItemRating.item_id, func.avg(LibraryItemRating.rating), func.count(LibraryItemRating.id))
            .where(LibraryItemRating.item_id.in_(item_ids))
            .group_by(LibraryItemRating.item_id)
        )
    ).all()
    ratings_by_item = {item_id: (round(float(avg), 2), count) for item_id, avg, count in rating_rows}

    favorited_ids = set()
    if current_user_id:
        favorited_ids = set(
            (
                await session.execute(
                    select(LibraryItemFavorite.item_id).where(
                        LibraryItemFavorite.item_id.in_(item_ids),
                        LibraryItemFavorite.user_id == current_user_id,
                    )
                )
            ).scalars().all()
        )

    category_ids = {item.category_id for item in items if item.category_id}
    categories_by_id: dict = {}
    if category_ids:
        cat_rows = (
            await session.execute(select(LibraryCategory).where(LibraryCategory.id.in_(category_ids)))
        ).scalars().all()
        categories_by_id = {c.id: c.name for c in cat_rows}

    copy_rows = (
        await session.execute(
            select(LibraryBookCopy.item_id, LibraryBookCopy.status).where(LibraryBookCopy.item_id.in_(item_ids))
        )
    ).all()
    copies_by_item: dict = {}
    for item_id, copy_status in copy_rows:
        totals = copies_by_item.setdefault(item_id, {"total": 0, "available": 0})
        totals["total"] += 1
        if copy_status == CopyStatus.AVAILABLE.value:
            totals["available"] += 1

    results = []
    for item in items:
        avg_rating, ratings_count = ratings_by_item.get(item.id, (None, 0))
        copy_totals = copies_by_item.get(item.id, {"total": 0, "available": 0})
        results.append(
            {
                "id": item.id,
                "school_id": item.school_id,
                "title": item.title,
                "description": item.description,
                "material_type": item.material_type,
                "content_type": item.content_type,
                "category_id": item.category_id,
                "category_name": categories_by_id.get(item.category_id),
                "subject_id": item.subject_id,
                "author": item.author,
                "publisher": item.publisher,
                "edition": item.edition,
                "language": item.language,
                "isbn": item.isbn,
                "publication_year": item.publication_year,
                "class_ids": classes_by_item.get(item.id, []),
                "tags": tags_by_item.get(item.id, []),
                "file_url": item.file_url,
                "thumbnail_url": item.thumbnail_url,
                "external_url": item.external_url,
                "view_count": item.view_count,
                "download_count": item.download_count,
                "copies_total": copy_totals["total"],
                "copies_available": copy_totals["available"],
                "average_rating": avg_rating,
                "ratings_count": ratings_count,
                "is_favorited": item.id in favorited_ids,
                "is_published": item.is_published,
                "is_featured": item.is_featured,
                "staff_only": item.staff_only,
                "max_concurrent_readers": item.max_concurrent_readers,
                "created_by": item.created_by,
                "updated_by": item.updated_by,
                "created_at": item.created_at,
                "updated_at": item.updated_at,
            }
        )
    return results


def _category_to_dict(category: LibraryCategory) -> dict:
    return {
        "id": category.id,
        "name": category.name,
        "slug": category.slug,
        "description": category.description,
        "parent_id": category.parent_id,
    }


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------

@router.get("/categories", response_model=List[dict])
async def list_categories(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    school_id = await get_current_school_id(current_user)
    result = await session.execute(
        select(LibraryCategory).where(LibraryCategory.school_id == school_id).order_by(LibraryCategory.name.asc())
    )
    return [_category_to_dict(c) for c in result.scalars().all()]


@router.post("/categories", response_model=dict, status_code=status.HTTP_201_CREATED)
async def create_category(
    payload: LibraryCategoryCreate,
    current_user: User = Depends(require_roles(*LIBRARY_ADMIN_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = await get_current_school_id(current_user)
    if payload.parent_id:
        parent = (
            await session.execute(
                select(LibraryCategory).where(LibraryCategory.id == payload.parent_id, LibraryCategory.school_id == school_id)
            )
        ).scalar_one_or_none()
        if not parent:
            raise HTTPException(status_code=400, detail="Parent category not found")
    slug = payload.slug or payload.name.lower().replace(" ", "-")
    category = LibraryCategory(
        school_id=school_id, name=payload.name, slug=slug, description=payload.description, parent_id=payload.parent_id
    )
    session.add(category)
    await session.commit()
    await session.refresh(category)
    return _category_to_dict(category)


@router.put("/categories/{category_id}", response_model=dict)
async def update_category(
    category_id: str,
    payload: LibraryCategoryUpdate,
    current_user: User = Depends(require_roles(*LIBRARY_ADMIN_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = await get_current_school_id(current_user)
    category = (
        await session.execute(
            select(LibraryCategory).where(LibraryCategory.id == category_id, LibraryCategory.school_id == school_id)
        )
    ).scalar_one_or_none()
    if not category:
        raise HTTPException(status_code=404, detail="Category not found")

    if payload.parent_id is not None:
        if payload.parent_id == category_id:
            raise HTTPException(status_code=400, detail="A category cannot be its own parent")
        if payload.parent_id:
            parent = (
                await session.execute(
                    select(LibraryCategory).where(LibraryCategory.id == payload.parent_id, LibraryCategory.school_id == school_id)
                )
            ).scalar_one_or_none()
            if not parent:
                raise HTTPException(status_code=400, detail="Parent category not found")
        category.parent_id = payload.parent_id or None

    if payload.name is not None:
        category.name = payload.name
    if payload.slug is not None:
        category.slug = payload.slug
    if payload.description is not None:
        category.description = payload.description
    category.updated_at = datetime.utcnow()

    await session.commit()
    await session.refresh(category)
    return _category_to_dict(category)


@router.delete("/categories/{category_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_category(
    category_id: str,
    current_user: User = Depends(require_roles(*LIBRARY_ADMIN_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = await get_current_school_id(current_user)
    category = (
        await session.execute(
            select(LibraryCategory).where(LibraryCategory.id == category_id, LibraryCategory.school_id == school_id)
        )
    ).scalar_one_or_none()
    if not category:
        raise HTTPException(status_code=404, detail="Category not found")
    await session.delete(category)
    await session.commit()


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------

@router.get("/tags", response_model=List[dict])
async def list_tags(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    school_id = await get_current_school_id(current_user)
    result = await session.execute(select(LibraryTag).where(LibraryTag.school_id == school_id).order_by(LibraryTag.name.asc()))
    return [{"id": t.id, "name": t.name, "slug": t.slug} for t in result.scalars().all()]


@router.delete("/tags/{tag_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_tag(
    tag_id: str,
    current_user: User = Depends(require_roles(*LIBRARY_ADMIN_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = await get_current_school_id(current_user)
    tag = (
        await session.execute(select(LibraryTag).where(LibraryTag.id == tag_id, LibraryTag.school_id == school_id))
    ).scalar_one_or_none()
    if not tag:
        raise HTTPException(status_code=404, detail="Tag not found")
    await session.delete(tag)
    await session.commit()


# ---------------------------------------------------------------------------
# Items — unified, role-aware listing
# ---------------------------------------------------------------------------

@router.get("/items", response_model=dict)
async def list_items(
    search: Optional[str] = Query(default=None),
    category_id: Optional[str] = Query(default=None),
    tag_id: Optional[str] = Query(default=None),
    material_type: Optional[str] = Query(default=None),
    content_type: Optional[str] = Query(default=None),
    subject_id: Optional[str] = Query(default=None),
    class_id: Optional[str] = Query(default=None),
    is_published: Optional[bool] = Query(default=None),
    mine: bool = Query(default=False),
    sort_by: str = Query(default="newest"),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    school_id = await get_current_school_id(current_user)
    stmt = select(LibraryItem).where(LibraryItem.school_id == school_id)
    count_stmt = select(func.count(LibraryItem.id)).where(LibraryItem.school_id == school_id)

    if current_user.role in LIBRARY_ADMIN_ROLES:
        if is_published is not None:
            stmt = stmt.where(LibraryItem.is_published == is_published)
            count_stmt = count_stmt.where(LibraryItem.is_published == is_published)
        if mine:
            stmt = stmt.where(LibraryItem.created_by == current_user.id)
            count_stmt = count_stmt.where(LibraryItem.created_by == current_user.id)
    elif current_user.role == UserRole.TEACHER:
        if mine:
            stmt = stmt.where(LibraryItem.created_by == current_user.id)
            count_stmt = count_stmt.where(LibraryItem.created_by == current_user.id)
        else:
            visibility = or_(LibraryItem.is_published == True, LibraryItem.created_by == current_user.id)  # noqa: E712
            stmt = stmt.where(visibility)
            count_stmt = count_stmt.where(visibility)
    else:
        stmt = stmt.where(LibraryItem.is_published == True)  # noqa: E712
        count_stmt = count_stmt.where(LibraryItem.is_published == True)  # noqa: E712
        no_class_restriction = ~(
            select(LibraryItemClass.id).where(LibraryItemClass.item_id == LibraryItem.id).exists()
        )
        student_class_id = await _get_student_class_id(session, current_user)
        if student_class_id:
            class_visible = or_(
                no_class_restriction,
                select(LibraryItemClass.id)
                .where(LibraryItemClass.item_id == LibraryItem.id, LibraryItemClass.class_id == student_class_id)
                .exists(),
            )
        else:
            class_visible = no_class_restriction
        stmt = stmt.where(class_visible)
        count_stmt = count_stmt.where(class_visible)

    if search:
        term = f"%{search}%"
        tag_match = select(LibraryItemTag.item_id).join(LibraryTag, LibraryTag.id == LibraryItemTag.tag_id).where(
            LibraryTag.name.ilike(term)
        )
        search_filter = or_(
            LibraryItem.title.ilike(term),
            LibraryItem.description.ilike(term),
            LibraryItem.id.in_(tag_match),
        )
        stmt = stmt.where(search_filter)
        count_stmt = count_stmt.where(search_filter)

    if category_id:
        stmt = stmt.where(LibraryItem.category_id == category_id)
        count_stmt = count_stmt.where(LibraryItem.category_id == category_id)
    if material_type:
        stmt = stmt.where(LibraryItem.material_type == material_type)
        count_stmt = count_stmt.where(LibraryItem.material_type == material_type)
    if content_type:
        stmt = stmt.where(LibraryItem.content_type == content_type)
        count_stmt = count_stmt.where(LibraryItem.content_type == content_type)
    if subject_id:
        stmt = stmt.where(LibraryItem.subject_id == subject_id)
        count_stmt = count_stmt.where(LibraryItem.subject_id == subject_id)
    if tag_id:
        tag_filter = LibraryItem.id.in_(select(LibraryItemTag.item_id).where(LibraryItemTag.tag_id == tag_id))
        stmt = stmt.where(tag_filter)
        count_stmt = count_stmt.where(tag_filter)
    if class_id:
        class_filter = LibraryItem.id.in_(select(LibraryItemClass.item_id).where(LibraryItemClass.class_id == class_id))
        stmt = stmt.where(class_filter)
        count_stmt = count_stmt.where(class_filter)

    total = (await session.execute(count_stmt)).scalar() or 0

    if sort_by == "views":
        stmt = stmt.order_by(LibraryItem.view_count.desc())
    elif sort_by == "downloads":
        stmt = stmt.order_by(LibraryItem.download_count.desc())
    elif sort_by == "rating":
        rating_subq = (
            select(func.coalesce(func.avg(LibraryItemRating.rating), 0))
            .where(LibraryItemRating.item_id == LibraryItem.id)
            .scalar_subquery()
        )
        stmt = stmt.order_by(rating_subq.desc())
    else:
        stmt = stmt.order_by(LibraryItem.created_at.desc())

    offset = (page - 1) * limit
    stmt = stmt.offset(offset).limit(limit)

    items = (await session.execute(stmt)).scalars().all()
    serialized = await _serialize_items(session, items, current_user.id)

    return {
        "items": serialized,
        "total": total,
        "page": page,
        "limit": limit,
        "pages": (total + limit - 1) // limit if total else 0,
    }


@router.post("/items", response_model=dict, status_code=status.HTTP_201_CREATED)
async def create_item(
    title: str = Form(...),
    description: Optional[str] = Form(default=None),
    material_type: str = Form(default="book"),
    content_type: str = Form(default="pdf"),
    category_id: Optional[str] = Form(default=None),
    subject_id: Optional[str] = Form(default=None),
    author: Optional[str] = Form(default=None),
    publisher: Optional[str] = Form(default=None),
    edition: Optional[str] = Form(default=None),
    language: Optional[str] = Form(default=None),
    isbn: Optional[str] = Form(default=None),
    publication_year: Optional[int] = Form(default=None),
    external_url: Optional[str] = Form(default=None),
    is_published: bool = Form(default=True),
    is_featured: bool = Form(default=False),
    staff_only: bool = Form(default=False),
    max_concurrent_readers: Optional[int] = Form(default=None),
    class_ids: List[str] = Form(default=[]),
    tags: List[str] = Form(default=[]),
    file: Optional[UploadFile] = File(default=None),
    thumbnail: Optional[UploadFile] = File(default=None),
    current_user: User = Depends(require_roles(*LIBRARY_UPLOAD_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = await get_current_school_id(current_user)

    file_url = await _save_validated_upload(file, school_id) if file is not None and file.filename else None
    thumbnail_url = await _save_thumbnail(thumbnail, school_id) if thumbnail is not None and thumbnail.filename else None

    item = LibraryItem(
        school_id=school_id,
        title=title,
        description=description,
        material_type=material_type,
        content_type=content_type,
        category_id=category_id or None,
        subject_id=subject_id or None,
        author=author,
        publisher=publisher,
        edition=edition,
        language=language,
        isbn=isbn,
        publication_year=publication_year,
        file_url=file_url,
        thumbnail_url=thumbnail_url,
        external_url=external_url,
        is_published=is_published,
        is_featured=is_featured,
        staff_only=staff_only,
        max_concurrent_readers=max_concurrent_readers,
        created_by=current_user.id,
        updated_by=current_user.id,
    )
    session.add(item)
    await session.flush()

    await _sync_item_classes(session, item.id, class_ids, school_id)
    await _sync_item_tags(session, item.id, tags, school_id)

    await session.commit()
    await session.refresh(item)
    return (await _serialize_items(session, [item], current_user.id))[0]


@router.get("/items/{item_id}", response_model=dict)
async def get_item(
    item_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    school_id = await get_current_school_id(current_user)
    item = await _get_item_or_404(session, item_id, school_id)
    await _ensure_visible(session, item, current_user)
    return (await _serialize_items(session, [item], current_user.id))[0]


@router.put("/items/{item_id}", response_model=dict)
async def update_item(
    item_id: str,
    title: Optional[str] = Form(default=None),
    description: Optional[str] = Form(default=None),
    material_type: Optional[str] = Form(default=None),
    content_type: Optional[str] = Form(default=None),
    category_id: Optional[str] = Form(default=None),
    subject_id: Optional[str] = Form(default=None),
    author: Optional[str] = Form(default=None),
    publisher: Optional[str] = Form(default=None),
    edition: Optional[str] = Form(default=None),
    language: Optional[str] = Form(default=None),
    isbn: Optional[str] = Form(default=None),
    publication_year: Optional[int] = Form(default=None),
    external_url: Optional[str] = Form(default=None),
    is_published: Optional[bool] = Form(default=None),
    is_featured: Optional[bool] = Form(default=None),
    staff_only: Optional[bool] = Form(default=None),
    max_concurrent_readers: Optional[int] = Form(default=None),
    max_concurrent_readers_provided: bool = Form(default=False),
    class_ids: List[str] = Form(default=[]),
    class_ids_provided: bool = Form(default=False),
    tags: List[str] = Form(default=[]),
    tags_provided: bool = Form(default=False),
    file: Optional[UploadFile] = File(default=None),
    thumbnail: Optional[UploadFile] = File(default=None),
    current_user: User = Depends(require_roles(*LIBRARY_UPLOAD_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = await get_current_school_id(current_user)
    item = await _get_item_or_404(session, item_id, school_id)
    _ensure_owner_or_admin(item, current_user)

    if file is not None and file.filename:
        item.file_url = await _save_validated_upload(file, school_id)
    if thumbnail is not None and thumbnail.filename:
        item.thumbnail_url = await _save_thumbnail(thumbnail, school_id)

    if title is not None:
        item.title = title
    if description is not None:
        item.description = description
    if material_type is not None:
        item.material_type = material_type
    if content_type is not None:
        item.content_type = content_type
    if category_id is not None:
        item.category_id = category_id or None
    if subject_id is not None:
        item.subject_id = subject_id or None
    if author is not None:
        item.author = author
    if publisher is not None:
        item.publisher = publisher
    if edition is not None:
        item.edition = edition
    if language is not None:
        item.language = language
    if isbn is not None:
        item.isbn = isbn
    if publication_year is not None:
        item.publication_year = publication_year
    if external_url is not None:
        item.external_url = external_url
    if is_published is not None:
        item.is_published = is_published
    if is_featured is not None:
        item.is_featured = is_featured
    if staff_only is not None:
        item.staff_only = staff_only
    if max_concurrent_readers_provided:
        item.max_concurrent_readers = max_concurrent_readers
    item.updated_by = current_user.id
    item.updated_at = datetime.utcnow()

    if class_ids_provided:
        await _sync_item_classes(session, item.id, class_ids, school_id)
    if tags_provided:
        await _sync_item_tags(session, item.id, tags, school_id)

    await session.commit()
    await session.refresh(item)
    return (await _serialize_items(session, [item], current_user.id))[0]


@router.delete("/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_item(
    item_id: str,
    current_user: User = Depends(require_roles(*LIBRARY_UPLOAD_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    school_id = await get_current_school_id(current_user)
    item = await _get_item_or_404(session, item_id, school_id)
    _ensure_owner_or_admin(item, current_user)

    for url in (item.file_url, item.thumbnail_url):
        if url:
            try:
                Path(url.lstrip("/")).unlink(missing_ok=True)
            except OSError:
                pass

    await session.delete(item)
    await session.commit()


# ---------------------------------------------------------------------------
# Analytics: view/download tracking
# ---------------------------------------------------------------------------

@router.post("/items/{item_id}/view", response_model=dict)
async def track_view(
    item_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    school_id = await get_current_school_id(current_user)
    item = await _get_item_or_404(session, item_id, school_id)
    await _ensure_visible(session, item, current_user)

    session.add(LibraryItemInteraction(item_id=item.id, user_id=current_user.id, interaction_type=LibraryInteractionType.VIEW.value))
    item.view_count = (item.view_count or 0) + 1
    await session.commit()
    return {"view_count": item.view_count}


@router.post("/items/{item_id}/download", response_model=dict)
async def track_download(
    item_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    school_id = await get_current_school_id(current_user)
    item = await _get_item_or_404(session, item_id, school_id)
    await _ensure_visible(session, item, current_user)

    session.add(LibraryItemInteraction(item_id=item.id, user_id=current_user.id, interaction_type=LibraryInteractionType.DOWNLOAD.value))
    item.download_count = (item.download_count or 0) + 1
    await session.commit()
    return {"download_count": item.download_count}


# ---------------------------------------------------------------------------
# Licensing: concurrent-access seats for items with max_concurrent_readers set
# ---------------------------------------------------------------------------

async def _active_session_count(session: AsyncSession, item_id: str, exclude_user_id: Optional[str] = None) -> int:
    stmt = select(func.count(LibraryItemAccessSession.id)).where(
        LibraryItemAccessSession.item_id == item_id, LibraryItemAccessSession.expires_at > datetime.utcnow()
    )
    if exclude_user_id:
        stmt = stmt.where(LibraryItemAccessSession.user_id != exclude_user_id)
    return (await session.execute(stmt)).scalar() or 0


@router.post("/items/{item_id}/checkout-access", response_model=dict)
async def checkout_digital_access(
    item_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Claims one of a licensed digital resource's limited concurrent-reader
    seats before opening it. Items with no max_concurrent_readers set are
    unlimited — this always succeeds for them, unchanged from before this
    existed. Re-claiming (the same user opening it again, or refreshing the
    reader) just renews this user's own session rather than double-counting."""
    school_id = await get_current_school_id(current_user)
    item = await _get_item_or_404(session, item_id, school_id)
    await _ensure_visible(session, item, current_user)

    if item.max_concurrent_readers is None:
        return {"granted": True, "unlimited": True}

    existing = (
        await session.execute(
            select(LibraryItemAccessSession).where(
                LibraryItemAccessSession.item_id == item_id, LibraryItemAccessSession.user_id == current_user.id
            )
        )
    ).scalar_one_or_none()

    active_others = await _active_session_count(session, item_id, exclude_user_id=current_user.id)
    if not existing and active_others >= item.max_concurrent_readers:
        raise HTTPException(status_code=409, detail="All licensed copies of this resource are currently in use. Please try again later.")

    expires_at = datetime.utcnow() + timedelta(minutes=ACCESS_SESSION_TTL_MINUTES)
    if existing:
        existing.expires_at = expires_at
    else:
        session.add(LibraryItemAccessSession(item_id=item_id, user_id=current_user.id, expires_at=expires_at))
    await session.commit()
    return {
        "granted": True,
        "unlimited": False,
        "expires_at": expires_at,
        "seats_in_use": active_others + 1,
        "seats_total": item.max_concurrent_readers,
    }


@router.post("/items/{item_id}/release-access", response_model=dict)
async def release_digital_access(
    item_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Frees this user's seat early (e.g. closing the reader) instead of
    waiting for the TTL to expire — lets the next patron in sooner."""
    existing = (
        await session.execute(
            select(LibraryItemAccessSession).where(
                LibraryItemAccessSession.item_id == item_id, LibraryItemAccessSession.user_id == current_user.id
            )
        )
    ).scalar_one_or_none()
    if existing:
        await session.delete(existing)
        await session.commit()
    return {"released": True}


# ---------------------------------------------------------------------------
# Favorites
# ---------------------------------------------------------------------------

@router.get("/favorites", response_model=dict)
async def list_favorites(
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    school_id = await get_current_school_id(current_user)

    count_stmt = (
        select(func.count(LibraryItemFavorite.id))
        .join(LibraryItem, LibraryItem.id == LibraryItemFavorite.item_id)
        .where(LibraryItemFavorite.user_id == current_user.id, LibraryItem.school_id == school_id)
    )
    total = (await session.execute(count_stmt)).scalar() or 0

    offset = (page - 1) * limit
    stmt = (
        select(LibraryItem)
        .join(LibraryItemFavorite, LibraryItemFavorite.item_id == LibraryItem.id)
        .where(LibraryItemFavorite.user_id == current_user.id, LibraryItem.school_id == school_id)
        .order_by(LibraryItemFavorite.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    items = (await session.execute(stmt)).scalars().all()
    serialized = await _serialize_items(session, items, current_user.id)

    return {
        "items": serialized,
        "total": total,
        "page": page,
        "limit": limit,
        "pages": (total + limit - 1) // limit if total else 0,
    }


@router.post("/items/{item_id}/favorite", response_model=dict)
async def favorite_item(
    item_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    school_id = await get_current_school_id(current_user)
    item = await _get_item_or_404(session, item_id, school_id)
    await _ensure_visible(session, item, current_user)

    existing = (
        await session.execute(
            select(LibraryItemFavorite).where(LibraryItemFavorite.item_id == item_id, LibraryItemFavorite.user_id == current_user.id)
        )
    ).scalar_one_or_none()
    if not existing:
        session.add(LibraryItemFavorite(item_id=item_id, user_id=current_user.id))
        await session.commit()
    return {"is_favorited": True}


@router.delete("/items/{item_id}/favorite", response_model=dict)
async def unfavorite_item(
    item_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    school_id = await get_current_school_id(current_user)
    await _get_item_or_404(session, item_id, school_id)

    existing = (
        await session.execute(
            select(LibraryItemFavorite).where(LibraryItemFavorite.item_id == item_id, LibraryItemFavorite.user_id == current_user.id)
        )
    ).scalar_one_or_none()
    if existing:
        await session.delete(existing)
        await session.commit()
    return {"is_favorited": False}


# ---------------------------------------------------------------------------
# Ratings
# ---------------------------------------------------------------------------

@router.post("/items/{item_id}/rating", response_model=dict)
async def rate_item(
    item_id: str,
    payload: LibraryItemRatingCreate,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    if payload.rating < 1 or payload.rating > 5:
        raise HTTPException(status_code=400, detail="Rating must be between 1 and 5")

    school_id = await get_current_school_id(current_user)
    item = await _get_item_or_404(session, item_id, school_id)
    await _ensure_visible(session, item, current_user)

    existing = (
        await session.execute(
            select(LibraryItemRating).where(LibraryItemRating.item_id == item_id, LibraryItemRating.user_id == current_user.id)
        )
    ).scalar_one_or_none()
    if existing:
        existing.rating = payload.rating
        existing.review = payload.review
        existing.updated_at = datetime.utcnow()
    else:
        session.add(LibraryItemRating(item_id=item_id, user_id=current_user.id, rating=payload.rating, review=payload.review))
    await session.commit()

    avg_rating, ratings_count = (
        await session.execute(
            select(func.avg(LibraryItemRating.rating), func.count(LibraryItemRating.id)).where(LibraryItemRating.item_id == item_id)
        )
    ).one()
    return {
        "average_rating": round(float(avg_rating), 2) if avg_rating is not None else None,
        "ratings_count": ratings_count,
        "my_rating": payload.rating,
    }


@router.get("/items/{item_id}/ratings", response_model=dict)
async def list_item_ratings(
    item_id: str,
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    school_id = await get_current_school_id(current_user)
    item = await _get_item_or_404(session, item_id, school_id)
    await _ensure_visible(session, item, current_user)

    total = (
        await session.execute(select(func.count(LibraryItemRating.id)).where(LibraryItemRating.item_id == item_id))
    ).scalar() or 0

    offset = (page - 1) * limit
    rows = (
        await session.execute(
            select(LibraryItemRating, User.first_name, User.last_name)
            .join(User, User.id == LibraryItemRating.user_id)
            .where(LibraryItemRating.item_id == item_id)
            .order_by(LibraryItemRating.updated_at.desc())
            .offset(offset)
            .limit(limit)
        )
    ).all()

    return {
        "items": [
            {
                "id": rating.id,
                "rating": rating.rating,
                "review": rating.review,
                "user_name": f"{first_name} {last_name}".strip(),
                "created_at": rating.created_at,
                "updated_at": rating.updated_at,
            }
            for rating, first_name, last_name in rows
        ],
        "total": total,
        "page": page,
        "limit": limit,
        "pages": (total + limit - 1) // limit if total else 0,
    }
