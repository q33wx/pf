# Deep Edge-Hunt Report — pump.fun Launch Trading (Retail Jito Scalp)

**Date:** 2026-07-18
**Scope:** Research only. All live trading is halted. Nothing here is a trade recommendation.
**Question:** Is there ANY capturable, out-of-sample, positive-EV strategy in pump.fun launch trading for a realistic retail Jito setup?

---

## Method & Ground Rules

- **Real entry price** = `price_at(early_traj, 0.8s)` — the drift we actually eat because we detect at creation and Jito lands ~0.8–1s later. This is our slippage, and it is unavoidable.
- **Real scalp return** = `(1 + snap_15.ret_from_first) / price_at(early_traj, 0.8) − 1 − 0.03` cost.
- **Bar for "real":** positive OUT-OF-SAMPLE on a temporal split, test mean EV > +2%, n_test ≥ 200, AND robust to tail-removal / slippage / split-direction stress.
- **Baseline (repeatedly reproduced):** the canonical 0.8s-entry / 15s-exit scalp earns ≈ **−3%** real EV. This matches the prior known result.

**Structural data limit that shadows everything:** `early_traj` — the ONLY field that lets us measure real 0.8s entry slippage — exists **only on a single day, 2026-07-18** (~5,700–7,800 tokens, hours 12–20 UTC). There is no weekday `early_traj`. So every "out-of-sample" slippage-inclusive test is really an *intra-day, time-of-day* split with zero cross-regime validation. Copy-trading data (07-17) has no `early_traj` at all.

---

## Per-Angle Results

| # | Angle | Best OOS number (raw) | Survives stress? | Verdict |
|---|-------|----------------------|------------------|---------|
| 1 | Portfolio / positive-skew | +4.8% (forward split) | No — flips to −1.2% reversed; hour-to-hour sign swings | **REFUTED** |
| 2 | Longer holds (30/60/120s+) | +2.99% (one split) | No — sign flips exactly on reversed split | **REFUTED** |
| 3 | Graduation prediction (ML) | +281% raw | No — dust-price artifact; −2% to −46% when realistic | **REFUTED** |
| 4 | Fade / bounce mean-reversion | +6.1% (test) | No — 26% win rate; top 1% of trades = all profit | **REFUTED** |
| 5 | Copy-trading skilled wallets | −17% (hold, measurable) | No — real EV clearly negative; scalp only "works" at 0 slippage | **REFUTED** |
| 6 | Optimal entry timing | −2.6% (capturable) | No — only idealized t=0 is positive, and that's uncapturable | **REFUTED** |
| 7 | Creator / dev behavioral signals | +2.09% (one split) | No — flips across splits/thresholds; dies at 5% cost | **REFUTED** |
| 8 | STEELMAN (best of 3,456 configs) | +38.2% (test) | No — one 101.7x token = 35% of PnL; −11.9% liquidity-capped | **REFUTED** |

### 1. Portfolio / positive-skew — REFUTED
Buying every launch is strongly negative (scalp −1.25%, hold-to-final −16.5%). The tail does **not** cover the losers after slippage + cost. A refined limit-buy + take-profit-at-5x + cut-at-15s hybrid showed +4.8% on one forward split, but reversing the split gives −1.2% and the sign swings hour to hour — it tracks an afternoon regime where 5x frequency happened to double, not a durable edge.

### 2. Longer holds — REFUTED
Fixed horizons get monotonically worse past 30s: 15s −2.86%, 30s −2.24%, 60s −3.55%, 120s −14.9%, end −16.4%. Holding *destroys* winners (tokens up at 60s are worth +212% sold at 60s but decay to +74% by 120s), and winners can't be identified at entry. The one +2.99% number flips sign exactly when the split is reversed — a pure time-of-day artifact.

