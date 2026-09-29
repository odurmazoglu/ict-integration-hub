"""Hub-owned Uyumsoft inbound invoice polling (one cycle).

A cycle reuses the existing manual-sync pipeline end to end --
``UyumsoftInvoiceSyncWorkflow`` -> ``UyumsoftCanonicalInvoiceImporter`` ->
``ImportInvoiceUseCase`` (idempotency, review creation, post-commit Workbench
projection sync) -- and adds only what unattended polling needs:

* a PostgreSQL session advisory lock, so at most one cycle runs across every
  process/container (a crashed holder's connection closes and releases it);
* a read-only "already imported" check keyed on the exact import idempotency key,
  so known invoices are not re-persisted, re-downloaded or re-resolved each cycle;
* per-invoice isolation, so one unexpected failure cannot block the rest of a batch;
* a bounded lookback window instead of a mutable watermark.

Nothing here calls Odoo directly or triggers Vendor Bill execution.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import perf_counter
from typing import Any, Protocol
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.connectors.exceptions import ConnectorError
from app.connectors.uyumsoft.client import UyumsoftSoapClient
from app.models.import_receipt import ImportReceipt
from app.models.uyumsoft_invoice import UyumsoftInvoiceMetadata
from app.models.workbench_review_item import WorkbenchReviewItem
from app.schemas.uyumsoft_invoices import UyumsoftInvoiceSummary
from app.services.invoice_persistence import InvoicePersistenceService, build_invoice_identity
from app.services.uyumsoft_canonical_import import (
    IMPORT_STATUS_CANONICAL_IMPORT_FAILED,
    IMPORT_STATUS_COMPANY_RESOLUTION_FAILED,
    IMPORT_STATUS_NORMALIZATION_FAILED,
    IMPORT_STATUS_PROVIDER_DOWNLOAD_FAILED,
    IMPORT_STATUS_PROVIDER_METADATA_NOT_FOUND,
    UyumsoftCanonicalImportBatchResult,
    UyumsoftCanonicalImportOutcome,
    UyumsoftCanonicalInvoiceImporter,
    import_idempotency_key,
)
from app.services.uyumsoft_invoice_sync import (
    SyncRunRepository,
    UyumsoftInvoiceSyncRequest,
    UyumsoftInvoiceSyncResult,
    UyumsoftInvoiceSyncWorkflow,
)

logger = logging.getLogger(__name__)

POLL_STATUS_COMPLETED = "completed"
POLL_STATUS_COMPLETED_WITH_ERRORS = "completed_with_errors"
POLL_STATUS_FAILED = "failed"
POLL_STATUS_SKIPPED_LOCKED = "skipped_locked"

#: Stable PostgreSQL advisory lock key: first 8 bytes (signed, big-endian) of
#: sha256(b"ict-integration-hub:uyumsoft-inbound-poll").
POLL_ADVISORY_LOCK_KEY = -2752363236075536948
#: The window end is pushed one day past "now" so an invoice dated "today" in
#: Europe/Istanbul is never cut off by a UTC boundary.
POLL_WINDOW_FORWARD_SKEW = timedelta(days=1)

_PROVIDER = "uyumsoft"
_INBOUND_DIRECTION = "Inbox"
_UNEXPECTED_FAILURE_MESSAGE = "Unexpected invoice import failure; see poller logs."
_FAILED_IMPORT_STATUSES = frozenset(
    {
        IMPORT_STATUS_PROVIDER_METADATA_NOT_FOUND,
        IMPORT_STATUS_PROVIDER_DOWNLOAD_FAILED,
        IMPORT_STATUS_NORMALIZATION_FAILED,
        IMPORT_STATUS_COMPANY_RESOLUTION_FAILED,
        IMPORT_STATUS_CANONICAL_IMPORT_FAILED,
    }
)


@dataclass(frozen=True, slots=True)
class InboundPollConfig:
    lookback_days: int = 10
    page_size: int = 100
    max_pages: int = 10


@dataclass(frozen=True, slots=True)
class InboundPollCycleResult:
    cycle_id: str
    status: str
    duration_ms: float
    discovered: int = 0
    already_known: int = 0
    imported: int = 0
    review_created: int = 0
    already_imported: int = 0
    failed: int = 0
    run_id: int | None = None
    failure_type: str | None = None
    failure_message: str | None = None


class PollLock(Protocol):
    def hold(self) -> Any:
        """Context manager yielding True when this caller owns the poll, False otherwise."""


class PostgresAdvisoryPollLock:
    """Cross-process single-flight via ``pg_try_advisory_lock`` on a dedicated connection.

    The lock is session-scoped: it lives exactly as long as the connection, so a
    crashed or killed worker releases it when PostgreSQL drops its socket. The
    connection runs in AUTOCOMMIT so it never sits "idle in transaction".
    """

    def __init__(self, engine: Engine, *, key: int = POLL_ADVISORY_LOCK_KEY) -> None:
        self._engine = engine
        self._key = key

    @contextmanager
    def hold(self) -> Iterator[bool]:
        connection = self._engine.connect().execution_options(isolation_level="AUTOCOMMIT")
        try:
            acquired = bool(connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": self._key}))
        except Exception:
            connection.close()
            raise
        try:
            yield acquired
        finally:
            if acquired:
                try:
                    connection.scalar(text("SELECT pg_advisory_unlock(:key)"), {"key": self._key})
                except Exception:
                    # Never return a possibly still-locked connection to the pool.
                    connection.invalidate()
            connection.close()


class InProcessPollLock:
    """Non-PostgreSQL fallback (local SQLite only): guards a single process, nothing more."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    @contextmanager
    def hold(self) -> Iterator[bool]:
        acquired = self._lock.acquire(blocking=False)
        try:
            yield acquired
        finally:
            if acquired:
                self._lock.release()


