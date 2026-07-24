"""PumpSwap pool discovery, pricing, buy, and sell for the fishing bot."""

from __future__ import annotations

import asyncio
import random
import struct
from dataclasses import dataclass

from solana.rpc.types import MemcmpOpts, TxOpts
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import Transaction
from spl.token.instructions import (
    CloseAccountParams,
    SyncNativeParams,
    close_account,
    create_idempotent_associated_token_account,
    get_associated_token_address,
    sync_native,
)

from core.client import SolanaClient
from core.pubkeys import LAMPORTS_PER_SOL, TOKEN_DECIMALS, SystemAddresses
from utils.logger import get_logger

logger = get_logger(__name__)

# Programs / accounts
SOL = SystemAddresses.SOL_MINT
PUMP_AMM_PROGRAM_ID = Pubkey.from_string("pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA")
PUMP_SWAP_GLOBAL_CONFIG = Pubkey.from_string(
    "ADyA8hdefvWN2dbGGWFotbzWxrAvLW83WG6QCVXvJKqw"
)
PUMP_SWAP_EVENT_AUTHORITY = Pubkey.from_string(
    "GS4CU59F31iL7aR2Q8zVS8DRrcRnXX1yjQ66TqNVQnaR"
)
PUMP_FEE_PROGRAM = Pubkey.from_string("pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ")
STANDARD_FEE_RECIPIENT = Pubkey.from_string(
    "7VtfL8fvgNfhz17qKRMjzQEXgbdpnHHHQRh54R9jP2RJ"
)

BREAKING_FEE_RECIPIENTS = [
    Pubkey.from_string("5YxQFdt3Tr9zJLvkFccqXVUwhdTWJQc1fFg2YPbxvxeD"),
    Pubkey.from_string("9M4giFFMxmFGXtc3feFzRai56WbBqehoSeRE5GK7gf7"),
    Pubkey.from_string("GXPFM2caqTtQYC2cJ5yJRi9VDkpsYZXzYdwYpGnLmtDL"),
    Pubkey.from_string("3BpXnfJaUTiwXnJNe7Ej1rcbzqTTQUvLShZaWazebsVR"),
    Pubkey.from_string("5cjcW9wExnJJiqgLjq7DEG75Pm6JBgE1hNv4B2vHXUW6"),
    Pubkey.from_string("EHAAiTxcdDwQ3U4bU6YcMsQGaekdzLS3B5SmYo46kJtL"),
    Pubkey.from_string("5eHhjP8JaYkz83CWwvGU2uMUXefd3AazWGx4gpcuEEYD"),
    Pubkey.from_string("A7hAgCzFw14fejgCp387JUJRMNyz4j89JKnhtKU8piqW"),
]

BUY_DISCRIMINATOR = bytes.fromhex("66063d1201daebea")
SELL_DISCRIMINATOR = bytes.fromhex("33e685a4017f83ad")

POOL_BASE_MINT_OFFSET = 43
POOL_QUOTE_MINT_OFFSET = 75
POOL_MAYHEM_MODE_OFFSET = 243
POOL_IS_CASHBACK_OFFSET = 244
POOL_MAYHEM_MODE_MIN_SIZE = 244
GLOBALCONFIG_RESERVED_FEE_OFFSET = 8 + 32 + 32  # disc + admin + default fee recipient

PROTOCOL_FEE_BUFFER = 0.1
DEFAULT_CU_BUY = 200_000
DEFAULT_CU_SELL = 150_000


@dataclass
class SwapResult:
    """Outcome of a PumpSwap buy or sell.

    status:
        ``ok``      — landed and executed; ``tokens``/``sol`` are ACTUAL fills
                      parsed from the transaction (not estimates).
        ``failed``  — definitively did not execute (send rejected, or landed
                      with a program error).
        ``unknown`` — submitted but unconfirmed within timeout. The trade MAY
                      still land; the caller must verify wallet balances
                      before acting as if it didn't.
    """

    status: str
    signature: str | None = None
    tokens: float = 0.0  # tokens received (buy) / sold (sell), UI units
    sol: float = 0.0  # SOL actually spent (buy) / received (sell), fees incl.
    price: float = 0.0  # effective SOL per token
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass
class SwapTemplate:
    """Account list learned from a real successful swap on the same pool.

    Inverted (SOL-base) pools are created directly on PumpSwap with layouts
    and fee recipients that differ from pump.fun-migrated pools. Instead of
    deriving every account, we copy the list from an observed transaction and
    substitute only the accounts derived from the signing user.
    """

    user: str  # the template tx's signer (accounts[1])
    accounts: list[tuple[str, bool, bool]]  # (pubkey, is_signer, is_writable)
    data_len: int


@dataclass
class PumpSwapPool:
    """Resolved PumpSwap pool accounts for a target token mint.

    ``token_is_base`` is True for classic pump.fun-migrated pools
    (base = token, quote = WSOL) and False for directly-created pools
    (base = WSOL, quote = token), where buy/sell semantics are flipped.
    """

    market: Pubkey
    base_mint: Pubkey
    quote_mint: Pubkey
    pool_base_token_account: Pubkey
    pool_quote_token_account: Pubkey
    coin_creator: Pubkey
    coin_creator_vault_authority: Pubkey
    coin_creator_vault_ata: Pubkey
    token_program_id: Pubkey
    is_mayhem_mode: bool
    is_cashback_coin: bool
    token_is_base: bool = True
    buy_template: SwapTemplate | None = None  # program `buy` ix (inverted pools)
    sell_template: SwapTemplate | None = None  # program `sell` ix (inverted pools)

    @property
    def token_mint(self) -> Pubkey:
        """The traded token's mint regardless of pool orientation."""
        return self.base_mint if self.token_is_base else self.quote_mint

    @property
    def sol_vault(self) -> Pubkey:
        """Pool vault holding WSOL."""
        return (
            self.pool_quote_token_account
            if self.token_is_base
            else self.pool_base_token_account
        )

    @property
    def token_vault(self) -> Pubkey:
        """Pool vault holding the traded token."""
        return (
            self.pool_base_token_account
            if self.token_is_base
            else self.pool_quote_token_account
        )


