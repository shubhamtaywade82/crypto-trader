# Crypto Trader v4 Dashboard

A real-time dashboard for the Crypto Trader bot built with SolidJS and Vite.

## Features
- **Live/Paper Toggle:** View activity across different modes.
- **Real-time Updates:** Powered by Server-Sent Events (SSE) from the FastAPI bridge.
- **Relational Read-Model:** Directly queries the Postgres projection for high-performance position/order history.

## Getting Started

1. **Start the Infrastructure:**
   Ensure Postgres and Redis are running.
   ```bash
   docker compose up -d
   ```

2. **Start the API:**
   ```bash
   export DATABASE_URL=postgresql://trader:trader@localhost:5434/crypto_trader
   export REDIS_URL=redis://localhost:6379/0
   python -m crypto_trader.api
   ```

3. **Start the Frontend:**
   ```bash
   cd ui
   npm install
   npm run dev
   ```

4. **Access the Dashboard:**
   Open [http://localhost:5173](http://localhost:5173) in your browser.

## Tech Stack
- **Frontend:** SolidJS, TypeScript, Vite.
- **Backend:** FastAPI, Redis Streams, Postgres.
