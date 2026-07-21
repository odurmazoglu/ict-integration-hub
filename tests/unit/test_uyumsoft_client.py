from datetime import UTC, datetime
from typing import Any

from pydantic import SecretStr
from requests import ConnectionError as RequestsConnectionError
from requests import Timeout as RequestsTimeout
from zeep.exceptions import Fault

from app.connectors.exceptions import ConnectorError, ConnectorTimeoutError
from app.connectors.uyumsoft.client import UyumsoftSoapClient, _sanitize_fault_message
from app.schemas.uyumsoft_invoices import UyumsoftInvoiceListRequest


class FakeInvoiceListQueryModel:
    def __init__(self, fields: set[str]) -> None:
        self.elements = [(field, object()) for field in fields]

    def __call__(self, **kwargs: Any) -> dict[str, Any]:
        return {"__model__": "InvoiceListQueryModel", **kwargs}


class MissingPageSizeQueryModel(FakeInvoiceListQueryModel):
    def __init__(self) -> None:
        super().__init__({"ExecutionStartDate", "ExecutionEndDate", "PageIndex"})


class FakeService:
    def TestConnection(self) -> str:
        return "OK"

    def WhoAmI(self) -> dict[str, str]:
        return {"username": "user"}

    def GetSystemDate(self) -> datetime:
        return datetime(2026, 7, 16, 9, 0, tzinfo=UTC)

    def GetInboxInvoiceList(self, query: dict[str, Any]) -> dict[str, Any]:
        assert query["__model__"] == "InvoiceListQueryModel"
        assert query["ExecutionStartDate"] == datetime(2026, 7, 15, tzinfo=UTC)
        assert query["ExecutionEndDate"] == datetime(2026, 7, 16, tzinfo=UTC)
        assert query["PageIndex"] == 1
        assert query["PageSize"] == 10
        assert "IncludeTagList" not in query
        assert query["OnlyNewestInvoices"] is False
        return {
            "Value": {"Items": [{"InvoiceId": "in-1", "DocumentId": "GIB2026001"}], "TotalCount": 1},
            "IsSucceded": True,
            "Message": None,
        }

    def GetOutboxInvoiceList(self, query: dict[str, Any]) -> dict[str, Any]:
        assert query["__model__"] == "InvoiceListQueryModel"
        assert query["PageSize"] == 10
        return {
            "Value": {"Items": [{"InvoiceId": "out-1", "DocumentId": "GIB2026002"}], "TotalCount": 1},
            "IsSucceded": True,
            "Message": None,
        }

    def GetInboxInvoiceData(self, invoice_id: str) -> dict[str, Any]:
        assert invoice_id == "in-1"
        return {"Value": {"Data": "PD94bWwgdmVyc2lvbj0iMS4wIj8+PEludm9pY2UvPg=="}, "IsSucceded": True}

    def GetOutboxInvoiceData(self, invoice_id: str) -> dict[str, Any]:
        assert invoice_id == "out-1"
        return {"Value": {"Data": b"<Invoice/>"}, "IsSucceded": True}


class FakeBinding:
    _operations = {
        "GetSystemDate": object(),
        "GetInboxInvoiceList": object(),
        "GetInboxInvoiceData": object(),
        "GetOutboxInvoiceList": object(),
        "GetOutboxInvoiceData": object(),
        "SendInvoice": object(),
        "TestConnection": object(),
        "WhoAmI": object(),
    }


class FakePort:
    binding = FakeBinding()


class FakeServiceDescription:
    ports = {"Integration": FakePort()}


class FakeWsdl:
    services = {"Integration": FakeServiceDescription()}


class FakeZeepClient:
    service = FakeService()
    wsdl = FakeWsdl()

    def get_type(self, name: str) -> FakeInvoiceListQueryModel:
        if name == "{http://tempuri.org/}InboxInvoiceListQueryModel":
            return FakeInvoiceListQueryModel(
                {"ExecutionStartDate", "ExecutionEndDate", "PageIndex", "PageSize", "OnlyNewestInvoices"}
            )
        if name == "{http://tempuri.org/}OutboxInvoiceListQueryModel":
            return FakeInvoiceListQueryModel(
                {"ExecutionStartDate", "ExecutionEndDate", "PageIndex", "PageSize", "IncludeTagList"}
            )
        raise AssertionError(f"Unexpected type requested: {name}")


def build_client() -> UyumsoftSoapClient:
    return UyumsoftSoapClient(
        wsdl_url="https://uyumsoft.test/wsdl",
        username="user",
        password=SecretStr("pass"),
        timeout_seconds=1,
        retry_attempts=2,
        retry_backoff_seconds=0,
        zeep_client=FakeZeepClient(),
    )