class KnownInboundInvoiceChecker:
    """Read-only: has this inbound invoice already produced a receipt or a review?

    Matches the exact key ``import_idempotency_key`` builds
    (``uyumsoft:company:<id>:inbox:<identity>``) for any company, because the
    company is only known after the UBL is downloaded and parsed. Anything not
    known here still goes through ``ImportInvoiceUseCase``, whose own duplicate
    check and unique constraints remain authoritative.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def is_known(self, invoice: UyumsoftInvoiceSummary) -> bool:
        if invoice.direction != _INBOUND_DIRECTION:
            return False
        suffix = _idempotency_key_suffix(invoice)
        like = f"{_PROVIDER}:company:%{_escape_like(suffix)}"
        exact = re.compile(rf"{re.escape(_PROVIDER)}:company:[1-9][0-9]*{re.escape(suffix)}")
        candidates = self._session.scalars(
            select(ImportReceipt.idempotency_key)
            .where(ImportReceipt.idempotency_key.like(like, escape="\\"))
            .union_all(
                select(WorkbenchReviewItem.idempotency_key).where(
                    WorkbenchReviewItem.idempotency_key.like(like, escape="\\")
                )
            )
        ).all()
        return any(exact.fullmatch(candidate) for candidate in candidates)


class IsolatingCanonicalImporter:
    """Imports a page invoice by invoice so one unexpected error cannot stop the rest.

    Expected failures are already turned into safe outcomes by the wrapped importer;
    only a genuinely unexpected exception is caught here. It is logged with its type
    only (never its message, which could echo document content) and reported as a
    failed outcome, so the invoice is retried on the next cycle.
    """

    def __init__(self, inner: UyumsoftCanonicalInvoiceImporter, *, cycle_id: str) -> None:
        self._inner = inner
        self._cycle_id = cycle_id

    def import_invoices(
        self,
        invoices: list[UyumsoftInvoiceSummary],
        *,
        persisted_records: dict[str, UyumsoftInvoiceMetadata],
    ) -> UyumsoftCanonicalImportBatchResult:
        outcomes = tuple(self._import_one(invoice, persisted_records=persisted_records) for invoice in invoices)
        return UyumsoftCanonicalImportBatchResult(outcomes=outcomes)

    def _import_one(
        self,
        invoice: UyumsoftInvoiceSummary,
        *,
        persisted_records: dict[str, UyumsoftInvoiceMetadata],
    ) -> UyumsoftCanonicalImportOutcome:
        identity = build_invoice_identity(invoice).key
        try:
            outcome = self._inner.import_invoice(invoice, persisted_record=persisted_records.get(identity))
        except Exception as exc:
            logger.error(
                "uyumsoft_inbound_poll_invoice_unexpected_error cycle_id=%s invoice_identity=%s error_type=%s",
                self._cycle_id,
                identity,
                exc.__class__.__name__,
                extra={"cycle_id": self._cycle_id, "invoice_identity": identity, "error_type": type(exc).__name__},
            )
            outcome = UyumsoftCanonicalImportOutcome(
                direction=invoice.direction,
                invoice_identity=identity,
                status=IMPORT_STATUS_CANONICAL_IMPORT_FAILED,
                safe_message=_UNEXPECTED_FAILURE_MESSAGE,
            )
        _log_invoice_outcome(self._cycle_id, outcome)
        return outcome


ImporterFactory = Callable[[Session], UyumsoftCanonicalInvoiceImporter]


class UyumsoftInboundPollCycle:
    """One inbound poll: lock -> list Inbox -> skip known -> existing import pipeline."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        client: UyumsoftSoapClient,
        importer_factory: ImporterFactory,
        lock: PollLock,
        config: InboundPollConfig,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._session_factory = session_factory
        self._client = client
        self._importer_factory = importer_factory
        self._lock = lock
        self._config = config
        self._clock = clock

    def run(self) -> InboundPollCycleResult:
        cycle_id = uuid4().hex[:12]
        started = perf_counter()
        request = self._request()
        logger.info(
            "uyumsoft_inbound_poll_started cycle_id=%s from=%s to=%s page_size=%s max_pages=%s",
            cycle_id,
            request.from_date.isoformat(),
            request.to_date.isoformat(),
            request.page_size,
            request.max_pages,
            extra={"cycle_id": cycle_id},
        )
        try:
            with self._lock.hold() as acquired:
                if not acquired:
                    result = InboundPollCycleResult(
                        cycle_id=cycle_id, status=POLL_STATUS_SKIPPED_LOCKED, duration_ms=_elapsed_ms(started)
                    )
                else:
                    result = self._run_locked(cycle_id, request, started)
        except Exception as exc:
            # Lock acquisition itself failed (e.g. database unreachable): nothing was read or written.
            result = _failed(cycle_id, started, exc)
        _log_cycle_finished(result)
        return result

    def _request(self) -> UyumsoftInvoiceSyncRequest:
        now = self._clock().astimezone(UTC)
        return UyumsoftInvoiceSyncRequest(
            from_date=now - timedelta(days=self._config.lookback_days),
            to_date=now + POLL_WINDOW_FORWARD_SKEW,
            directions=(_INBOUND_DIRECTION,),
            page_size=self._config.page_size,
            max_pages=self._config.max_pages,
        )

    def _run_locked(
        self,
        cycle_id: str,
        request: UyumsoftInvoiceSyncRequest,
        started: float,
    ) -> InboundPollCycleResult:
        session = self._session_factory()
        try:
            workflow = UyumsoftInvoiceSyncWorkflow(
                client=self._client,
                persistence=InvoicePersistenceService(session),
                run_repository=SyncRunRepository(session),
                canonical_importer=IsolatingCanonicalImporter(self._importer_factory(session), cycle_id=cycle_id),
                skip_invoice=KnownInboundInvoiceChecker(session).is_known,
            )
            sync_result = workflow.run(request)
            session.commit()
        except ConnectorError as exc:
            # Same as the manual route: keep the failed sync-run audit row. Reviews created
            # before the failure were already committed by ImportInvoiceUseCase.
            _commit_or_rollback(session)
            return _failed(cycle_id, started, exc)
        except Exception as exc:
            session.rollback()
            return _failed(cycle_id, started, exc)
        finally:
            session.close()
        _warn_if_window_truncated(cycle_id, sync_result, request)
        return _completed(cycle_id, started, sync_result)


