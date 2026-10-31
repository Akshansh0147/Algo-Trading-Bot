"""Market data layer: connectivity, instrument tracking, snapshots, streaming."""

from __future__ import annotations

import io
import threading
import time as time_module
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any, TypeVar

import pandas as pd
import requests
from kiteconnect import KiteConnect, KiteTicker
from kiteconnect.exceptions import (
    DataException,
    KiteException,
    NetworkException,
    TokenException,
)
from loguru import logger

from config import AppConfig

T = TypeVar("T")
TickListener = Callable[[list[dict[str, Any]]], None]

_RETRYABLE = (NetworkException, DataException, requests.RequestException)
_NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/csv,*/*;q=0.8",
}


# ---------------------------------------------------------------------- #
# Errors / enums / models
# ---------------------------------------------------------------------- #
class MarketDataError(RuntimeError):
    """Generic market data failure."""


class AuthenticationError(MarketDataError):
    """Access token is missing, invalid or expired."""


class InstrumentGroup(StrEnum):
    NIFTY50_INDEX = "NIFTY50_INDEX"
    NIFTY100_INDEX = "NIFTY100_INDEX"
    NIFTY100_STOCK = "NIFTY100_STOCK"


class SnapshotKind(StrEnum):
    OPEN = "market_open"
    CLOSE = "market_close"


@dataclass(frozen=True, slots=True)
class TrackedInstrument:
    instrument_token: int
    tradingsymbol: str
    exchange: str
    name: str
    group: InstrumentGroup

    @property
    def quote_key(self) -> str:
        return f"{self.exchange}:{self.tradingsymbol}"


@dataclass(frozen=True, slots=True)
class PriceRecord:
    """One row of a snapshot CSV.

    ``close`` is only populated in the closing dump (it equals the last traded
    price at capture time). ``prev_close`` is the previous session's close as
    reported by the exchange. ``pct_change`` is measured against ``prev_close``.
    """

    trade_date: str
    captured_at: str
    kind: str
    instrument_token: int
    tradingsymbol: str
    name: str
    group: str
    open: float
    high: float
    low: float
    close: float | None
    prev_close: float
    last_price: float
    volume: int
    net_change: float
    pct_change: float


def _retry(
    operation: Callable[[], T],
    *,
    description: str,
    attempts: int = 3,
    delay_seconds: float = 2.0,
) -> T:
    """Run ``operation`` retrying transient network/gateway failures."""
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except TokenException:
            raise
        except _RETRYABLE as exc:
            last_error = exc
            logger.warning("{} failed (attempt {}/{}): {}", description, attempt, attempts, exc)
            time_module.sleep(delay_seconds * attempt)
        except KiteException as exc:
            raise MarketDataError(f"{description} failed: {exc}") from exc
    raise MarketDataError(f"{description} failed after {attempts} attempts") from last_error


def _quote_date(quote: dict[str, Any]) -> date | None:
    for field in ("timestamp", "last_trade_time"):
        value = quote.get(field)
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value).date()
            except ValueError:
                continue
    return None


# ---------------------------------------------------------------------- #
# Manager
# ---------------------------------------------------------------------- #
class MarketDataManager:
    """Owns the Kite connection, tracked instruments, snapshots and live ticks."""

    _INDEX_MAP: dict[str, InstrumentGroup] = {
        "NIFTY 50": InstrumentGroup.NIFTY50_INDEX,
        "NIFTY 100": InstrumentGroup.NIFTY100_INDEX,
    }

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._kite = KiteConnect(api_key=config.credentials.api_key)
        self._kite.set_access_token(config.credentials.access_token)

        self._lock = threading.RLock()
        self._by_token: dict[int, TrackedInstrument] = {}
        self._by_symbol: dict[str, TrackedInstrument] = {}
        self._snapshots: dict[SnapshotKind, dict[str, PriceRecord]] = {k: {} for k in SnapshotKind}
        self._latest_ticks: dict[int, dict[str, Any]] = {}
        self._tick_listeners: list[TickListener] = []
        self._ticker: KiteTicker | None = None
        self._ready = False

        config.storage_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    # Morning connectivity routine
    # ------------------------------------------------------------------ #
    def verify_connection(self) -> dict[str, Any]:
        """Validate the access token by fetching the user profile."""
        try:
            profile = _retry(self._kite.profile, description="Profile check")
        except TokenException as exc:
            raise AuthenticationError(
                "Access token is invalid or expired. Re-run login.py to refresh it."
            ) from exc
        logger.info("Connected to Zerodha as {} ({})", profile.get("user_name"), profile.get("user_id"))
        return profile

    def morning_setup(self) -> None:
        """Verify connectivity, refresh instrument tokens and reset daily state."""
        logger.info("Morning setup started")
        self.verify_connection()
        self.refresh_instruments()
        self._persist_instrument_map()
        with self._lock:
            self._snapshots = {k: {} for k in SnapshotKind}
            self._latest_ticks.clear()
            self._ready = True
        logger.info("Morning setup complete: tracking {} instruments", len(self._by_token))

    # ------------------------------------------------------------------ #
    # Instrument tracking
    # ------------------------------------------------------------------ #
    def refresh_instruments(self) -> None:
        """Resolve tokens for Nifty 50, Nifty 100 and all Nifty 100 constituents."""
        raw = _retry(lambda: self._kite.instruments("NSE"), description="Instrument dump")
        frame = pd.DataFrame(raw)
        if frame.empty:
            raise MarketDataError("Kite returned an empty NSE instrument list.")

        tracked: list[TrackedInstrument] = []

        # Indices (listed under the INDICES segment)
        indices = frame[
            (frame["segment"] == "INDICES")
            & (frame["tradingsymbol"].str.upper().isin(self._INDEX_MAP))
        ]
        found = set()
        for row in indices.to_dict("records"):
            symbol = str(row["tradingsymbol"])
            found.add(symbol.upper())
            tracked.append(
                TrackedInstrument(
                    instrument_token=int(row["instrument_token"]),
                    tradingsymbol=symbol,
                    exchange=str(row["exchange"]),
                    name=str(row.get("name") or symbol),
                    group=self._INDEX_MAP[symbol.upper()],
                )
            )
        for missing_index in set(self._INDEX_MAP) - found:
            logger.warning("Index {!r} not found in the instrument dump", missing_index)

        # Constituents
        symbols = self._load_nifty100_symbols()
        equities = frame[
            (frame["segment"] == "NSE")
            & (frame["instrument_type"] == "EQ")
            & (frame["tradingsymbol"].isin(symbols))
        ]
        for row in equities.to_dict("records"):
            tracked.append(
                TrackedInstrument(
                    instrument_token=int(row["instrument_token"]),
                    tradingsymbol=str(row["tradingsymbol"]),
                    exchange=str(row["exchange"]),
                    name=str(row.get("name") or row["tradingsymbol"]),
                    group=InstrumentGroup.NIFTY100_STOCK,
                )
            )
        unresolved = set(symbols) - set(equities["tradingsymbol"])
        if unresolved:
            logger.warning("Constituents without a Kite match: {}", sorted(unresolved))
        if equities.empty:
            raise MarketDataError("No Nifty 100 constituents could be resolved to instrument tokens.")

        with self._lock:
            self._by_token = {i.instrument_token: i for i in tracked}
            self._by_symbol = {i.tradingsymbol: i for i in tracked}

    def _load_nifty100_symbols(self) -> list[str]:
        """Download the Nifty 100 list from NSE, falling back to the cached copy."""
        cache = self._config.storage_dir / "nifty100_constituents.csv"
        try:
            response = requests.get(
                self._config.nifty100_constituents_url,
                headers=_NSE_HEADERS,
                timeout=self._config.http_timeout_seconds,
            )
            response.raise_for_status()
            symbols = self._parse_symbols(response.text)
            cache.write_text(response.text, encoding="utf-8")
            logger.info("Downloaded Nifty 100 constituents ({} symbols)", len(symbols))
            return symbols
        except (requests.RequestException, KeyError, ValueError) as exc:
            logger.warning("Could not download constituents ({}); trying cache", exc)

        if cache.is_file():
            try:
                symbols = self._parse_symbols(cache.read_text(encoding="utf-8"))
                logger.warning("Using cached constituents from {}", cache)
                return symbols
            except (KeyError, ValueError) as exc:
                raise MarketDataError(f"Cached constituents file is unusable: {exc}") from exc
        raise MarketDataError("Nifty 100 constituents unavailable (download failed, no cache).")

    @staticmethod
    def _parse_symbols(csv_text: str) -> list[str]:
        frame = pd.read_csv(io.StringIO(csv_text))
        symbols = frame["Symbol"].astype(str).str.strip().tolist()
        if not 90 <= len(symbols) <= 110:
            raise ValueError(f"Unexpected constituent count: {len(symbols)}")
        return symbols

    def _persist_instrument_map(self) -> None:
        today = self._today()
        rows = [
            {
                "instrument_token": i.instrument_token,
                "tradingsymbol": i.tradingsymbol,
                "exchange": i.exchange,
                "name": i.name,
                "group": i.group.value,
            }
            for i in self._by_token.values()
        ]
        path = self._config.storage_dir / f"instrument_tokens_{today.isoformat()}.csv"
        pd.DataFrame(rows).sort_values(["group", "tradingsymbol"]).to_csv(path, index=False)
        logger.info("Instrument token map saved to {}", path)

    # Lookups ----------------------------------------------------------- #
    @property
    def tracked_instruments(self) -> list[TrackedInstrument]:
        with self._lock:
            return list(self._by_token.values())

    def get_tokens(self) -> list[int]:
        with self._lock:
            return list(self._by_token)

    def get_instrument(self, tradingsymbol: str) -> TrackedInstrument | None:
        return self._by_symbol.get(tradingsymbol)

    def get_instrument_by_token(self, token: int | None) -> TrackedInstrument | None:
        return self._by_token.get(token) if token is not None else None

    # ------------------------------------------------------------------ #
    # Snapshots
    # ------------------------------------------------------------------ #
    def capture_open_snapshot(self) -> bool:
        """Record open/high/low/volume etc. Returns False on non-trading days."""
        return self._capture(SnapshotKind.OPEN)

    def capture_close_snapshot(self) -> bool:
        """Record the closing dump. Returns False on non-trading days."""
        return self._capture(SnapshotKind.CLOSE)

    def _capture(self, kind: SnapshotKind) -> bool:
        if not self._ready:
            logger.info("Not initialised yet; running morning setup first")
            self.morning_setup()

        instruments = self.tracked_instruments
        quotes = self._fetch_quotes(instruments)
        trade_date = self._today()

        if not any(_quote_date(q) == trade_date for q in quotes.values()):
            logger.warning("No instrument has traded on {}: holiday? Skipping {}", trade_date, kind.value)
            return False

        captured_at = datetime.now(self._config.timezone).isoformat(timespec="seconds")
        records: dict[str, PriceRecord] = {}
        for instrument in instruments:
            quote = quotes.get(instrument.instrument_token)
            if quote is None:
                logger.warning("No quote returned for {}", instrument.tradingsymbol)
                continue
            records[instrument.tradingsymbol] = self._build_record(
                kind, instrument, quote, trade_date, captured_at
            )

        with self._lock:
            self._snapshots[kind] = records
        path = self._write_csv(kind, trade_date, records.values())
        logger.info("{} snapshot: {} rows -> {}", kind.value, len(records), path)
        return True

    def _fetch_quotes(self, instruments: Iterable[TrackedInstrument]) -> dict[int, dict[str, Any]]:
        keys = [i.quote_key for i in instruments]
        size = max(1, self._config.quote_batch_size)
        result: dict[int, dict[str, Any]] = {}
        for start in range(0, len(keys), size):
            batch = keys[start : start + size]
            try:
                data = _retry(lambda b=batch: self._kite.quote(b), description="Quote fetch")
            except TokenException as exc:
                raise AuthenticationError("Access token expired while fetching quotes.") from exc
            for quote in data.values():
                result[int(quote["instrument_token"])] = quote
            if start + size < len(keys):
                time_module.sleep(1.1)  # quote endpoint is limited to 1 req/sec
        return result

    @staticmethod
    def _build_record(
        kind: SnapshotKind,
        instrument: TrackedInstrument,
        quote: dict[str, Any],
        trade_date: date,
        captured_at: str,
    ) -> PriceRecord:
        ohlc = quote.get("ohlc") or {}
        last_price = float(quote.get("last_price") or 0.0)
        prev_close = float(ohlc.get("close") or 0.0)
        net_change = last_price - prev_close
        pct_change = (net_change / prev_close * 100.0) if prev_close else 0.0
        return PriceRecord(
            trade_date=trade_date.isoformat(),
            captured_at=captured_at,
            kind=kind.value,
            instrument_token=instrument.instrument_token,
            tradingsymbol=instrument.tradingsymbol,
            name=instrument.name,
            group=instrument.group.value,
            open=float(ohlc.get("open") or 0.0),
            high=float(ohlc.get("high") or 0.0),
            low=float(ohlc.get("low") or 0.0),
            close=last_price if kind is SnapshotKind.CLOSE else None,
            prev_close=prev_close,
            last_price=last_price,
            volume=int(quote.get("volume") or 0),
            net_change=round(net_change, 2),
            pct_change=round(pct_change, 2),
        )

    def _write_csv(self, kind: SnapshotKind, trade_date: date, records: Iterable[PriceRecord]) -> Path:
        from pathlib import Path  # local import keeps the type-only usage above cheap

        path: Path = self._config.storage_dir / f"{kind.value}_{trade_date.isoformat()}.csv"
        frame = pd.DataFrame([asdict(r) for r in records]).sort_values(["group", "tradingsymbol"])
        tmp = path.with_suffix(".csv.tmp")
        frame.to_csv(tmp, index=False)
        tmp.replace(path)  # atomic: readers never see a half-written file
        return path

    # Snapshot accessors ------------------------------------------------ #
    def get_snapshot(self, kind: SnapshotKind = SnapshotKind.OPEN) -> dict[str, PriceRecord]:
        with self._lock:
            return dict(self._snapshots[kind])

    def get_record(self, tradingsymbol: str, kind: SnapshotKind = SnapshotKind.OPEN) -> PriceRecord | None:
        with self._lock:
            return self._snapshots[kind].get(tradingsymbol)

    def get_snapshot_frame(self, kind: SnapshotKind = SnapshotKind.OPEN) -> pd.DataFrame:
        return pd.DataFrame([asdict(r) for r in self.get_snapshot(kind).values()])

    # ------------------------------------------------------------------ #
    # Live streaming
    # ------------------------------------------------------------------ #
    def add_tick_listener(self, listener: TickListener) -> None:
        self._tick_listeners.append(listener)

    def get_latest_tick(self, token: int) -> dict[str, Any] | None:
        return self._latest_ticks.get(token)

    def start_streaming(self) -> None:
        """Open a KiteTicker websocket (own thread) for every tracked instrument."""
        with self._lock:
            if self._ticker is not None:
                logger.warning("Streaming already active")
                return
            creds = self._config.credentials
            # A fresh ticker per session; Twisted's reactor thread is reused.
            ticker = KiteTicker(creds.api_key, creds.access_token)
            ticker.on_connect = self._on_connect
            ticker.on_ticks = self._on_ticks
            ticker.on_close = self._on_close
            ticker.on_error = self._on_error
            ticker.on_reconnect = self._on_reconnect
            ticker.on_noreconnect = self._on_noreconnect
            self._ticker = ticker
        ticker.connect(threaded=True)
        logger.info("Streaming started")

    def stop_streaming(self) -> None:
        with self._lock:
            ticker, self._ticker = self._ticker, None
        if ticker is None:
            return
        try:
            ticker.close(1000, "End of session")
            logger.info("Streaming stopped")
        except Exception:  # noqa: BLE001 - shutdown must never raise
            logger.exception("Error while closing the ticker")

    # KiteTicker callbacks (run on the Twisted thread) ------------------- #
    def _on_connect(self, ws: KiteTicker, response: Any) -> None:
        tokens = self.get_tokens()
        ws.subscribe(tokens)
        ws.set_mode(ws.MODE_FULL, tokens)
        logger.info("Websocket connected; subscribed to {} instruments", len(tokens))

    def _on_ticks(self, ws: KiteTicker, ticks: list[dict[str, Any]]) -> None:
        for tick in ticks:
            self._latest_ticks[tick["instrument_token"]] = tick
        for listener in list(self._tick_listeners):
            try:
                listener(ticks)
            except Exception:  # noqa: BLE001 - one bad listener must not kill the feed
                logger.exception("Tick listener raised")

    def _on_close(self, ws: KiteTicker, code: int | None, reason: str | None) -> None:
        logger.info("Websocket closed: {} {}", code, reason)

    def _on_error(self, ws: KiteTicker, code: int | None, reason: str | None) -> None:
        logger.error("Websocket error: {} {}", code, reason)

    def _on_reconnect(self, ws: KiteTicker, attempts: int) -> None:
        logger.warning("Websocket reconnecting (attempt {})", attempts)

    def _on_noreconnect(self, ws: KiteTicker) -> None:
        logger.error("Websocket gave up reconnecting; live ticks have stopped")

    # ------------------------------------------------------------------ #
    def _today(self) -> date:
        return datetime.now(self._config.timezone).date()