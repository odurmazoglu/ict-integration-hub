import logging
from datetime import UTC, datetime
from decimal import Decimal
from time import perf_counter
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.connectors.exceptions import ConnectorError, ConnectorTimeoutError
from app.connectors.odoo.client import OdooJson2Client
from app.models.odoo_draft_invoice import OdooDraftInvoice
from app.schemas.odoo_draft_invoice import OdooDraftInvoiceCreateRequest, OdooDraftInvoiceCreateResponse
from app.schemas.odoo_mapping import OdooInvoiceLinePayload, OdooMappingPreview, OdooTaxCandidate

logger = logging.getLogger(__name__)

STATUS_PENDING = "pending"
STATUS_CREATED = "created"
STATUS_FAILED = "failed"
ODOO_MODEL_ACCOUNT_MOVE = "account.move"


class OdooDraftInvoiceError(Exception):
    error_category = "draft_creation_error"

    def __init__(self, safe_message: str) -> None:
        super().__init__(safe_message)
        self.safe_message = safe_message


class OdooDraftInvoiceValidationError(OdooDraftInvoiceError):
    error_category = "validation_error"


class OdooDraftInvoiceDuplicateInProgressError(OdooDraftInvoiceError):
    error_category = "duplicate_in_progress"


class OdooDraftInvoicePersistenceError(OdooDraftInvoiceError):
    error_category = "persistence_error"


class OdooDraftInvoiceConnectorFailure(OdooDraftInvoiceError):
    error_category = "connector_error"


class OdooDraftInvoiceTimeoutFailure(OdooDraftInvoiceConnectorFailure):
    error_category = "timeout"


class OdooDraftInvoiceService:
    def __init__(self, *, session: Session, client: OdooJson2Client) -> None:
        self._session = session
        self._client = client

    async def create_draft(self, request: OdooDraftInvoiceCreateRequest) -> OdooDraftInvoiceCreateResponse:
        started = perf_counter()
        preview = request.preview
        ettn = _required_ettn(preview)
        existing = self._find_by_ettn(ettn)
        if existing is not None and existing.creation_status == STATUS_CREATED and existing.odoo_move_id is not None:
            _log_completed(
                started=started,
                integration_invoice_id=existing.integration_invoice_id,
                ettn=existing.ettn,
                operation_status="existing",
                odoo_move_id=existing.odoo_move_id,
                attempt_count=existing.attempt_count,
                error_category=None,
            )
            return _response_from_record(existing, creation_status="existing")

        _validate_request(request)
        payload = _account_move_payload(preview)
        record = self._claim_record(
            existing=existing,
            ettn=ettn,
            integration_invoice_id=request.integration_invoice_id,
            invoice_number=preview.invoice.invoice_number,
        )

        try:
            odoo_move_id = await self._client.create_account_move(payload)
        except ConnectorTimeoutError as exc:
            self._mark_failed(record, category="timeout", message=exc.safe_message)
            _log_completed(
                started=started,
                integration_invoice_id=record.integration_invoice_id,
                ettn=record.ettn,
                operation_status=STATUS_FAILED,
                odoo_move_id=None,
                attempt_count=record.attempt_count,
                error_category="timeout",
            )
            raise OdooDraftInvoiceTimeoutFailure(exc.safe_message) from exc
        except ConnectorError as exc:
            self._mark_failed(record, category="connector_error", message=exc.safe_message)
            _log_completed(
                started=started,
                integration_invoice_id=record.integration_invoice_id,
                ettn=record.ettn,
                operation_status=STATUS_FAILED,
                odoo_move_id=None,
                attempt_count=record.attempt_count,
                error_category="connector_error",
            )
            raise OdooDraftInvoiceConnectorFailure(exc.safe_message) from exc

        record.odoo_move_id = odoo_move_id
        record.creation_status = STATUS_CREATED
        record.safe_error_category = None
        record.safe_error_message = None
        record.odoo_created_at = datetime.now(UTC)
        try:
            self._session.flush()
        except SQLAlchemyError as exc:
            raise OdooDraftInvoicePersistenceError("Odoo draft reference persistence failed.") from exc

        _log_completed(
            started=started,
            integration_invoice_id=record.integration_invoice_id,
            ettn=record.ettn,
            operation_status=STATUS_CREATED,
            odoo_move_id=record.odoo_move_id,
            attempt_count=record.attempt_count,
            error_category=None,
        )
        return _response_from_record(record, creation_status="created")

    def _find_by_ettn(self, ettn: str) -> OdooDraftInvoice | None:
        try:
            return self._session.scalar(select(OdooDraftInvoice).where(OdooDraftInvoice.ettn == ettn))
        except SQLAlchemyError as exc:
            raise OdooDraftInvoicePersistenceError("Odoo draft idempotency lookup failed.") from exc

    def _claim_record(
        self,
        *,
        existing: OdooDraftInvoice | None,
        ettn: str,
        integration_invoice_id: int | None,
        invoice_number: str | None,
    ) -> OdooDraftInvoice:
        now = datetime.now(UTC)
        if existing is not None:
            if existing.creation_status == STATUS_PENDING:
                raise OdooDraftInvoiceDuplicateInProgressError("Odoo draft creation is already in progress.")
            existing.integration_invoice_id = integration_invoice_id or existing.integration_invoice_id
            existing.invoice_number = invoice_number or existing.invoice_number
            existing.creation_status = STATUS_PENDING
            existing.safe_error_category = None
            existing.safe_error_message = None
            existing.attempt_count += 1
            existing.last_attempt_at = now
            record = existing
        else:
            record = OdooDraftInvoice(
                integration_invoice_id=integration_invoice_id,
                ettn=ettn,
                invoice_number=invoice_number,
                odoo_model=ODOO_MODEL_ACCOUNT_MOVE,
                creation_status=STATUS_PENDING,
                attempt_count=1,
                last_attempt_at=now,
            )
            self._session.add(record)
        try:
            self._session.flush()
        except IntegrityError as exc:
            raise OdooDraftInvoiceDuplicateInProgressError("Odoo draft creation is already in progress.") from exc
        except SQLAlchemyError as exc:
            raise OdooDraftInvoicePersistenceError("Odoo draft creation claim could not be persisted.") from exc
        return record

    def _mark_failed(self, record: OdooDraftInvoice, *, category: str, message: str) -> None:
        record.creation_status = STATUS_FAILED
        record.safe_error_category = category
        record.safe_error_message = message[:512]
        try:
            self._session.flush()
        except SQLAlchemyError as exc:
            raise OdooDraftInvoicePersistenceError("Odoo draft failure status could not be persisted.") from exc


