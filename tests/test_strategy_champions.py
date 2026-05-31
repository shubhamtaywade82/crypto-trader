"""Champion map: load/route logic + selector payload shape + write/read round-trip."""
import json

from crypto_trader.selection.champions import load_champions, champion_for
from crypto_trader.selection import selector
from crypto_trader.config import TradingConfig


def _write(tmp_path, payload):
    p = tmp_path / "strategy_champions.json"
    p.write_text(json.dumps(payload))
    return p


def test_champion_for_returns_entry(tmp_path):
    p = _write(tmp_path, {"champions": {
        "ETHUSDT": {"strategy_mode": "liquidity_sweep", "params": {"lsw_tp_r": 2.5},
                    "oos_pf": 1.55, "oos_expectancy_r": 0.14, "oos_trades": 39},
    }})
    c = champion_for("ETHUSDT", path=p)
    assert c is not None and c["strategy_mode"] == "liquidity_sweep"
    assert c["params"]["lsw_tp_r"] == 2.5


def test_none_champion_is_skipped(tmp_path):
    p = _write(tmp_path, {"champions": {
        "SOLUSDT": {"strategy_mode": "none", "reason": "no candidate passed gates"},
    }})
    assert champion_for("SOLUSDT", path=p) is None       # 'none' → no champion
    assert champion_for("XRPUSDT", path=p) is None       # absent → no champion
    assert load_champions(path=p)                         # file still loads


def test_missing_file_is_empty(tmp_path):
    assert load_champions(path=tmp_path / "nope.json") == {}
    assert champion_for("ETHUSDT", path=tmp_path / "nope.json") is None


def test_write_champions_drops_detail(tmp_path):
    payload = {"generated_at": 1, "champions": {"ETHUSDT": {"strategy_mode": "smc"}},
               "_detail": {"ETHUSDT": [("smc", {}, {})]}}
    p = tmp_path / "strategy_champions.json"
    selector.write_champions(payload, path=p)
    on_disk = json.loads(p.read_text())
    assert "_detail" not in on_disk            # verbose detail not persisted
    assert on_disk["champions"]["ETHUSDT"]["strategy_mode"] == "smc"


def test_config_flag_from_env(monkeypatch):
    monkeypatch.setenv("USE_STRATEGY_CHAMPIONS", "true")
    assert TradingConfig.from_env().use_strategy_champions is True
