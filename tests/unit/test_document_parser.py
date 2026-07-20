from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.models.invoice_document import InvoiceDocument
from app.services import document_parser
from app.services.document_parser import (
    DocumentReadError,
    InvalidDateTimeValueError,
    InvalidDecimalValueError,
    InvoiceDocumentParseService,
    MalformedXmlError,
    MissingRequiredFieldError,
    UblInvoiceParser,
    UnsupportedDocumentTypeError,
    UnsupportedInvoiceStructureError,
)
from app.services.document_storage import DocumentStorageError, LocalDocumentStorage

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "ubl"


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db_session:
        yield db_session


class FailingReadStorage(LocalDocumentStorage):
    def read(self, storage_key: str) -> bytes:
        raise DocumentStorageError("Document storage read failed.")


def test_representative_valid_ubl_invoice_header_and_datetime() -> None:
    invoice = UblInvoiceParser().parse(_fixture("valid_invoice.xml"))

    assert invoice.invoice_number == "SYN202600001"
    assert invoice.ettn == "11111111-2222-3333-4444-555555555555"
    assert invoice.invoice_type_code == "SATIS"
    assert invoice.profile_id == "TEMELFATURA"
    assert invoice.issue_datetime == datetime(2026, 7, 20, 10, 15, 30, tzinfo=timezone(timedelta(hours=3)))
    assert invoice.document_currency == "TRY"
    assert invoice.notes == ["Safe synthetic fixture."]
    assert invoice.references == ["ORD-001"]


def test_supplier_and_customer_extraction() -> None:
    invoice = UblInvoiceParser().parse(_fixture("valid_invoice.xml"))

    assert invoice.supplier.tax_id == "1111111111"
    assert invoice.supplier.party_name == "Synthetic Supplier Ltd"
    assert invoice.supplier.tax_office == "Besiktas"
    assert invoice.supplier.address is not None
    assert invoice.supplier.address.street_name == "Supplier Street"
    assert invoice.supplier.address.city_name == "Istanbul"
    assert invoice.supplier.contact is not None
    assert invoice.supplier.contact.email == "supplier@example.invalid"
    assert invoice.customer.tax_id == "2222222222"
    assert invoice.customer.party_name == "Synthetic Customer A.S."
    assert invoice.customer.address is not None
    assert invoice.customer.address.city_name == "Ankara"


def test_tax_totals_subtotals_and_exemption_fields() -> None:
    invoice = UblInvoiceParser().parse(_fixture("valid_invoice.xml"))

    assert invoice.tax_totals[0].total_tax_amount == Decimal("36.00")
    assert invoice.tax_totals[0].subtotals[0].tax_category == "KDV"
    assert invoice.tax_totals[0].subtotals[0].percent == Decimal("18")
    assert invoice.tax_totals[0].subtotals[0].taxable_amount == Decimal("200.00")
    assert invoice.tax_totals[0].subtotals[0].tax_amount == Decimal("36.00")
    assert invoice.tax_totals[0].subtotals[1].exemption_reason_code == "351"
    assert invoice.tax_totals[0].subtotals[1].exemption_reason == "Istisna"


def test_monetary_totals_and_decimal_precision() -> None:
    invoice = UblInvoiceParser().parse(_fixture("valid_invoice.xml"))

    assert invoice.monetary_totals.line_extension_amount == Decimal("250.00")
    assert invoice.monetary_totals.tax_exclusive_amount == Decimal("250.00")
    assert invoice.monetary_totals.tax_inclusive_amount == Decimal("286.00")
    assert invoice.monetary_totals.allowance_total_amount == Decimal("5.50")
    assert invoice.monetary_totals.charge_total_amount == Decimal("1.25")
    assert invoice.monetary_totals.payable_amount == Decimal("281.75")
    assert invoice.lines[0].quantity == Decimal("2.0000")
    assert invoice.lines[0].unit_price == Decimal("100.0000")


