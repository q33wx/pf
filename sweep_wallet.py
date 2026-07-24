"""Wallet sweeper: no token ever rots unseen in the wallet.

For every SPL token account owned by the wallet:
- mint managed by an active fisher bot  -> leave alone (bot's job)
- WSOL                                   -> leave alone (trade plumbing)
- balance worth selling (pool has liquidity) -> SELL for SOL
- worthless (no/drained pool) or dust    -> BURN + CLOSE account (reclaims
                                            ~0.002 SOL rent per account)

Usage:
    python sweep_wallet.py            # dry-run: report only
    python sweep_wallet.py --live     # actually sell/burn/close
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

import base58
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from spl.token.instructions import (
    BurnParams,
    CloseAccountParams,
    burn,
    close_account,
)

from core.client import SolanaClient
from core.pubkeys import SystemAddresses
from platforms.pumpswap.client import PumpSwapClient

WSOL = "So11111111111111111111111111111111111111112"
MIN_SELL_VALUE_SOL = 0.001  # below this, burning is cheaper than selling
LIVE = "--live" in sys.argv


def managed_mints() -> set[str]:
    """Mints belonging to currently-RUNNING fisher bots (via open log fds)."""
    import glob
    import re

    import yaml

    running_logs = set()
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            # Only actual bot processes (not log viewers holding the files)
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmdline = f.read().decode(errors="replace")
            if "start_bot" not in cmdline:
                continue
            for fd in os.listdir(f"/proc/{pid}/fd"):
                target = os.readlink(f"/proc/{pid}/fd/{fd}")
                m = re.search(r"(bot-fisher[\w-]*)_\d+_\d+\.log", target)
                if m:
                    running_logs.add(m.group(1))
        except (PermissionError, FileNotFoundError, OSError):
            continue

    mints = set()
    for cfg_path in glob.glob("bots/bot-fisher*.yaml"):
        try:
            cfg = yaml.safe_load(open(cfg_path))
            if cfg.get("name") in running_logs:
                mints.add(cfg["fishing"]["mint"])
        except Exception:  # noqa: BLE001
            continue
    return mints


async def main() -> None:
    rpc = os.environ["SOLANA_NODE_RPC_ENDPOINT"]
    raw = base58.b58decode(os.environ["SOLANA_PRIVATE_KEY"])
    kp = Keypair.from_bytes(raw) if len(raw) == 64 else Keypair.from_seed(raw[:32])
    client = SolanaClient(rpc, max_rps=8)
    ps = PumpSwapClient(client)
    managed = managed_mints()
    print(f"mode: {'LIVE' if LIVE else 'DRY-RUN'} | managed mints: {len(managed)}")

    rpc_client = await client.get_client()
    from solana.rpc.types import TokenAccountOpts

    resp = await rpc_client.get_token_accounts_by_owner_json_parsed(
        kp.pubkey(), TokenAccountOpts(program_id=SystemAddresses.TOKEN_PROGRAM)
    )
    reclaimed = 0.0
    for acc in resp.value:
        info = acc.account.data.parsed["info"]
        mint = info["mint"]
        amount_ui = float(info["tokenAmount"]["uiAmount"] or 0)
        amount_raw = int(info["tokenAmount"]["amount"])
        decimals = int(info["tokenAmount"]["decimals"])
        label = f"{mint[:8]}… bal={amount_ui:,.2f}"

        if mint == WSOL:
            print(f"SKIP  {label} (WSOL plumbing)")
            continue
        if mint in managed:
            print(f"SKIP  {label} (managed by an active bot)")
            continue

        value_sol = 0.0
        pool = None
        if amount_ui > 0:
            try:
                pool = await ps.find_pool(Pubkey.from_string(mint))
                # Drained (rugged) pools produce garbage prices — verify the
                # pool actually holds SOL before trusting any valuation.
                sol_vault = await ps._safe_token_balance(pool.sol_vault)  # noqa: SLF001
                if sol_vault < int(0.5 * 1e9):
                    print(f"      {mint[:8]}…: pool drained (rug) — worthless")
                    pool = None
                else:
                    price = await ps.calculate_price(pool)
                    value_sol = amount_ui * price
            except Exception:  # noqa: BLE001
                pool = None

        if amount_ui > 0 and pool is not None and value_sol >= MIN_SELL_VALUE_SOL:
            print(f"SELL  {label} ≈ {value_sol:.5f} SOL")
            if LIVE:
                result = await ps.sell(
                    pool, kp, amount_ui, 0.0, slippage=0.15, priority_fee=500_000
                )
                print(f"      -> {result.status} {result.sol:.5f} SOL received")
                if result.status != "ok":
                    continue  # don't burn if the sell didn't work
                amount_raw = 0  # sold; fall through to close below
            else:
                continue

        # Worthless or dust (or just sold): burn remainder + close for rent
        action = "BURN+CLOSE" if amount_raw > 0 else "CLOSE"
        print(f"{action:5} {label} (value {value_sol:.6f} SOL, rent back ~0.002)")
        if LIVE:
            ixs = []
            if amount_raw > 0:
                ixs.append(
                    burn(
                        BurnParams(
                            program_id=SystemAddresses.TOKEN_PROGRAM,
                            account=acc.pubkey,
                            mint=Pubkey.from_string(mint),
                            owner=kp.pubkey(),
                            amount=amount_raw,
                        )
                    )
                )
            ixs.append(
                close_account(
                    CloseAccountParams(
                        program_id=SystemAddresses.TOKEN_PROGRAM,
                        account=acc.pubkey,
                        dest=kp.pubkey(),
                        owner=kp.pubkey(),
                    )
                )
            )
            sig = await ps._send(ixs, kp, priority_fee=300_000, compute_units=60_000)  # noqa: SLF001
            status = await client.confirm_transaction_status(sig) if sig else "failed"
            print(f"      -> {status} (tx={str(sig)[:16]}…)")
            if status == "ok":
                reclaimed += 0.00203928

    if LIVE:
        print(f"\nrent reclaimed: ~{reclaimed:.5f} SOL")
    await client.close()


asyncio.run(main())
