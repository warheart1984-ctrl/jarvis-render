"""Local calculator: deterministic arithmetic, never eval, never memory, never network.

Runs only on an explicit calculate request. Results are evidence (local facts),
not instructions, not governance, and not automatic memory.
"""

from __future__ import annotations

import ast
import asyncio
import math
import operator
import re
import time
from typing import Any

from jarvis.brain.deliberation import EvidenceRef, admit_external_suggestion
from jarvis.brain.tools.envelope import (
    LOCAL_TIMEOUT_SECONDS,
    MAX_EXPRESSION_CHARS,
    ToolCallRecord,
    ToolCallStatus,
    ToolName,
    validate_tool_arguments,
)
from jarvis.governance.hashing import content_hash

MAX_ABS_VALUE = 1e12
MAX_EXPONENT = 12
MAX_NODES = 64
MAX_DEPTH = 16

_EXPLICIT_CALC = (
    re.compile(r"(?is)\b(?:please\s+)?(?:calculate|compute)\b\s*[:\-]?\s*(.*)$"),
    re.compile(r"(?is)\bcalculator\b\s*[:\-]?\s*(.*)$"),
)
_MATH_ASK = (
    re.compile(r"(?is)\bhow much is\s+(.+)$"),
    re.compile(r"(?is)\bwhat(?:'s| is)\s+(-?[\d.(].+)$"),
)

_BINOPS: dict[type[ast.operator], Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY: dict[type[ast.unaryop], Any] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


class UnsafeExpression(ValueError):
    """Expression is not safe arithmetic."""


def looks_like_math(expression: str) -> bool:
    text = (expression or "").strip()
    if not text or len(text) > MAX_EXPRESSION_CHARS:
        return False
    if not re.fullmatch(r"[\d\s+\-*/().%eE]+", text):
        return False
    if not re.search(r"\d", text):
        return False
    return bool(re.search(r"[\d.)]\s*[+\-*/%]|[*/]", text)) or "(" in text


def resolve_calculator_expression(message: str) -> str | None:
    """Return an expression only for an explicit calculate request. Never auto-run.

    ``None`` means the user did not ask to calculate. An empty string means they
    asked, but supplied no expression (fail closed later).
    """

    text = (message or "").strip()
    if not text:
        return None
    for pattern in _EXPLICIT_CALC:
        match = pattern.search(text)
        if match:
            return (match.group(1) or "").strip(" .?!:;= ")
    for pattern in _MATH_ASK:
        match = pattern.search(text)
        if not match:
            continue
        captured = (match.group(1) or "").strip(" .?!:;= ")
        if looks_like_math(captured):
            return captured
    return None


def _bound(value: Any) -> int | float:
    if isinstance(value, bool) or isinstance(value, complex) or not isinstance(value, (int, float)):
        raise UnsafeExpression("unsupported value")
    if not math.isfinite(value) or abs(value) > MAX_ABS_VALUE:
        raise UnsafeExpression("value out of bounds")
    return value


def _eval_node(node: ast.AST, *, depth: int) -> int | float:
    if depth > MAX_DEPTH:
        raise UnsafeExpression("expression too deep")
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, depth=depth + 1)
    if isinstance(node, ast.Constant):
        return _bound(node.value)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        return _bound(_UNARY[type(node.op)](_eval_node(node.operand, depth=depth + 1)))
    if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
        left = _eval_node(node.left, depth=depth + 1)
        right = _eval_node(node.right, depth=depth + 1)
        if isinstance(node.op, ast.Pow):
            if not isinstance(right, int) or abs(right) > MAX_EXPONENT:
                raise UnsafeExpression("exponent out of bounds")
        try:
            return _bound(_BINOPS[type(node.op)](left, right))
        except (OverflowError, ZeroDivisionError, ValueError) as exc:
            raise UnsafeExpression("arithmetic failed") from exc
    raise UnsafeExpression("unsupported expression")


