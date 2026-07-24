"""Single-mint fishing trader: volume-triggered entries + partial scale-outs.

Supports:
- pump.fun bonding curve (pre-migration) via TradeEvents
- PumpSwap AMM (post-migration) via BuyEvent/SellEvent  ← use for $200k+ mcap coins

Strategy:
1. Watch one mint for buy/sell SOL flow.
2. When net buy volume in a rolling window is strong enough → buy a small bag.
3. Scale out little bits at TP ladder levels.
4. Full exit on stop-loss or max hold.
5. Cooldown, then re-enter the next wave.
"""

from __future__ import annotations

import asyncio
import json
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from time import monotonic, time
from typing import Any

from solders.pubkey import Pubkey
from spl.token.instructions import get_associated_token_address

from core.client import SolanaClient
from core.priority_fee.manager import PriorityFeeManager
from core.pubkeys import SystemAddresses
from core.wallet import Wallet
from interfaces.core import Platform, TokenInfo
from monitoring.mint_volume_listener import MintVolumeListener
from monitoring.pool_volume_listener import PoolVolumeListener
from monitoring.pool_volume_poller import PoolVolumePoller
from platforms import get_platform_implementations
from platforms.pumpfun.address_provider import PumpFunAddressProvider
from platforms.pumpswap.client import PumpSwapClient, PumpSwapPool, SwapResult
from trading.platform_aware import PlatformAwareBuyer, PlatformAwareSeller
from trading.volume_window import TradeTick, VolumeWindow
from utils.logger import get_logger

logger = get_logger(__name__)


class FisherState(Enum):
    """High-level fisher state machine."""

    WATCHING = "watching"
    IN_POSITION = "in_position"
    COOLDOWN = "cooldown"
    STOPPED = "stopped"


@dataclass
class ScaleOutLevel:
    """One rung on the take-profit ladder."""

    pnl_pct: float
    sell_pct: float
    hit: bool = False


@dataclass
class FisherPosition:
    """Live fishing bag on the target mint."""

    entry_price: float
    entry_time: float
    original_tokens: float
    remaining_tokens: float
    entry_sol: float
    peak_price: float
    scale_levels: list[ScaleOutLevel] = field(default_factory=list)
    realized_sol: float = 0.0

    def mark_price(self, price: float) -> None:
        """Update peak price tracker."""
        self.peak_price = max(self.peak_price, price)

    def unrealized_pnl_pct(self, price: float) -> float:
        """Return unrealized PnL as a fraction of entry price."""
        if self.entry_price <= 0:
            return 0.0
        return (price - self.entry_price) / self.entry_price


