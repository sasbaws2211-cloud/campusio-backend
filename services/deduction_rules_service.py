"""Deduction Rules Service for evaluating and applying payroll rules"""
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from decimal import Decimal
import json

from models.payroll import (
    DeductionRule, RuleOperator, RuleType, RuleEvaluationResult
)
from models.staff import Staff
from models.user import User
from services.safe_expression_eval import safe_eval_expression, UnsafeExpressionError

import logging
logger = logging.getLogger(__name__)


class RulesEvaluationService:
    """Service for evaluating and applying deduction rules"""
    
    def __init__(self, session: AsyncSession, school_id: str):
        self.session = session
        self.school_id = school_id
    
    async def get_active_rules(self, rule_type: Optional[str] = None) -> List[DeductionRule]:
        """Get all active rules for the school, optionally filtered by type"""
        query = select(DeductionRule).where(
            DeductionRule.school_id == self.school_id,
            DeductionRule.is_active == True
        )
        
        if rule_type:
            query = query.where(DeductionRule.rule_type == rule_type)
        
        # Order by priority (higher priority first)
        query = query.order_by(DeductionRule.priority.desc())
        
        result = await self.session.execute(query)
        return result.scalars().all()
    
    async def evaluate_rules_for_staff(
        self,
        staff_id: str,
        basic_salary: float,
        period_year: int,
        period_month: int,
        staff_metadata: Optional[Dict[str, Any]] = None
    ) -> Tuple[List[RuleEvaluationResult], float]:
        """
        Evaluate all applicable rules for a staff member
        
        Args:
            staff_id: Staff ID
            basic_salary: Basic salary amount
            period_year: Payroll period year
            period_month: Payroll period month
            staff_metadata: Additional staff data (years_service, absent_days, etc.)
        
        Returns:
            Tuple of (list of evaluation results, total deduction amount)
        """
        rules = await self.get_active_rules()
        results = []
        total_deduction = 0.0
        
        # Build staff context for evaluation
        context = await self._build_context(staff_id, basic_salary, staff_metadata)
        
        for rule in rules:
            try:
                matched, deduction_amount = self._evaluate_rule(rule, context)
                
                if matched:
                    result = RuleEvaluationResult(
                        rule_id=rule.id,
                        rule_name=rule.name,
                        matched=True,
                        deduction_amount=deduction_amount,
                        deduction_type=rule.deduction_type,
                        reason=f"{rule.deduction_category}: {rule.deduction_description or rule.name}"
                    )
                    results.append(result)
                    total_deduction += deduction_amount
                    
            except Exception as e:
                # Log rule evaluation error but continue
                print(f"Error evaluating rule {rule.id}: {str(e)}")
        
        return results, total_deduction
    
    def _evaluate_rule(self, rule: DeductionRule, context: Dict[str, Any]) -> Tuple[bool, float]:
        """
        Evaluate a single rule against the context
        
        Returns:
            Tuple of (matched: bool, deduction_amount: float)
        """
        matched = False
        
        try:
            # For complex rules with expressions
            if rule.expression:
                matched = self._evaluate_expression(rule.expression, context)
            else:
                # Simple condition-based evaluation
                condition_value = context.get(rule.condition_field)
                
                if condition_value is None:
                    return False, 0.0
                
                matched = self._evaluate_condition(
                    rule.operator,
                    condition_value,
                    rule.condition_value_min,
                    rule.condition_value_max
                )
            
            if matched:
                deduction_amount = self._calculate_deduction(
                    rule.deduction_type,
                    rule.deduction_amount,
                    context.get("basic_salary", 0)
                )
                return True, deduction_amount
            
            return False, 0.0
            
        except Exception as e:
            print(f"Error in rule evaluation: {str(e)}")
            return False, 0.0
    
    def _evaluate_condition(
        self,
        operator: RuleOperator,
        value: float,
        min_val: Optional[float],
        max_val: Optional[float]
    ) -> bool:
        """Evaluate a single condition"""
        
        if operator == RuleOperator.EQUALS:
            return value == min_val
        
        elif operator == RuleOperator.GREATER_THAN:
            return value > min_val
        
        elif operator == RuleOperator.LESS_THAN:
            return value < min_val
        
        elif operator == RuleOperator.GREATER_EQUAL:
            return value >= min_val
        
        elif operator == RuleOperator.LESS_EQUAL:
            return value <= min_val
        
        elif operator == RuleOperator.BETWEEN:
            return min_val <= value <= max_val
        
        elif operator == RuleOperator.CONTAINS:
            return str(min_val) in str(value)
        
        return False
    
    def _evaluate_expression(self, expression: str, context: Dict[str, Any]) -> bool:
        """
        Evaluate a complex expression against context.

        Example expressions:
        - "basic_salary > 5000 and years_service > 5"
        - "absent_days >= 3 or leave_type == 'unpaid'"

        Evaluated via services.safe_expression_eval — an AST-walking
        evaluator against `context` directly, not a string-substitution +
        eval() (the prior approach here, which risked a string context
        value with a stray quote breaking out of its literal and injecting
        arbitrary expression syntax).
        """
        try:
            return safe_eval_expression(expression, context)
        except (UnsafeExpressionError, SyntaxError, ValueError, TypeError) as e:
            logger.warning(f"Error evaluating expression '{expression}': {str(e)}")
            return False
    
    def _calculate_deduction(
        self,
        deduction_type: str,
        deduction_amount: float,
        basic_salary: float
    ) -> float:
        """Calculate deduction amount based on type"""
        
        if deduction_type == "percentage":
            # Percentage of basic salary
            return (basic_salary * deduction_amount) / 100.0
        
        elif deduction_type == "fixed":
            # Fixed amount
            return deduction_amount
        
        return 0.0
    
    async def _build_context(
        self,
        staff_id: str,
        basic_salary: float,
        metadata: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Build context dictionary for rule evaluation"""
        
        # Fetch staff data
        result = await self.session.execute(
            select(Staff).where(Staff.id == staff_id)
        )
        staff = result.scalar_one_or_none()
        
        # Build context
        context = {
            "staff_id": staff_id,
            "basic_salary": basic_salary,
        }
        
        if staff:
            # Calculate years of service
            if staff.date_joined:
                # Handle both datetime and string date_joined
                joined_date = staff.date_joined
                if isinstance(joined_date, str):
                    joined_date = datetime.fromisoformat(joined_date.replace('Z', '+00:00'))
                years_service = (datetime.utcnow() - joined_date).days / 365.25
                context["years_service"] = years_service
            
            context["staff_type"] = staff.staff_type.value if staff.staff_type else ""
            context["position"] = staff.position or ""
        
        # Add custom metadata
        if metadata:
            context.update(metadata)
        
        return context
    
    async def get_rule_by_id(self, rule_id: str) -> Optional[DeductionRule]:
        """Get a specific rule by ID"""
        result = await self.session.execute(
            select(DeductionRule).where(
                DeductionRule.id == rule_id,
                DeductionRule.school_id == self.school_id
            )
        )
        return result.scalar_one_or_none()
    
    async def create_rule(self, rule_data: Dict[str, Any], created_by: str) -> DeductionRule:
        """Create a new deduction rule"""
        rule = DeductionRule(
            school_id=self.school_id,
            created_by=created_by,
            **rule_data
        )
        self.session.add(rule)
        await self.session.commit()
        await self.session.refresh(rule)
        return rule
    
    async def update_rule(self, rule_id: str, rule_data: Dict[str, Any]) -> Optional[DeductionRule]:
        """Update an existing deduction rule"""
        rule = await self.get_rule_by_id(rule_id)
        if not rule:
            return None
        
        # Update fields
        for field, value in rule_data.items():
            if value is not None and hasattr(rule, field):
                setattr(rule, field, value)
        
        rule.updated_at = datetime.utcnow()
        self.session.add(rule)
        await self.session.commit()
        await self.session.refresh(rule)
        return rule
    
    async def delete_rule(self, rule_id: str) -> bool:
        """Soft delete a deduction rule"""
        rule = await self.get_rule_by_id(rule_id)
        if not rule:
            return False
        
        rule.is_active = False
        rule.updated_at = datetime.utcnow()
        self.session.add(rule)
        await self.session.commit()
        return True


class RulePresetService:
    """Service providing preset rule templates"""
    
    @staticmethod
    def get_preset_rules() -> List[Dict[str, Any]]:
        """Get common preset rules for payroll systems in Ghana"""
        return [
            {
                "name": "Pension Bracket A (Basic < 2000)",
                "description": "5% pension for staff earning less than GHS 2000",
                "rule_type": "salary_bracket",
                "operator": "less_than",
                "condition_field": "basic_salary",
                "condition_value_min": 2000,
                "deduction_type": "percentage",
                "deduction_amount": 5.0,
                "deduction_category": "pension",
                "priority": 10,
            },
            {
                "name": "Pension Bracket B (Basic 2000-5000)",
                "description": "5.5% pension for staff earning GHS 2000-5000",
                "rule_type": "salary_bracket",
                "operator": "between",
                "condition_field": "basic_salary",
                "condition_value_min": 2000,
                "condition_value_max": 5000,
                "deduction_type": "percentage",
                "deduction_amount": 5.5,
                "deduction_category": "pension",
                "priority": 9,
            },
            {
                "name": "Pension Bracket C (Basic > 5000)",
                "description": "6% pension for staff earning more than GHS 5000",
                "rule_type": "salary_bracket",
                "operator": "greater_than",
                "condition_field": "basic_salary",
                "condition_value_min": 5000,
                "deduction_type": "percentage",
                "deduction_amount": 6.0,
                "deduction_category": "pension",
                "priority": 8,
            },
            {
                "name": "Long Service Bonus (5+ years)",
                "description": "2% additional deduction for staff with 5+ years service",
                "rule_type": "years_service",
                "operator": "greater_equal",
                "condition_field": "years_service",
                "condition_value_min": 5,
                "deduction_type": "percentage",
                "deduction_amount": 2.0,
                "deduction_category": "bonus",
                "priority": 5,
            },
            {
                "name": "Absence Penalty",
                "description": "GHS 50 deduction per day absent (max 5 days/month)",
                "rule_type": "attendance",
                "operator": "greater_equal",
                "condition_field": "absent_days",
                "condition_value_min": 1,
                "deduction_type": "fixed",
                "deduction_amount": 50.0,
                "deduction_category": "absence_penalty",
                "priority": 3,
                "expression": "absent_days >= 1 and absent_days <= 5"
            },
        ]
