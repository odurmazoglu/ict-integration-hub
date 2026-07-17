from collections.abc import Mapping
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any

from zeep.helpers import serialize_object

from app.schemas.uyumsoft_invoices import (
    InvoiceDirection,
    UyumsoftInvoiceListRequest,
    UyumsoftInvoiceListResponse,
    UyumsoftInvoiceSummary,
)

ITEM_CONTAINER_KEYS = {
    "data",
    "invoice",
    "invoices",
    "invoiceitem",
    "invoiceitems",
    "invoicelist",
    "items",
    "list",
    "result",
    "results",
    "value",
}

TOTAL_COUNT_KEYS = ("TotalCount", "TotalRecords", "RecordCount", "Count", "total_count")

FIELD_ALIASES = {
    "invoice_id": ("InvoiceId", "InvoiceID", "Id", "ID"),
    "ettn": ("ETTN", "Ettn", "UUID", "Uuid"),
    "invoice_number": ("InvoiceNumber", "InvoiceNo", "InvoiceNoText", "DocumentNo", "Number"),
    "invoice_date": ("InvoiceDate", "IssueDate", "Date", "CreateDate"),
    "sender": ("Sender", "SenderName", "Supplier", "SupplierName"),
    "receiver": ("Receiver", "ReceiverName", "Customer", "CustomerName"),
    "tax_number": ("TaxNumber", "VKN", "TCKN", "SenderTaxNumber", "ReceiverTaxNumber"),
    "currency": ("Currency", "CurrencyCode", "DocumentCurrencyCode"),
    "total_amount": ("TotalAmount", "PayableAmount", "Amount", "InvoiceTotal", "Total"),
    "status": ("Status", "State", "InvoiceStatus"),
}


def build_invoice_list_query(request: UyumsoftInvoiceListRequest) -> dict[str, Any]:
    return {
        "StartDate": request.from_date,
        "EndDate": request.to_date,
        "PageIndex": request.page,
        "PageSize": request.page_size,
    }


def normalize_invoice_list_response(
    raw_response: Any,
    *,
    direction: InvoiceDirection,
    request: UyumsoftInvoiceListRequest,
) -> UyumsoftInvoiceListResponse:
    response_mapping = _to_mapping(raw_response)
    raw_items = _extract_items(raw_response)
    invoices = [_normalize_invoice_summary(raw_item, direction=direction) for raw_item in raw_items]
    return UyumsoftInvoiceListResponse(
        direction=direction,
        page=request.page,
        page_size=request.page_size,
        total_count=_to_int(_first_value(response_mapping, TOTAL_COUNT_KEYS)),
        invoices=invoices,
        extra_fields=_response_extra_fields(response_mapping),
    )


def _normalize_invoice_summary(raw_item: Any, *, direction: InvoiceDirection) -> UyumsoftInvoiceSummary:
    item = _to_mapping(raw_item)
    known_source_keys: set[str] = set()

    values: dict[str, Any] = {}
    for target, aliases in FIELD_ALIASES.items():
        value, source_key = _first_value_with_source(item, aliases)
        if source_key is not None:
            known_source_keys.add(source_key)
        values[target] = value

    return UyumsoftInvoiceSummary(
        invoice_id=_to_optional_str(values["invoice_id"]),
        ettn=_to_optional_str(values["ettn"]),
        invoice_number=_to_optional_str(values["invoice_number"]),
        invoice_date=_to_datetime(values["invoice_date"]),
        sender=_to_optional_str(values["sender"]),
        receiver=_to_optional_str(values["receiver"]),
        tax_number=_to_optional_str(values["tax_number"]),
        currency=_to_optional_str(values["currency"]),
        total_amount=_to_decimal(values["total_amount"]),
        direction=direction,
        status=_to_optional_str(values["status"]),
        extra_fields={key: value for key, value in item.items() if key not in known_source_keys},
    )


def _extract_items(raw_response: Any) -> list[Any]:
    if isinstance(raw_response, list):
        return raw_response

    mapping = _to_mapping(raw_response)
    for key, value in mapping.items():
        if _normalize_key(key) not in ITEM_CONTAINER_KEYS:
            continue
        if isinstance(value, list):
            return value
        if isinstance(value, tuple):
            return list(value)
        nested = _extract_items(value)
        if nested:
            return nested
    return []


def _response_extra_fields(mapping: dict[str, Any]) -> dict[str, Any]:
    excluded_keys = {_normalize_key(key) for key in (*ITEM_CONTAINER_KEYS, *TOTAL_COUNT_KEYS)}
    return {key: value for key, value in mapping.items() if _normalize_key(key) not in excluded_keys}


def _to_mapping(value: Any) -> dict[str, Any]:
    serialized = serialize_object(value)
    if isinstance(serialized, Mapping):
        return dict(serialized)
    if hasattr(serialized, "__dict__"):
        return {key: item for key, item in vars(serialized).items() if not key.startswith("_")}
    return {}


def _first_value(mapping: dict[str, Any], aliases: tuple[str, ...]) -> Any:
    value, _source = _first_value_with_source(mapping, aliases)
    return value


def _first_value_with_source(mapping: dict[str, Any], aliases: tuple[str, ...]) -> tuple[Any, str | None]:
    normalized_aliases = {_normalize_key(alias) for alias in aliases}
    for key, value in mapping.items():
        if _normalize_key(key) in normalized_aliases:
            return value, key
    return None, None


def _normalize_key(value: str) -> str:
    return value.replace("_", "").replace("-", "").lower()


def _to_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _to_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=UTC)
    text = str(value).strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    text = str(value).strip()
    if not text:
        return None
    if "," in text and "." not in text:
        text = text.replace(",", ".")
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
