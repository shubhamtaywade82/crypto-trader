from __future__ import annotations

def _ema(values: list[float], span: int) -> float:
    if not values:
        return 0.0
    alpha = 2 / (span + 1)
    ema_v = values[0]
    for v in values[1:]:
        ema_v = alpha * v + (1 - alpha) * ema_v
    return ema_v

def classify_regime(df_4h: list[dict]):
    closes = [row["close"] for row in df_4h]
    ema_fast = _ema(closes, span=20)
    ema_slow = _ema(closes, span=50)
    score = abs((ema_fast - ema_slow) / max(ema_slow, 1e-9))
    if ema_fast > ema_slow:
        return "TRENDING_UP", min(1.0, score * 20)
    if ema_fast < ema_slow:
        return "TRENDING_DOWN", min(1.0, score * 20)
    return "CHOP", 0.4
