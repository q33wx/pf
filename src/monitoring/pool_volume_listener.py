"""WebSocket listener for PumpSwap pool buy/sell volume."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from time import monotonic

import websockets
from solders.pubkey import Pubkey

from platforms.pumpswap.events import PumpSwapSwap, parse_pumpswap_swap_from_logs
from trading.volume_window import TradeTick
from utils.logger import get_logger

logger = get_logger(__name__)

TradeCallback = Callable[[TradeTick, PumpSwapSwap], Awaitable[None] | None]

RECONNECT_DELAY = 5
PING_INTERVAL = 20


class PoolVolumeListener:
    """Listen for PumpSwap BuyEvent/SellEvent on a specific pool via logsSubscribe."""

    def __init__(
        self, wss_endpoint: str, pool: Pubkey, token_is_base: bool = True
    ) -> None:
        """Initialize pool volume listener.

        Args:
            wss_endpoint: Solana WebSocket RPC endpoint.
            pool: PumpSwap market/pool address.
            token_is_base: Pool orientation (False for inverted SOL-base pools).
        """
        self.wss_endpoint = wss_endpoint
        self.pool = pool
        self.pool_str = str(pool)
        self.token_is_base = token_is_base
        self._running = False

    async def listen(
        self,
        on_trade: TradeCallback,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        """Connect and stream pool swaps until stopped."""
        self._running = True
        logger.info(f"PoolVolumeListener starting for pool={self.pool_str}")

        while self._running and (stop_event is None or not stop_event.is_set()):
            try:
                async with websockets.connect(self.wss_endpoint) as websocket:
                    await self._subscribe(websocket)
                    ping_task = asyncio.create_task(self._keep_alive(websocket))
                    try:
                        await self._read_loop(websocket, on_trade, stop_event)
                    finally:
                        ping_task.cancel()
                        try:
                            await ping_task
                        except asyncio.CancelledError:
                            pass
            except asyncio.CancelledError:
                logger.info("PoolVolumeListener cancelled")
                break
            except Exception:
                if not self._running:
                    break
                logger.exception(
                    f"PoolVolumeListener error; reconnecting in {RECONNECT_DELAY}s"
                )
                await asyncio.sleep(RECONNECT_DELAY)

        self._running = False
        logger.info("PoolVolumeListener stopped")

    def stop(self) -> None:
        """Request stop."""
        self._running = False

    async def _subscribe(self, websocket: websockets.ClientConnection) -> None:
        msg = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "logsSubscribe",
            "params": [
                {"mentions": [self.pool_str]},
                {"commitment": "processed"},
            ],
        }
        await websocket.send(json.dumps(msg))
        response = json.loads(await websocket.recv())
        if "result" in response:
            logger.info(f"Subscribed to pool logs (sub_id={response['result']})")
        else:
            logger.warning(f"Unexpected subscription response: {response}")

    async def _keep_alive(self, websocket: websockets.ClientConnection) -> None:
        try:
            while True:
                await asyncio.sleep(PING_INTERVAL)
                pong = await websocket.ping()
                await asyncio.wait_for(pong, timeout=10)
        except (asyncio.CancelledError, TimeoutError, websockets.ConnectionClosed):
            return

    async def _read_loop(
        self,
        websocket: websockets.ClientConnection,
        on_trade: TradeCallback,
        stop_event: asyncio.Event | None,
    ) -> None:
        async for raw in websocket:
            if stop_event is not None and stop_event.is_set():
                break
            if not self._running:
                break
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue

            value = message.get("params", {}).get("result", {}).get("value") or {}
            if not value or value.get("err"):
                continue

            logs = value.get("logs") or []
            signature = value.get("signature", "")
            for swap in parse_pumpswap_swap_from_logs(
                logs, token_is_base=self.token_is_base
            ):
                if swap.pool != self.pool_str:
                    continue
                tick = TradeTick(
                    timestamp=monotonic(),
                    is_buy=swap.is_buy,
                    sol_amount=swap.sol_amount,
                    token_amount=swap.token_amount,
                    price=swap.price_per_token,
                    signature=signature,
                    user=swap.user,
                )
                try:
                    result_cb = on_trade(tick, swap)
                    if asyncio.iscoroutine(result_cb):
                        await result_cb
                except Exception:
                    logger.exception("Error in pool trade callback")
