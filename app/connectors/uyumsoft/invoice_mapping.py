from collections.abc import Mapping
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any

from zeep.helpers import serialize_object

from app.connectors.exceptions import ConnectorError
from app.schemas.uyumsoft_invoices import (
    InvoiceDirection,
    UyumsoftInvoiceListRequest,
    UyumsoftInvoiceListResponse,
    UyumsoftInvoiceSummary,
)

UYUMSOFT_NAMESPACE = "http://tempuri.org/"
QUERY_MODEL_NAMES: dict[InvoiceDirection, str] = {
    "Inbox": "InboxInvoiceListQueryModel",
    "Outbox": "OutboxInvoiceListQueryModel",
}
REQUIRED_QUERY_FIELDS = frozenset({"ExecutionStartDate", "ExecutionEndDate", "PageIndex", "PageSize"})

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
PAGED_RESPONSE_KEYS = ("Value", "value", "Result", "result")
SENSITIVE_FIELD_MARKERS = ("password", "secret", "token", "apikey", "api_key", "authorization", "credential")
MAX_EXTRA_TEXT_LENGTH = 500
REDACTED_TEXT_MARKERS = ("<?xml", "<Invoice", "<Despatch", "<CreditNote", "<ApplicationResponse", "UBLExtensions")

FIELD_ALIASES = {
    "invoice_id": ("InvoiceId", "InvoiceID", "Id", "ID"),
    "ettn": ("ETTN", "Ettn", "UUID", "Uuid", "InvoiceId", "InvoiceID"),
    "invoice_number": ("InvoiceNumber", "InvoiceNo", "InvoiceNoText", "DocumentNo", "DocumentId", "Number"),
    "invoice_date": ("InvoiceDate", "IssueDate", "ExecutionDate", "Date", "CreateDate", "CreateDateUtc"),
    "sender": ("Sender", "SenderName", "Supplier", "SupplierName"),
    "receiver": ("Receiver", "ReceiverName", "Customer", "CustomerName", "TargetTitle"),
    "tax_number": ("TaxNumber", "VKN", "TCKN", "SenderTaxNumber", "ReceiverTaxNumber", "TargetTcknVkn"),
    "currency": ("Currency", "CurrencyCode", "DocumentCurrencyCode"),
    "total_amount": ("TotalAmount", "PayableAmount", "Amount", "InvoiceTotal", "Total"),
    "status": ("Status", "State", "InvoiceStatus"),
}


def build_invoice_list_query_model(
    zeep_client: Any,
    request: UyumsoftInvoiceListRequest,
    *,
    direction: InvoiceDirection,
) -> Any:
    query_model_name = QUERY_MODEL_NAMES[direction]
    query_model = zeep_client.get_type(f"{{{UYUMSOFT_NAMESPACE}}}{query_model_name}")
    supported_fields = get_supported_zeep_fields(query_model)
    missing_required_fields = sorted(REQUIRED_QUERY_FIELDS - supported_fields)
    if missing_required_fields:
        missing = ", ".join(missing_required_fields)
        raise ConnectorError(f"Uyumsoft {query_model_name} WSDL query model is missing required fields: {missing}.")

    candidate_values: dict[str, Any] = {
        "ExecutionStartDate": request.from_date,
        "ExecutionEndDate": request.to_date,
        "PageIndex": request.page,
        "PageSize": request.page_size,
        "IncludeTagList": False,
    }
    if direction == "Inbox":
        candidate_values["OnlyNewestInvoices"] = False
    query_values = {key: value for key, value in candidate_values.items() if key in supported_fields}
    return query_model(
        **query_values,
    )


def get_supported_zeep_fields(query_model: Any) -> set[str]:
    fields: set[str] = set()
    _collect_zeep_fields(query_model, fields, set())
    return fields


def normalize_invoice_list_response(
    raw_response: Any,
    *,
    direction: InvoiceDirection,
    request: UyumsoftInvoiceListRequest,
) -> UyumsoftInvoiceListResponse:
    response_mapping = _to_mapping(raw_response)
    paged_response = _extract_paged_response(response_mapping)
    raw_items = _extract_items(raw_response)
    invoices = [_normalize_invoice_summary(raw_item, direction=direction) for raw_item in raw_items]
    return UyumsoftInvoiceListResponse(
        direction=direction,
        page=request.page,
        page_size=request.page_size,
        total_count=_first_int(
            _first_value(response_mapping, TOTAL_COUNT_KEYS),
            _first_value(paged_response, TOTAL_COUNT_KEYS),
        ),
        invoices=invoices,
        extra_fields=_response_extra_fields(response_mapping),
    )


