import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.connectors.uyumsoft.client import UyumsoftSoapClient
from app.models.uyumsoft_sync_run import UyumsoftSyncRun
from app.schemas.uyumsoft_invoices import InvoiceDirection, UyumsoftInvoiceListRequest
from app.services.invoice_persistence import InvoicePersistenceResult, InvoicePersistenceService

logger = logging.getLogger(__name__)

MAX_SYNC_PAGES = 10
MAX_SYNC_PAGE_SIZE = 100
MAX_SYNC_WINDOW_DAYS = 31
SYNC_STATUS_RUNNING = "running"
SYNC_STATUS_COMPLETED = "completed"
SYNC_STATUS_FAILED = "failed"


@dataclass(frozen=True)
class UyumsoftInvoiceSyncRequest:
    from_date: datetime
    to_date: datetime
    directions: tuple[InvoiceDirection, ...] = ("Inbox", "Outbox")
    page_size: int = 50
    max_pages: int = 1


@dataclass(frozen=True)
class DirectionSyncSummary:
    direction: InvoiceDirection
    pages_fetched: int
    invoices_seen: int
    created: int
    updated: int
    skipped: int
    status: str = SYNC_STATUS_COMPLETED
    failure_message: str | None = None


@dataclass(frozen=True)
class UyumsoftInvoiceSyncResult:
    run_id: int | None = None
    status: str = SYNC_STATUS_COMPLETED
    provider: str = "uyumsoft"
    directions: list[DirectionSyncSummary] = field(default_factory=list)
    cursor_state: dict[str, Any] = field(default_factory=dict)
    failure_message: str | None = None

    @property
    def created(self) -> int:
        return sum(direction.created for direction in self.directions)

    @property
    def updated(self) -> int:
        return sum(direction.updated for direction in self.directions)

    @property
    def skipped(self) -> int:
        return sum(direction.skipped for direction in self.directions)


class UyumsoftInvoiceSyncWorkflow:
    def __init__(
        self,
        *,
        client: UyumsoftSoapClient,
        persistence: InvoicePersistenceService,
        run_repository: "SyncRunRepository | None" = None,
    ) -> None:
        self._client = client
        self._persistence = persistence
        self._run_repository = run_repository

    def run(self, request: UyumsoftInvoiceSyncRequest) -> UyumsoftInvoiceSyncResult:
        _validate_request(request)
        sync_run = self._run_repository.start(request) if self._run_repository is not None else None
        summaries: list[DirectionSyncSummary] = []
        try:
            for direction in request.directions:
                summaries.append(self._sync_direction(direction, request, sync_run=sync_run))
        except SyncDirectionError as exc:
            summaries.append(exc.summary)
            result = UyumsoftInvoiceSyncResult(
                run_id=sync_run.id if sync_run is not None else None,
                status=SYNC_STATUS_FAILED,
                directions=summaries,
                cursor_state=_cursor_state(summaries),
                failure_message=exc.summary.failure_message,
            )
            if self._run_repository is not None and sync_run is not None:
                self._run_repository.fail(sync_run, result, exc.__cause__ or exc)
            logger.info(
                "uyumsoft_invoice_sync_failed",
                extra=_log_extra(result),
            )
            if exc.__cause__ is not None:
                raise exc.__cause__ from exc
            raise
        else:
            result = UyumsoftInvoiceSyncResult(
                run_id=sync_run.id if sync_run is not None else None,
                status=SYNC_STATUS_COMPLETED,
                directions=summaries,
                cursor_state=_cursor_state(summaries),
            )
            if self._run_repository is not None and sync_run is not None:
                self._run_repository.complete(sync_run, result)
        logger.info(
            "uyumsoft_invoice_sync_completed",
            extra=_log_extra(result),
        )
        return result

    def _sync_direction(
        self,
        direction: InvoiceDirection,
        request: UyumsoftInvoiceSyncRequest,
        *,
        sync_run: UyumsoftSyncRun | None,
    ) -> DirectionSyncSummary:
        pages_fetched = 0
        invoices_seen = 0
        persistence_result = InvoicePersistenceResult()
        try:
            for page in range(1, request.max_pages + 1):
                if self._run_repository is not None and sync_run is not None:
                    self._run_repository.mark_page_started(sync_run, direction=direction, page=page)
                list_request = UyumsoftInvoiceListRequest(
                    from_date=request.from_date,
                    to_date=request.to_date,
                    page=page,
                    page_size=request.page_size,
                )
                response = (
                    self._client.list_inbox_invoices(list_request)
                    if direction == "Inbox"
                    else self._client.list_outbox_invoices(list_request)
                )
                pages_fetched += 1
                invoices_seen += len(response.invoices)
                persistence_result = persistence_result.add(self._persistence.persist_invoices(response.invoices))
                summary = DirectionSyncSummary(
                    direction=direction,
                    pages_fetched=pages_fetched,
                    invoices_seen=invoices_seen,
                    created=persistence_result.created,
                    updated=persistence_result.updated,
                    skipped=persistence_result.skipped,
                )
                if self._run_repository is not None and sync_run is not None:
                    self._run_repository.mark_page_completed(sync_run, summary)
                if len(response.invoices) < request.page_size:
                    break
                if response.total_count is not None and invoices_seen >= response.total_count:
                    break
        except Exception as exc:
            summary = self._failed_direction_summary(
                direction=direction,
                pages_fetched=pages_fetched,
                invoices_seen=invoices_seen,
                persistence_result=persistence_result,
                exc=exc,
            )
            raise SyncDirectionError(summary) from exc
        return DirectionSyncSummary(
            direction=direction,
            pages_fetched=pages_fetched,
            invoices_seen=invoices_seen,
            created=persistence_result.created,
            updated=persistence_result.updated,
            skipped=persistence_result.skipped,
        )

    @staticmethod
    def _failed_direction_summary(
        *,
        direction: InvoiceDirection,
        pages_fetched: int,
        invoices_seen: int,
        persistence_result: InvoicePersistenceResult,
        exc: Exception,
    ) -> DirectionSyncSummary:
        return DirectionSyncSummary(
            direction=direction,
            pages_fetched=pages_fetched,
            invoices_seen=invoices_seen,
            created=persistence_result.created,
            updated=persistence_result.updated,
            skipped=persistence_result.skipped,
            status=SYNC_STATUS_FAILED,
            failure_message=_safe_failure_message(exc),
        )


