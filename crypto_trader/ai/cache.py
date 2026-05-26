"""crypto_trader.ai.cache — sha256 prompt-keyed disk/memory cache with TTL."""

import json
import hashlib
import time
from typing import Optional
from pathlib import Path

from crypto_trader.ai.schemas import MarketStatePayload, LLMDecision

DATA_DIR = Path.home() / ".crypto_trader"
DATA_DIR.mkdir(exist_ok=True)


class LLMCache:
    def __init__(self, intraday_ttl_s: int = 45, swing_ttl_s: int = 300):
        self.intraday_ttl = intraday_ttl_s
        self.swing_ttl = swing_ttl_s
        self.dir = DATA_DIR / "llm_cache"
        self.dir.mkdir(exist_ok=True)

    def _hash_key(self, state: MarketStatePayload, sys_prompt: str, user_prompt: str) -> str:
        # Standardize state properties for deterministic hashing (Pydantic v2)
        state_serialized = state.model_dump_json()
        payload = f"{state_serialized}:{sys_prompt}:{user_prompt}"
        return hashlib.sha256(payload.encode()).hexdigest()

    def get(self, state: MarketStatePayload, sys_prompt: str, user_prompt: str) -> Optional[LLMDecision]:
        key = self._hash_key(state, sys_prompt, user_prompt)
        path = self.dir / f"cache_{key}.json"
        if not path.exists():
            return None

        try:
            with open(path, "r") as f:
                data = json.load(f)

            # Check TTL based on trading mode
            ttl = self.intraday_ttl if state.mode == "intraday" else self.swing_ttl
            if time.time() - data.get("cached_at", 0) > ttl:
                path.unlink()  # Delete stale file
                return None

            return LLMDecision(**data["decision"])
        except Exception:
            return None

    def set(self, state: MarketStatePayload, sys_prompt: str, user_prompt: str, decision: LLMDecision) -> None:
        key = self._hash_key(state, sys_prompt, user_prompt)
        path = self.dir / f"cache_{key}.json"

        data = {
            "cached_at": time.time(),
            "decision": decision.model_dump(),  # Pydantic v2
        }
        try:
            with open(path, "w") as f:
                json.dump(data, f)
        except Exception:
            pass
