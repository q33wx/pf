#!/usr/bin/env python3
"""Reproducible verdict for the launch-momentum seed-pop scalp.

One command: .venv/bin/python3 analyze.py
Reads trades/dataset.jsonl (collector output) and prints, for weekday vs
weekend and per-hour:
  - REAL Jito entry slippage = price drift from creation to our ~1-2s
    landing, measured from each token's early_traj (median/p75/p90).
  - REAL scalp EV using each token's OWN measured slippage (no assumed
    number): enter at the drift price, exit at the 15s price, minus a
    round-trip cost. Reported for immediate (0.8s) and observe (2.3s).
  - Peak-hour slice (17-21 UTC = US afternoon = 20-24 Turkey).

Verdict rule: a slice is "viable" only if its real scalp EV is clearly
positive (> +2%/trade) on a meaningful sample (n >= 300). Everything
found so far is negative-to-breakeven; this script re-checks as data grows.
"""
import json
import statistics as st
import sys
from datetime import datetime

DATA = "trades/dataset.jsonl"
SELL_COST = 0.03          # round-trip fee+tip+sell-slippage estimate
ENTRY_TIMES = {"immediate(0.8s)": 0.8, "observe(2.3s)": 2.3}


def price_at(traj, t):
    """Market price (as multiple of first trade) as of t seconds after creation."""
    v = 1.0
    for tr, m in traj:
        if tr <= t:
            v = m
        else:
            break
    return v


def load():
    wk, we = [], []
    for line in open(DATA):
        try:
            r = json.loads(line)
        except Exception:
            continue
        ts = r.get("created_ts", "")
        day = ts[:10]
        if day in ("2026-07-16", "2026-07-17"):
            wk.append(r)
        elif day in ("2026-07-18", "2026-07-19"):
            we.append(r)
    return wk, we


def slippage(rows):
    out = {}
    for lab, t in ENTRY_TIMES.items():
        d = sorted(price_at(r["early_traj"], t) - 1 for r in rows if r.get("early_traj"))
        if len(d) < 30:
            continue
        n = len(d)
        out[lab] = (d[n // 2], d[3 * n // 4], d[int(0.9 * n)], n)
    return out


def scalp_ev(rows, entry_t):
    v = []
    for r in rows:
        s15 = r.get("snap_15") or {}
        tr = r.get("early_traj")
        if not tr or s15.get("ret_from_first") is None:
            continue
        entry = price_at(tr, entry_t)
        if entry > 0:
            v.append((1 + s15["ret_from_first"]) / entry - 1 - SELL_COST)
    if not v:
        return None
    return st.mean(v), sum(1 for x in v if x > 0) / len(v), len(v)


def report(name, rows):
    print(f"\n===== {name}  (n={len(rows)}) =====")
    slp = slippage(rows)
    if slp:
        print("  real Jito entry slippage (drift):")
        for lab, (med, p75, p90, n) in slp.items():
            print(f"    @{lab:16s} median {med:+.1%}  p75 {p75:+.1%}  p90 {p90:+.1%}  (n={n})")
    print("  real scalp EV (own measured slippage, exit~15s, -3% cost):")
    for lab, t in ENTRY_TIMES.items():
        r = scalp_ev(rows, t)
        if r:
            m, w, n = r
            verdict = "VIABLE" if (m > 0.02 and n >= 300) else "not viable"
            print(f"    entry {lab:16s}: EV {m:+.1%}  win {100*w:.0f}%  n={n}  -> {verdict}")


def main():
    wk, we = load()
    print(f"dataset: {len(wk)} weekday + {len(we)} weekend tokens")
    report("WEEKDAY (Thu/Fri)", wk)
    report("WEEKEND (Sat/Sun)", we)
    # peak-hour slice (17-21 UTC), weekend
    peak = [r for r in we if r.get("created_ts") and
            17 <= datetime.fromisoformat(r["created_ts"]).hour <= 21]
    report("WEEKEND PEAK 17-21 UTC (20-24 Turkey)", peak)


if __name__ == "__main__":
    sys.path.insert(0, ".")
    main()
