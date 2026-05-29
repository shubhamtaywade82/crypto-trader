"""
Unit tests for crypto_trader.patterns — all base-class design patterns.
"""
import time
import threading
import pytest

from crypto_trader.patterns import (
    CircuitBreaker, CircuitOpenError, CircuitState,
    Specification,
    Chain, Handler, HandlerResult,
    StateMachine, InvalidTransitionError,
    UnitOfWork,
)


# ─────────────────────────────────────────────────────────────────────────────
# CircuitBreaker
# ─────────────────────────────────────────────────────────────────────────────

class TestCircuitBreaker:

    def test_initial_state_is_closed(self):
        cb = CircuitBreaker(name="test", failure_threshold=3, recovery_timeout=1.0)
        assert cb.state == CircuitState.CLOSED
        assert not cb.is_open

    def test_successful_calls_stay_closed(self):
        cb = CircuitBreaker(name="test", failure_threshold=3)
        for _ in range(10):
            assert cb.call(lambda: 42) == 42
        assert cb.state == CircuitState.CLOSED

    def test_trips_after_threshold_failures(self):
        cb = CircuitBreaker(name="test", failure_threshold=3)

        def boom():
            raise ValueError("fail")

        for _ in range(3):
            with pytest.raises(ValueError):
                cb.call(boom)

        assert cb.is_open

    def test_open_rejects_immediately(self):
        cb = CircuitBreaker(name="test", failure_threshold=1)
        with pytest.raises(ValueError):
            cb.call(lambda: (_ for _ in ()).throw(ValueError("x")))
        with pytest.raises(CircuitOpenError):
            cb.call(lambda: None)

    def test_half_open_after_recovery_timeout(self):
        cb = CircuitBreaker(name="test", failure_threshold=1, recovery_timeout=0.05)
        with pytest.raises(Exception):
            cb.call(lambda: (_ for _ in ()).throw(Exception("x")))
        time.sleep(0.06)
        assert cb.state == CircuitState.HALF_OPEN

    def test_half_open_success_closes(self):
        cb = CircuitBreaker(name="test", failure_threshold=1, recovery_timeout=0.05)
        with pytest.raises(Exception):
            cb.call(lambda: (_ for _ in ()).throw(Exception("x")))
        time.sleep(0.06)
        assert cb.state == CircuitState.HALF_OPEN
        cb.call(lambda: None)  # probe succeeds
        assert cb.state == CircuitState.CLOSED

    def test_half_open_failure_reopens(self):
        cb = CircuitBreaker(name="test", failure_threshold=1, recovery_timeout=0.05)
        with pytest.raises(Exception):
            cb.call(lambda: (_ for _ in ()).throw(Exception("x")))
        time.sleep(0.06)
        with pytest.raises(Exception):
            cb.call(lambda: (_ for _ in ()).throw(Exception("x")))
        assert cb.is_open

    def test_manual_reset(self):
        cb = CircuitBreaker(name="test", failure_threshold=1)
        with pytest.raises(Exception):
            cb.call(lambda: (_ for _ in ()).throw(Exception("x")))
        assert cb.is_open
        cb.reset()
        assert cb.state == CircuitState.CLOSED

    def test_manual_trip(self):
        cb = CircuitBreaker(name="test", failure_threshold=100)
        cb.trip()
        assert cb.is_open

    def test_decorator_usage(self):
        cb = CircuitBreaker(name="dec", failure_threshold=2)

        @cb
        def risky():
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            risky()
        with pytest.raises(RuntimeError):
            risky()
        assert cb.is_open
        with pytest.raises(CircuitOpenError):
            risky()

    def test_only_expected_exceptions_count(self):
        cb = CircuitBreaker(
            name="test", failure_threshold=2, expected_exception=ValueError
        )
        # TypeError is not in expected_exception — should re-raise but NOT count
        with pytest.raises(TypeError):
            cb.call(lambda: (_ for _ in ()).throw(TypeError("wrong type")))
        assert cb.failure_count == 0

    def test_thread_safety(self):
        cb = CircuitBreaker(name="threads", failure_threshold=50, recovery_timeout=10)
        errors = []

        def worker():
            try:
                cb.call(lambda: (_ for _ in ()).throw(Exception("fail")))
            except Exception:
                pass
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors


# ─────────────────────────────────────────────────────────────────────────────
# Specification
# ─────────────────────────────────────────────────────────────────────────────

class _TrueSpec(Specification):
    def is_satisfied_by(self, _) -> tuple:
        return True, ""

class _FalseSpec(Specification):
    def __init__(self, reason="failed"):
        self._reason = reason
    def is_satisfied_by(self, _) -> tuple:
        return False, self._reason


