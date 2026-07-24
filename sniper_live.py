"""LIVE pump.fun launch sniper — REAL MONEY.

Reuses the repo's proven bonding-curve buy/sell path (PlatformAwareBuyer/
Seller) and its production creation parser (PumpFunEventParser) — no new
money-handling or address-derivation code. This module adds only:
  (1) same-slot creation detection + the v2 filter (proven on 53 paper
      trades: 60% win, +0.43 SOL),
  (2) position management with TP / SL / trailing exits,
  (3) hard safety rails + a one-trade verification checkpoint.

Why no dry-simulate gate: this RPC (Helius) returns errors on
simulateTransaction, so a buy cannot be validated without sending. The
first real buy is therefore the test — so FIRST_TRADE_PAUSE halts after
the first completed snipe for a human check before running unattended.

Rails (hard):
  SNIPE_SOL / MIN_RESERVE_SOL   size + wallet floor
  MAX_DAILY_LOSS_SOL            realized-loss kill-switch
  MAX_CONCURRENT                open positions
  MAX_CONSEC_FAILS              consecutive buy/sell failures -> halt
  STOP_SNIPER file              instant manual halt of new buys
"""

import asyncio
import base64
import hashlib
import json
import os
import struct
import sys
import time
from datetime import datetime, timezone

import websockets
from solders.pubkey import Pubkey

SNIPE_SOL = 0.05
MIN_RESERVE_SOL = 0.10
MAX_DAILY_LOSS_SOL = 0.30
MAX_CONCURRENT = 3
MAX_CONSEC_FAILS = 3
FIRST_TRADE_PAUSE = True        # halt after first completed snipe for verification

OBSERVE_SECONDS = 2.0
MIN_BUY_SOL = 5.0
MIN_UNIQUE_BUYERS = 10
MAX_DEV_SHARE = 0.20

TP_MULT = 2.0
SL_MULT = 0.6
TRAIL_ARM = 1.4
TRAIL_GIVEBACK = 1.15
TIME_STOP = 300
PRICE_POLL = 3.0
BUY_SLIPPAGE = 0.25

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
CREATE_DISC = hashlib.sha256(b"event:CreateEvent").digest()[:8]
TRADE_DISC = hashlib.sha256(b"event:TradeEvent").digest()[:8]

LOG = open(  # noqa: SIM115
    f"logs/sniper-live_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.log",
    "a", buffering=1,
)
LEDGER = open("trades/sniper_live.jsonl", "a", buffering=1)  # noqa: SIM115


def log(msg: str) -> None:
    line = f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} {msg}"
    LOG.write(line + "\n")
    print(line, flush=True)


def _pk(buf, off):
    import base58
    return base58.b58encode(buf[off : off + 32]).decode(), off + 32


def parse_trade(raw):
    try:
        off = 8
        mint, off = _pk(raw, off)
        sol_amount, _tok = struct.unpack_from("<QQ", raw, off)
        off += 16
        is_buy = raw[off] == 1
        off += 1
        user, off = _pk(raw, off)
        return {"mint": mint, "sol": sol_amount / 1e9, "is_buy": is_buy, "user": user}
    except Exception:
        return None


def create_mint(raw):
    """Just the mint, for indexing — full TokenInfo parsed on qualify."""
    try:
        off = 8
        for _ in range(3):  # name, symbol, uri
            n = struct.unpack_from("<I", raw, off)[0]
            off += 4 + n
        mint, off = _pk(raw, off)
        _bc, off = _pk(raw, off)
        creator, off = _pk(raw, off)
        return mint, creator
    except Exception:
        return None, None