class PumpSwapClient:
    """High-level PumpSwap operations used by the fishing bot."""

    def __init__(self, solana_client: SolanaClient):
        """Initialize with shared Solana client.

        Args:
            solana_client: Project SolanaClient wrapper.
        """
        self.client = solana_client

    # ------------------------------------------------------------------
    # Discovery / price
    # ------------------------------------------------------------------

    async def find_pool(self, base_mint: Pubkey) -> PumpSwapPool:
        """Find and parse the PumpSwap pool for a token mint.

        Searches the base-mint slot first (pump.fun-migrated pools), then the
        quote-mint slot (directly-created pools with WSOL as base).

        Args:
            base_mint: Token mint (name kept for backward compatibility).

        Returns:
            PumpSwapPool with all accounts needed for trading.

        Raises:
            ValueError: If no pool is found.
        """
        rpc = await self.client.get_client()
        token_is_base = True
        filters = [MemcmpOpts(offset=POOL_BASE_MINT_OFFSET, bytes=bytes(base_mint))]
        response = await rpc.get_program_accounts(
            PUMP_AMM_PROGRAM_ID, encoding="base64", filters=filters
        )
        if not response.value:
            token_is_base = False
            filters = [
                MemcmpOpts(offset=POOL_QUOTE_MINT_OFFSET, bytes=bytes(base_mint))
            ]
            response = await rpc.get_program_accounts(
                PUMP_AMM_PROGRAM_ID, encoding="base64", filters=filters
            )
        if not response.value:
            raise ValueError(f"No PumpSwap pool found for mint {base_mint}")

        # Multiple pools can exist for one mint (including decoy/dust pools —
        # observed live: a near-empty second INK pool hijacked resolution and
        # every sell failed against its drained vaults). Pick the one with
        # the deepest SOL vault.
        market = response.value[0].pubkey
        if len(response.value) > 1:
            import struct as _struct

            best_sol = -1
            for acct in response.value:
                try:
                    raw = bytes(acct.account.data)
                    b_mint = raw[43:75]
                    vault_off = 139 if bytes(base_mint) == b_mint else 171
                    sol_off = 171 if bytes(base_mint) == b_mint else 139
                    sol_vault = Pubkey.from_bytes(raw[sol_off : sol_off + 32])
                    bal = await self.client.get_token_account_balance(sol_vault)
                except Exception:  # noqa: BLE001
                    continue
                if bal > best_sol:
                    best_sol = bal
                    market = acct.pubkey
            logger.info(
                f"{len(response.value)} pools found for mint; picked {market} "
                f"(SOL vault {best_sol / 1e9:.2f})"
            )
        account = await self.client.get_account_info(market)
        data = account.data
        if isinstance(data, tuple):  # base64 encoding sometimes returns (bytes, enc)
            data = data[0]
        if isinstance(data, str):
            import base64

            data = base64.b64decode(data)

        market_data = self._parse_pool_data(bytes(data))
        token_program_id = await self._get_token_program_id(base_mint)

        coin_creator = Pubkey.from_string(market_data["coin_creator"])
        vault_auth = self._find_coin_creator_vault(coin_creator)
        # Creator fees are paid in the QUOTE mint (WSOL for standard pools,
        # the token itself for inverted pools)
        quote_mint = Pubkey.from_string(market_data["quote_mint"])
        vault_ata = get_associated_token_address(
            vault_auth,
            SOL if token_is_base else quote_mint,
            SystemAddresses.TOKEN_PROGRAM,
        )

        is_mayhem = len(data) >= POOL_MAYHEM_MODE_MIN_SIZE and bool(
            data[POOL_MAYHEM_MODE_OFFSET]
        )
        is_cashback = len(data) > POOL_IS_CASHBACK_OFFSET and bool(
            data[POOL_IS_CASHBACK_OFFSET]
        )

        pool = PumpSwapPool(
            market=market,
            base_mint=(
                base_mint
                if token_is_base
                else Pubkey.from_string(market_data["base_mint"])
            ),
            quote_mint=quote_mint,
            pool_base_token_account=Pubkey.from_string(
                market_data["pool_base_token_account"]
            ),
            pool_quote_token_account=Pubkey.from_string(
                market_data["pool_quote_token_account"]
            ),
            coin_creator=coin_creator,
            coin_creator_vault_authority=vault_auth,
            coin_creator_vault_ata=vault_ata,
            token_program_id=token_program_id,
            is_mayhem_mode=is_mayhem,
            is_cashback_coin=is_cashback,
            token_is_base=token_is_base,
        )
        logger.info(
            f"PumpSwap pool: market={market} | "
            f"orientation={'token/SOL' if token_is_base else 'SOL/token (inverted)'} | "
            f"mayhem={is_mayhem} | cashback={is_cashback}"
        )
        if not token_is_base:
            await self._learn_swap_templates(pool)
        return pool

    async def calculate_price(self, pool: PumpSwapPool) -> float:
        """Price in SOL per token from vault balances.

        Args:
            pool: Resolved pool.

        Returns:
            SOL per token.
        """
        token_bal = await self.client.get_token_account_balance(pool.token_vault)
        sol_bal = await self.client.get_token_account_balance(pool.sol_vault)
        # balances are raw integers — convert with decimals
        token_ui = token_bal / (10**TOKEN_DECIMALS)
        sol_ui = sol_bal / LAMPORTS_PER_SOL
        if token_ui <= 0:
            return 0.0
        return sol_ui / token_ui

    # ------------------------------------------------------------------
    # Buy / sell
    # ------------------------------------------------------------------

    async def buy(
        self,
        pool: PumpSwapPool,
        payer: Keypair,
        sol_amount: float,
        slippage: float = 0.05,
        priority_fee: int = 200_000,
        compute_units: int = DEFAULT_CU_BUY,
    ) -> SwapResult:
        """Buy tokens with SOL on PumpSwap.

        Wraps exactly enough SOL to cover max slippage + protocol fees and
        closes the WSOL account in the same transaction, so leftovers are
        returned to the wallet instead of stranding as wrapped SOL.
        """
        if not pool.token_is_base:
            return await self._buy_inverted(
                pool, payer, sol_amount, slippage, priority_fee, compute_units
            )
        try:
            price = await self.calculate_price(pool)
            if price <= 0:
                return SwapResult(status="failed", error="Invalid pool price")

            base_amount_out = int((sol_amount / price) * 10**TOKEN_DECIMALS)
            max_sol_input = int((sol_amount * (1 + slippage)) * LAMPORTS_PER_SOL)

            user_base = get_associated_token_address(
                payer.pubkey(), pool.base_mint, pool.token_program_id
            )
            user_quote = get_associated_token_address(
                payer.pubkey(), SOL, SystemAddresses.TOKEN_PROGRAM
            )

            fee_recipient, fee_recipient_ata = await self._fee_recipients(pool)
            accounts = self._buy_accounts(
                pool,
                payer.pubkey(),
                user_base,
                user_quote,
                fee_recipient,
                fee_recipient_ata,
            )

            data = (
                BUY_DISCRIMINATOR
                + struct.pack("<Q", base_amount_out)
                + struct.pack("<Q", max_sol_input)
                + struct.pack("<B", 1)  # track_volume
            )
            buy_ix = Instruction(PUMP_AMM_PROGRAM_ID, data, accounts)

            # Protocol/LP/creator fees are charged ON TOP of quote_amount_in,
            # so the WSOL account must hold max_sol_input plus fee headroom.
            wrap_amount = int(max_sol_input * (1 + PROTOCOL_FEE_BUFFER))
            instructions = [
                create_idempotent_associated_token_account(
                    payer.pubkey(),
                    payer.pubkey(),
                    SOL,
                    SystemAddresses.TOKEN_PROGRAM,
                ),
                transfer(
                    TransferParams(
                        from_pubkey=payer.pubkey(),
                        to_pubkey=user_quote,
                        lamports=wrap_amount,
                    )
                ),
                sync_native(
                    SyncNativeParams(SystemAddresses.TOKEN_PROGRAM, user_quote)
                ),
                create_idempotent_associated_token_account(
                    payer.pubkey(),
                    payer.pubkey(),
                    pool.base_mint,
                    pool.token_program_id,
                ),
                buy_ix,
                # Unwrap leftover WSOL (+ rent) back to native SOL
                close_account(
                    CloseAccountParams(
                        program_id=SystemAddresses.TOKEN_PROGRAM,
                        account=user_quote,
                        dest=payer.pubkey(),
                        owner=payer.pubkey(),
                    )
                ),
            ]

            sig = await self._send(
                instructions,
                payer,
                priority_fee=priority_fee,
                compute_units=compute_units,
            )
            if not sig:
                return SwapResult(status="failed", error="Send failed", price=price)

            return await self._finalize_swap(
                sig, payer.pubkey(), pool.base_mint, side="buy", ref_price=price
            )

        except Exception as e:
            logger.exception("PumpSwap buy failed")
            return SwapResult(status="failed", error=str(e))

    async def sell(
        self,
        pool: PumpSwapPool,
        payer: Keypair,
        token_amount: float,
        price: float,
        slippage: float = 0.10,
        priority_fee: int = 200_000,
        compute_units: int = DEFAULT_CU_SELL,
    ) -> SwapResult:
        """Sell a token amount on PumpSwap.

        The amount is clamped to the actual on-chain token balance so a
        position tracked from estimates can never produce an unsellable
        oversized order. Proceeds are unwrapped to native SOL in the same
        transaction.
        """
        if not pool.token_is_base:
            return await self._sell_inverted(
                pool, payer, token_amount, price, slippage, priority_fee, compute_units
            )
        try:
            if token_amount <= 0:
                return SwapResult(status="failed", error="No tokens to sell")

            # Refresh price if caller passed a stale one
            if price <= 0:
                price = await self.calculate_price(pool)

            user_base = get_associated_token_address(
                payer.pubkey(), pool.base_mint, pool.token_program_id
            )
            user_quote = get_associated_token_address(
                payer.pubkey(), SOL, SystemAddresses.TOKEN_PROGRAM
            )

            token_raw = int(token_amount * 10**TOKEN_DECIMALS)
            balance_raw = await self._safe_token_balance(user_base)
            if balance_raw <= 0:
                return SwapResult(status="failed", error="No token balance on-chain")
            if token_raw > balance_raw:
                logger.info(
                    f"Sell clamped to on-chain balance: {token_raw} → {balance_raw}"
                )
                token_raw = balance_raw
            # If we'd leave unsellable dust behind, sweep it into this sell
            elif balance_raw - token_raw < 10**TOKEN_DECIMALS:
                token_raw = balance_raw

            expected_sol = (token_raw / 10**TOKEN_DECIMALS) * price
            min_sol_output = int((expected_sol * (1 - slippage)) * LAMPORTS_PER_SOL)

            fee_recipient, fee_recipient_ata = await self._fee_recipients(pool)
            accounts = self._sell_accounts(
                pool,
                payer.pubkey(),
                user_base,
                user_quote,
                fee_recipient,
                fee_recipient_ata,
            )

            data = (
                SELL_DISCRIMINATOR
                + struct.pack("<Q", token_raw)
                + struct.pack("<Q", min_sol_output)
            )
            sell_ix = Instruction(PUMP_AMM_PROGRAM_ID, data, accounts)

            instructions = [
                create_idempotent_associated_token_account(
                    payer.pubkey(),
                    payer.pubkey(),
                    SOL,
                    SystemAddresses.TOKEN_PROGRAM,
                ),
                sell_ix,
                # Unwrap sale proceeds (+ rent) back to native SOL
                close_account(
                    CloseAccountParams(
                        program_id=SystemAddresses.TOKEN_PROGRAM,
                        account=user_quote,
                        dest=payer.pubkey(),
                        owner=payer.pubkey(),
                    )
                ),
            ]

            sig = await self._send(
                instructions,
                payer,
                priority_fee=priority_fee,
                compute_units=compute_units,
            )
            if not sig:
                return SwapResult(status="failed", error="Send failed", price=price)

            return await self._finalize_swap(
                sig, payer.pubkey(), pool.base_mint, side="sell", ref_price=price
            )

        except Exception as e:
            logger.exception("PumpSwap sell failed")
            return SwapResult(status="failed", error=str(e), price=price)

    # ------------------------------------------------------------------
    # Inverted (SOL-base) pools — learned from live transactions
    # ------------------------------------------------------------------

    async def _learn_swap_templates(self, pool: PumpSwapPool) -> None:
        """Learn buy/sell account lists from recent successful pool swaps.

        Inverted pools use fee recipients and account variants that are not
        derivable from the vendored IDL, so we copy a real transaction's
        account list and later substitute only the user-derived accounts
        (verified: positions 1, 5, 6 and the volume-accumulator PDAs are the
        only user-specific accounts).
        """
        sigs_resp = await self.client.post_rpc(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getSignaturesForAddress",
                "params": [str(pool.market), {"limit": 40}],
            }
        )
        sigs = (sigs_resp or {}).get("result") or []
        program_str = str(PUMP_AMM_PROGRAM_ID)

        for s in sigs:
            if pool.buy_template and pool.sell_template:
                break
            if s.get("err"):
                continue
            tx_resp = await self.client.post_rpc(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getTransaction",
                    "params": [
                        s["signature"],
                        {
                            "encoding": "jsonParsed",
                            "maxSupportedTransactionVersion": 0,
                        },
                    ],
                }
            )
            tx = (tx_resp or {}).get("result")
            if not tx or (tx.get("meta") or {}).get("err"):
                continue
            message = tx["transaction"]["message"]
            flags = {
                k["pubkey"]: (bool(k.get("signer")), bool(k.get("writable")))
                for k in message.get("accountKeys", [])
            }
            all_ix = list(message.get("instructions", []))
            for inner in (tx.get("meta") or {}).get("innerInstructions") or []:
                all_ix.extend(inner.get("instructions", []))

            import base58

            for ix in all_ix:
                if ix.get("programId") != program_str:
                    continue
                if "data" not in ix or "accounts" not in ix:
                    continue
                try:
                    raw = base58.b58decode(ix["data"])
                except ValueError:
                    continue
                if len(raw) < 8 or len(ix["accounts"]) < 9:
                    continue
                disc = raw[:8]
                template = SwapTemplate(
                    user=ix["accounts"][1],
                    accounts=[
                        (a, *flags.get(a, (False, False))) for a in ix["accounts"]
                    ],
                    data_len=len(raw),
                )
                if disc == BUY_DISCRIMINATOR and pool.buy_template is None:
                    pool.buy_template = template
                elif disc == SELL_DISCRIMINATOR and pool.sell_template is None:
                    pool.sell_template = template

        # Derive a missing side from the other: the program `buy` layout is
        # the `sell` layout with (global, user) volume accumulators inserted
        # before fee_config (observed on-chain: sell=23 accts, buy=25).
        if pool.sell_template and not pool.buy_template:
            t = pool.sell_template
            user_pk = Pubkey.from_string(t.user)
            gva = str(self._find_global_volume_accumulator())
            uva = str(self._find_user_volume_accumulator(user_pk))
            accounts = (
                t.accounts[:19]
                + [(gva, False, False), (uva, False, True)]
                + t.accounts[19:]
            )
            pool.buy_template = SwapTemplate(
                user=t.user, accounts=accounts, data_len=25
            )
        if pool.buy_template and not pool.sell_template:
            t = pool.buy_template
            accounts = t.accounts[:19] + t.accounts[21:]
            pool.sell_template = SwapTemplate(
                user=t.user, accounts=accounts, data_len=24
            )

        if not (pool.buy_template and pool.sell_template):
            raise ValueError(
                "Inverted pool has no recent successful swaps to learn the "
                "account layout from — cannot trade it safely"
            )
        logger.info(
            f"Learned inverted-pool swap templates: "
            f"buy={len(pool.buy_template.accounts)} accts "
            f"(data {pool.buy_template.data_len}B), "
            f"sell={len(pool.sell_template.accounts)} accts "
            f"(data {pool.sell_template.data_len}B)"
        )

    def _accounts_from_template(
        self, template: SwapTemplate, user: Pubkey, pool: PumpSwapPool
    ) -> list[AccountMeta]:
        """Rebuild a learned account list for our user.

        Every account derived from the template signer is remapped to the
        equivalent account for ``user``; all pool-level accounts are copied
        verbatim.
        """
        t_user = Pubkey.from_string(template.user)
        wsol_prog = SystemAddresses.TOKEN_PROGRAM
        remap: dict[str, Pubkey] = {template.user: user}
        remap[str(get_associated_token_address(t_user, SOL, wsol_prog))] = (
            get_associated_token_address(user, SOL, wsol_prog)
        )
        remap[
            str(
                get_associated_token_address(
                    t_user, pool.token_mint, pool.token_program_id
                )
            )
        ] = get_associated_token_address(user, pool.token_mint, pool.token_program_id)
        t_uva = self._find_user_volume_accumulator(t_user)
        u_uva = self._find_user_volume_accumulator(user)
        remap[str(t_uva)] = u_uva
        remap[str(get_associated_token_address(t_uva, SOL, wsol_prog))] = (
            get_associated_token_address(u_uva, SOL, wsol_prog)
        )
        remap[
            str(
                get_associated_token_address(
                    t_uva, pool.token_mint, pool.token_program_id
                )
            )
        ] = get_associated_token_address(u_uva, pool.token_mint, pool.token_program_id)

        metas: list[AccountMeta] = []
        for i, (pk_str, _signer, writable) in enumerate(template.accounts):
            mapped = remap.get(pk_str)
            pubkey = mapped if mapped is not None else Pubkey.from_string(pk_str)
            metas.append(
                AccountMeta(
                    pubkey=pubkey,
                    is_signer=(i == 1),
                    is_writable=True if mapped is not None else writable,
                )
            )
        return metas

    async def _buy_inverted(
        self,
        pool: PumpSwapPool,
        payer: Keypair,
        sol_amount: float,
        slippage: float,
        priority_fee: int,
        compute_units: int,
    ) -> SwapResult:
        """Buy the token on a SOL-base pool (program ``sell``: base SOL in)."""
        try:
            price = await self.calculate_price(pool)
            if price <= 0:
                return SwapResult(status="failed", error="Invalid pool price")

            base_in = int(sol_amount * LAMPORTS_PER_SOL)
            expected_tokens = sol_amount / price
            min_quote_out = int(expected_tokens * (1 - slippage) * 10**TOKEN_DECIMALS)
            template = pool.sell_template
            if template is None:
                return SwapResult(status="failed", error="No sell template")

            accounts = self._accounts_from_template(template, payer.pubkey(), pool)
            data = SELL_DISCRIMINATOR + struct.pack("<QQ", base_in, min_quote_out)
            user_wsol = get_associated_token_address(
                payer.pubkey(), SOL, SystemAddresses.TOKEN_PROGRAM
            )
            instructions = [
                create_idempotent_associated_token_account(
                    payer.pubkey(), payer.pubkey(), SOL, SystemAddresses.TOKEN_PROGRAM
                ),
                transfer(
                    TransferParams(
                        from_pubkey=payer.pubkey(),
                        to_pubkey=user_wsol,
                        lamports=base_in,
                    )
                ),
                sync_native(SyncNativeParams(SystemAddresses.TOKEN_PROGRAM, user_wsol)),
                create_idempotent_associated_token_account(
                    payer.pubkey(),
                    payer.pubkey(),
                    pool.token_mint,
                    pool.token_program_id,
                ),
                Instruction(PUMP_AMM_PROGRAM_ID, data, accounts),
                close_account(
                    CloseAccountParams(
                        program_id=SystemAddresses.TOKEN_PROGRAM,
                        account=user_wsol,
                        dest=payer.pubkey(),
                        owner=payer.pubkey(),
                    )
                ),
            ]
            sig = await self._send(
                instructions,
                payer,
                priority_fee=priority_fee,
                compute_units=compute_units,
            )
            if not sig:
                return SwapResult(status="failed", error="Send failed", price=price)
            return await self._finalize_swap(
                sig, payer.pubkey(), pool.token_mint, side="buy", ref_price=price
            )
        except Exception as e:
            logger.exception("PumpSwap inverted buy failed")
            return SwapResult(status="failed", error=str(e))

    async def _sell_inverted(
        self,
        pool: PumpSwapPool,
        payer: Keypair,
        token_amount: float,
        price: float,
        slippage: float,
        priority_fee: int,
        compute_units: int,
    ) -> SwapResult:
        """Sell the token on a SOL-base pool (program ``buy``: base SOL out).

        The program spends only the tokens needed to output the requested
        SOL, so up to ``slippage`` of the tokens may remain after a fill —
        the caller reconciles from the on-chain balance and re-sells the
        remainder (converges in a couple of iterations).
        """
        try:
            if token_amount <= 0:
                return SwapResult(status="failed", error="No tokens to sell")
            if price <= 0:
                price = await self.calculate_price(pool)

            user_token = get_associated_token_address(
                payer.pubkey(), pool.token_mint, pool.token_program_id
            )
            token_raw = int(token_amount * 10**TOKEN_DECIMALS)
            balance_raw = await self._safe_token_balance(user_token)
            if balance_raw <= 0:
                return SwapResult(status="failed", error="No token balance on-chain")
            if token_raw > balance_raw or balance_raw - token_raw < 10**TOKEN_DECIMALS:
                token_raw = balance_raw

            expected_sol = (token_raw / 10**TOKEN_DECIMALS) * price
            base_out = int(expected_sol * (1 - slippage) * LAMPORTS_PER_SOL)
            if base_out <= 0:
                return SwapResult(status="failed", error="Sell too small")

            template = pool.buy_template
            if template is None:
                return SwapResult(status="failed", error="No buy template")
            accounts = self._accounts_from_template(template, payer.pubkey(), pool)
            data = BUY_DISCRIMINATOR + struct.pack("<QQ", base_out, token_raw)
            if template.data_len >= 25:
                data += struct.pack("<B", 1)  # track_volume

            user_wsol = get_associated_token_address(
                payer.pubkey(), SOL, SystemAddresses.TOKEN_PROGRAM
            )
            instructions = [
                create_idempotent_associated_token_account(
                    payer.pubkey(), payer.pubkey(), SOL, SystemAddresses.TOKEN_PROGRAM
                ),
                Instruction(PUMP_AMM_PROGRAM_ID, data, accounts),
                close_account(
                    CloseAccountParams(
                        program_id=SystemAddresses.TOKEN_PROGRAM,
                        account=user_wsol,
                        dest=payer.pubkey(),
                        owner=payer.pubkey(),
                    )
                ),
            ]
            sig = await self._send(
                instructions,
                payer,
                priority_fee=priority_fee,
                compute_units=compute_units,
            )
            if not sig:
                return SwapResult(status="failed", error="Send failed", price=price)
            return await self._finalize_swap(
                sig, payer.pubkey(), pool.token_mint, side="sell", ref_price=price
            )
        except Exception as e:
            logger.exception("PumpSwap inverted sell failed")
            return SwapResult(status="failed", error=str(e), price=price)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _send(
        self,
        instructions: list[Instruction],
        payer: Keypair,
        priority_fee: int = 200_000,
        compute_units: int = DEFAULT_CU_BUY,
        max_attempts: int = 4,
    ) -> str | None:
        """Sign once with a fresh blockhash, then send (resending is idempotent).

        The transaction is signed exactly once — retries resubmit the SAME
        signature, so a retry can never execute the swap twice. (The previous
        version re-signed with a new blockhash per attempt, which could
        double-send if the first submit landed but its response was lost.)

        Note: Helius returns BlockhashNotFound on simulate_transaction even for
        fresh confirmed blockhashes (reproduced with a trivial transfer), so
        preflight is skipped; execution success is verified after landing.
        """
        rpc = await self.client.get_client()

        fee_ixs = [
            set_compute_unit_limit(compute_units),
            set_compute_unit_price(priority_fee),
        ]
        full_ixs = fee_ixs + list(instructions)

        try:
            await self.client._rate_limiter.acquire()  # noqa: SLF001
            bh_resp = await rpc.get_latest_blockhash(commitment="confirmed")
            blockhash = bh_resp.value.blockhash
        except Exception as e:
            logger.error(f"PumpSwap: could not fetch blockhash: {e}")
            return None

        message = Message(full_ixs, payer.pubkey())
        tx = Transaction([payer], message, blockhash)

        last_err: str | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                await self.client._rate_limiter.acquire()  # noqa: SLF001
                resp = await rpc.send_transaction(
                    tx,
                    opts=TxOpts(
                        skip_preflight=True,
                        preflight_commitment="confirmed",
                    ),
                )
                sig = str(resp.value)
                logger.info(f"PumpSwap tx submitted: {sig}")
                return sig
            except Exception as e:
                last_err = str(e)
                if attempt < max_attempts:
                    logger.warning(f"PumpSwap send retry {attempt}/{max_attempts}: {e}")
                    await asyncio.sleep(0.5 * attempt)

        logger.error(f"PumpSwap send failed after retries: {last_err}")
        return None

    async def _finalize_swap(
        self,
        sig: str,
        wallet: Pubkey,
        mint: Pubkey,
        *,
        side: str,
        ref_price: float,
    ) -> SwapResult:
        """Confirm a submitted swap and parse ACTUAL fills from the chain.

        Never guesses: ``ok`` results carry real balance deltas; timeouts are
        reported as ``unknown`` so the caller can reconcile wallet state.
        """
        status = await self.client.confirm_transaction_status(sig)
        if status == "failed":
            return SwapResult(
                status="failed",
                signature=sig,
                price=ref_price,
                error="Transaction landed but program failed",
            )
        if status == "unknown":
            return SwapResult(
                status="unknown",
                signature=sig,
                price=ref_price,
                error="Confirmation timed out",
            )

        sol_delta, token_delta = await self._wallet_deltas(sig, wallet, mint)
        if token_delta is None:
            # Landed OK but we could not parse the tx — report unknown so the
            # caller reconciles from wallet balances rather than guessing.
            return SwapResult(
                status="unknown",
                signature=sig,
                price=ref_price,
                error="Landed but fill parse failed",
            )

        if side == "buy":
            tokens = token_delta
            sol_spent = -(sol_delta or 0.0)
            eff_price = sol_spent / tokens if tokens > 0 else ref_price
            logger.info(
                f"PumpSwap buy OK: {tokens:.4f} tokens for {sol_spent:.6f} SOL "
                f"(eff price {eff_price:.12f}, tx={sig})"
            )
            return SwapResult(
                status="ok",
                signature=sig,
                tokens=tokens,
                sol=sol_spent,
                price=eff_price,
            )

        tokens_sold = -token_delta
        sol_recv = sol_delta or 0.0
        eff_price = sol_recv / tokens_sold if tokens_sold > 0 else ref_price
        logger.info(
            f"PumpSwap sell OK: {tokens_sold:.4f} tokens for {sol_recv:.6f} SOL "
            f"(eff price {eff_price:.12f}, tx={sig})"
        )
        return SwapResult(
            status="ok",
            signature=sig,
            tokens=tokens_sold,
            sol=sol_recv,
            price=eff_price,
        )

    async def _wallet_deltas(
        self, sig: str, wallet: Pubkey, mint: Pubkey
    ) -> tuple[float | None, float | None]:
        """Return (native SOL delta, token delta) for the wallet in a tx.

        SOL delta includes tx/priority fees and the WSOL wrap/unwrap round
        trip, so for a buy it is the true all-in cost.
        """
        wallet_str = str(wallet)
        mint_str = str(mint)
        for attempt in range(5):
            result = await self.client._get_transaction_result(sig)  # noqa: SLF001
            if result:
                break
            await asyncio.sleep(1.0 + attempt)
        else:
            return None, None

        meta = result.get("meta") or {}
        message = (result.get("transaction") or {}).get("message") or {}
        account_keys = message.get("accountKeys") or []

        sol_delta = None
        for i, key in enumerate(account_keys):
            key_str = key if isinstance(key, str) else key.get("pubkey", "")
            if key_str == wallet_str:
                pre = meta.get("preBalances") or []
                post = meta.get("postBalances") or []
                if i < len(pre) and i < len(post):
                    sol_delta = (post[i] - pre[i]) / LAMPORTS_PER_SOL
                break

        def _token_amount(balances: list[dict]) -> float:
            for b in balances:
                if b.get("owner") == wallet_str and b.get("mint") == mint_str:
                    return float(b.get("uiTokenAmount", {}).get("uiAmount") or 0.0)
            return 0.0

        pre_tok = _token_amount(meta.get("preTokenBalances") or [])
        post_tok = _token_amount(meta.get("postTokenBalances") or [])
        return sol_delta, post_tok - pre_tok

    async def _safe_token_balance(self, token_account: Pubkey) -> int:
        """Raw token balance, 0 if the account doesn't exist."""
        try:
            return await self.client.get_token_account_balance(token_account)
        except Exception:
            return 0

    def _buy_accounts(
        self,
        pool: PumpSwapPool,
        user: Pubkey,
        user_base: Pubkey,
        user_quote: Pubkey,
        fee_recipient: Pubkey,
        fee_recipient_ata: Pubkey,
    ) -> list[AccountMeta]:
        """Build buy account metas (post 2026-04-28 layout)."""
        global_vol = self._find_global_volume_accumulator()
        user_vol = self._find_user_volume_accumulator(user)
        user_vol_quote_ata = get_associated_token_address(
            user_vol, SOL, SystemAddresses.TOKEN_PROGRAM
        )

        accounts = [
            AccountMeta(pubkey=pool.market, is_signer=False, is_writable=True),
            AccountMeta(pubkey=user, is_signer=True, is_writable=True),
            AccountMeta(
                pubkey=PUMP_SWAP_GLOBAL_CONFIG, is_signer=False, is_writable=False
            ),
            AccountMeta(pubkey=pool.base_mint, is_signer=False, is_writable=False),
            AccountMeta(pubkey=SOL, is_signer=False, is_writable=False),
            AccountMeta(pubkey=user_base, is_signer=False, is_writable=True),
            AccountMeta(pubkey=user_quote, is_signer=False, is_writable=True),
            AccountMeta(
                pubkey=pool.pool_base_token_account, is_signer=False, is_writable=True
            ),
            AccountMeta(
                pubkey=pool.pool_quote_token_account, is_signer=False, is_writable=True
            ),
            AccountMeta(pubkey=fee_recipient, is_signer=False, is_writable=False),
            AccountMeta(pubkey=fee_recipient_ata, is_signer=False, is_writable=True),
            AccountMeta(
                pubkey=pool.token_program_id, is_signer=False, is_writable=False
            ),
            AccountMeta(
                pubkey=SystemAddresses.TOKEN_PROGRAM, is_signer=False, is_writable=False
            ),
            AccountMeta(
                pubkey=SystemAddresses.SYSTEM_PROGRAM,
                is_signer=False,
                is_writable=False,
            ),
            AccountMeta(
                pubkey=SystemAddresses.ASSOCIATED_TOKEN_PROGRAM,
                is_signer=False,
                is_writable=False,
            ),
            AccountMeta(
                pubkey=PUMP_SWAP_EVENT_AUTHORITY, is_signer=False, is_writable=False
            ),
            AccountMeta(pubkey=PUMP_AMM_PROGRAM_ID, is_signer=False, is_writable=False),
            AccountMeta(
                pubkey=pool.coin_creator_vault_ata, is_signer=False, is_writable=True
            ),
            AccountMeta(
                pubkey=pool.coin_creator_vault_authority,
                is_signer=False,
                is_writable=False,
            ),
            AccountMeta(pubkey=global_vol, is_signer=False, is_writable=False),
            AccountMeta(pubkey=user_vol, is_signer=False, is_writable=True),
            AccountMeta(
                pubkey=self._find_fee_config(), is_signer=False, is_writable=False
            ),
            AccountMeta(pubkey=PUMP_FEE_PROGRAM, is_signer=False, is_writable=False),
        ]
        if pool.is_cashback_coin:
            accounts.append(
                AccountMeta(
                    pubkey=user_vol_quote_ata, is_signer=False, is_writable=True
                )
            )
        accounts.append(
            AccountMeta(
                pubkey=self._find_pool_v2(pool.base_mint),
                is_signer=False,
                is_writable=False,
            )
        )
        breaking = random.choice(BREAKING_FEE_RECIPIENTS)
        breaking_ata = get_associated_token_address(
            breaking, SOL, SystemAddresses.TOKEN_PROGRAM
        )
        accounts.extend(
            [
                AccountMeta(pubkey=breaking, is_signer=False, is_writable=False),
                AccountMeta(pubkey=breaking_ata, is_signer=False, is_writable=True),
            ]
        )
        return accounts

    def _sell_accounts(
        self,
        pool: PumpSwapPool,
        user: Pubkey,
        user_base: Pubkey,
        user_quote: Pubkey,
        fee_recipient: Pubkey,
        fee_recipient_ata: Pubkey,
    ) -> list[AccountMeta]:
        """Build sell account metas (post 2026-04-28 layout)."""
        user_vol = self._find_user_volume_accumulator(user)
        user_vol_quote_ata = get_associated_token_address(
            user_vol, SOL, SystemAddresses.TOKEN_PROGRAM
        )

        accounts = [
            AccountMeta(pubkey=pool.market, is_signer=False, is_writable=True),
            AccountMeta(pubkey=user, is_signer=True, is_writable=True),
            AccountMeta(
                pubkey=PUMP_SWAP_GLOBAL_CONFIG, is_signer=False, is_writable=False
            ),
            AccountMeta(pubkey=pool.base_mint, is_signer=False, is_writable=False),
            AccountMeta(pubkey=SOL, is_signer=False, is_writable=False),
            AccountMeta(pubkey=user_base, is_signer=False, is_writable=True),
            AccountMeta(pubkey=user_quote, is_signer=False, is_writable=True),
            AccountMeta(
                pubkey=pool.pool_base_token_account, is_signer=False, is_writable=True
            ),
            AccountMeta(
                pubkey=pool.pool_quote_token_account, is_signer=False, is_writable=True
            ),
            AccountMeta(pubkey=fee_recipient, is_signer=False, is_writable=False),
            AccountMeta(pubkey=fee_recipient_ata, is_signer=False, is_writable=True),
            AccountMeta(
                pubkey=pool.token_program_id, is_signer=False, is_writable=False
            ),
            AccountMeta(
                pubkey=SystemAddresses.TOKEN_PROGRAM, is_signer=False, is_writable=False
            ),
            AccountMeta(
                pubkey=SystemAddresses.SYSTEM_PROGRAM,
                is_signer=False,
                is_writable=False,
            ),
            AccountMeta(
                pubkey=SystemAddresses.ASSOCIATED_TOKEN_PROGRAM,
                is_signer=False,
                is_writable=False,
            ),
            AccountMeta(
                pubkey=PUMP_SWAP_EVENT_AUTHORITY, is_signer=False, is_writable=False
            ),
            AccountMeta(pubkey=PUMP_AMM_PROGRAM_ID, is_signer=False, is_writable=False),
            AccountMeta(
                pubkey=pool.coin_creator_vault_ata, is_signer=False, is_writable=True
            ),
            AccountMeta(
                pubkey=pool.coin_creator_vault_authority,
                is_signer=False,
                is_writable=False,
            ),
            AccountMeta(
                pubkey=self._find_fee_config(), is_signer=False, is_writable=False
            ),
            AccountMeta(pubkey=PUMP_FEE_PROGRAM, is_signer=False, is_writable=False),
        ]
        if pool.is_cashback_coin:
            accounts.extend(
                [
                    AccountMeta(
                        pubkey=user_vol_quote_ata, is_signer=False, is_writable=True
                    ),
                    AccountMeta(pubkey=user_vol, is_signer=False, is_writable=True),
                ]
            )
        accounts.append(
            AccountMeta(
                pubkey=self._find_pool_v2(pool.base_mint),
                is_signer=False,
                is_writable=False,
            )
        )
        breaking = random.choice(BREAKING_FEE_RECIPIENTS)
        breaking_ata = get_associated_token_address(
            breaking, SOL, SystemAddresses.TOKEN_PROGRAM
        )
        accounts.extend(
            [
                AccountMeta(pubkey=breaking, is_signer=False, is_writable=False),
                AccountMeta(pubkey=breaking_ata, is_signer=False, is_writable=True),
            ]
        )
        return accounts

    async def _fee_recipients(self, pool: PumpSwapPool) -> tuple[Pubkey, Pubkey]:
        """Return (fee_recipient, fee_recipient_wsol_ata)."""
        if pool.is_mayhem_mode:
            account = await self.client.get_account_info(PUMP_SWAP_GLOBAL_CONFIG)
            data = account.data
            if isinstance(data, tuple):
                data = data[0]
            data = bytes(data)
            fee_recipient = Pubkey.from_bytes(
                data[
                    GLOBALCONFIG_RESERVED_FEE_OFFSET : GLOBALCONFIG_RESERVED_FEE_OFFSET
                    + 32
                ]
            )
        else:
            fee_recipient = STANDARD_FEE_RECIPIENT

        ata = get_associated_token_address(
            fee_recipient, SOL, SystemAddresses.TOKEN_PROGRAM
        )
        return fee_recipient, ata

    async def _get_token_program_id(self, mint: Pubkey) -> Pubkey:
        """Return Token or Token-2022 program owning the mint."""
        account = await self.client.get_account_info(mint)
        owner = account.owner
        if owner == SystemAddresses.TOKEN_PROGRAM:
            return SystemAddresses.TOKEN_PROGRAM
        if owner == SystemAddresses.TOKEN_2022_PROGRAM:
            return SystemAddresses.TOKEN_2022_PROGRAM
        raise ValueError(f"Unknown mint owner: {owner}")

    @staticmethod
    def _parse_pool_data(data: bytes) -> dict[str, str | int]:
        """Parse PumpSwap pool account fields."""
        import base58

        offset = 8
        fields = [
            ("pool_bump", "u8"),
            ("index", "u16"),
            ("creator", "pubkey"),
            ("base_mint", "pubkey"),
            ("quote_mint", "pubkey"),
            ("lp_mint", "pubkey"),
            ("pool_base_token_account", "pubkey"),
            ("pool_quote_token_account", "pubkey"),
            ("lp_supply", "u64"),
            ("coin_creator", "pubkey"),
        ]
        parsed: dict[str, str | int] = {}
        for name, ftype in fields:
            if ftype == "pubkey":
                parsed[name] = base58.b58encode(data[offset : offset + 32]).decode()
                offset += 32
            elif ftype == "u64":
                parsed[name] = struct.unpack_from("<Q", data, offset)[0]
                offset += 8
            elif ftype == "u16":
                parsed[name] = struct.unpack_from("<H", data, offset)[0]
                offset += 2
            elif ftype == "u8":
                parsed[name] = data[offset]
                offset += 1
        return parsed

    @staticmethod
    def _find_coin_creator_vault(coin_creator: Pubkey) -> Pubkey:
        derived, _ = Pubkey.find_program_address(
            [b"creator_vault", bytes(coin_creator)], PUMP_AMM_PROGRAM_ID
        )
        return derived

    @staticmethod
    def _find_global_volume_accumulator() -> Pubkey:
        derived, _ = Pubkey.find_program_address(
            [b"global_volume_accumulator"], PUMP_AMM_PROGRAM_ID
        )
        return derived

    @staticmethod
    def _find_user_volume_accumulator(user: Pubkey) -> Pubkey:
        derived, _ = Pubkey.find_program_address(
            [b"user_volume_accumulator", bytes(user)], PUMP_AMM_PROGRAM_ID
        )
        return derived

    @staticmethod
    def _find_fee_config() -> Pubkey:
        derived, _ = Pubkey.find_program_address(
            [b"fee_config", bytes(PUMP_AMM_PROGRAM_ID)], PUMP_FEE_PROGRAM
        )
        return derived

    @staticmethod
    def _find_pool_v2(base_mint: Pubkey) -> Pubkey:
        derived, _ = Pubkey.find_program_address(
            [b"pool-v2", bytes(base_mint)], PUMP_AMM_PROGRAM_ID
        )
        return derived
