"""Uyumsoft inbound invoice poller process: ``python -m app.workers.uyumsoft_inbound_poller``.

Runs outside the API process (its own container), so API replicas/workers never
start a scheduler and a poller failure never affects API health. Cycles run
strictly one after another in this process; across processes the PostgreSQL
advisory lock in :class:`UyumsoftInboundPollCycle` makes any overlap end as
``skipped_locked``.

With ``UYUMSOFT_INBOUND_POLL_ENABLED=false`` (the default) the process only logs
that it is disabled and idles until stopped: no Uyumsoft, Odoo or Hub-data call.
"""

from __future__ import annotations

import argparse
import logging
import signal
import threading
import time
from collections.abc import Callable, Sequence
from typing import Any

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.core.runtime_checks import validate_runtime_configuration

logger = logging.getLogger(__name__)


class InboundPollScheduler:
    """Fixed-interval, non-overlapping loop: the next cycle starts ``interval`` after the
    previous one started, or immediately after it finished if it ran longer."""

    def __init__(
        self,
        *,
        cycle: Callable[[], Any],
        interval_seconds: float,
        stop_event: threading.Event,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive.")
        self._cycle = cycle
        self._interval_seconds = interval_seconds
        self._stop_event = stop_event
        self._monotonic = monotonic

    @property
    def interval_seconds(self) -> float:
        return self._interval_seconds

    def run(self, *, max_cycles: int | None = None) -> int:
        cycles = 0
        while not self._stop_event.is_set():
            started = self._monotonic()
            try:
                self._cycle()
            except Exception as exc:  # the loop must survive anything a cycle raises
                logger.error("uyumsoft_inbound_poll_cycle_crashed error_type=%s", exc.__class__.__name__)
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                break
            elapsed = self._monotonic() - started
            self._stop_event.wait(max(0.0, self._interval_seconds - elapsed))
        return cycles


def main(
    argv: Sequence[str] | None = None,
    *,
    settings: Settings | None = None,
    stop_event: threading.Event | None = None,
    cycle_builder: Callable[[Settings], Callable[[], Any]] | None = None,
) -> int:
    args = _parse_args(argv)
    resolved_settings = settings or get_settings()
    validate_runtime_configuration(resolved_settings)
    configure_logging(resolved_settings)
    stop = stop_event or threading.Event()
    _install_signal_handlers(stop)

    if not resolved_settings.uyumsoft_inbound_poll_enabled:
        logger.info("uyumsoft_inbound_poller_disabled: UYUMSOFT_INBOUND_POLL_ENABLED is false; idling")
        if not args.once:
            stop.wait()
        return 0

    cycle = (cycle_builder or _default_cycle_builder)(resolved_settings)
    scheduler = InboundPollScheduler(
        cycle=cycle,
        interval_seconds=resolved_settings.uyumsoft_inbound_poll_interval_seconds,
        stop_event=stop,
    )
    logger.info(
        "uyumsoft_inbound_poller_started interval_seconds=%s lookback_days=%s",
        scheduler.interval_seconds,
        resolved_settings.uyumsoft_inbound_poll_lookback_days,
    )
    scheduler.run(max_cycles=1 if args.once else None)
    logger.info("uyumsoft_inbound_poller_stopped")
    return 0


def _default_cycle_builder(settings: Settings) -> Callable[[], Any]:
    # Imported lazily so a disabled poller never builds a database engine or a client.
    from app.composition.uyumsoft_inbound_poll import build_uyumsoft_inbound_poll_cycle
    from app.db.session import engine

    return build_uyumsoft_inbound_poll_cycle(settings=settings, engine=engine).run


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Hub-owned Uyumsoft inbound invoice poller.")
    parser.add_argument("--once", action="store_true", help="Run a single cycle (if enabled) and exit.")
    return parser.parse_args(argv)


def _install_signal_handlers(stop: threading.Event) -> None:
    if threading.current_thread() is not threading.main_thread():
        return

    def _handle(signum: int, _frame: object) -> None:
        logger.info("uyumsoft_inbound_poller_stop_requested signal=%s", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)


if __name__ == "__main__":  # pragma: no cover - thin process entry point
    raise SystemExit(main())
