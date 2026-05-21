from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path.home() / ".crypto_trader" / "journal"
DATA_DIR.mkdir(parents=True, exist_ok=True)

class TradeJournal:
    def __init__(self):
        self.path = DATA_DIR / f"{datetime.now(timezone.utc).date().isoformat()}.jsonl"

    def append(self, row: dict):
        with self.path.open("a") as f:
            f.write(json.dumps(row) + "\n")
