"""Entry point: scheduling and orchestration of the trading bot.

Usage:
    python main.py                      # run the scheduler (blocking)
    python main.py --run-once morning   # test a single job right now
    python main.py --run-once open
    python main.py --run-once close
"""

from __future__ import annotations

import argparse
import signal
import sys
from collections.abc import Callable, Sequence
from datetime import datetime, time
from types import FrameType
from typing import Any

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED, JobExecutionEvent
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from config import AppConfig, ConfigError
from market_data import AuthenticationError, MarketDataError, MarketDataManager
from strategies.base_strategy import BaseStrategy
from strategies.strategy_sample import GapUpBreakoutStrategy
from auth import ensure_authenticated

_LOG_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | "
    "<cyan>{extra[strategy]}</cyan> | {name}:{function}:{line} - <level>{message}</level>"
)


def setup_logging(config: AppConfig) -> None:
    config.log_dir.mkdir(parents=True, exist_ok=True)
    logger.remove()
    logger.configure(extra={"strategy": "-"})
    logger.add(sys.stderr, level=config.log_level, format=_LOG_FORMAT, colorize=True)
    logger.add(
        config.log_dir / "bot_{time:YYYY-MM-DD}.log",
        level=config.log_level,
        format=_LOG_FORMAT.replace("<green>", "").replace("</green>", "")
        .replace("<cyan>", "").replace("</cyan>", "")
        .replace("<level>", "").replace("</level>", ""),
        rotation="00:00",
        retention="14 days",
        encoding="utf-8",
        enqueue=True,  # safe with the websocket thread
    )


class TradingBot:
    """Wires the scheduler, market data manager and strategies together."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._market_data = MarketDataManager(config)
        self._scheduler = BlockingScheduler(timezone=config.timezone)
        self._strategies: list[BaseStrategy] = [
            GapUpBreakoutStrategy(self._market_data, capital=config.virtual_capital),
            # Register additional strategies here, one class per file.
        ]

    # ------------------------------------------------------------------ #
    # Scheduled jobs
    # ------------------------------------------------------------------ #
    def job_morning_setup(self) -> None:
        logger.info("=== JOB: morning setup ===")
        self._market_data.morning_setup()

    def job_market_open(self, stream: bool = True) -> None:
        logger.info("=== JOB: market open ===")
        if not self._market_data.capture_open_snapshot():
            logger.warning("Market appears closed today; skipping the session")
            return
        self._dispatch(lambda s: s.on_market_open(), "on_market_open")
        if stream:
            self._market_data.start_streaming()

    def job_market_close(self) -> None:
        logger.info("=== JOB: market close ===")
        self._market_data.stop_streaming()
        if not self._market_data.capture_close_snapshot():
            logger.warning("No closing data captured (holiday?)")
            return
        self._dispatch(lambda s: s.on_market_close(), "on_market_close")

    # ------------------------------------------------------------------ #
    # Dispatch with per-strategy error isolation
    # ------------------------------------------------------------------ #
    def _dispatch(self, hook: Callable[[BaseStrategy], None], label: str) -> None:
        for strategy in self._strategies:
            try:
                hook(strategy)
            except Exception:  # noqa: BLE001 - a faulty strategy must not stop the bot
                logger.bind(strategy=strategy.name).exception("{} raised", label)

    def _on_ticks(self, ticks: list[dict[str, Any]]) -> None:
        self._dispatch(lambda s: s.on_tick(ticks), "on_tick")

    # ------------------------------------------------------------------ #
    # Scheduling
    # ------------------------------------------------------------------ #
    def _add_cron(self, job_id: str, func: Callable[[], None], at: time, second: int = 0) -> None:
        trigger = CronTrigger(
            day_of_week="mon-fri",
            hour=at.hour,
            minute=at.minute,
            second=second,
            timezone=self._config.timezone,
        )
        self._scheduler.add_job(
            func,
            trigger,
            id=job_id,
            name=job_id,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=300,
            replace_existing=True,
        )

    def _schedule_jobs(self) -> None:
        c = self._config
        self._add_cron("morning_setup", self.job_morning_setup, c.morning_setup_time)
        self._add_cron("market_open", self.job_market_open, c.market_open_time, c.open_snapshot_delay_seconds)
        self._add_cron("market_close", self.job_market_close, c.market_close_time, c.close_snapshot_delay_seconds)

    def _on_job_event(self, event: JobExecutionEvent) -> None:
        if event.code == EVENT_JOB_MISSED:
            logger.warning("Job {} missed its scheduled run time", event.job_id)
        else:
            logger.opt(exception=event.exception).error("Job {} failed", event.job_id)

    def _catch_up_if_late(self) -> None:
        """If started mid-session on a weekday, run the jobs that were missed."""
        c = self._config
        now = datetime.now(c.timezone)
        if now.weekday() >= 5 or not (c.morning_setup_time <= now.time() < c.market_close_time):
            return
        logger.info("Started inside today's session window; catching up on missed jobs")
        self.job_morning_setup()
        if now.time() >= c.market_open_time:
            self.job_market_open()

    # ------------------------------------------------------------------ #
    # Run modes
    # ------------------------------------------------------------------ #
    def run(self) -> None:
        self._market_data.add_tick_listener(self._on_ticks)
        self._market_data.verify_connection()  # fail fast on a stale token
        self._schedule_jobs()
        self._scheduler.add_listener(self._on_job_event, EVENT_JOB_ERROR | EVENT_JOB_MISSED)
        self._install_signal_handlers()
        self._catch_up_if_late()

        logger.info("Scheduler started (timezone {}). Press Ctrl+C to stop.", self._config.timezone)
        try:
            self._scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            self.shutdown()

    def run_once(self, job: str) -> None:
        self._market_data.add_tick_listener(self._on_ticks)
        if job == "morning":
            self.job_morning_setup()
        elif job == "open":
            self.job_market_open(stream=False)
        elif job == "close":
            self.job_market_close()

    def shutdown(self) -> None:
        logger.info("Shutting down")
        self._market_data.stop_streaming()

    def _install_signal_handlers(self) -> None:
        def handler(signum: int, _frame: FrameType | None) -> None:
            logger.info("Received signal {}; stopping scheduler", signum)
            if self._scheduler.running:
                self._scheduler.shutdown(wait=False)

        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, handler)


# ---------------------------------------------------------------------- #
def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Automated algo trading bot (paper trading)")
        parser.add_argument("--force-login", action="store_true", help="ignore the saved token and log in again")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        config = AppConfig.load()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    setup_logging(config)
    try:
        config = ensure_authenticated(config, force_login=args.force_login)
        bot = TradingBot(config)
        if args.run_once:
            bot.run_once(args.run_once)
        else:
            bot.run()
    except AuthenticationError as exc:
        logger.error("Authentication problem: {}", exc)
        return 1
    except MarketDataError as exc:
        logger.error("Market data problem: {}", exc)
        return 1
    except Exception:  # noqa: BLE001
        logger.exception("Fatal error")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())