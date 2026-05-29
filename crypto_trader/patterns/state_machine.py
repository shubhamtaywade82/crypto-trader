"""
Finite State Machine — explicit states, guarded transitions, and lifecycle hooks.

Prefer this over scattered ``if status == "X"`` checks: all valid transitions
are declared in one place, invalid ones are silently rejected (or raise in
strict mode), and entry/exit hooks fire automatically.

Usage::

    sm = StateMachine("open")
    sm.add("open",     "reducing", "partial_close")
    sm.add("open",     "closed",   "full_close")
    sm.add("reducing", "closed",   "full_close")
    sm.on_enter("closed", lambda **kw: persist_close_time(kw["closed_at"]))

    sm.trigger("partial_close", qty=50, exit_price=100.0)
    assert sm.state == "reducing"
    sm.trigger("full_close",    qty=50, exit_price=105.0)
    assert sm.state == "closed"
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Set

logger = logging.getLogger("crypto_trader.patterns.state_machine")


@dataclass
class _Transition:
    from_state: str
    to_state: str
    trigger: str
    guard: Optional[Callable[..., bool]] = None
    action: Optional[Callable[..., None]] = None


class InvalidTransitionError(Exception):
    """Raised in strict mode when no valid transition matches."""


class StateMachine:
    """
    Explicit finite-state machine.

    Parameters
    ----------
    initial:
        Starting state name.
    strict:
        When True, ``trigger()`` raises :class:`InvalidTransitionError` if
        no matching transition is found. When False (default), logs a debug
        message and returns False — useful for idempotent event replay.
    """

    def __init__(self, initial: str, *, strict: bool = False) -> None:
        self._state = initial
        self._strict = strict
        self._transitions: List[_Transition] = []
        self._on_enter: Dict[str, List[Callable]] = {}
        self._on_exit: Dict[str, List[Callable]] = {}

    # ── Build API ─────────────────────────────────────────────────────────────

    def add(
        self,
        from_state: str,
        to_state: str,
        trigger: str,
        *,
        guard: Optional[Callable[..., bool]] = None,
        action: Optional[Callable[..., None]] = None,
    ) -> "StateMachine":
        """Register a transition. Returns self for fluent chaining."""
        self._transitions.append(
            _Transition(from_state, to_state, trigger, guard, action)
        )
        return self

    def on_enter(self, state: str, callback: Callable[..., None]) -> "StateMachine":
        """Register a callback that fires when *state* is entered."""
        self._on_enter.setdefault(state, []).append(callback)
        return self

    def on_exit(self, state: str, callback: Callable[..., None]) -> "StateMachine":
        """Register a callback that fires when *state* is exited."""
        self._on_exit.setdefault(state, []).append(callback)
        return self

    # ── Runtime API ───────────────────────────────────────────────────────────

    @property
    def state(self) -> str:
        return self._state

    def trigger(self, event: str, **kwargs) -> bool:
        """
        Fire *event*. Returns True if a transition occurred.

        All guard-passing transitions from the current state matching *event*
        are evaluated in registration order; the first match executes.
        Keyword arguments are forwarded to guards, actions, and hooks.
        """
        for t in self._transitions:
            if t.from_state != self._state or t.trigger != event:
                continue
            if t.guard is not None and not t.guard(**kwargs):
                continue
            # Transition matched — execute lifecycle
            for cb in self._on_exit.get(self._state, []):
                cb(**kwargs)
            if t.action is not None:
                t.action(**kwargs)
            prev, self._state = self._state, t.to_state
            logger.debug("SM: %s → %s (event=%s)", prev, t.to_state, event)
            for cb in self._on_enter.get(t.to_state, []):
                cb(**kwargs)
            return True

        msg = (
            f"StateMachine: no valid transition from '{self._state}' "
            f"on event '{event}'"
        )
        if self._strict:
            raise InvalidTransitionError(msg)
        logger.debug(msg)
        return False

    def can_trigger(self, event: str, **kwargs) -> bool:
        """Return True if *event* would produce a transition from the current state."""
        for t in self._transitions:
            if t.from_state == self._state and t.trigger == event:
                if t.guard is None or t.guard(**kwargs):
                    return True
        return False

    @property
    def valid_triggers(self) -> Set[str]:
        """All events that can fire from the current state (ignoring guards)."""
        return {t.trigger for t in self._transitions if t.from_state == self._state}

    def __repr__(self) -> str:
        return f"StateMachine(state={self._state!r})"
