"""Sample strategy: gap-up watchlist + intraday breakout (paper trading only).

Shows how one file maps to one algorithm:
    * on_market_open  -> read the morning-recorded prices from MarketDataManager
    * on_tick         -> evaluate a dummy trade condition on live ticks
    * on_market_close -> square off and report using the closing snapshot

No real orders are ever placed; there is no kite.place_order() call anywhere.
"""

from __future__ import annotations

from typing import Any

from market_data import InstrumentGroup, MarketDataManager, SnapshotKind
from strategies.base_strategy import DEFAULT_VIRTUAL_CAPITAL, BaseStrategy


class GapUpBreakoutStrategy(BaseStrategy):
    GAP_THRESHOLD_PCT = 1.0      # watch stocks opening >= 1% above the previous close
    TRIGGER_ABOVE_OPEN_PCT = 0.2  # dummy entry: LTP >= open * (1 + 0.2%)
    MAX_WATCHLIST = 3

    def __init__(self, market_data: MarketDataManager, capital: float = DEFAULT_VIRTUAL_CAPITAL) -> None:
        super().__init__(market_data, capital, name="GapUpBreakout")
        self._watchlist: dict[str, float] = {}  # symbol -> recorded open price
        self._triggered: set[str] = set()

    # ------------------------------------------------------------------ #
    def on_market_open(self) -> None:
        snapshot = self.market_data.get_snapshot(SnapshotKind.OPEN)
        if not snapshot:
            self.log.warning("Opening snapshot is empty; nothing to watch today")
            return

        candidates = []
        for symbol, record in snapshot.items():
            if record.group != InstrumentGroup.NIFTY100_STOCK or record.prev_close <= 0:
                continue
            gap_pct = (record.open - record.prev_close) / record.prev_close * 100
            if gap_pct >= self.GAP_THRESHOLD_PCT:
                candidates.append((gap_pct, symbol, record.open))

        candidates.sort(reverse=True)
        self._watchlist = {symbol: open_price for _, symbol, open_price in candidates[: self.MAX_WATCHLIST]}
        self._triggered.clear()
        self.log.info("Watchlist for today: {}", self._watchlist or "none")

    # ------------------------------------------------------------------ #
    def on_tick(self, ticks: list[dict[str, Any]]) -> None:
        if not self._watchlist:
            return
        for tick in ticks:
            instrument = self.market_data.get_instrument_by_token(tick.get("instrument_token"))
            if instrument is None:
                continue
            symbol = instrument.tradingsymbol
            if symbol not in self._watchlist or symbol in self._triggered:
                continue

            ltp = float(tick.get("last_price") or 0.0)
            trigger_price = self._watchlist[symbol] * (1 + self.TRIGGER_ABOVE_OPEN_PCT / 100)
            if ltp >= trigger_price > 0:
                self._triggered.add(symbol)
                self.log.info(
                    "[PLACEHOLDER] BUY signal for {} (LTP {:.2f} >= trigger {:.2f}). "
                    "No real order placed.",
                    symbol, ltp, trigger_price,
                )
                budget = self.wallet.initial_capital / self.MAX_WATCHLIST
                quantity = self.wallet.max_affordable_quantity(ltp, budget)
                if quantity >= 1:
                    self.paper_buy(symbol, ltp, quantity)
                else:
                    self.log.info("Skipping {}: budget ₹{:.2f} too small for one share", symbol, budget)

    # ------------------------------------------------------------------ #
    def on_market_close(self) -> None:
        close_snapshot = self.market_data.get_snapshot(SnapshotKind.CLOSE)
        prices = {s: r.last_price for s, r in close_snapshot.items()}

        for symbol, position in self.wallet.positions.items():
            price = prices.get(symbol)
            if price is None:
                self.log.warning("No closing price for {}; position left open", symbol)
                continue
            self.paper_sell(symbol, price, position.quantity)

        self.log.info("End-of-day wallet summary: {}", self.wallet.summary(prices))
        self._watchlist.clear()
        self._triggered.clear()