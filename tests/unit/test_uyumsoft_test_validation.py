from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.connectors.exceptions import ConnectorError
from app.connectors.uyumsoft.client import UyumsoftSoapClient
from app.core.config import Settings
from app.db.base import Base
from app.models.invoice_document import InvoiceDocument
from app.models.uyumsoft_invoice import UyumsoftInvoiceMetadata
from app.schemas.uyumsoft import UyumsoftIdentityResponse, UyumsoftOperationsResponse, UyumsoftTestConnectionResponse
from app.schemas.uyumsoft_invoices import (
    UyumsoftInvoiceListRequest,
    UyumsoftInvoiceListResponse,
    UyumsoftInvoiceSummary,
)
from app.services.document_storage import LocalDocumentStorage
from app.services.uyumsoft_test_validation import (
    APPROVED_UYUMSOFT_TEST_HOSTS,
    UyumsoftTestValidationRequest,
    UyumsoftTestValidationService,
)

STANDARD_XML = b"""<?xml version="1.0"?>
<Invoice>
  <DocumentCurrencyCode>TRY</DocumentCurrencyCode>
  <InvoiceLine>
    <Item>
      <Name>Consulting</Name>
      <SellersItemIdentification><ID>CONSULT-001</ID></SellersItemIdentification>
    </Item>
  </InvoiceLine>
</Invoice>
"""
MULTI_LINE_XML = b"""<Invoice>
  <DocumentCurrencyCode>TRY</DocumentCurrencyCode>
  <InvoiceLine>
    <Item>
      <Name>Consulting</Name>
      <SellersItemIdentification><ID>CONSULT-001</ID></SellersItemIdentification>
    </Item>
  </InvoiceLine>
  <InvoiceLine>
    <Item>
      <Name>Implementation</Name>
      <SellersItemIdentification><ID>IMPL-001</ID></SellersItemIdentification>
    </Item>
  </InvoiceLine>
</Invoice>
"""
FOREIGN_CURRENCY_XML = b"""<Invoice>
  <DocumentCurrencyCode>USD</DocumentCurrencyCode>
  <InvoiceLine>
    <Item>
      <Name>Service</Name>
      <SellersItemIdentification><ID>SERVICE-001</ID></SellersItemIdentification>
    </Item>
  </InvoiceLine>
</Invoice>
"""
DISCOUNT_XML = b"""<Invoice>
  <DocumentCurrencyCode>TRY</DocumentCurrencyCode>
  <AllowanceCharge><Amount>1.00</Amount></AllowanceCharge>
  <TaxSubtotal><Percent>10</Percent></TaxSubtotal>
  <TaxSubtotal><Percent>20</Percent></TaxSubtotal>
  <InvoiceLine>
    <Item>
      <Name>Discounted</Name>
      <SellersItemIdentification><ID>DISC-001</ID></SellersItemIdentification>
    </Item>
  </InvoiceLine>
</Invoice>
"""
AMBIGUOUS_XML = b"""<Invoice>
  <DocumentCurrencyCode>TRY</DocumentCurrencyCode>
  <InvoiceLine><Item><Name>Same</Name></Item></InvoiceLine>
  <InvoiceLine><Item><Name>Same</Name></Item></InvoiceLine>
</Invoice>
"""


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db_session:
        yield db_session