class Sniper:
    def __init__(self) -> None:
        self.daily_loss = 0.0
        self.consec_fails = 0
        self.open_positions = 0
        self.halted = False
        self.real_trades = 0

    async def setup(self) -> None:
        sys.path.insert(0, "src")
        from core.client import SolanaClient
        from core.wallet import Wallet
        from core.priority_fee.manager import PriorityFeeManager
        from platforms import get_platform_implementations
        from interfaces.core import Platform
        from trading.platform_aware import PlatformAwareBuyer, PlatformAwareSeller

        self.client = SolanaClient(os.environ["SOLANA_NODE_RPC_ENDPOINT"])
        self.wallet = Wallet(os.environ["SOLANA_PRIVATE_KEY"])
        impls = get_platform_implementations(Platform.PUMP_FUN, self.client)
        self.address_provider = impls.address_provider
        self.curve_manager = impls.curve_manager
        self.event_parser = impls.event_parser
        pfm = PriorityFeeManager(
            self.client,
            enable_dynamic_fee=False,
            enable_fixed_fee=True,
            fixed_fee=1_000_000,
            extra_fee=0.0,
            hard_cap=2_000_000,
        )
        # extreme_fast_mode skips the curve-price fetch and would need a
        # preset token amount (0 here) -> BuyZeroAmount. Keep it off so the
        # buyer fetches the real price, sizes the buy, and sets a slippage
        # floor; one extra RPC (~40ms) is fine at creation+2s.
        self.buyer = PlatformAwareBuyer(
            self.client, self.wallet, pfm, amount=SNIPE_SOL,
            slippage=BUY_SLIPPAGE, max_retries=1, extreme_fast_mode=False,
        )
        self.seller = PlatformAwareSeller(
            self.client, self.wallet, pfm, slippage=0.25, max_retries=3,
        )
        bal = await self.balance()
        log(f"setup ok | wallet {bal:.3f} SOL | size {SNIPE_SOL} | "
            f"FIRST_TRADE_PAUSE={FIRST_TRADE_PAUSE}")

    async def balance(self) -> float:
        c = await self.client.get_client()
        return (await c.get_balance(self.wallet.pubkey)).value / 1e9

    def rails_block(self) -> str | None:
        if os.path.exists("STOP_SNIPER"):
            return "STOP_SNIPER file"
        if self.halted:
            return "halted"
        if self.daily_loss >= MAX_DAILY_LOSS_SOL:
            return f"daily loss cap ({self.daily_loss:.3f})"
        if self.consec_fails >= MAX_CONSEC_FAILS:
            return "consec-fail kill-switch"
        # Under first-trade-pause, block the moment ANY real buy has started
        # (real_trades increments on buy success) — otherwise up to
        # MAX_CONCURRENT positions could open before the first one exits,
        # defeating the "one trade then verify" guarantee.
        if FIRST_TRADE_PAUSE and (self.real_trades >= 1 or self.open_positions >= 1):
            return "first-trade pause (awaiting verification)"
        if self.open_positions >= MAX_CONCURRENT:
            return "max concurrent"
        return None

    async def on_qualified(self, logs: list, sym: str, wstats: dict) -> None:
        block = self.rails_block()
        if block:
            log(f"RAIL skip {sym}: {block}")
            return
        if await self.balance() - SNIPE_SOL < MIN_RESERVE_SOL:
            log(f"RAIL skip {sym}: reserve floor")
            return
        ti = self.event_parser.parse_token_creation_from_logs(logs, "snipe")
        if ti is None or ti.bonding_curve is None:
            log(f"PARSE FAIL {sym}: could not build TokenInfo — skip")
            return

        # The logs parser cannot tell legacy-SPL from Token-2022 and hard-codes
        # Token-2022; a wrong guess makes the buy fail IncorrectProgramId. Read
        # the mint's real owning program on-chain and re-derive the ATA to match.
        try:
            acct = await self.client.get_account_info(ti.mint, commitment="processed")
            owner = acct.owner
            if owner != ti.token_program_id:
                ti.token_program_id = owner
                ti.associated_bonding_curve = (
                    self.address_provider.derive_associated_bonding_curve(
                        ti.mint, ti.bonding_curve, owner
                    )
                )
                log(f"{sym}: corrected token program -> {owner}")
        except Exception as exc:  # noqa: BLE001
            log(f"{sym}: token-program probe failed ({exc}) — skip")
            return

        # ROOT-CAUSE FIX (per curve_manager.get_pool_state docstring): a
        # just-created bonding curve is only readable at "processed"
        # commitment in its creation slot; "confirmed" (the buyer's default)
        # lags 1-2 slots -> "bonding curve state not found". Read the price
        # ourselves at processed, size the buy from it, and hand the buyer
        # extreme_fast_mode (which also reads at processed internally). Retry
        # the fresh-read race; skip cleanly (no penalty) if it never reads.
        price = None
        for _ in range(6):
            try:
                ps = await self.curve_manager.get_pool_state(
                    ti.bonding_curve, commitment="processed"
                )
                price = ps.get("price_per_token")
                if price and price > 0:
                    break
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(0.25)
        if not price or price <= 0:
            log(f"{sym}: curve price unreadable at processed — skip (no penalty)")
            return
        self.buyer.extreme_fast_mode = True
        self.buyer.extreme_fast_token_amount = SNIPE_SOL / price  # decimal tokens

        self.open_positions += 1
        err = "?"
        try:
            res = await self.buyer.execute(ti)
        except Exception as exc:  # noqa: BLE001
            res = None
            err = str(exc)
        if res is None or not getattr(res, "success", False):
            if res is not None:
                err = getattr(res, "error_message", err)
            self.consec_fails += 1
            self.open_positions -= 1
            log(f"BUY FAIL {sym}: {err} (consec {self.consec_fails})")
            return
        self.consec_fails = 0
        self.real_trades += 1
        entry = res.price
        LEDGER.write(json.dumps({
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "ev": "buy", "symbol": sym, "mint": str(ti.mint),
            "entry": entry, "sol": SNIPE_SOL, "sig": str(res.tx_signature), **wstats,
        }) + "\n")
        log(f"REAL BUY #{self.real_trades} {sym} @ {entry:.3e} "
            f"tokens={res.amount:.0f} sig={res.tx_signature}")
        asyncio.create_task(self._manage(ti, entry, res.amount, sym))

    async def _manage(self, ti, entry, held, sym) -> None:
        peak, t0 = entry, time.time()
        try:
            while True:
                await asyncio.sleep(PRICE_POLL)
                price = await self.curve_manager.calculate_price(ti.bonding_curve)
                peak = max(peak, price)
                mult = price / entry
                reason = (
                    "take_profit" if mult >= TP_MULT
                    else "stop_loss" if mult <= SL_MULT
                    else "trail" if peak >= entry * TRAIL_ARM and price <= entry * TRAIL_GIVEBACK
                    else "time_stop" if time.time() - t0 >= TIME_STOP
                    else None
                )
                if reason:
                    await self._exit(ti, entry, price, held, sym, reason)
                    return
        except Exception as exc:  # noqa: BLE001
            log(f"MANAGE ERROR {sym}: {exc} — forcing exit")
            try:
                price = await self.curve_manager.calculate_price(ti.bonding_curve)
            except Exception:
                price = entry
            await self._exit(ti, entry, price, held, sym, "error_exit")

    async def _exit(self, ti, entry, price, held, sym, reason) -> None:
        # PlatformAwareSeller.execute REQUIRES (token_info, token_amount,
        # token_price) — the amount is what we actually bought, the price
        # sets the min-SOL-out slippage floor. Retry a few times; a sell
        # that never lands means tokens are STRANDED, so halt rather than
        # silently book a flat loss and move on.
        ok = False
        sig = None
        for attempt in range(3):
            try:
                res = await self.seller.execute(ti, token_amount=held, token_price=price)
                ok = getattr(res, "success", False)
                sig = str(getattr(res, "tx_signature", None))
                if ok:
                    break
            except Exception as exc:  # noqa: BLE001
                sig = f"error:{exc}"
            await asyncio.sleep(2)
        self.open_positions -= 1
        mult = price / entry
        pnl = SNIPE_SOL * (mult - 1)
        if mult < 1:
            self.daily_loss += SNIPE_SOL * (1 - mult)
        if not ok:
            self.consec_fails += 1
        LEDGER.write(json.dumps({
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "ev": "sell", "symbol": sym, "reason": reason, "mult": round(mult, 3),
            "pnl_sol": round(pnl, 5), "ok": ok, "held": held, "sig": sig,
        }) + "\n")
        log(f"EXIT {sym} {reason} mult={mult:.2f} pnl={pnl:+.4f} sell_ok={ok} "
            f"daily_loss={self.daily_loss:.3f}")
        if not ok:
            self.halted = True
            log(f"=== STRANDED: {sym} sell failed 3x, {held:.0f} tokens held. "
                "HALTED — manual exit needed (see STOP_SNIPER). ===")
        if FIRST_TRADE_PAUSE and self.real_trades >= 1:
            self.halted = True
            log("=== FIRST TRADE COMPLETE — HALTED for verification. "
                "Clear FIRST_TRADE_PAUSE=False to run unattended. ===")


