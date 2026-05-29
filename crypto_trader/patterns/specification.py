"""
Specification Pattern — composable predicate objects for multi-criteria validation.

Each `Specification` encapsulates one rule and returns `(satisfied: bool, reason: str)`.
Specs are combined with `&` (AND), `|` (OR), and `~` (NOT) to build complex gates
without nested if-chains.

Usage::

    class KillSwitchSpec(Specification[RiskContext]):
        def is_satisfied_by(self, ctx) -> Tuple[bool, str]:
            if ctx.kill_switch:
                return False, f"Kill switch active: {ctx.kill_switch_reason}"
            return True, ""

    gate = KillSwitchSpec() & DailyLimitSpec() & DrawdownSpec()
    ok, reason = gate.is_satisfied_by(ctx)
    return ok, reason or "OK"
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Generic, Tuple, TypeVar

T = TypeVar("T")


class Specification(ABC, Generic[T]):
    """Base composable specification. Override `is_satisfied_by`."""

    @abstractmethod
    def is_satisfied_by(self, candidate: T) -> Tuple[bool, str]:
        """Return ``(True, "")`` when satisfied, ``(False, reason)`` otherwise."""
        ...

    # ── Combinators ───────────────────────────────────────────────────────────

    def __and__(self, other: Specification[T]) -> _AndSpec[T]:
        return _AndSpec(self, other)

    def __or__(self, other: Specification[T]) -> _OrSpec[T]:
        return _OrSpec(self, other)

    def __invert__(self) -> _NotSpec[T]:
        return _NotSpec(self)


class _AndSpec(Specification[T]):
    """Short-circuit AND — first failure wins and stops evaluation."""

    def __init__(self, *specs: Specification[T]) -> None:
        self._specs = specs

    def is_satisfied_by(self, candidate: T) -> Tuple[bool, str]:
        for spec in self._specs:
            ok, reason = spec.is_satisfied_by(candidate)
            if not ok:
                return False, reason
        return True, ""

    # Support chaining: a & b & c flattens into a single _AndSpec
    def __and__(self, other: Specification[T]) -> _AndSpec[T]:
        return _AndSpec(*self._specs, other)


class _OrSpec(Specification[T]):
    """Short-circuit OR — first success wins."""

    def __init__(self, *specs: Specification[T]) -> None:
        self._specs = specs

    def is_satisfied_by(self, candidate: T) -> Tuple[bool, str]:
        reasons = []
        for spec in self._specs:
            ok, reason = spec.is_satisfied_by(candidate)
            if ok:
                return True, ""
            reasons.append(reason)
        return False, " | ".join(filter(None, reasons))

    def __or__(self, other: Specification[T]) -> _OrSpec[T]:
        return _OrSpec(*self._specs, other)


class _NotSpec(Specification[T]):
    def __init__(self, spec: Specification[T]) -> None:
        self._spec = spec

    def is_satisfied_by(self, candidate: T) -> Tuple[bool, str]:
        ok, reason = self._spec.is_satisfied_by(candidate)
        if ok:
            return False, f"NOT({reason or 'condition was true'})"
        return True, ""
