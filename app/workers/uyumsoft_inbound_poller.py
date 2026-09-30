"""Uyumsoft inbound invoice poller process: ``python -m app.workers.uyumsoft_inbound_poller``.

Runs outside the API process (its own container), so API replicas/workers never
start a scheduler and a poller failure never affects API health. Cycles run
strictly one after another in this process; across processes the PostgreSQL
advisory lock in :class:`UyumsoftInboundPollCycle` makes any overlap end as
``skipped_locked``.

With ``UYUMSOFT_INBOUND_POLL_ENABLED=false`` (the default) the process only logs
that it is disabled and idles until stopped: no Uyumsoft, Odoo or Hub-data call.

``--preview`` is the first-run safety check: a read-only dry run, allowed while
polling is disabled, that prints every Inbox invoice in the next cycle's window as
ALREADY_KNOWN / NEW / WOULD_IMPORT and then exits. Nothing is persisted,
downloaded or imported.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from collections.abc import Callable, Sequence
from typing import Any, TextIO

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.core.runtime_checks import validate_runtime_configuration
from app.services.uyumsoft_inbound_poll import (
    PREVIEW_ALREADY_KNOWN,
    PREVIEW_NEW,
    PREVIEW_WOULD_IMPORT,
    InboundPollPreview,
)

EXIT_PREVIEW_FAILED = 2

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
    preview_builder: Callable[[Settings], Callable[[], InboundPollPreview]] | None = None,
    out: TextIO | None = None,
) -> int:
    args = _parse_args(argv)
    resolved_settings = settings or get_settings()
    validate_runtime_configuration(resolved_settings)
    configure_logging(resolved_settings)
    if args.preview:
        return _run_preview((preview_builder or _default_preview_builder)(resolved_settings), out or sys.stdout)
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


def _run_preview(preview: Callable[[], InboundPollPreview], out: TextIO) -> int:
    try:
        result = preview()
    except Exception as exc:
        safe_message = getattr(exc, "safe_message", None)
        print(f"Preview failed: {exc.__class__.__name__}: {safe_message or 'see logs'}", file=sys.stderr)
        return EXIT_PREVIEW_FAILED
    _write_preview(result, out)
    return 0


def _write_preview(preview: InboundPollPreview, out: TextIO) -> None:
    print("Uyumsoft inbound poll preview (read-only: nothing persisted, downloaded or imported)", file=out)
    print(
        f"window_from={preview.from_date.isoformat()} window_to={preview.to_date.isoformat()} "
        f"pages={preview.pages_fetched} truncated={str(preview.truncated).lower()}",
        file=out,
    )
    print("STATUS\tETTN\tINVOICE_NUMBER\tINVOICE_DATE\tSENDER_VKN\tTOTAL\tCURRENCY", file=out)
    for item in preview.items:
        print(
            "\t".join(
                str(value if value is not None else "-")
                for value in (
                    item.status,
                    item.ettn or item.invoice_identity,
                    item.invoice_number,
                    item.invoice_date.date().isoformat() if item.invoice_date else None,
                    item.sender_tax_number,
                    item.total_amount,
                    item.currency,
                )
            ),
            file=out,
        )
    print(
        f"summary discovered={len(preview.items)} already_known={preview.count(PREVIEW_ALREADY_KNOWN)} "
        f"new={preview.count(PREVIEW_NEW)} would_import={preview.count(PREVIEW_WOULD_IMPORT)} "
        f"next_cycle_would_import={len(preview.would_import)}",
        file=out,
    )
    if preview.truncated:
        print("WARNING: window truncated at page_size x max_pages; the list above is incomplete.", file=out)


def _default_preview_builder(settings: Settings) -> Callable[[], InboundPollPreview]:
    from app.composition.uyumsoft_inbound_poll import build_uyumsoft_inbound_poll_preview
    from app.db.session import engine

    return build_uyumsoft_inbound_poll_preview(settings=settings, engine=engine).run


def _default_cycle_builder(settings: Settings) -> Callable[[], Any]:
    # Imported lazily so a disabled poller never builds a database engine or a client.
    from app.composition.uyumsoft_inbound_poll import build_uyumsoft_inbound_poll_cycle
    from app.db.session import engine

    return build_uyumsoft_inbound_poll_cycle(settings=settings, engine=engine).run


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Hub-owned Uyumsoft inbound invoice poller.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="Run a single cycle (if enabled) and exit.")
    mode.add_argument(
        "--preview",
        action="store_true",
        help="Read-only dry run of the next cycle's window (works while disabled); prints and exits.",
    )
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