class TestSpecification:

    def test_single_true(self):
        ok, reason = _TrueSpec().is_satisfied_by(None)
        assert ok and reason == ""

    def test_single_false(self):
        ok, reason = _FalseSpec("bad").is_satisfied_by(None)
        assert not ok and reason == "bad"

    def test_and_both_true(self):
        ok, reason = (_TrueSpec() & _TrueSpec()).is_satisfied_by(None)
        assert ok

    def test_and_first_false_short_circuits(self):
        ok, reason = (_FalseSpec("A") & _FalseSpec("B")).is_satisfied_by(None)
        assert not ok and reason == "A"

    def test_and_second_false(self):
        ok, reason = (_TrueSpec() & _FalseSpec("B")).is_satisfied_by(None)
        assert not ok and reason == "B"

    def test_or_first_true_short_circuits(self):
        ok, _ = (_TrueSpec() | _FalseSpec("B")).is_satisfied_by(None)
        assert ok

    def test_or_all_false(self):
        ok, reason = (_FalseSpec("A") | _FalseSpec("B")).is_satisfied_by(None)
        assert not ok and "A" in reason and "B" in reason

    def test_not_true_becomes_false(self):
        ok, _ = (~_TrueSpec()).is_satisfied_by(None)
        assert not ok

    def test_not_false_becomes_true(self):
        ok, _ = (~_FalseSpec()).is_satisfied_by(None)
        assert ok

    def test_complex_composition(self):
        # (True & False) | True  → should be True (OR of False and True)
        spec = (_TrueSpec() & _FalseSpec("inner")) | _TrueSpec()
        ok, _ = spec.is_satisfied_by(None)
        assert ok

    def test_chained_and_flattens(self):
        # a & b & c should still short-circuit on first failure
        spec = _TrueSpec() & _TrueSpec() & _FalseSpec("third") & _TrueSpec()
        ok, reason = spec.is_satisfied_by(None)
        assert not ok and reason == "third"


# ─────────────────────────────────────────────────────────────────────────────
# Chain of Responsibility
# ─────────────────────────────────────────────────────────────────────────────

class _PassHandler(Handler):
    def handle(self, ctx):
        ctx["visited"].append(type(self).__name__)
        return self.forward(ctx)

class _StopHandler(Handler):
    def handle(self, ctx):
        ctx["visited"].append("STOP")
        return HandlerResult.stop("stopped here")

class _MutateHandler(Handler):
    def handle(self, ctx):
        ctx["value"] *= 2
        return self.forward(ctx)


class TestChain:

    def test_all_handlers_called(self):
        class H1(_PassHandler): pass
        class H2(_PassHandler): pass
        ctx = {"visited": []}
        result = Chain(H1(), H2()).run(ctx)
        assert result.ok
        assert ctx["visited"] == ["H1", "H2"]

    def test_stop_terminates_chain(self):
        class H1(_PassHandler): pass
        class H2(_PassHandler): pass
        ctx = {"visited": []}
        result = Chain(H1(), _StopHandler(), H2()).run(ctx)
        assert not result.ok
        assert result.reason == "stopped here"
        assert "H2" not in ctx["visited"]

    def test_empty_chain_proceeds(self):
        result = Chain().run({})
        assert result.ok

    def test_mutation_through_chain(self):
        ctx = {"value": 3}
        Chain(_MutateHandler(), _MutateHandler()).run(ctx)
        assert ctx["value"] == 12  # 3 * 2 * 2

    def test_append_adds_handler(self):
        class H(_PassHandler): pass
        chain = Chain(H())
        chain.append(H())
        assert len(chain) == 2

    def test_handler_result_classmethod(self):
        r = HandlerResult.stop("oops", payload=42)
        assert not r.ok and r.reason == "oops" and r.payload == 42


# ─────────────────────────────────────────────────────────────────────────────
# StateMachine
# ─────────────────────────────────────────────────────────────────────────────

