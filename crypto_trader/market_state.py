"""
crypto_trader.market_state — Lightweight market state trackers.
"""

from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional


@dataclass
class OIMetrics:
    oi_delta_5m: float = 0.0
    oi_delta_15m: float = 0.0
    oi_velocity: float = 0.0
    oi_acceleration: float = 0.0


class OpenInterestTracker:
    """Tracks rolling OI snapshots and derives delta/velocity/acceleration."""

    def __init__(self, max_points: int = 64):
        self.values: Deque[float] = deque(maxlen=max_points)
        self.velocities: Deque[float] = deque(maxlen=max_points)

    def update(self, oi: float) -> OIMetrics:
        if oi <= 0:
            return OIMetrics()
        self.values.append(float(oi))
        if len(self.values) >= 2:
            self.velocities.append(self.values[-1] - self.values[-2])

        d5 = self._delta(1)
        d15 = self._delta(3)
        vel = self.velocities[-1] if self.velocities else 0.0
        acc = (self.velocities[-1] - self.velocities[-2]) if len(self.velocities) >= 2 else 0.0
        return OIMetrics(oi_delta_5m=d5, oi_delta_15m=d15, oi_velocity=vel, oi_acceleration=acc)

    def _delta(self, steps: int) -> float:
        if len(self.values) <= steps:
            return 0.0
        prev = self.values[-1 - steps]
        if prev == 0:
            return 0.0
        return ((self.values[-1] - prev) / prev) * 100.0

