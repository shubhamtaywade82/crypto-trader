import os
import json
import asyncio
import logging
from datetime import datetime
from typing import Optional, List
from fastapi import FastAPI, Query, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from sse_starlette.sse import EventSourceResponse
import psycopg2
from psycopg2.extras import RealDictCursor

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("crypto_trader.api")

app = FastAPI(title="Crypto Trader v4 Dashboard API")

# Enable CORS for the SolidJS frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, specify the actual frontend URL
    allow_methods=["*"],
    allow_headers=["*"],
)

DATABASE_URL = os.environ.get("DATABASE_URL")
REDIS_URL = os.environ.get("REDIS_URL")

def get_db_conn():
    if not DATABASE_URL:
        raise HTTPException(status_code=500, detail="DATABASE_URL not set")
    try:
        return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    except Exception as e:
        logger.error(f"DB connection failed: {e}")
        raise HTTPException(status_code=500, detail="Database connection failed")

@app.get("/health")
def health():
    return {"status": "ok", "db": bool(DATABASE_URL), "redis": bool(REDIS_URL)}

@app.get("/watchlist")
def get_watchlist():
    from crypto_trader.multi_engine import load_watchlist
    try:
        return {"watchlist": load_watchlist()}
    except Exception as e:
        logger.error("Failed to load watchlist: %s", e)
        sym = os.environ.get("TRADE_SYMBOL", "SOLUSDT")
        return {"watchlist": [sym]}

@app.get("/positions")
def get_positions(mode: str = Query("paper")):
    if mode == "live":
        try:
            eng = _get_venue_engine()
            if eng is not None:
                venue_positions = eng.get_positions()
                out = []
                for p in venue_positions:
                    qty = float(p.get("quantity", 0) or 0)
                    if qty > 0:
                        out.append({
                            "symbol": p["symbol"],
                            "mode": "live",
                            "side": p["side"].upper(),
                            "qty": qty,
                            "avg_price": float(p.get("entry_price", 0) or p.get("avg_price", 0) or 0),
                            "status": "OPEN",
                            "last_event_ts": int(datetime.now().timestamp() * 1000)
                        })
                return out
        except Exception as e:
            logger.error("venue positions fetch failed in get_positions: %s", e)

    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM active_positions WHERE mode = %s", (mode,))
            return cur.fetchall()
    finally:
        conn.close()

@app.get("/orders")
def get_orders(mode: str = Query("paper"), limit: int = 50):
    if mode == "live":
        try:
            eng = _get_venue_engine()
            if eng is not None:
                open_orders = eng.get_open_orders()
                out = []
                for o in open_orders:
                    qty = float(o.get("quantity", 0) or 0)
                    filled = float(o.get("filled_quantity", 0) or 0)
                    out.append({
                        "exchange_order_id": o["exchange_order_id"],
                        "mode": "live",
                        "symbol": o["symbol"],
                        "side": o["side"].upper(),
                        "order_type": o.get("stage", "LIMIT").upper(),
                        "qty": qty,
                        "filled_qty": filled,
                        "avg_fill_price": 0.0,
                        "status": o["status"].upper(),
                        "created_at": int(datetime.now().timestamp() * 1000)
                    })
                return out
        except Exception as e:
            logger.error("venue orders fetch failed in get_orders: %s", e)

    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM orders WHERE mode = %s ORDER BY created_at DESC LIMIT %s",
                (mode, limit)
            )
            return cur.fetchall()
    finally:
        conn.close()

@app.get("/fills")
def get_fills(mode: str = Query("paper"), limit: int = 50):
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM fills WHERE mode = %s ORDER BY ts DESC LIMIT %s",
                (mode, limit)
            )
            return cur.fetchall()
    finally:
        conn.close()

