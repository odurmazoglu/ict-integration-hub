from __future__ import annotations

import json

import pytest

from scripts import inspect_uyumsoft_wsdl


class FakeQName:
    def __init__(self, localname: str) -> None:
        self.localname = localname

    def __str__(self) -> str:
        return f"{{http://tempuri.org/}}{self.localname}"


class FakeElement:
    def __init__(self, name: str, type_name: str = "xsd:string") -> None:
        self.name = name
        self.type = type_name


class FakeSequence:
    def __init__(self, fields: list[str]) -> None:
        self.elements = [(field, FakeElement(field)) for field in fields]


class FakeXsdBackedType:
    qname = FakeQName("NestedQueryModel")

    def __init__(self) -> None:
        self._xsd_type = FakeSequence(["ExecutionStartDate", "ExecutionEndDate"])

    def __str__(self) -> str:
        return "NestedQueryModel({ExecutionStartDate, ExecutionEndDate})"


class FakeQueryType:
    qname = FakeQName("InboxInvoiceListQueryModel")

    def __init__(self) -> None:
        self.elements = [
            ("ExecutionStartDate", FakeElement("ExecutionStartDate", "xsd:dateTime")),
            ("ExecutionEndDate", FakeElement("ExecutionEndDate", "xsd:dateTime")),
            ("PageIndex", FakeElement("PageIndex", "xsd:int")),
            ("PageSize", FakeElement("PageSize", "xsd:int")),
        ]

    def __str__(self) -> str:
        return "InboxInvoiceListQueryModel({ExecutionStartDate, ExecutionEndDate, PageIndex, PageSize})"


class FakeNonModelType:
    qname = FakeQName("InvoiceDataResponse")

    def __init__(self) -> None:
        self.elements = [("Value", FakeElement("Value"))]


class FakeWsdlTypes:
    def types(self) -> list[object]:
        return [FakeQueryType(), FakeXsdBackedType(), FakeNonModelType()]


class ExplodingService:
    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"SOAP service operation must not be inspected or called: {name}")


class FakeClient:
    service = ExplodingService()

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.wsdl = type("FakeWsdl", (), {"types": FakeWsdlTypes()})()
        self.requested_type: str | None = None

    def type_factory(self, namespace: str) -> object:
        assert namespace == inspect_uyumsoft_wsdl.UYUMSOFT_NAMESPACE
        return type("FakeFactory", (), {})()

    def get_type(self, name: str) -> FakeQueryType:
        self.requested_type = name
        if name == "{http://tempuri.org/}InboxInvoiceListQueryModel":
            return FakeQueryType()
        raise AssertionError(f"Unexpected WSDL type requested: {name}")


def test_list_models_returns_available_complex_query_models() -> None:
    assert inspect_uyumsoft_wsdl.list_models(FakeClient()) == [
        "InboxInvoiceListQueryModel",
        "NestedQueryModel",
        "OutboxInvoiceListQueryModel",
    ]


def test_inspect_model_reports_fields_and_discovery_paths() -> None:
    metadata = inspect_uyumsoft_wsdl.inspect_model(FakeClient(), "InboxInvoiceListQueryModel")

    assert metadata.model == "InboxInvoiceListQueryModel"
    assert metadata.factory_type == "FakeFactory"
    assert metadata.underlying_zeep_type.startswith("InboxInvoiceListQueryModel")
    assert [field.name for field in metadata.fields] == [
        "ExecutionStartDate",
        "ExecutionEndDate",
        "PageIndex",
        "PageSize",
    ]
    assert metadata.fields[0].type == "xsd:dateTime"
    assert metadata.discovery_path == ["type.elements"]


def test_inspect_model_discovers_xsd_type_backed_fields() -> None:
    fields, paths = inspect_uyumsoft_wsdl._discover_fields(FakeXsdBackedType())

    assert [field.name for field in fields] == ["ExecutionStartDate", "ExecutionEndDate"]
    assert paths == ["type._xsd_type.elements"]


def test_cli_json_output_uses_wsdl_model_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(inspect_uyumsoft_wsdl, "Client", FakeClient)

    exit_code = inspect_uyumsoft_wsdl.main(["--model", "InboxInvoiceListQueryModel", "--json"])

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["model"] == "InboxInvoiceListQueryModel"
    assert [field["name"] for field in output["fields"]] == [
        "ExecutionStartDate",
        "ExecutionEndDate",
        "PageIndex",
        "PageSize",
    ]


def test_cli_rejects_wsdl_urls_with_credentials() -> None:
    with pytest.raises(SystemExit) as exc_info:
        inspect_uyumsoft_wsdl.main(["--list-models", "--wsdl-url", "https://user:secret@example.test/wsdl"])

    assert str(exc_info.value) == "WSDL URL must not contain embedded credentials."