class FishingTrader:
    """Volume-aware single-mint fisher (bonding curve or PumpSwap)."""

    def __init__(
        self,
        rpc_endpoint: str,
        wss_endpoint: str,
        private_key: str,
        mint: str,
        venue: str = "pumpswap",
        buy_amount: float = 0.01,
        buy_slippage: float = 0.25,
        sell_slippage: float = 0.25,
        volume_window_seconds: float = 30.0,
        min_net_buy_sol: float = 0.5,
        min_buy_sell_ratio: float = 1.5,
        min_trades_in_window: int = 3,
        min_price_change_pct: float = 0.0,
        volume_source: str = "poll",  # poll | wss | both
        # entry_mode: volume = wait for net-buy signal | always = buy when flat (after cooldown)
        entry_mode: str = "volume",
        # exit_mode: simple = sell 100% at take-profit | scale_out = ladder
        exit_mode: str = "simple",
        take_profit_percentage: float = 0.10,
        scale_out_levels: list[dict[str, float]] | None = None,
        stop_loss_percentage: float = 0.15,
        max_hold_seconds: float = 300.0,
        price_check_interval: float = 2.0,
        cooldown_seconds: float = 15.0,
        max_cycles: int | None = None,
        dry_run: bool = True,
        enable_dynamic_priority_fee: bool = False,
        enable_fixed_priority_fee: bool = True,
        fixed_priority_fee: int = 200_000,
        extra_priority_fee: float = 0.0,
        hard_cap_prior_fee: int = 200_000,
        max_retries: int = 3,
        compute_units: dict | None = None,
        max_rps: float = 25.0,
        symbol: str | None = None,
        min_wallet_sol_reserve: float = 0.01,
        max_daily_loss_sol: float | None = None,
        max_consecutive_buy_failures: int = 5,
    ):
        """Initialize the fishing trader.

        Args:
            venue: ``pumpswap`` (migrated AMM) or ``bonding_curve`` (pre-migration).
            mint: Target token mint.
            dry_run: Log trades without submitting when True.
        """
        self.solana_client = SolanaClient(rpc_endpoint, max_rps=max_rps)
        self.rpc_endpoint = rpc_endpoint
        self.wallet = Wallet(private_key)
        self.wss_endpoint = wss_endpoint
        self.mint = Pubkey.from_string(mint)
        self.mint_str = str(self.mint)
        self.symbol = symbol or self.mint_str[:8]
        self.venue = venue.lower().strip()
        if self.venue not in {"pumpswap", "bonding_curve"}:
            raise ValueError("venue must be 'pumpswap' or 'bonding_curve'")
        self.volume_source = (volume_source or "poll").lower().strip()
        if self.volume_source not in {"poll", "wss", "both"}:
            raise ValueError("volume_source must be poll, wss, or both")

        self.entry_mode = (entry_mode or "volume").lower().strip()
        if self.entry_mode not in {"volume", "always"}:
            raise ValueError("entry_mode must be 'volume' or 'always'")
        self.exit_mode = (exit_mode or "simple").lower().strip()
        if self.exit_mode not in {"simple", "scale_out"}:
            raise ValueError("exit_mode must be 'simple' or 'scale_out'")
        self.take_profit_percentage = float(take_profit_percentage)

        self.buy_amount = buy_amount
        self.buy_slippage = buy_slippage
        self.sell_slippage = sell_slippage
        self.min_net_buy_sol = min_net_buy_sol
        self.min_buy_sell_ratio = min_buy_sell_ratio
        self.min_trades_in_window = min_trades_in_window
        self.min_price_change_pct = float(min_price_change_pct)
        self.volume_window = VolumeWindow(volume_window_seconds)

        if scale_out_levels is None:
            scale_out_levels = [
                {"pnl_pct": 0.10, "sell_pct": 0.25},
                {"pnl_pct": 0.25, "sell_pct": 0.33},
                {"pnl_pct": 0.50, "sell_pct": 0.50},
            ]
        sorted_levels = sorted(scale_out_levels, key=lambda lv: float(lv["pnl_pct"]))
        self.scale_out_template = [
            ScaleOutLevel(pnl_pct=float(lv["pnl_pct"]), sell_pct=float(lv["sell_pct"]))
            for lv in sorted_levels
        ]
        self.stop_loss_percentage = stop_loss_percentage
        self.max_hold_seconds = max_hold_seconds
        self.price_check_interval = price_check_interval
        self.cooldown_seconds = cooldown_seconds
        self.max_cycles = max_cycles
        self.dry_run = dry_run
        self.fixed_priority_fee = fixed_priority_fee
        self.max_retries = max_retries
        self.compute_units = compute_units or {}

        self.priority_fee_manager = PriorityFeeManager(
            client=self.solana_client,
            enable_dynamic_fee=enable_dynamic_priority_fee,
            enable_fixed_fee=enable_fixed_priority_fee,
            fixed_fee=fixed_priority_fee,
            extra_fee=extra_priority_fee,
            hard_cap=hard_cap_prior_fee,
        )

        # Bonding-curve path components (lazy if pumpswap)
        self.token_info: TokenInfo | None = None
        self.bonding_curve: Pubkey | None = None
        self.buyer: PlatformAwareBuyer | None = None
        self.seller: PlatformAwareSeller | None = None
        self.curve_manager = None
        self.address_provider: PumpFunAddressProvider | None = None

        # PumpSwap path
        self.pumpswap = PumpSwapClient(self.solana_client)
        self.pool: PumpSwapPool | None = None

        if self.venue == "bonding_curve":
            self.platform_impl = get_platform_implementations(
                Platform.PUMP_FUN, self.solana_client
            )
            self.address_provider = self.platform_impl.address_provider  # type: ignore[assignment]
            self.curve_manager = self.platform_impl.curve_manager
            self.buyer = PlatformAwareBuyer(
                self.solana_client,
                self.wallet,
                self.priority_fee_manager,
                buy_amount,
                buy_slippage,
                max_retries,
                extreme_fast_token_amount=0,
                extreme_fast_mode=False,
                compute_units=self.compute_units,
            )
            self.seller = PlatformAwareSeller(
                self.solana_client,
                self.wallet,
                self.priority_fee_manager,
                sell_slippage,
                max_retries,
                compute_units=self.compute_units,
            )

        self.position: FisherPosition | None = None
        self.state = FisherState.WATCHING
        self.cycles_completed = 0
        self._cooldown_until = 0.0
        self._trade_lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._last_volume_log = 0.0
        # Dedupe trades when volume_source='both' (WSS + poller overlap)
        self._seen_ticks: OrderedDict[tuple, None] = OrderedDict()
        self._price_monitor_task: asyncio.Task | None = None
        self._next_entry_allowed_at = 0.0  # rate-limit failed entry spam

        # Safety rails
        self.min_wallet_sol_reserve = min_wallet_sol_reserve
        self.max_daily_loss_sol = max_daily_loss_sol
        self.max_consecutive_buy_failures = max_consecutive_buy_failures
        self._consecutive_buy_failures = 0
        self._sell_failures = 0
        self._exit_failures = 0
        self._price_anomalies = 0
        self._last_good_price = 0.0
        self._daily = {"date": self._utc_date(), "pnl_sol": 0.0}

        trades_dir = Path("trades")
        trades_dir.mkdir(exist_ok=True)
        self._trade_log_path = trades_dir / "fisher_trades.log"
        self._state_path = trades_dir / f"fisher_state_{self.mint_str[:8]}.json"

    # ------------------------------------------------------------------
    # State persistence / reconciliation
    # ------------------------------------------------------------------

    @staticmethod
    def _utc_date() -> str:
        return datetime.now(UTC).strftime("%Y-%m-%d")

    def _save_state(self) -> None:
        """Persist position + daily P&L so restarts never orphan a bag."""
        pos = None
        if self.position is not None:
            p = self.position
            pos = {
                "entry_price": p.entry_price,
                "entry_wall_time": time() - (monotonic() - p.entry_time),
                "original_tokens": p.original_tokens,
                "remaining_tokens": p.remaining_tokens,
                "entry_sol": p.entry_sol,
                "peak_price": p.peak_price,
                "realized_sol": p.realized_sol,
                "levels_hit": [lv.hit for lv in p.scale_levels],
            }
        payload = {
            "mint": self.mint_str,
            "position": pos,
            "daily": self._daily,
            "cycles_completed": self.cycles_completed,
        }
        try:
            self._state_path.write_text(json.dumps(payload, indent=2))
        except OSError:
            logger.exception("Failed to persist fisher state")

    def _load_state(self) -> dict | None:
        try:
            if not self._state_path.exists():
                return None
            data = json.loads(self._state_path.read_text())
            if data.get("mint") != self.mint_str:
                return None
            return data
        except (OSError, ValueError):
            logger.exception("Failed to load fisher state")
            return None

    async def _wallet_token_balance(self) -> float:
        """Wallet's on-chain balance of the target token (UI units)."""
        token_program = (
            self.pool.token_program_id
            if self.pool is not None
            else SystemAddresses.TOKEN_PROGRAM
        )
        ata = get_associated_token_address(self.wallet.pubkey, self.mint, token_program)
        raw = await self.pumpswap._safe_token_balance(ata)  # noqa: SLF001
        return raw / 1e6

    async def _wallet_sol_balance(self) -> float:
        """Wallet's native SOL balance."""
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getBalance",
            "params": [str(self.wallet.pubkey)],
        }
        response = await self.solana_client.post_rpc(body)
        if response and "result" in response:
            return (response["result"].get("value") or 0) / 1e9
        return 0.0

    async def _reconcile_position(self) -> None:
        """Adopt any tokens already in the wallet instead of buying more.

        Covers: bot restarted mid-position, or a previous buy landed after the
        bot gave up on it. On-chain balance is the source of truth.
        """
        saved = self._load_state()
        if saved:
            day = saved.get("daily") or {}
            if day.get("date") == self._utc_date():
                self._daily = {
                    "date": day["date"],
                    "pnl_sol": float(day.get("pnl_sol", 0.0)),
                }
            self.cycles_completed = int(saved.get("cycles_completed", 0))

        balance = await self._wallet_token_balance()
        dust = 1.0  # ignore < 1 token
        if balance <= dust:
            if saved and saved.get("position"):
                logger.warning(
                    "Saved position found but wallet holds no tokens — clearing"
                )
                self._save_state()
            return

        price = await self._get_price()
        pos_data = (saved or {}).get("position")
        if pos_data:
            entry_wall = float(pos_data.get("entry_wall_time", time()))
            entry_price = float(pos_data.get("entry_price", price))
            levels = [
                ScaleOutLevel(pnl_pct=lv.pnl_pct, sell_pct=lv.sell_pct)
                for lv in self.scale_out_template
            ]
            for lv, hit in zip(levels, pos_data.get("levels_hit", []), strict=False):
                lv.hit = bool(hit)
            self.position = FisherPosition(
                entry_price=entry_price,
                entry_time=monotonic() - max(0.0, time() - entry_wall),
                original_tokens=float(pos_data.get("original_tokens", balance)),
                remaining_tokens=balance,
                entry_sol=float(pos_data.get("entry_sol", balance * entry_price)),
                peak_price=max(float(pos_data.get("peak_price", price)), price),
                scale_levels=levels,
                realized_sol=float(pos_data.get("realized_sol", 0.0)),
            )
            logger.warning(
                f"RESTORED position from state file: {balance:.2f} tokens, "
                f"entry {entry_price:.12f} SOL"
            )
        else:
            self.position = FisherPosition(
                entry_price=price,
                entry_time=monotonic(),
                original_tokens=balance,
                remaining_tokens=balance,
                entry_sol=balance * price,
                peak_price=price,
                scale_levels=[
                    ScaleOutLevel(pnl_pct=lv.pnl_pct, sell_pct=lv.sell_pct)
                    for lv in self.scale_out_template
                ],
            )
            logger.warning(
                f"ADOPTED untracked wallet balance as position: {balance:.2f} "
                f"tokens @ current price {price:.12f} SOL"
            )
        self.state = FisherState.IN_POSITION
        self._save_state()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Resolve market accounts, start volume listener + price monitor."""
        mode = "DRY-RUN" if self.dry_run else "LIVE"
        logger.info("=" * 60)
        logger.info(f"FishingTrader starting [{mode}] venue={self.venue}")
        logger.info(f"  mint:              {self.mint_str}")
        logger.info(f"  buy_amount:        {self.buy_amount} SOL")
        logger.info(f"  entry_mode:        {self.entry_mode}")
        logger.info(f"  exit_mode:         {self.exit_mode}")
        if self.entry_mode == "volume":
            logger.info(
                f"  volume_window:     {self.volume_window.window_seconds}s | "
                f"min_net={self.min_net_buy_sol} SOL | "
                f"min_ratio={self.min_buy_sell_ratio} | "
                f"min_trades={self.min_trades_in_window} | "
                f"min_chg={self.min_price_change_pct:+.1%}"
            )
        if self.exit_mode == "simple":
            logger.info(
                f"  take_profit:       +{self.take_profit_percentage * 100:.1f}% "
                f"(sell 100%)"
            )
        else:
            levels_str = ", ".join(
                f"+{lv.pnl_pct * 100:.0f}%→sell {lv.sell_pct * 100:.0f}% rem"
                for lv in self.scale_out_template
            )
            logger.info(f"  scale_out:         {levels_str}")
        logger.info(
            f"  stop_loss:         {self.stop_loss_percentage * 100:.1f}% | "
            f"max_hold: {self.max_hold_seconds}s | "
            f"cooldown: {self.cooldown_seconds}s"
        )
        logger.info("=" * 60)

        try:
            if self.venue == "pumpswap":
                # Transient RPC failures (429 bursts at startup) must not kill
                # the bot — especially when it may be holding a position.
                for attempt in range(5):
                    try:
                        self.pool = await self.pumpswap.find_pool(self.mint)
                        break
                    except Exception:
                        if attempt == 4:
                            raise
                        wait = 5.0 * (attempt + 1)
                        logger.warning(
                            f"Pool resolution failed (attempt {attempt + 1}/5), "
                            f"retrying in {wait:.0f}s"
                        )
                        await asyncio.sleep(wait)
                price = await self.pumpswap.calculate_price(self.pool)
                logger.info(
                    f"Pool price: {price:.12f} SOL/token | market={self.pool.market}"
                )
            else:
                await self._resolve_bonding_curve_token()
            # Adopt tokens already in the wallet BEFORE any buying can happen
            await self._reconcile_position()
            if self.max_daily_loss_sol is not None:
                logger.info(
                    f"Daily loss cap: {self.max_daily_loss_sol} SOL "
                    f"(today so far: {self._daily['pnl_sol']:+.6f})"
                )
        except Exception:
            logger.exception("Failed to resolve market for mint")
            await self.solana_client.close()
            raise

        self._price_monitor_task = asyncio.create_task(self._price_monitor_loop())
        # Heartbeat so a quiet market still shows the bot is alive
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        volume_tasks: list[asyncio.Task] = []
        try:
            if self.venue == "pumpswap":
                assert self.pool is not None
                if self.volume_source in {"poll", "both"}:
                    # With WSS as the primary feed, the poller is only a
                    # gap-filler — slow it down hard to save RPC budget
                    # (4 bots on one free-tier key saturate it otherwise).
                    both = self.volume_source == "both"
                    poller = PoolVolumePoller(
                        self.rpc_endpoint,
                        self.pool.market,
                        poll_interval=90.0 if both else 2.5,
                        batch_limit=10 if both else 25,
                        token_is_base=self.pool.token_is_base,
                    )
                    volume_tasks.append(
                        asyncio.create_task(
                            poller.listen(self._on_trade, stop_event=self._stop_event)
                        )
                    )
                    logger.info("Volume source: HTTP poller (reliable)")
                if self.volume_source in {"wss", "both"}:
                    listener = PoolVolumeListener(
                        self.wss_endpoint,
                        self.pool.market,
                        token_is_base=self.pool.token_is_base,
                    )
                    volume_tasks.append(
                        asyncio.create_task(
                            listener.listen(self._on_trade, stop_event=self._stop_event)
                        )
                    )
                    logger.info("Volume source: WebSocket logsSubscribe")
            else:
                assert self.bonding_curve is not None
                listener = MintVolumeListener(
                    self.wss_endpoint, self.mint, self.bonding_curve
                )
                volume_tasks.append(
                    asyncio.create_task(
                        listener.listen(self._on_trade, stop_event=self._stop_event)
                    )
                )

            if volume_tasks:
                await asyncio.wait(volume_tasks, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            logger.info("FishingTrader cancelled")
        finally:
            self.state = FisherState.STOPPED
            self._stop_event.set()
            for task in volume_tasks:
                task.cancel()
            if self._price_monitor_task:
                self._price_monitor_task.cancel()
            if getattr(self, "_heartbeat_task", None):
                self._heartbeat_task.cancel()
            for task in [
                *volume_tasks,
                self._price_monitor_task,
                getattr(self, "_heartbeat_task", None),
            ]:
                if task is None:
                    continue
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            await self.solana_client.close()
            logger.info(
                f"FishingTrader shut down after {self.cycles_completed} cycle(s)"
            )

    async def _heartbeat_loop(self) -> None:
        """Log volume snapshot every 15s even if no new trades."""
        while not self._stop_event.is_set():
            try:
                await asyncio.sleep(15)
                snap = self.volume_window.snapshot()
                need = (
                    f"need net≥{self.min_net_buy_sol} "
                    f"ratio≥{self.min_buy_sell_ratio} "
                    f"trades≥{self.min_trades_in_window}"
                    if self.entry_mode == "volume"
                    else "entry_mode=always"
                )
                logger.info(
                    f"Heartbeat | state={self.state.value} | "
                    f"window buy={snap.buy_sol:.3f} sell={snap.sell_sol:.3f} "
                    f"net={snap.net_sol:+.3f} SOL trades={snap.trade_count} | "
                    f"{need} | daily_pnl={self._daily['pnl_sol']:+.4f} SOL"
                )
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Heartbeat error")

    def stop(self) -> None:
        """Signal the fisher to shut down."""
        self._stop_event.set()

    # ------------------------------------------------------------------
    # Bonding-curve resolution (legacy path)
    # ------------------------------------------------------------------

    async def _resolve_bonding_curve_token(self) -> None:
        """Build TokenInfo for a pre-migration bonding-curve mint."""
        assert self.address_provider is not None
        assert self.curve_manager is not None

        bonding_curve = self.address_provider.derive_pool_address(self.mint)
        self.bonding_curve = bonding_curve
        pool_state = await self.curve_manager.get_pool_state(bonding_curve)

        if pool_state.get("complete"):
            raise ValueError(
                "Bonding curve is complete (migrated). "
                "Set fishing.venue: pumpswap for this token."
            )

        creator_raw = pool_state.get("creator", "")
        if not creator_raw:
            raise ValueError("Bonding curve has no creator field")
        creator = (
            Pubkey.from_string(creator_raw)
            if isinstance(creator_raw, str)
            else creator_raw
        )

        mint_account = await self.solana_client.get_account_info(self.mint)
        token_program_id = mint_account.owner
        if token_program_id not in (
            SystemAddresses.TOKEN_PROGRAM,
            SystemAddresses.TOKEN_2022_PROGRAM,
        ):
            token_program_id = SystemAddresses.TOKEN_2022_PROGRAM

        associated_bc = self.address_provider.derive_associated_bonding_curve(
            self.mint, bonding_curve, token_program_id
        )
        creator_vault = self.address_provider.derive_creator_vault(creator)

        self.token_info = TokenInfo(
            name=self.symbol,
            symbol=self.symbol,
            uri="",
            mint=self.mint,
            platform=Platform.PUMP_FUN,
            bonding_curve=bonding_curve,
            associated_bonding_curve=associated_bc,
            user=self.wallet.pubkey,
            creator=creator,
            creator_vault=creator_vault,
            token_program_id=token_program_id,
            is_mayhem_mode=bool(pool_state.get("is_mayhem_mode", False)),
            is_cashback_coin=bool(pool_state.get("is_cashback_coin", False)),
        )
        logger.info(f"Bonding curve resolved: {bonding_curve}")

    # ------------------------------------------------------------------
    # Volume signal
    # ------------------------------------------------------------------

    async def _on_trade(self, tick: TradeTick, _event: Any = None) -> None:
        """Handle an observed market trade."""
        # WSS and the poller can deliver the same trade; count it once.
        key = (tick.signature, tick.is_buy, tick.sol_amount, tick.token_amount)
        if key in self._seen_ticks:
            return
        self._seen_ticks[key] = None
        while len(self._seen_ticks) > 4000:
            self._seen_ticks.popitem(last=False)

        self.volume_window.add_trade(tick)

        now = monotonic()
        if now - self._last_volume_log >= 5.0:
            self._last_volume_log = now
            snap = self.volume_window.snapshot(now)
            logger.info(
                f"Volume[{self.volume_window.window_seconds:.0f}s]: "
                f"buy={snap.buy_sol:.3f} sell={snap.sell_sol:.3f} "
                f"net={snap.net_sol:+.3f} SOL | "
                f"ratio={snap.buy_sell_ratio:.2f} | "
                f"trades={snap.trade_count} | state={self.state.value}"
            )

        if self.state == FisherState.WATCHING:
            await self._maybe_enter()

    def _entry_signal_ready(self) -> bool:
        """Return True if entry criteria are met."""
        if self.entry_mode == "always":
            return True
        snap = self.volume_window.snapshot()
        if snap.trade_count < self.min_trades_in_window:
            return False
        if snap.net_sol < self.min_net_buy_sol:
            return False
        if snap.buy_sell_ratio < self.min_buy_sell_ratio:
            return False
        if (
            self.min_price_change_pct > 0
            and snap.price_change_pct < self.min_price_change_pct
        ):
            return False
        return True

    async def _maybe_enter(self) -> None:
        """Enter if signal is ready."""
        if self.max_cycles is not None and self.cycles_completed >= self.max_cycles:
            logger.info(f"Reached max_cycles={self.max_cycles}; stopping")
            self.stop()
            return

        if monotonic() < self._next_entry_allowed_at:
            return

        if not self._entry_signal_ready():
            return

        async with self._trade_lock:
            if self.state != FisherState.WATCHING or not self._entry_signal_ready():
                return
            if monotonic() < self._next_entry_allowed_at:
                return

            if self.entry_mode == "always":
                logger.info(f"🎣 ENTRY (always mode) → buying {self.buy_amount} SOL")
            else:
                snap = self.volume_window.snapshot()
                logger.info(
                    f"🎣 ENTRY SIGNAL: net={snap.net_sol:+.3f} SOL | "
                    f"ratio={snap.buy_sell_ratio:.2f} | "
                    f"trades={snap.trade_count} → buying {self.buy_amount} SOL"
                )
            await self._execute_buy()

    async def _get_price(self) -> float:
        """Current SOL price per token."""
        if self.venue == "pumpswap":
            assert self.pool is not None
            return await self.pumpswap.calculate_price(self.pool)
        assert self.bonding_curve is not None and self.curve_manager is not None
        return await self.curve_manager.calculate_price(self.bonding_curve)

    async def _execute_buy(self) -> None:
        """Execute (or dry-run) a buy and open a FisherPosition."""
        if self.dry_run:
            try:
                price = await self._get_price()
            except Exception:
                logger.exception("Dry-run buy: failed to fetch price")
                return
            if price <= 0:
                logger.warning("Dry-run buy: invalid price")
                return
            tokens = self.buy_amount / price
            self._open_position(price, tokens, self.buy_amount)
            self._log_trade(
                "buy_dry",
                price,
                tokens,
                None,
                extra={"net_sol": self.volume_window.snapshot().net_sol},
            )
            logger.info(
                f"[DRY-RUN] Bought ~{tokens:.2f} tokens @ {price:.12f} SOL "
                f"(cost {self.buy_amount} SOL)"
            )
            return

        if self.venue == "pumpswap":
            assert self.pool is not None

            # Daily loss cap: refuse new entries once breached
            if self._daily_loss_breached():
                return

            # Never buy with money we don't have (or that would drain rent/fees)
            needed = self.buy_amount * (1 + self.buy_slippage) * 1.15
            sol_balance = await self._wallet_sol_balance()
            if sol_balance < needed + self.min_wallet_sol_reserve:
                logger.error(
                    f"Insufficient SOL for entry: balance={sol_balance:.4f}, "
                    f"need ~{needed + self.min_wallet_sol_reserve:.4f} — "
                    f"pausing entries 60s"
                )
                self._next_entry_allowed_at = monotonic() + 60.0
                return

            pre_tokens = await self._wallet_token_balance()
            result = await self.pumpswap.buy(
                self.pool,
                self.wallet.keypair,
                self.buy_amount,
                slippage=self.buy_slippage,
                priority_fee=self.fixed_priority_fee,
                compute_units=int(self.compute_units.get("buy", 200_000)),
            )

            if result.status == "unknown":
                # The tx may still land. Watch the wallet before deciding.
                logger.warning(
                    "Buy outcome unknown — reconciling from wallet balance "
                    "(will NOT re-buy blindly)"
                )
                result = await self._reconcile_unknown_buy(pre_tokens, result)

            if result.ok:
                self._consecutive_buy_failures = 0
                self._open_position(result.price, result.tokens, result.sol)
                self._log_trade(
                    "buy",
                    result.price,
                    result.tokens,
                    result.signature,
                    extra={
                        "sol_spent": result.sol,
                        "net_sol": self.volume_window.snapshot().net_sol,
                    },
                )
                return

            self._consecutive_buy_failures += 1
            backoff = min(300.0, 8.0 * (2 ** (self._consecutive_buy_failures - 1)))
            logger.error(
                f"Buy failed ({self._consecutive_buy_failures} in a row): "
                f"{result.error} — backing off {backoff:.0f}s"
            )
            self._next_entry_allowed_at = monotonic() + backoff
            if self._consecutive_buy_failures >= self.max_consecutive_buy_failures:
                logger.error(
                    "Too many consecutive buy failures — halting bot. "
                    "Check RPC health, wallet balance, and slippage settings."
                )
                self.stop()
            return

        assert self.buyer is not None and self.token_info is not None
        buy_result = await self.buyer.execute(self.token_info)
        if not buy_result.success:
            logger.error(f"Buy failed: {buy_result.error_message}")
            self._next_entry_allowed_at = monotonic() + 8.0
            return
        price = buy_result.price or 0.0
        tokens = buy_result.amount or 0.0
        if price <= 0 or tokens <= 0:
            logger.error("Buy succeeded but missing price/amount")
            self._next_entry_allowed_at = monotonic() + 8.0
            return
        self._open_position(price, tokens, self.buy_amount)
        self._log_trade("buy", price, tokens, buy_result.tx_signature)

    def _daily_loss_breached(self) -> bool:
        """True if today's realized loss exceeds the configured cap."""
        if self.max_daily_loss_sol is None:
            return False
        if self._daily["date"] != self._utc_date():
            self._daily = {"date": self._utc_date(), "pnl_sol": 0.0}
            self._save_state()
        if self._daily["pnl_sol"] <= -abs(self.max_daily_loss_sol):
            logger.warning(
                f"Daily loss cap hit ({self._daily['pnl_sol']:+.6f} SOL) — "
                f"no new entries until UTC midnight"
            )
            self._next_entry_allowed_at = monotonic() + 300.0
            return True
        return False

    async def _reconcile_unknown_buy(self, pre_tokens: float, result) -> Any:
        """Resolve an unconfirmed buy by watching the wallet token balance.

        Returns the (possibly upgraded) SwapResult. Never allows a re-buy
        while the original tx could still land.
        """
        for _ in range(12):  # up to ~36s beyond confirm timeout
            await asyncio.sleep(3.0)
            balance = await self._wallet_token_balance()
            delta = balance - pre_tokens
            if delta > 0:
                est_cost = self.buy_amount * 1.02  # + fees estimate
                logger.warning(
                    f"Unconfirmed buy DID land: +{delta:.2f} tokens — adopting"
                )
                return SwapResult(
                    status="ok",
                    signature=result.signature,
                    tokens=delta,
                    sol=est_cost,
                    price=est_cost / delta,
                )
        return SwapResult(
            status="failed",
            signature=result.signature,
            error="Unconfirmed buy never appeared in wallet",
        )

    def _open_position(
        self, entry_price: float, tokens: float, entry_sol: float
    ) -> None:
        """Record a new open fishing position."""
        levels = [
            ScaleOutLevel(pnl_pct=lv.pnl_pct, sell_pct=lv.sell_pct)
            for lv in self.scale_out_template
        ]
        self.position = FisherPosition(
            entry_price=entry_price,
            entry_time=monotonic(),
            original_tokens=tokens,
            remaining_tokens=tokens,
            entry_sol=entry_sol,
            peak_price=entry_price,
            scale_levels=levels,
        )
        self.state = FisherState.IN_POSITION
        self.volume_window.clear()
        self._save_state()

    # ------------------------------------------------------------------
    # Position management
    # ------------------------------------------------------------------

    async def _price_monitor_loop(self) -> None:
        """Poll price while in position; advance cooldown; always-mode re-entry."""
        while not self._stop_event.is_set():
            try:
                await asyncio.sleep(self.price_check_interval)

                if self.state == FisherState.COOLDOWN:
                    if monotonic() >= self._cooldown_until:
                        self.state = FisherState.WATCHING
                        logger.info("Cooldown complete — ready for next entry")
                        if self.entry_mode == "always":
                            await self._maybe_enter()
                    continue

                # always mode: buy as soon as we're flat and watching
                if self.state == FisherState.WATCHING and self.entry_mode == "always":
                    await self._maybe_enter()
                    continue

                if self.state != FisherState.IN_POSITION or self.position is None:
                    continue

                try:
                    price = await self._get_price()
                except Exception:
                    logger.exception("Price check failed")
                    continue

                if not await self._price_is_sane(price):
                    continue

                await self._manage_position(price)

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in price monitor loop")

    async def _price_is_sane(self, price: float) -> bool:
        """Reject impossible price jumps; detect drained (rugged) pools.

        A liquidity pull makes the vault-ratio price explode by orders of
        magnitude in one tick, which previously fired TAKE PROFIT into an
        empty pool. A >4x move in one 6-second check is treated as an anomaly;
        three in a row triggers a pool-drain check, and a drained pool ends
        the bot with one best-effort exit attempt.
        """
        last = getattr(self, "_last_good_price", 0.0)
        if price <= 0:
            return False
        if last <= 0 or 0.25 <= price / last <= 4.0:
            self._last_good_price = price
            self._price_anomalies = 0
            return True

        self._price_anomalies = getattr(self, "_price_anomalies", 0) + 1
        logger.warning(
            f"Price anomaly #{self._price_anomalies}: {last:.3g} → {price:.3g} "
            f"({price / last:.1f}x in one tick) — ignoring"
        )
        if self._price_anomalies < 3:
            return False

        # Persistent anomaly: is the pool drained?
        assert self.pool is not None
        sol_vault_raw = await self.pumpswap._safe_token_balance(  # noqa: SLF001
            self.pool.sol_vault
        )
        if sol_vault_raw < int(0.5 * 1e9):  # < 0.5 SOL left in the pool
            logger.error(
                f"POOL DRAINED (rug pull): SOL vault holds "
                f"{sol_vault_raw / 1e9:.4f} SOL. Attempting best-effort exit, "
                f"then halting."
            )
            if self.position is not None:
                await self._sell_tokens(
                    self.position.remaining_tokens, last, "rug_exit"
                )
                self._close_cycle()
            self.stop()
            return False

        # Pool still has liquidity — accept the new price regime (real move)
        logger.warning("Price moved >4x but pool has liquidity — accepting")
        self._last_good_price = price
        self._price_anomalies = 0
        return True

    async def _manage_position(self, price: float) -> None:
        """Apply stop-loss, max-hold, simple TP, or scale-out ladder."""
        async with self._trade_lock:
            if self.state != FisherState.IN_POSITION or self.position is None:
                return

            pos = self.position
            pos.mark_price(price)
            pnl_pct = pos.unrealized_pnl_pct(price)
            held = monotonic() - pos.entry_time

            logger.info(
                f"Position: pnl={pnl_pct * 100:+.1f}% | "
                f"price={price:.12f} | peak={pos.peak_price:.12f} | "
                f"held={held:.0f}s | remaining={pos.remaining_tokens:.2f}"
            )

            if pnl_pct <= -self.stop_loss_percentage:
                logger.info(f"🛑 STOP LOSS ({pnl_pct * 100:.1f}%)")
                await self._full_exit(pos, price, "stop_loss")
                return

            if held >= self.max_hold_seconds:
                logger.info(f"⏰ MAX HOLD ({held:.0f}s)")
                await self._full_exit(pos, price, "max_hold")
                return

            # Simple mode: sell entire bag at take-profit
            if self.exit_mode == "simple":
                if pnl_pct >= self.take_profit_percentage:
                    logger.info(
                        f"💰 TAKE PROFIT "
                        f"(+{pnl_pct * 100:.1f}% ≥ "
                        f"+{self.take_profit_percentage * 100:.1f}%) "
                        f"— selling 100%"
                    )
                    await self._full_exit(pos, price, "take_profit")
                return

            for level in pos.scale_levels:
                if level.hit:
                    continue
                if pnl_pct < level.pnl_pct:
                    break

                sell_amount = pos.remaining_tokens * level.sell_pct
                if sell_amount <= 0:
                    level.hit = True
                    continue

                logger.info(
                    f"🎣 SCALE-OUT +{level.pnl_pct * 100:.0f}%: "
                    f"selling {level.sell_pct * 100:.0f}% remaining "
                    f"({sell_amount:.4f} tokens)"
                )
                success = await self._sell_tokens(
                    sell_amount, price, f"scale_out_{level.pnl_pct}"
                )
                if success:
                    level.hit = True
                    self._save_state()
                    if pos.remaining_tokens <= 1.0:
                        logger.info("Bag fully scaled out")
                        self._close_cycle()
                        return
                break

            if pos.scale_levels and all(lv.hit for lv in pos.scale_levels):
                if pos.remaining_tokens > 1.0:
                    logger.info("All scale-out levels hit — selling remainder")
                    await self._full_exit(pos, price, "scale_out_remainder")
                else:
                    self._close_cycle()

    async def _full_exit(self, pos: FisherPosition, price: float, reason: str) -> None:
        """Sell the whole remaining bag; close the cycle ONLY if it worked.

        A failed exit sell (network error, slippage, drained pool) must NOT
        book the position as closed — that records a phantom total loss while
        real tokens sit untracked in the wallet (this happened live when a
        token rugged). Instead: retry on subsequent ticks with escalating
        slippage; after repeated failures declare the position STRANDED, book
        reality, and halt so the supervisor can take over.
        """
        sold_amount = pos.remaining_tokens
        ok = await self._sell_tokens(pos.remaining_tokens, price, reason)
        if ok:
            # Inverted-pool sells can leave a slippage remainder even on
            # success — flush it before closing so no value is stranded
            # in the wallet or misbooked as loss.
            for _ in range(3):
                balance = await self._wallet_token_balance()
                if balance <= 1.0:
                    break
                # A just-landed sell can outrun RPC account state. A
                # balance still equal to what we just sold is a stale
                # read, not a remainder — poll it out rather than firing
                # a doomed flush sell (lands as Custom(1), wastes fees).
                if abs(balance - sold_amount) <= 1.0:
                    await asyncio.sleep(5)
                    continue
                logger.info(f"Flushing sell remainder: {balance:.2f} tokens")
                pos.remaining_tokens = balance
                if not await self._sell_tokens(balance, price, f"{reason}_flush"):
                    break
        if ok or pos.remaining_tokens <= 1.0:
            self._exit_failures = 0
            self._close_cycle()
            return

        self._exit_failures += 1
        logger.error(
            f"Exit sell failed ({reason}) — attempt {self._exit_failures}/8; "
            f"keeping position and retrying"
        )
        if self._exit_failures >= 8:
            logger.error(
                f"POSITION STRANDED: {pos.remaining_tokens:.2f} tokens of "
                f"{self.symbol} cannot be sold (pool drained/rugged?). "
                f"Booking realized-only and halting bot."
            )
            self._close_cycle()
            self.stop()

    async def _sell_tokens(
        self, token_amount: float, price: float, reason: str
    ) -> bool:
        """Sell a portion (or all) of the position."""
        assert self.position is not None
        token_amount = min(token_amount, self.position.remaining_tokens)
        if token_amount <= 0:
            return False

        if self.dry_run:
            sol_out = token_amount * price
            self.position.remaining_tokens -= token_amount
            self.position.realized_sol += sol_out
            self._log_trade(
                "sell_dry",
                price,
                token_amount,
                None,
                extra={"reason": reason, "sol_out": sol_out},
            )
            logger.info(
                f"[DRY-RUN] Sold {token_amount:.4f} @ {price:.12f} "
                f"(~{sol_out:.6f} SOL) reason={reason} | "
                f"remaining={self.position.remaining_tokens:.4f}"
            )
            return True

        if self.venue == "pumpswap":
            assert self.pool is not None
            # Escalate slippage if sells keep failing (never get stuck holding
            # through a dump because min_sol_output is too tight)
            slippage = min(0.30, self.sell_slippage * (1 + self._sell_failures))
            if self._sell_failures:
                logger.warning(
                    f"Sell retry #{self._sell_failures}: slippage → {slippage:.2f}"
                )
            pre_tokens = await self._wallet_token_balance()
            result = await self.pumpswap.sell(
                self.pool,
                self.wallet.keypair,
                token_amount,
                price,
                slippage=slippage,
                priority_fee=self.fixed_priority_fee,
                compute_units=int(self.compute_units.get("sell", 150_000)),
            )

            if result.status == "unknown":
                # Watch balance: if tokens left the wallet, the sell landed.
                for _ in range(12):
                    await asyncio.sleep(3.0)
                    balance = await self._wallet_token_balance()
                    if balance < pre_tokens - 0.5:
                        sold = pre_tokens - balance
                        est_out = sold * price * 0.99
                        logger.warning(f"Unconfirmed sell DID land: -{sold:.2f} tokens")
                        result = SwapResult(
                            status="ok",
                            signature=result.signature,
                            tokens=sold,
                            sol=est_out,
                            price=price,
                        )
                        break
                else:
                    result.status = "failed"
                    result.error = "Unconfirmed sell never reflected in wallet"

            if not result.ok:
                self._sell_failures += 1
                logger.error(f"Sell failed ({reason}): {result.error}")
                return False

            self._sell_failures = 0
            # On-chain balance is the truth for what's left
            self.position.remaining_tokens = await self._wallet_token_balance()
            self.position.realized_sol += result.sol
            self._log_trade(
                "sell",
                result.price,
                result.tokens,
                result.signature,
                extra={"reason": reason, "sol_out": result.sol},
            )
            self._save_state()
            return True

        assert self.seller is not None and self.token_info is not None
        sell_result = await self.seller.execute(
            self.token_info, token_amount=token_amount, token_price=price
        )
        if not sell_result.success:
            logger.error(f"Sell failed ({reason}): {sell_result.error_message}")
            return False
        sold = sell_result.amount or token_amount
        sell_price = sell_result.price or price
        sol_out = sold * sell_price
        self.position.remaining_tokens = max(0.0, self.position.remaining_tokens - sold)
        self.position.realized_sol += sol_out
        self._log_trade(
            "sell",
            sell_price,
            sold,
            sell_result.tx_signature,
            extra={"reason": reason, "sol_out": sol_out},
        )
        return True

    def _close_cycle(self) -> None:
        """Finish a fishing cycle, record P&L, and enter cooldown."""
        pos = self.position
        if pos:
            pnl_sol = pos.realized_sol - pos.entry_sol
            logger.info(
                f"Cycle complete: entry={pos.entry_sol:.6f} SOL | "
                f"realized={pos.realized_sol:.6f} SOL | "
                f"pnl={pnl_sol:+.6f} SOL"
            )
            if self._daily["date"] != self._utc_date():
                self._daily = {"date": self._utc_date(), "pnl_sol": 0.0}
            self._daily["pnl_sol"] += pnl_sol
            logger.info(f"Today's realized P&L: {self._daily['pnl_sol']:+.6f} SOL")
            self._log_trade(
                "cycle_close",
                pos.entry_price,
                pos.original_tokens,
                None,
                extra={
                    "realized_sol": pos.realized_sol,
                    "entry_sol": pos.entry_sol,
                    "pnl_sol": pnl_sol,
                    "daily_pnl_sol": self._daily["pnl_sol"],
                },
            )

        self.position = None
        self.cycles_completed += 1
        self.volume_window.clear()
        self._save_state()

        if self.max_cycles is not None and self.cycles_completed >= self.max_cycles:
            logger.info(f"max_cycles={self.max_cycles} reached — stopping")
            self.state = FisherState.STOPPED
            self.stop()
            return

        self._cooldown_until = monotonic() + self.cooldown_seconds
        self.state = FisherState.COOLDOWN
        logger.info(
            f"Cooldowning {self.cooldown_seconds:.0f}s (cycles={self.cycles_completed})"
        )

    def _log_trade(
        self,
        action: str,
        price: float,
        amount: float,
        tx_signature: str | None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Append a JSON line to the fisher trade log."""
        record: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "action": action,
            "venue": self.venue,
            "mint": self.mint_str,
            "symbol": self.symbol,
            "price": price,
            "amount": amount,
            "tx": tx_signature,
            "dry_run": self.dry_run,
            "state": self.state.value,
            "cycles": self.cycles_completed,
        }
        if extra:
            record.update(extra)
        try:
            with self._trade_log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except OSError:
            logger.exception("Failed to write fisher trade log")
