"""WebSocket listener for pump.fun trade volume on a single mint."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from time import monotonic

import websockets
from solders.pubkey import Pubkey

from platforms.pumpfun.address_provider import PumpFunAddresses
from trading.trade_event import TradeEvent, parse_trade_events_from_logs
from trading.volume_window import TradeTick
from utils.logger import get_logger

logger = get_logger(__name__)

TradeCallback = Callable[[TradeTick, TradeEvent], Awaitable[None] | None]

RECONNECT_DELAY = 5
PING_INTERVAL = 20


class MintVolumeListener:
    """Listen for TradeEvents on a specific pump.fun mint via logsSubscribe.

    Subscribes to logs mentioning the mint's bonding-curve PDA so we only
    receive activity for that curve, then filters TradeEvents by mint address.
    """

    def __init__(
        self,
        wss_endpoint: str,
        mint: Pubkey,
        bonding_curve: Pubkey | None = None,
    ) -> None:
        """Initialize the mint volume listener.

        Args:
            wss_endpoint: Solana WebSocket RPC endpoint.
            mint: Target token mint.
            bonding_curve: Optional pre-derived bonding curve. Derived if omitted.
        """
        self.wss_endpoint = wss_endpoint
        self.mint = mint
        self.mint_str = str(mint)
        if bonding_curve is None:
            bonding_curve, _ = Pubkey.find_program_address(
                [b"bonding-curve", bytes(mint)],
                PumpFunAddresses.PROGRAM,
            )
        self.bonding_curve = bonding_curve
        self.bonding_curve_str = str(bonding_curve)
        self._running = False

    async def listen(
        self,
        on_trade: TradeCallback,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        """Connect and stream trades until stopped.

        Args:
            on_trade: Callback invoked for each matching TradeEvent.
            stop_event: Optional event that, when set, stops the listener.
        """
        self._running = True
        logger.info(
            f"MintVolumeListener starting for mint={self.mint_str} "
            f"curve={self.bonding_curve_str}"
        )

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
                logger.info("MintVolumeListener cancelled")
                break
            except Exception:
                if not self._running:
                    break
                logger.exception(
                    f"MintVolumeListener connection error; "
                    f"reconnecting in {RECONNECT_DELAY}s"
                )
                await asyncio.sleep(RECONNECT_DELAY)

        self._running = False
        logger.info("MintVolumeListener stopped")

    def stop(self) -> None:
        """Request the listener to stop after the current iteration."""
        self._running = False

    async def _subscribe(self, websocket: websockets.ClientConnection) -> None:
        """Subscribe to logs mentioning the bonding curve."""
        msg = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "logsSubscribe",
            "params": [
                {"mentions": [self.bonding_curve_str]},
                {"commitment": "processed"},
            ],
        }
        await websocket.send(json.dumps(msg))
        response = json.loads(await websocket.recv())
        if "result" in response:
            logger.info(
                f"Subscribed to bonding-curve logs (sub_id={response['result']})"
            )
        else:
            logger.warning(f"Unexpected subscription response: {response}")

    async def _keep_alive(self, websocket: websockets.ClientConnection) -> None:
        """Send periodic pings to keep the WebSocket open."""
        try:
            while True:
                await asyncio.sleep(PING_INTERVAL)
                pong = await websocket.ping()
                await asyncio.wait_for(pong, timeout=10)
        except (asyncio.CancelledError, TimeoutError, websockets.ConnectionClosed):
            return

    async def _dispatch_events(
        self,
        logs: list[str],
        signature: str,
        on_trade: TradeCallback,
    ) -> None:
        """Parse logs and invoke callback for matching mint trades."""
        for event in parse_trade_events_from_logs(logs):
            if event.mint != self.mint_str:
                continue
            tick = TradeTick(
                timestamp=monotonic(),
                is_buy=event.is_buy,
                sol_amount=event.sol_amount,
                token_amount=event.token_amount,
                price=event.price_per_token,
                signature=signature,
                user=event.user,
            )
            try:
                result_cb = on_trade(tick, event)
                if asyncio.iscoroutine(result_cb):
                    await result_cb
            except Exception:
                logger.exception("Error in trade callback")

    async def _read_loop(
        self,
        websocket: websockets.ClientConnection,
        on_trade: TradeCallback,
        stop_event: asyncio.Event | None,
    ) -> None:
        """Read notifications and dispatch matching trades."""
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

            await self._dispatch_events(
                value.get("logs") or [],
                value.get("signature", ""),
                on_trade,
            )
