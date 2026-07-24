"""Wallet skill tracker — test the copy-trading frame.

Instead of predicting tokens, find WALLETS whose early buys consistently
land in tokens that SUSTAIN (not just spike). If such wallets exist and
are identifiable, copying their buys (and holding) sidesteps the
fast-exit wall that killed the prediction strategies.

Per token (WSS-only, free): record every wallet that BOUGHT in the first
30s (with their first buy price), then track the token to +7min and log
the outcome (peak / final relative to a 30s reference). Offline we
aggregate per wallet: how many tokens they entered early, and the
distribution of those tokens' FINAL outcomes -> a real track record that
separates skill from luck (needs many tokens per wallet).

Output: trades/wallet_events.jsonl  (one record per token)
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

EARLY_SECS = 30            # "early buyer" = bought within this window
REF_SECS = 30              # outcome measured relative to price here
TRACK_SECONDS = 420

LOG = open(  # noqa: SIM115
    f"logs/wallet_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.log",
    "a", buffering=1)
OUT = open("trades/wallet_events.jsonl", "a", buffering=1)  # noqa: SIM115


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
        mint = b58(raw[o:o+32]); o += 32; o += 32
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
        self.early = {}          # wallet -> first early buy price
        self.ref_price = None
        self.peak = None
        self.final = None
        self.done = False

    def on_trade(self, tr):
        el = time.time() - self.t0
        if tr["is_buy"] and el <= EARLY_SECS and tr["user"] not in self.early:
            self.early[tr["user"]] = round(tr["price"], 12)
        if self.ref_price is None and el >= REF_SECS:
            self.ref_price = tr["price"]
        if self.ref_price:
            m = tr["price"]/self.ref_price
            self.peak = m if self.peak is None else max(self.peak, m)
            self.final = m

    def finalize(self):
        if self.done:
            return
        self.done = True
        if not self.ref_price or not self.early:
            return
        OUT.write(json.dumps({
            "mint": self.mint, "creator": self.creator,
            "ts": datetime.fromtimestamp(self.t0, timezone.utc).isoformat(timespec="seconds"),
            "n_early": len(self.early),
            "early_buyers": list(self.early.keys()),
            "peak_mult": round(self.peak or 1, 3),
            "final_mult": round(self.final or 1, 3),
        }) + "\n")


async def main():
    wss = os.environ.get("WSS_OVERRIDE") or os.environ["SOLANA_NODE_WSS_ENDPOINT"]
    toks = {}
    created = written = 0
    last = time.time()
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
                                toks[mint] = Tok(mint, creator); created += 1
                        elif raw[:8] == TRADE:
                            tr = parse_trade(raw)
                            t = tr and toks.get(tr["mint"])
                            if t and not t.done:
                                t.on_trade(tr)
                    for m, t in list(toks.items()):
                        if now - t.t0 > TRACK_SECONDS:
                            t.finalize(); written += 1; del toks[m]
                    if now - last >= 60:
                        last = now
                        log(f"HEARTBEAT created={created} written={written} tracking={len(toks)}")
        except Exception as exc:  # noqa: BLE001
            log(f"stream error, reconnect 5s: {exc}")
            await asyncio.sleep(5)


asyncio.run(main())