class FakeUyumsoftValidationClient(UyumsoftSoapClient):
    def __init__(
        self,
        *,
        invoices: list[UyumsoftInvoiceSummary] | None = None,
        documents: dict[str, bytes] | None = None,
        fail_auth: Exception | None = None,
        fail_identity: Exception | None = None,
        fail_listing: Exception | None = None,
        fail_download_ids: set[str] | None = None,
    ) -> None:
        self.invoices = invoices if invoices is not None else [_invoice("standard", currency="TRY")]
        self.documents = documents or {"standard": STANDARD_XML}
        self.fail_auth = fail_auth
        self.fail_identity = fail_identity
        self.fail_listing = fail_listing
        self.fail_download_ids = fail_download_ids or set()
        self.calls: list[str] = []

    def inspect_wsdl(self) -> UyumsoftOperationsResponse:
        self.calls.append("inspect_wsdl")
        return UyumsoftOperationsResponse(
            status="ok",
            wsdl_url="https://efatura-test.uyumsoft.com.tr/Services/Integration?wsdl",
            operations=["TestConnection", "WhoAmI", "GetInboxInvoiceList", "GetInboxInvoiceData"],
            read_only_operations=["TestConnection", "WhoAmI", "GetInboxInvoiceList", "GetInboxInvoiceData"],
        )

    def test_connection(self) -> UyumsoftTestConnectionResponse:
        self.calls.append("TestConnection")
        if self.fail_auth is not None:
            raise self.fail_auth
        return UyumsoftTestConnectionResponse(status="ok", result="ok")

    def who_am_i(self) -> UyumsoftIdentityResponse:
        self.calls.append("WhoAmI")
        if self.fail_auth is not None:
            raise self.fail_auth
        if self.fail_identity is not None:
            raise self.fail_identity
        return UyumsoftIdentityResponse(status="ok", identity={"authenticated": True})

    def list_inbox_invoices(self, request: UyumsoftInvoiceListRequest) -> UyumsoftInvoiceListResponse:
        self.calls.append("GetInboxInvoiceList")
        if self.fail_listing is not None:
            raise self.fail_listing
        return UyumsoftInvoiceListResponse(
            direction="Inbox",
            page=request.page,
            page_size=request.page_size,
            total_count=len(self.invoices),
            invoices=self.invoices[: request.page_size],
        )

    def download_invoice_ubl_xml(self, *, direction: str, invoice_id: str) -> bytes:
        self.calls.append("GetInboxInvoiceData")
        if direction != "Inbox":
            raise AssertionError("Issue #30 validation must only acquire incoming invoices.")
        if invoice_id in self.fail_download_ids:
            raise ConnectorError("Uyumsoft invoice document request was not successful.")
        return self.documents[invoice_id]

    def __getattribute__(self, name: str) -> Any:
        forbidden = {
            "SetInvoicesTaken",
            "SendInvoice",
            "CancelInvoice",
            "RetrySendInvoices",
            "MoveToDraftStatus",
        }
        if name in forbidden:
            raise AssertionError(f"Forbidden operation accessed: {name}")
        return super().__getattribute__(name)


class TamperingStorage(LocalDocumentStorage):
    def read(self, storage_key: str) -> bytes:
        content = super().read(storage_key)
        if storage_key.endswith(".xml"):
            return content + b"tampered"
        return content


def test_approved_test_host_accepted_and_listing_success(session: Session, tmp_path: Path) -> None:
    report = _validate(session=session, tmp_path=tmp_path)

    assert report["target_host"] in APPROVED_UYUMSOFT_TEST_HOSTS
    assert report["wsdl_reachability"]["status"] == "ok"
    assert report["authentication"]["status"] == "ok"
    assert report["invoice_listing"]["status"] == "ok"
    assert report["invoice_listing"]["returned_count"] == 1
    assert report["no_provider_state_change_attempted"] is True


def test_production_or_non_approved_host_rejected_before_network_access(session: Session, tmp_path: Path) -> None:
    client = FakeUyumsoftValidationClient()
    report = _validate(
        session=session,
        tmp_path=tmp_path,
        client=client,
        settings=_settings(uyumsoft_test_wsdl_url="https://efatura.uyumsoft.com.tr/Services/Integration?wsdl"),
    )

    assert report["overall_status"] == "failed"
    assert "UYUMSOFT_TEST_WSDL_URL host is not approved for test validation." in report["configuration_failures"]
    assert report["read_only_operations_planned"] == [
        "TestConnection",
        "WhoAmI",
        "GetInboxInvoiceList",
        "GetInboxInvoiceData",
    ]
    assert report["read_only_operations_validated"] == []
    assert client.calls == []


def test_configuration_failure_reports_no_validated_operations(session: Session, tmp_path: Path) -> None:
    client = FakeUyumsoftValidationClient()
    report = _validate(
        session=session,
        tmp_path=tmp_path,
        client=client,
        settings=_settings(uyumsoft_username="change-me", uyumsoft_password=SecretStr("change-me")),
    )

    assert report["configuration_failures"] == [
        "UYUMSOFT_USERNAME must be configured with test credentials.",
        "UYUMSOFT_PASSWORD must be configured with test credentials.",
    ]
    assert report["read_only_operations_validated"] == []
    assert client.calls == []


def test_partial_operation_success_reports_only_actual_successes(session: Session, tmp_path: Path) -> None:
    report = _validate(
        session=session,
        tmp_path=tmp_path,
        client=FakeUyumsoftValidationClient(fail_identity=ConnectorError("Uyumsoft returned HTTP 403.")),
    )

    assert report["authentication"]["status"] == "permission_failure"
    assert report["read_only_operations_validated"] == ["TestConnection"]
    assert report["invoice_listing"]["status"] == "pending"
    assert report["ubl_download"]["status"] == "pending"


