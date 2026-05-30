# CoinDCX API — Authentication (Signed Requests)

Source: https://docs.coindcx.com/ (captured 2026-05-30). Consolidated from the
inline signing shown in every private-endpoint code sample.

Private endpoints (orders, positions, wallets, etc.) require an HMAC-SHA256
signature of the **exact JSON request body**.

## Scheme

1. Build the request body as a JSON object that **MUST include** a
   `timestamp` field = current epoch in **milliseconds**
   (`int(time.time() * 1000)`).
2. Serialize to a compact JSON string (no spaces):
   `json.dumps(body, separators=(',', ':'))`.
3. `signature = HMAC_SHA256(api_secret, json_body).hexdigest()`  (hex digest).
4. Send the **same** `json_body` string as the POST body with headers:

| Header | Value |
|--------|-------|
| `X-AUTH-APIKEY` | your API key |
| `X-AUTH-SIGNATURE` | the hex HMAC signature |
| `Content-Type` | `application/json` |

> CRITICAL: sign the byte-for-byte body string you send. Re-serializing the dict
> after signing (different key order / spacing) breaks the signature → 401.

## Reference (Python)

```python
import hmac, hashlib, json, time, requests

key, secret = "XXXX", "YYYY"
secret_bytes = bytes(secret, encoding="utf-8")

body = {"timestamp": int(round(time.time() * 1000)), "page": "1", "size": "10",
        "margin_currency_short_name": ["USDT"]}
json_body = json.dumps(body, separators=(",", ":"))
signature = hmac.new(secret_bytes, json_body.encode(), hashlib.sha256).hexdigest()

headers = {"Content-Type": "application/json",
           "X-AUTH-APIKEY": key, "X-AUTH-SIGNATURE": signature}
resp = requests.post(
    "https://api.coindcx.com/exchange/v1/derivatives/futures/positions",
    data=json_body, headers=headers)
```

## Notes
- Orders are rejected if the `timestamp` is older than **10 seconds** — keep the
  local clock NTP-synced (the bot probes clock skew at live preflight).
- Public market-data endpoints (active_instruments, instrument, candlesticks,
  orderbook, current_prices) need **no** signature.
- WebSocket account channel auth: sign `{"channel":"coindcx"}` the same way and
  pass `authSignature` + `apiKey` on `join` (see `futures_websockets.md`).

## Bot implementation
`crypto_trader/exchanges/coindcx_client.py` — `_sign()` / `_signed_headers()`.
Body is built and serialized once, signed, and sent unchanged.
