from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.connectors.exceptions import ConnectorError  # noqa: E402
from app.connectors.uyumsoft.client import UyumsoftSoapClient  # noqa: E402
from app.core.config import Settings, get_settings  # noqa: E402
from app.core.runtime_checks import runtime_configuration_errors  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.models.invoice_document import InvoiceDocument  # noqa: E402
from app.models.uyumsoft_invoice import UyumsoftInvoiceMetadata  # noqa: E402
from app.schemas.uyumsoft_invoices import (  # noqa: E402
    InvoiceListDateField,
    UyumsoftInvoiceListRequest,
    UyumsoftInvoiceSummary,
)
from app.services.document_service import (  # noqa: E402
    MIME_TYPE_UBL_XML,
    DocumentDownloadError,
    InvoiceDocumentService,
)
from app.services.document_storage import DocumentStorage, DocumentStorageError, LocalDocumentStorage  # noqa: E402
from app.services.invoice_persistence import InvoicePersistenceService, build_invoice_identity  # noqa: E402

ENABLE_FLAG = "ICT_UYUMSOFT_ENABLE_LIVE_SMOKE"
SAFE_FAILURE_MESSAGE = "Uyumsoft invoice download readonly validation failed."
MISSING_INVOICE_ID_MESSAGE = "Selected invoice does not contain an invoice identifier."
SAFE_CONNECTOR_MESSAGE_PREFIXES = (
    "--from must be before or equal to --to.",
    "Invoice lookup requires --from and --to.",
    "No Inbox invoice was returned for the provided readonly lookup.",
    "Uyumsoft invoice id is required for document download.",
    "Uyumsoft request failed.",
    "Uyumsoft request timed out.",
    "Uyumsoft transport request failed.",
    "Uyumsoft invoice document data is not valid base64.",
    "Uyumsoft invoice document data has an unsupported type.",
)
DirectionArg = Literal["inbox"]


@dataclass(frozen=True)
class DownloadSmokeSelection:
    direction: DirectionArg
    invoice_id: str | None
    from_date: datetime | None
    to_date: datetime | None
    date_field: InvoiceListDateField


def main() -> int:
    args = _parse_args()
    settings = get_settings()
    safety_errors = _live_download_readonly_errors(settings)
    if safety_errors:
        print(_json_dumps(_failure(error="; ".join(safety_errors))))
        return 1

    selection = _selection_from_args(args)
    with SessionLocal() as session:
        report = run_validation(
            settings=settings,
            client=UyumsoftSoapClient.from_settings(settings),
            session=session,
            storage=LocalDocumentStorage(settings.document_storage_root),
            selection=selection,
        )
        session.commit()
    print(_json_dumps(report))
    return 0 if report["success"] else 1


