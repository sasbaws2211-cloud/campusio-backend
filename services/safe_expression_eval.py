"""A minimal, restricted expression evaluator for DeductionRule.expression
(services/deduction_rules_service.py) — replaces a prior implementation
that substituted field values directly into a string via regex and then
called Python's eval() on the result. That approach had two real risks:
a string context value containing a stray quote could break out of its
quoted literal and inject arbitrary expression syntax, and substitution
order was never guaranteed safe for every possible field-name shape.

This evaluates the expression's parsed AST directly against a context
dict — no string substitution, no eval() — so a field's value (however it
looks) is only ever used as a Python value, never spliced into source
text. Supports exactly what a deduction-rule expression needs: boolean
and/or/not, comparisons, +-*/, name lookups, and literals — nothing else
(no function calls, no attribute access, no imports).
"""
import ast
import operator
from typing import Any, Dict

_BINOPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}
_COMPARES = {
    ast.Eq: operator.eq, ast.NotEq: operator.ne,
    ast.Lt: operator.lt, ast.LtE: operator.le,
    ast.Gt: operator.gt, ast.GtE: operator.ge,
}


class UnsafeExpressionError(ValueError):
    pass


def _eval_node(node: ast.AST, context: Dict[str, Any]) -> Any:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, context)
    if isinstance(node, ast.BoolOp):
        values = [_eval_node(v, context) for v in node.values]
        if isinstance(node.op, ast.And):
            return all(values)
        if isinstance(node.op, ast.Or):
            return any(values)
        raise UnsafeExpressionError(f"Unsupported boolean operator: {node.op}")
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _eval_node(node.operand, context)
    if isinstance(node, ast.Compare):
        left = _eval_node(node.left, context)
        result = True
        for op_node, comparator in zip(node.ops, node.comparators):
            right = _eval_node(comparator, context)
            op = _COMPARES.get(type(op_node))
            if op is None:
                raise UnsafeExpressionError(f"Unsupported comparison operator: {op_node}")
            result = result and op(left, right)
            left = right
        return result
    if isinstance(node, ast.BinOp):
        op = _BINOPS.get(type(node.op))
        if op is None:
            raise UnsafeExpressionError(f"Unsupported arithmetic operator: {node.op}")
        return op(_eval_node(node.left, context), _eval_node(node.right, context))
    if isinstance(node, ast.Name):
        if node.id in ("True", "False"):
            return node.id == "True"
        if node.id not in context:
            raise UnsafeExpressionError(f"Unknown field: {node.id}")
        return context[node.id]
    if isinstance(node, ast.Constant):
        return node.value
    raise UnsafeExpressionError(f"Unsupported expression element: {type(node).__name__}")


def safe_eval_expression(expression: str, context: Dict[str, Any]) -> bool:
    """Parses `expression` and evaluates it against `context`. Raises
    UnsafeExpressionError (or a ValueError/SyntaxError from ast.parse) on
    anything not in the small allowed grammar above — callers should catch
    and treat as "rule did not match", same as the prior implementation's
    blanket except-and-return-False."""
    tree = ast.parse(expression, mode="eval")
    return bool(_eval_node(tree, context))
