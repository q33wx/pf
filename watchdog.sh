#!/usr/bin/env bash
# Deterministic bot keep-alive — runs from cron every 5 min, no AI involved.
# Restarts any bot in bots/fleet.txt whose process died. Never trades, never
# decides — only revives. Claude supervises separately (bots/fleet.txt is
# the contract: retired bots are removed from it, new bots added).
set -u
cd "$(dirname "$0")"
LOG="logs/watchdog.log"
for cfg in $(grep -v '^#' bots/fleet.txt 2>/dev/null); do
  [ -f "$cfg" ] || continue
  name=$(grep -E '^name:' "$cfg" | sed 's/name: *"\?\([^"]*\)"\?/\1/')
  running=0
  for pid in $(pgrep -f "start_bot"); do
    if tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q "$cfg"; then
      running=1; break
    fi
  done
  if [ "$running" -eq 0 ]; then
    echo "$(date -u '+%F %T') restarting $name ($cfg)" >> "$LOG"
    setsid nohup .venv/bin/python3 -c "
import sys; sys.path.insert(0, 'src')
import asyncio
from bot_runner import start_bot
asyncio.run(start_bot('$cfg'))
" >/dev/null 2>&1 &
    sleep 20
  fi
done
