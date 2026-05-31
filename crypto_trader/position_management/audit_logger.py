"""
crypto_trader.position_management.audit_logger
===============================================
Writes every evaluation cycle (input → AI decision → policy decision →
execution outcome) to both a JSONL file and the Postgres audit table.

Every record is independently parseable for replay, debugging, and
backtesting the management policy against historical position states.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Optional

from .schemas import AuditRecord, PolicyDecision, PositionManagementDecision, PositionManagementInput

logger = logging.getLogger("crypto_trader.position_management.audit_logger")

_AUDIT_DIR = Path.home() / ".crypto_trader" / "position_management_audit"
_AUDIT_DIR.mkdir(parents=True, exist_ok=True)


class AuditLogger:
    """
    Two-tier audit sink:
      1. JSONL flat file — zero-dependency, always available
      2. Postgres `position_management_audit` table — structured queries
    """

    def __init__(self, database_url: str = ""):
        self._db_url = database_url or os.getenv("DATABASE_URL", "")
        self._jsonl_path = _AUDIT_DIR / "audit.jsonl"
        self._db = self._connect_db() if self._db_url else None

    # ── Public API ────────────────────────────────────────────────────────

    def log(
        self,
        inp: PositionManagementInput,
        ai_decision: PositionManagementDecision,
        policy_decision: PolicyDecision,
        execution_outcome: str,
        trigger: str,
        cycle_id: Optional[str] = None,
        latency_ms: int = 0,
    ) -> str:
        cycle_id = cycle_id or str(uuid.uuid4())
        record = AuditRecord(
            position_id=inp.position.position_id,
            symbol=inp.position.symbol,
            cycle_id=cycle_id,
            trigger=trigger,
            input=inp,
            ai_decision=ai_decision,
            policy_decision=policy_decision,
            execution_outcome=execution_outcome,
            latency_ms=latency_ms,
        )
        self._write_jsonl(record)
        if self._db:
            self._write_db(record)
        return cycle_id

    # ── JSONL ────────────────────────────────────────────────────────────

    def _write_jsonl(self, record: AuditRecord):
        try:
            line = record.model_dump_json() + "\n"
            with open(self._jsonl_path, "a", encoding="utf-8") as fh:
                fh.write(line)
        except Exception as exc:
            logger.error("[AuditLogger] JSONL write failed: %s", exc)

    # ── Postgres ─────────────────────────────────────────────────────────

    def _connect_db(self):
        try:
            import psycopg2
            conn = psycopg2.connect(self._db_url)
            conn.autocommit = True
            self._ensure_schema(conn)
            return conn
        except ImportError:
            logger.warning("[AuditLogger] psycopg2 not installed — Postgres audit disabled")
            return None
        except Exception as exc:
            logger.warning("[AuditLogger] DB connect failed: %s — falling back to JSONL only", exc)
            return None

    def _ensure_schema(self, conn):
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS position_management_audit (
                    id              BIGSERIAL PRIMARY KEY,
                    cycle_id        TEXT        NOT NULL,
                    position_id     TEXT        NOT NULL,
                    symbol          TEXT        NOT NULL,
                    trigger         TEXT        NOT NULL,
                    action          TEXT        NOT NULL,
                    ai_action       TEXT        NOT NULL,
                    approved        BOOLEAN     NOT NULL,
                    confidence      NUMERIC(5,4),
                    score           NUMERIC(6,2),
                    reason          TEXT,
                    rejection_reason TEXT,
                    execution_outcome TEXT,
                    latency_ms      INTEGER,
                    full_record     JSONB       NOT NULL DEFAULT '{}',
                    created_at      BIGINT      NOT NULL
                                      DEFAULT (EXTRACT(EPOCH FROM NOW()) * 1000)::BIGINT
                );
                CREATE INDEX IF NOT EXISTS idx_pma_position ON position_management_audit (position_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_pma_symbol   ON position_management_audit (symbol, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_pma_action   ON position_management_audit (action);
            """)

    def _write_db(self, record: AuditRecord):
        if self._db is None:
            return
        try:
            row = {
                "cycle_id":          record.cycle_id,
                "position_id":       record.position_id,
                "symbol":            record.symbol,
                "trigger":           record.trigger,
                "action":            record.policy_decision.action,
                "ai_action":         record.ai_decision.action,
                "approved":          record.policy_decision.approved,
                "confidence":        record.ai_decision.confidence,
                "score":             record.ai_decision.score,
                "reason":            record.ai_decision.reason[:1000] if record.ai_decision.reason else None,
                "rejection_reason":  record.policy_decision.rejection_reason,
                "execution_outcome": record.execution_outcome,
                "latency_ms":        record.latency_ms,
                "full_record":       json.dumps(record.model_dump()),
                "created_at":        record.timestamp_ms,
            }
            with self._db.cursor() as cur:
                cur.execute("""
                    INSERT INTO position_management_audit
                        (cycle_id, position_id, symbol, trigger, action, ai_action,
                         approved, confidence, score, reason, rejection_reason,
                         execution_outcome, latency_ms, full_record, created_at)
                    VALUES
                        (%(cycle_id)s, %(position_id)s, %(symbol)s, %(trigger)s,
                         %(action)s, %(ai_action)s, %(approved)s, %(confidence)s,
                         %(score)s, %(reason)s, %(rejection_reason)s,
                         %(execution_outcome)s, %(latency_ms)s, %(full_record)s::jsonb,
                         %(created_at)s)
                """, row)
        except Exception as exc:
            logger.error("[AuditLogger] DB write failed: %s — continuing", exc)
            # Reconnect on next write
            try:
                self._db = self._connect_db()
            except Exception:
                pass
