"""API Router for Account Hierarchy

Endpoints for:
- Hierarchy creation and management
- Node creation and relationships
- Balance rollup calculations
- Hierarchy tree queries
- Consolidated reporting
"""
import logging
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from services.account_hierarchy_service import (
    AccountHierarchyService,
    AccountHierarchyError,
)
from models.finance.account_hierarchy import (
    AccountHierarchyType,
    HierarchyLevel,
)
from dependencies import get_current_school_id
from auth import get_current_user, require_roles
from database import get_session
from models.user import User, UserRole
logger = logging.getLogger(__name__)
router = APIRouter(prefix="/account-hierarchy", tags=["Account Hierarchy"])

# Hierarchy structure/relationship edits and rollups affect consolidated
# financial reporting — same role gate as journal.py's posting endpoints.
FINANCE_ADMIN_ROLES = (UserRole.SUPER_ADMIN, UserRole.SCHOOL_ADMIN, UserRole.HR)


# ==================== Hierarchy Creation ====================

@router.post("/hierarchies", response_model=dict)
async def create_hierarchy(
    body: dict,
    current_user: User = Depends(require_roles(*FINANCE_ADMIN_ROLES)),
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Create a new account hierarchy
    
    Defines a new hierarchical structure for GL accounts (e.g., organizational,
    functional, program-based).
    
    Args:
        body: Request body containing:
            - hierarchy_name: Name of hierarchy (e.g., "Organizational Structure")
            - hierarchy_type: Type (ACCOUNT_LEVEL, ORGANIZATIONAL, FUNCTIONAL, etc.)
            - description: Optional description
        current_user: Current user
        school_id: School identifier
        session: Database session
        
    Returns:
        Hierarchy ID and confirmation
    """
    hierarchy_name = body.get("hierarchy_name")
    hierarchy_type = body.get("hierarchy_type")
    description = body.get("description")
    
    if not hierarchy_name:
        raise HTTPException(status_code=400, detail="hierarchy_name is required")
    if not hierarchy_type:
        raise HTTPException(status_code=400, detail="hierarchy_type is required")
    
    try:
        hierarchy_type = AccountHierarchyType(hierarchy_type)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid hierarchy_type. Must be one of: {', '.join([t.value for t in AccountHierarchyType])}"
        )
    try:
        service = AccountHierarchyService(session)
        user_id = current_user.id
        hierarchy_id = await service.create_hierarchy(
            school_id=school_id,
            hierarchy_name=hierarchy_name,
            hierarchy_type=hierarchy_type,
            description=description,
            created_by=user_id,
        )
        
        logger.info(
            f"Hierarchy '{hierarchy_name}' created by {user_id} "
            f"({hierarchy_type.value})"
        )
        
        return {
            "status": "success",
            "hierarchy_id": hierarchy_id,
            "hierarchy_name": hierarchy_name,
            "hierarchy_type": hierarchy_type.value,
            "next_step": "Create hierarchy nodes",
        }
    except AccountHierarchyError as e:
        logger.warning(f"Error creating hierarchy: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error in hierarchy creation: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to create hierarchy")


# ==================== Node Management ====================

@router.post("/nodes", response_model=dict)
async def create_hierarchy_node(
    body: dict,
    current_user: User = Depends(require_roles(*FINANCE_ADMIN_ROLES)),
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Create a hierarchy node
    
    Creates a node in the hierarchy tree. Can be:
    - Detail: Leaf node pointing to GL account
    - Summary: Parent node rolling up children
    - Total: Root node for entire hierarchy
    
    Args:
        body: Request body containing:
            - hierarchy_id: Parent hierarchy ID
            - node_name: Display name
            - node_code: Hierarchical code (e.g., "1.1.1" for 3 levels)
            - level: Node level (DETAIL, SUMMARY, TOTAL)
            - parent_node_id: Parent node (if not root) (optional)
            - gl_account_id: GL account ID (if detail node) (optional)
            - sequence: Display order (optional, default 0)
        school_id: School identifier
        session: Database session
        
    Returns:
        Node ID and confirmation
    """
    hierarchy_id = body.get("hierarchy_id")
    node_name = body.get("node_name")
    node_code = body.get("node_code")
    level = body.get("level")
    parent_node_id = body.get("parent_node_id")
    gl_account_id = body.get("gl_account_id")
    sequence = body.get("sequence", 0)
    
    if not hierarchy_id:
        raise HTTPException(status_code=400, detail="hierarchy_id is required")
    if not node_name:
        raise HTTPException(status_code=400, detail="node_name is required")
    if not node_code:
        raise HTTPException(status_code=400, detail="node_code is required")
    if not level:
        raise HTTPException(status_code=400, detail="level is required")
    
    try:
        level = HierarchyLevel(level)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid level. Must be one of: {', '.join([l.value for l in HierarchyLevel])}"
        )
    try:
        service = AccountHierarchyService(session)
        node_id = await service.create_hierarchy_node(
            school_id=school_id,
            hierarchy_id=hierarchy_id,
            node_name=node_name,
            node_code=node_code,
            level=level,
            parent_node_id=parent_node_id,
            gl_account_id=gl_account_id,
            sequence=sequence,
        )
        
        return {
            "status": "success",
            "node_id": node_id,
            "node_name": node_name,
            "node_code": node_code,
            "level": level.value,
            "parent_node_id": parent_node_id,
        }
    except AccountHierarchyError as e:
        logger.warning(f"Error creating node: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error in node creation: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to create node")


