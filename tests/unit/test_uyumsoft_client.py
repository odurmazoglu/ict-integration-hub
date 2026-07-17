from datetime import UTC, datetime
from typing import Any

from pydantic import SecretStr
from requests import ConnectionError as RequestsConnectionError
from zeep.exceptions import Fault

from app.connectors.exceptions import ConnectorError
from app.connectors.uyumsoft.client import UyumsoftSoapClient
from app.schemas.uyumsoft_invoices import UyumsoftInvoiceListRequest


class FakeInvoiceListQueryModel:
    def __call__(self, **kwargs: Any) -> dict[str, Any]:
        return {"__model__": "InvoiceListQueryModel", **kwargs}


class FakeService:
    def TestConnection(self, username: str, password: str) -> str:
        assert username == "user"
        assert password == "pass"
        return "OK"

    def WhoAmI(self, username: str, password: str) -> dict[str, str]:
        assert username == "user"
        assert password == "pass"
        return {"username": username}

    def GetSystemDate(self, username: str, password: str) -> datetime:
        assert username == "user"
        assert password == "pass"
        return datetime(2026, 7, 16, 9, 0, tzinfo=UTC)

    def GetInboxInvoiceList(self, query: dict[str, Any]) -> dict[str, Any]:
        assert query["__model__"] == "InvoiceListQueryModel"
        assert query["ExecutionStartDate"] == datetime(2026, 7, 15, tzinfo=UTC)
        assert query["ExecutionEndDate"] == datetime(2026, 7, 16, tzinfo=UTC)
        assert query["PageIndex"] == 1
        assert query["PageSize"] == 10
        assert query["IncludeTagList"] is False
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


class FakeBinding:
    _operations = {
        "GetSystemDate": object(),
        "GetInboxInvoiceList": object(),
        "GetOutboxInvoiceList": object(),
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
        assert name in {
            "{http://tempuri.org/}InboxInvoiceListQueryModel",
            "{http://tempuri.org/}OutboxInvoiceListQueryModel",
        }
        return FakeInvoiceListQueryModel()


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
        "GetInboxInvoiceList",
        "GetOutboxInvoiceList",
        "GetSystemDate",
        "TestConnection",
        "WhoAmI",
    ]


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


class UnsuccessfulInvoiceService(FakeService):
    def GetInboxInvoiceList(self, query: dict[str, Any]) -> dict[str, Any]:
        return {"Value": None, "IsSucceded": False, "Message": "Query rejected"}


class CustomZeepClient(FakeZeepClient):
    def __init__(self, service: FakeService) -> None:
        self.service = service


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
