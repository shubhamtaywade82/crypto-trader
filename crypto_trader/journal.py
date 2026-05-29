"""
crypto_trader.journal — Trade Journal & Performance Analytics
===============================================================
Append-only JSONL journal for every trade decision.
Enables post-hoc analysis of LLM contribution, regime performance, etc.

Also provides TradeOutcomeRecord + TradeOutcomeJournal for capturing the
complete features-at-entry + outcome-at-exit record for every closed trade.
"""

import json
import logging
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional, Dict, List, Union

logger = logging.getLogger("crypto_trader.journal")

DATA_DIR = Path.home() / ".crypto_trader" / "journal"
DATA_DIR.mkdir(parents=True, exist_ok=True)

_OUTCOME_DATA_DIR = Path.home() / ".crypto_trader" / "outcomes"


@dataclass
class TradeJournalEntry:
    trade_id: str
    timestamp: int
    symbol: str
    regime: str
    regime_score: float
    technical_setup: Dict
    technical_score: float
    llm_bias: Optional[str]
    llm_confidence: Optional[float]
    llm_latency_ms: Optional[float]
    llm_age_seconds: Optional[float]
    llm_weight: float
    final_score: float
    margin_used: float
    notional: float
    entry_price: float
    exit_price: Optional[float] = None
    realized_pnl: Optional[float] = None
    exit_reason: Optional[str] = None
    funding_rate_at_entry: Optional[float] = None
    oi_delta_at_entry: Optional[float] = None
    taker_ratio_at_entry: Optional[float] = None
    execution_slippage: Optional[float] = None
    tds_paid: Optional[float] = None


class TradeJournal:
    """Append-only journal with daily file rotation."""

    def __init__(self, data_dir: Optional[Union[str, Path]] = None):
        self._data_dir = Path(data_dir) if data_dir else DATA_DIR
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._current_file = None
        self._current_date = None

    def _get_file(self) -> Path:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._current_date:
            self._current_date = today
            self._current_file = self._data_dir / f"{today}.jsonl"
        return self._current_file

    def log(self, entry: TradeJournalEntry):
        """Append entry to today's journal file."""
        path = self._get_file()
        with open(path, "a") as f:
            f.write(json.dumps(asdict(entry), default=str) + "\n")
        logger.debug(f"[JOURNAL] Logged trade {entry.trade_id}")

    def log_open(
        self,
        trade_id: str,
        symbol: str,
        regime: str,
        regime_score: float,
        setup: Dict,
        technical_score: float,
        llm_advice: Optional[Dict],
        llm_weight: float,
        final_score: float,
        margin: float,
        notional: float,
        entry_price: float,
        funding_rate: float,
        oi_delta: float,
        taker_ratio: float,
    ):
        entry = TradeJournalEntry(
            trade_id=trade_id,
            timestamp=int(datetime.now(timezone.utc).timestamp() * 1000),
            symbol=symbol,
            regime=regime,
            regime_score=regime_score,
            technical_setup=setup,
            technical_score=technical_score,
            llm_bias=llm_advice.get("bias") if llm_advice else None,
            llm_confidence=llm_advice.get("confidence") if llm_advice else None,
            llm_latency_ms=llm_advice.get("latency_ms") if llm_advice else None,
            llm_age_seconds=llm_advice.get("age_seconds") if llm_advice else None,
            llm_weight=llm_weight,
            final_score=final_score,
            margin_used=margin,
            notional=notional,
            entry_price=entry_price,
            funding_rate_at_entry=funding_rate,
            oi_delta_at_entry=oi_delta,
            taker_ratio_at_entry=taker_ratio,
        )
        self.log(entry)

    def log_close(
        self,
        trade_id: str,
        exit_price: float,
        realized_pnl: float,
        exit_reason: str,
        slippage: Optional[float] = None,
        tds: Optional[float] = None,
    ):
        """Update the last entry for this trade_id with close data."""
        # In append-only, we write a close record referencing the open
        close_record = {
            "trade_id": trade_id,
            "event": "close",
            "timestamp": int(datetime.now(timezone.utc).timestamp() * 1000),
            "exit_price": exit_price,
            "realized_pnl": realized_pnl,
            "exit_reason": exit_reason,
            "slippage": slippage,
            "tds_paid": tds,
        }
        path = self._get_file()
        with open(path, "a") as f:
            f.write(json.dumps(close_record, default=str) + "\n")

    def get_recent(self, days: int = 7) -> List[Dict]:
        """Read recent journal entries."""
        entries = []
        for f in sorted(self._data_dir.glob("*.jsonl"))[-days:]:
            with open(f, "r") as fh:
                for line in fh:
                    if line.strip():
                        entries.append(json.loads(line))
        return entries

    def analyze_llm_contribution(self, days: int = 30) -> Dict:
        """Analyze whether LLM-filtered trades performed better."""
        entries = self.get_recent(days)

        # Pass 1: index open records by trade_id (no "event" key = open record)
        open_records: Dict[str, dict] = {}
        for e in entries:
            if e.get("event") == "close":
                continue
            tid = e.get("trade_id")
            if tid:
                open_records[tid] = e

        # Pass 2: join close records with their opens to get full trade data
        closed_trades = []
        for e in entries:
            if e.get("event") != "close":
                continue
            tid = e.get("trade_id")
            open_rec = open_records.get(tid)
            if open_rec and e.get("realized_pnl") is not None:
                closed_trades.append({
                    "trade_id": tid,
                    "realized_pnl": float(e["realized_pnl"]),
                    "llm_weight": open_rec.get("llm_weight", 0.0),
                    "llm_bias": open_rec.get("llm_bias"),
                    "regime": open_rec.get("regime"),
                })

        with_llm = [t for t in closed_trades if t["llm_weight"] > 0]
        without_llm = [t for t in closed_trades if t["llm_weight"] == 0]

        def avg_pnl(trades):
            pnls = [t["realized_pnl"] for t in trades]
            return round(sum(pnls) / len(pnls), 4) if pnls else 0.0

        def win_rate(trades):
            if not trades:
                return 0.0
            return round(sum(1 for t in trades if t["realized_pnl"] > 0) / len(trades), 4)

        total_pnl = sum(t["realized_pnl"] for t in closed_trades)

        return {
            "total_trades": len(closed_trades),
            "total_pnl": round(total_pnl, 4),
            "win_rate": win_rate(closed_trades),
            "with_llm_count": len(with_llm),
            "with_llm_avg_pnl": avg_pnl(with_llm),
            "with_llm_win_rate": win_rate(with_llm),
            "without_llm_count": len(without_llm),
            "without_llm_avg_pnl": avg_pnl(without_llm),
            "without_llm_win_rate": win_rate(without_llm),
            "llm_value_add": round(avg_pnl(with_llm) - avg_pnl(without_llm), 4),
        }