### 3. Graduation prediction (ML classifier) — REFUTED
GradientBoosting on 15/30s features to catch the 1.69% "moon" base-rate looked spectacular (+281%), but the entire signal is a **dust-price denominator artifact**: winners had entry snapshots at near-zero prices (0.002x–0.114x) that are unfillable with any real size. Cap gross at a generous 5x → −2.3%; require a fillable ≥0.5x entry → −46.5%. No threshold or timing produced a robust +2%.

### 4. Fade / bounce mean-reversion — REFUTED
Enter after a drop from running peak, hold, hope for a bounce. Best config: +6.1% test, but win rate 26%, median trade −39%, and removing just the top 1% of trades turns every config negative. Bootstrap CI [−12%, +28%] straddles zero hugely. A realistic 5–10% entry slippage (you're buying an illiquid *crashing* token) erases it entirely.

### 5. Copy-trading skilled wallets — REFUTED
Coordination-deduped "skilled" entities produced a statistically-real lift in P(final≥1.5) (6.25% vs 3.82%, z=2.74) — but this is a **variance/lottery** signal: more 1.5x hits AND more rugs, with mean final (0.858) *below* baseline. Directly measurable hold EV is −17% to −38%. Scalp only looks positive at impossible zero slippage; the 07-18 proxy shows slippage (mean 20%, p90 47%) kills it.

### 6. Optimal entry timing — REFUTED
The only positive entry is the idealized t=0 (be the literal first buyer, zero latency): +15.4% — uncapturable. Our real capturable creation entry is −2.6%, and entering later is monotonically worse. A limit-TP variant looked like +11–13% but is again a crashed-token denominator artifact; restricted to normally-priced tokens it loses −7.5%.

### 7. Creator / dev behavioral signals — REFUTED
The strong dev_share signal is **look-ahead** (snap_15 dev_share shares the exit window). The one tradeable rule that touched +2.09% flips positive/negative across split points, swings −3.3% to +4.8% on tiny threshold changes, and dies at 5% cost. It's really just a slippage proxy: pedigreed creators get front-run less.

### 8. STEELMAN (aggressive optimization) — REFUTED
Best of 3,456 configs: train +7.5% → test +38.2% at face value. But the top single token is a 101.7x = 35% of all positive PnL; remove top-2 → −2.2%; liquidity-cap per-token at +2x → −11.9%. Of 3,456 configs, 12 cleared the train+test bar, **0 were tail-robust**. Median trade loses money in every time slice.

---

## Confirmed vs Refuted

- **Edges confirmed by all skeptics: 0.**
- **Edges refuted: 8 of 8.**

Every single positive out-of-sample number in this study died to at least one of four recurring failure modes:

1. **Tail lottery** — the whole mean rests on 1–5 tokens out of hundreds; remove them and it's a loss. Median trade loses money everywhere.
2. **Denominator / dust-price artifact** — "returns" computed off near-zero crashed-token entry prices that are unfillable with real size.
3. **Regime luck** — the positive split is a time-of-day fluke; reversing the train/test order flips the sign exactly.
4. **Slippage** — assuming clean fills. Adding a realistic 5–10% entry slippage (or even the measured 0.8s drift) erases the edge.

Underlying all of it is one structural wall, confirmed again from every angle: **winner-pop == winner-slippage.** The tokens that moon are precisely the tokens you cannot get filled on at a good price. The pop and the slippage are the same event.

---

## Bottom Line

**No.** Across 8 independent angles — portfolio skew, longer holds, ML graduation prediction, mean-reversion, copy-trading, entry timing, creator signals, and a fully-optimized steelman — there is **no capturable, out-of-sample, positive-EV strategy** in pump.fun launch trading for a realistic retail Jito setup. The realistic, tail-robust, liquidity-aware real EV sits around **−3%** (roughly the round-trip cost), and every apparent exception was a mirage under honest scrutiny. Additionally, the single day of slippage data means even a robust-looking edge could not be trusted. Do not deploy. The correct action is to stop.
