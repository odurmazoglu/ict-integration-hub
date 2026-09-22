from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPBearer

from app.api.dependencies import DbSessionDep, DocumentStorageDep, RequestContextDep, SettingsDep, UyumsoftClientDep
from app.api.security import Permission, require_permission
from app.connectors.exceptions import ConnectorError, ConnectorTimeoutError
from app.schemas.invoice_document import (
    DocumentDownloadItemResponse,
    DocumentDownloadRequest,
    DocumentDownloadResponse,
)
from app.services.document_service import (
    DocumentConflictError,
    DocumentDownloadError,
    DocumentDownloadResult,
    DocumentPersistenceError,
    DocumentValidationError,
    InvoiceDocumentNotFoundError,
    InvoiceDocumentService,
    UnsupportedDocumentTypeError,
)
from app.services.document_storage import DocumentStorageError

# P0-PROD-12B: same bearer-scheme + RequestContext/RBAC pattern as app/api/routers/workbench.py --
# authentication answers "who may call this?"; the test-environment-only gate and confirm_read_only
# below remain the separate, independent "is this operation currently enabled?" controls. This route
# has no company_id-scoped ownership check today (UyumsoftInvoiceMetadata carries no company field at
# this stage of the pipeline -- company is resolved later, during canonical import); an authenticated
# caller is required, but company_id from the token is not used to filter/scope this endpoint, because
# the underlying service cannot enforce that filter.
_bearer_scheme = HTTPBearer(auto_error=False)
router = APIRouter(
    prefix="/api/v1/documents/uyumsoft", tags=["document-download"], dependencies=[Depends(_bearer_scheme)]
)


@router.post("/invoices/download", response_model=DocumentDownloadResponse)
def download_uyumsoft_invoice_documents(
    context: RequestContextDep,
    request: DocumentDownloadRequest,
    settings: SettingsDep,
    client: UyumsoftClientDep,
    session: DbSessionDep,
    storage: DocumentStorageDep,
) -> DocumentDownloadResponse:
    require_permission(Permission.INVOICE_DOCUMENT_READ)(context)
    if settings.uyumsoft_environment != "test":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Uyumsoft document download is available only for the test environment.",
        )
    if not request.confirm_read_only:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="confirm_read_only=true is required for this read-only document download endpoint.",
        )

    try:
        service = InvoiceDocumentService(session=session, client=client, storage=storage)
        result = service.download_documents(
            invoice_ids=request.invoice_ids,
            document_type=request.document_type,
        )
        session.commit()
    except InvoiceDocumentNotFoundError as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.safe_message) from exc
    except (UnsupportedDocumentTypeError, DocumentValidationError) as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=exc.safe_message) from exc
    except DocumentConflictError as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.safe_message) from exc
    except ConnectorTimeoutError as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail=exc.safe_message) from exc
    except ConnectorError as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=exc.safe_message) from exc
    except (DocumentStorageError, DocumentPersistenceError) as exc:
        session.rollback()
        detail = exc.safe_message if isinstance(exc, DocumentDownloadError) else str(exc)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=detail) from exc
    except Exception:
        session.rollback()
        raise

    return _response_from_result(result)


def _response_from_result(result: DocumentDownloadResult) -> DocumentDownloadResponse:
    return DocumentDownloadResponse(
        provider=result.provider,
        document_type=result.document_type,
        downloaded=result.downloaded,
        existing=result.existing,
        items=[
            DocumentDownloadItemResponse(
                invoice_id=item.invoice_id,
                document_id=item.document_id,
                status=item.status,
                document_type=item.document_type,
                storage_backend=item.storage_backend,
                storage_key=item.storage_key,
                content_hash_sha256=item.content_hash_sha256,
                content_size_bytes=item.content_size_bytes,
            )
            for item in result.items
        ],
    )
