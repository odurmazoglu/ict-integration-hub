from datetime import UTC, datetime
from decimal import Decimal

import pytest
from zeep import xsd

from app.connectors.exceptions import ConnectorError
from app.connectors.uyumsoft.invoice_mapping import (
    build_invoice_list_query_model,
    get_supported_zeep_fields,
    normalize_invoice_list_response,
)
from app.schemas.uyumsoft_invoices import UyumsoftInvoiceListRequest


class FieldElement:
    def __init__(self, name: str) -> None:
        self.name = name


class FakeQueryModel:
    def __init__(self, fields: set[str]) -> None:
        self.elements = [(field, FieldElement(field)) for field in fields]

    def __call__(self, **kwargs: object) -> dict[str, object]:
        return kwargs


class AttributeBackedQueryModel:
    def __init__(self, *, element_fields: set[str], attribute_fields: set[str]) -> None:
        self.elements = [(field, FieldElement(field)) for field in element_fields]
        self.attributes = [(field, FieldElement(field)) for field in attribute_fields]

    def __call__(self, **kwargs: object) -> dict[str, object]:
        return kwargs


class FakeZeepClient:
    def __init__(
        self,
        *,
        inbox_fields: set[str] | None = None,
        outbox_fields: set[str] | None = None,
    ) -> None:
        self.requested_type: str | None = None
        self.inbox_fields = inbox_fields or _fields(
            "ExecutionStartDate",
            "ExecutionEndDate",
            "PageIndex",
            "PageSize",
            "OnlyNewestInvoices",
        )
        self.outbox_fields = outbox_fields or _fields(
            "ExecutionStartDate",
            "ExecutionEndDate",
            "PageIndex",
            "PageSize",
            "IncludeTagList",
        )

    def get_type(self, name: str) -> FakeQueryModel:
        self.requested_type = name
        if name == "{http://tempuri.org/}InboxInvoiceListQueryModel":
            return FakeQueryModel(self.inbox_fields)
        if name == "{http://tempuri.org/}OutboxInvoiceListQueryModel":
            return FakeQueryModel(self.outbox_fields)
        raise AssertionError(f"Unexpected type requested: {name}")


class XsdTypeBackedQueryModel:
    def __init__(self, fields: set[str]) -> None:
        self._xsd_type = FakeQueryModel(fields)

    def __call__(self, **kwargs: object) -> dict[str, object]:
        return kwargs


class XsdTypeBackedZeepClient(FakeZeepClient):
    def get_type(self, name: str) -> XsdTypeBackedQueryModel:
        self.requested_type = name
        if name == "{http://tempuri.org/}InboxInvoiceListQueryModel":
            return XsdTypeBackedQueryModel(self.inbox_fields)
        if name == "{http://tempuri.org/}OutboxInvoiceListQueryModel":
            return XsdTypeBackedQueryModel(self.outbox_fields)
        raise AssertionError(f"Unexpected type requested: {name}")


class AttributeBackedZeepClient(FakeZeepClient):
    def get_type(self, name: str) -> AttributeBackedQueryModel:
        self.requested_type = name
        if name == "{http://tempuri.org/}InboxInvoiceListQueryModel":
            return AttributeBackedQueryModel(
                element_fields=_fields("ExecutionStartDate", "ExecutionEndDate"),
                attribute_fields=_fields("PageIndex", "PageSize", "OnlyNewestInvoices"),
            )
        if name == "{http://tempuri.org/}OutboxInvoiceListQueryModel":
            return AttributeBackedQueryModel(
                element_fields=_fields("ExecutionStartDate", "ExecutionEndDate"),
                attribute_fields=_fields("PageIndex", "PageSize"),
            )
        raise AssertionError(f"Unexpected type requested: {name}")


class NestedElement:
    def __init__(self, name: str | None = None) -> None:
        self.name = name
        self.elements: list[object] = []
        self.elements_nested: list[object] = []
        self.type: object | None = None


def test_build_invoice_list_query_model_uses_supported_wsdl_fields_for_inbox() -> None:
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
        "OnlyNewestInvoices": False,
    }


def test_build_invoice_list_query_model_uses_supported_wsdl_fields_for_outbox() -> None:
    request = UyumsoftInvoiceListRequest(
        from_date=datetime(2026, 7, 15, tzinfo=UTC),
        to_date=datetime(2026, 7, 16, tzinfo=UTC),
        page=1,
        page_size=1,
    )
    zeep_client = FakeZeepClient()

    query = build_invoice_list_query_model(zeep_client, request, direction="Outbox")

    assert zeep_client.requested_type == "{http://tempuri.org/}OutboxInvoiceListQueryModel"
    assert query == {
        "ExecutionStartDate": datetime(2026, 7, 15, tzinfo=UTC),
        "ExecutionEndDate": datetime(2026, 7, 16, tzinfo=UTC),
        "PageIndex": 1,
        "PageSize": 1,
        "IncludeTagList": False,
    }


def test_inbox_model_without_include_tag_list_omits_optional_field() -> None:
    query = build_invoice_list_query_model(FakeZeepClient(), _request(), direction="Inbox")

    assert "IncludeTagList" not in query


