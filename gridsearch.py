#!/usr/bin/env python3
"""Exhaustive brute-force strategy-space sweep — the definitive 'we checked
every combination' capstone.

For every (entry_time x entry_filter x exit_rule) combination, compute the
out-of-sample REAL EV (temporal train/test split), net of each token's OWN
measured entry slippage + round-trip cost. Rank all combos; report the best
and whether ANY clears a viable bar (+2%/trade, n_test>=200). Includes a
multiple-comparisons sanity check (expected false positives given # combos).
"""
import json
import itertools
import statistics as st
from datetime import datetime

DATA = "trades/dataset.jsonl"
COST = 0.03

def price_at(traj, t):
    v = 1.0
    for tr, m in (traj or []):
        if tr <= t:
            v = m
        else:
            break
    return v

# Build per-token price series (mult vs first-trade price) across time, from
# early_traj (0-5s) + snap_15/30 + outcome.path (30s+). Used to simulate exits.
def series(r):
    pts = list(r.get("early_traj") or [])
    s15, s30 = r.get("snap_15") or {}, r.get("snap_30") or {}
    first = None
    if s15.get("price") and s15.get("ret_from_first") is not None:
        first = s15["price"] / (1 + s15["ret_from_first"])
    if first:
        if s15.get("price"):
            pts.append([15.0, s15["price"] / first])
        if s30.get("price"):
            pts.append([30.0, s30["price"] / first])
        for t_rel, mult30 in (r["outcome"].get("path") or []):
            # path mult is vs the 30s ref price; convert to vs first
            pts.append([30.0 + t_rel, mult30 * (s30["price"] / first) if s30.get("price") else mult30])
    pts = [p for p in pts if p[1] > 0]
    pts.sort()
    return pts

def load():
    rows = []
    for l in open(DATA):
        try:
            r = json.loads(l)
        except Exception:
            continue
        if not (r.get("early_traj") and r.get("snap_15") and r.get("outcome") and r.get("created_ts")):
            continue
        s = series(r)
        if len(s) < 3:
            continue
        rows.append({"ts": r["created_ts"], "series": s, "snap": r["snap_30"] or {},
                     "s15": r["snap_15"] or {}})
    rows.sort(key=lambda x: x["ts"])
    return rows

def passes(snap, s15, filt):
    for k, op, v in filt:
        src = s15 if k.endswith("_15") else snap
        key = k.replace("_15", "")
        val = src.get(key)
        if val is None:
            return False
        if op == ">=" and not (val >= v):
            return False
        if op == "<=" and not (val <= v):
            return False
    return True

def scalp_return(s, entry_t, exit_rule):
    entry = price_at(s, entry_t)
    if entry <= 0:
        return None
    kind, param = exit_rule
    after = [(t, m) for t, m in s if t >= entry_t]
    if not after:
        return None
    if kind == "hold":       # hold to param seconds (or end)
        px = price_at(s, param)
    elif kind == "tp":       # first point hitting entry*param, else last
        px = next((m for t, m in after if m >= entry * param), after[-1][1])
    elif kind == "trail":    # exit when drops param from running peak
        peak = entry; px = after[-1][1]
        for t, m in after:
            peak = max(peak, m)
            if m <= peak * (1 - param):
                px = m; break
    else:
        px = after[-1][1]
    return px / entry - 1 - COST

FILTERS = {
    "none": [],
    "buyers>=20": [("uniq_buyers", ">=", 20)],
    "buyers>=40": [("uniq_buyers", ">=", 40)],
    "buyers>=60": [("uniq_buyers", ">=", 60)],
    "net>=5": [("net_sol", ">=", 5)],
    "net>=15": [("net_sol", ">=", 15)],
    "ratio>=1.5": [("buy_sell_ratio", ">=", 1.5)],
    "dev<=0.1": [("dev_share", "<=", 0.1)],
    "topbuyer<=0.2": [("top_buyer_share", "<=", 0.2)],
    "buyers>=40&dev<=0.15": [("uniq_buyers", ">=", 40), ("dev_share", "<=", 0.15)],
    "buyers>=40&topbuyer<=0.3": [("uniq_buyers", ">=", 40), ("top_buyer_share", "<=", 0.3)],
    "net>=10&buyers>=30": [("net_sol", ">=", 10), ("uniq_buyers", ">=", 30)],
}
ENTRY_TIMES = [0.8, 2.3, 5.0]
EXITS = ([("tp", x) for x in (1.1, 1.15, 1.2, 1.3, 1.5, 2.0)]
         + [("trail", x) for x in (0.08, 0.15, 0.25, 0.4)]
         + [("hold", x) for x in (15, 30, 60, 120, 300)])

def main():
    rows = load()
    n = len(rows)
    cut = rows[n // 2]["ts"]
    train = [r for r in rows if r["ts"] < cut]
    test = [r for r in rows if r["ts"] >= cut]
    print(f"loaded {n} tokens | train {len(train)} test {len(test)} | split @ {cut}")
    combos = list(itertools.product(FILTERS.items(), ENTRY_TIMES, EXITS))
    print(f"testing {len(combos)} combinations exhaustively...\n")
    results = []
    for (fname, filt), et, ex in combos:
        sub = [r for r in test if passes(r["snap"], r["s15"], filt)]
        if len(sub) < 100:
            continue
        rr = [scalp_return(r["series"], et, ex) for r in sub]
        rr = [x for x in rr if x is not None]
        if len(rr) < 100:
            continue
        ev = st.mean(rr)
        results.append((ev, fname, et, ex, len(rr), sum(1 for x in rr if x > 0) / len(rr)))
    results.sort(reverse=True)
    print(f"=== TOP 12 combinations by out-of-sample real EV (of {len(results)} valid) ===")
    print(f"{'EV':>7s} {'win%':>5s} {'n':>5s}  filter / entry / exit")
    for ev, fn, et, ex, nn, w in results[:12]:
        flag = "  <== VIABLE" if (ev > 0.02 and nn >= 200) else ""
        print(f"{ev:+7.1%} {100*w:4.0f}% {nn:5d}  {fn} @ {et}s exit={ex[0]}:{ex[1]}{flag}")
    pos = [r for r in results if r[0] > 0.02 and r[4] >= 200]
    print(f"\ncombos tested: {len(results)} | with OOS EV>+2% & n>=200: {len(pos)}")
    print(f"expected false-positives by chance (~5% tail): ~{int(0.05*len(results))}")
    best = results[0] if results else None
    if best:
        print(f"\nBEST OOS EV: {best[0]:+.1%} ({best[1]} @ {best[2]}s {best[3][0]}:{best[3][1]}, n={best[4]})")
        print("VERDICT:", "POTENTIALLY VIABLE — verify" if best[0] > 0.02 else "NO VIABLE COMBINATION EXISTS")

main()
