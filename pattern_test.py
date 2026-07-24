"""Forward paper-test of the creator-defense hypothesis (user's idea).

Thesis: after other wallets sell, the creator buys their own token to
defend the price -> the creator's buy marks a local bottom that bounces.

Test, per new pump.fun token, entirely on the free WSS stream (no RPC,
correct creator from the CreateEvent):
  - track the full trade tape + who the creator is
  - detect a SELL WAVE (net-negative SOL flow over a short window)
  - PAPER-ENTER right after the sell wave (thesis entry)
  - PAPER-EXIT when the creator next BUYS (thesis exit), else time-stop
  - ALSO log: did the creator ever buy after a sell wave at all, and
    what did price do next (bounce or keep dumping?) — the core question

Output: logs/pattern_<ts>.log, trades/pattern_paper.jsonl
Measures whether the pattern (a) occurs and (b) is profitable — BEFORE
worrying about whether we could execute it fast enough for real.
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

SELLWAVE_WINDOW = 6        # trades to look back for a sell wave
SELLWAVE_NET = -0.5        # net SOL <= this over the window = sell wave
MIN_TRADES_BEFORE = 8      # only consider tokens with real early activity
TRACK_SECONDS = 300        # follow each token this long
SIZE = 0.02

LOG = open(  # noqa: SIM115
    f"logs/pattern_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.log",
    "a", buffering=1)
OUT = open("trades/pattern_paper.jsonl", "a", buffering=1)  # noqa: SIM115


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
        creator = b58(raw[o:o+32])
        return mint, creator
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
        self.trades = []
        self.entry = None
        self.entry_i = None
        self.creator_bought_after_wave = 0
        self.done = False

    def on_trade(self, tr):
        self.trades.append(tr)
        i = len(self.trades) - 1
        # detect sell wave over the last window (excluding this trade)
        win = self.trades[max(0, i-SELLWAVE_WINDOW):i]
        net = sum((t["sol"] if t["is_buy"] else -t["sol"]) for t in win)
        sellwave = len(self.trades) >= MIN_TRADES_BEFORE and net <= SELLWAVE_NET

        # thesis entry: right after a sell wave, if flat
        if self.entry is None and sellwave and not tr["is_buy"]:
            self.entry = tr["price"]
            self.entry_i = i
            log(f"ENTER {self.mint[:8]} @ {self.entry:.3e} (sellwave net={net:.2f})")

        # is THIS a creator buy after a sell wave? (the core measurement)
        if tr["is_buy"] and tr["user"] == self.creator and sellwave:
            self.creator_bought_after_wave += 1
            fwd_from = tr["price"]
            log(f"CREATOR-BUY-AFTER-SELLWAVE {self.mint[:8]} @ {fwd_from:.3e}")
            # thesis exit: if we're in a position, exit here
            if self.entry is not None and not self.done:
                self._close(tr["price"], "creator_bought")

    def _close(self, price, reason):
        if self.done or self.entry is None:
            return
        self.done = True
        mult = price / self.entry
        OUT.write(json.dumps({
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "mint": self.mint, "reason": reason, "mult": round(mult, 3),
            "pnl_sol": round(SIZE*(mult-1), 5),
            "creator_buys_after_wave": self.creator_bought_after_wave,
        }) + "\n")
        log(f"EXIT {self.mint[:8]} {reason} mult={mult:.2f}")

    def timeout_close(self):
        if self.entry is not None and not self.done:
            self._close(self.trades[-1]["price"], "time_stop")
        elif not self.done:
            self.done = True  # never entered


async def main():
    wss = os.environ["SOLANA_NODE_WSS_ENDPOINT"]
    toks = {}
    created = entered = closed = creator_events = 0
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
                            if mint:
                                toks[mint] = Tok(mint, creator)
                                created += 1
                        elif raw[:8] == TRADE:
                            tr = parse_trade(raw)
                            t = tr and toks.get(tr["mint"])
                            if t and not t.done:
                                before = t.creator_bought_after_wave
                                had = t.entry is not None
                                t.on_trade(tr)
                                if not had and t.entry is not None:
                                    entered += 1
                                if t.creator_bought_after_wave > before:
                                    creator_events += 1
                                if t.done:
                                    closed += 1
                    for m, t in list(toks.items()):
                        if now - t.t0 > TRACK_SECONDS:
                            t.timeout_close()
                            del toks[m]
                    if now - last_beat >= 60:
                        last_beat = now
                        log(f"HEARTBEAT created={created} entered={entered} "
                            f"closed={closed} creator_buys_after_wave={creator_events} "
                            f"tracking={len(toks)}")
        except Exception as exc:  # noqa: BLE001
            log(f"stream error, reconnect 5s: {exc}")
            await asyncio.sleep(5)


asyncio.run(main())
