"""Behavior data collector for pump.fun launches — edge discovery.

For every new token, record its early-life fingerprint at snapshot times
(15s / 30s / 60s after creation) plus how it actually turned out over the
next several minutes. Writes one compact record per token to a dataset we
mine offline for ANY early feature that predicts winners.

Pure WSS stream — no RPC, near-zero cost. Correct creator identity comes
from the CreateEvent. Runs for hours to build a real sample.

Per-token record (trades/dataset.jsonl):
  mint, creator, created_ts
  snap_15 / snap_30 / snap_60: {n_trades, buys_sol, sells_sol, net_sol,
      buy_sell_ratio, uniq_buyers, uniq_sellers, dev_buys_sol, dev_share,
      top_buyer_share, price, ret_from_first}   # features observable then
  outcome: {peak_mult_from_30s, final_mult_from_30s, secs_to_peak,
      max_trades, rugged}                         # what happened after
"""
import asyncio
import base64
import hashlib
import json
import os
import struct
import time
from datetime import datetime, timezone

import websockets

PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
CREATE = hashlib.sha256(b"event:CreateEvent").digest()[:8]
TRADE = hashlib.sha256(b"event:TradeEvent").digest()[:8]

SNAPS = (15, 30, 60)        # feature snapshot times (s after creation)
ENTRY_REF = 30              # outcomes measured relative to price at this snap
TRACK_SECONDS = 420         # follow each token this long for the outcome
RUG_MULT = 0.15             # final price <= 15% of ref => "rugged"

LOG = open(  # noqa: SIM115
    f"logs/collector_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.log",
    "a", buffering=1)
DATA = open("trades/dataset.jsonl", "a", buffering=1)  # noqa: SIM115


def log(m):
    LOG.write(f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} {m}\n")


def b58(b):
    import base58
    return base58.b58encode(b).decode()


def parse_create(raw):
    try:
        o = 8
        for _ in range(3):
            n = struct.unpack_from("<I", raw, o)[0]; o += 4 + n
        mint = b58(raw[o:o+32]); o += 32
        o += 32
        return mint, b58(raw[o:o+32])
    except Exception:
        return None, None


def parse_trade(raw):
    try:
        o = 8
        mint = b58(raw[o:o+32]); o += 32
        sol, tok = struct.unpack_from("<QQ", raw, o); o += 16
        is_buy = raw[o] == 1; o += 1
        user = b58(raw[o:o+32]); o += 32; o += 8
        vsol, vtok = struct.unpack_from("<QQ", raw, o)
        if not vtok:
            return None
        return {"mint": mint, "sol": sol/1e9, "is_buy": is_buy,
                "user": user, "price": (vsol/1e9)/(vtok/1e6)}
    except Exception:
        return None