def test_authentication_failure_is_sanitized(session: Session, tmp_path: Path) -> None:
    report = _validate(
        session=session,
        tmp_path=tmp_path,
        client=FakeUyumsoftValidationClient(fail_auth=ConnectorError("Uyumsoft returned HTTP 403.")),
    )

    assert report["authentication"]["status"] == "permission_failure"
    assert report["permission_failures"] == [{"target": "authentication", "message": "Uyumsoft returned HTTP 403."}]


def test_empty_listing_is_reported_without_document_writes(session: Session, tmp_path: Path) -> None:
    report = _validate(session=session, tmp_path=tmp_path, client=FakeUyumsoftValidationClient(invoices=[]))

    assert report["invoice_listing"]["status"] == "empty"
    assert report["records_inspected"] == 0
    assert session.scalars(select(InvoiceDocument)).all() == []
    assert "No incoming Uyumsoft test invoices were returned." in report["blockers_for_parser_validation"]


def test_detail_retrieval_failure_is_reported(session: Session, tmp_path: Path) -> None:
    report = _validate(
        session=session,
        tmp_path=tmp_path,
        client=FakeUyumsoftValidationClient(fail_download_ids={"standard"}),
    )

    assert report["detail_retrieval"]["status"] == "failed"
    assert report["ubl_download"]["status"] == "failed"
    assert session.scalars(select(InvoiceDocument)).all() == []


def test_successful_ubl_download_persistence_and_idempotent_rerun(session: Session, tmp_path: Path) -> None:
    client = FakeUyumsoftValidationClient()
    first = _validate(session=session, tmp_path=tmp_path, client=client)
    second = _validate(session=session, tmp_path=tmp_path, client=client)

    documents = session.scalars(select(InvoiceDocument)).all()
    invoices = session.scalars(select(UyumsoftInvoiceMetadata)).all()
    assert first["detail_retrieval"]["status"] == "ok"
    assert first["ubl_download"]["status"] == "ok"
    assert first["document_persistence"]["status"] == "ok"
    assert first["sha256_verification"]["status"] == "ok"
    assert second["document_persistence"]["last_document_status"] == "existing"
    assert len(documents) == 1
    assert len(invoices) == 1


def test_sha256_mismatch_failure(session: Session, tmp_path: Path) -> None:
    report = _validate(
        session=session,
        tmp_path=tmp_path,
        storage=TamperingStorage(tmp_path),
    )

    assert report["sha256_verification"]["status"] == "failed"
    assert "Persisted document hash or metadata verification failed." in report["blockers_for_parser_validation"]


def test_missing_representative_scenarios_reported_honestly(session: Session, tmp_path: Path) -> None:
    report = _validate(session=session, tmp_path=tmp_path)

    assert "standard_single_line_invoice" in report["collected_dataset_scenarios"]
    assert "likely_missing_or_ambiguous_odoo_resolution_invoice" not in report["collected_dataset_scenarios"]
    assert "multi_line_invoice" in report["missing_dataset_scenarios"]
    assert (
        "One or more representative UBL dataset scenarios were not found." in report["blockers_for_parser_validation"]
    )


def test_collects_representative_scenarios_where_available(session: Session, tmp_path: Path) -> None:
    invoices = [
        _invoice("standard", currency="TRY"),
        _invoice("multi", currency="TRY"),
        _invoice("foreign", currency="USD"),
        _invoice("discount", currency="TRY"),
        _invoice("ambiguous", currency="TRY", tax_number=None),
    ]
    documents = {
        "standard": STANDARD_XML,
        "multi": MULTI_LINE_XML,
        "foreign": FOREIGN_CURRENCY_XML,
        "discount": DISCOUNT_XML,
        "ambiguous": AMBIGUOUS_XML,
    }
    report = _validate(
        session=session,
        tmp_path=tmp_path,
        client=FakeUyumsoftValidationClient(invoices=invoices, documents=documents),
        request=_request(limit=5),
    )

    assert set(report["collected_dataset_scenarios"]) == {
        "standard_single_line_invoice",
        "multi_line_invoice",
        "foreign_currency_invoice",
        "discount_or_multiple_tax_rates_invoice",
        "likely_missing_or_ambiguous_odoo_resolution_invoice",
    }
    assert report["missing_dataset_scenarios"] == []


