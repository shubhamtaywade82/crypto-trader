from __future__ import annotations
import pandas as pd

def classify_regime(df_4h: pd.DataFrame):
    ema_fast = df_4h["close"].ewm(span=20).mean().iloc[-1]
    ema_slow = df_4h["close"].ewm(span=50).mean().iloc[-1]
    score = abs((ema_fast - ema_slow) / max(ema_slow, 1e-9))
    if ema_fast > ema_slow:
        return "TRENDING_UP", min(1.0, score * 20)
    if ema_fast < ema_slow:
        return "TRENDING_DOWN", min(1.0, score * 20)
    return "CHOP", 0.4
