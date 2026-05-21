"""
crypto_trader.journal — Trade Journal & Performance Analytics
===============================================================
Append-only JSONL journal for every trade decision.
Enables post-hoc analysis of LLM contribution, regime performance, etc.
"""

import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, List

logger = logging.getLogger("crypto_trader.journal")

DATA_DIR = Path.home() / ".crypto_trader" / "journal"
DATA_DIR.mkdir(parents=True, exist_ok=True)


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


class TradeJournal:
    """Append-only journal with daily file rotation."""

    def __init__(self):
        self._current_file = None
        self._current_date = None

    def _get_file(self) -> Path:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._current_date:
            self._current_date = today
            self._current_file = DATA_DIR / f"{today}.jsonl"
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
        }
        path = self._get_file()
        with open(path, "a") as f:
            f.write(json.dumps(close_record, default=str) + "\n")

    def get_recent(self, days: int = 7) -> List[Dict]:
        """Read recent journal entries."""
        entries = []
        for f in sorted(DATA_DIR.glob("*.jsonl"))[-days:]:
            with open(f, "r") as fh:
                for line in fh:
                    if line.strip():
                        entries.append(json.loads(line))
        return entries

    def analyze_llm_contribution(self, days: int = 30) -> Dict:
        """Analyze whether LLM-filtered trades performed better."""
        entries = self.get_recent(days)
        trades_with_llm = []
        trades_without_llm = []

        for e in entries:
            if e.get("event") == "close":
                continue
            if e.get("llm_weight", 0) > 0:
                trades_with_llm.append(e)
            else:
                trades_without_llm.append(e)

        def avg_pnl(trades):
            pnls = [t.get("realized_pnl", 0) for t in trades if t.get("realized_pnl") is not None]
            return sum(pnls) / len(pnls) if pnls else 0

        return {
            "with_llm_count": len(trades_with_llm),
            "with_llm_avg_pnl": avg_pnl(trades_with_llm),
            "without_llm_count": len(trades_without_llm),
            "without_llm_avg_pnl": avg_pnl(trades_without_llm),
            "llm_value_add": avg_pnl(trades_with_llm) - avg_pnl(trades_without_llm),
        }
