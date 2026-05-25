# Antigravity — Project Memory

## 🏗️ Evolution (v4)
The project transitioned from a single-process bot to a decoupled, production-grade architecture.

- **Redis Streams:** Implemented for at-least-once delivery of trade signals.
- **Postgres Projections:** Added for a high-performance relational read-model.
- **Unified Orchestration:** Replaced manual component startup with `./bin/dev`.
- **Infrastructure:** Fixed port mappings (Postgres: 5435, Redis: 6382) to avoid local dev conflicts.

## 🛡️ Safety Hardening
- **Authoritative Guard:** G4 Guard monitors CoinDCX directly via `cross_margin_details`.
- **TDS Compliance:** 1% Indian TDS correctly modelled in PnL curves.
- **Clock Drift:** G1 Guard checks venue synchronization.

## 📊 Monitoring
- **Dashboard:** SolidJS frontend connected via FastAPI SSE bridge.
- **Port:** Running on 3030 (UI) and 8088 (API).
- **Test signals:** Verified using `bin/test_ui`.
