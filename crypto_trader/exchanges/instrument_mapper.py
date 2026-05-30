"""
crypto_trader.exchanges.instrument_mapper — Symbol & precision mapping
=======================================================================
Bridges the bot's internal Binance-style symbols (e.g. ``SOLUSDT``) to CoinDCX
futures pairs (e.g. ``B-SOL_USDT``) and exposes per-instrument trading
constraints (tick size, step size, min notional, leverage caps, fees) sourced
live from CoinDCX's instrument endpoint.

Endpoint shape (verified against the live API)::

    GET /exchange/v1/derivatives/futures/data/instrument?pair=B-SOL_USDT
    -> {"instrument": {
          "price_increment": 0.01, "quantity_increment": 0.01,
          "min_quantity": 0.01, "min_notional": 6.0,
          "max_leverage_long": 5.0, "max_leverage_short": 5.0,
          "maker_fee": 0.0236, "taker_fee": 0.059, ...   # fees are PERCENT
       }}
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Dict, Optional, Tuple

from .coindcx_client import CoinDCXClient

logger = logging.getLogger("crypto_trader.exchanges.instrument_mapper")

_INSTRUMENT_ENDPOINT = "exchange/v1/derivatives/futures/data/instrument"

# System-wide sane leverage ceiling. The venue's dynamic tier table can list up
# to 100x for tiny positions, but we never operate above this — keep in sync with
# risk.LeverageEngine.hard_max_leverage. Per-symbol specs are clamped to it so a
# venue's top tier never becomes the bot's default/operating leverage.
_MAX_USABLE_LEVERAGE = 20


def internal_to_coindcx(symbol: str) -> str:
    """``SOLUSDT`` -> ``B-SOL_USDT``. Idempotent for already-CoinDCX symbols."""
    s = symbol.upper()
    if s.startswith("B-") and "_" in s:
        return s
    if s.endswith("USDT"):
        base = s[:-4]
        return f"B-{base}_USDT"
    raise ValueError(f"Cannot map symbol to CoinDCX pair: {symbol!r}")


def coindcx_to_internal(pair: str) -> str:
    """``B-SOL_USDT`` -> ``SOLUSDT``."""
    p = pair.upper()
    if p.startswith("B-"):
        p = p[2:]
    base, _, quote = p.partition("_")
    return f"{base}{quote}"


@dataclass
class InstrumentSpec:
    internal_symbol: str
    pair: str
    price_increment: Decimal
    quantity_increment: Decimal
    min_quantity: Decimal
    min_notional: Decimal
    max_leverage: int
    maker_fee_rate: float          # fraction, e.g. 0.000236
    taker_fee_rate: float          # fraction, e.g. 0.00059
    status: str = "active"

    def round_price(self, price: Decimal) -> Decimal:
        return _round_to_increment(Decimal(str(price)), self.price_increment)

    def round_qty(self, qty: Decimal) -> Decimal:
        return _round_to_increment(Decimal(str(qty)), self.quantity_increment)

    def validate_order(self, qty: Decimal, price: Decimal) -> Optional[str]:
        """Return an error string if the order violates exchange constraints."""
        qty = Decimal(str(qty))
        price = Decimal(str(price))
        if qty < self.min_quantity:
            return f"quantity {qty} below min_quantity {self.min_quantity}"
        notional = qty * price
        if notional < self.min_notional:
            return f"notional {notional} below min_notional {self.min_notional}"
        return None


_SPEC_CACHE_TTL_S = 3600  # refresh venue instrument specs at most once per hour


class InstrumentMapper:
    def __init__(self, client: Optional[CoinDCXClient] = None, margin_currency: str = "USDT"):
        self.client = client or CoinDCXClient()
        self.margin_currency = margin_currency.upper()
        # (spec, fetched_at) tuples keyed by internal symbol
        self._cache: Dict[str, Tuple[InstrumentSpec, float]] = {}

    def get_spec(self, symbol: str, *, refresh: bool = False) -> InstrumentSpec:
        internal = coindcx_to_internal(symbol) if symbol.upper().startswith("B-") else symbol.upper()
        now = time.monotonic()
        if not refresh and internal in self._cache:
            spec, fetched_at = self._cache[internal]
            if now - fetched_at < _SPEC_CACHE_TTL_S:
                return spec
        pair = internal_to_coindcx(internal)
        raw = self.client.get_public(
            _INSTRUMENT_ENDPOINT,
            params={"pair": pair, "margin_currency_short_name": self.margin_currency},
        )
        inst = raw.get("instrument") if isinstance(raw, dict) else None
        if not inst:
            raise ValueError(f"No instrument data for {pair}: {raw}")
        # CoinDCX docs: max_leverage_long/short are "Ignore this". The real
        # leverage cap is dynamic_position_leverage_details — a {leverage: max
        # notional} tier table. The venue's TOP tier (e.g. 100x) only applies to
        # microscopic positions and is NOT a sane operating cap, so we clamp the
        # derived ceiling to the bot's system hard cap (matches
        # LeverageEngine.hard_max_leverage). Fall back to max_leverage_long when
        # the tier table is absent. The venue still enforces per-position-size
        # gating at order time.
        dyn = inst.get("dynamic_position_leverage_details") or {}
        tier_max = 0
        try:
            tier_max = max((int(k) for k in dyn.keys()), default=0)
        except (TypeError, ValueError):
            tier_max = 0
        legacy_max = int(float(inst.get("max_leverage_long", 1) or 1))
        max_leverage = min(tier_max or legacy_max or 1, _MAX_USABLE_LEVERAGE)
        spec = InstrumentSpec(
            internal_symbol=internal,
            pair=pair,
            price_increment=Decimal(str(inst["price_increment"])),
            quantity_increment=Decimal(str(inst["quantity_increment"])),
            min_quantity=Decimal(str(inst.get("min_quantity", inst.get("min_trade_size", "0")))),
            min_notional=Decimal(str(inst.get("min_notional", "0"))),
            max_leverage=max_leverage,
            maker_fee_rate=float(inst.get("maker_fee", 0.0)) / 100.0,
            taker_fee_rate=float(inst.get("taker_fee", 0.0)) / 100.0,
            status=inst.get("status", "active"),
        )
        self._cache[internal] = (spec, now)
        logger.info(
            "Loaded instrument %s: tick=%s step=%s minNotional=%s maxLev=%s",
            pair, spec.price_increment, spec.quantity_increment, spec.min_notional, spec.max_leverage,
        )
        return spec


def _round_to_increment(value: Decimal, increment: Decimal) -> Decimal:
    if increment <= 0:
        return value
    return (value / increment).to_integral_value(rounding=ROUND_DOWN) * increment