class TestStateMachine:

    def _position_sm(self, initial="open"):
        sm = StateMachine(initial)
        sm.add("open",     "open",     "add")
        sm.add("open",     "reducing", "partial_close")
        sm.add("reducing", "reducing", "partial_close")
        sm.add("open",     "closed",   "full_close")
        sm.add("reducing", "closed",   "full_close")
        return sm

    def test_initial_state(self):
        sm = self._position_sm()
        assert sm.state == "open"

    def test_valid_transition(self):
        sm = self._position_sm()
        assert sm.trigger("partial_close")
        assert sm.state == "reducing"

    def test_full_close_from_open(self):
        sm = self._position_sm()
        sm.trigger("full_close")
        assert sm.state == "closed"

    def test_full_close_from_reducing(self):
        sm = self._position_sm("reducing")
        sm.trigger("full_close")
        assert sm.state == "closed"

    def test_invalid_trigger_returns_false(self):
        sm = self._position_sm("closed")
        result = sm.trigger("full_close")
        assert result is False  # no transition from closed
        assert sm.state == "closed"  # unchanged

    def test_strict_mode_raises_on_invalid(self):
        sm = StateMachine("closed", strict=True)
        with pytest.raises(InvalidTransitionError):
            sm.trigger("anything")

    def test_can_trigger_respects_state(self):
        sm = self._position_sm()
        assert sm.can_trigger("partial_close")
        assert not sm.can_trigger("nonexistent")

    def test_valid_triggers_set(self):
        sm = self._position_sm()
        triggers = sm.valid_triggers
        assert "partial_close" in triggers
        assert "full_close" in triggers
        assert "add" in triggers

    def test_on_enter_callback(self):
        events = []
        sm = self._position_sm()
        sm.on_enter("closed", lambda **kw: events.append("entered_closed"))
        sm.trigger("full_close")
        assert events == ["entered_closed"]

    def test_on_exit_callback(self):
        events = []
        sm = self._position_sm()
        sm.on_exit("open", lambda **kw: events.append("left_open"))
        sm.trigger("partial_close")
        assert events == ["left_open"]

    def test_guard_blocks_transition(self):
        sm = StateMachine("open")
        sm.add("open", "closed", "close", guard=lambda qty=0, **kw: qty == 0)
        assert not sm.trigger("close", qty=5)   # guard fails
        assert sm.state == "open"
        assert sm.trigger("close", qty=0)        # guard passes
        assert sm.state == "closed"

    def test_action_called_on_transition(self):
        log = []
        sm = StateMachine("open")
        sm.add("open", "closed", "close", action=lambda **kw: log.append("action"))
        sm.trigger("close")
        assert log == ["action"]

    def test_kwargs_forwarded_to_callbacks(self):
        received = {}
        sm = StateMachine("open")
        sm.add("open", "closed", "close")
        sm.on_enter("closed", lambda **kw: received.update(kw))
        sm.trigger("close", price=100.0, reason="tp")
        assert received == {"price": 100.0, "reason": "tp"}


# ─────────────────────────────────────────────────────────────────────────────
# UnitOfWork
# ─────────────────────────────────────────────────────────────────────────────

class _MemoryUoW(UnitOfWork):
    def __init__(self):
        super().__init__()
        self.began = False
        self.committed = False
        self.rolled_back = False
        self.store: list = []

    def _begin(self):
        self.began = True

    def _commit(self):
        self.committed = True

    def _rollback(self):
        self.rolled_back = True
        self.store.clear()


class TestUnitOfWork:

    def test_happy_path_commits(self):
        uow = _MemoryUoW()
        with uow:
            uow.store.append("item")
        assert uow.began
        assert uow.committed
        assert not uow.rolled_back

    def test_exception_rolls_back(self):
        uow = _MemoryUoW()
        with pytest.raises(ValueError):
            with uow:
                uow.store.append("item")
                raise ValueError("oops")
        assert uow.rolled_back
        assert not uow.committed

    def test_events_collected_and_popped(self):
        uow = _MemoryUoW()
        with uow:
            uow.collect_event("evt_a")
            uow.collect_event("evt_b")
        events = uow.pop_events()
        assert events == ["evt_a", "evt_b"]
        assert uow.pop_events() == []  # drained

    def test_events_cleared_on_rollback(self):
        uow = _MemoryUoW()
        with pytest.raises(RuntimeError):
            with uow:
                uow.collect_event("evt")
                raise RuntimeError("fail")
        assert uow.pop_events() == []  # cleared on rollback

    def test_does_not_suppress_exception(self):
        uow = _MemoryUoW()
        with pytest.raises(KeyError):
            with uow:
                raise KeyError("should propagate")


# ─────────────────────────────────────────────────────────────────────────────
# Integration: Specification + StateMachine used together (risk-gate analogy)
# ─────────────────────────────────────────────────────────────────────────────

class TestIntegration:

    def test_risk_gate_composition(self):
        """Simulate a composed risk gate like RiskManager.can_trade()."""
        from dataclasses import dataclass

        @dataclass
        class Ctx:
            kill_switch: bool = False
            balance: float = 1000.0
            max_dd: float = 0.2

        class KillSpec(Specification[Ctx]):
            def is_satisfied_by(self, c):
                return (not c.kill_switch), "kill switch active" if c.kill_switch else ""

        class BalanceSpec(Specification[Ctx]):
            def is_satisfied_by(self, c):
                ok = c.balance > 0
                return ok, "" if ok else "zero balance"

        gate = KillSpec() & BalanceSpec()

        ok, _ = gate.is_satisfied_by(Ctx())
        assert ok

        ok, reason = gate.is_satisfied_by(Ctx(kill_switch=True))
        assert not ok and "kill switch" in reason

        ok, reason = gate.is_satisfied_by(Ctx(balance=0.0))
        assert not ok and "zero balance" in reason

    def test_position_sm_with_on_enter_hook(self):
        """Simulate position lifecycle with side-effect on close."""
        closed_at = []

        sm = StateMachine("open")
        sm.add("open",     "reducing", "partial_close")
        sm.add("reducing", "closed",   "full_close")
        sm.add("open",     "closed",   "full_close")
        sm.on_enter("closed", lambda ts=0, **kw: closed_at.append(ts))

        sm.trigger("partial_close")
        assert sm.state == "reducing"
        assert not closed_at

        sm.trigger("full_close", ts=1234567890)
        assert sm.state == "closed"
        assert closed_at == [1234567890]
