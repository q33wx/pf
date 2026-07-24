"""Jito block-engine bundle submission.

Sends signed transactions as tipped bundles for priority inclusion,
bypassing the public RPC submission queue. A bundle-of-one with a
competitive tip typically lands in the next block, which is the
difference between sniping a launch and buying someone else's exit.

Usage: build your transaction with ``tip_instruction()`` appended
before signing, then submit the signed transaction via ``send_bundle``.
"""

import base58
import random

import aiohttp
from solders.instruction import Instruction
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import Transaction

from utils.logger import get_logger

logger = get_logger(__name__)

BLOCK_ENGINES = [
    "https://ny.mainnet.block-engine.jito.wtf",
    "https://mainnet.block-engine.jito.wtf",
]
MIN_TIP_LAMPORTS = 1_000
DEFAULT_TIP_LAMPORTS = 100_000  # 0.0001 SOL


class JitoClient:
    """Minimal async client for the Jito block engine bundles API."""

    def __init__(self, tip_lamports: int = DEFAULT_TIP_LAMPORTS) -> None:
        self.tip_lamports = max(tip_lamports, MIN_TIP_LAMPORTS)
        self._tip_accounts: list[str] = []

    async def _rpc(self, method: str, params: list) -> dict:
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        last_error: Exception | None = None
        async with aiohttp.ClientSession() as session:
            for engine in BLOCK_ENGINES:
                try:
                    async with session.post(
                        f"{engine}/api/v1/bundles",
                        json=payload,
                        timeout=aiohttp.ClientTimeout(total=8),
                    ) as resp:
                        body = await resp.json()
                        if "result" in body:
                            return body
                        last_error = RuntimeError(str(body.get("error")))
                except (aiohttp.ClientError, TimeoutError) as exc:
                    last_error = exc
                    logger.warning(f"Jito engine {engine} failed: {exc}")
        raise RuntimeError(f"All Jito engines failed: {last_error}")

    async def tip_accounts(self) -> list[str]:
        """Fetch (and cache) the official tip accounts."""
        if not self._tip_accounts:
            body = await self._rpc("getTipAccounts", [])
            self._tip_accounts = body["result"]
        return self._tip_accounts

    async def tip_instruction(self, payer: Pubkey) -> Instruction:
        """Build the tip transfer to append before signing."""
        accounts = await self.tip_accounts()
        tip_to = Pubkey.from_string(random.choice(accounts))  # noqa: S311
        return transfer(
            TransferParams(
                from_pubkey=payer, to_pubkey=tip_to, lamports=self.tip_lamports
            )
        )

    async def send_bundle(self, signed_txs: list[Transaction]) -> str:
        """Submit signed transactions as one bundle; returns the bundle id."""
        encoded = [base58.b58encode(bytes(tx)).decode() for tx in signed_txs]
        body = await self._rpc("sendBundle", [encoded])
        bundle_id = body["result"]
        logger.info(f"Jito bundle submitted: {bundle_id}")
        return bundle_id