def test_seller_item_id_nested_under_identification_is_recognized(session: Session, tmp_path: Path) -> None:
    report = _validate(session=session, tmp_path=tmp_path)

    assert "standard_single_line_invoice" in report["collected_dataset_scenarios"]
    assert "likely_missing_or_ambiguous_odoo_resolution_invoice" not in report["collected_dataset_scenarios"]


def test_unrelated_name_elements_do_not_create_false_duplicate_product_match(
    session: Session,
    tmp_path: Path,
) -> None:
    xml = b"""<Invoice>
      <AccountingSupplierParty>
        <Party><PartyName><Name>Duplicated Header</Name></PartyName></Party>
      </AccountingSupplierParty>
      <AccountingCustomerParty>
        <Party><PartyName><Name>Duplicated Header</Name></PartyName></Party>
      </AccountingCustomerParty>
      <InvoiceLine>
        <Item>
          <Name>Line Product</Name>
          <SellersItemIdentification><ID>LINE-001</ID></SellersItemIdentification>
        </Item>
      </InvoiceLine>
    </Invoice>
    """
    report = _validate(
        session=session,
        tmp_path=tmp_path,
        client=FakeUyumsoftValidationClient(documents={"standard": xml}),
    )

    assert "standard_single_line_invoice" in report["collected_dataset_scenarios"]
    assert "likely_missing_or_ambiguous_odoo_resolution_invoice" not in report["collected_dataset_scenarios"]


def test_secrets_and_xml_content_absent_from_report(session: Session, tmp_path: Path) -> None:
    report = _validate(
        session=session,
        tmp_path=tmp_path,
        settings=_settings(uyumsoft_username="secret-user", uyumsoft_password=SecretStr("secret-password")),
    )

    serialized = str(report)
    assert "secret-user" not in serialized
    assert "secret-password" not in serialized
    assert "<Invoice" not in serialized
    assert "Consulting" not in serialized
    assert "1234567890" not in serialized


def test_no_forbidden_provider_operation_invoked(session: Session, tmp_path: Path) -> None:
    client = FakeUyumsoftValidationClient()
    _validate(session=session, tmp_path=tmp_path, client=client)

    assert client.calls == [
        "inspect_wsdl",
        "TestConnection",
        "WhoAmI",
        "GetInboxInvoiceList",
        "GetInboxInvoiceData",
    ]
    assert "SetInvoicesTaken" not in str(client.calls)
    assert "SendInvoice" not in str(client.calls)


def test_validation_cli_file_is_available() -> None:
    script = Path("scripts/validate_uyumsoft_test.py")

    assert script.exists()


def _validate(
    *,
    session: Session,
    tmp_path: Path,
    client: FakeUyumsoftValidationClient | None = None,
    settings: Settings | None = None,
    request: UyumsoftTestValidationRequest | None = None,
    storage: LocalDocumentStorage | None = None,
) -> dict[str, Any]:
    return UyumsoftTestValidationService(
        settings=settings or _settings(),
        client=client or FakeUyumsoftValidationClient(),
        session=session,
        storage=storage or LocalDocumentStorage(tmp_path),
    ).validate(request or _request())


def _request(*, limit: int = 5) -> UyumsoftTestValidationRequest:
    return UyumsoftTestValidationRequest(
        from_date=datetime(2026, 7, 1, tzinfo=UTC),
        to_date=datetime(2026, 7, 20, tzinfo=UTC),
        limit=limit,
    )


def _settings(**overrides: Any) -> Settings:
    defaults: dict[str, Any] = {
        "app_env": "development",
        "uyumsoft_environment": "test",
        "uyumsoft_test_wsdl_url": "https://efatura-test.uyumsoft.com.tr/Services/Integration?wsdl",
        "uyumsoft_username": "test-user",
        "uyumsoft_password": SecretStr("test-password"),
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _invoice(invoice_id: str, *, currency: str, tax_number: str | None = "1234567890") -> UyumsoftInvoiceSummary:
    return UyumsoftInvoiceSummary(
        invoice_id=invoice_id,
        ettn=f"{invoice_id}-ettn",
        invoice_number=f"{invoice_id}-number",
        invoice_date=datetime(2026, 7, 17, tzinfo=UTC),
        sender="Sensitive Sender",
        receiver="Sensitive Receiver",
        tax_number=tax_number,
        currency=currency,
        total_amount=Decimal("10.00"),
        direction="Inbox",
        status="NEW",
        extra_fields={"SecretToken": "secret"},
    )
