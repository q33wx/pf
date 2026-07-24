#!/usr/bin/env bash
# Live view of the REAL-MONEY sniper.
#   ./watch-live.sh        buys, exits, halts, heartbeats + scoreboard
#   ./watch-live.sh all    also show RAIL/PARSE skips (noisy)
cd "$(dirname "$0")" || exit 1
if ! infocmp "${TERM:-dumb}" >/dev/null 2>&1; then export TERM=xterm-256color; fi
G=$'\033[32m'; R=$'\033[31m'; Y=$'\033[33m'; B=$'\033[36m'; D=$'\033[2m'; N=$'\033[0m'

scoreboard() {
  .venv/bin/python3 - <<'PY'
import json, os, urllib.request
try:
    recs = [json.loads(l) for l in open("trades/sniper_live.jsonl")]
except FileNotFoundError:
    recs = []
buys = [r for r in recs if r.get("ev") == "buy"]
sells = [r for r in recs if r.get("ev") == "sell"]
pnl = sum(r.get("pnl_sol", 0) for r in sells)
wins = sum(1 for r in sells if r.get("mult", 0) >= 1)
try:
    rpc = [l.split("=", 1)[1].strip() for l in open(".env") if l.startswith("SOLANA_NODE_RPC_ENDPOINT=")][0]
    addr = [l.split("=", 1)[1].strip() for l in open(".env") if l.startswith("WALLET_PUBKEY=")][0]
    b = json.dumps({"jsonrpc":"2.0","id":1,"method":"getBalance","params":[addr]}).encode()
    bal = json.load(urllib.request.urlopen(urllib.request.Request(rpc, data=b, headers={"content-type":"application/json"}), timeout=8))["result"]["value"] / 1e9
    bals = f"{bal:.4f} SOL"
except Exception:
    bals = "?"
wr = f"{100*wins/len(sells):.0f}%" if sells else "-"
print(f"SCOREBOARD: {len(buys)} buys, {len(sells)} closed ({wr} win) | "
      f"realized P&L {pnl:+.4f} SOL | wallet {bals}")
PY
}

L=$(ls -t logs/sniper-live_*.log 2>/dev/null | head -1)
[ -z "$L" ] && echo "no live-sniper log — is sniper_live.py running?" && exit 1
echo "watching $L  (Ctrl+C to quit — this is a viewer only, it does NOT stop the sniper)"
scoreboard
( while sleep 120; do echo; scoreboard; done ) &
trap 'kill %1 2>/dev/null' EXIT

FILTER='REAL BUY|EXIT|STRANDED|HALTED|BUY FAIL|BUY ERROR|HEARTBEAT|stream|setup'
[ "${1:-}" = "all" ] && FILTER='.'

tail -n 15 -F "$L" | grep --line-buffered -E "$FILTER" | while IFS= read -r line; do
  case "$line" in
    *"REAL BUY"*)        printf '%s\n' "${B}${line}${N}" ;;
    *EXIT*pnl=-*|*STRANDED*|*"BUY FAIL"*|*"BUY ERROR"*) printf '%s\n' "${R}${line}${N}" ;;
    *EXIT*)              printf '%s\n' "${G}${line}${N}" ;;
    *HALTED*)            printf '%s\n' "${Y}${line}${N}" ;;
    *HEARTBEAT*)         printf '%s\n' "${D}${line}${N}" ;;
    *)                   printf '%s\n' "$line" ;;
  esac
done