class SyncRunRepository:
    def __init__(self, session: Session, *, provider: str = "uyumsoft") -> None:
        self._session = session
        self._provider = provider

    def start(self, request: UyumsoftInvoiceSyncRequest) -> UyumsoftSyncRun:
        now = datetime.now(UTC)
        sync_run = UyumsoftSyncRun(
            provider=self._provider,
            status=SYNC_STATUS_RUNNING,
            requested_directions=list(request.directions),
            from_date=request.from_date,
            to_date=request.to_date,
            page_size=request.page_size,
            max_pages=request.max_pages,
            pages_fetched=0,
            invoices_seen=0,
            created_count=0,
            updated_count=0,
            skipped_count=0,
            cursor_state={},
            summary={},
            started_at=now,
            created_at=now,
            updated_at=now,
        )
        self._session.add(sync_run)
        self._session.flush()
        return sync_run

    def mark_page_started(
        self,
        sync_run: UyumsoftSyncRun,
        *,
        direction: InvoiceDirection,
        page: int,
    ) -> None:
        now = datetime.now(UTC)
        previous_direction_state = (sync_run.cursor_state or {}).get(direction, {})
        sync_run.current_direction = direction
        sync_run.current_page = page
        sync_run.cursor_state = {
            **(sync_run.cursor_state or {}),
            direction: {
                **previous_direction_state,
                "current_page": page,
                "status": SYNC_STATUS_RUNNING,
            },
        }
        sync_run.updated_at = now
        self._session.flush()

    def mark_page_completed(self, sync_run: UyumsoftSyncRun, summary: DirectionSyncSummary) -> None:
        sync_run.pages_fetched += 1
        sync_run.invoices_seen += summary.invoices_seen - _direction_seen(sync_run, summary.direction)
        sync_run.created_count = (
            sync_run.created_count + summary.created - _direction_count(sync_run, summary.direction, "created")
        )
        sync_run.updated_count = (
            sync_run.updated_count + summary.updated - _direction_count(sync_run, summary.direction, "updated")
        )
        sync_run.skipped_count = (
            sync_run.skipped_count + summary.skipped - _direction_count(sync_run, summary.direction, "skipped")
        )
        sync_run.cursor_state = {
            **(sync_run.cursor_state or {}),
            summary.direction: {
                "current_page": summary.pages_fetched,
                "pages_fetched": summary.pages_fetched,
                "invoices_seen": summary.invoices_seen,
                "created": summary.created,
                "updated": summary.updated,
                "skipped": summary.skipped,
                "status": summary.status,
            },
        }
        sync_run.updated_at = datetime.now(UTC)
        self._session.flush()

    def complete(self, sync_run: UyumsoftSyncRun, result: UyumsoftInvoiceSyncResult) -> None:
        now = datetime.now(UTC)
        sync_run.status = SYNC_STATUS_COMPLETED
        sync_run.current_direction = None
        sync_run.current_page = None
        sync_run.cursor_state = result.cursor_state
        sync_run.summary = _result_summary(result)
        sync_run.finished_at = now
        sync_run.updated_at = now
        self._session.flush()

    def fail(self, sync_run: UyumsoftSyncRun, result: UyumsoftInvoiceSyncResult, exc: Exception) -> None:
        now = datetime.now(UTC)
        sync_run.status = SYNC_STATUS_FAILED
        sync_run.cursor_state = {
            **(sync_run.cursor_state or {}),
            **result.cursor_state,
        }
        sync_run.summary = _result_summary(result)
        sync_run.failure_message = _safe_failure_message(exc)
        sync_run.failure_detail = {"type": exc.__class__.__name__}
        sync_run.finished_at = now
        sync_run.updated_at = now
        self._session.flush()


