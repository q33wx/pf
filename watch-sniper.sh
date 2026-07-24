#!/usr/bin/env bash
# Live view of the dry-run sniper.
#   ./watch-sniper.sh        buys, exits, heartbeats + 5-min scoreboards
#   ./watch-sniper.sh all    also show every SKIP decision (noisy)
cd "$(dirname "$0")" || exit 1
if ! infocmp "${TERM:-dumb}" >/dev/null 2>&1; then
  export TERM=xterm-256color
fi

G=$'\033[32m'; R=$'\033[31m'; Y=$'\033[33m'; D=$'\033[2m'; N=$'\033[0m'

scoreboard() {
  .venv/bin/python3 - <<'PY'
import json
try:
    recs = [json.loads(l) for l in open("trades/sniper_paper.jsonl")]
except FileNotFoundError:
    recs = []
if not recs:
    print("SCOREBOARD: no completed paper trades yet")
else:
    n = len(recs)
    wins = sum(1 for r in recs if r["mult"] >= 1)
    pnl = sum(r["paper_pnl_sol"] for r in recs)
    best = max(recs, key=lambda r: r["mult"])
    reasons = {}
    for r in recs:
        reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
    print(
        f"SCOREBOARD: {n} closed | {wins} wins ({100*wins/n:.0f}%) | "
        f"paper P&L {pnl:+.4f} SOL @0.02/snipe | best {best['mult']}x "
        f"({best['symbol']}) | exits {reasons}"
    )
PY
}

L=$(ls -t logs/sniper-dry_*.log 2>/dev/null | head -1)
[ -z "$L" ] && echo "no sniper log found — is sniper_dry.py running?" && exit 1
echo "watching $L  (Ctrl+C to quit)"
scoreboard

# periodic scoreboard alongside the stream
( while sleep 300; do echo; scoreboard; done ) &
SB=$!
trap 'kill $SB 2>/dev/null' EXIT

if [ "${1:-}" = "all" ]; then
  FILTER='.'
else
  FILTER='PAPER BUY|PAPER EXIT|HEARTBEAT|stream'
fi

tail -n 20 -F "$L" | grep --line-buffered -E "$FILTER" | while IFS= read -r line; do
  case "$line" in
    *"PAPER BUY"*)              printf '%s\n' "${G}${line}${N}" ;;
    *"PAPER EXIT"*pnl=-*)       printf '%s\n' "${R}${line}${N}" ;;
    *"PAPER EXIT"*)             printf '%s\n' "${G}${line}${N}" ;;
    *HEARTBEAT*)                printf '%s\n' "${Y}${line}${N}" ;;
    *SKIP*)                     printf '%s\n' "${D}${line}${N}" ;;
    *)                          printf '%s\n' "$line" ;;
  esac
done
