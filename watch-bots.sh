#!/usr/bin/env bash
# Watch all fisher bot logs in one stream.
# Auto-switches to each bot's newest log file after restarts.
# Usage: ./watch-bots.sh          (all log lines)
#        ./watch-bots.sh trades   (only entries/exits/errors/P&L lines)
cd "$(dirname "$0")"

# Auto-discover bots: newest log per bot name, but only bots that have
# written recently (so retired/replaced bots drop out and new ones appear
# without editing this script).
latest_logs() {
  for name in $(ls logs/bot-fisher*.log 2>/dev/null \
      | sed -E 's|logs/(.+)_[0-9]{8}_[0-9]{6}\.log|\1|' | sort -u); do
    f=$(ls -t "logs/${name}_"*.log 2>/dev/null | head -1)
    if [ -n "$f" ] && [ -n "$(find "$f" -mmin -10 2>/dev/null)" ]; then
      echo "$f"
    fi
  done | tr '\n' ' '
}

FILTER="cat"
if [[ "${1:-}" == "trades" ]]; then
  FILTER='grep --line-buffered -E "ENTRY|buy OK|sell OK|TAKE PROFIT|STOP LOSS|MAX HOLD|SCALE-OUT|Cycle complete|daily_pnl|ERROR|ADOPTED|RESTORED"'
fi

TAIL_PID=""
cleanup() { [ -n "$TAIL_PID" ] && kill "$TAIL_PID" 2>/dev/null; exit 0; }
trap cleanup INT TERM EXIT

echo "Watching all fisher bots (Ctrl+C to quit)..."
while true; do
  FILES=$(latest_logs)
  [ -z "$FILES" ] && { echo "no logs yet"; sleep 5; continue; }
  # shellcheck disable=SC2086
  eval "tail -n 3 -F $FILES 2>/dev/null | $FILTER" &
  TAIL_PID=$!
  # restart the tail when any bot rotates to a new log file
  while sleep 15; do
    [ "$(latest_logs)" != "$FILES" ] && break
    kill -0 "$TAIL_PID" 2>/dev/null || break
  done
  kill "$TAIL_PID" 2>/dev/null
  wait "$TAIL_PID" 2>/dev/null
done