class SyncDirectionError(RuntimeError):
    def __init__(self, summary: DirectionSyncSummary) -> None:
        super().__init__(summary.failure_message or "Uyumsoft sync direction failed.")
        self.summary = summary


def _validate_request(request: UyumsoftInvoiceSyncRequest) -> None:
    if request.from_date > request.to_date:
        raise ValueError("from_date must be before or equal to to_date.")
    if request.to_date - request.from_date > timedelta(days=MAX_SYNC_WINDOW_DAYS):
        raise ValueError(f"Sync window must be {MAX_SYNC_WINDOW_DAYS} days or less.")
    if request.page_size < 1 or request.page_size > MAX_SYNC_PAGE_SIZE:
        raise ValueError(f"page_size must be between 1 and {MAX_SYNC_PAGE_SIZE}.")
    if request.max_pages < 1 or request.max_pages > MAX_SYNC_PAGES:
        raise ValueError(f"max_pages must be between 1 and {MAX_SYNC_PAGES}.")
    if not request.directions:
        raise ValueError("At least one direction is required.")
    for value in (request.from_date, request.to_date):
        if value.tzinfo is None:
            raise ValueError("from_date and to_date must be timezone-aware.")
        value.astimezone(UTC)
    invalid_directions = sorted(set(request.directions) - {"Inbox", "Outbox"})
    if invalid_directions:
        raise ValueError(f"Invalid directions: {', '.join(invalid_directions)}.")


def _cursor_state(summaries: list[DirectionSyncSummary]) -> dict[str, Any]:
    return {
        summary.direction: {
            "current_page": summary.pages_fetched + 1
            if summary.status == SYNC_STATUS_FAILED
            else summary.pages_fetched,
            "pages_fetched": summary.pages_fetched,
            "invoices_seen": summary.invoices_seen,
            "created": summary.created,
            "updated": summary.updated,
            "skipped": summary.skipped,
            "status": summary.status,
        }
        for summary in summaries
    }


def _result_summary(result: UyumsoftInvoiceSyncResult) -> dict[str, Any]:
    return {
        "status": result.status,
        "provider": result.provider,
        "created": result.created,
        "updated": result.updated,
        "skipped": result.skipped,
        "directions": [
            {
                "direction": summary.direction,
                "status": summary.status,
                "pages_fetched": summary.pages_fetched,
                "invoices_seen": summary.invoices_seen,
                "created": summary.created,
                "updated": summary.updated,
                "skipped": summary.skipped,
            }
            for summary in result.directions
        ],
    }


def _log_extra(result: UyumsoftInvoiceSyncResult) -> dict[str, Any]:
    return {
        "provider": result.provider,
        "run_id": result.run_id,
        "status": result.status,
        "created": result.created,
        "updated": result.updated,
        "skipped": result.skipped,
        "directions": [summary.direction for summary in result.directions],
    }


def _safe_failure_message(exc: Exception) -> str:
    message = str(exc).strip() or exc.__class__.__name__
    return message[:1000]


def _direction_seen(sync_run: UyumsoftSyncRun, direction: InvoiceDirection) -> int:
    return int((sync_run.cursor_state or {}).get(direction, {}).get("invoices_seen", 0))


def _direction_count(sync_run: UyumsoftSyncRun, direction: InvoiceDirection, key: str) -> int:
    return int((sync_run.cursor_state or {}).get(direction, {}).get(key, 0))
