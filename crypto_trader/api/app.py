"""
crypto_trader.api.app — dashboard API (read-only) + realtime SSE + static UI
=============================================================================
A separate process from the bot. Reads the Postgres projection the bot writes and
relays bot events over Server-Sent Events for a realtime dashboard. Read-only by
design: there are NO endpoints that place, cancel, or modify trades.

Run:  uvicorn crypto_trader.api.app:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse

from ..config import load_config
from .repo import ProjectionRepo, PostgresRepo

logger = logging.getLogger("crypto_trader.api")

# Built SolidJS bundle (Phase 2). Served at "/" when present.
UI_DIST = Path(__file__).resolve().parents[2] / "ui" / "dist"


def create_app(repo: Optional[ProjectionRepo] = None, event_source=None, cfg=None) -> FastAPI:
    """App factory. ``repo`` / ``event_source`` are injectable for tests."""
    cfg = cfg or load_config()
    app = FastAPI(title="crypto-trader dashboard", version="1.0")
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"],
    )

    if repo is None and cfg.database_url:
        repo = PostgresRepo(cfg.database_url)
    app.state.repo = repo
    app.state.cfg = cfg
    app.state.event_source = event_source  # async generator factory or None

    def _repo() -> ProjectionRepo:
        if app.state.repo is None:
            raise HTTPException(status_code=503, detail="no projection repo configured (set DATABASE_URL)")
        return app.state.repo

    @app.get("/api/health")
    def health():
        try:
            return {"status": "ok", **_repo().health(), "mode": cfg.mode.value}
        except Exception as e:
            return JSONResponse({"status": "degraded", "error": str(e)}, status_code=200)

    @app.get("/api/positions")
    def positions(mode: Optional[str] = Query(None, pattern="^(paper|live)$")):
        return _repo().positions(mode)

    @app.get("/api/orders")
    def orders(mode: Optional[str] = Query(None, pattern="^(paper|live)$"), limit: int = 50):
        return _repo().orders(mode, limit)

    @app.get("/api/fills")
    def fills(mode: Optional[str] = Query(None, pattern="^(paper|live)$"), limit: int = 50):
        return _repo().fills(mode, limit)

    @app.get("/api/pnl")
    def pnl(mode: Optional[str] = Query(None, pattern="^(paper|live)$")):
        return _repo().pnl(mode)

    @app.get("/api/stream")
    async def stream():
        """SSE: realtime bot events relayed from Redis pub/sub."""
        source_factory = app.state.event_source
        if source_factory is None:
            from .events_bridge import redis_event_source
            if not cfg.redis_url:
                return JSONResponse({"error": "REDIS_URL not configured"}, status_code=503)
            source_factory = lambda: redis_event_source(cfg.redis_url, cfg.ui_events_channel)

        async def gen():
            yield ": connected\n\n"  # initial comment so the client opens fast
            try:
                async for event in source_factory():
                    yield f"data: {json.dumps(event, default=str)}\n\n"
            except asyncio.CancelledError:  # client disconnected
                return

        return StreamingResponse(gen(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive",
        })

    # ── static UI (built SolidJS bundle) ─────────────────────────────────────
    if UI_DIST.exists():
        from fastapi.staticfiles import StaticFiles
        app.mount("/assets", StaticFiles(directory=str(UI_DIST / "assets")), name="assets")

        @app.get("/")
        def index():
            return FileResponse(str(UI_DIST / "index.html"))
    else:
        @app.get("/")
        def index_placeholder():
            return JSONResponse({
                "message": "Dashboard API running. Build the UI with: cd ui && npm install && npm run build",
                "endpoints": ["/api/health", "/api/positions", "/api/orders", "/api/fills", "/api/pnl", "/api/stream"],
            })

    return app


app = create_app()


def main():
    import uvicorn
    cfg = load_config()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run("crypto_trader.api.app:app", host=cfg.api_host, port=cfg.api_port)


if __name__ == "__main__":
    main()