def test_outbox_model_with_include_tag_list_includes_optional_field() -> None:
    query = build_invoice_list_query_model(FakeZeepClient(), _request(), direction="Outbox")

    assert query["IncludeTagList"] is False


def test_inbox_model_with_only_newest_invoices_includes_optional_field() -> None:
    query = build_invoice_list_query_model(FakeZeepClient(), _request(), direction="Inbox")

    assert query["OnlyNewestInvoices"] is False


def test_model_without_only_newest_invoices_omits_optional_field() -> None:
    zeep_client = FakeZeepClient(
        inbox_fields=_fields("ExecutionStartDate", "ExecutionEndDate", "PageIndex", "PageSize")
    )

    query = build_invoice_list_query_model(zeep_client, _request(), direction="Inbox")

    assert "OnlyNewestInvoices" not in query


def test_unsupported_optional_fields_are_omitted() -> None:
    zeep_client = FakeZeepClient(
        inbox_fields=_fields("ExecutionStartDate", "ExecutionEndDate", "PageIndex", "PageSize", "OnlyNewestInvoices")
    )

    query = build_invoice_list_query_model(zeep_client, _request(), direction="Inbox")

    assert "IncludeTagList" not in query
    assert set(query) == {
        "ExecutionStartDate",
        "ExecutionEndDate",
        "PageIndex",
        "PageSize",
        "OnlyNewestInvoices",
    }


def test_test_and_production_shaped_models_with_different_optional_fields_are_supported() -> None:
    test_shaped = FakeZeepClient(
        inbox_fields=_fields("ExecutionStartDate", "ExecutionEndDate", "PageIndex", "PageSize", "IncludeTagList"),
        outbox_fields=_fields("ExecutionStartDate", "ExecutionEndDate", "PageIndex", "PageSize", "IncludeTagList"),
    )
    production_shaped = FakeZeepClient(
        inbox_fields=_fields("ExecutionStartDate", "ExecutionEndDate", "PageIndex", "PageSize", "OnlyNewestInvoices"),
        outbox_fields=_fields("ExecutionStartDate", "ExecutionEndDate", "PageIndex", "PageSize"),
    )

    test_inbox_query = build_invoice_list_query_model(test_shaped, _request(), direction="Inbox")
    production_inbox_query = build_invoice_list_query_model(production_shaped, _request(), direction="Inbox")
    production_outbox_query = build_invoice_list_query_model(production_shaped, _request(), direction="Outbox")

    assert test_inbox_query["IncludeTagList"] is False
    assert "OnlyNewestInvoices" not in test_inbox_query
    assert production_inbox_query["OnlyNewestInvoices"] is False
    assert "IncludeTagList" not in production_inbox_query
    assert "IncludeTagList" not in production_outbox_query
    assert "OnlyNewestInvoices" not in production_outbox_query


def test_real_zeep_complex_type_factory_fields_are_discovered() -> None:
    query_model = xsd.ComplexType(
        xsd.Sequence(
            [
                xsd.Element("ExecutionStartDate", xsd.DateTime()),
                xsd.Element("ExecutionEndDate", xsd.DateTime()),
                xsd.Element("PageIndex", xsd.Int()),
                xsd.Element("PageSize", xsd.Int()),
                xsd.Element("OnlyNewestInvoices", xsd.Boolean()),
            ]
        )
    )

    assert get_supported_zeep_fields(query_model) >= _fields(
        "ExecutionStartDate",
        "ExecutionEndDate",
        "PageIndex",
        "PageSize",
        "OnlyNewestInvoices",
    )


def test_real_zeep_value_object_xsd_type_fields_are_discovered() -> None:
    query_model = xsd.ComplexType(
        xsd.Sequence(
            [
                xsd.Element("ExecutionStartDate", xsd.DateTime()),
                xsd.Element("ExecutionEndDate", xsd.DateTime()),
                xsd.Element("PageIndex", xsd.Int()),
                xsd.Element("PageSize", xsd.Int()),
            ]
        )
    )
    value = query_model(
        ExecutionStartDate=datetime(2026, 7, 15, tzinfo=UTC),
        ExecutionEndDate=datetime(2026, 7, 16, tzinfo=UTC),
        PageIndex=1,
        PageSize=10,
    )

    assert get_supported_zeep_fields(value) >= _fields(
        "ExecutionStartDate",
        "ExecutionEndDate",
        "PageIndex",
        "PageSize",
    )


def test_xsd_type_backed_factory_fields_are_used_for_query_construction() -> None:
    query = build_invoice_list_query_model(XsdTypeBackedZeepClient(), _request(), direction="Inbox")

    assert query["PageIndex"] == 1
    assert query["PageSize"] == 10
    assert query["OnlyNewestInvoices"] is False
    assert "IncludeTagList" not in query


