"""Seed e-library data for the School ERP System.

Creates:
- library categories (with a parent/child pair)
- library tags
- library items for books, textbooks, videos, worksheets, and references,
  including author/publisher/ISBN-style metadata
- class-visibility and tag links via the normalized join tables
- a few sample ratings/favorites so the UI has non-empty demo data

This seeder is idempotent and safe to rerun.
"""
import asyncio
import sys
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent))

from sqlmodel import select
from database import async_session, init_db
from models.user import User, UserRole
from models.school import School
from models.classroom import Class as Classroom, Subject
from models.library import (
    LibraryCategory,
    LibraryItem,
    LibraryTag,
    LibraryItemTag,
    LibraryItemClass,
    LibraryItemFavorite,
    LibraryItemRating,
)


CATEGORY_DEFINITIONS = [
    {"name": "Books", "slug": "books", "description": "General reading books.", "parent_slug": None},
    {"name": "Textbooks", "slug": "textbooks", "description": "Core subject textbooks.", "parent_slug": None},
    {"name": "Videos", "slug": "videos", "description": "Educational videos and lectures.", "parent_slug": None},
    {"name": "Worksheets", "slug": "worksheets", "description": "Practice worksheets and exercises.", "parent_slug": None},
    {"name": "Reference", "slug": "reference", "description": "Reference materials and study guides.", "parent_slug": None},
    {"name": "Fiction", "slug": "fiction", "description": "Fiction and storybooks.", "parent_slug": "books"},
]

ITEM_DEFINITIONS = [
    {
        "title": "Intro to Computer Science",
        "description": "A beginner-friendly PDF guide covering computing basics and programming concepts.",
        "material_type": "book",
        "content_type": "pdf",
        "category_slug": "books",
        "author": "Kwame Owusu",
        "publisher": "Campusio Press",
        "edition": "2nd Edition",
        "language": "English",
        "isbn": "978-1-234567-01-2",
        "publication_year": 2022,
        "external_url": None,
        "file_url": "/uploads/library/sample/intro_to_cs.pdf",
        "tags": ["computer science", "programming", "beginner"],
    },
    {
        "title": "Mathematics Practice Worksheets",
        "description": "PDF worksheets with exercises for primary school mathematics.",
        "material_type": "worksheet",
        "content_type": "pdf",
        "category_slug": "worksheets",
        "author": "Ama Serwaa",
        "publisher": None,
        "edition": None,
        "language": "English",
        "isbn": None,
        "publication_year": 2023,
        "external_url": None,
        "file_url": "/uploads/library/sample/math_worksheets.pdf",
        "tags": ["mathematics", "worksheets", "practice"],
    },
    {
        "title": "Science Revision Video",
        "description": "A short lesson video covering science topics and revision tips.",
        "material_type": "educational_video",
        "content_type": "video",
        "category_slug": "videos",
        "author": "Dr. Kofi Mensah",
        "publisher": None,
        "edition": None,
        "language": "English",
        "isbn": None,
        "publication_year": 2024,
        "external_url": "https://example.com/science-revision-video",
        "file_url": None,
        "tags": ["science", "video", "revision"],
    },
    {
        "title": "English Grammar Reference",
        "description": "Reference document describing grammar rules, examples and tips.",
        "material_type": "reference",
        "content_type": "document",
        "category_slug": "reference",
        "author": "Grace Adjei",
        "publisher": "Campusio Press",
        "edition": "1st Edition",
        "language": "English",
        "isbn": "978-1-234567-02-9",
        "publication_year": 2021,
        "external_url": None,
        "file_url": "/uploads/library/sample/english_grammar_reference.pdf",
        "tags": ["english", "grammar", "reference"],
    },
    {
        "title": "Social Studies Case Study",
        "description": "A downloadable case study from the social studies curriculum.",
        "material_type": "academic_material",
        "content_type": "pdf",
        "category_slug": "textbooks",
        "author": "Yaw Boateng",
        "publisher": "Ministry of Education",
        "edition": None,
        "language": "English",
        "isbn": None,
        "publication_year": 2023,
        "external_url": None,
        "file_url": "/uploads/library/sample/social_studies_case_study.pdf",
        "tags": ["social studies", "case study", "curriculum"],
    },
]


async def get_school_and_users(session):
    result = await session.exec(select(User).where(User.email == "admin@school.edu.gh"))
    admin = result.first()
    if not admin:
        return None, None, []

    result = await session.exec(select(School).where(School.id == admin.school_id))
    school = result.first()

    result = await session.exec(select(User).where(User.school_id == admin.school_id, User.role == UserRole.STUDENT))
    students = result.all()

    return school, admin, students


async def get_first_subject_and_classes(session, school):
    subject_result = await session.exec(select(Subject).where(Subject.school_id == school.id).limit(1))
    subject = subject_result.first()

    class_result = await session.exec(select(Classroom).where(Classroom.school_id == school.id))
    classes = class_result.all()

    return subject, [cls.id for cls in classes[:2]]


