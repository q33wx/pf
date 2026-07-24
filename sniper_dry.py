"""Dry-run pump.fun launch sniper — paper trades only, no SOL at risk.

Strategy under test:
  detect creation (same slot) -> observe first ~2s of trading ->
  filter (real buyers, dev not dominant) -> paper-buy at prevailing
  price with our size's impact -> track price path -> paper exits.

Everything runs off ONE websocket stream (all pump.fun program logs),
so steady-state RPC usage is ~zero. Every HTTP call and 429 is counted
to answer "when does the free tier stop being enough?".

Output:
  logs/sniper-dry_<ts>.log       heartbeats + decisions
  trades/sniper_paper.jsonl      one record per paper snipe
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

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
CREATE_DISC = hashlib.sha256(b"event:CreateEvent").digest()[:8]
TRADE_DISC = hashlib.sha256(b"event:TradeEvent").digest()[:8]

# --- filter / paper-trade parameters (v1 — the dry run tunes these) ---
OBSERVE_SECONDS = 2.0          # watch the tape this long after creation
MIN_BUY_SOL = 5.0              # real demand in the window
MIN_UNIQUE_BUYERS = 10
MAX_DEV_SHARE = 0.20            # creator's own buys / total buys
PAPER_SIZE_SOL = 0.02
ENTRY_EXTRA_SLIPPAGE = 0.02    # assumed fill worse than observed price
TRACK_SECONDS = 600            # follow each snipe's price for 10 min
TP_MULT = 2.0                  # default paper exit rules
SL_MULT = 0.6
TIME_STOP = 300

LOG = open(  # noqa: SIM115 - long-lived append handle
    f"logs/sniper-dry_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.log",
    "a",
    buffering=1,
)
PAPER = open("trades/sniper_paper.jsonl", "a", buffering=1)  # noqa: SIM115

http_calls = 0
http_429s = 0


def log(msg: str) -> None:
    LOG.write(f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} {msg}\n")


def read_pubkey(buf: bytes, off: int) -> tuple[str, int]:
    import base58
    return base58.b58encode(buf[off : off + 32]).decode(), off + 32


def read_string(buf: bytes, off: int) -> tuple[str, int]:
    n = struct.unpack_from("<I", buf, off)[0]
    off += 4
    return buf[off : off + n].decode(errors="replace"), off + n


def parse_create(raw: bytes) -> dict | None:
    try:
        off = 8
        name, off = read_string(raw, off)
        symbol, off = read_string(raw, off)
        _uri, off = read_string(raw, off)
        mint, off = read_pubkey(raw, off)
        _curve, off = read_pubkey(raw, off)
        creator, off = read_pubkey(raw, off)
        return {"name": name, "symbol": symbol, "mint": mint, "creator": creator}
    except Exception:
        return None


def parse_trade(raw: bytes) -> dict | None:
    try:
        off = 8
        mint, off = read_pubkey(raw, off)
        sol_amount, token_amount = struct.unpack_from("<QQ", raw, off)
        off += 16
        is_buy = raw[off] == 1
        off += 1
        user, off = read_pubkey(raw, off)
        off += 8  # timestamp
        vsol, vtok = struct.unpack_from("<QQ", raw, off)
        if vtok == 0:
            return None
        price = (vsol / 1e9) / (vtok / 1e6)
        return {
            "mint": mint,
            "sol": sol_amount / 1e9,
            "is_buy": is_buy,
            "user": user,
            "price": price,
        }
    except Exception:
        return None


class Watch:
    """A token from creation through observation and paper-position."""

    def __init__(self, info: dict, slot: int) -> None:
        self.info = info
        self.slot0 = slot
        self.t0 = time.time()
        self.buy_sol = 0.0
        self.dev_buy_sol = 0.0
        self.buyers: set[str] = set()
        self.last_price: float | None = None
        self.decided = False
        # paper position (set on entry)
        self.entry: float | None = None
        self.entry_t: float | None = None
        self.peak = 0.0
        self.path: list[tuple[float, float]] = []
        self.closed = False

    def on_trade(self, tr: dict) -> None:
        self.last_price = tr["price"]
        if tr["is_buy"]:
            self.buy_sol += tr["sol"]
            self.buyers.add(tr["user"])
            if tr["user"] == self.info["creator"]:
                self.dev_buy_sol += tr["sol"]
        if self.entry is not None and not self.closed:
            self.peak = max(self.peak, tr["price"])
            if not self.path or time.time() - self.path[-1][0] >= 2.0:
                self.path.append((time.time(), tr["price"]))

    def decide(self) -> None:
        self.decided = True
        dev_share = self.dev_buy_sol / self.buy_sol if self.buy_sol else 1.0
        ok = (
            self.buy_sol >= MIN_BUY_SOL
            and len(self.buyers) >= MIN_UNIQUE_BUYERS
            and dev_share <= MAX_DEV_SHARE
            and self.last_price
        )
        sym = self.info["symbol"]
        if not ok:
            log(
                f"SKIP {sym}: buys={self.buy_sol:.2f} SOL "
                f"buyers={len(self.buyers)} dev={dev_share:.0%}"
            )
            self.closed = True
            return
        self.win_stats = {
            "w_buys": round(self.buy_sol, 2),
            "w_buyers": len(self.buyers),
            "w_dev": round(dev_share, 2),
        }
        self.entry = self.last_price * (1 + ENTRY_EXTRA_SLIPPAGE)
        self.entry_t = time.time()
        self.peak = self.entry
        log(
            f"PAPER BUY {sym} @ {self.entry:.3e} | window: buys={self.buy_sol:.2f} "
            f"buyers={len(self.buyers)} dev={dev_share:.0%}"
        )

    def maybe_close(self, reason: str, price: float) -> None:
        if self.closed or self.entry is None:
            return
        self.closed = True
        mult = price / self.entry
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "mint": self.info["mint"],
            "symbol": self.info["symbol"],
            "entry": self.entry,
            "exit": price,
            "mult": round(mult, 3),
            "peak_mult": round(self.peak / self.entry, 3),
            "reason": reason,
            **getattr(self, "win_stats", {}),
            "paper_pnl_sol": round(PAPER_SIZE_SOL * (mult - 1), 5),
            "held_s": round(time.time() - (self.entry_t or 0)),
            "path": [(round(t - self.entry_t, 1), f"{p:.3e}") for t, p in self.path],
        }
        PAPER.write(json.dumps(rec) + "\n")
        log(
            f"PAPER EXIT {self.info['symbol']} {reason} mult={mult:.2f} "
            f"peak={self.peak / self.entry:.2f} pnl={rec['paper_pnl_sol']} SOL"
        )


async def main() -> None:
    wss = os.environ["SOLANA_NODE_WSS_ENDPOINT"]
    watches: dict[str, Watch] = {}
    created = skipped = entered = 0
    last_beat = time.time()

    while True:
        try:
            async with websockets.connect(wss, ping_interval=20) as ws:
                await ws.send(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "logsSubscribe",
                            "params": [
                                {"mentions": [PUMP_PROGRAM]},
                                {"commitment": "processed"},
                            ],
                        }
                    )
                )
                await ws.recv()
                log("stream connected")
                while True:
                    msg = json.loads(
                        await asyncio.wait_for(ws.recv(), timeout=30)
                    )
                    val = msg.get("params", {}).get("result", {})
                    slot = val.get("context", {}).get("slot", 0)
                    now = time.time()
                    for line in val.get("value", {}).get("logs", []):
                        if not line.startswith("Program data: "):
                            continue
                        try:
                            raw = base64.b64decode(line[14:])
                        except Exception:
                            continue
                        if raw[:8] == CREATE_DISC:
                            info = parse_create(raw)
                            if info:
                                watches[info["mint"]] = Watch(info, slot)
                                created += 1
                        elif raw[:8] == TRADE_DISC:
                            tr = parse_trade(raw)
                            if tr and tr["mint"] in watches:
                                watches[tr["mint"]].on_trade(tr)

                    # lifecycle ticks
                    for mint, w in list(watches.items()):
                        if not w.decided and now - w.t0 >= OBSERVE_SECONDS:
                            w.decide()
                            if w.entry:
                                entered += 1
                            elif w.closed:
                                skipped += 1
                        if w.entry and not w.closed and w.last_price:
                            p = w.last_price
                            if p >= w.entry * TP_MULT:
                                w.maybe_close("take_profit", p)
                            elif (
                                w.peak >= w.entry * 1.4
                                and p <= w.entry * 1.15
                            ):
                                w.maybe_close("trail", p)
                            elif p <= w.entry * SL_MULT:
                                w.maybe_close("stop_loss", p)
                            elif now - (w.entry_t or 0) >= TIME_STOP:
                                w.maybe_close("time_stop", p)
                        if now - w.t0 > TRACK_SECONDS:
                            if w.entry and not w.closed:
                                w.maybe_close("track_end", w.last_price or w.entry)
                            del watches[mint]

                    if now - last_beat >= 60:
                        last_beat = now
                        log(
                            f"HEARTBEAT created={created} entered={entered} "
                            f"skipped={skipped} watching={len(watches)} "
                            f"http={http_calls} 429s={http_429s}"
                        )
        except Exception as exc:  # noqa: BLE001
            log(f"stream error, reconnecting in 5s: {exc}")
            await asyncio.sleep(5)


asyncio.run(main())
