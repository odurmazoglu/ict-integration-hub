from datetime import UTC, datetime

from pydantic import SecretStr

from app.connectors.uyumsoft.client import UyumsoftSoapClient


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


class FakeBinding:
    _operations = {
        "GetSystemDate": object(),
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


def build_client() -> UyumsoftSoapClient:
    return UyumsoftSoapClient(
        wsdl_url="https://uyumsoft.test/wsdl",
        username="user",
        password=SecretStr("pass"),
        timeout_seconds=1,
        zeep_client=FakeZeepClient(),
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
    assert result.read_only_operations == ["GetSystemDate", "TestConnection", "WhoAmI"]