async def create_categories(session, school):
    created = []
    categories = {}
    for category_data in CATEGORY_DEFINITIONS:
        result = await session.exec(
            select(LibraryCategory).where(
                (LibraryCategory.school_id == school.id) & (LibraryCategory.slug == category_data["slug"])
            )
        )
        category = result.first()
        if not category:
            parent = categories.get(category_data["parent_slug"]) if category_data["parent_slug"] else None
            category = LibraryCategory(
                school_id=school.id,
                name=category_data["name"],
                slug=category_data["slug"],
                description=category_data["description"],
                parent_id=parent.id if parent else None,
            )
            session.add(category)
            await session.flush()
            created.append(category)
        categories[category_data["slug"]] = category
    return categories, created


async def get_or_create_tag(session, school_id, name, cache):
    key = name.lower()
    if key in cache:
        return cache[key]
    result = await session.exec(
        select(LibraryTag).where(LibraryTag.school_id == school_id, LibraryTag.name == name)
    )
    tag = result.first()
    if not tag:
        tag = LibraryTag(school_id=school_id, name=name, slug=name.lower().replace(" ", "-"))
        session.add(tag)
        await session.flush()
    cache[key] = tag
    return tag


async def create_library_items(session, school, category_map, creator_id, subject_id, class_ids):
    created = []
    tag_cache = {}
    for item_data in ITEM_DEFINITIONS:
        category = category_map.get(item_data["category_slug"])
        if not category:
            continue

        result = await session.exec(
            select(LibraryItem).where((LibraryItem.school_id == school.id) & (LibraryItem.title == item_data["title"]))
        )
        item = result.first()
        if item:
            continue

        item = LibraryItem(
            school_id=school.id,
            title=item_data["title"],
            description=item_data["description"],
            material_type=item_data["material_type"],
            content_type=item_data["content_type"],
            category_id=category.id,
            subject_id=subject_id,
            author=item_data["author"],
            publisher=item_data["publisher"],
            edition=item_data["edition"],
            language=item_data["language"],
            isbn=item_data["isbn"],
            publication_year=item_data["publication_year"],
            file_url=item_data["file_url"],
            external_url=item_data["external_url"],
            is_published=True,
            is_featured=(item_data["category_slug"] in ["videos", "textbooks"]),
            created_by=creator_id,
            updated_by=creator_id,
        )
        session.add(item)
        await session.flush()

        for class_id in class_ids:
            session.add(LibraryItemClass(item_id=item.id, class_id=class_id))
        for tag_name in item_data["tags"]:
            tag = await get_or_create_tag(session, school.id, tag_name, tag_cache)
            session.add(LibraryItemTag(item_id=item.id, tag_id=tag.id))

        created.append(item)

    return created


async def create_sample_engagement(session, items, students):
    if not items or not students:
        return
    sample_student = students[0]
    featured_item = next((item for item in items if item.is_featured), items[0])

    existing_favorite = await session.exec(
        select(LibraryItemFavorite).where(
            LibraryItemFavorite.item_id == featured_item.id, LibraryItemFavorite.user_id == sample_student.id
        )
    )
    if not existing_favorite.first():
        session.add(LibraryItemFavorite(item_id=featured_item.id, user_id=sample_student.id))

    existing_rating = await session.exec(
        select(LibraryItemRating).where(
            LibraryItemRating.item_id == featured_item.id, LibraryItemRating.user_id == sample_student.id
        )
    )
    if not existing_rating.first():
        session.add(
            LibraryItemRating(
                item_id=featured_item.id,
                user_id=sample_student.id,
                rating=5,
                review="Really helpful resource, easy to follow!",
            )
        )


async def seed_elibrary_data():
    await init_db()

    async with async_session() as session:
        school, admin, students = await get_school_and_users(session)
        if not school or not admin:
            print("No admin user or school found. Run seed_data.py first.")
            return

        subject, class_ids = await get_first_subject_and_classes(session, school)
        subject_id = subject.id if subject else None

        print(f"Seeding e-library data for school: {school.name}")

        category_map, created_categories = await create_categories(session, school)
        created_items = await create_library_items(
            session, school, category_map, creator_id=admin.id, subject_id=subject_id, class_ids=class_ids
        )
        await create_sample_engagement(session, created_items or [], students)

        await session.commit()

        print("\n✓ E-library seed completed")
        print(f"Categories created: {len(created_categories)}")
        print(f"Library items created: {len(created_items)}")
        if subject:
            print(f"Subject linked: {subject.name}")
        if class_ids:
            print(f"Class IDs assigned: {len(class_ids)}")


if __name__ == "__main__":
    asyncio.run(seed_elibrary_data())
