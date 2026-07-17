from datetime import UTC, datetime
from decimal import Decimal

from app.connectors.uyumsoft.invoice_mapping import build_invoice_list_query_model, normalize_invoice_list_response
from app.schemas.uyumsoft_invoices import UyumsoftInvoiceListRequest


class FakeQueryModel:
    def __call__(self, **kwargs: object) -> dict[str, object]:
        return kwargs


class FakeZeepClient:
    def __init__(self) -> None:
        self.requested_type: str | None = None

    def get_type(self, name: str) -> FakeQueryModel:
        self.requested_type = name
        return FakeQueryModel()


def test_build_invoice_list_query_model_uses_wsdl_fields_for_inbox() -> None:
    request = UyumsoftInvoiceListRequest(
        from_date=datetime(2026, 7, 15, tzinfo=UTC),
        to_date=datetime(2026, 7, 16, tzinfo=UTC),
        page=2,
        page_size=25,
    )
    zeep_client = FakeZeepClient()

    query = build_invoice_list_query_model(zeep_client, request, direction="Inbox")

    assert zeep_client.requested_type == "{http://tempuri.org/}InboxInvoiceListQueryModel"
    assert query == {
        "ExecutionStartDate": datetime(2026, 7, 15, tzinfo=UTC),
        "ExecutionEndDate": datetime(2026, 7, 16, tzinfo=UTC),
        "PageIndex": 2,
        "PageSize": 25,
        "IncludeTagList": False,
        "OnlyNewestInvoices": False,
    }


def test_build_invoice_list_query_model_uses_wsdl_fields_for_outbox() -> None:
    request = UyumsoftInvoiceListRequest(
        from_date=datetime(2026, 7, 15, tzinfo=UTC),
        to_date=datetime(2026, 7, 16, tzinfo=UTC),
        page=1,
        page_size=1,
    )
    zeep_client = FakeZeepClient()

    build_invoice_list_query_model(zeep_client, request, direction="Outbox")

    assert zeep_client.requested_type == "{http://tempuri.org/}OutboxInvoiceListQueryModel"


def test_normalize_invoice_list_response_maps_real_paged_inbox_shape() -> None:
    request = UyumsoftInvoiceListRequest(
        from_date=datetime(2026, 7, 15, tzinfo=UTC),
        to_date=datetime(2026, 7, 16, tzinfo=UTC),
        page=1,
        page_size=10,
    )
    raw_response = {
        "Value": {
            "Items": [
                {
                    "InvoiceId": "ettn-1",
                    "DocumentId": "ABC2026001",
                    "ExecutionDate": "2026-07-16T10:30:00",
                    "TargetTitle": "Receiver B",
                    "TargetTcknVkn": "1234567890",
                    "DocumentCurrencyCode": "TRY",
                    "PayableAmount": Decimal("42.50"),
                    "Status": "Approved",
                    "ProviderSpecific": "kept",
                    "XmlPayload": "<?xml version='1.0'?><Invoice>secret invoice content</Invoice>",
                    "ApiToken": "do-not-keep",
                }
            ],
            "PageIndex": 1,
            "PageSize": 10,
            "TotalCount": 1,
            "TotalPages": 1,
        },
        "IsSucceded": True,
        "Message": None,
    }

    result = normalize_invoice_list_response(raw_response, direction="Inbox", request=request)

    assert result.total_count == 1
    assert result.extra_fields == {"IsSucceded": True, "Message": None}
    invoice = result.invoices[0]
    assert invoice.invoice_id == "ettn-1"
    assert invoice.ettn == "ettn-1"
    assert invoice.invoice_number == "ABC2026001"
    assert invoice.invoice_date == datetime(2026, 7, 16, 10, 30, tzinfo=UTC)
    assert invoice.receiver == "Receiver B"
    assert invoice.tax_number == "1234567890"
    assert invoice.currency == "TRY"
    assert invoice.total_amount == Decimal("42.50")
    assert invoice.direction == "Inbox"
    assert invoice.status == "Approved"
    assert invoice.extra_fields == {
        "ProviderSpecific": "kept",
        "XmlPayload": "<redacted text: 62 chars>",
        "ApiToken": "<redacted>",
    }


def test_normalize_invoice_list_response_maps_real_paged_outbox_shape_with_optional_fields() -> None:
    request = UyumsoftInvoiceListRequest(
        from_date=datetime(2026, 7, 15, tzinfo=UTC),
        to_date=datetime(2026, 7, 16, tzinfo=UTC),
        page=1,
        page_size=10,
    )
    raw_response = {
        "Value": {
            "Items": [
                {
                    "InvoiceId": "out-ettn-1",
                    "DocumentId": "OUT2026001",
                    "ExecutionDate": None,
                    "TargetTitle": None,
                    "TargetTcknVkn": "",
                    "DocumentCurrencyCode": "USD",
                    "PayableAmount": "100,25",
                    "Status": "Sent",
                    "Scenario": "Commercial",
                    "ExtraInformation": "kept",
                }
            ],
            "TotalCount": 1,
        },
        "IsSucceded": True,
    }

    result = normalize_invoice_list_response(raw_response, direction="Outbox", request=request)

    invoice = result.invoices[0]
    assert invoice.invoice_id == "out-ettn-1"
    assert invoice.ettn == "out-ettn-1"
    assert invoice.invoice_number == "OUT2026001"
    assert invoice.invoice_date is None
    assert invoice.receiver is None
    assert invoice.tax_number is None
    assert invoice.currency == "USD"
    assert invoice.total_amount == Decimal("100.25")
    assert invoice.extra_fields == {"Scenario": "Commercial", "ExtraInformation": "kept"}


def test_normalize_invoice_list_response_handles_empty_result_set() -> None:
    request = UyumsoftInvoiceListRequest(
        from_date=datetime(2026, 7, 15, tzinfo=UTC),
        to_date=datetime(2026, 7, 16, tzinfo=UTC),
        page=1,
        page_size=10,
    )

    result = normalize_invoice_list_response(
        {"Value": {"Items": [], "TotalCount": 0}, "IsSucceded": True},
        direction="Inbox",
        request=request,
    )

    assert result.invoices == []
    assert result.total_count == 0


def test_normalize_invoice_list_response_accepts_list_root() -> None:
    request = UyumsoftInvoiceListRequest(
        from_date=datetime(2026, 7, 15, tzinfo=UTC),
        to_date=datetime(2026, 7, 16, tzinfo=UTC),
        page=1,
        page_size=10,
    )

    result = normalize_invoice_list_response([{"InvoiceId": "out-1"}], direction="Outbox", request=request)

    assert result.direction == "Outbox"
    assert result.invoices[0].invoice_id == "out-1"
