"""Poll PumpSwap pool signatures for buy/sell volume (HTTP fallback to WSS)."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from time import monotonic, time
from typing import Any

import aiohttp
from solders.pubkey import Pubkey

from platforms.pumpswap.events import PumpSwapSwap, parse_pumpswap_swap_from_logs
from trading.volume_window import TradeTick
from utils.logger import get_logger

logger = get_logger(__name__)

TradeCallback = Callable[[TradeTick, PumpSwapSwap], Awaitable[None] | None]

HTTP_TOO_MANY_REQUESTS = 429


class PoolVolumePoller:
    """Poll getSignaturesForAddress + getTransaction for pool swaps.

    More reliable than logsSubscribe when the pool is quiet or the WSS
    mentions filter misses PumpSwap account layouts.
    """

    def __init__(
        self,
        rpc_endpoint: str,
        pool: Pubkey,
        poll_interval: float = 2.5,
        batch_limit: int = 25,
        token_is_base: bool = True,
    ) -> None:
        self.rpc_endpoint = rpc_endpoint
        self.pool = pool
        self.pool_str = str(pool)
        self.token_is_base = token_is_base
        self.poll_interval = poll_interval
        self.batch_limit = batch_limit
        # Insertion-ordered so trimming drops the OLDEST signatures, not
        # random ones (a plain set() trim could re-process recent txs and
        # double-count volume).
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._running = False
        self._session: aiohttp.ClientSession | None = None

    async def listen(
        self,
        on_trade: TradeCallback,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        """Poll until stopped."""
        self._running = True
        logger.info(
            f"PoolVolumePoller starting for pool={self.pool_str} "
            f"interval={self.poll_interval}s"
        )
        try:
            # First poll: backfill recent successful swaps into the volume
            # window so entry logic has real market context immediately.
            try:
                n = await self._poll_once(on_trade, first_backfill=True)
                logger.info(f"Backfilled {n} recent pool trade(s) into volume window")
            except Exception:
                logger.exception("Failed initial volume backfill")

            while self._running and (stop_event is None or not stop_event.is_set()):
                try:
                    await self._poll_once(on_trade, first_backfill=False)
                except asyncio.CancelledError:
                    break
                except Exception:
                    logger.exception("PoolVolumePoller poll error")
                try:
                    await asyncio.sleep(self.poll_interval)
                except asyncio.CancelledError:
                    break
        finally:
            self._running = False
            if self._session and not self._session.closed:
                await self._session.close()
            logger.info("PoolVolumePoller stopped")

    def stop(self) -> None:
        self._running = False

    async def _poll_once(
        self, on_trade: TradeCallback, *, first_backfill: bool = False
    ) -> int:
        """Fetch new signatures and dispatch trades. Returns trade event count."""
        sigs = await self._get_signatures()
        # Process oldest-first so volume window order is natural
        new_items = [
            s
            for s in reversed(sigs)
            if s.get("signature")
            and s["signature"] not in self._seen
            and not s.get("err")
        ]
        trades = 0
        for item in new_items:
            sig = item["signature"]
            self._seen[sig] = None
            while len(self._seen) > 2000:
                self._seen.popitem(last=False)

            try:
                logs = await self._get_tx_logs(sig)
            except Exception:
                logger.debug(f"Could not fetch tx {sig[:12]}…")
                continue
            if not logs:
                continue

            # Anchor the tick to the trade's real block time so old trades
            # age out of the volume window correctly (backfill previously
            # stamped them "now", faking fresh volume).
            block_time = item.get("blockTime")
            if block_time:
                age = max(0.0, time() - float(block_time))
                tick_ts = monotonic() - age
            else:
                tick_ts = monotonic()

            for swap in parse_pumpswap_swap_from_logs(
                logs, token_is_base=self.token_is_base
            ):
                if swap.pool != self.pool_str:
                    continue
                tick = TradeTick(
                    timestamp=tick_ts,
                    is_buy=swap.is_buy,
                    sol_amount=swap.sol_amount,
                    token_amount=swap.token_amount,
                    price=swap.price_per_token,
                    signature=sig,
                    user=swap.user,
                )
                side = "BUY" if tick.is_buy else "SELL"
                if first_backfill:
                    logger.info(
                        f"Backfill {side} {tick.sol_amount:.4f} SOL "
                        f"age={time() - float(block_time or time()):.0f}s "
                        f"({sig[:12]}…)"
                    )
                try:
                    result = on_trade(tick, swap)
                    if asyncio.iscoroutine(result):
                        await result
                    trades += 1
                except Exception:
                    logger.exception("Error in poller trade callback")
        return trades

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20)
            )
        return self._session

    async def _rpc(self, method: str, params: list[Any]) -> Any:
        session = await self._get_session()
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        for attempt in range(5):
            async with session.post(self.rpc_endpoint, json=body) as resp:
                if resp.status == HTTP_TOO_MANY_REQUESTS:
                    wait = min(2.0 * (attempt + 1), 10.0)
                    logger.warning(f"Poller rate-limited (429), waiting {wait:.0f}s")
                    await asyncio.sleep(wait)
                    continue
                data = await resp.json()
                if "error" in data:
                    raise RuntimeError(str(data["error"]))
                return data.get("result")
        raise RuntimeError(f"Poller {method}: rate-limited after retries")

    async def _get_signatures(self) -> list[dict[str, Any]]:
        result = await self._rpc(
            "getSignaturesForAddress",
            [self.pool_str, {"limit": self.batch_limit, "commitment": "confirmed"}],
        )
        return result or []

    async def _get_tx_logs(self, signature: str) -> list[str]:
        result = await self._rpc(
            "getTransaction",
            [
                signature,
                {
                    "encoding": "json",
                    "maxSupportedTransactionVersion": 0,
                    "commitment": "confirmed",
                },
            ],
        )
        if not result:
            return []
        meta = result.get("meta") or {}
        if meta.get("err"):
            return []
        return meta.get("logMessages") or []
