"""PR R: Uyumsoft outbox exact-number lookup (query building, field semantics, adapters)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import SecretStr

from app.application.return_reference import (
    OutboxLookupUnavailableError,
    ReturnReferenceLookupUnavailableError,
    ReturnReferenceSourceDocumentError,
)
from app.composition.return_reference import (
    OdooCompanyTaxIdentityResolver,
    StoredSourceDocumentReader,
    UyumsoftOutboxInvoiceLookup,
    build_lookup_return_reference_use_case,
)
from app.connectors.exceptions import ConnectorError, ConnectorTimeoutError
from app.connectors.uyumsoft.client import READ_ONLY_OPERATIONS, UyumsoftSoapClient
from app.connectors.uyumsoft.invoice_mapping import (
    UYUMSOFT_LOCAL_TIMEZONE,
    build_invoice_list_query_model,
    normalize_outbox_lookup_response,
)
from app.core.config import Settings
from app.erp.exceptions import ErpRepositoryError
from app.erp.models import Company
from app.schemas.uyumsoft_invoices import (
    UyumsoftInvoiceListRequest,
    UyumsoftOutboxInvoiceRecord,
    UyumsoftOutboxLookupResponse,
)
from app.services.document_storage import DocumentStorageError
from tests.unit.pr_r_fixtures import CUSTOMER_VKN, ORIGINAL_ETTN, REFERENCE_NUMBER

OUTBOX_FIELDS = {
    "ExecutionStartDate",
    "ExecutionEndDate",
    "PageIndex",
    "PageSize",
    "IncludeTagList",
    "InvoiceNumbers",
    "InvoiceIds",
    "IsArchived",
}


class _QueryModel:
    def __init__(self, fields: set[str]) -> None:
        self.elements = [(field, object()) for field in fields]

    def __call__(self, **kwargs: Any) -> dict[str, Any]:
        return kwargs


class _ZeepClient:
    def __init__(self, *, fields: set[str] = OUTBOX_FIELDS, response: Any = None) -> None:
        self._fields = fields
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.response = response if response is not None else _raw_response([_raw_item()])
        self.service = self

    def get_type(self, name: str) -> _QueryModel:
        assert name == "{http://tempuri.org/}OutboxInvoiceListQueryModel"
        return _QueryModel(self._fields)

    def GetOutboxInvoiceList(self, query: dict[str, Any]) -> Any:  # noqa: N802 - SOAP operation name
        self.calls.append(("GetOutboxInvoiceList", query))
        return self.response


def _request(**overrides: Any) -> UyumsoftInvoiceListRequest:
    values: dict[str, Any] = {
        "from_date": datetime(2026, 9, 9, tzinfo=UTC),
        "to_date": datetime(2026, 9, 11, tzinfo=UTC),
        "page": 1,
        "page_size": 10,
    }
    values.update(overrides)
    return UyumsoftInvoiceListRequest(**values)


def _raw_item(**overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "InvoiceId": REFERENCE_NUMBER,
        "DocumentId": ORIGINAL_ETTN,
        "InvoiceTipType": "Sales",
        "Type": "ComercialInvoice",
        "Status": "Approved",
        "TargetTcknVkn": CUSTOMER_VKN,
        "DocumentCurrencyCode": "TRY",
        "ExecutionDate": datetime(2026, 9, 10, 14, 0),
        "IsArchived": False,
    }
    item.update(overrides)
    return item


def _raw_response(items: list[dict[str, Any]], *, total: int | None = None) -> dict[str, Any]:
    return {
        "IsSucceded": True,
        "Message": None,
        "Value": {"Items": items, "TotalCount": len(items) if total is None else total, "PageIndex": 0},
    }


def _client(zeep_client: _ZeepClient) -> UyumsoftSoapClient:
    return UyumsoftSoapClient(
        wsdl_url="https://uyumsoft.test/wsdl",
        username="user",
        password=SecretStr("pass"),
        timeout_seconds=1,
        retry_attempts=1,
        retry_backoff_seconds=0,
        zeep_client=zeep_client,
    )


# ------------------------------------------------------------------ query building


def test_query_includes_invoice_numbers_and_is_archived_when_requested() -> None:
    query = build_invoice_list_query_model(
        _ZeepClient(), _request(invoice_numbers=(REFERENCE_NUMBER,), is_archived=True), direction="Outbox"
    )

    assert query["InvoiceNumbers"] == [REFERENCE_NUMBER]
    assert query["IsArchived"] is True


def test_is_archived_false_is_sent_not_dropped() -> None:
    query = build_invoice_list_query_model(
        _ZeepClient(), _request(invoice_numbers=(REFERENCE_NUMBER,), is_archived=False), direction="Outbox"
    )

    assert query["IsArchived"] is False


def test_default_request_is_unchanged_by_the_new_filters() -> None:
    query = build_invoice_list_query_model(_ZeepClient(), _request(), direction="Outbox")

    assert query == {
        "ExecutionStartDate": datetime(2026, 9, 9, tzinfo=UTC),
        "ExecutionEndDate": datetime(2026, 9, 11, tzinfo=UTC),
        "PageIndex": 1,
        "PageSize": 10,
        "IncludeTagList": False,
    }


@pytest.mark.parametrize(
    ("missing", "overrides"),
    [
        ("InvoiceNumbers", {"invoice_numbers": (REFERENCE_NUMBER,)}),
        ("IsArchived", {"is_archived": False}),
    ],
)
def test_requested_filter_missing_from_wsdl_fails_closed(missing: str, overrides: dict[str, Any]) -> None:
    with pytest.raises(ConnectorError, match=missing):
        build_invoice_list_query_model(
            _ZeepClient(fields=OUTBOX_FIELDS - {missing}), _request(**overrides), direction="Outbox"
        )


# ------------------------------------------------------------------ field semantics


def test_outbox_record_uses_verified_field_semantics() -> None:
    response = normalize_outbox_lookup_response(_raw_response([_raw_item()]), page_size=10)

    assert response.total_count == 1
    (record,) = response.invoices
    assert record == UyumsoftOutboxInvoiceRecord(
        invoice_number=REFERENCE_NUMBER,
        ettn=ORIGINAL_ETTN,
        invoice_type="Sales",
        scenario="ComercialInvoice",
        status="Approved",
        target_tax_number=CUSTOMER_VKN,
        currency="TRY",
        issue_date=date(2026, 9, 10),
        is_archived=False,
    )


def test_absent_total_count_is_unknown_not_zero() -> None:
    raw = {"IsSucceded": True, "Message": None, "Value": {"Items": [_raw_item()], "PageIndex": 0}}

    response = normalize_outbox_lookup_response(raw, page_size=1)

    assert response.total_count is None
    assert (response.page_size, len(response.invoices)) == (1, 1)


def test_naive_provider_timestamp_keeps_its_local_calendar_date() -> None:
    raw = _raw_response([_raw_item(ExecutionDate=datetime(2026, 9, 10, 23, 30))])

    assert normalize_outbox_lookup_response(raw, page_size=10).invoices[0].issue_date == date(2026, 9, 10)


def test_aware_provider_timestamp_is_converted_to_turkish_local_date() -> None:
    raw = _raw_response([_raw_item(ExecutionDate=datetime(2026, 9, 9, 22, 30, tzinfo=UTC))])

    assert normalize_outbox_lookup_response(raw, page_size=10).invoices[0].issue_date == date(2026, 9, 10)


def test_unparseable_date_and_non_boolean_archive_flag_become_none() -> None:
    raw = _raw_response([_raw_item(ExecutionDate="not-a-date", IsArchived="yes")])

    record = normalize_outbox_lookup_response(raw, page_size=10).invoices[0]

    assert record.issue_date is None
    assert record.is_archived is None


# ------------------------------------------------------------------ client


def test_client_lookup_uses_only_get_outbox_invoice_list_with_exact_filter() -> None:
    zeep_client = _ZeepClient()

    response = _client(zeep_client).lookup_outbox_invoices(
        _request(invoice_numbers=(REFERENCE_NUMBER,), is_archived=False)
    )

    assert [call[0] for call in zeep_client.calls] == ["GetOutboxInvoiceList"]
    assert zeep_client.calls[0][1]["InvoiceNumbers"] == [REFERENCE_NUMBER]
    assert response.invoices[0].invoice_number == REFERENCE_NUMBER


@pytest.mark.parametrize("numbers", [(), ("  ",)])
def test_client_lookup_refuses_requests_without_exact_numbers(numbers: tuple[str, ...]) -> None:
    zeep_client = _ZeepClient()

    with pytest.raises(ConnectorError, match="exact invoice numbers"):
        _client(zeep_client).lookup_outbox_invoices(_request(invoice_numbers=numbers))
    assert zeep_client.calls == []


def test_client_lookup_raises_on_unsuccessful_provider_response() -> None:
    zeep_client = _ZeepClient(response={"IsSucceded": False, "Message": "provider refused"})

    with pytest.raises(ConnectorError, match="provider refused"):
        _client(zeep_client).lookup_outbox_invoices(_request(invoice_numbers=(REFERENCE_NUMBER,)))


def test_read_only_operation_allowlist_is_unchanged() -> None:
    assert READ_ONLY_OPERATIONS == frozenset(
        {
            "TestConnection",
            "WhoAmI",
            "GetSystemDate",
            "GetInboxInvoiceList",
            "GetOutboxInvoiceList",
            "GetInboxInvoiceData",
            "GetOutboxInvoiceData",
        }
    )


# ------------------------------------------------------------------ composition adapters


class _RecordingLookupClient:
    def __init__(self, result: UyumsoftOutboxLookupResponse | Exception) -> None:
        self.result = result
        self.requests: list[UyumsoftInvoiceListRequest] = []

    def lookup_outbox_invoices(self, request: UyumsoftInvoiceListRequest) -> UyumsoftOutboxLookupResponse:
        self.requests.append(request)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_adapter_builds_a_narrow_exact_request_around_the_reference_date() -> None:
    record = UyumsoftOutboxInvoiceRecord(invoice_number=REFERENCE_NUMBER, ettn=ORIGINAL_ETTN, target_tax_number="1")
    client = _RecordingLookupClient(UyumsoftOutboxLookupResponse(page_size=10, total_count=1, invoices=[record]))

    page = UyumsoftOutboxInvoiceLookup(client).find_by_invoice_number(
        REFERENCE_NUMBER, issue_date=date(2026, 9, 10), archived=True
    )

    (request,) = client.requests
    assert request.invoice_numbers == (REFERENCE_NUMBER,)
    assert request.is_archived is True
    assert (request.page, request.page_size) == (1, 10)
    assert request.from_date == datetime.combine(date(2026, 9, 9), time.min, tzinfo=UYUMSOFT_LOCAL_TIMEZONE)
    assert request.to_date == datetime.combine(date(2026, 9, 11), time.max, tzinfo=UYUMSOFT_LOCAL_TIMEZONE)
    assert page.provider_total == 1
    assert page.page_size == 10
    assert page.candidates[0].invoice_number == REFERENCE_NUMBER
    assert page.candidates[0].ettn == ORIGINAL_ETTN
    assert page.candidates[0].buyer_tax_number == "1"


def test_adapter_keeps_a_missing_provider_total_unknown_and_reports_its_page_size() -> None:
    client = _RecordingLookupClient(UyumsoftOutboxLookupResponse(page_size=3, total_count=None, invoices=[]))

    page = UyumsoftOutboxInvoiceLookup(client, page_size=3).find_by_invoice_number(
        REFERENCE_NUMBER, issue_date=date(2026, 9, 10), archived=False
    )

    assert (page.provider_total, page.page_size) == (None, 3)
    assert client.requests[0].page_size == 3


@pytest.mark.parametrize("error", [ConnectorError("soap fault"), ConnectorTimeoutError("timeout")])
def test_adapter_translates_provider_failures(error: ConnectorError) -> None:
    lookup = UyumsoftOutboxInvoiceLookup(_RecordingLookupClient(error))

    with pytest.raises(OutboxLookupUnavailableError):
        lookup.find_by_invoice_number(REFERENCE_NUMBER, issue_date=date(2026, 9, 10), archived=False)


class _CompanyRepository:
    def __init__(self, result: tuple[Company, ...] | Exception) -> None:
        self.result = result

    def find_by_tax_number(self, tax_number: str) -> tuple[Company, ...]:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_company_resolver_returns_matching_ids() -> None:
    resolver = OdooCompanyTaxIdentityResolver(_CompanyRepository((Company(id=7, name="c", tax_number="x"),)))

    assert resolver.company_ids_for_tax_number("x") == (7,)


def test_company_resolver_translates_erp_failure() -> None:
    resolver = OdooCompanyTaxIdentityResolver(_CompanyRepository(ErpRepositoryError("down")))

    with pytest.raises(ReturnReferenceLookupUnavailableError):
        resolver.company_ids_for_tax_number("x")


class _Storage:
    backend_name = "test"

    def read(self, storage_key: str) -> bytes:
        raise DocumentStorageError("missing")


def test_document_reader_translates_storage_failure() -> None:
    with pytest.raises(ReturnReferenceSourceDocumentError):
        StoredSourceDocumentReader(_Storage()).read("key")


# ------------------------------------------------------------------ outbox account ownership wiring


def test_outbox_account_company_is_unbound_by_default() -> None:
    assert Settings.model_fields["uyumsoft_account_company_id"].default is None


@pytest.mark.parametrize("value", [0, -1])
def test_outbox_account_company_must_be_positive(value: int) -> None:
    with pytest.raises(ValueError):
        Settings(uyumsoft_account_company_id=value)


@pytest.mark.parametrize("value", [None, 7])
def test_composition_passes_the_configured_outbox_account_company(value: int | None) -> None:
    settings = Settings().model_copy(update={"uyumsoft_account_company_id": value})

    use_case = build_lookup_return_reference_use_case(
        session=MagicMock(),
        settings=settings,
        uyumsoft_client=_RecordingLookupClient(ConnectorError("unused")),
        odoo_client=MagicMock(),
        storage=_Storage(),
    )

    assert use_case._outbox_account_company_id == value
