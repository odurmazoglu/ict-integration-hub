from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from app.api.dependencies import DbSessionDep, SettingsDep, UyumsoftClientDep
from app.connectors.exceptions import ConnectorError, ConnectorTimeoutError
from app.schemas.invoice_sync import SyncDirection, UyumsoftInvoiceSyncResponse
from app.schemas.uyumsoft_invoices import InvoiceDirection
from app.services.invoice_persistence import InvoicePersistenceService
from app.services.uyumsoft_invoice_sync import (
    MAX_SYNC_PAGES,
    SyncRunRepository,
    UyumsoftInvoiceSyncRequest,
    UyumsoftInvoiceSyncResult,
    UyumsoftInvoiceSyncWorkflow,
)

router = APIRouter(prefix="/api/v1/sync/uyumsoft", tags=["uyumsoft-sync"])

SyncFromQuery = Annotated[datetime, Query(alias="from", description="Inclusive invoice start date/time.")]
SyncToQuery = Annotated[datetime, Query(alias="to", description="Inclusive invoice end date/time.")]
SyncPageSizeQuery = Annotated[int, Query(ge=1, le=100, description="Number of invoices per page, max 100.")]
SyncMaxPagesQuery = Annotated[int, Query(ge=1, le=MAX_SYNC_PAGES, description="Maximum pages per direction.")]
SyncConfirmQuery = Annotated[bool, Query(description="Must be true to explicitly opt into read-only sync.")]


@router.post("/invoices", response_model=UyumsoftInvoiceSyncResponse)
def sync_uyumsoft_invoices(
    settings: SettingsDep,
    client: UyumsoftClientDep,
    session: DbSessionDep,
    from_date: SyncFromQuery,
    to_date: SyncToQuery,
    direction: SyncDirection = "Both",
    page_size: SyncPageSizeQuery = 50,
    max_pages: SyncMaxPagesQuery = 1,
    confirm_read_only: SyncConfirmQuery = False,
) -> UyumsoftInvoiceSyncResponse:
    if settings.uyumsoft_environment != "test":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Uyumsoft synchronization is available only for the test environment.",
        )
    if not confirm_read_only:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="confirm_read_only=true is required for this read-only sync endpoint.",
        )
    if from_date > to_date:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Query parameter 'from' must be before or equal to query parameter 'to'.",
        )
    try:
        workflow = UyumsoftInvoiceSyncWorkflow(
            client=client,
            persistence=InvoicePersistenceService(session),
            run_repository=SyncRunRepository(session),
        )
        result = workflow.run(
            UyumsoftInvoiceSyncRequest(
                from_date=from_date,
                to_date=to_date,
                directions=_directions(direction),
                page_size=page_size,
                max_pages=max_pages,
            )
        )
        session.commit()
    except ConnectorTimeoutError as exc:
        session.commit()
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail=exc.safe_message) from exc
    except ConnectorError as exc:
        session.commit()
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=exc.safe_message) from exc
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    except Exception:
        session.rollback()
        raise

    return _response_from_result(result)


def _response_from_result(result: UyumsoftInvoiceSyncResult) -> UyumsoftInvoiceSyncResponse:
    return UyumsoftInvoiceSyncResponse(
        run_id=result.run_id,
        provider=result.provider,
        status=result.status,
        created=result.created,
        updated=result.updated,
        skipped=result.skipped,
        cursor_state=result.cursor_state,
        failure_message=result.failure_message,
        directions=[
            {
                "direction": summary.direction,
                "pages_fetched": summary.pages_fetched,
                "invoices_seen": summary.invoices_seen,
                "created": summary.created,
                "updated": summary.updated,
                "skipped": summary.skipped,
                "status": summary.status,
                "failure_message": summary.failure_message,
            }
            for summary in result.directions
        ],
    )


def _directions(direction: SyncDirection) -> tuple[InvoiceDirection, ...]:
    if direction == "Both":
        return ("Inbox", "Outbox")
    return (direction,)
