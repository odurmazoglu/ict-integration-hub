from fastapi import APIRouter, HTTPException, status

from app.api.dependencies import DbSessionDep, OdooClientDep, SettingsDep
from app.schemas.normalized_invoice import NormalizedInvoice
from app.schemas.odoo_draft_invoice import OdooDraftInvoiceCreateRequest, OdooDraftInvoiceCreateResponse
from app.schemas.odoo_mapping import OdooMappingPreview
from app.services.odoo_draft_invoice import (
    OdooDraftInvoiceConnectorFailure,
    OdooDraftInvoiceDuplicateInProgressError,
    OdooDraftInvoicePersistenceError,
    OdooDraftInvoiceService,
    OdooDraftInvoiceTimeoutFailure,
    OdooDraftInvoiceValidationError,
)
from app.services.odoo_mapping_preview import OdooMappingPreviewService

router = APIRouter(prefix="/api/v1/odoo", tags=["odoo-mapping"])


@router.post("/mapping-preview", response_model=OdooMappingPreview)
def preview_odoo_mapping(invoice: NormalizedInvoice) -> OdooMappingPreview:
    return OdooMappingPreviewService().build_preview(invoice)


@router.post("/draft-invoices", response_model=OdooDraftInvoiceCreateResponse)
async def create_odoo_draft_invoice(
    request: OdooDraftInvoiceCreateRequest,
    settings: SettingsDep,
    client: OdooClientDep,
    session: DbSessionDep,
) -> OdooDraftInvoiceCreateResponse:
    if settings.app_env == "production":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Odoo draft invoice creation is not enabled in production.",
        )
    service = OdooDraftInvoiceService(session=session, client=client)
    try:
        result = await service.create_draft(request)
        session.commit()
        return result
    except OdooDraftInvoiceValidationError as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=exc.safe_message) from exc
    except OdooDraftInvoiceDuplicateInProgressError as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.safe_message) from exc
    except OdooDraftInvoiceTimeoutFailure as exc:
        session.commit()
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail=exc.safe_message) from exc
    except OdooDraftInvoiceConnectorFailure as exc:
        session.commit()
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=exc.safe_message) from exc
    except OdooDraftInvoicePersistenceError as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=exc.safe_message) from exc
    except Exception:
        session.rollback()
        raise
