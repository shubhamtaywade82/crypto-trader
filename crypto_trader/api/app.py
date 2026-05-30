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
import os
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ..config import load_config
from .repo import ProjectionRepo, PostgresRepo

logger = logging.getLogger("crypto_trader.api")

API_TOKEN = os.getenv("API_DASHBOARD_TOKEN", "")

# Built SolidJS bundle (Phase 2). Served at "/" when present.
UI_DIST = Path(__file__).resolve().parents[2] / "ui" / "dist"


def create_app(repo: Optional[ProjectionRepo] = None, event_source=None, cfg=None) -> FastAPI:
    """App factory. ``repo`` / ``event_source`` are injectable for tests."""
    cfg = cfg or load_config()
    app = FastAPI(title="crypto-trader dashboard", version="1.0")
    _allowed_origins = os.getenv("ALLOWED_ORIGINS", "*").split(",")
    app.add_middleware(
        CORSMiddleware, allow_origins=_allowed_origins, allow_methods=["GET"], allow_headers=["*"],
    )

    _bearer = HTTPBearer(auto_error=False)

    def _require_auth(creds: Optional[HTTPAuthorizationCredentials] = Security(_bearer)):
        if API_TOKEN and (creds is None or creds.credentials != API_TOKEN):
            raise HTTPException(status_code=401, detail="unauthorized")

    if repo is None and cfg.database_url:
        repo = PostgresRepo(cfg.database_url)
    app.state.repo = repo
    app.state.cfg = cfg
    app.state.event_source = event_source  # async generator factory or None

    def _repo() -> ProjectionRepo:
        if app.state.repo is None:
            raise HTTPException(status_code=503, detail="no projection repo configured (set DATABASE_URL)")
        return app.state.repo

    @app.get("/api/health", dependencies=[Depends(_require_auth)])
    def health():
        try:
            return {"status": "ok", **_repo().health(), "mode": cfg.mode.value}
        except Exception as e:
            logger.error("Health check error: %s", e)
            return JSONResponse({"status": "degraded", "error": "database unavailable"}, status_code=200)

    @app.get("/api/watchlist", dependencies=[Depends(_require_auth)])
    def watchlist():
        from ...multi_engine import load_watchlist
        try:
            return {"watchlist": load_watchlist()}
        except Exception as e:
            logger.error("Failed to load watchlist in API app: %s", e)
            sym = getattr(cfg, "symbol", "SOLUSDT")
            return {"watchlist": [sym]}

    @app.get("/api/positions", dependencies=[Depends(_require_auth)])
    def positions(mode: Optional[str] = Query(None, pattern="^(paper|live)$")):
        return _repo().positions(mode)

    @app.get("/api/orders", dependencies=[Depends(_require_auth)])
    def orders(mode: Optional[str] = Query(None, pattern="^(paper|live)$"), limit: int = Query(default=50, ge=1, le=500)):
        return _repo().orders(mode, limit)

    @app.get("/api/fills", dependencies=[Depends(_require_auth)])
    def fills(mode: Optional[str] = Query(None, pattern="^(paper|live)$"), limit: int = Query(default=50, ge=1, le=500)):
        return _repo().fills(mode, limit)

    @app.get("/api/pnl", dependencies=[Depends(_require_auth)])
    def pnl(mode: Optional[str] = Query(None, pattern="^(paper|live)$")):
        return _repo().pnl(mode)

    @app.get("/api/metrics", dependencies=[Depends(_require_auth)])
    def metrics():
        """Performance metrics over persisted closed-trade outcomes (win-rate,
        expectancy-R, profit factor, max drawdown, per-regime …). File-backed via
        TradeOutcomeJournal; degrades to zeroed/None metrics (HTTP 200) when no
        records exist."""
        from ..analytics import metrics as _metrics
        from ..journal import TradeOutcomeJournal
        try:
            records = list(TradeOutcomeJournal().load_all())
        except Exception as e:
            logger.error("Failed to load trade outcomes for /api/metrics: %s", e)
            records = []
        return _metrics.compute_metrics(records)

    @app.get("/api/equity", dependencies=[Depends(_require_auth)])
    def equity():
        """Equity curve: cumulative realized PnL over closed-trade outcomes,
        ordered by closed_at. Returns [] when there are no records."""
        from ..analytics import metrics as _metrics
        from ..journal import TradeOutcomeJournal
        try:
            records = list(TradeOutcomeJournal().load_all())
        except Exception as e:
            logger.error("Failed to load trade outcomes for /api/equity: %s", e)
            records = []
        return _metrics.equity_curve_from_records(records)

    @app.get("/api/stream", dependencies=[Depends(_require_auth)])
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
                "endpoints": ["/api/health", "/api/positions", "/api/orders", "/api/fills", "/api/pnl", "/api/metrics", "/api/equity", "/api/stream"],
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
