"""
crypto_trader.calibration.cli — human-gated calibration workflow.

    python -m crypto_trader.calibration.cli propose [--days 30] [--out DIR]
    python -m crypto_trader.calibration.cli approve <proposal.json> --yes
    python -m crypto_trader.calibration.cli revert

propose : read closed-trade outcomes → bounded proposals → out-of-sample check →
          write proposal-<utc>.json (status: pending_approval). Applies NOTHING.
approve : print the diff, require --yes, then write the whitelisted overrides to
          runtime_overrides.json (status: approved, active_profile: challenger).
          The live engine's ConfigStore picks it up on the next signal tick.
revert  : delete runtime_overrides.json (engine reverts to base config).

Safety: approve only writes keys in the Phase-2 SAFE/FLAT_ONLY whitelist; it can
never reach a restart-only or safety-gate parameter.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Dict, List

from ..config_store import DATA_DIR, SAFE_KEYS, FLAT_ONLY_KEYS
from ..journal import TradeOutcomeJournal
from . import proposer as _proposer
from . import walkforward as _walkforward

_CAL_DIR = DATA_DIR / "calibration"
_OVERRIDES_PATH = DATA_DIR / "runtime_overrides.json"
_APPROVABLE = SAFE_KEYS | FLAT_ONLY_KEYS

# levers the proposer reads from config (champion values)
_LEVERS = [
    "risk_per_trade_pct", "regime_size_multiplier_high",
    "regime_size_multiplier_low", "st2_sl_atr_mult", "mr_entry_band",
]


def _current_params() -> Dict[str, float]:
    from ..config import load_config
    cfg = load_config()
    return {k: float(getattr(cfg, k)) for k in _LEVERS if hasattr(cfg, k)}


def _load_records(days: int):
    cutoff_ms = int((time.time() - days * 86400) * 1000)
    return [r for r in TradeOutcomeJournal().load_all() if (r.closed_at or 0) >= cutoff_ms]


def cmd_propose(args) -> int:
    records = _load_records(args.days)
    current = _current_params()
    proposals = _proposer.propose(records, current)
    validation = _walkforward.validate(records)

    out_dir = Path(args.out) if args.out else _CAL_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    artifact = {
        "generated_at": ts,
        "data_window_days": args.days,
        "n_trades": len(records),
        "champion_params": current,
        "proposals": [p.to_dict() for p in proposals],
        "validation": validation,
        "status": "pending_approval",
    }
    path = out_dir / f"proposal-{ts}.json"
    path.write_text(json.dumps(artifact, indent=2))

    print(f"Wrote {path}")
    print(f"  trades={len(records)}  proposals={len(proposals)}  "
          f"verdict={validation['verdict']} ({validation['reason']})")
    for p in proposals:
        print(f"  - {p.param}: {p.current} -> {p.proposed}  [{p.rationale}]")
    if not proposals:
        print("  (no changes proposed)")
    return 0


def cmd_approve(args) -> int:
    path = Path(args.proposal)
    if not path.exists():
        print(f"error: {path} not found", file=sys.stderr)
        return 2
    artifact = json.loads(path.read_text())
    proposals: List[dict] = artifact.get("proposals", [])
    verdict = artifact.get("validation", {}).get("verdict")

    overrides = {p["param"]: p["proposed"] for p in proposals if p["param"] in _APPROVABLE}
    rejected = [p["param"] for p in proposals if p["param"] not in _APPROVABLE]

    print(f"Proposal {path.name}  verdict={verdict}")
    for p in proposals:
        mark = "" if p["param"] in _APPROVABLE else "  (SKIPPED: not whitelisted)"
        print(f"  {p['param']}: {p['current']} -> {p['proposed']}{mark}")
    if not overrides:
        print("Nothing approvable to write.")
        return 1
    if verdict == "reject":
        print("WARNING: validation verdict is 'reject' — applying anyway only if you insist.")
    if not args.yes:
        print("\nRe-run with --yes to write these overrides to "
              f"{_OVERRIDES_PATH}.")
        return 1

    payload = {
        "generated_from": path.name,
        "approved_at": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
        "active_profile": "challenger",
        "overrides": overrides,
        "status": "approved",
    }
    _OVERRIDES_PATH.parent.mkdir(parents=True, exist_ok=True)
    _OVERRIDES_PATH.write_text(json.dumps(payload, indent=2))
    # mark the proposal consumed
    artifact["status"] = "approved"
    path.write_text(json.dumps(artifact, indent=2))
    print(f"\nWrote {len(overrides)} override(s) to {_OVERRIDES_PATH}")
    if rejected:
        print(f"Skipped non-whitelisted: {rejected}")
    return 0


def cmd_revert(args) -> int:
    if _OVERRIDES_PATH.exists():
        _OVERRIDES_PATH.unlink()
        print(f"Removed {_OVERRIDES_PATH} — engine reverts to base config.")
    else:
        print("No active overrides to revert.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="crypto_trader.calibration.cli")
    sub = p.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("propose", help="generate a calibration proposal")
    pp.add_argument("--days", type=int, default=30)
    pp.add_argument("--out", type=str, default="")
    pp.set_defaults(func=cmd_propose)

    pa = sub.add_parser("approve", help="approve a proposal → runtime_overrides.json")
    pa.add_argument("proposal")
    pa.add_argument("--yes", action="store_true", help="confirm the write")
    pa.set_defaults(func=cmd_approve)

    pr = sub.add_parser("revert", help="remove active overrides")
    pr.set_defaults(func=cmd_revert)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
