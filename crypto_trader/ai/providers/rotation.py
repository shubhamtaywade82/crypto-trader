"""crypto_trader.ai.providers.rotation — Shared resource rotation helper for keys/hosts failover."""

import time
import logging
import threading
from typing import List, Optional, Tuple

logger = logging.getLogger("crypto_trader.ai.providers.rotation")


class ResourceRotator:
    def __init__(self, items: List[str], cooldown_s: int = 60):
        self.items = [item.strip() for item in items if item.strip()]
        self.cooldown_s = cooldown_s
        self.cooldown_until = [0.0] * len(self.items)
        self.current_idx = 0
        self.lock = threading.Lock()

    def get_next_available(self) -> Tuple[Optional[str], Optional[int]]:
        """Get the next item in a round-robin fashion, skipping those on cooldown.
        
        Advances the internal pointer proactively so the next call gets a different item.
        Returns (item, index), or (None, None) if all items are on cooldown or empty.
        """
        with self.lock:
            n = len(self.items)
            if n == 0:
                return None, None
            
            now = time.time()
            for i in range(n):
                idx = (self.current_idx + i) % n
                if now >= self.cooldown_until[idx]:
                    # Advance the pointer proactively for the next request
                    self.current_idx = (idx + 1) % n
                    return self.items[idx], idx
            
            return None, None

    def record_failure(self, index: int) -> None:
        """Mark an item as failed and place it on cooldown."""
        with self.lock:
            if 0 <= index < len(self.items):
                self.cooldown_until[index] = time.time() + self.cooldown_s
                logger.warning(
                    "[ResourceRotator] Item index %d (%s) put on cooldown for %ds",
                    index, self.items[index], self.cooldown_s
                )

    def record_success(self, index: int) -> None:
        """Mark an item as successful (clears any cooldown)."""
        with self.lock:
            if 0 <= index < len(self.items):
                self.cooldown_until[index] = 0.0

    def has_available(self) -> bool:
        """Check if at least one item is configured and not on cooldown."""
        with self.lock:
            if not self.items:
                return False
            now = time.time()
            return any(now >= t for t in self.cooldown_until)

    def total_configured(self) -> int:
        return len(self.items)
