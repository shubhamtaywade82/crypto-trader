"""Redis Streams signal pipeline + Postgres projection reducer (no server needed).

Uses an in-memory FakeRedis implementing the consumer-group / PEL subset the
RedisStreamBus relies on, so the idempotency / poison-pill / recovery behaviour
is exercised deterministically.
"""
import pytest

from crypto_trader.infra.redis_streams import RedisStreamBus
from crypto_trader.execution.signal_bus import Signal, SignalPublisher, SignalConsumer
from crypto_trader.storage.projection import ProjectionState


# ── in-memory fake Redis (streams + string subset) ───────────────────────────
class FakeRedis:
    def __init__(self):
        self.streams = {}          # stream -> list[(id, fields)]
        self.groups = {}           # (stream, group) -> {"last": id, "pel": {id: {"delivered": n}}}
        self._seq = 0
        self.kv = {}

    def xadd(self, stream, fields):
        self._seq += 1
        msg_id = f"{self._seq}-0"
        self.streams.setdefault(stream, []).append((msg_id, dict(fields)))
        return msg_id

    def xgroup_create(self, stream, group, id="0", mkstream=False):
        if mkstream:
            self.streams.setdefault(stream, [])
        key = (stream, group)
        if key in self.groups:
            raise Exception("BUSYGROUP Consumer Group name already exists")
        self.groups[key] = {"last": "0-0", "pel": {}}

    def xreadgroup(self, group, consumer, streams, count=10, block=None):
        out = []
        for stream, key in streams.items():
            g = self.groups[(stream, group)]
            msgs = []
            if key == ">":
                for mid, fields in self.streams.get(stream, []):
                    if _gt(mid, g["last"]):
                        g["last"] = mid
                        g["pel"][mid] = {"delivered": 1}
                        msgs.append((mid, fields))
                        if len(msgs) >= count:
                            break
            else:  # "0" -> this consumer's pending backlog; re-deliver (claim)
                for mid, fields in self.streams.get(stream, []):
                    if mid in g["pel"]:
                        g["pel"][mid]["delivered"] += 1
                        msgs.append((mid, fields))
            out.append((stream, msgs))
        return out

    def xack(self, stream, group, msg_id):
        self.groups[(stream, group)]["pel"].pop(msg_id, None)
        return 1

    def xpending_range(self, stream, group, min, max, count=1):
        g = self.groups[(stream, group)]
        ids = list(g["pel"].keys()) if min == "-" else [m for m in g["pel"] if min <= m <= max]
        return [{"message_id": m, "times_delivered": g["pel"][m]["delivered"]} for m in ids[:count]]

    def set(self, key, val, nx=False, ex=None):
        if nx and key in self.kv:
            return None
        self.kv[key] = val
        return True

    def get(self, key):
        return self.kv.get(key)


def _gt(a, b):
    return tuple(int(x) for x in a.split("-")) > tuple(int(x) for x in b.split("-"))


# ── adapters used in tests ────────────────────────────────────────────────────
class RecordingAdapter:
    def __init__(self):
        self.executed = []

    def execute(self, signal):
        self.executed.append(signal.signal_id)


class FailingAdapter:
    def __init__(self):
        self.attempts = 0

    def execute(self, signal):
        self.attempts += 1
        raise RuntimeError("boom")


def _bus():
    return RedisStreamBus(FakeRedis())


def _sig(**kw):
    base = dict(strategy_id="S1", symbol="SOLUSDT", side="LONG", quantity=1.0, mode="paper", price=100.0)
    base.update(kw)
    return Signal(**base)


# ── happy path + ack ──────────────────────────────────────────────────────────
def test_signal_executed_and_acked():
    bus = _bus()
    SignalPublisher(bus).emit(_sig())
    adapter = RecordingAdapter()
    c = SignalConsumer(bus, {"paper": adapter}, consumer="w1")
    c.run_forever(block_ms=None, max_batches=1)
    assert len(adapter.executed) == 1
    # PEL is empty (acked)
    assert bus.r.groups[(c.stream, c.group)]["pel"] == {}


# ── idempotency ───────────────────────────────────────────────────────────────
def test_duplicate_signal_suppressed():
    bus = _bus()
    pub = SignalPublisher(bus)
    s = _sig()
    pub.emit(s)
    pub.emit(s)   # same strategy_id + timestamp -> idempotency key collision
    adapter = RecordingAdapter()
    SignalConsumer(bus, {"paper": adapter}, consumer="w1").run_forever(block_ms=None, max_batches=1)
    assert len(adapter.executed) == 1


