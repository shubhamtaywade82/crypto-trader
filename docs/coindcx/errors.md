# CoinDCX API — Error Codes

Source: https://docs.coindcx.com/ (captured 2026-05-30).

## General HTTP errors

| Code | Meaning |
|------|---------|
| 400 | Bad Request — your request is invalid |
| 401 | Unauthorized — your API key/signature is wrong |
| 404 | Not Found — the specified link could not be found |
| 429 | Too Many Requests — you're making too many API calls |
| 500 | Internal Server Error — problem on CoinDCX's side; try again later |
| 503 | Service Unavailable — temporarily offline for maintenance |

## Order-creation errors (Create Order)

| Code | Message | Reason |
|------|---------|--------|
| 422 | Order leverage must be equal to position leverage | order leverage ≠ current position leverage |
| 422 | Quantity for limit variant orders should be less than 9500.0 | limit qty over max |
| 422 | Quantity for market variant orders should be less than 9500.0 | market qty over max |
| 422 | Quantity should be greater than y | qty below `min_quantity` |
| 422 | Price can't be empty for limit_order | missing limit price |
| 400 | Price is out of permissible range | price > max_price or < min_price |
| 400 | Please enter a value lower than x | price > ltp·(1 + multiplier_up) |
| 400 | Please enter a value higher than x | price < ltp·(1 − multiplier_down) |
| 400 | Price should be divisible by 0.01 | price not on tick size |
| 400 | Insufficient funds | wallet lacks free margin for the order |
| 400 | Minimum order value should be x USDT | order value below `min_notional` |
| 400 | Instrument is in exit-only mode. You can't add more position | `exit_only=true` |
| 400 | You've exceeded the max allowed position of x USDT | current size over threshold |
| 400 | Order is exceeding the max allowed position of x USDT | size + order over threshold |
| 400 | Trigger price should be greater than the current price | buy order, trigger < price |
| 400 | Limit price should be greater than the trigger price | buy limit, limit < trigger |
| 400 | Trigger price should be less than the current price | sell order, trigger > price |
| 400 | Limit price should be less than the trigger price | sell order, limit < trigger |
| 500 | (Invalid input) | malformed request |

## Update-leverage errors

| Code | Message | Reason |
|------|---------|--------|
| 400 | Leverage cannot be less than 1x | below min leverage |
| 400 | Max allowed leverage for current position size = 5x | over tiered cap |
| 400 | Insufficient funds | wallet lacks funds to update |
| 422 | Liquidation will be triggered instantly | change would liquidate immediately |

## Remove-margin errors

| Code | Message | Reason |
|------|---------|--------|
| 422 | Cannot remove margin as exit or liquidation is already in process | exit/liq in progress |
| 422 | Cannot change margin for an inactive position | position inactive |
| 422 | Cannot remove margin more than available in position | amount > available |
| 422 | Liquidation will be triggered instantly | would liquidate immediately |
| 422 | Max Y USDT can be removed | over removable amount |
| 400 | Insufficient funds | wallet lacks funds |

## How the bot classifies these
`crypto_trader/exchanges/coindcx_client.py` `_send()`:
- **429** → honour `Retry-After`, back off, retry (safe — rejected pre-processing).
- **5xx / timeout on non-idempotent calls** → NOT retried; surfaced so the engine
  reconciles against the venue (no duplicate orders).
- **4xx (auth/validation)** → raised immediately, never counted as a circuit-breaker
  failure (caller bug, not venue outage).

`engine_ws._is_benign_entry_rejection()` treats a CoinDCX **4xx (≠429)** on entry
as a benign pre-fill rejection (e.g. min-notional, insufficient funds) → skip the
tick, keep trading. 5xx/429/timeout still HALT + reconcile.
