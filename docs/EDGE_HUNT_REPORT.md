# Pump.fun Launch-Scalping Edge Hunt — Final Report

**Date:** 2026-07-18
**Scope:** Exhaustive search for a capturable, out-of-sample, real-slippage-adjusted positive EV in scalping pump.fun token launches with a retail Jito setup.
**Dataset:** `/home/ubuntu/pumpfun-bonkfun-bot/trades/dataset.jsonl` (~41.7k lines; ~5.7k–7.3k tradeable rows with `early_traj` + `snap_15` + `outcome`).

---

## 1. Bottom-line verdict

**NO.** There is no capturable, out-of-sample, real-slippage-adjusted positive edge.

Four independent research angles and one adversarial re-implementation all converge on the same answer. The single positive-looking result (+12.72% OOS) was independently re-implemented and **refuted** — the exact stated rule produces **-5.6%** on the test half. After adversarial verification, **confirmed positive edges = 0.**

The honest, tradeable REAL scalp EV is **≈ -2.8% per trade** — almost exactly the ~3% round-trip cost. You are not trading a signal; you are paying the toll.

---

## 2. Key numbers by angle

| Angle | Best "actionable" rule | n_test | OOS REAL EV | Positive? |
|---|---|---|---|---|
| **Feature-interaction mining** (GBC/RF on snap features, honest entry-only redo) | Honest entry-only top-decile / drift@0.8 ≤ 0 | 363 | **-0.01%** | No |
| **Exit-rule optimization** (294 exit rules × 37 entry subsets) | Creator-prior-drift ≤ 0 + TP/time-stop | 681 | +12.72% *(claimed)* | **Refuted → -5.6%** |
| **Slippage↔pop regime** (is there a cheap winner?) | Entry cap: fill only if drift@0.8 ≤ 0 | 1507 | +3.39% *(regime artifact)* | No |
| **Regime / multiple-comparisons** (weekday/weekend, per-hour, FWER) | Best cell = UTC hour 16 | 3776 | **-1.99%** | No |

Supporting facts:
- Overall REAL EV: **-2.78%** (n=7552, t=-4.49, significantly negative). Matches the prior manual finding.
- Median entry slippage (drift@0.8s) = **0.0%**; mean **+20.2%** — the whole cost lives in a heavy right tail.
- corr(drift@0.8, peak_mult) = **-0.03**; corr(drift@0.8, final_mult) = **-0.02**. Entry slippage does **not** predict which token pops.
- Honest entry-only ML (features knowable at the 0.8s decision): AUC **≈ 0.58** — essentially no signal.
- Every "impressive" model (AUC 0.91–0.985, +70–105% top-decile) is **target leakage or look-ahead**: snap_15/snap_30 are measured *at or after* the 15s exit, so they cannot inform a 0.8s entry.

---

## 3. Why — the structural wall

**Winner-pop == winner-slippage.** The tokens that actually pop do so because of dense, simultaneous early buying. That same dense early buying *is* the slippage you eat, because your Jito bundle lands ~0.8–1s after you detect the launch. By the time you're filled, the pop you were chasing has already been priced into your entry. The pop and the cost are the same event.

Corollaries that killed every candidate edge:
1. **Winners are enterable at ~0% drift** (median winner slippage = 0%), so "buy only cheap entries" does *not* select for winners — it just avoids the sub-0.8s poppers, and the fixed 15s exit still fails to capture enough later upside to clear the 3% cost.
2. **All predictive features arrive too late.** The only genuinely informative variables (n_trades, uniq_buyers, top_buyer_share, ret_from_first) are snapshotted at 15s — contemporaneous with the exit, not the entry.
3. **The one real, look-ahead-free signal** (a creator's *prior* tokens averaged low drift; corr 0.64 with current drift) yields at best a fragile t≈1.95, tail-only raw edge that flips sign under exit choice and evaporates when the top ~10 of 960 winners are removed.

**Fatal data limitation:** `early_traj` — required to compute REAL slippage — exists **only for Saturday 2026-07-18**. Zero coverage on weekdays. So the "out-of-sample" split is only afternoon-vs-evening of a single day (~3.5 hours apart). No cross-day or weekday/weekend validation is possible. Any positive test number is as likely an intraday drift as a real edge; a genuine edge does not flip sign between two adjacent 2-hour windows (drift≤0: train -2.35% vs test +3.39%).

---

## 4. Confirmed edges & recommendation

**Confirmed positive edges: 0.**

The most promising candidate — the creator-prior-low-drift filter — is *real as a mechanism* (it genuinely selects low-slippage entries, look-ahead-free) but is **not tradeable**: sub-t=2 significance, entirely tail-concentrated (drop the top 20 of 960 winners and it goes negative), median trade -3%, win rate ~20–25%, and the tail winners are precisely the high-slippage dense-buying events where real Jito/priority fees exceed the assumed 3% and where partial/failed fills bite hardest. Bootstrap lower bound sits at ~0%.

**Recommendation: DO NOT GO LIVE.** No rule clears the pre-registered bar (mean REAL EV > +2%, n_test ≥ 200, stable sign OOS). The book on launch-scalping this setup is **closed with rigor**: the leftover expected value is negative once realistic execution costs are included.

If the collector eventually gathers multi-day `early_traj` (especially weekdays), the creator-history mechanism is the *only* thread worth re-testing — but only with a fresh, truly out-of-sample multi-day holdout and multiple-comparison correction. Until then, there is nothing to deploy.

---

*Metrics used (frozen definitions): `price_at(early_traj,t)` = last tick with t_rel ≤ t else 1.0. Entry = price_at(0.8). REAL scalp return = (1+snap_15.ret_from_first)/entry − 1 − 0.03. Positive finding required mean REAL EV > +2% on the later (test) temporal half with n_test ≥ 200.*
