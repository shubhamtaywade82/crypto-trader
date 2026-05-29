"""
Chain of Responsibility — ordered handler pipeline where each link decides
whether to process the request itself or forward it to the next handler.

A handler that cannot or should not process the request calls ``self.forward(ctx)``
to pass it down. A handler that stops the chain returns ``HandlerResult.stop(reason)``.

Usage::

    class AuthHandler(Handler[SignalCtx]):
        def handle(self, ctx):
            if not ctx.authenticated:
                return HandlerResult.stop("not authenticated")
            return self.forward(ctx)   # pass to next

    pipeline = Chain(AuthHandler(), DupHandler(), RiskGateHandler(), ExecHandler())
    result = pipeline.run(ctx)
    if not result.ok:
        log(result.reason)
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Generic, List, Optional, TypeVar

T = TypeVar("T")


@dataclass
class HandlerResult:
    ok: bool
    reason: str = ""
    payload: Any = None

    @classmethod
    def stop(cls, reason: str = "", payload: Any = None) -> "HandlerResult":
        """Terminate the chain — request rejected or handled terminally."""
        return cls(ok=False, reason=reason, payload=payload)

    @classmethod
    def proceed(cls, payload: Any = None) -> "HandlerResult":
        """Chain exhausted without rejection — request accepted."""
        return cls(ok=True, payload=payload)


class Handler(ABC, Generic[T]):
    """Single link in a chain. Call ``self.forward(ctx)`` to continue."""

    def __init__(self) -> None:
        self._next: Optional["Handler[T]"] = None

    def set_next(self, handler: "Handler[T]") -> "Handler[T]":
        self._next = handler
        return handler

    def forward(self, ctx: T) -> HandlerResult:
        """Pass to the next handler, or return proceed() if this is the last link."""
        if self._next is not None:
            return self._next.handle(ctx)
        return HandlerResult.proceed()

    @abstractmethod
    def handle(self, ctx: T) -> HandlerResult:
        ...


class Chain(Generic[T]):
    """Builds and runs a linear handler pipeline."""

    def __init__(self, *handlers: Handler[T]) -> None:
        self._handlers: List[Handler[T]] = list(handlers)
        for i in range(len(handlers) - 1):
            handlers[i].set_next(handlers[i + 1])

    def run(self, ctx: T) -> HandlerResult:
        if not self._handlers:
            return HandlerResult.proceed()
        return self._handlers[0].handle(ctx)

    def append(self, handler: Handler[T]) -> "Chain[T]":
        """Add a handler to the end of the chain at runtime."""
        if self._handlers:
            self._handlers[-1].set_next(handler)
        self._handlers.append(handler)
        return self

    def __len__(self) -> int:
        return len(self._handlers)