def build_request() -> UyumsoftInvoiceListRequest:
    return UyumsoftInvoiceListRequest(
        from_date=datetime(2026, 7, 15, tzinfo=UTC),
        to_date=datetime(2026, 7, 16, tzinfo=UTC),
        page=1,
        page_size=10,
    )


def test_uyumsoft_test_connection() -> None:
    result = build_client().test_connection()

    assert result.status == "ok"
    assert result.result == "OK"


def test_uyumsoft_who_am_i() -> None:
    result = build_client().who_am_i()

    assert result.identity == {"username": "user"}


def test_uyumsoft_system_date() -> None:
    result = build_client().get_system_date()

    assert result.system_date == datetime(2026, 7, 16, 9, 0, tzinfo=UTC)


def test_wsdl_inspection_marks_read_only_operations() -> None:
    result = build_client().inspect_wsdl()

    assert "SendInvoice" in result.operations
    assert "SendInvoice" not in result.read_only_operations
    assert result.read_only_operations == [
        "GetInboxInvoiceData",
        "GetInboxInvoiceList",
        "GetOutboxInvoiceData",
        "GetOutboxInvoiceList",
        "GetSystemDate",
        "TestConnection",
        "WhoAmI",
    ]


def test_fault_message_sanitizer_redacts_username_and_ip() -> None:
    message = "Bu sisteme erişmek için gerekli yetkiniz yok, Kullanıcı: secret-user, Ip: 192.0.2.10"

    assert _sanitize_fault_message(message) == (
        "Bu sisteme erişmek için gerekli yetkiniz yok, Kullanıcı: <redacted>, Ip: <redacted>"
    )


def test_uyumsoft_list_inbox_invoices_maps_response() -> None:
    result = build_client().list_inbox_invoices(build_request())

    assert result.direction == "Inbox"
    assert result.total_count == 1
    assert result.invoices[0].invoice_id == "in-1"
    assert result.invoices[0].ettn == "in-1"
    assert result.invoices[0].invoice_number == "GIB2026001"


def test_uyumsoft_list_outbox_invoices_maps_response() -> None:
    result = build_client().list_outbox_invoices(build_request())

    assert result.direction == "Outbox"
    assert result.invoices[0].invoice_id == "out-1"


def test_uyumsoft_downloads_inbox_invoice_xml_data() -> None:
    result = build_client().download_invoice_ubl_xml(direction="Inbox", invoice_id="in-1")

    assert result == b'<?xml version="1.0"?><Invoice/>'


def test_uyumsoft_download_invoice_returns_typed_document() -> None:
    result = build_client().download_invoice(direction="Inbox", invoice_id="in-1")

    assert result.direction == "Inbox"
    assert result.invoice_id == "in-1"
    assert result.mime_type == "application/xml"
    assert result.content == b'<?xml version="1.0"?><Invoice/>'


def test_uyumsoft_downloads_outbox_invoice_xml_data() -> None:
    result = build_client().download_invoice_ubl_xml(direction="Outbox", invoice_id="out-1")

    assert result == b"<Invoice/>"