# ─────────────────────────────────────────────────────────────────────────────
# TradeOutcomeRecord — one complete record per closed trade
# ─────────────────────────────────────────────────────────────────────────────

_RECORD_TYPE = "trade_outcome"


@dataclass
class TradeOutcomeRecord:
    # ── identity ──────────────────────────────────────────────
    trade_id: str
    symbol: str
    side: str                          # "long" | "short"
    opened_at: int                     # ms epoch
    closed_at: int                     # ms epoch

    # ── outcome at exit (required) ────────────────────────────
    entry_price: float
    exit_price: float
    quantity: float
    realized_pnl: float
    holding_time_s: float
    exit_reason: str

    # ── features at entry (optional) ─────────────────────────
    regime: Optional[str] = None
    regime_score: Optional[float] = None
    funding_rate: Optional[float] = None
    vol_regime: Optional[str] = None
    oi_delta: Optional[float] = None
    session: Optional[str] = None
    entry_reason: Optional[str] = None       # a.k.a. setup_type
    llm_action: Optional[str] = None
    llm_confidence: Optional[float] = None
    llm_rationale: Optional[str] = None

    # ── params used at entry (optional) ──────────────────────
    entry_band: Optional[float] = None
    stop_loss_pct: Optional[float] = None
    risk_per_trade_pct: Optional[float] = None
    leverage: Optional[int] = None

    # ── extra outcome fields (optional) ──────────────────────
    pnl_r: Optional[float] = None            # realized_pnl / initial_risk_amount
    mfe: Optional[float] = None              # max favourable excursion
    mae: Optional[float] = None              # max adverse excursion
    slippage_bps: Optional[float] = None


# ─────────────────────────────────────────────────────────────────────────────
# TradeOutcomeJournal — append-only JSONL sink for TradeOutcomeRecord
# ─────────────────────────────────────────────────────────────────────────────

class TradeOutcomeJournal:
    """
    Append-only JSONL journal for closed-trade outcome records.

    One file per day (same rotation scheme as TradeJournal).
    Each line is a JSON object with ``record_type = "trade_outcome"``.

    Parameters
    ----------
    data_dir:
        Directory to store JSONL files.  Defaults to
        ``~/.crypto_trader/outcomes``.  Override in tests via ``tmp_path``.
    """

    def __init__(self, data_dir: Optional[Union[str, Path]] = None):
        self._data_dir = Path(data_dir) if data_dir else _OUTCOME_DATA_DIR
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._current_file: Optional[Path] = None
        self._current_date: Optional[str] = None

    # ── internal ──────────────────────────────────────────────

    def _get_file(self) -> Path:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._current_date:
            self._current_date = today
            self._current_file = self._data_dir / f"{today}.jsonl"
        return self._current_file  # type: ignore[return-value]

    # ── write ─────────────────────────────────────────────────

    def record(self, outcome: TradeOutcomeRecord) -> None:
        """Append a completed TradeOutcomeRecord to today's file."""
        payload = asdict(outcome)
        payload["record_type"] = _RECORD_TYPE
        path = self._get_file()
        with open(path, "a") as fh:
            fh.write(json.dumps(payload, default=str) + "\n")
        logger.debug("[OUTCOME] Recorded trade %s", outcome.trade_id)

    # ── read ──────────────────────────────────────────────────

    def load_all(self) -> Iterator[TradeOutcomeRecord]:
        """
        Yield every TradeOutcomeRecord from all stored files in
        chronological order (oldest file first, line order within file).
        """
        for path in sorted(self._data_dir.glob("*.jsonl")):
            with open(path, "r") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        logger.warning("[OUTCOME] Skipping malformed line in %s", path)
                        continue
                    if payload.get("record_type") != _RECORD_TYPE:
                        continue  # skip non-outcome lines (e.g. mixed files)
                    payload.pop("record_type", None)
                    yield TradeOutcomeRecord(**payload)
