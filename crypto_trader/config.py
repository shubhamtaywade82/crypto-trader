"""
crypto_trader.config — Typed runtime configuration & validation
=================================================================
Single source of truth for trading mode, exchange credentials, leverage caps,
the active symbol, data-source selection, and persistence backend.

Loaded from environment variables (the package's ``__init__`` already populates
``os.environ`` from a ``.env`` file). Nothing here ever logs secrets.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class TradingMode(str, Enum):
    PAPER = "paper"
    LIVE = "live"


class DataSource(str, Enum):
    BINANCE = "binance"
    COINDCX = "coindcx"
    AUTO = "auto"            # Binance primary, CoinDCX fallback


class TradingProfile(str, Enum):
    CONSERVATIVE = "conservative"
    MODERATE = "moderate"
    AGGRESSIVE = "aggressive"


@dataclass
class _ProfileDefaults:
    final_score_threshold: float
    adaptive_min_threshold: float
    adaptive_target_trades_per_day: float   # decay starts after (24/target) hours of no trades
    adaptive_decay_per_hour: float          # how fast threshold drops per idle hour
    max_daily_trades: int
    max_consecutive_losses: int
    max_drawdown_pct: float
    max_leverage: int
    risk_per_trade_pct: float
    funding_extreme_threshold: float
    require_kill_zone: bool
    allowed_regimes: list  # ExtendedRegime value strings that allow entries


_TRADING_PROFILES: dict = {
    "conservative": _ProfileDefaults(
        final_score_threshold=0.82,
        adaptive_min_threshold=0.75,
        adaptive_target_trades_per_day=1.0,   # decay starts at 24h idle
        adaptive_decay_per_hour=0.005,
        max_daily_trades=2,
        max_consecutive_losses=1,
        max_drawdown_pct=0.08,
        max_leverage=2,
        risk_per_trade_pct=0.01,
        funding_extreme_threshold=0.0003,
        require_kill_zone=True,
        allowed_regimes=["TREND_EXPANSION", "BREAKOUT_ENVIRONMENT"],
    ),
    "moderate": _ProfileDefaults(
        final_score_threshold=0.72,
        adaptive_min_threshold=0.60,
        adaptive_target_trades_per_day=2.0,   # decay starts at 12h idle
        adaptive_decay_per_hour=0.01,
        max_daily_trades=5,
        max_consecutive_losses=2,
        max_drawdown_pct=0.15,
        max_leverage=3,
        risk_per_trade_pct=0.02,
        funding_extreme_threshold=0.0005,
        require_kill_zone=False,
        allowed_regimes=["TREND_EXPANSION", "BREAKOUT_ENVIRONMENT", "ACCUMULATION", "MEAN_REVERSION"],
    ),
    "aggressive": _ProfileDefaults(
        final_score_threshold=0.62,
        adaptive_min_threshold=0.45,          # floor lower so decay goes further
        adaptive_target_trades_per_day=3.0,   # decay starts at 8h idle
        adaptive_decay_per_hour=0.02,         # drops 0.02/h → after 9h: 0.60 → scores pass
        max_daily_trades=10,
        max_consecutive_losses=3,
        max_drawdown_pct=0.25,
        max_leverage=5,
        risk_per_trade_pct=0.04,
        funding_extreme_threshold=0.001,
        require_kill_zone=False,
        allowed_regimes=["TREND_EXPANSION", "BREAKOUT_ENVIRONMENT", "ACCUMULATION", "MEAN_REVERSION", "LOW_VOL_CHOP"],
    ),
}


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _get_bool(name: str, default: bool = False) -> bool:
    return _get(name, str(default)).lower() in ("1", "true", "yes", "on")


def _get_float(name: str, default: float) -> float:
    try:
        return float(_get(name, str(default)))
    except ValueError:
        return default


def _get_int(name: str, default: int) -> int:
    try:
        return int(float(_get(name, str(default))))
    except ValueError:
        return default


@dataclass
class TradingConfig:
    mode: TradingMode = TradingMode.PAPER
    symbol: str = "SOLUSDT"
    data_source: DataSource = DataSource.AUTO

    # Risk / sizing guardrails at launch
    max_leverage: int = 2
    initial_balance: float = 1000.0

    # CoinDCX credentials (never logged / committed)
    coindcx_api_key: str = ""
    coindcx_api_secret: str = ""
    coindcx_base_url: str = "https://api.coindcx.com"
    # Futures margin currency: "USDT" or "INR" (CoinDCX supports both; the
    # account's funded futures wallet determines which one to use).
    coindcx_margin_currency: str = "USDT"

    # Persistence
    database_url: str = ""          # postgresql://...; empty => SQLite dev fallback

    # Feed freshness budget (ms) before a source is considered stale
    feed_stale_ms: int = 15_000

    # ── Venue-resident protective stops (F1) ──
    # Place a resting STOP_MARKET (and optionally TAKE_PROFIT) on CoinDCX after an
    # entry fills, so a process/WS crash cannot leave a position unprotected.
    venue_sl_enabled: bool = True
    venue_tp_enabled: bool = False          # software TP/scale-outs already exist
    require_venue_sl: bool = False          # if True, SL placement failure trips HALT
    software_sl_backup_bps: int = 15        # software exit only fires this far past SL

    # ── TDS accounting (F2) ──
    # India deducts 1% TDS on the sell leg of a crypto trade. Modelled in PnL.
    tds_rate: float = 0.01
    tds_enabled: bool = False               # defaults on when margin currency is INR

    # ── Cross-venue basis guard (F3) ──
    # Signals fire on Binance price but fills happen on CoinDCX; guard the gap.
    basis_guard_enabled: bool = True
    max_cross_venue_basis: float = 0.005    # reject entry when |basis| exceeds this
    entry_basis_buffer: float = 0.0005      # nudge entry trigger toward the fill venue

    # ── Paper execution-degradation model (F4) ──
    # Make paper fills realistic vs thin CoinDCX liquidity.
    paper_cdcx_spread_coeff: float = 0.0015  # per-side fill penalty in paper mode
    paper_collar_pct: float = 0.10           # reject paper market fills beyond this deviation

    # ── Entry order style (maker-limit with market fallback) ──
    # Global opt-in (default "market" = unchanged behaviour). When set to
    # "maker_limit", entries from playbooks that request it (volx/sweep) post a
    # passive LIMIT and fall back per maker_limit_fallback if unfilled.
    entry_order_style: str = "market"        # "market" | "maker_limit"
    maker_limit_timeout_s: float = 8.0       # how long to wait for a passive fill
    maker_limit_offset_bps: float = 1.0      # post this far inside the spread (bps)
    maker_limit_fallback: str = "market"     # "market" | "skip" when unfilled

    # ── Delta-neutral funding-rate arbitrage (Strategy C, default-off) ──
    # Non-directional spot+perp carry. Phased rollout: paper -> live observe-only
    # -> tiny live. Requires the spot execution engine to be wired.
    funding_arb_enabled: bool = False
    funding_arb_notional_usdt: float = 0.0   # per-arb notional (0 = no entries)
    funding_arb_qty_step: float = 0.0001     # common qty step across spot/perp legs
    min_funding_to_enter: float = 0.0001     # enter when funding rate >= this (per interval)
    funding_exit_floor: float = 0.0          # unwind when funding <= this (flips against carry)
    min_funding_to_hold: float = 0.00003     # unwind when carry no longer beats round-trip cost
    max_entry_basis: float = 0.005           # skip entry if |perp-spot|/spot exceeds this
    max_basis_drift: float = 0.006           # unwind if basis drifts this far from entry
    funding_interval_ms: int = 28_800_000    # CoinDCX funding settles every 8h

    # ── CoinDCX private/user stream (F5, optional, default-off) ──
    coindcx_user_stream_enabled: bool = False
    coindcx_stream_url: str = "wss://stream.coindcx.com"
    coindcx_stream_channel: str = "coindcx"

    # ── Redis Streams pipeline + Postgres projection (data modeling) ──
    # Redis decouples strategy (producer) from execution (consumer group) and
    # gives at-least-once delivery + crash recovery via the Pending Entries List.
    redis_url: str = ""                       # redis://host:6379/0; empty => disabled
    execution_bus: str = "inproc"             # "inproc" (default) | "redis"
    signal_stream: str = "execution:signals"
    consumer_group: str = "execution_engine"
    signal_max_deliveries: int = 3            # poison-pill -> DLQ after this many tries
    idempotency_ttl_seconds: int = 300        # duplicate-suppression window
    # Postgres relational read-model derived from the JSONB event stream.
    projection_enabled: bool = False

    # ── Dashboard API + realtime UI bridge ──
    # The bot publishes each domain event to a Redis pub/sub channel; the FastAPI
    # service relays it over SSE for true realtime (no polling).
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    ui_events_enabled: bool = True          # bot -> Redis pub/sub for the UI
    ui_events_channel: str = "events:ui"

    # ── Production-readiness guards (G1–G5) ──
    # G1: max tolerated local↔venue clock drift (ms) before warning/halting.
    clock_skew_max_ms: int = 2000
    # G2: per-minute order velocity circuit breaker (on top of the daily cap).
    max_orders_per_minute: int = 6
    # G3: supervise WS feed / position-manager threads; HALT if one dies silently.
    thread_supervisor_enabled: bool = True
    # G4: max exchange margin ratio (0.80 = 80%) before halting to avoid liquidation.
    max_margin_ratio: float = 0.80
    # G2: daily trade limit
    max_daily_trades: int = 2
    # G5: on an unresolved venue desync, cancel ALL venue orders to protect capital.
    reconcile_strict_cancel: bool = False

    # ── Market Regime / Session Engine ──
    # Restrict new entries to institutional kill zones only (London Open, NY Open, etc.).
    # Default False — keeps the bot trading while you validate regime labels in paper mode.
    require_kill_zone: bool = False
    # RVOL thresholds for ExtendedRegime classification.
    rvol_dead_threshold: float = 0.6    # below this → DEAD_MARKET, no entries
    rvol_weak_threshold: float = 0.9    # below this → LOW_VOL_CHOP, half size
    rvol_strong_threshold: float = 1.5  # above this → full/boosted size
    # Position-size multipliers applied by RegimeClassifier output.
    regime_size_multiplier_high: float = 1.25  # TREND_EXPANSION
    regime_size_multiplier_low: float = 0.5    # LOW_VOL_CHOP / MEAN_REVERSION

    # ── Trading Profile ──
    # Controls entry quality bar, risk sizing, regime filtering, and daily limits.
    trading_profile: str = "moderate"
    final_score_threshold: float = 0.72
    adaptive_min_threshold: float = 0.60
    adaptive_target_trades_per_day: float = 2.0   # decay starts after 24/target idle hours
    adaptive_decay_per_hour: float = 0.01         # threshold drop rate per idle hour
    risk_per_trade_pct: float = 0.02
    funding_extreme_threshold: float = 0.0005
    max_consecutive_losses: int = 2
    max_drawdown_pct: float = 0.20
    allowed_regimes: list = field(default_factory=list)  # empty = all non-blocked regimes

    @classmethod
    def from_env(cls) -> "TradingConfig":
        mode = _get("MODE", "paper").lower()
        try:
            mode_enum = TradingMode(mode)
        except ValueError:
            mode_enum = TradingMode.PAPER

        ds = _get("DATA_SOURCE", "auto").lower()
        try:
            ds_enum = DataSource(ds)
        except ValueError:
            ds_enum = DataSource.AUTO

        margin_currency = _get("COINDCX_MARGIN_CURRENCY", "USDT").upper()

        # Resolve trading profile and apply its defaults (env vars can still override)
        profile_name = _get("TRADING_PROFILE", "moderate").lower()
        profile = _TRADING_PROFILES.get(profile_name, _TRADING_PROFILES["moderate"])

        return cls(
            mode=mode_enum,
            symbol=_get("TRADE_SYMBOL", "SOLUSDT").upper(),
            data_source=ds_enum,
            max_leverage=_get_int("MAX_LEVERAGE", profile.max_leverage),
            initial_balance=_get_float("INITIAL_BALANCE", 1000.0),
            coindcx_api_key=_get("COINDCX_API_KEY"),
            coindcx_api_secret=_get("COINDCX_API_SECRET"),
            coindcx_base_url=_get("COINDCX_BASE_URL", "https://api.coindcx.com"),
            coindcx_margin_currency=margin_currency,
            database_url=_get("DATABASE_URL"),
            feed_stale_ms=_get_int("FEED_STALE_MS", 15_000),
            venue_sl_enabled=_get_bool("COINDCX_VENUE_SL_ENABLED", True),
            venue_tp_enabled=_get_bool("COINDCX_VENUE_TP_ENABLED", False),
            require_venue_sl=_get_bool("COINDCX_REQUIRE_VENUE_SL", False),
            software_sl_backup_bps=_get_int("SOFTWARE_SL_BACKUP_BPS", 15),
            tds_rate=_get_float("COINDCX_TDS_RATE", 0.01),
            tds_enabled=_get_bool("COINDCX_TDS_ENABLED", margin_currency == "INR"),
            basis_guard_enabled=_get_bool("BASIS_GUARD_ENABLED", True),
            max_cross_venue_basis=_get_float("MAX_CROSS_VENUE_BASIS", 0.005),
            entry_basis_buffer=_get_float("ENTRY_BASIS_BUFFER", 0.0005),
            paper_cdcx_spread_coeff=_get_float("PAPER_CDCX_SPREAD_COEFF", 0.0015),
            paper_collar_pct=_get_float("PAPER_COLLAR_PCT", 0.10),
            entry_order_style=os.getenv("ENTRY_ORDER_STYLE", "market").lower(),
            maker_limit_timeout_s=_get_float("MAKER_LIMIT_TIMEOUT_S", 8.0),
            maker_limit_offset_bps=_get_float("MAKER_LIMIT_OFFSET_BPS", 1.0),
            maker_limit_fallback=os.getenv("MAKER_LIMIT_FALLBACK", "market").lower(),
            funding_arb_enabled=_get_bool("FUNDING_ARB_ENABLED", False),
            funding_arb_notional_usdt=_get_float("FUNDING_ARB_NOTIONAL_USDT", 0.0),
            funding_arb_qty_step=_get_float("FUNDING_ARB_QTY_STEP", 0.0001),
            min_funding_to_enter=_get_float("MIN_FUNDING_TO_ENTER", 0.0001),
            funding_exit_floor=_get_float("FUNDING_EXIT_FLOOR", 0.0),
            min_funding_to_hold=_get_float("MIN_FUNDING_TO_HOLD", 0.00003),
            max_entry_basis=_get_float("MAX_ENTRY_BASIS", 0.005),
            max_basis_drift=_get_float("MAX_BASIS_DRIFT", 0.006),
            funding_interval_ms=_get_int("FUNDING_INTERVAL_MS", 28_800_000),
            coindcx_user_stream_enabled=_get_bool("COINDCX_USER_STREAM_ENABLED", False),
            coindcx_stream_url=_get("COINDCX_STREAM_URL", "wss://stream.coindcx.com"),
            coindcx_stream_channel=_get("COINDCX_STREAM_CHANNEL", "coindcx"),
            redis_url=_get("REDIS_URL"),
            execution_bus=_get("EXECUTION_BUS", "inproc").lower(),
            signal_stream=_get("SIGNAL_STREAM", "execution:signals"),
            consumer_group=_get("CONSUMER_GROUP", "execution_engine"),
            signal_max_deliveries=_get_int("SIGNAL_MAX_DELIVERIES", 3),
            idempotency_ttl_seconds=_get_int("IDEMPOTENCY_TTL_SECONDS", 300),
            projection_enabled=_get_bool("PROJECTION_ENABLED", False),
            api_host=_get("API_HOST", "0.0.0.0"),
            api_port=_get_int("API_PORT", 8000),
            ui_events_enabled=_get_bool("UI_EVENTS_ENABLED", True),
            ui_events_channel=_get("UI_EVENTS_CHANNEL", "events:ui"),
            clock_skew_max_ms=_get_int("CLOCK_SKEW_MAX_MS", 2000),
            max_orders_per_minute=_get_int("MAX_ORDERS_PER_MINUTE", 6),
            max_daily_trades=_get_int("MAX_DAILY_TRADES", profile.max_daily_trades),
            max_margin_ratio=_get_float("MAX_MARGIN_RATIO", 0.80),
            thread_supervisor_enabled=_get_bool("THREAD_SUPERVISOR_ENABLED", True),
            reconcile_strict_cancel=_get_bool("RECONCILE_STRICT_CANCEL", False),
            require_kill_zone=_get_bool("REQUIRE_KILL_ZONE", profile.require_kill_zone),
            rvol_dead_threshold=_get_float("RVOL_DEAD_THRESHOLD", 0.6),
            rvol_weak_threshold=_get_float("RVOL_WEAK_THRESHOLD", 0.9),
            rvol_strong_threshold=_get_float("RVOL_STRONG_THRESHOLD", 1.5),
            regime_size_multiplier_high=_get_float("REGIME_SIZE_MULTIPLIER_HIGH", 1.25),
            regime_size_multiplier_low=_get_float("REGIME_SIZE_MULTIPLIER_LOW", 0.5),
            trading_profile=profile_name,
            final_score_threshold=_get_float("FINAL_SCORE_THRESHOLD", profile.final_score_threshold),
            adaptive_min_threshold=_get_float("ADAPTIVE_MIN_THRESHOLD", profile.adaptive_min_threshold),
            adaptive_target_trades_per_day=_get_float("ADAPTIVE_TARGET_TRADES_PER_DAY", profile.adaptive_target_trades_per_day),
            adaptive_decay_per_hour=_get_float("ADAPTIVE_DECAY_PER_HOUR", profile.adaptive_decay_per_hour),
            risk_per_trade_pct=_get_float("RISK_PER_TRADE_PCT", profile.risk_per_trade_pct),
            funding_extreme_threshold=_get_float("FUNDING_EXTREME_THRESHOLD", profile.funding_extreme_threshold),
            max_consecutive_losses=_get_int("MAX_CONSECUTIVE_LOSSES", profile.max_consecutive_losses),
            max_drawdown_pct=_get_float("MAX_DRAWDOWN_PCT", profile.max_drawdown_pct),
            allowed_regimes=profile.allowed_regimes,
        )

    @property
    def is_live(self) -> bool:
        return self.mode == TradingMode.LIVE

    @property
    def has_coindcx_credentials(self) -> bool:
        return bool(self.coindcx_api_key and self.coindcx_api_secret)

    @property
    def redis_enabled(self) -> bool:
        return self.execution_bus == "redis" and bool(self.redis_url)

    def validate(self) -> List[str]:
        """Return a list of human-readable problems. Empty list == OK."""
        errs: List[str] = []
        if self.max_leverage < 1:
            errs.append("MAX_LEVERAGE must be >= 1")
        profile = _TRADING_PROFILES.get(self.trading_profile, _TRADING_PROFILES["moderate"])
        if self.max_leverage > profile.max_leverage:
            errs.append(
                f"MAX_LEVERAGE={self.max_leverage} exceeds {self.trading_profile} profile cap of {profile.max_leverage}x"
            )
        if not self.symbol:
            errs.append("TRADE_SYMBOL is required")
        if self.is_live and not self.has_coindcx_credentials:
            errs.append(
                "Live mode requires COINDCX_API_KEY and COINDCX_API_SECRET"
            )
        return errs

    def redacted(self) -> dict:
        """Safe-to-log view of the config (no secrets)."""
        def mask(v: str) -> str:
            return (v[:4] + "****") if v else "(unset)"

        return {
            "mode": self.mode.value,
            "symbol": self.symbol,
            "data_source": self.data_source.value,
            "margin_currency": self.coindcx_margin_currency,
            "max_leverage": self.max_leverage,
            "initial_balance": self.initial_balance,
            "coindcx_api_key": mask(self.coindcx_api_key),
            "coindcx_api_secret": mask(self.coindcx_api_secret),
            "database_url": "postgres" if self.database_url else "sqlite (dev)",
            "feed_stale_ms": self.feed_stale_ms,
            "venue_sl_enabled": self.venue_sl_enabled,
            "tds_enabled": self.tds_enabled,
            "basis_guard_enabled": self.basis_guard_enabled,
            "user_stream_enabled": self.coindcx_user_stream_enabled,
        }


def load_config() -> TradingConfig:
    return TradingConfig.from_env()