# ── poison pill -> DLQ ─────────────────────────────────────────────────────────
def test_poison_pill_routed_to_dlq():
    bus = _bus()
    SignalPublisher(bus).emit(_sig())
    adapter = FailingAdapter()
    c = SignalConsumer(bus, {"paper": adapter}, consumer="w1", max_deliveries=3)
    # Several batches: each retry from the PEL bumps the delivery count until DLQ.
    c.run_forever(block_ms=None, max_batches=6)
    dlq = bus.r.streams.get("execution:signals:dlq", [])
    assert len(dlq) == 1
    # Once DLQ'd it is acked out of the PEL and not retried forever.
    assert bus.r.groups[(c.stream, c.group)]["pel"] == {}


# ── risk gate veto ──────────────────────────────────────────────────────────────
def test_risk_gate_veto_acks_without_execution():
    bus = _bus()
    SignalPublisher(bus).emit(_sig())
    adapter = RecordingAdapter()
    c = SignalConsumer(bus, {"paper": adapter}, consumer="w1", risk_gate=lambda s: False)
    c.run_forever(block_ms=None, max_batches=1)
    assert adapter.executed == []
    assert bus.r.groups[(c.stream, c.group)]["pel"] == {}   # vetoed = terminal, acked


# ── unknown mode -> DLQ ────────────────────────────────────────────────────────
def test_unknown_mode_routed_to_dlq():
    bus = _bus()
    SignalPublisher(bus).emit(_sig(mode="weird"))
    c = SignalConsumer(bus, {"paper": RecordingAdapter()}, consumer="w1")
    c.run_forever(block_ms=None, max_batches=1)
    assert len(bus.r.streams.get("execution:signals:dlq", [])) == 1


# ── boot recovery from PEL ─────────────────────────────────────────────────────
def test_recover_pending_processes_inflight():
    bus = _bus()
    SignalPublisher(bus).emit(_sig())
    adapter = RecordingAdapter()
    c = SignalConsumer(bus, {"paper": adapter}, consumer="w1")
    c.setup()
    # Simulate a prior worker that read the message (into PEL) but crashed pre-ack.
    bus.read_new(c.stream, c.group, "w1")
    n = c.recover_pending()
    assert n == 1 and len(adapter.executed) == 1


# ── Postgres projection reducer (pure, no DB) ─────────────────────────────────
def _ev(t, **payload):
    return {"event_type": t, "ts": 1, "payload": payload}


def test_projection_tracks_open_and_close():
    st = ProjectionState(mode="paper")
    st.apply(_ev("ORDER_CREATED", order_id="o1", symbol="SOLUSDT", side="LONG", quantity=2, status="NEW"))
    st.apply(_ev("POSITION_OPENED", symbol="SOLUSDT", side="LONG", quantity=2, execution_price=100, mode="paper"))
    assert "SOLUSDT" in st.positions
    assert st.positions["SOLUSDT"]["qty"] == 2.0
    assert st.positions["SOLUSDT"]["mode"] == "paper"

    st.apply(_ev("POSITION_CLOSED", symbol="SOLUSDT"))
    assert "SOLUSDT" not in st.positions


def test_projection_partial_close_and_fills():
    st = ProjectionState(mode="live")
    st.apply(_ev("POSITION_OPENED", symbol="BTCUSDT", side="LONG", quantity=3, execution_price=50000))
    st.apply(_ev("ORDER_FILLED", order_id="x", symbol="BTCUSDT", side="LONG", quantity=1, fill_price=50000, fee=0.5))
    st.apply(_ev("POSITION_PARTIALLY_CLOSED", symbol="BTCUSDT", closed_qty=1))
    assert st.positions["BTCUSDT"]["qty"] == 2.0
    assert len(st.fills) == 1 and st.fills[0]["fee"] == 0.5


def test_projection_recon_adjust_corrects_truth():
    st = ProjectionState(mode="live")
    st.apply(_ev("POSITION_OPENED", symbol="ETHUSDT", side="LONG", quantity=5, execution_price=3000))
    # Reconciler says the venue actually holds 4 (drift correction).
    st.apply(_ev("RECON_ADJUST", symbol="ETHUSDT", side="LONG", quantity=4, avg_price=3000))
    assert st.positions["ETHUSDT"]["qty"] == 4.0
    # Or that the position is flat -> removed.
    st.apply(_ev("RECON_ADJUST", symbol="ETHUSDT", quantity=0))
    assert "ETHUSDT" not in st.positions