def run_validation(
    *,
    settings: Settings,
    client: UyumsoftSoapClient,
    session: Session,
    storage: DocumentStorage,
    selection: DownloadSmokeSelection,
) -> dict[str, Any]:
    safety_errors = _live_download_readonly_errors(settings)
    if safety_errors:
        return _failure(error="; ".join(safety_errors), direction=_display_direction(selection.direction))

    try:
        invoice = _select_invoice(client, selection)
        if not _invoice_identifier_present(invoice):
            return _failure(
                direction=invoice.direction,
                invoice_identifier_present=False,
                error=MISSING_INVOICE_ID_MESSAGE,
            )

        InvoicePersistenceService(session).persist_invoice(invoice)
        record = _find_persisted_invoice(session, invoice)
        if record is None:
            return _failure(
                direction=invoice.direction,
                invoice_identifier_present=True,
                error="Invoice metadata persistence failed.",
            )

        service = InvoiceDocumentService(session=session, client=client, storage=storage)
        before_count = _document_count(session, record.id)
        first = service.download_documents(invoice_ids=[record.id]).items[0]
        after_first_count = _document_count(session, record.id)
        second = service.download_documents(invoice_ids=[record.id]).items[0]
        after_second_count = _document_count(session, record.id)
        stored_content = storage.read(first.storage_key)
        stored_hash = hashlib.sha256(stored_content).hexdigest()
        checksum_verified = stored_hash == first.content_hash_sha256
        idempotent = (
            second.status == "existing"
            and first.document_id == second.document_id
            and first.content_hash_sha256 == second.content_hash_sha256
            and after_second_count == after_first_count
        )

        return {
            "success": checksum_verified and idempotent,
            "direction": invoice.direction,
            "invoice_identifier_present": True,
            "document_id": first.document_id,
            "storage_identifier_present": bool(first.storage_key),
            "filename": _safe_filename(first.storage_key),
            "content_type": MIME_TYPE_UBL_XML,
            "byte_size": first.content_size_bytes,
            "sha256": first.content_hash_sha256,
            "newly_created": first.status == "downloaded" and after_first_count == before_count + 1,
            "reused": first.status == "existing",
            "idempotency": {
                "ok": idempotent,
                "second_status": second.status,
                "duplicate_document_created": after_second_count > after_first_count,
            },
            "provider_mutation_attempted": False,
            "error": None,
        }
    except (ConnectorError, DocumentDownloadError, DocumentStorageError) as exc:
        return _failure(direction=_display_direction(selection.direction), error=_safe_error_message(exc))
    except Exception:
        return _failure(direction=_display_direction(selection.direction), error=SAFE_FAILURE_MESSAGE)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate Uyumsoft invoice UBL download in strict readonly mode.")
    parser.add_argument("--direction", choices=("inbox",), default="inbox")
    parser.add_argument("--invoice-id", help="Explicit provider invoice identifier to download.")
    parser.add_argument("--from", dest="from_date", help="Inclusive date or ISO datetime for page-size-1 Inbox lookup.")
    parser.add_argument("--to", dest="to_date", help="Inclusive date or ISO datetime for page-size-1 Inbox lookup.")
    parser.add_argument(
        "--date-field",
        choices=("execution", "create"),
        default="execution",
        help="Use invoice execution date or provider create/receipt date filtering for lookup.",
    )
    args = parser.parse_args()
    if args.invoice_id and (args.from_date or args.to_date):
        parser.error("--invoice-id cannot be combined with --from/--to lookup arguments.")
    if args.invoice_id is not None and not args.invoice_id.strip():
        parser.error("--invoice-id must not be empty.")
    if not args.invoice_id and (not args.from_date or not args.to_date):
        parser.error("Provide --invoice-id, or provide both --from and --to for a page-size-1 Inbox lookup.")
    return args


def _selection_from_args(args: argparse.Namespace) -> DownloadSmokeSelection:
    return DownloadSmokeSelection(
        direction=args.direction,
        invoice_id=args.invoice_id.strip() if args.invoice_id else None,
        from_date=_parse_cli_datetime(args.from_date, boundary="start") if args.from_date else None,
        to_date=_parse_cli_datetime(args.to_date, boundary="end") if args.to_date else None,
        date_field=args.date_field,
    )


def _parse_cli_datetime(value: str, *, boundary: str) -> datetime:
    if _is_date_only(value):
        parsed_time = time.max if boundary == "end" else time.min
        return datetime.combine(date.fromisoformat(value), parsed_time, tzinfo=UTC)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _is_date_only(value: str) -> bool:
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return "T" not in value


def _live_download_readonly_errors(settings: Settings) -> list[str]:
    errors: list[str] = []
    if os.getenv(ENABLE_FLAG) != "1":
        errors.append(f"Live invoice download smoke is disabled. Set {ENABLE_FLAG}=1 to run it.")
    if settings.app_env != "development":
        errors.append("Live invoice download smoke requires APP_ENV=development.")
    if settings.uyumsoft_environment != "production":
        errors.append("Live invoice download smoke requires UYUMSOFT_ENVIRONMENT=production.")
    if not settings.live_connector_readonly:
        errors.append("Live invoice download smoke requires LIVE_CONNECTOR_READONLY=true.")
    if settings.production_operations_enabled:
        errors.append("Live invoice download smoke requires PRODUCTION_OPERATIONS_ENABLED=false.")
    if settings.production_approval_ack:
        errors.append("Live invoice download smoke requires empty PRODUCTION_APPROVAL_ACK.")
    errors.extend(runtime_configuration_errors(settings))
    return list(dict.fromkeys(errors))