def test_multiple_line_items_and_line_level_tax_information() -> None:
    invoice = UblInvoiceParser().parse(_fixture("valid_invoice.xml"))

    assert len(invoice.lines) == 2
    assert invoice.lines[0].line_id == "1"
    assert invoice.lines[0].item_name == "Consulting"
    assert invoice.lines[0].description == "Synthetic consulting service"
    assert invoice.lines[0].unit_code == "C62"
    assert invoice.lines[0].line_extension_amount == Decimal("200.00")
    assert invoice.lines[0].allowance_charges[0].charge_indicator is False
    assert invoice.lines[0].allowance_charges[0].amount == Decimal("5.50")
    assert invoice.lines[0].tax_totals[0].subtotals[0].percent == Decimal("18")
    assert invoice.lines[1].item_name == "Exempt item"
    assert invoice.lines[1].tax_totals[0].subtotals[0].exemption_reason_code == "351"


def test_optional_missing_fields_and_timezone_default_to_utc() -> None:
    invoice = UblInvoiceParser().parse(_fixture("minimal_invoice.xml"))

    assert invoice.issue_datetime == datetime(2026, 7, 20, 0, 0, tzinfo=UTC)
    assert invoice.invoice_type_code is None
    assert invoice.profile_id is None
    assert invoice.notes == []
    assert invoice.references == []
    assert invoice.supplier.tax_id is None
    assert invoice.lines[0].quantity is None
    assert invoice.lines[0].tax_totals == []


def test_malformed_xml_is_explicit_and_safe() -> None:
    xml = b"<Invoice><cbc:ID>SECRET-CONTENT</cbc:ID>"

    with pytest.raises(MalformedXmlError) as exc_info:
        UblInvoiceParser().parse(xml)

    assert exc_info.value.category == "malformed_xml"
    assert "SECRET-CONTENT" not in exc_info.value.safe_message
    assert "<Invoice" not in str(exc_info.value)


def test_doctype_and_entities_are_rejected() -> None:
    xml = b'<!DOCTYPE Invoice [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><Invoice>&xxe;</Invoice>'

    with pytest.raises(MalformedXmlError):
        UblInvoiceParser().parse(xml)


def test_missing_required_field_reports_safe_path() -> None:
    xml = _fixture("minimal_invoice.xml").replace(b"<cbc:UUID>aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee</cbc:UUID>", b"")

    with pytest.raises(MissingRequiredFieldError) as exc_info:
        UblInvoiceParser().parse(xml)

    assert exc_info.value.field_path == "Invoice/cbc:UUID"
    assert "aaaaaaaa" not in exc_info.value.safe_message


def test_invalid_decimal_reports_field_path() -> None:
    xml = _fixture("minimal_invoice.xml").replace(b">1.00</cbc:PayableAmount>", b">bad</cbc:PayableAmount>")

    with pytest.raises(InvalidDecimalValueError) as exc_info:
        UblInvoiceParser().parse(xml)

    assert exc_info.value.field_path == "Invoice/cac:LegalMonetaryTotal/cbc:PayableAmount"
    assert "bad" not in exc_info.value.safe_message


def test_invalid_datetime_reports_field_path() -> None:
    xml = _fixture("minimal_invoice.xml").replace(
        b"<cbc:IssueDate>2026-07-20</cbc:IssueDate>", b"<cbc:IssueDate>bad</cbc:IssueDate>"
    )

    with pytest.raises(InvalidDateTimeValueError) as exc_info:
        UblInvoiceParser().parse(xml)

    assert exc_info.value.field_path == "Invoice/cbc:IssueDate"


def test_unsupported_invoice_structure() -> None:
    xml = b"<CreditNote/>"

    with pytest.raises(UnsupportedInvoiceStructureError):
        UblInvoiceParser().parse(xml)