class TransientInvoiceService(FakeService):
    def __init__(self) -> None:
        self.calls = 0

    def GetInboxInvoiceList(self, query: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        if self.calls == 1:
            raise RequestsConnectionError("temporary network failure")
        return {"Value": {"Items": [{"InvoiceId": "retry-ok"}], "TotalCount": 1}, "IsSucceded": True}


class FaultingInvoiceService(FakeService):
    def __init__(self) -> None:
        self.calls = 0

    def GetInboxInvoiceList(self, query: dict[str, Any]) -> dict:
        self.calls += 1
        raise Fault("SOAP fault")


class FaultingInvoiceDataService(FakeService):
    def GetInboxInvoiceData(self, invoice_id: str) -> dict:
        raise Fault("SOAP fault")


class TimeoutInvoiceDataService(FakeService):
    def GetInboxInvoiceData(self, invoice_id: str) -> dict:
        raise RequestsTimeout("timeout")


class InvalidBase64InvoiceDataService(FakeService):
    def GetInboxInvoiceData(self, invoice_id: str) -> dict[str, Any]:
        return {"Value": {"Data": "not-base64"}, "IsSucceded": True}


class UnsuccessfulInvoiceService(FakeService):
    def GetInboxInvoiceList(self, query: dict[str, Any]) -> dict[str, Any]:
        return {"Value": None, "IsSucceded": False, "Message": "Query rejected"}


class CustomZeepClient(FakeZeepClient):
    def __init__(self, service: FakeService) -> None:
        self.service = service


class MissingRequiredFieldZeepClient(FakeZeepClient):
    def __init__(self) -> None:
        self.service = ProviderCallFailingService()

    def get_type(self, name: str) -> MissingPageSizeQueryModel:
        if name == "{http://tempuri.org/}InboxInvoiceListQueryModel":
            return MissingPageSizeQueryModel()
        raise AssertionError(f"Unexpected type requested: {name}")


class ProviderCallFailingService(FakeService):
    def __init__(self) -> None:
        self.calls = 0

    def GetInboxInvoiceList(self, query: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        raise AssertionError("Provider method must not be invoked when query model is missing required fields.")


def test_invoice_listing_retries_transient_transport_failure() -> None:
    service = TransientInvoiceService()
    client = UyumsoftSoapClient(
        wsdl_url="https://uyumsoft.test/wsdl",
        username="user",
        password=SecretStr("pass"),
        timeout_seconds=1,
        retry_attempts=2,
        retry_backoff_seconds=0,
        zeep_client=CustomZeepClient(service),
    )

    result = client.list_inbox_invoices(build_request())

    assert service.calls == 2
    assert result.invoices[0].invoice_id == "retry-ok"


def test_invoice_listing_does_not_retry_soap_fault() -> None:
    service = FaultingInvoiceService()
    client = UyumsoftSoapClient(
        wsdl_url="https://uyumsoft.test/wsdl",
        username="user",
        password=SecretStr("pass"),
        timeout_seconds=1,
        retry_attempts=2,
        retry_backoff_seconds=0,
        zeep_client=CustomZeepClient(service),
    )

    try:
        client.list_inbox_invoices(build_request())
    except ConnectorError:
        pass
    else:
        raise AssertionError("Expected ConnectorError")

    assert service.calls == 1


def test_invoice_listing_raises_connector_error_for_unsuccessful_response() -> None:
    client = UyumsoftSoapClient(
        wsdl_url="https://uyumsoft.test/wsdl",
        username="user",
        password=SecretStr("pass"),
        timeout_seconds=1,
        retry_attempts=2,
        retry_backoff_seconds=0,
        zeep_client=CustomZeepClient(UnsuccessfulInvoiceService()),
    )

    try:
        client.list_inbox_invoices(build_request())
    except ConnectorError as exc:
        assert exc.safe_message == "Query rejected"
    else:
        raise AssertionError("Expected ConnectorError")


def test_missing_required_query_field_fails_before_provider_call() -> None:
    zeep_client = MissingRequiredFieldZeepClient()
    client = UyumsoftSoapClient(
        wsdl_url="https://uyumsoft.test/wsdl",
        username="user",
        password=SecretStr("pass"),
        timeout_seconds=1,
        retry_attempts=1,
        retry_backoff_seconds=0,
        zeep_client=zeep_client,
    )

    try:
        client.list_inbox_invoices(build_request())
    except ConnectorError as exc:
        assert exc.safe_message == (
            "Uyumsoft InboxInvoiceListQueryModel WSDL query model is missing required fields: PageSize."
        )
    else:
        raise AssertionError("Expected ConnectorError")

    assert zeep_client.service.calls == 0


def test_invoice_data_timeout_maps_to_timeout_error() -> None:
    client = UyumsoftSoapClient(
        wsdl_url="https://uyumsoft.test/wsdl",
        username="user",
        password=SecretStr("pass"),
        timeout_seconds=1,
        retry_attempts=1,
        retry_backoff_seconds=0,
        zeep_client=CustomZeepClient(TimeoutInvoiceDataService()),
    )

    try:
        client.download_invoice_ubl_xml(direction="Inbox", invoice_id="in-1")
    except ConnectorTimeoutError as exc:
        assert exc.safe_message == "Uyumsoft request timed out."
    else:
        raise AssertionError("Expected ConnectorTimeoutError")


def test_invoice_data_fault_maps_to_connector_error() -> None:
    client = UyumsoftSoapClient(
        wsdl_url="https://uyumsoft.test/wsdl",
        username="user",
        password=SecretStr("pass"),
        timeout_seconds=1,
        retry_attempts=1,
        retry_backoff_seconds=0,
        zeep_client=CustomZeepClient(FaultingInvoiceDataService()),
    )

    try:
        client.download_invoice_ubl_xml(direction="Inbox", invoice_id="in-1")
    except ConnectorError as exc:
        assert "Uyumsoft SOAP error" in exc.safe_message
    else:
        raise AssertionError("Expected ConnectorError")


def test_invoice_data_invalid_base64_maps_to_connector_error() -> None:
    client = UyumsoftSoapClient(
        wsdl_url="https://uyumsoft.test/wsdl",
        username="user",
        password=SecretStr("pass"),
        timeout_seconds=1,
        retry_attempts=1,
        retry_backoff_seconds=0,
        zeep_client=CustomZeepClient(InvalidBase64InvoiceDataService()),
    )

    try:
        client.download_invoice_ubl_xml(direction="Inbox", invoice_id="in-1")
    except ConnectorError as exc:
        assert exc.safe_message == "Uyumsoft invoice document data is not valid base64."
    else:
        raise AssertionError("Expected ConnectorError")
