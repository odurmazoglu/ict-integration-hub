import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.uyumsoft_invoice import UyumsoftInvoiceMetadata
from app.schemas.uyumsoft_invoices import UyumsoftInvoiceSummary

DEFAULT_PROVIDER = "uyumsoft"


@dataclass(frozen=True)
class InvoicePersistenceResult:
    created: int = 0
    updated: int = 0
    skipped: int = 0

    def add(self, other: "InvoicePersistenceResult") -> "InvoicePersistenceResult":
        return InvoicePersistenceResult(
            created=self.created + other.created,
            updated=self.updated + other.updated,
            skipped=self.skipped + other.skipped,
        )


@dataclass(frozen=True)
class InvoiceIdentity:
    strategy: str
    key: str


class InvoicePersistenceService:
    def __init__(self, session: Session, *, provider: str = DEFAULT_PROVIDER) -> None:
        self._session = session
        self._provider = provider

    def persist_invoices(
        self,
        invoices: list[UyumsoftInvoiceSummary],
        *,
        seen_at: datetime | None = None,
    ) -> InvoicePersistenceResult:
        result = InvoicePersistenceResult()
        effective_seen_at = _aware_datetime(seen_at or datetime.now(UTC))
        for invoice in invoices:
            result = result.add(self.persist_invoice(invoice, seen_at=effective_seen_at))
        return result

    def persist_invoice(
        self,
        invoice: UyumsoftInvoiceSummary,
        *,
        seen_at: datetime | None = None,
    ) -> InvoicePersistenceResult:
        effective_seen_at = _aware_datetime(seen_at or datetime.now(UTC))
        identity = build_invoice_identity(invoice)
        existing = self._find_existing(invoice, identity)
        if existing is not None:
            return self._update_existing(existing, invoice, identity=identity, seen_at=effective_seen_at)

        record = _build_record(
            invoice,
            provider=self._provider,
            identity=identity,
            seen_at=effective_seen_at,
        )
        try:
            with self._session.begin_nested():
                self._session.add(record)
                self._session.flush()
        except IntegrityError:
            concurrent_record = self._find_existing(invoice, identity)
            if concurrent_record is None:
                raise
            return self._update_existing(concurrent_record, invoice, identity=identity, seen_at=effective_seen_at)
        return InvoicePersistenceResult(created=1)

    def _find_existing(
        self,
        invoice: UyumsoftInvoiceSummary,
        identity: InvoiceIdentity,
    ) -> UyumsoftInvoiceMetadata | None:
        if invoice.ettn:
            by_ettn = self._session.scalar(
                select(UyumsoftInvoiceMetadata).where(
                    UyumsoftInvoiceMetadata.provider == self._provider,
                    UyumsoftInvoiceMetadata.direction == invoice.direction,
                    UyumsoftInvoiceMetadata.ettn == invoice.ettn,
                )
            )
            if by_ettn is not None:
                return by_ettn
        return self._session.scalar(
            select(UyumsoftInvoiceMetadata).where(
                UyumsoftInvoiceMetadata.provider == self._provider,
                UyumsoftInvoiceMetadata.direction == invoice.direction,
                UyumsoftInvoiceMetadata.identity_key == identity.key,
            )
        )

    def _update_existing(
        self,
        record: UyumsoftInvoiceMetadata,
        invoice: UyumsoftInvoiceSummary,
        *,
        identity: InvoiceIdentity,
        seen_at: datetime,
    ) -> InvoicePersistenceResult:
        updates = _record_values(invoice, provider=self._provider, identity=identity)
        changed = False
        for key, value in updates.items():
            if getattr(record, key) != value:
                setattr(record, key, value)
                changed = True
        record.last_seen_at = seen_at
        self._session.flush()
        if changed:
            return InvoicePersistenceResult(updated=1)
        return InvoicePersistenceResult(skipped=1)


def build_invoice_identity(invoice: UyumsoftInvoiceSummary) -> InvoiceIdentity:
    ettn = _normalized_text(invoice.ettn)
    if ettn is not None:
        return InvoiceIdentity(strategy="ettn", key=f"ettn:{ettn}")

    fallback_parts = {
        "currency": _normalized_text(invoice.currency),
        "direction": invoice.direction,
        "invoice_date": _identity_datetime(invoice.invoice_date),
        "invoice_number": _normalized_text(invoice.invoice_number),
        "provider_invoice_id": _normalized_text(invoice.invoice_id),
        "tax_number": _normalized_text(invoice.tax_number),
        "total_amount": _identity_decimal(invoice.total_amount),
    }
    canonical = json.dumps(fallback_parts, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return InvoiceIdentity(strategy="fallback_v1", key=f"fallback_v1:{digest}")


def _build_record(
    invoice: UyumsoftInvoiceSummary,
    *,
    provider: str,
    identity: InvoiceIdentity,
    seen_at: datetime,
) -> UyumsoftInvoiceMetadata:
    return UyumsoftInvoiceMetadata(
        **_record_values(invoice, provider=provider, identity=identity),
        first_seen_at=seen_at,
        last_seen_at=seen_at,
    )


def _record_values(
    invoice: UyumsoftInvoiceSummary,
    *,
    provider: str,
    identity: InvoiceIdentity,
) -> dict[str, Any]:
    sender_tax_number, receiver_tax_number = _split_tax_numbers(invoice)
    return {
        "provider": provider,
        "direction": invoice.direction,
        "provider_invoice_id": invoice.invoice_id,
        "ettn": _normalized_text(invoice.ettn),
        "identity_key": identity.key,
        "identity_strategy": identity.strategy,
        "invoice_number": invoice.invoice_number,
        "invoice_date": _aware_datetime(invoice.invoice_date) if invoice.invoice_date else None,
        "sender_name": invoice.sender,
        "sender_tax_number": sender_tax_number,
        "receiver_name": invoice.receiver,
        "receiver_tax_number": receiver_tax_number,
        "currency": invoice.currency,
        "total_amount": invoice.total_amount,
        "provider_status": invoice.status,
        "raw_metadata": _json_safe(invoice.model_dump(mode="python")),
    }


def _split_tax_numbers(invoice: UyumsoftInvoiceSummary) -> tuple[str | None, str | None]:
    sender_tax_number = _first_extra_text(invoice.extra_fields, "SenderTaxNumber", "SenderTcknVkn", "SourceTcknVkn")
    receiver_tax_number = _first_extra_text(
        invoice.extra_fields,
        "ReceiverTaxNumber",
        "ReceiverTcknVkn",
        "TargetTcknVkn",
    )
    if invoice.direction == "Inbox" and sender_tax_number is None:
        sender_tax_number = invoice.tax_number
    if invoice.direction == "Outbox" and receiver_tax_number is None:
        receiver_tax_number = invoice.tax_number
    return sender_tax_number, receiver_tax_number


def _first_extra_text(extra_fields: dict[str, Any], *keys: str) -> str | None:
    normalized = {_normalize_key(key): key for key in extra_fields}
    for key in keys:
        actual_key = normalized.get(_normalize_key(key))
        if actual_key is None:
            continue
        return _normalized_text(extra_fields.get(actual_key))
    return None


def _normalized_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_key(value: str) -> str:
    return value.replace("_", "").replace("-", "").lower()


def _identity_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _aware_datetime(value).astimezone(UTC).isoformat()


def _identity_decimal(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return str(value.normalize())


def _aware_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return _aware_datetime(value).isoformat()
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=UTC).isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    return value
