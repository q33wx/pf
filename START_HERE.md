# Start here (fisher only)

## You only do this

### 1. Edit `.env` — put your 3 secrets

```env
SOLANA_NODE_RPC_ENDPOINT=https://mainnet.helius-rpc.com/?api-key=YOUR_KEY
SOLANA_NODE_WSS_ENDPOINT=wss://mainnet.helius-rpc.com/?api-key=YOUR_KEY
SOLANA_PRIVATE_KEY=your_base58_wallet_secret
```

- Helius key: [dashboard.helius.dev](https://dashboard.helius.dev)
- WSS = same URL as HTTP, but `wss://` instead of `https://`
- Use a **small test wallet**, not your main funds

### 2. Pick a token

In `bots/bot-fisher.yaml`, set `fishing.mint` to the token you want to fish
(and tune the thresholds to that token's tempo — see the comments in the file).

### 3. Run

```bash
./run-fisher.sh
```

Stop with **Ctrl+C**.

---

## Already set for you

| Setting | Value |
|--------|--------|
| Venue | PumpSwap |
| Mode | dry-run (no real money) |
| Config | `bots/bot-fisher.yaml` |

## When dry-run looks good (live trading)

1. Fund the wallet with a little SOL  
2. In `bots/bot-fisher.yaml` set `dry_run: false`  
3. Keep `buy_amount` small  
4. Run `./run-fisher.sh` again  

## Logs

- Terminal output  
- `logs/`  
- `trades/fisher_trades.log`