def test_real_zeep_complex_type_attribute_fields_are_discovered() -> None:
    query_model = xsd.ComplexType(
        xsd.Sequence(
            [
                xsd.Element("ExecutionStartDate", xsd.DateTime()),
                xsd.Element("ExecutionEndDate", xsd.DateTime()),
            ]
        ),
        [
            xsd.Attribute("PageIndex", xsd.Int()),
            xsd.Attribute("PageSize", xsd.Int()),
            xsd.Attribute("OnlyNewestInvoices", xsd.Boolean()),
        ],
    )

    assert get_supported_zeep_fields(query_model) >= _fields(
        "ExecutionStartDate",
        "ExecutionEndDate",
        "PageIndex",
        "PageSize",
        "OnlyNewestInvoices",
    )


def test_attribute_backed_production_shaped_query_model_keeps_required_pagination_fields() -> None:
    query = build_invoice_list_query_model(AttributeBackedZeepClient(), _request(), direction="Inbox")

    assert query["ExecutionStartDate"] == datetime(2026, 7, 15, tzinfo=UTC)
    assert query["ExecutionEndDate"] == datetime(2026, 7, 16, tzinfo=UTC)
    assert query["PageIndex"] == 1
    assert query["PageSize"] == 10
    assert query["OnlyNewestInvoices"] is False
    assert "IncludeTagList" not in query


def test_direct_elements_are_discovered() -> None:
    query_model = FakeQueryModel(_fields("ExecutionStartDate", "ExecutionEndDate", "PageIndex", "PageSize"))

    assert get_supported_zeep_fields(query_model) >= _fields(
        "ExecutionStartDate", "ExecutionEndDate", "PageIndex", "PageSize"
    )


def test_elements_nested_are_discovered() -> None:
    query_model = NestedElement()
    query_model.elements_nested = [
        ("ExecutionStartDate", FieldElement("ExecutionStartDate")),
        ("ExecutionEndDate", FieldElement("ExecutionEndDate")),
        ("PageIndex", FieldElement("PageIndex")),
        ("PageSize", FieldElement("PageSize")),
    ]

    assert get_supported_zeep_fields(query_model) >= _fields(
        "ExecutionStartDate", "ExecutionEndDate", "PageIndex", "PageSize"
    )


def test_tuple_and_nested_group_representations_are_discovered() -> None:
    group = NestedElement()
    group.elements = [
        ("PageIndex", FieldElement("PageIndex")),
        ("PageSize", FieldElement("PageSize")),
    ]
    sequence = NestedElement()
    sequence.elements_nested = [
        ("ExecutionStartDate", FieldElement("ExecutionStartDate")),
        ("ExecutionEndDate", FieldElement("ExecutionEndDate")),
        ("_value_1", group),
    ]

    assert get_supported_zeep_fields(sequence) >= _fields(
        "ExecutionStartDate", "ExecutionEndDate", "PageIndex", "PageSize"
    )
    assert "_value_1" not in get_supported_zeep_fields(sequence)


def test_duplicate_and_cyclic_references_do_not_recurse_forever() -> None:
    root = NestedElement()
    child = NestedElement("PageIndex")
    root.elements = [("ExecutionStartDate", FieldElement("ExecutionStartDate")), ("PageIndex", child)]
    root.elements_nested = [("PageIndex", child)]
    child.type = root

    assert get_supported_zeep_fields(root) >= _fields("ExecutionStartDate", "PageIndex")


def test_required_pagination_and_date_fields_remain_included() -> None:
    query = build_invoice_list_query_model(FakeZeepClient(), _request(), direction="Inbox")

    assert query["ExecutionStartDate"] == datetime(2026, 7, 15, tzinfo=UTC)
    assert query["ExecutionEndDate"] == datetime(2026, 7, 16, tzinfo=UTC)
    assert query["PageIndex"] == 1
    assert query["PageSize"] == 10


def test_missing_required_wsdl_fields_produces_safe_connector_error() -> None:
    zeep_client = FakeZeepClient(inbox_fields=_fields("ExecutionStartDate", "ExecutionEndDate", "PageIndex"))

    with pytest.raises(ConnectorError) as exc_info:
        build_invoice_list_query_model(zeep_client, _request(), direction="Inbox")

    assert exc_info.value.safe_message == (
        "Uyumsoft InboxInvoiceListQueryModel WSDL query model is missing required fields: PageSize."
    )
    assert "password" not in exc_info.value.safe_message.lower()
    assert "secret" not in exc_info.value.safe_message.lower()


def test_query_model_construction_does_not_introduce_provider_write_operations() -> None:
    query = build_invoice_list_query_model(FakeZeepClient(), _request(), direction="Inbox")

    assert "SendInvoice" not in query
    assert "SetInvoicesTaken" not in query
    assert "CancelInvoice" not in query
    assert "RetrySendInvoices" not in query
    assert "MoveToDraftStatus" not in query


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


def _request() -> UyumsoftInvoiceListRequest:
    return UyumsoftInvoiceListRequest(
        from_date=datetime(2026, 7, 15, tzinfo=UTC),
        to_date=datetime(2026, 7, 16, tzinfo=UTC),
        page=1,
        page_size=10,
    )


def _fields(*names: str) -> set[str]:
    return set(names)
