# CoinDCX API — Rate Limits

Source: https://docs.coindcx.com/ (captured 2026-05-30).

> **Futures rate limits are NOT published.** CoinDCX documents per-endpoint
> limits for the **SPOT** API only. The bot treats the spot numbers as the best
> available proxy and applies a conservative global throttle well under them.

## Published SPOT limits (count per window)

| API | Limit | Window |
|-----|-------|--------|
| Create Order Multiple | 2000 | 60s |
| Create Order | 2000 | 60s |
| Order Status | 2000 | 60s |
| Multiple Order Status | 2000 | 60s |
| Cancel | 2000 | 60s |
| Edit Price | 2000 | 60s |
| Cancel Multiple by ID | 300 | 60s |
| Active Order | 300 | 60s |
| Cancel All | 30 | 60s |

Notes:
- Most endpoints: **2000 / 60s** (~33/s).
- Stricter: **Active Order 300/60s**, **Cancel All 30/60s**.
- CoinDCX uses **per-endpoint count/window**, NOT Binance-style request weights.
- No published: per-endpoint weights, IP-ban policy, request/second granularity.

## General HTTP throttle errors
- `429 Too Many Requests` — back off. CoinDCX may send a `Retry-After` header.

## How the bot handles it

Two layers in `crypto_trader/exchanges/coindcx_client.py` + `infra/rate_limiter.py`:

1. **Proactive** (`get_coindcx_limiter`, token bucket): one shared global bucket
   at **600/min (10/s)**, burst 20 — far under the 2000/60s spot ceiling, with
   margin for the unknown futures limits. Acquired once per signed call.
   Tune via env: `COINDCX_RATE_PER_MIN`, `COINDCX_BURST_CAPACITY`.
2. **Reactive** (`_send`): on `429`, honour `Retry-After` (fallback
   `5 * (attempt+1)`s), then retry — for non-idempotent order mutations a 429 is
   safe to retry because the request was rejected before processing. 5xx /
   timeout on non-idempotent calls are NOT retried (reconcile instead).

The bot's call volume is low (per-symbol 60s reconcile + 5-min signal ticks), so
10/s is ample headroom. Raise `COINDCX_RATE_PER_MIN` only if scaling to many
symbols pushes sustained request rate toward the cap.

Binance market-data has a separate limiter (`get_rest_limiter`, default
1000/min) injected in `data_feed._request`.
