"""Rolling window tracker for net buy/sell volume on a single mint."""

from collections import deque
from dataclasses import dataclass
from time import monotonic


@dataclass(frozen=True)
class TradeTick:
    """A single observed market trade on the target mint."""

    timestamp: float  # monotonic clock
    is_buy: bool
    sol_amount: float  # decimal SOL
    token_amount: float  # decimal tokens
    price: float  # SOL per token
    signature: str
    user: str


@dataclass
class VolumeSnapshot:
    """Aggregate volume stats for the current rolling window."""

    buy_sol: float
    sell_sol: float
    buy_count: int
    sell_count: int
    trade_count: int
    window_seconds: float
    first_price: float = 0.0
    last_price: float = 0.0

    @property
    def net_sol(self) -> float:
        """Net buy volume in SOL (positive = more buying)."""
        return self.buy_sol - self.sell_sol

    @property
    def buy_sell_ratio(self) -> float:
        """Buy/sell ratio. Returns inf if no sells, 0 if no buys."""
        if self.sell_sol <= 0:
            return float("inf") if self.buy_sol > 0 else 0.0
        return self.buy_sol / self.sell_sol

    @property
    def price_change_pct(self) -> float:
        """Price change across the window (last vs first trade), as a fraction."""
        if self.first_price <= 0 or self.last_price <= 0:
            return 0.0
        return self.last_price / self.first_price - 1.0


class VolumeWindow:
    """Keep a rolling window of trades and compute net volume."""

    def __init__(self, window_seconds: float = 30.0):
        """Initialize the volume window.

        Args:
            window_seconds: How far back to keep trades for net-volume calc.
        """
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self.window_seconds = window_seconds
        self._trades: deque[TradeTick] = deque()

    def add_trade(self, trade: TradeTick) -> None:
        """Add a trade and prune entries outside the window."""
        self._trades.append(trade)
        self._prune(trade.timestamp)

    def _prune(self, now: float | None = None) -> None:
        """Drop trades older than the window."""
        if now is None:
            now = monotonic()
        cutoff = now - self.window_seconds
        while self._trades and self._trades[0].timestamp < cutoff:
            self._trades.popleft()

    def snapshot(self, now: float | None = None) -> VolumeSnapshot:
        """Return current window aggregates.

        Args:
            now: Optional monotonic timestamp (defaults to now).

        Returns:
            VolumeSnapshot with buy/sell totals and counts.
        """
        self._prune(now)
        buy_sol = 0.0
        sell_sol = 0.0
        buy_count = 0
        sell_count = 0
        first_price = 0.0
        last_price = 0.0
        for trade in self._trades:
            if trade.is_buy:
                buy_sol += trade.sol_amount
                buy_count += 1
            else:
                sell_sol += trade.sol_amount
                sell_count += 1
            if trade.price > 0:
                if first_price <= 0:
                    first_price = trade.price
                last_price = trade.price
        return VolumeSnapshot(
            buy_sol=buy_sol,
            sell_sol=sell_sol,
            buy_count=buy_count,
            sell_count=sell_count,
            trade_count=buy_count + sell_count,
            window_seconds=self.window_seconds,
            first_price=first_price,
            last_price=last_price,
        )

    def clear(self) -> None:
        """Remove all trades from the window."""
        self._trades.clear()

    def __len__(self) -> int:
        """Number of trades currently in the window."""
        return len(self._trades)
