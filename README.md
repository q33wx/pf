# pump-fisher

Trading bots for pump.fun / PumpSwap. Built on top of
[chainstacklabs/pumpfun-bonkfun-bot](https://github.com/chainstacklabs/pumpfun-bonkfun-bot)
(Apache-2.0 — see `LICENSE` and `NOTICE`); this repo keeps only the parts of
that framework the strategies here actually use, plus everything built on top.

**Not financial advice. Trading memecoins can lose all funds. Start with
`dry_run: true` and a small throwaway wallet.**

## What's here

### Fisher — single-token scale-out trading (the main strategy)

`src/trading/fisher.py` watches **one** mint's buy/sell flow (PumpSwap AMM or
pump.fun bonding curve), enters when a rolling window shows a genuine buy wave
(net SOL inflow, buy/sell ratio, trade count, price momentum), scales out at
take-profit levels, and fully exits on stop-loss or max hold. Then cooldown and
re-arm for the next wave.

- Runs through the upstream bot runner: `mode: "fishing"` in a YAML under
  `bots/` (see `bots/bot-fisher.yaml` — a documented template; make one copy
  per token and tune the thresholds to that token's tempo).
- Extensive safety rails: daily-loss kill switch, consecutive-failure halt,
  rug/pool-drain detection, wallet reconciliation on restart, stranded-position
  handling, state persistence under `trades/`.
- Quickstart: `START_HERE.md`, or `./run-fisher.sh` (defaults to
  `bots/bot-fisher.yaml`). The template ships with `dry_run: true`; flip it
  only after watching a few entry signals look sane.

### Launch snipers (standalone scripts)

- `sniper_dry.py` — paper-trades new pump.fun launches (no funds needed).
- `sniper_live.py` — live version with hard rails (daily loss cap, concurrency
  cap, kill-file, first-trade pause).
- `sniper_scalp.py` — Jito-bundle-routed seed-pop scalper (`src/core/jito.py`).

**Read `docs/EDGE_HUNT_REPORT.md` before going live with any sniper.** An
exhaustive, adversarially-verified study over ~42k collected launches found
**no positive expected value** in launch scalping with this setup (real EV
≈ −2.8%/trade ≈ the round-trip cost). The snipers remain for data collection
and future re-testing.

### Research pipeline

- `collector.py` — records an early-life fingerprint + outcome for every new
  launch to `trades/dataset.jsonl` (pure WebSocket, near-zero RPC).
- `analyze.py`, `gridsearch.py` — mine the dataset for tradeable edges
  (out-of-sample, real-slippage-adjusted, multiple-comparison-aware).
- `pattern_test.py`, `wallet_tracker.py` — forward-tests of specific
  hypotheses (creator-defends-price pattern; early-buyer copy-trading).
- `scratchpad_*.py` — ad-hoc dataset exploration (needs `numpy`, not in
  `pyproject.toml`: `uv pip install numpy`).
- `docs/EDGE_HUNT_REPORT.md`, `docs/EDGE_HUNT_DEEP_REPORT.md` — the findings.

### Ops

- `watchdog.sh` — cron keep-alive; revives any bot listed in `bots/fleet.txt`
  (one config path per line). Deterministic — only revives, never decides.
- `watch-bots.sh` / `watch-live.sh` / `watch-sniper.sh` — live log viewers
  with P&L scoreboards.
- `sweep_wallet.py` — sells/burns leftover tokens in the wallet, skipping
  mints owned by currently-running fishers (dry-run by default, `--live` to
  act).

## Setup

```bash
uv sync
cp .env.example .env   # fill in RPC/WSS endpoints + wallet private key
```

`.env` needs `SOLANA_NODE_RPC_ENDPOINT`, `SOLANA_NODE_WSS_ENDPOINT`,
`SOLANA_PRIVATE_KEY` (base58). Helius free tier works; configs assume
`node.max_rps: 10` per bot to stay inside it.

Run a fisher:

```bash
./run-fisher.sh                          # bots/bot-fisher.yaml
./run-fisher.sh bots/my-token.yaml       # a specific fisher config
```

Trades append to `trades/fisher_trades.log`; session logs under `logs/`.
Protocol-level gotchas (incomplete IDL, account layouts, cashback/mayhem
handling) are documented in `docs/PROTOCOL_NOTES.md`.
