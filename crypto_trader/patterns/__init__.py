"""
crypto_trader.patterns — reusable design-pattern base classes.

Importing from here is the stable public API; internal module structure
may change without breaking callers.
"""
from .circuit_breaker import CircuitBreaker, CircuitOpenError, CircuitState
from .specification import Specification
from .chain import Chain, Handler, HandlerResult
from .state_machine import StateMachine, InvalidTransitionError
from .unit_of_work import UnitOfWork

__all__ = [
    # Circuit Breaker
    "CircuitBreaker",
    "CircuitOpenError",
    "CircuitState",
    # Specification
    "Specification",
    # Chain of Responsibility
    "Chain",
    "Handler",
    "HandlerResult",
    # State Machine
    "StateMachine",
    "InvalidTransitionError",
    # Unit of Work
    "UnitOfWork",
]
