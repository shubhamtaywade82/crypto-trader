# ── Stage 1: build the React dashboard ──────────────────────────────────────
FROM node:22-alpine AS ui
WORKDIR /ui
COPY ui/package.json ui/package-lock.json* ./
RUN npm install
COPY ui/ ./
RUN npm run build

# ── Stage 2: Python runtime (bot + execution worker + dashboard API) ─────────
FROM python:3.11-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1

COPY crypto_trader/requirements.txt ./crypto_trader/requirements.txt
RUN pip install --no-cache-dir -r crypto_trader/requirements.txt

COPY . .
# Built UI bundle (served by the FastAPI dashboard at "/").
COPY --from=ui /ui/dist ./ui/dist

# Default entrypoint is the gated live engine; compose overrides per service.
CMD ["python", "-m", "crypto_trader.engine_live", "--tick", "300"]