@app.get("/pnl")
def get_pnl(mode: str = Query("paper")):
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT COUNT(*) AS fills, 
                          COALESCE(SUM(fee), 0) AS total_fees, 
                          COALESCE(SUM(qty), 0) AS total_qty 
                   FROM fills 
                   WHERE mode = %s""",
                (mode,)
            )
            fills_row = cur.fetchone()
            
            cur.execute(
                """SELECT COALESCE(SUM(
                    CASE 
                        WHEN type = 'POSITION_PARTIALLY_CLOSED' THEN COALESCE((payload->>'pnl')::numeric, (payload->>'remaining_pnl')::numeric, 0) - COALESCE((payload->>'fee')::numeric, 0)
                        WHEN type IN ('POSITION_CLOSED', 'LIQUIDATION') THEN COALESCE((payload->>'remaining_pnl')::numeric, 0) - COALESCE((payload->>'fee')::numeric, 0)
                        WHEN type = 'FUNDING_APPLIED' THEN COALESCE((payload->>'amount')::numeric, 0)
                        WHEN type = 'FEE_CHARGED' THEN -COALESCE((payload->>'amount')::numeric, 0)
                        WHEN type IN ('ORDER_FILLED', 'ORDER_PARTIALLY_FILLED') THEN -COALESCE((payload->>'fee')::numeric, 0)
                        ELSE 0
                    END
                ), 0) AS realized_pnl
                FROM events
                WHERE payload->>'mode' = %s""",
                (mode,)
            )
            pnl_row = cur.fetchone()
            
            return {
                mode: {
                    "mode": mode,
                    "fills": int(fills_row["fills"] or 0),
                    "total_fees": float(fills_row["total_fees"] or 0),
                    "total_qty": float(fills_row["total_qty"] or 0),
                    "realized_pnl": float(pnl_row["realized_pnl"] or 0)
                }
            }
    finally:
        conn.close()

BINANCE_FAPI = "https://fapi.binance.com"
INITIAL_BALANCE = float(os.environ.get("INITIAL_BALANCE", "1000") or 1000)


@app.get("/metrics")
def get_metrics(days: int = Query(30, ge=1, le=3650)):
    """Performance metrics from the closed-trade outcome substrate: win-rate,
    equity curve, per-regime PnL, MFE/MAE distribution, Sharpe, profit factor,
    expectancy, and LLM attribution. File-based (no DB dependency)."""
    import time as _time
    from crypto_trader.analytics import metrics as _metrics
    from crypto_trader.journal import TradeOutcomeJournal, TradeJournal

    cutoff_ms = int((_time.time() - days * 86400) * 1000)
    records = [
        r for r in TradeOutcomeJournal().load_all()
        if (r.closed_at or 0) >= cutoff_ms
    ]
    try:
        llm_attr = TradeJournal().analyze_llm_contribution(days=days)
    except Exception as e:
        logger.warning("llm attribution failed: %s", e)
        llm_attr = {}

    return _metrics.summary(
        records,
        llm_attribution=llm_attr,
        window_days=days,
        initial_balance=INITIAL_BALANCE,
    )


async def _fetch_ltp(symbols: List[str]) -> dict:
    """Mark/last price per symbol from Binance USDⓈ-M public API (no creds)."""
    import httpx
    out: dict = {}
    if not symbols:
        return out
    async with httpx.AsyncClient(timeout=5.0) as client:
        async def one(sym: str):
            try:
                r = await client.get(f"{BINANCE_FAPI}/fapi/v1/ticker/price", params={"symbol": sym})
                r.raise_for_status()
                out[sym] = float(r.json()["price"])
            except Exception as e:
                logger.warning("ltp fetch failed for %s: %s", sym, e)
                out[sym] = None
        await asyncio.gather(*(one(s) for s in symbols))
    return out


def _realized_pnl(cur, mode: str) -> float:
    cur.execute(
        """SELECT COALESCE(SUM(
            CASE
                WHEN type = 'POSITION_PARTIALLY_CLOSED' THEN COALESCE((payload->>'pnl')::numeric, (payload->>'remaining_pnl')::numeric, 0) - COALESCE((payload->>'fee')::numeric, 0)
                WHEN type IN ('POSITION_CLOSED', 'LIQUIDATION') THEN COALESCE((payload->>'remaining_pnl')::numeric, 0) - COALESCE((payload->>'fee')::numeric, 0)
                WHEN type = 'FUNDING_APPLIED' THEN COALESCE((payload->>'amount')::numeric, 0)
                WHEN type = 'FEE_CHARGED' THEN -COALESCE((payload->>'amount')::numeric, 0)
                WHEN type IN ('ORDER_FILLED', 'ORDER_PARTIALLY_FILLED') THEN -COALESCE((payload->>'fee')::numeric, 0)
                ELSE 0
            END), 0) AS realized
           FROM events WHERE payload->>'mode' = %s""",
        (mode,),
    )
    return float(cur.fetchone()["realized"] or 0)


@app.get("/ltp")
async def get_ltp(symbols: str = Query("", description="comma-separated symbols")):
    syms = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    return await _fetch_ltp(syms)


@app.get("/account")
async def get_account(mode: str = Query("paper")):
    """Balance, realized + unrealized PnL, equity. Balance is derived from the
    event journal (initial + realized) so it matches the projection."""
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            realized = _realized_pnl(cur, mode)
            positions = []
            if mode == "live":
                try:
                    eng = _get_venue_engine()
                    if eng is not None:
                        venue_positions = eng.get_positions()
                        for p in venue_positions:
                            qty = float(p.get("quantity", 0) or 0)
                            if qty > 0:
                                positions.append({
                                    "symbol": p["symbol"],
                                    "side": p["side"].upper(),
                                    "qty": qty,
                                    "avg_price": float(p.get("entry_price", 0) or p.get("avg_price", 0) or 0)
                                })
                    else:
                        cur.execute(
                            "SELECT symbol, side, qty, avg_price FROM active_positions WHERE mode=%s AND COALESCE(qty,0) > 0",
                            (mode,),
                        )
                        positions = cur.fetchall()
                except Exception as e:
                    logger.error("Failed to fetch venue positions in get_account: %s", e)
                    cur.execute(
                        "SELECT symbol, side, qty, avg_price FROM active_positions WHERE mode=%s AND COALESCE(qty,0) > 0",
                        (mode,),
                    )
                    positions = cur.fetchall()
            else:
                cur.execute(
                    "SELECT symbol, side, qty, avg_price FROM active_positions WHERE mode=%s AND COALESCE(qty,0) > 0",
                    (mode,),
                )
                positions = cur.fetchall()
    finally:
        conn.close()

    ltp = await _fetch_ltp([p["symbol"] for p in positions])
    unrealized = 0.0
    enriched = []
    for p in positions:
        mark = ltp.get(p["symbol"])
        avg = float(p["avg_price"] or 0)
        qty = float(p["qty"] or 0)
        upnl = None
        if mark is not None and avg > 0:
            sign = 1.0 if str(p["side"]).upper() in ("LONG", "BUY") else -1.0
            upnl = (mark - avg) * qty * sign
            unrealized += upnl
        enriched.append({**p, "mark_price": mark, "unrealized_pnl": upnl})

    if mode == "live":
        try:
            eng = _get_venue_engine()
            if eng is not None:
                avail = float(eng.sync_balance())
                conv = float(eng.get_usdt_conversion()) or 1.0
                margin_currency = os.environ.get("COINDCX_MARGIN_CURRENCY", "USDT").upper()
                balance = avail / conv if margin_currency != "USDT" else avail
            else:
                balance = INITIAL_BALANCE + realized
        except Exception as e:
            logger.error("Failed to sync live balance in get_account: %s", e)
            balance = INITIAL_BALANCE + realized
    else:
        balance = INITIAL_BALANCE + realized

    return {
        "mode": mode,
        "initial_balance": INITIAL_BALANCE,
        "realized_pnl": realized,
        "unrealized_pnl": unrealized,
        "balance": balance,
        "equity": balance + unrealized,
        "open_positions": enriched,
    }


@app.get("/risk")
def get_risk():
    """RiskManager persisted state (kill switch, daily count, drawdown)."""
    import json as _json
    from pathlib import Path
    f = Path.home() / ".crypto_trader" / "risk_state.json"
    if not f.exists():
        return {"available": False}
    try:
        return {"available": True, **_json.loads(f.read_text())}
    except Exception as e:
        return {"available": False, "error": str(e)}


@app.get("/gate")
def get_gate():
    """Live-trading gate status (safe_mode): is real-money placement enabled?"""
    from pathlib import Path
    halt = (Path.home() / ".crypto_trader" / "HALT").exists()
    enabled = os.environ.get("LIVE_TRADING_ENABLED", "").lower() in ("true", "1", "yes")
    ack_ok = os.environ.get("LIVE_TRADING_ACK", "") == "I_UNDERSTAND_REAL_MONEY_WILL_BE_LOST"
    po_raw = os.environ.get("PLACE_ORDER", "").strip().lower()
    place_order = True if po_raw == "" else po_raw in ("true", "1", "yes", "on")
    return {
        "mode": os.environ.get("MODE", "paper"),
        "live_enabled": enabled,
        "ack_ok": ack_ok,
        "halt_file": halt,
        "place_order": place_order,
        "read_only": not place_order,
        "live_orders_allowed": enabled and ack_ok and not halt and place_order,
    }


_venue_engine = None
_venue_err = None


def _get_venue_engine():
    """Lazily build a READ-ONLY CoinDCX engine (i_understand_real_money=False).
    Reads (balances/positions/orders) don't pass the live gate, so this never
    places orders. Returns None if credentials are missing/invalid."""
    global _venue_engine, _venue_err
    if _venue_engine is not None or _venue_err is not None:
        return _venue_engine
    try:
        from crypto_trader.config import load_config
        from crypto_trader.exchanges.coindcx_execution import CoinDCXExecutionEngine
        cfg = load_config()
        if not (cfg.coindcx_api_key and cfg.coindcx_api_secret):
            _venue_err = "no CoinDCX credentials"
            return None
        _venue_engine = CoinDCXExecutionEngine(
            api_key=cfg.coindcx_api_key, api_secret=cfg.coindcx_api_secret,
            i_understand_real_money=False,
        )
    except Exception as e:
        _venue_err = str(e)
        logger.error("venue engine init failed: %s", e)
    return _venue_engine


@app.get("/venue/health")
def venue_health():
    """Authoritative CoinDCX account health (cross_margin_details).
    Includes margin ratio, maintenance margin, and total account PnL."""
    eng = _get_venue_engine()
    if eng is None:
        return {"ok": False, "error": _venue_err or "venue unavailable"}
    try:
        details = eng.get_cross_margin_details()
        if details:
            return {"ok": True, "details": details}
        return {"ok": False, "error": "failed to fetch cross margin details"}
    except Exception as e:
        logger.error("venue health fetch failed: %s", e)
        return {"ok": False, "error": str(e)}

@app.get("/venue/account")
def venue_account(symbol: Optional[str] = Query(None)):
    """Live CoinDCX account snapshot (real balances/positions/open orders).
    This is the authoritative venue state — independent of the bot's projection."""
    eng = _get_venue_engine()
    if eng is None:
        return {"ok": False, "error": _venue_err or "venue unavailable"}
    try:
        from crypto_trader.execution.account_sync import AccountSync
        snap = AccountSync(eng).snapshot(symbol)
        return {
            "ok": bool(getattr(snap, "ok", False)),
            "error": getattr(snap, "error", None),
            "balances": getattr(snap, "balances", None),
            "positions": getattr(snap, "positions", None),
            "open_orders": getattr(snap, "open_orders", None),
        }
    except Exception as e:
        logger.error("venue snapshot failed: %s", e)
        return {"ok": False, "error": str(e)}