def test_parse_service_reads_stored_document_and_logs_safely(
    monkeypatch: pytest.MonkeyPatch,
    session: Session,
    tmp_path: Path,
) -> None:
    storage = LocalDocumentStorage(tmp_path)
    storage.write("synthetic/valid.xml", _fixture("valid_invoice.xml"))
    document = _document(session, storage_key="synthetic/valid.xml")
    log_calls: list[dict[str, Any]] = []

    def capture_log(message: str, *args: Any, **kwargs: Any) -> None:
        log_calls.append({"message": message, **kwargs})

    monkeypatch.setattr(document_parser.logger, "info", capture_log)
    result = InvoiceDocumentParseService(session=session, storage=storage).parse_document(document.id)

    assert result.document_id == document.id
    assert result.storage_key == "synthetic/valid.xml"
    assert result.invoice.invoice_number == "SYN202600001"
    assert result.diagnostics == []
    assert log_calls[0]["message"] == "invoice_document_parse_completed"
    assert log_calls[0]["extra"]["document_id"] == document.id
    assert "Synthetic Supplier" not in str(log_calls)
    assert "<Invoice" not in str(log_calls)


def test_parse_service_rejects_unsupported_document_type(session: Session, tmp_path: Path) -> None:
    document = _document(session, storage_key="synthetic/valid.xml", document_type="PDF")

    with pytest.raises(UnsupportedDocumentTypeError) as exc_info:
        InvoiceDocumentParseService(session=session, storage=LocalDocumentStorage(tmp_path)).parse_document(document.id)

    assert exc_info.value.category == "unsupported_document_type"
    assert exc_info.value.field_path == "document_type"


def test_parse_service_storage_read_failure(session: Session, tmp_path: Path) -> None:
    document = _document(session, storage_key="missing.xml")

    with pytest.raises(DocumentReadError) as exc_info:
        InvoiceDocumentParseService(session=session, storage=FailingReadStorage(tmp_path)).parse_document(document.id)

    assert exc_info.value.category == "storage_read_failure"
    assert "missing.xml" not in exc_info.value.safe_message


def test_parse_service_safe_failure_log_has_no_xml(
    monkeypatch: pytest.MonkeyPatch,
    session: Session,
    tmp_path: Path,
) -> None:
    storage = LocalDocumentStorage(tmp_path)
    storage.write("synthetic/bad.xml", b"<Invoice><Secret>should-not-leak</Secret>")
    document = _document(session, storage_key="synthetic/bad.xml")
    log_calls: list[dict[str, Any]] = []

    def capture_log(message: str, *args: Any, **kwargs: Any) -> None:
        log_calls.append({"message": message, **kwargs})

    monkeypatch.setattr(document_parser.logger, "info", capture_log)
    with pytest.raises(MalformedXmlError):
        InvoiceDocumentParseService(session=session, storage=storage).parse_document(document.id)

    assert log_calls[0]["message"] == "invoice_document_parse_failed"
    assert log_calls[0]["extra"]["category"] == "malformed_xml"
    assert "should-not-leak" not in str(log_calls)
    assert "<Secret" not in str(log_calls)


def test_parser_is_provider_independent() -> None:
    parser_source = (Path(__file__).resolve().parents[2] / "app" / "services" / "document_parser.py").read_text()

    assert "connectors.uyumsoft" not in parser_source
    assert "odoo" not in parser_source.lower()
    assert "SOAP" not in parser_source


def _fixture(name: str) -> bytes:
    return (FIXTURE_ROOT / name).read_bytes()


def _document(
    session: Session,
    *,
    storage_key: str,
    document_type: str = "UBL_XML",
) -> InvoiceDocument:
    document = InvoiceDocument(
        invoice_id=1,
        provider="uyumsoft",
        direction="Inbox",
        document_type=document_type,
        storage_backend="local_filesystem",
        storage_key=storage_key,
        content_hash_sha256="0" * 64,
        mime_type="application/xml",
        content_size_bytes=1,
        downloaded_at=datetime(2026, 7, 20, tzinfo=UTC),
    )
    session.add(document)
    session.flush()
    return document