def _completed(cycle_id: str, started: float, sync: UyumsoftInvoiceSyncResult) -> InboundPollCycleResult:
    discovered = sum(direction.invoices_seen for direction in sync.directions)
    failed = sync.failed_import_count
    return InboundPollCycleResult(
        cycle_id=cycle_id,
        status=POLL_STATUS_COMPLETED_WITH_ERRORS if failed else POLL_STATUS_COMPLETED,
        duration_ms=_elapsed_ms(started),
        discovered=discovered,
        already_known=discovered - sync.selected_invoices,
        imported=sync.imported_count + sync.review_count,
        review_created=sync.review_count,
        already_imported=sync.already_imported_count,
        failed=failed,
        run_id=sync.run_id,
    )


def _failed(cycle_id: str, started: float, exc: Exception) -> InboundPollCycleResult:
    safe_message = getattr(exc, "safe_message", None)
    return InboundPollCycleResult(
        cycle_id=cycle_id,
        status=POLL_STATUS_FAILED,
        duration_ms=_elapsed_ms(started),
        failure_type=exc.__class__.__name__,
        failure_message=safe_message if isinstance(safe_message, str) and safe_message.strip() else None,
    )


def _commit_or_rollback(session: Session) -> None:
    try:
        session.commit()
    except Exception:
        session.rollback()


