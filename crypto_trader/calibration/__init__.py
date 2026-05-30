"""
crypto_trader.calibration — offline, human-gated parameter calibration.

Never imported by the live engines. Run as `python -m crypto_trader.calibration.cli`.

Flow: closed-trade outcomes → bounded proposals → out-of-sample check →
proposal artifact → human approval → runtime_overrides.json (picked up live by
the Phase-2 ConfigStore). The optimizer never touches live capital.
"""
