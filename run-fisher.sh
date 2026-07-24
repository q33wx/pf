#!/usr/bin/env bash
# Start a PumpSwap fishing bot.
#   ./run-fisher.sh                      → bots/bot-fisher.yaml
#   ./run-fisher.sh bots/my-token.yaml   → a specific fisher config
set -euo pipefail
cd "$(dirname "$0")"

CONFIG="${1:-bots/bot-fisher.yaml}"
if [[ ! -f "$CONFIG" ]]; then
  echo "ERROR: config '$CONFIG' not found."
  exit 1
fi

if [[ ! -f .env ]]; then
  echo "ERROR: .env missing. Create it and fill in your keys."
  exit 1
fi

# shellcheck disable=SC1091
set -a
# Load .env without exporting comments-only lines
source .env
set +a

need() {
  local name="$1"
  local val="${!name:-}"
  if [[ -z "$val" || "$val" == *"PASTE_YOUR"* || "$val" == "..." ]]; then
    echo "ERROR: set $name in .env (replace the PASTE_YOUR_... placeholder)."
    exit 1
  fi
}

need SOLANA_NODE_RPC_ENDPOINT
need SOLANA_NODE_WSS_ENDPOINT
need SOLANA_PRIVATE_KEY

if [[ "$SOLANA_NODE_RPC_ENDPOINT" != https://* ]]; then
  echo "ERROR: SOLANA_NODE_RPC_ENDPOINT should start with https://"
  exit 1
fi
if [[ "$SOLANA_NODE_WSS_ENDPOINT" != wss://* ]]; then
  echo "ERROR: SOLANA_NODE_WSS_ENDPOINT should start with wss://"
  echo "       Use the same Helius URL as RPC but change https → wss"
  exit 1
fi

echo "Installing/updating deps if needed..."
uv sync --quiet
uv pip install -e . --quiet

echo ""
echo "Starting fisher (config: $CONFIG)"
echo "  dry_run: see $CONFIG (true = no real trades)"
echo "  stop with Ctrl+C"
echo ""

export PYTHONPATH=src
export FISHER_CONFIG="$CONFIG"
exec uv run python -c "
import asyncio, os
from bot_runner import start_bot
asyncio.run(start_bot(os.environ['FISHER_CONFIG']))
"
