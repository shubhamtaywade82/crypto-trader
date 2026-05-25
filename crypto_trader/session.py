"""
crypto_trader.session — Trading Session & Kill Zone Detection
=============================================================
Classifies UTC time into trading sessions and identifies institutional
kill zones (high-probability entry windows).

Sessions:
    ASIA      00:00–07:00 UTC  (mean reversion favoured)
    LONDON    07:00–12:00 UTC  (breakout / momentum)
    NY        12:00–17:00 UTC  (momentum / continuation)
    LATE_NY   17:00–22:00 UTC  (reversal / fading)
    DEAD      22:00–00:00 UTC  (no new entries)

Kill zones are sub-windows within sessions where institutional order
flow tends to be highest. Trading OUTSIDE kill zones is not blocked by
default — enable REQUIRE_KILL_ZONE in config to restrict entries.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class TradingSession(str, Enum):
    ASIA    = "ASIA"
    LONDON  = "LONDON"
    NY      = "NY"
    LATE_NY = "LATE_NY"
    DEAD    = "DEAD"


# Kill zones: (start_hour_utc_inclusive, end_hour_utc_exclusive)
_KILL_ZONES: dict[str, tuple[int, int]] = {
    "LONDON_OPEN":  (7,  9),
    "NY_OPEN":      (12, 15),
    "NY_LONDON_OVERLAP": (13, 16),
}

# Session boundaries: (start_hour_utc_inclusive, end_hour_utc_exclusive, session)
_SESSION_BOUNDS: list[tuple[int, int, TradingSession]] = [
    (0,  7,  TradingSession.ASIA),
    (7,  12, TradingSession.LONDON),
    (12, 17, TradingSession.NY),
    (17, 22, TradingSession.LATE_NY),
    (22, 24, TradingSession.DEAD),
]

# Which sessions are preferred for each strategy class
SESSION_STRATEGY_PREFERENCE: dict[TradingSession, list[str]] = {
    TradingSession.ASIA:    ["playbook_ares"],              # mean reversion only
    TradingSession.LONDON:  ["playbook_b", "playbook_a"],   # breakout first
    TradingSession.NY:      ["playbook_a", "playbook_b"],   # momentum continuation
    TradingSession.LATE_NY: ["playbook_ares", "playbook_a"],# reversal / fading
    TradingSession.DEAD:    [],                             # no entries
}


@dataclass
class SessionContext:
    session: TradingSession
    is_kill_zone: bool
    kill_zone_name: Optional[str]
    utc_hour: int

    def allows_entries(self) -> bool:
        return self.session != TradingSession.DEAD

    def preferred_strategies(self) -> list[str]:
        return SESSION_STRATEGY_PREFERENCE.get(self.session, [])


def get_session_context(now: Optional[datetime] = None) -> SessionContext:
    """Return the current trading session and kill zone membership.

    Args:
        now: UTC datetime to evaluate (defaults to current UTC time).
    """
    if now is None:
        now = datetime.now(timezone.utc)

    hour = now.hour

    session = TradingSession.DEAD
    for start, end, s in _SESSION_BOUNDS:
        if start <= hour < end:
            session = s
            break

    kill_zone_name: Optional[str] = None
    for name, (kz_start, kz_end) in _KILL_ZONES.items():
        if kz_start <= hour < kz_end:
            kill_zone_name = name
            break

    return SessionContext(
        session=session,
        is_kill_zone=kill_zone_name is not None,
        kill_zone_name=kill_zone_name,
        utc_hour=hour,
    )
