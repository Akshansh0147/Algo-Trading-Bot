"""Abstract strategy contract and the virtual (paper) wallet."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from market_data import MarketDataManager

DEFAULT_VIRTUAL_CAPITAL: float = 10_000.0  # ₹


# ---------------------------------------------------------------------- #
# Virtual wallet
# ---------------------------------------------------------------------- #
class WalletError(Exception):
    """Base class for wallet violations."""


class InsufficientFundsError(WalletError):
    """Order cost exceeds the available virtual cash."""


class InsufficientHoldingsError(WalletError):
    """Attempt to sell more than is held."""


class TradeSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(slots=True)
class Position:
    symbol: str
    quantity: int
    avg_price: float


@dataclass(frozen=True, slots=True)
class VirtualTrade:
    timestamp: datetime
    symbol: str
    side: TradeSide
    quantity: int
    price: float
    realized_pnl: float = 0.0


class VirtualWallet:
    """Paper-trading wallet with a hard capital cap.

    Cash can never go negative: any buy costing more than the available cash
    raises ``InsufficientFundsError``. Brokerage/taxes are not modelled.
    """

    def __init__(self, initial_capital: float = DEFAULT_VIRTUAL_CAPITAL) -> None:
        if initial_capital <= 0:
            raise ValueError("initial_capital must be positive")
        self._initial_capital = float(initial_capital)
        self._cash = float(initial_capital)
        self._positions: dict[str, Position] = {}
        self._trades: list[VirtualTrade] = []
        self._realized_pnl = 0.0

    # Read-only views ---------------------------------------------------- #
    @property
    def initial_capital(self) -> float:
        return self._initial_capital

    @property
    def cash(self) -> float:
        return round(self._cash, 2)

    @property
    def realized_pnl(self) -> float:
        return round(self._realized_pnl, 2)

    @property
    def positions(self) -> dict[str, Position]:
        return {s: Position(p.symbol, p.quantity, p.avg_price) for s, p in self._positions.items()}

    @property
    def trades(self) -> list[VirtualTrade]:
        return list(self._trades)

    # Sizing helpers ----------------------------------------------------- #
    def can_afford(self, price: float, quantity: int) -> bool:
        return price > 0 and quantity > 0 and price * quantity <= self._cash + 1e-9

    def max_affordable_quantity(self, price: float, budget: float | None = None) -> int:
        """Largest whole-share quantity within cash (and optional ``budget``)."""
        if price <= 0:
            return 0
        limit = self._cash if budget is None else min(self._cash, budget)
        return int(limit // price)

    # Trading ------------------------------------------------------------ #
    def buy(self, symbol: str, price: float, quantity: int) -> VirtualTrade:
        self._validate_order(price, quantity)
        cost = price * quantity
        if cost > self._cash + 1e-9:
            raise InsufficientFundsError(
                f"BUY {quantity} {symbol} @ {price:.2f} costs ₹{cost:.2f}, "
                f"but only ₹{self._cash:.2f} is available"
            )
        self._cash -= cost
        position = self._positions.get(symbol)
        if position is None:
            self._positions[symbol] = Position(symbol, quantity, price)
        else:
            total_qty = position.quantity + quantity
            position.avg_price = (position.quantity * position.avg_price + cost) / total_qty
            position.quantity = total_qty
        return self._record(symbol, TradeSide.BUY, quantity, price, 0.0)

    def sell(self, symbol: str, price: float, quantity: int) -> VirtualTrade:
        self._validate_order(price, quantity)
        position = self._positions.get(symbol)
        if position is None or position.quantity < quantity:
            held = position.quantity if position else 0
            raise InsufficientHoldingsError(f"Cannot sell {quantity} {symbol}; holding {held}")
        pnl = (price - position.avg_price) * quantity
        self._cash += price * quantity
        self._realized_pnl += pnl
        position.quantity -= quantity
        if position.quantity == 0:
            del self._positions[symbol]
        return self._record(symbol, TradeSide.SELL, quantity, price, pnl)

    # Valuation ---------------------------------------------------------- #
    def equity(self, prices: Mapping[str, float]) -> float:
        """Cash plus holdings valued at ``prices`` (falls back to average cost)."""
        holdings = sum(p.quantity * prices.get(s, p.avg_price) for s, p in self._positions.items())
        return round(self._cash + holdings, 2)

    def summary(self, prices: Mapping[str, float] | None = None) -> dict[str, Any]:
        equity = self.equity(prices or {})
        return {
            "initial_capital": self._initial_capital,
            "cash": self.cash,
            "equity": equity,
            "realized_pnl": self.realized_pnl,
            "total_return_pct": round((equity - self._initial_capital) / self._initial_capital * 100, 2),
            "open_positions": {s: p.quantity for s, p in self._positions.items()},
            "trade_count": len(self._trades),
        }

    # Internals ---------------------------------------------------------- #
    @staticmethod
    def _validate_order(price: float, quantity: int) -> None:
        if not isinstance(quantity, int) or quantity <= 0:
            raise ValueError("quantity must be a positive integer")
        if price <= 0:
            raise ValueError("price must be positive")

    def _record(self, symbol: str, side: TradeSide, qty: int, price: float, pnl: float) -> VirtualTrade:
        trade = VirtualTrade(datetime.now(), symbol, side, qty, price, round(pnl, 2))
        self._trades.append(trade)
        return trade


# ---------------------------------------------------------------------- #
# Strategy contract
# ---------------------------------------------------------------------- #
class BaseStrategy(ABC):
    """One subclass == one trading algorithm == one file in ``strategies/``."""

    def __init__(
        self,
        market_data: MarketDataManager,
        capital: float = DEFAULT_VIRTUAL_CAPITAL,
        name: str | None = None,
    ) -> None:
        self.market_data = market_data
        self.name = name or type(self).__name__
        self.wallet = VirtualWallet(capital)
        self.log = logger.bind(strategy=self.name)

    # Lifecycle (must be implemented) ------------------------------------ #
    @abstractmethod
    def on_market_open(self) -> None:
        """Called once after the opening snapshot has been recorded."""

    @abstractmethod
    def on_tick(self, ticks: list[dict[str, Any]]) -> None:
        """Called for every batch of live ticks. Keep it fast and non-blocking."""

    @abstractmethod
    def on_market_close(self) -> None:
        """Called once after the closing snapshot has been recorded."""

    # Paper-trading helpers ---------------------------------------------- #
    def paper_buy(self, symbol: str, price: float, quantity: int) -> VirtualTrade | None:
        try:
            trade = self.wallet.buy(symbol, price, quantity)
        except (WalletError, ValueError) as exc:
            self.log.warning("Paper BUY rejected: {}", exc)
            return None
        self.log.info("PAPER BUY {} x{} @ {:.2f} | cash ₹{:.2f}", symbol, quantity, price, self.wallet.cash)
        return trade

    def paper_sell(self, symbol: str, price: float, quantity: int) -> VirtualTrade | None:
        try:
            trade = self.wallet.sell(symbol, price, quantity)
        except (WalletError, ValueError) as exc:
            self.log.warning("Paper SELL rejected: {}", exc)
            return None
        self.log.info(
            "PAPER SELL {} x{} @ {:.2f} | pnl ₹{:.2f} | cash ₹{:.2f}",
            symbol, quantity, price, trade.realized_pnl, self.wallet.cash,
        )
        return trade