def _select_invoice(client: UyumsoftSoapClient, selection: DownloadSmokeSelection) -> UyumsoftInvoiceSummary:
    direction = _display_direction(selection.direction)
    if selection.invoice_id is not None:
        return UyumsoftInvoiceSummary(
            invoice_id=selection.invoice_id,
            invoice_number=selection.invoice_id,
            direction=direction,
        )

    if selection.from_date is None or selection.to_date is None:
        raise ConnectorError("Invoice lookup requires --from and --to.")
    if selection.from_date > selection.to_date:
        raise ConnectorError("--from must be before or equal to --to.")

    request = UyumsoftInvoiceListRequest(
        from_date=selection.from_date,
        to_date=selection.to_date,
        page=1,
        page_size=1,
        only_newest_invoices=False,
        date_field=selection.date_field,
    )
    response = client.list_inbox_invoices(request)
    if not response.invoices:
        raise ConnectorError("No Inbox invoice was returned for the provided readonly lookup.")
    return response.invoices[0]


def _find_persisted_invoice(session: Session, invoice: UyumsoftInvoiceSummary) -> UyumsoftInvoiceMetadata | None:
    identity = build_invoice_identity(invoice)
    if invoice.ettn:
        by_ettn = session.scalar(
            select(UyumsoftInvoiceMetadata).where(
                UyumsoftInvoiceMetadata.provider == "uyumsoft",
                UyumsoftInvoiceMetadata.direction == invoice.direction,
                UyumsoftInvoiceMetadata.ettn == invoice.ettn.strip(),
            )
        )
        if by_ettn is not None:
            return by_ettn
    return session.scalar(
        select(UyumsoftInvoiceMetadata).where(
            UyumsoftInvoiceMetadata.provider == "uyumsoft",
            UyumsoftInvoiceMetadata.direction == invoice.direction,
            UyumsoftInvoiceMetadata.identity_key == identity.key,
        )
    )


def _document_count(session: Session, invoice_id: int) -> int:
    return (
        session.scalar(
            select(func.count()).select_from(InvoiceDocument).where(InvoiceDocument.invoice_id == invoice_id)
        )
        or 0
    )


def _invoice_identifier_present(invoice: UyumsoftInvoiceSummary) -> bool:
    return bool(invoice.invoice_id and invoice.invoice_id.strip())


def _safe_filename(storage_key: str) -> str:
    return PurePosixPath(storage_key).name


def _safe_error_message(exc: Exception) -> str:
    safe = getattr(exc, "safe_message", None)
    if isinstance(safe, str) and safe.strip():
        if _looks_like_sensitive_payload(safe) or not _is_known_safe_message(safe):
            return SAFE_FAILURE_MESSAGE
        return safe
    return SAFE_FAILURE_MESSAGE


def _is_known_safe_message(value: str) -> bool:
    return any(value.startswith(prefix) for prefix in SAFE_CONNECTOR_MESSAGE_PREFIXES)


def _looks_like_sensitive_payload(value: str) -> bool:
    lowered = value.lower()
    if "<" in value and ">" in value:
        return True
    return any(marker in lowered for marker in ("password", "api_key", "apikey", "token", "secret"))


def _failure(
    *,
    error: str,
    direction: str | None = None,
    invoice_identifier_present: bool | None = None,
) -> dict[str, Any]:
    return {
        "success": False,
        "direction": direction,
        "invoice_identifier_present": invoice_identifier_present,
        "document_id": None,
        "storage_identifier_present": False,
        "filename": None,
        "content_type": None,
        "byte_size": None,
        "sha256": None,
        "newly_created": False,
        "reused": False,
        "idempotency": {"ok": False, "duplicate_document_created": False},
        "provider_mutation_attempted": False,
        "error": error,
    }


def _display_direction(direction: DirectionArg) -> str:
    return "Inbox" if direction == "inbox" else direction


def _json_dumps(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=True, default=str)


if __name__ == "__main__":
    raise SystemExit(main())
