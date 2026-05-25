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
