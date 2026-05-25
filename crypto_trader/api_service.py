import os
import json
import asyncio
import logging
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

@app.get("/positions")
def get_positions(mode: str = Query("paper")):
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM active_positions WHERE mode = %s", (mode,))
            return cur.fetchall()
    finally:
        conn.close()

@app.get("/orders")
def get_orders(mode: str = Query("paper"), limit: int = 50):
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
    return {
        "mode": os.environ.get("MODE", "paper"),
        "live_enabled": enabled,
        "ack_ok": ack_ok,
        "halt_file": halt,
        "live_orders_allowed": enabled and ack_ok and not halt,
    }


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