def _validate_request(request: OdooDraftInvoiceCreateRequest) -> None:
    if not request.confirm_create_draft:
        raise OdooDraftInvoiceValidationError("confirm_create_draft=true is required.")
    preview = request.preview
    if preview.mapping_status != "ready" or preview.missing_fields:
        raise OdooDraftInvoiceValidationError("Mapping preview is not ready for Odoo draft creation.")
    missing = _missing_odoo_identifiers(preview)
    if missing:
        missing_text = ", ".join(missing)
        raise OdooDraftInvoiceValidationError(f"Reviewed mapping preview is missing Odoo identifiers: {missing_text}.")


def _missing_odoo_identifiers(preview: OdooMappingPreview) -> list[str]:
    missing: list[str] = []
    if preview.invoice.partner is None or preview.invoice.partner.odoo_id is None:
        missing.append("partner.odoo_id")
    if preview.invoice.currency_id is None:
        missing.append("currency_id")
    if preview.invoice.journal is None or preview.invoice.journal.odoo_id is None:
        missing.append("journal.odoo_id")
    for index, line in enumerate(preview.lines):
        if line.product is None or line.product.odoo_id is None:
            missing.append(f"lines[{index}].product.odoo_id")
        if not _tax_ids(line.taxes):
            missing.append(f"lines[{index}].taxes.odoo_id")
    return missing


def _required_ettn(preview: OdooMappingPreview) -> str:
    ettn = preview.invoice.ettn
    if ettn is None or not ettn.strip():
        raise OdooDraftInvoiceValidationError("ETTN is required for Odoo draft idempotency.")
    return ettn.strip()


def _account_move_payload(preview: OdooMappingPreview) -> dict[str, Any]:
    invoice = preview.invoice
    payload: dict[str, Any] = {
        "move_type": "in_invoice",
        "partner_id": invoice.partner.odoo_id if invoice.partner else None,
        "currency_id": invoice.currency_id,
        "journal_id": invoice.journal.odoo_id if invoice.journal else None,
        "invoice_date": invoice.invoice_date.isoformat() if invoice.invoice_date else None,
        "ref": _reference(invoice.references),
        "narration": _notes(invoice.notes),
        "invoice_line_ids": [[0, 0, _line_payload(line)] for line in preview.lines],
    }
    if invoice.payment_term_id is not None:
        payload["invoice_payment_term_id"] = invoice.payment_term_id
    return {key: value for key, value in payload.items() if value is not None}


def _line_payload(line: OdooInvoiceLinePayload) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "sequence": line.sequence,
        "name": line.description,
        "product_id": line.product.odoo_id if line.product else None,
        "quantity": _decimal_text(line.quantity if line.quantity is not None else Decimal("1")),
        "price_unit": _decimal_text(line.unit_price if line.unit_price is not None else line.line_extension_amount),
        "tax_ids": [[6, 0, _tax_ids(line.taxes)]],
    }
    return {key: value for key, value in payload.items() if value is not None}


def _tax_ids(taxes: list[OdooTaxCandidate]) -> list[int]:
    return [tax.odoo_id for tax in taxes if tax.odoo_id is not None]


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _reference(references: list[str]) -> str | None:
    safe_references = [reference.strip() for reference in references if reference and reference.strip()]
    return " / ".join(safe_references) or None


def _notes(notes: list[str]) -> str | None:
    safe_notes = [note.strip() for note in notes if note and note.strip()]
    return "\n".join(safe_notes) or None


def _response_from_record(record: OdooDraftInvoice, *, creation_status: str) -> OdooDraftInvoiceCreateResponse:
    return OdooDraftInvoiceCreateResponse(
        integration_invoice_id=record.integration_invoice_id,
        ettn=record.ettn,
        odoo_model="account.move",
        odoo_move_id=record.odoo_move_id,
        creation_status=creation_status,
        safe_message=record.safe_error_message,
        created_at=record.odoo_created_at,
    )


def _log_completed(
    *,
    started: float,
    integration_invoice_id: int | None,
    ettn: str,
    operation_status: str,
    odoo_move_id: int | None,
    attempt_count: int,
    error_category: str | None,
) -> None:
    extra: dict[str, Any] = {
        "integration_invoice_id": integration_invoice_id,
        "ettn": ettn,
        "operation_status": operation_status,
        "odoo_move_id": odoo_move_id,
        "duration_ms": round((perf_counter() - started) * 1000, 2),
        "attempt_count": attempt_count,
    }
    if error_category is not None:
        extra["error_category"] = error_category
    logger.info("odoo_draft_invoice_creation_completed", extra=extra)
