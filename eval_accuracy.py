"""
eval_accuracy.py - measure the pipeline's accuracy against known ground truth (offline, via cache).

Runs each labeled unit through the SAME parse + score pipeline used for real answers (replaying
archived raw/ captures - no network) and checks whether the expected person landed where they
should:

    expect "current"  -> expected name IS the top confirmed current resident
    expect "possible" -> expected name is surfaced (as current OR an unconfirmed lead)
    expect "none"     -> no confirmed current resident

This turns weight/threshold tuning into a measurement instead of a guess: change scoring, re-run,
watch precision. Extend the labeled set in eval_ground_truth.json as you verify more units.

    python eval_accuracy.py

Requires archived captures in raw/ for the building (do a live --browser run first to populate).
"""

from __future__ import annotations

import json
import os
import sys

from resident_core import normalize_address, name_key
from scoring import score_candidates
from lookup import gather_from_cache, normalize_street


def _has(expected: str, cands) -> bool:
    """Is `expected` among these candidates (matched on first+last name key)?"""
    return name_key(expected) in {name_key(c.name) for c in cands}


def run() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "eval_ground_truth.json"), encoding="utf-8") as f:
        gt = json.load(f)
    b = gt["building"]
    street = normalize_street(b["street"])

    print(f"Evaluating {len(gt['cases'])} labeled unit(s) at {street}, "
          f"{b['city']}, {b['state']} {b['zip']} (replaying raw/ cache)\n")

    passed = 0
    for case in gt["cases"]:
        unit, expect, name = case["unit"], case["expect"], case.get("name", "")
        people = gather_from_cache(street, [unit], verbose=False)
        target = normalize_address(f"{street} Unit {unit}, {b['city']}, {b['state']} {b['zip']}")
        current, possible, former, _ = score_candidates(people, target)

        if expect == "current":
            ok = bool(current) and _has(name, current[:1])   # must be the TOP pick
            got = f"{current[0].name} [{current[0].confidence}]" if current else "(none)"
        elif expect == "possible":
            ok = _has(name, current) or _has(name, possible)
            surfaced = [c.name for c in current] + [c.name for c in possible]
            got = ", ".join(surfaced[:4]) or "(none surfaced)"
        elif expect == "none":
            ok = not current
            got = current[0].name if current else "(none)"
        else:
            ok, got = False, f"(unknown expect '{expect}')"

        passed += ok
        print(f"  [{'PASS' if ok else 'FAIL'}] unit {unit}: expect {expect} "
              f"'{name}'  ->  {got}")

    total = len(gt["cases"])
    pct = f" ({100 * passed // total}%)" if total else ""
    print(f"\n  Precision: {passed}/{total}{pct}")
    return 1 if passed < total else 0


if __name__ == "__main__":
    sys.exit(run())
