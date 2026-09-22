"""Account Hierarchy Service - GL account hierarchy and balance rollup

Handles:
- Account hierarchy creation and management
- Parent-child account relationships
- Balance rollup from detail to summary accounts
- Hierarchical consolidation
- Hierarchy tree queries
"""
import logging
from typing import Optional, List, Dict, Any, Set
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select, and_, or_
from datetime import datetime

from models.finance.account_hierarchy import (
    AccountHierarchy,
    AccountHierarchyType,
    HierarchyNode,
    HierarchyLevel,
    HierarchyRollup,
    HierarchyConsolidation,
)
from models.finance.chart_of_accounts import GLAccount
from services.coa_service import CoaService

logger = logging.getLogger(__name__)


class AccountHierarchyError(Exception):
    """Base exception for account hierarchy operations"""
    pass


class AccountHierarchyService:
    """Service for managing GL account hierarchies and balance rollup
    
    **Concepts**:
    - Hierarchy: Named structure defining how accounts relate (e.g., "Org Structure")
    - Node: Account node in the tree (can be detail or summary)
    - Detail Account: Leaf node with GL account posting
    - Summary Account: Parent node rollup balance from children
    - Rollup: Calculation of parent balances from detail accounts
    
    **Examples**:
    - Org Hierarchy: School → Departments → Cost Centers → GL Accounts
    - Function Hierarchy: Operations → Academics, Admin, Finance
    - Program Hierarchy: Program A → Subprograms → GL Accounts
    """
    
    def __init__(self, session: AsyncSession):
        """Initialize service
        
        Args:
            session: AsyncSession for database operations
        """
        self.session = session
        self.coa_service = CoaService(session)
    
    # ==================== Hierarchy Creation ====================
    
    async def create_hierarchy(
        self,
        school_id: str,
        hierarchy_name: str,
        hierarchy_type: AccountHierarchyType,
        description: Optional[str] = None,
        created_by: str = "SYSTEM",
    ) -> str:
        """Create a new account hierarchy
        
        Args:
            school_id: School identifier
            hierarchy_name: Name of hierarchy
            hierarchy_type: Type of hierarchy
            description: Optional description
            created_by: User creating
            
        Returns:
            Hierarchy ID
        """
        try:
            hierarchy = AccountHierarchy(
                school_id=school_id,
                hierarchy_name=hierarchy_name,
                hierarchy_type=hierarchy_type,
                description=description,
                created_by=created_by,
            )
            
            self.session.add(hierarchy)
            await self.session.commit()
            
            logger.info(
                f"Created hierarchy '{hierarchy_name}' ({hierarchy_type.value}) "
                f"for school {school_id}"
            )
            
            return hierarchy.id
            
        except Exception as e:
            await self.session.rollback()
            logger.error(f"Error creating hierarchy: {str(e)}")
            raise AccountHierarchyError(f"Failed to create hierarchy: {str(e)}")
    
    # ==================== Node Management ====================
    
    async def create_hierarchy_node(
        self,
        school_id: str,
        hierarchy_id: str,
        node_name: str,
        node_code: str,
        level: HierarchyLevel,
        parent_node_id: Optional[str] = None,
        gl_account_id: Optional[str] = None,
        sequence: int = 0,
    ) -> str:
        """Create a hierarchy node
        
        Args:
            school_id: School identifier
            hierarchy_id: Parent hierarchy ID
            node_name: Node display name
            node_code: Hierarchical code (e.g., "1.1.1")
            level: Node level (DETAIL, SUMMARY, TOTAL)
            parent_node_id: Parent node (if not root)
            gl_account_id: GL account (if detail node)
            sequence: Display order
            
        Returns:
            Node ID
            
        Raises:
            AccountHierarchyError: If node creation fails
        """
        try:
            # Validate hierarchy exists — school_id scoped, so a caller can't
            # attach a node to another school's hierarchy by passing its id.
            hierarchy_result = await self.session.execute(
                select(AccountHierarchy).where(
                    AccountHierarchy.id == hierarchy_id,
                    AccountHierarchy.school_id == school_id,
                )
            )
            if not hierarchy_result.scalar_one_or_none():
                raise AccountHierarchyError(f"Hierarchy {hierarchy_id} not found")
            
            # Validate GL account if detail level
            if level == HierarchyLevel.DETAIL and gl_account_id:
                gl_account = await self.coa_service.get_account_by_id(
                    school_id,
                    gl_account_id
                )
                if not gl_account:
                    raise AccountHierarchyError(f"GL Account {gl_account_id} not found")
            
            # Validate parent if not root
            if parent_node_id:
                parent_result = await self.session.execute(
                    select(HierarchyNode).where(
                        HierarchyNode.id == parent_node_id,
                        HierarchyNode.school_id == school_id,
                    )
                )
                parent = parent_result.scalar_one_or_none()
                if not parent:
                    raise AccountHierarchyError(f"Parent node {parent_node_id} not found")
                
                # Update parent to mark as parent
                parent.is_parent = True
                self.session.add(parent)
            
            # Create node
            node = HierarchyNode(
                school_id=school_id,
                hierarchy_id=hierarchy_id,
                node_name=node_name,
                node_code=node_code,
                level=level,
                parent_node_id=parent_node_id,
                gl_account_id=gl_account_id,
                sequence=sequence,
            )
            
            self.session.add(node)
            await self.session.commit()
            
            logger.info(
                f"Created hierarchy node '{node_name}' ({node_code}) "
                f"in hierarchy {hierarchy_id}"
            )
            
            return node.id
            
        except AccountHierarchyError:
            await self.session.rollback()
            raise
        except Exception as e:
            await self.session.rollback()
            logger.error(f"Error creating hierarchy node: {str(e)}")
            raise AccountHierarchyError(f"Failed to create hierarchy node: {str(e)}")
    
    # ==================== Balance Rollup ====================
    
    async def calculate_node_balance(
        self,
        school_id: str,
        node_id: str,
    ) -> float:
        """Calculate total balance for a hierarchy node
        
        For detail nodes: Returns GL account balance
        For summary nodes: Returns sum of all descendant detail account balances
        
        Args:
            school_id: School identifier
            node_id: Node ID
            
        Returns:
            Calculated balance
        """
        try:
            # Get node — school_id scoped
            node_result = await self.session.execute(
                select(HierarchyNode).where(HierarchyNode.id == node_id, HierarchyNode.school_id == school_id)
            )
            node = node_result.scalar_one_or_none()
            if not node:
                raise AccountHierarchyError(f"Node {node_id} not found")

            # If detail node, get GL account balance (current_balance is
            # Decimal; this hierarchy subsystem's own fields are still float)
            if node.level == HierarchyLevel.DETAIL and node.gl_account_id:
                gl_account = await self.coa_service.get_account_by_id(
                    school_id,
                    node.gl_account_id
                )
                return float(gl_account.current_balance) if gl_account else 0.0

            # If summary node, get all descendant detail balances
            if node.is_parent:
                total_balance = 0.0
                descendants = await self._get_all_descendants(school_id, node_id)

                for descendant_id in descendants:
                    desc_result = await self.session.execute(
                        select(HierarchyNode).where(HierarchyNode.id == descendant_id, HierarchyNode.school_id == school_id)
                    )
                    descendant = desc_result.scalar_one_or_none()

                    if descendant and descendant.level == HierarchyLevel.DETAIL and descendant.gl_account_id:
                        gl_account = await self.coa_service.get_account_by_id(
                            school_id,
                            descendant.gl_account_id
                        )
                        if gl_account:
                            total_balance += float(gl_account.current_balance)
                
                return total_balance
            
            return 0.0
            
        except Exception as e:
            logger.error(f"Error calculating node balance: {str(e)}")
            return 0.0
    
    async def _get_all_descendants(
        self,
        school_id: str,
        node_id: str,
    ) -> List[str]:
        """Get all descendant node IDs (recursive)

        Args:
            school_id: School identifier — scopes traversal to this tenant's own nodes
            node_id: Parent node ID

        Returns:
            List of all descendant IDs
        """
        descendants = []

        # Get direct children
        child_result = await self.session.execute(
            select(HierarchyNode).where(HierarchyNode.parent_node_id == node_id, HierarchyNode.school_id == school_id)
        )
        children = child_result.scalars().all()

        for child in children:
            descendants.append(child.id)
            # Recursively get grandchildren
            grandchildren = await self._get_all_descendants(school_id, child.id)
            descendants.extend(grandchildren)

        return descendants
    
    async def rollup_all_nodes(
        self,
        school_id: str,
        hierarchy_id: str,
    ) -> Dict[str, float]:
        """Calculate rollup balances for all nodes in hierarchy
        
        Args:
            school_id: School identifier
            hierarchy_id: Hierarchy ID
            
        Returns:
            Dictionary mapping node_id to calculated balance
        """
        try:
            # Get all nodes in hierarchy — school_id scoped
            result = await self.session.execute(
                select(HierarchyNode).where(
                    HierarchyNode.hierarchy_id == hierarchy_id,
                    HierarchyNode.school_id == school_id,
                )
            )
            nodes = result.scalars().all()

            rollup_data = {}
            
            for node in nodes:
                balance = await self.calculate_node_balance(school_id, node.id)
                node.current_balance = balance
                self.session.add(node)
                rollup_data[node.id] = balance
            
            await self.session.commit()
            
            logger.info(
                f"Completed rollup for hierarchy {hierarchy_id} "
                f"({len(nodes)} nodes)"
            )
            
            return rollup_data
            
        except Exception as e:
            await self.session.rollback()
            logger.error(f"Error in rollup: {str(e)}")
            raise AccountHierarchyError(f"Failed to rollup balances: {str(e)}")
    
    # ==================== Hierarchy Queries ====================
    
    async def get_hierarchy_tree(
        self,
        school_id: str,
        hierarchy_id: str,
        root_node_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get hierarchy tree structure
        
        Args:
            school_id: School identifier
            hierarchy_id: Hierarchy ID
            root_node_id: Start from specific node (default: root)
            
        Returns:
            Tree structure with balances
        """
        try:
            # Get root node if not specified — school_id scoped
            if not root_node_id:
                result = await self.session.execute(
                    select(HierarchyNode).where(
                        and_(
                            HierarchyNode.hierarchy_id == hierarchy_id,
                            HierarchyNode.school_id == school_id,
                            HierarchyNode.parent_node_id == None
                        )
                    )
                )
                root = result.scalar_one_or_none()
                if not root:
                    raise AccountHierarchyError("No root node found for hierarchy")
                root_node_id = root.id

            # Build tree recursively
            tree = await self._build_tree_node(school_id, root_node_id)
            return tree
            
        except AccountHierarchyError:
            raise
        except Exception as e:
            logger.error(f"Error getting hierarchy tree: {str(e)}")
            raise AccountHierarchyError(f"Failed to get hierarchy tree: {str(e)}")
    
    async def _build_tree_node(
        self,
        school_id: str,
        node_id: str,
    ) -> Dict[str, Any]:
        """Build tree structure for a node (recursive)

        Args:
            school_id: School identifier — scopes this and all recursive
                calls to the caller's own tenant, so a root_node_id passed
                by the caller can't be used to read another school's tree.
            node_id: Node ID

        Returns:
            Tree structure
        """
        node_result = await self.session.execute(
            select(HierarchyNode).where(HierarchyNode.id == node_id, HierarchyNode.school_id == school_id)
        )
        node = node_result.scalar_one_or_none()

        if not node:
            return {}

        # Get children
        children_result = await self.session.execute(
            select(HierarchyNode).where(HierarchyNode.parent_node_id == node_id, HierarchyNode.school_id == school_id)
            .order_by(HierarchyNode.sequence)
        )
        children = children_result.scalars().all()

        # Build tree
        tree_node = {
            "id": node.id,
            "name": node.node_name,
            "code": node.node_code,
            "level": node.level.value,
            "balance": node.current_balance,
            "children": []
        }

        for child in children:
            child_tree = await self._build_tree_node(school_id, child.id)
            tree_node["children"].append(child_tree)

        return tree_node
    
    # ==================== Consolidated Reporting ====================
    
    async def get_consolidated_report(
        self,
        school_id: str,
        hierarchy_id: str,
    ) -> Dict[str, Any]:
        """Get consolidated report by hierarchy level
        
        Args:
            school_id: School identifier
            hierarchy_id: Hierarchy ID
            
        Returns:
            Report with balances aggregated by level
        """
        try:
            # Rollup all nodes first
            await self.rollup_all_nodes(school_id, hierarchy_id)
            
            # Get hierarchy info — school_id scoped
            h_result = await self.session.execute(
                select(AccountHierarchy).where(
                    AccountHierarchy.id == hierarchy_id,
                    AccountHierarchy.school_id == school_id,
                )
            )
            hierarchy = h_result.scalar_one_or_none()
            if not hierarchy:
                raise AccountHierarchyError(f"Hierarchy {hierarchy_id} not found")

            # Group nodes by level
            by_level_result = await self.session.execute(
                select(HierarchyNode).where(
                    HierarchyNode.hierarchy_id == hierarchy_id,
                    HierarchyNode.school_id == school_id,
                ).order_by(HierarchyNode.level, HierarchyNode.sequence)
            )
            nodes = by_level_result.scalars().all()
            
            by_level = {
                "detail": {"nodes": [], "total": 0.0},
                "summary": {"nodes": [], "total": 0.0},
                "total": {"nodes": [], "total": 0.0},
            }
            
            for node in nodes:
                level_key = node.level.value
                by_level[level_key]["nodes"].append({
                    "id": node.id,
                    "name": node.node_name,
                    "code": node.node_code,
                    "balance": node.current_balance,
                })
                by_level[level_key]["total"] += node.current_balance
            
            return {
                "hierarchy_id": hierarchy_id,
                "hierarchy_name": hierarchy.hierarchy_name,
                "by_level": by_level,
                "grand_total": by_level["total"]["total"],
                "timestamp": datetime.utcnow().isoformat(),
            }
            
        except Exception as e:
            logger.error(f"Error generating consolidated report: {str(e)}")
            raise AccountHierarchyError(f"Failed to generate report: {str(e)}")
