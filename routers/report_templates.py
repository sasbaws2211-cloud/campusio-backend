"""Report Templates API Routes"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from typing import List
from database import get_session
from models import ReportTemplate, ReportTemplateCreate, ReportTemplateUpdate, ReportTemplateResponse
from auth import get_current_user, require_roles
from models.user import User, UserRole
from services.plan_gating import require_plan_feature

router = APIRouter(
    prefix="/report-templates", tags=["report-templates"],
    dependencies=[Depends(require_plan_feature("academic_reports"))],
)
WRITE_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN)


async def get_school_id(current_user: User = Depends(get_current_user)) -> str:
    """Get school_id from current user"""
    if not current_user.school_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User is not associated with any school"
        )
    return current_user.school_id


@router.post("", response_model=ReportTemplateResponse, status_code=201)
async def create_template(
    template_data: ReportTemplateCreate,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    school_id: str = Depends(get_school_id)
):
    """Create a new report card template"""
    # If this is the first template or is_default=True, ensure it's the only default
    if template_data.is_default:
        result = await session.execute(
            select(ReportTemplate).where(
                ReportTemplate.school_id == school_id,
                ReportTemplate.is_default == True
            )
        )
        existing_default = result.scalar_one_or_none()
        if existing_default:
            existing_default.is_default = False
            session.add(existing_default)
    
    new_template = ReportTemplate(
        school_id=school_id,
        name=template_data.name,
        description=template_data.description,
        html_content=template_data.html_content,
        is_default=template_data.is_default,
        created_by=current_user.id
    )
    
    session.add(new_template)
    await session.commit()
    await session.refresh(new_template)
    return new_template


@router.get("", response_model=List[ReportTemplateResponse])
async def list_templates(
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
    school_id: str = Depends(get_school_id),
    active_only: bool = True
):
    """List all report templates for a school"""
    query = select(ReportTemplate).where(ReportTemplate.school_id == school_id)
    
    if active_only:
        query = query.where(ReportTemplate.is_active == True)
    
    result = await session.execute(query.order_by(ReportTemplate.created_at.desc()))
    templates = result.scalars().all()
    return templates


@router.get("/{template_id}", response_model=ReportTemplateResponse)
async def get_template(
    template_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
    school_id: str = Depends(get_school_id)
):
    """Get a specific report template"""
    result = await session.execute(
        select(ReportTemplate).where(
            ReportTemplate.id == template_id,
            ReportTemplate.school_id == school_id
        )
    )
    template = result.scalar_one_or_none()
    
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    
    return template


@router.put("/{template_id}", response_model=ReportTemplateResponse)
async def update_template(
    template_id: str,
    template_data: ReportTemplateUpdate,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    school_id: str = Depends(get_school_id)
):
    """Update a report template"""
    result = await session.execute(
        select(ReportTemplate).where(
            ReportTemplate.id == template_id,
            ReportTemplate.school_id == school_id
        )
    )
    template = result.scalar_one_or_none()
    
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    
    # If setting as default, unset other defaults
    if template_data.is_default and template_data.is_default != template.is_default:
        result = await session.execute(
            select(ReportTemplate).where(
                ReportTemplate.school_id == school_id,
                ReportTemplate.is_default == True,
                ReportTemplate.id != template_id
            )
        )
        existing_default = result.scalar_one_or_none()
        if existing_default:
            existing_default.is_default = False
            session.add(existing_default)
    
    # Increment version if html_content changes
    if template_data.html_content and template_data.html_content != template.html_content:
        template.version += 1
    
    # Update fields
    for field, value in template_data.dict(exclude_unset=True).items():
        setattr(template, field, value)
    
    session.add(template)
    await session.commit()
    await session.refresh(template)
    return template


@router.delete("/{template_id}", status_code=204)
async def delete_template(
    template_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    school_id: str = Depends(get_school_id)
):
    """Delete a report template"""
    result = await session.execute(
        select(ReportTemplate).where(
            ReportTemplate.id == template_id,
            ReportTemplate.school_id == school_id
        )
    )
    template = result.scalar_one_or_none()
    
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    
    # Hard delete - remove from database
    await session.delete(template)
    await session.commit()


@router.post("/{template_id}/set-default", response_model=ReportTemplateResponse)
async def set_default_template(
    template_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(require_roles(*WRITE_ROLES)),
    school_id: str = Depends(get_school_id)
):
    """Set a template as the default for the school"""
    result = await session.execute(
        select(ReportTemplate).where(
            ReportTemplate.id == template_id,
            ReportTemplate.school_id == school_id
        )
    )
    template = result.scalar_one_or_none()
    
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    
    # Unset current default
    result = await session.execute(
        select(ReportTemplate).where(
            ReportTemplate.school_id == school_id,
            ReportTemplate.is_default == True,
            ReportTemplate.id != template_id
        )
    )
    existing_default = result.scalar_one_or_none()
    if existing_default:
        existing_default.is_default = False
        session.add(existing_default)
    
    # Set new default
    template.is_default = True
    session.add(template)
    await session.commit()
    await session.refresh(template)
    
    return template


@router.get("/default/active", response_model=ReportTemplateResponse)
async def get_default_template(
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
    school_id: str = Depends(get_school_id)
):
    """Get the default active template for the school"""
    result = await session.execute(
        select(ReportTemplate).where(
            ReportTemplate.school_id == school_id,
            ReportTemplate.is_default == True,
            ReportTemplate.is_active == True
        )
    )
    template = result.scalar_one_or_none()
    
    if not template:
        raise HTTPException(status_code=404, detail="No default template found")
    
    return template