def evaluate_expression(expression: str) -> int | float:
    """Safe arithmetic only. Never ``eval`` / ``exec`` of Python."""

    if not expression or len(expression) > MAX_EXPRESSION_CHARS:
        raise UnsafeExpression("expression length out of bounds")
    tree = ast.parse(expression, mode="eval")
    if sum(1 for _ in ast.walk(tree)) > MAX_NODES:
        raise UnsafeExpression("expression too large")
    return _eval_node(tree, depth=0)


def evidence_from_calculator(record: ToolCallRecord) -> EvidenceRef | None:
    if record.status is not ToolCallStatus.ACCEPTED:
        return None
    expression = str(record.arguments.get("expression") or "")
    value = record.result.get("value")
    summary = f"{expression} = {value}"
    citation_id = record.citations[0] if record.citations else f"calc-{record.result_hash[:16]}"
    return admit_external_suggestion(
        source="calculator",
        summary=summary,
        evidence_id=citation_id,
        requested_authority=True,
        match_text=summary,
        citation_id=citation_id,
    )


def calculator_citation(record: ToolCallRecord, *, session_id: str) -> dict[str, Any]:
    citation_id = record.citations[0] if record.citations else f"calc-{record.result_hash[:16]}"
    return {
        "source_type": "calculator",
        "source_id": citation_id,
        "session_id": session_id,
        "expression": record.arguments.get("expression"),
        "value": record.result.get("value"),
        "content_sha256": record.result_hash,
        "trust_status": "local_deterministic",
        "citation_id": citation_id,
    }


def _identity(arguments: dict[str, Any], transaction_id: str, correlation_id: str) -> dict[str, Any]:
    return {
        "tool_name": ToolName.CALCULATOR.value,
        "arguments": arguments,
        "transaction_id": transaction_id,
        "correlation_id": correlation_id,
        "timeout_seconds": LOCAL_TIMEOUT_SECONDS,
        "attempts": 1,
        "provider": "local",
        "model": "ast-arithmetic-v0",
    }


async def maybe_calculator(
    *,
    message: str,
    transaction_id: str,
    correlation_id: str,
) -> ToolCallRecord | None:
    expression = resolve_calculator_expression(message)
    if expression is None:
        return None
    return await run_calculator(
        expression=expression,
        transaction_id=transaction_id,
        correlation_id=correlation_id,
    )


async def run_calculator(
    *,
    expression: str,
    transaction_id: str,
    correlation_id: str,
) -> ToolCallRecord:
    started = time.perf_counter()
    try:
        arguments = validate_tool_arguments(ToolName.CALCULATOR.value, {"expression": expression})
    except ValueError as exc:
        return ToolCallRecord(
            **_identity({"expression": expression}, transaction_id, correlation_id),
            status=ToolCallStatus.INVALID,
            attempt=0,
            retryable=False,
            error=str(exc),
            latency_ms=_latency(started),
        )
    identity = _identity(arguments, transaction_id, correlation_id)
    try:
        async with asyncio.timeout(LOCAL_TIMEOUT_SECONDS):
            value = evaluate_expression(arguments["expression"])
    except TimeoutError:
        return ToolCallRecord(
            **identity,
            status=ToolCallStatus.TIMEOUT,
            attempt=1,
            retryable=False,
            error="calculator timed out",
            latency_ms=_latency(started),
        )
    except (UnsafeExpression, SyntaxError, ValueError, TypeError, RecursionError):
        return ToolCallRecord(
            **identity,
            status=ToolCallStatus.INVALID,
            attempt=1,
            retryable=False,
            error="invalid calculator expression",
            latency_ms=_latency(started),
        )
    result = {"value": value, "expression": arguments["expression"]}
    digest = content_hash(result)
    return ToolCallRecord(
        **identity,
        status=ToolCallStatus.ACCEPTED,
        attempt=1,
        retryable=False,
        latency_ms=_latency(started),
        source_hash=content_hash(arguments),
        result_hash=digest,
        citations=[f"calc-{digest[:16]}"],
        result=result,
    )


def _latency(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)