# ==================== Balance Rollup ====================

@router.post("/rollup/{hierarchy_id}", response_model=dict)
async def rollup_all_nodes(
    hierarchy_id: str,
    current_user: User = Depends(require_roles(*FINANCE_ADMIN_ROLES)),
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Calculate rollup balances for all nodes in hierarchy
    
    **CRITICAL OPERATION** - Traverses entire hierarchy tree and calculates
    balance for each node based on GL account balances at detail level.
    
    Args:
        hierarchy_id: Hierarchy ID
        school_id: School identifier
        session: Database session
        
    Returns:
        Rollup completion summary
    """
    try:
        service = AccountHierarchyService(session)
        rollup_data = await service.rollup_all_nodes(school_id, hierarchy_id)
        
        return {
            "status": "success",
            "hierarchy_id": hierarchy_id,
            "nodes_updated": len(rollup_data),
            "rollup_data": rollup_data,
            "timestamp": "now",
        }
    except AccountHierarchyError as e:
        logger.warning(f"Error in rollup: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error in rollup process: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to rollup balances")


@router.get("/node-balance/{node_id}", response_model=dict)
async def get_node_balance(
    node_id: str,
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Get calculated balance for a hierarchy node
    
    For detail nodes: Returns GL account balance
    For summary nodes: Returns rollup of all descendant detail balances
    
    Args:
        node_id: Node ID
        school_id: School identifier
        session: Database session
        
    Returns:
        Node balance and calculation details
    """
    try:
        service = AccountHierarchyService(session)
        balance = await service.calculate_node_balance(school_id, node_id)
        
        return {
            "node_id": node_id,
            "balance": balance,
            "calculated_at": "now",
        }
    except AccountHierarchyError as e:
        logger.warning(f"Error getting node balance: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error calculating balance: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to calculate balance")


# ==================== Hierarchy Queries ====================

@router.get("/tree/{hierarchy_id}", response_model=dict)
async def get_hierarchy_tree(
    hierarchy_id: str,
    root_node_id: Optional[str] = Query(None),
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Get hierarchy tree structure
    
    Returns complete tree structure with all nodes and their balances.
    Optionally start from specific node instead of root.
    
    Args:
        hierarchy_id: Hierarchy ID
        root_node_id: Optional starting node (default: root)
        school_id: School identifier
        session: Database session
        
    Returns:
        Tree structure with node names, codes, and balances
    """
    try:
        service = AccountHierarchyService(session)
        tree = await service.get_hierarchy_tree(
            school_id=school_id,
            hierarchy_id=hierarchy_id,
            root_node_id=root_node_id,
        )
        
        return {
            "hierarchy_id": hierarchy_id,
            "tree": tree,
        }
    except AccountHierarchyError as e:
        logger.warning(f"Error getting tree: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error retrieving tree: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to get hierarchy tree")


# ==================== Consolidated Reporting ====================

@router.get("/consolidated-report/{hierarchy_id}", response_model=dict)
async def get_consolidated_report(
    hierarchy_id: str,
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Get consolidated report by hierarchy level
    
    Returns summary report with:
    - Nodes grouped by level (detail, summary, total)
    - Balance totals at each level
    - Grand total for hierarchy
    
    Useful for:
    - Consolidated financial statements
    - Department-level reporting
    - Program budget vs actual
    - Drill-down analysis
    
    Args:
        hierarchy_id: Hierarchy ID
        school_id: School identifier
        session: Database session
        
    Returns:
        Consolidated report with level-by-level breakdown
    """
    try:
        service = AccountHierarchyService(session)
        report = await service.get_consolidated_report(school_id, hierarchy_id)
        
        return report
    except AccountHierarchyError as e:
        logger.warning(f"Error getting report: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error generating report: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to generate consolidated report")


# ==================== Export / Reporting ====================

@router.get("/export/{hierarchy_id}", response_model=dict)
async def export_hierarchy_data(
    hierarchy_id: str,
    format: str = Query("json", regex="^(json|csv)$"),
    school_id: str = Depends(get_current_school_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Export hierarchy and balances
    
    Args:
        hierarchy_id: Hierarchy ID to export
        format: Export format (json or csv)
        school_id: School identifier
        session: Database session
        
    Returns:
        Exported data
    """
    try:
        service = AccountHierarchyService(session)
        
        # Get tree and report
        tree = await service.get_hierarchy_tree(school_id, hierarchy_id)
        report = await service.get_consolidated_report(school_id, hierarchy_id)
        
        return {
            "status": "success",
            "format": format,
            "hierarchy_id": hierarchy_id,
            "tree": tree,
            "report": report,
            "export_timestamp": "now",
        }
    except AccountHierarchyError as e:
        logger.warning(f"Error exporting hierarchy: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error in export: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to export hierarchy data")