@app.get("/positions/detail")
def positions_detail(mode: str = Query("paper")):
    """Active positions joined with SL/TP from the latest POSITION_OPENED event."""
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM active_positions WHERE mode=%s", (mode,))
            rows = cur.fetchall()
            for r in rows:
                cur.execute(
                    """SELECT payload->>'sl_price' AS sl, payload->'tp_levels' AS tp
                       FROM events
                       WHERE type='POSITION_OPENED' AND payload->>'symbol'=%s AND payload->>'mode'=%s
                       ORDER BY id DESC LIMIT 1""",
                    (r["symbol"], mode),
                )
                d = cur.fetchone()
                r["sl_price"] = float(d["sl"]) if d and d["sl"] else None
                r["tp_levels"] = d["tp"] if d else None
            return rows
    finally:
        conn.close()


@app.get("/events/stream")
async def event_stream(request: Request):
    """Bridge Redis events to Server-Sent Events (SSE)."""
    if not REDIS_URL:
        return {"error": "REDIS_URL not set, streaming unavailable"}

    import redis.asyncio as redis
    r = redis.from_url(REDIS_URL, decode_responses=True)
    
    async def event_generator():
        # Stream from 'events:broadcast'
        # Replay the last 10 events on connect so the UI isn't empty
        last_id = "0" # Start from beginning of stream (or use XRANGE for limited replay)
        
        # To avoid replaying EVERYTHING since forever, we can use $ initially
        # but let's try replaying the last few messages.
        try:
            # Get last 20 messages for initial burst
            past_events = await r.xrevrange("events:broadcast", max="+", min="-", count=20)
            for msg_id, data in reversed(past_events):
                payload = data.get("payload", "{}")
                yield {
                    "id": msg_id,
                    "event": "message",
                    "data": payload
                }
            # Now switch to tailing
            last_info = await r.xinfo_stream("events:broadcast")
            last_id = last_info["last-generated-id"]
        except Exception as e:
            logger.warning(f"Initial stream replay failed (maybe stream is empty): {e}")
            last_id = "$"
        while True:
            if await request.is_disconnected():
                break
            
            try:
                # XREAD block=5000
                events = await r.xread({"events:broadcast": last_id}, count=10, block=5000)
                for stream, msgs in events:
                    for msg_id, data in msgs:
                        last_id = msg_id
                        # The engine will pack the event as a JSON string in 'payload'
                        payload = data.get("payload", "{}")
                        yield {
                            "id": msg_id,
                            "event": "message",
                            "data": payload
                        }
            except Exception as e:
                logger.error(f"SSE stream error: {e}")
                await asyncio.sleep(1)

    return EventSourceResponse(event_generator())

if __name__ == "__main__":
    import uvicorn
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port)
