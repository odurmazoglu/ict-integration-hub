from datetime import UTC, datetime
from decimal import Decimal

from app.connectors.uyumsoft.invoice_mapping import build_invoice_list_query, normalize_invoice_list_response
from app.schemas.uyumsoft_invoices import UyumsoftInvoiceListRequest


def test_build_invoice_list_query_uses_date_range_and_pagination() -> None:
    request = UyumsoftInvoiceListRequest(
        from_date=datetime(2026, 7, 15, tzinfo=UTC),
        to_date=datetime(2026, 7, 16, tzinfo=UTC),
        page=2,
        page_size=25,
    )

    query = build_invoice_list_query(request)

    assert query == {
        "StartDate": datetime(2026, 7, 15, tzinfo=UTC),
        "EndDate": datetime(2026, 7, 16, tzinfo=UTC),
        "PageIndex": 2,
        "PageSize": 25,
    }


def test_normalize_invoice_list_response_maps_known_and_extra_fields() -> None:
    request = UyumsoftInvoiceListRequest(
        from_date=datetime(2026, 7, 15, tzinfo=UTC),
        to_date=datetime(2026, 7, 16, tzinfo=UTC),
        page=1,
        page_size=10,
    )
    raw_response = {
        "Value": [
            {
                "InvoiceId": "inv-1",
                "ETTN": "ettn-1",
                "InvoiceNumber": "ABC2026001",
                "InvoiceDate": "2026-07-16T10:30:00",
                "SenderName": "Sender A",
                "ReceiverName": "Receiver B",
                "VKN": "1234567890",
                "CurrencyCode": "TRY",
                "TotalAmount": "42.50",
                "Status": "Approved",
                "ProviderSpecific": "kept",
            }
        ],
        "TotalCount": "1",
        "ResponseCode": "OK",
    }

    result = normalize_invoice_list_response(raw_response, direction="Inbox", request=request)

    assert result.total_count == 1
    assert result.extra_fields == {"ResponseCode": "OK"}
    invoice = result.invoices[0]
    assert invoice.invoice_id == "inv-1"
    assert invoice.ettn == "ettn-1"
    assert invoice.invoice_number == "ABC2026001"
    assert invoice.invoice_date == datetime(2026, 7, 16, 10, 30, tzinfo=UTC)
    assert invoice.sender == "Sender A"
    assert invoice.receiver == "Receiver B"
    assert invoice.tax_number == "1234567890"
    assert invoice.currency == "TRY"
    assert invoice.total_amount == Decimal("42.50")
    assert invoice.direction == "Inbox"
    assert invoice.status == "Approved"
    assert invoice.extra_fields == {"ProviderSpecific": "kept"}


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
