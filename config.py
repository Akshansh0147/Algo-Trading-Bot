from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv
from loguru import logger

BASE_DIR: Path = Path(__file__).resolve().parent


class ConfigError(RuntimeError):
    """Raised when mandatory configuration is missing or invalid."""


@dataclass(frozen=True, slots=True)
class FallbackConfig:
    """Defaults used whenever the matching environment variable is absent."""

    timezone: str = "Asia/Kolkata"
    morning_setup_time: str = "08:45"
    market_open_time: str = "09:15"
    market_close_time: str = "15:30"
    # Seconds after the scheduled time before data is read, so the exchange
    # has published the first/last prices.
    open_snapshot_delay_seconds: int = 5
    close_snapshot_delay_seconds: int = 10
    storage_dir: str = "storage"
    log_dir: str = "logs"
    log_level: str = "INFO"
    access_token_file: str = "access_token.txt"
    virtual_capital: float = 10_000.0
    quote_batch_size: int = 500  # Kite quote() limit per request
    http_timeout_seconds: int = 20
    nifty100_constituents_url: str = (
        "https://archives.nseindia.com/content/indices/ind_nifty100list.csv"
    )
    redirect_url: str = "http://127.0.0.1:5000/"
    login_timeout_seconds: int = 300


@dataclass(frozen=True, slots=True, repr=False)
class KiteCredentials:
    """Zerodha credentials. ``repr`` is masked so secrets never reach logs."""

    api_key: str
    api_secret: str
    access_token: str

    @staticmethod
    def _mask(value: str) -> str:
        return f"{value[:3]}***" if value else "<empty>"

    def __repr__(self) -> str:
        return (
            f"KiteCredentials(api_key={self._mask(self.api_key)}, "
            f"api_secret={self._mask(self.api_secret)}, "
            f"access_token={self._mask(self.access_token)})"
        )


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Fully-resolved, immutable application configuration."""

    credentials: KiteCredentials
    timezone: ZoneInfo
    morning_setup_time: time
    market_open_time: time
    market_close_time: time
    open_snapshot_delay_seconds: int
    close_snapshot_delay_seconds: int
    storage_dir: Path
    log_dir: Path
    log_level: str
    virtual_capital: float
    quote_batch_size: int
    http_timeout_seconds: int
    nifty100_constituents_url: str
    redirect_url: str
    login_timeout_seconds: int
    token_file: Path

    # ------------------------------------------------------------------ #
    @classmethod
    def load(
        cls,
        env_file: Path | None = None,
        fallback: FallbackConfig | None = None,
    ) -> AppConfig:
        """Build the configuration from env vars / .env / fallbacks."""
        fb = fallback or FallbackConfig()
        load_dotenv(env_file or BASE_DIR / ".env", override=False)

        tz_name = _env("BOT_TIMEZONE", fb.timezone)
        try:
            tz = ZoneInfo(tz_name)
        except ZoneInfoNotFoundError as exc:
            raise ConfigError(
                f"Unknown timezone {tz_name!r}. On Windows run: pip install tzdata"
            ) from exc

        setup_t = _parse_clock(_env("MORNING_SETUP_TIME", fb.morning_setup_time), "MORNING_SETUP_TIME")
        open_t = _parse_clock(_env("MARKET_OPEN_TIME", fb.market_open_time), "MARKET_OPEN_TIME")
        close_t = _parse_clock(_env("MARKET_CLOSE_TIME", fb.market_close_time), "MARKET_CLOSE_TIME")
        if not setup_t < open_t < close_t:
            raise ConfigError("Times must satisfy MORNING_SETUP < MARKET_OPEN < MARKET_CLOSE.")

        capital = _env_float("VIRTUAL_CAPITAL", fb.virtual_capital)
        if capital <= 0:
            raise ConfigError("VIRTUAL_CAPITAL must be positive.")

        token_file = _resolve_path(_env("ACCESS_TOKEN_FILE", fb.access_token_file))

        return cls(
            credentials=cls._load_credentials(token_file),
            timezone=tz,
            morning_setup_time=setup_t,
            market_open_time=open_t,
            market_close_time=close_t,
            open_snapshot_delay_seconds=_env_int(
                "OPEN_SNAPSHOT_DELAY_SECONDS", fb.open_snapshot_delay_seconds
            ),
            close_snapshot_delay_seconds=_env_int(
                "CLOSE_SNAPSHOT_DELAY_SECONDS", fb.close_snapshot_delay_seconds
            ),
            storage_dir=_resolve_path(_env("STORAGE_DIR", fb.storage_dir)),
            log_dir=_resolve_path(_env("LOG_DIR", fb.log_dir)),
            log_level=_env("LOG_LEVEL", fb.log_level).upper(),
            virtual_capital=capital,
            quote_batch_size=_env_int("QUOTE_BATCH_SIZE", fb.quote_batch_size),
            http_timeout_seconds=_env_int("HTTP_TIMEOUT_SECONDS", fb.http_timeout_seconds),
            nifty100_constituents_url=_env(
                "NIFTY100_CONSTITUENTS_URL", fb.nifty100_constituents_url
            ),
            redirect_url=_env("KITE_REDIRECT_URL", fb.redirect_url),
            login_timeout_seconds=_env_int("LOGIN_TIMEOUT_SECONDS", fb.login_timeout_seconds),
            token_file=token_file,
        )

    # ------------------------------------------------------------------ #
    @staticmethod
    def _load_credentials(token_file: Path) -> KiteCredentials:
        api_key = _env("KITE_API_KEY", "")
        api_secret = _env("KITE_API_SECRET", "")
        if not api_key:
            raise ConfigError("KITE_API_KEY is not set (env var or .env file).")
        return KiteCredentials(api_key, api_secret, _read_access_token(token_file))


# ---------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------- #
def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value.strip().strip('"').strip("'") if value and value.strip() else default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}.") from exc


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}.") from exc


def _parse_clock(value: str, name: str) -> time:
    try:
        return datetime.strptime(value.strip(), "%H:%M").time()
    except ValueError as exc:
        raise ConfigError(f"{name} must be in HH:MM format, got {value!r}.") from exc


def _resolve_path(value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else BASE_DIR / path


def _read_access_token(token_file: Path) -> str:
    """Env var first, then the token file. Returns '' if neither exists."""
    for var in ("KITE_ACCESS_TOKEN", "ACCESS_TOKEN"):
        value = os.environ.get(var, "").strip()
        if value:
            return value
    if token_file.is_file():
        return token_file.read_text(encoding="utf-8").strip()
    return ""
    

    if token_file.is_file():
        token = token_file.read_text(encoding="utf-8").strip()
        if token:
            modified = datetime.fromtimestamp(token_file.stat().st_mtime, tz).date()
            if modified != datetime.now(tz).date():
                logger.warning(
                    "{} was last written on {}; Kite tokens expire daily, so re-run login.py.",
                    token_file.name,
                    modified,
                )
            return token
    return ""