def _warn_if_window_truncated(
    cycle_id: str,
    sync: UyumsoftInvoiceSyncResult,
    request: UyumsoftInvoiceSyncRequest,
) -> None:
    capacity = request.page_size * request.max_pages
    if any(direction.invoices_seen >= capacity for direction in sync.directions):
        logger.warning(
            "uyumsoft_inbound_poll_window_truncated cycle_id=%s capacity=%s",
            cycle_id,
            capacity,
            extra={"cycle_id": cycle_id, "capacity": capacity},
        )


def _log_invoice_outcome(cycle_id: str, outcome: UyumsoftCanonicalImportOutcome) -> None:
    logger.log(
        logging.WARNING if outcome.status in _FAILED_IMPORT_STATUSES else logging.INFO,
        "uyumsoft_inbound_poll_invoice cycle_id=%s invoice_identity=%s status=%s company_id=%s review_id=%s "
        "safe_message=%s",
        cycle_id,
        outcome.invoice_identity,
        outcome.status,
        outcome.company_id,
        outcome.review_id,
        outcome.safe_message,
        extra={
            "cycle_id": cycle_id,
            "invoice_identity": outcome.invoice_identity,
            "import_outcome": outcome.status,
            "company_id": outcome.company_id,
            "review_id": outcome.review_id,
        },
    )


def _log_cycle_finished(result: InboundPollCycleResult) -> None:
    logger.log(
        logging.ERROR if result.status == POLL_STATUS_FAILED else logging.INFO,
        "uyumsoft_inbound_poll_finished cycle_id=%s status=%s discovered=%s already_known=%s imported=%s "
        "review_created=%s already_imported=%s failed=%s duration_ms=%s run_id=%s failure_type=%s "
        "failure_message=%s",
        result.cycle_id,
        result.status,
        result.discovered,
        result.already_known,
        result.imported,
        result.review_created,
        result.already_imported,
        result.failed,
        result.duration_ms,
        result.run_id,
        result.failure_type,
        result.failure_message,
        extra={
            "cycle_id": result.cycle_id,
            "poll_status": result.status,
            "discovered": result.discovered,
            "already_known": result.already_known,
            "imported": result.imported,
            "failed": result.failed,
            "duration_ms": result.duration_ms,
        },
    )


def _idempotency_key_suffix(invoice: UyumsoftInvoiceSummary) -> str:
    # Derived from the real key builder (company id 1 is a placeholder) so the two can never drift.
    key = import_idempotency_key(company_id=1, provider=_PROVIDER, invoice=invoice)
    return key.removeprefix(f"{_PROVIDER}:company:1")


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _elapsed_ms(started: float) -> float:
    return round((perf_counter() - started) * 1000, 2)


__all__ = [
    "POLL_ADVISORY_LOCK_KEY",
    "POLL_STATUS_COMPLETED",
    "POLL_STATUS_COMPLETED_WITH_ERRORS",
    "POLL_STATUS_FAILED",
    "POLL_STATUS_SKIPPED_LOCKED",
    "InProcessPollLock",
    "InboundPollConfig",
    "InboundPollCycleResult",
    "IsolatingCanonicalImporter",
    "KnownInboundInvoiceChecker",
    "PollLock",
    "PostgresAdvisoryPollLock",
    "UyumsoftInboundPollCycle",
]
