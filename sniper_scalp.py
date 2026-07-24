"""LIVE seed-pop scalp sniper — Jito-routed, real-fill accounting.

Strategy (pending data confirmation): detect a pump.fun creation same-slot,
optionally confirm a brief activity signal, buy FAST via a Jito bundle
(so we land ~1-2 slots and eat minimal price drift), then SCALP — exit on
a small take-profit or a short time-stop. Every buy/sell reads its REAL
fill from the transaction meta, so the ledger records true SOL-in/out and
true slippage (quote price vs actual fill) — no more optimistic estimates.

SAFETY:
  DRY_RUN=True (default) builds and signs the real transaction but does
    NOT send it — used to verify the pipeline constructs valid txs for $0.
  FIRST_TRADE_PAUSE halts after the first completed real cycle for a human
    check before running unattended.
  Rails: per-trade size cap, daily-loss kill-switch, one position at a
    time, consecutive-failure halt, STOP_SNIPER kill-file, wallet reserve
    floor. Mints in EXCLUDE_MINTS are hard-blocked (never traded).

Config values (filter, TP, time-stop) are PLACEHOLDERS to be set from the
weekend + real-slippage analysis before going live.
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
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.message import Message
from solders.pubkey import Pubkey
from solders.transaction import Transaction

# ---- config (PLACEHOLDERS — set from analysis before live) ----
DRY_RUN = True                 # True = build+sign but DON'T send (verify for $0)
FIRST_TRADE_PAUSE = True       # halt after first completed real cycle
SIZE_SOL = 0.03
BUY_SLIPPAGE = 0.30            # max we'll pay vs quote (loose; we MEASURE real)
SELL_SLIPPAGE = 0.20
TIP_LAMPORTS = 100_000         # Jito tip (~0.0001 SOL); benched at 95th pct

# entry: brief activity gate (keep short — the edge is near the seed)
OBSERVE_SECONDS = 1.5
MIN_BUY_SOL = 3.0
MIN_UNIQUE_BUYERS = 5
MAX_DEV_SHARE = 0.30

# scalp exit
TP_MULT = 1.15                 # small quick profit
STOP_MULT = 0.75
TIME_STOP = 20                 # seconds — bank the fast pop or bail
PRICE_POLL = 2.0

# rails
MIN_RESERVE_SOL = 0.10
MAX_DAILY_LOSS_SOL = 0.30
MAX_CONSEC_FAILS = 3

PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
CREATE = hashlib.sha256(b"event:CreateEvent").digest()[:8]
TRADE = hashlib.sha256(b"event:TradeEvent").digest()[:8]
EXCLUDE_MINTS: set[str] = set()  # mints to NEVER trade (e.g. tokens you hold)
TOKEN_DECIMALS = 6

LOG = open(  # noqa: SIM115
    f"logs/scalp_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.log",
    "a", buffering=1)
LEDGER = open("trades/scalp_live.jsonl", "a", buffering=1)  # noqa: SIM115


def log(m):
    line = f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} {m}"
    LOG.write(line + "\n"); print(line, flush=True)


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
        user = b58(raw[o:o+32])
        return {"mint": mint, "sol": sol/1e9, "is_buy": is_buy, "user": user}
    except Exception:
        return None


class Scalp:
    def __init__(self):
        self.daily_loss = 0.0
        self.consec_fails = 0
        self.open = 0
        self.halted = False
        self.real_trades = 0

    async def setup(self):
        sys.path.insert(0, "src")
        from core.client import SolanaClient
        from core.wallet import Wallet
        from core.priority_fee.manager import PriorityFeeManager
        from core.jito import JitoClient
        from platforms import get_platform_implementations
        from interfaces.core import Platform

        self.rpc = os.environ.get("RPC_OVERRIDE") or os.environ["SOLANA_NODE_RPC_ENDPOINT"]
        self.client = SolanaClient(self.rpc)
        self.wallet = Wallet(os.environ["SOLANA_PRIVATE_KEY"])
        impls = get_platform_implementations(Platform.PUMP_FUN, self.client)
        self.addr = impls.address_provider
        self.curve = impls.curve_manager
        self.ib = impls.instruction_builder
        self.parser = impls.event_parser
        self.pfm = PriorityFeeManager(
            self.client, enable_dynamic_fee=False, enable_fixed_fee=True,
            fixed_fee=1_000_000, extra_fee=0.0, hard_cap=3_000_000)
        self.jito = JitoClient(tip_lamports=TIP_LAMPORTS)
        self.Platform = Platform
        bal = await self.balance()
        log(f"setup ok | DRY_RUN={DRY_RUN} | wallet {bal:.3f} SOL | size {SIZE_SOL} "
            f"| tip {TIP_LAMPORTS/1e9} | TP {TP_MULT} time_stop {TIME_STOP}s")

    async def balance(self):
        c = await self.client.get_client()
        return (await c.get_balance(self.wallet.pubkey)).value / 1e9

    async def _blockhash(self):
        c = await self.client.get_client()
        return str((await c.get_latest_blockhash()).value.blockhash)

    async def _token_info(self, logs, mint):
        ti = self.parser.parse_token_creation_from_logs(logs, "scalp")
        if ti is None or ti.bonding_curve is None:
            return None
        # correct token program on-chain (SPL vs Token-2022)
        try:
            acct = await self.client.get_account_info(ti.mint, commitment="processed")
            if acct.owner != ti.token_program_id:
                ti.token_program_id = acct.owner
                ti.associated_bonding_curve = self.addr.derive_associated_bonding_curve(
                    ti.mint, ti.bonding_curve, acct.owner)
        except Exception:
            return None
        return ti

    def rails(self):
        if os.path.exists("STOP_SNIPER"):
            return "STOP_SNIPER"
        if self.halted:
            return "halted"
        if self.daily_loss >= MAX_DAILY_LOSS_SOL:
            return "daily-loss cap"
        if self.consec_fails >= MAX_CONSEC_FAILS:
            return "consec-fail kill"
        if self.open >= 1:
            return "position open"
        if FIRST_TRADE_PAUSE and self.real_trades >= 1:
            return "first-trade pause"
        return None

    async def _price(self, ti):
        for _ in range(6):
            try:
                ps = await self.curve.get_pool_state(ti.bonding_curve, commitment="processed")
                p = ps.get("price_per_token")
                if p and p > 0:
                    return p
            except Exception:
                pass
            await asyncio.sleep(0.2)
        return None

    async def _fill_from_tx(self, sig, mint):
        """Read REAL (sol_delta, token_delta) for our wallet from tx meta.
        sol_delta<0 = spent; token_delta>0 = received. Returns None if the
        tx never confirmed (caller reconciles via wallet balance)."""
        import urllib.request
        rpc = self.rpc
        wpk = str(self.wallet.pubkey)
        for _ in range(20):
            await asyncio.sleep(0.4)
            body = json.dumps({"jsonrpc":"2.0","id":1,"method":"getTransaction",
                "params":[sig, {"maxSupportedTransactionVersion":0,"encoding":"jsonParsed"}]}).encode()
            try:
                r = json.load(urllib.request.urlopen(urllib.request.Request(
                    rpc, data=body, headers={"content-type":"application/json"}), timeout=8))
                tx = r.get("result")
            except Exception:
                tx = None
            if not tx or not tx.get("meta"):
                continue
            m = tx["meta"]
            if m.get("err"):
                return ("failed", 0.0, 0.0)
            keys = [k["pubkey"] for k in tx["transaction"]["message"]["accountKeys"]]
            i = keys.index(wpk)
            sol_delta = (m["postBalances"][i] - m["preBalances"][i]) / 1e9
            def tok(bals):
                return sum(float(b["uiTokenAmount"]["uiAmount"] or 0)
                           for b in bals if b.get("owner") == wpk and b.get("mint") == mint)
            token_delta = tok(m.get("postTokenBalances", [])) - tok(m.get("preTokenBalances", []))
            return ("ok", sol_delta, token_delta)
        return None

    async def buy(self, ti, sym):
        quote = await self._price(ti)
        if not quote:
            log(f"{sym}: no price — skip"); return None
        amount_lamports = int(SIZE_SOL * 1e9)
        max_lamports = int(amount_lamports * (1 + BUY_SLIPPAGE))
        token_amount = SIZE_SOL / quote
        min_out = int(token_amount * (1 - BUY_SLIPPAGE) * (10 ** TOKEN_DECIMALS))
        ixs = await self.ib.build_buy_instruction(
            ti, self.wallet.pubkey, max_lamports, min_out, self.addr)
        tip = await self.jito.tip_instruction(self.wallet.pubkey)
        cu = self.ib.get_buy_compute_unit_limit()
        full = [set_compute_unit_limit(cu), set_compute_unit_price(1_000_000), *ixs, tip]
        bh = await self._blockhash()
        msg = Message.new_with_blockhash(full, self.wallet.pubkey, Hash.from_string(bh))
        tx = Transaction([self.wallet.keypair], msg, Hash.from_string(bh))
        if DRY_RUN:
            log(f"DRY_RUN buy {sym}: tx built OK ({len(full)} ix), quote={quote:.3e}, "
                f"would buy ~{token_amount:.0f} tok for {SIZE_SOL} SOL — NOT sent")
            return
        # LIVE: send via Jito, then read the REAL fill from tx meta.
        sig = str(tx.signatures[0])
        try:
            await self.jito.send_bundle([tx])
        except Exception as exc:  # noqa: BLE001
            self.consec_fails += 1
            log(f"BUY SEND FAIL {sym}: {exc}"); return
        fill = await self._fill_from_tx(sig, str(ti.mint))
        if not fill or fill[0] != "ok" or fill[2] <= 0:
            # landed-but-failed or never confirmed -> no position, no strand
            self.consec_fails += 1
            log(f"BUY FAIL {sym}: {fill[0] if fill else 'unconfirmed'} sig={sig}"); return
        _, sol_delta, tokens = fill
        spent = -sol_delta                       # includes fee + tip
        fill_price = spent / tokens
        real_slip = fill_price / quote - 1
        self.consec_fails = 0
        self.real_trades += 1
        self.open += 1
        LEDGER.write(json.dumps({
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "ev": "buy",
            "sym": sym, "mint": str(ti.mint), "quote": quote, "fill_price": fill_price,
            "real_slippage": round(real_slip, 4), "sol_spent": round(spent, 6),
            "tokens": tokens, "sig": sig}) + "\n")
        log(f"REAL BUY #{self.real_trades} {sym}: fill {fill_price:.3e} vs quote {quote:.3e} "
            f"= slip {real_slip:+.1%} | spent {spent:.5f} SOL | {tokens:.0f} tok")
        asyncio.create_task(self._manage(ti, sym, fill_price, tokens, spent))

    async def _manage(self, ti, sym, entry, tokens, cost):
        peak, t0 = entry, time.time()
        reason = None
        try:
            while reason is None:
                await asyncio.sleep(PRICE_POLL)
                p = await self._price(ti)
                if not p:
                    if time.time() - t0 >= TIME_STOP:
                        reason = "time_stop"; p = entry
                    continue
                peak = max(peak, p)
                mult = p / entry
                reason = ("take_profit" if mult >= TP_MULT
                          else "stop_loss" if mult <= STOP_MULT
                          else "time_stop" if time.time() - t0 >= TIME_STOP
                          else None)
        except Exception as exc:  # noqa: BLE001
            log(f"MANAGE ERR {sym}: {exc}"); reason = "error_exit"
        await self._exit(ti, sym, entry, tokens, cost, reason)

    async def _exit(self, ti, sym, entry, tokens, cost, reason):
        raw = int(tokens * (10 ** TOKEN_DECIMALS))
        ok = False; proceeds = 0.0; sig = None
        for attempt in range(3):
            try:
                q = await self._price(ti) or entry
                min_out = int(tokens * q * (1 - SELL_SLIPPAGE) * 1e9)
                ixs = await self.ib.build_sell_instruction(
                    ti, self.wallet.pubkey, raw, min_out, self.addr)
                tip = await self.jito.tip_instruction(self.wallet.pubkey)
                cu = self.ib.get_sell_compute_unit_limit()
                full = [set_compute_unit_limit(cu), set_compute_unit_price(1_000_000), *ixs, tip]
                bh = await self._blockhash()
                msg = Message.new_with_blockhash(full, self.wallet.pubkey, Hash.from_string(bh))
                tx = Transaction([self.wallet.keypair], msg, Hash.from_string(bh))
                sig = str(tx.signatures[0])
                await self.jito.send_bundle([tx])
                fill = await self._fill_from_tx(sig, str(ti.mint))
                # SUCCESS = tx landed AND tokens actually left the wallet
                # (token_delta<0). Do NOT gate on sol_delta>0: a dumped token
                # can net negative SOL (proceeds < fee+tip) on a genuine sell,
                # and gating on it would false-fail, retry a sold position,
                # and wrongly STRAND-halt. proceeds is the real net SOL delta.
                if fill and fill[0] == "ok" and fill[2] < 0:
                    proceeds = fill[1]; ok = True; break
            except Exception as exc:  # noqa: BLE001
                log(f"SELL attempt {attempt+1} {sym}: {exc}")
            await asyncio.sleep(1.5)
        self.open -= 1
        pnl = proceeds - cost
        if pnl < 0:
            self.daily_loss += -pnl
        if not ok:
            self.consec_fails += 1
            self.halted = True
            log(f"=== STRANDED {sym}: sell failed 3x, {tokens:.0f} tok held. HALTED. ===")
        LEDGER.write(json.dumps({
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "ev": "sell",
            "sym": sym, "reason": reason, "sold_ok": ok, "proceeds": round(proceeds, 6),
            "cost": round(cost, 6), "pnl_sol": round(pnl, 6), "sig": sig}) + "\n")
        log(f"EXIT {sym} {reason} sold_ok={ok} proceeds {proceeds:.5f} pnl {pnl:+.5f} SOL "
            f"daily_loss={self.daily_loss:.3f}")
        if FIRST_TRADE_PAUSE and self.real_trades >= 1:
            self.halted = True
            log("=== FIRST REAL CYCLE DONE — HALTED for verification. ===")


async def main():
    s = Scalp()
    await s.setup()
    wss = os.environ.get("WSS_OVERRIDE") or os.environ["SOLANA_NODE_WSS_ENDPOINT"]
    watches = {}
    created = qualified = 0
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
                    val = msg.get("params",{}).get("result",{}).get("value",{})
                    logs = val.get("logs", [])
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
                            if mint and mint not in EXCLUDE_MINTS and mint not in watches:
                                watches[mint] = {"logs": logs, "creator": creator,
                                    "t0": now, "buy": 0.0, "devbuy": 0.0,
                                    "buyers": set(), "done": False}
                                created += 1
                        elif raw[:8] == TRADE:
                            tr = parse_trade(raw)
                            w = tr and watches.get(tr["mint"])
                            if w and tr["is_buy"]:
                                w["buy"] += tr["sol"]; w["buyers"].add(tr["user"])
                                if tr["user"] == w["creator"]:
                                    w["devbuy"] += tr["sol"]
                    for mint, w in list(watches.items()):
                        if not w["done"] and now - w["t0"] >= OBSERVE_SECONDS:
                            w["done"] = True
                            dev = w["devbuy"]/w["buy"] if w["buy"] else 1.0
                            if (w["buy"] >= MIN_BUY_SOL and len(w["buyers"]) >= MIN_UNIQUE_BUYERS
                                    and dev <= MAX_DEV_SHARE):
                                qualified += 1
                                block = s.rails()
                                if block:
                                    log(f"RAIL {mint[:6]}: {block}")
                                elif mint not in EXCLUDE_MINTS:
                                    ti = await s._token_info(w["logs"], mint)
                                    if ti:
                                        # buy() manages the open-position count and
                                        # spawns _manage on a real fill
                                        await s.buy(ti, mint[:6])
                        if now - w["t0"] > 30:
                            del watches[mint]
                    if now - last >= 60:
                        last = now
                        log(f"HEARTBEAT created={created} qualified={qualified} "
                            f"real_trades={s.real_trades} open={s.open} "
                            f"daily_loss={s.daily_loss:.3f} DRY_RUN={DRY_RUN}")
        except Exception as exc:  # noqa: BLE001
            log(f"stream error, reconnect 5s: {exc}")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