class Tok:
    def __init__(self, mint, creator):
        self.mint = mint
        self.creator = creator
        self.t0 = time.time()
        self.trades = []          # (t_rel, is_buy, user, sol, price)
        self.snaps = {}
        self.snaps_due = list(SNAPS)
        self.done = False

    def on_trade(self, tr):
        self.trades.append((time.time()-self.t0, tr["is_buy"], tr["user"],
                            tr["sol"], tr["price"]))

    def snapshot(self, upto):
        rows = [r for r in self.trades if r[0] <= upto]
        if not rows:
            return None
        buys = [r for r in rows if r[1]]
        sells = [r for r in rows if not r[1]]
        buy_sol = sum(r[3] for r in buys)
        sell_sol = sum(r[3] for r in sells)
        dev_buys = sum(r[3] for r in buys if r[2] == self.creator)
        # per-buyer totals for concentration
        bytop = {}
        for r in buys:
            bytop[r[2]] = bytop.get(r[2], 0) + r[3]
        top_share = (max(bytop.values())/buy_sol) if buy_sol else 0
        first_p = rows[0][4]
        last_p = rows[-1][4]
        return {
            "n_trades": len(rows),
            "buys_sol": round(buy_sol, 3),
            "sells_sol": round(sell_sol, 3),
            "net_sol": round(buy_sol - sell_sol, 3),
            "buy_sell_ratio": round(buy_sol/sell_sol, 2) if sell_sol else None,
            "uniq_buyers": len({r[2] for r in buys}),
            "uniq_sellers": len({r[2] for r in sells}),
            "dev_share": round(dev_buys/buy_sol, 3) if buy_sol else None,
            "top_buyer_share": round(top_share, 3),
            "price": last_p,
            "ret_from_first": round(last_p/first_p - 1, 3) if first_p else 0,
        }

    def check_snaps(self):
        el = time.time() - self.t0
        while self.snaps_due and el >= self.snaps_due[0]:
            s = self.snaps_due.pop(0)
            self.snaps[f"snap_{s}"] = self.snapshot(s)

    def finalize(self):
        if self.done:
            return
        self.done = True
        for s in self.snaps_due:
            self.snaps[f"snap_{s}"] = self.snapshot(s)
        ref = self.snaps.get(f"snap_{ENTRY_REF}")
        outcome = None
        if ref and ref["price"]:
            after = [r for r in self.trades if r[0] >= ENTRY_REF]
            if after:
                refp = ref["price"]
                peak = max(r[4] for r in after)
                peak_t = min(r[0] for r in after if r[4] == peak)
                final = after[-1][4]
                # Downsampled price path from the entry ref (~3s buckets),
                # as multiples of the ref price — lets us back-test exit
                # rules (trailing stops, time exits, scalps) offline.
                path = []
                last_t = -99
                for t_rel, _isb, _u, _sol, p in after:
                    if t_rel - last_t >= 3:
                        path.append([round(t_rel - ENTRY_REF, 1), round(p/refp, 3)])
                        last_t = t_rel
                outcome = {
                    "peak_mult": round(peak/refp, 3),
                    "final_mult": round(final/refp, 3),
                    "secs_to_peak": round(peak_t - ENTRY_REF, 1),
                    "max_trades": len(self.trades),
                    "rugged": (final/refp) <= RUG_MULT,
                    "path": path,
                }
        # EARLY trajectory: every trade in the first 5s as [t_rel, price/first].
        # We detect a token at its creation (first trade) and Jito lands our
        # buy ~1-2s later, so the price drift over that window IS our real
        # entry slippage — measurable here at scale, for free.
        early_traj = None
        early = [r for r in self.trades if r[0] <= 5.0]
        if early:
            fp = early[0][4]
            if fp:
                early_traj = [[round(r[0], 2), round(r[4]/fp, 4)] for r in early]
        DATA.write(json.dumps({
            "mint": self.mint, "creator": self.creator,
            "created_ts": datetime.fromtimestamp(self.t0, timezone.utc).isoformat(timespec="seconds"),
            "early_traj": early_traj,
            **self.snaps, "outcome": outcome,
        }) + "\n")


async def main():
    wss = os.environ.get("WSS_OVERRIDE") or os.environ["SOLANA_NODE_WSS_ENDPOINT"]
    toks = {}
    created = written = 0
    last_beat = time.time()
    while True:
        try:
            async with websockets.connect(wss, ping_interval=20) as ws:
                await ws.send(json.dumps({"jsonrpc":"2.0","id":1,"method":"logsSubscribe",
                    "params":[{"mentions":[PUMP]},{"commitment":"processed"}]}))
                await ws.recv()
                log("stream connected")
                while True:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
                    logs = msg.get("params",{}).get("result",{}).get("value",{}).get("logs",[])
                    now = time.time()
                    for l in logs:
                        if not l.startswith("Program data: "):
                            continue
                        try:
                            raw = base64.b64decode(l[14:])
                        except Exception:
                            continue
                        if raw[:8] == CREATE:
                            mint, creator = parse_create(raw)
                            if mint and mint not in toks:
                                toks[mint] = Tok(mint, creator)
                                created += 1
                        elif raw[:8] == TRADE:
                            tr = parse_trade(raw)
                            t = tr and toks.get(tr["mint"])
                            if t and not t.done:
                                t.on_trade(tr)
                    for m, t in list(toks.items()):
                        t.check_snaps()
                        if now - t.t0 > TRACK_SECONDS:
                            t.finalize()
                            written += 1
                            del toks[m]
                    if now - last_beat >= 60:
                        last_beat = now
                        log(f"HEARTBEAT created={created} written={written} "
                            f"tracking={len(toks)}")
        except Exception as exc:  # noqa: BLE001
            log(f"stream error, reconnect 5s: {exc}")
            await asyncio.sleep(5)


asyncio.run(main())