async def main() -> None:
    s = Sniper()
    await s.setup()
    wss = os.environ["SOLANA_NODE_WSS_ENDPOINT"]
    watches: dict = {}
    created = qualified = 0
    last_beat = time.time()

    while True:
        try:
            async with websockets.connect(wss, ping_interval=20) as ws:
                await ws.send(json.dumps({
                    "jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
                    "params": [{"mentions": [PUMP_PROGRAM]}, {"commitment": "processed"}],
                }))
                await ws.recv()
                log("stream connected")
                while True:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
                    val = msg.get("params", {}).get("result", {})
                    logs = val.get("value", {}).get("logs", [])
                    now = time.time()
                    is_create = False
                    for line in logs:
                        if not line.startswith("Program data: "):
                            continue
                        try:
                            raw = base64.b64decode(line[14:])
                        except Exception:
                            continue
                        if raw[:8] == CREATE_DISC:
                            mint, creator = create_mint(raw)
                            if mint:
                                watches[mint] = {
                                    "logs": logs, "creator": creator, "t0": now,
                                    "buy": 0.0, "devbuy": 0.0, "buyers": set(),
                                    "sym": "?", "done": False,
                                }
                                created += 1
                                is_create = True
                        elif raw[:8] == TRADE_DISC:
                            tr = parse_trade(raw)
                            w = tr and watches.get(tr["mint"])
                            if w and tr["is_buy"]:
                                w["buy"] += tr["sol"]
                                w["buyers"].add(tr["user"])
                                if tr["user"] == w["creator"]:
                                    w["devbuy"] += tr["sol"]
                    if is_create:
                        pass

                    for mint, w in list(watches.items()):
                        if not w["done"] and now - w["t0"] >= OBSERVE_SECONDS:
                            w["done"] = True
                            dev = w["devbuy"] / w["buy"] if w["buy"] else 1.0
                            if (w["buy"] >= MIN_BUY_SOL
                                    and len(w["buyers"]) >= MIN_UNIQUE_BUYERS
                                    and dev <= MAX_DEV_SHARE):
                                qualified += 1
                                await s.on_qualified(w["logs"], mint[:6], {
                                    "w_buys": round(w["buy"], 2),
                                    "w_buyers": len(w["buyers"]), "w_dev": round(dev, 2),
                                })
                        if now - w["t0"] > 30:
                            del watches[mint]

                    if now - last_beat >= 60:
                        last_beat = now
                        log(f"HEARTBEAT created={created} qualified={qualified} "
                            f"real_trades={s.real_trades} open={s.open_positions} "
                            f"daily_loss={s.daily_loss:.3f} halted={s.halted}")
        except Exception as exc:  # noqa: BLE001
            log(f"stream error, reconnect 5s: {exc}")
            await asyncio.sleep(5)


asyncio.run(main())
