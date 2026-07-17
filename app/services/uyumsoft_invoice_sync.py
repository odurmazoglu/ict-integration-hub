import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.connectors.uyumsoft.client import UyumsoftSoapClient
from app.schemas.uyumsoft_invoices import InvoiceDirection, UyumsoftInvoiceListRequest
from app.services.invoice_persistence import InvoicePersistenceResult, InvoicePersistenceService

logger = logging.getLogger(__name__)

MAX_SYNC_PAGES = 10


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


@dataclass(frozen=True)
class UyumsoftInvoiceSyncResult:
    provider: str = "uyumsoft"
    directions: list[DirectionSyncSummary] = field(default_factory=list)

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
    ) -> None:
        self._client = client
        self._persistence = persistence

    def run(self, request: UyumsoftInvoiceSyncRequest) -> UyumsoftInvoiceSyncResult:
        _validate_request(request)
        summaries = [self._sync_direction(direction, request) for direction in request.directions]
        result = UyumsoftInvoiceSyncResult(directions=summaries)
        logger.info(
            "uyumsoft_invoice_sync_completed",
            extra={
                "provider": result.provider,
                "created": result.created,
                "updated": result.updated,
                "skipped": result.skipped,
                "directions": [summary.direction for summary in result.directions],
            },
        )
        return result

    def _sync_direction(
        self,
        direction: InvoiceDirection,
        request: UyumsoftInvoiceSyncRequest,
    ) -> DirectionSyncSummary:
        pages_fetched = 0
        invoices_seen = 0
        persistence_result = InvoicePersistenceResult()
        for page in range(1, request.max_pages + 1):
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
            if len(response.invoices) < request.page_size:
                break
            if response.total_count is not None and invoices_seen >= response.total_count:
                break
        return DirectionSyncSummary(
            direction=direction,
            pages_fetched=pages_fetched,
            invoices_seen=invoices_seen,
            created=persistence_result.created,
            updated=persistence_result.updated,
            skipped=persistence_result.skipped,
        )


def _validate_request(request: UyumsoftInvoiceSyncRequest) -> None:
    if request.from_date > request.to_date:
        raise ValueError("from_date must be before or equal to to_date.")
    if request.page_size < 1 or request.page_size > 100:
        raise ValueError("page_size must be between 1 and 100.")
    if request.max_pages < 1 or request.max_pages > MAX_SYNC_PAGES:
        raise ValueError(f"max_pages must be between 1 and {MAX_SYNC_PAGES}.")
    if not request.directions:
        raise ValueError("At least one direction is required.")
    for value in (request.from_date, request.to_date):
        if value.tzinfo is None:
            raise ValueError("from_date and to_date must be timezone-aware.")
        value.astimezone(UTC)