def is_unsuccessful_response(raw_response: Any) -> bool:
    response_mapping = _to_mapping(raw_response)
    return response_mapping.get("IsSucceded") is False


def response_message(raw_response: Any) -> str | None:
    return _to_optional_str(_to_mapping(raw_response).get("Message"))


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
        extra_fields={
            key: _sanitize_extra_value(key, value) for key, value in item.items() if key not in known_source_keys
        },
    )


def _extract_items(raw_response: Any) -> list[Any]:
    if isinstance(raw_response, list):
        return raw_response

    mapping = _to_mapping(raw_response)
    paged_response = _extract_paged_response(mapping)
    if paged_response:
        items = paged_response.get("Items")
        if isinstance(items, list):
            return items
        if isinstance(items, tuple):
            return list(items)
        if items is None:
            return []

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
    return {
        key: _sanitize_extra_value(key, value)
        for key, value in mapping.items()
        if _normalize_key(key) not in excluded_keys
    }


def _extract_paged_response(mapping: dict[str, Any]) -> dict[str, Any]:
    for key in PAGED_RESPONSE_KEYS:
        value = mapping.get(key)
        nested_mapping = _to_mapping(value)
        if nested_mapping:
            return nested_mapping
    return {}


def _collect_zeep_fields(value: Any, fields: set[str], visited: set[int]) -> None:
    if value is None:
        return
    value_id = id(value)
    if value_id in visited:
        return
    visited.add(value_id)

    for attr in ("elements", "elements_nested"):
        raw_elements = getattr(value, attr, ())
        if callable(raw_elements):
            raw_elements = raw_elements()
        for item in raw_elements or ():
            if isinstance(item, tuple) and item:
                item_name = item[0]
                if _is_supported_field_name(item_name):
                    fields.add(item_name)
                if len(item) > 1:
                    _collect_zeep_fields(item[1], fields, visited)
                continue
            item_name = getattr(item, "name", None)
            if _is_supported_field_name(item_name):
                fields.add(item_name)
            _collect_zeep_fields(item, fields, visited)

    for attr in ("attributes", "_attributes", "_attributes_unwrapped"):
        raw_attributes = getattr(value, attr, ())
        if callable(raw_attributes):
            raw_attributes = raw_attributes()
        for item in raw_attributes or ():
            if isinstance(item, tuple) and item:
                item_name = item[0]
                if _is_supported_field_name(item_name):
                    fields.add(item_name)
                if len(item) > 1:
                    _collect_zeep_fields(item[1], fields, visited)
                continue
            item_name = getattr(item, "name", None)
            if _is_supported_field_name(item_name):
                fields.add(item_name)
            _collect_zeep_fields(item, fields, visited)

    for attr in ("_xsd_type", "type", "_element"):
        nested = getattr(value, attr, None)
        if nested is not value:
            _collect_zeep_fields(nested, fields, visited)


def _is_supported_field_name(value: object) -> bool:
    return isinstance(value, str) and bool(value) and not value.startswith("_")


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
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
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


def _first_int(*values: Any) -> int | None:
    for value in values:
        parsed = _to_int(value)
        if parsed is not None:
            return parsed
    return None


def _sanitize_extra_value(key: str, value: Any) -> Any:
    normalized_key = _normalize_key(key)
    if any(marker in normalized_key for marker in SENSITIVE_FIELD_MARKERS):
        return "<redacted>"
    if isinstance(value, bytes):
        return f"<redacted binary: {len(value)} bytes>"
    if isinstance(value, str):
        stripped = value.strip()
        if _looks_like_invoice_payload(stripped) or len(stripped) > MAX_EXTRA_TEXT_LENGTH:
            return f"<redacted text: {len(value)} chars>"
        return value
    if isinstance(value, Mapping):
        return {str(item_key): _sanitize_extra_value(str(item_key), item) for item_key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_extra_value(key, item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_extra_value(key, item) for item in value]
    return value


def _looks_like_invoice_payload(value: str) -> bool:
    return any(marker in value for marker in REDACTED_TEXT_MARKERS)
