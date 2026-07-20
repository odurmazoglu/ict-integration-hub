from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from httpx import AsyncClient

from app.schemas.normalized_invoice import (
    NormalizedInvoice,
    NormalizedInvoiceLine,
    NormalizedParty,
)
from app.services import odoo_mapping_preview
from app.services.document_parser import UblInvoiceParser
from app.services.odoo_mapping_preview import OdooMappingPreviewService

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "ubl"


def test_successful_mapping_preview() -> None:
    preview = OdooMappingPreviewService().build_preview(_invoice())

    assert preview.mapping_status == "ready"
    assert preview.missing_fields == []
    assert preview.invoice.move_type == "in_invoice"
    assert preview.invoice.invoice_date.isoformat() == "2026-07-20"
    assert preview.invoice.currency == "TRY"
    assert preview.invoice.journal is not None
    assert preview.invoice.journal.journal_type == "purchase"
    assert preview.invoice.journal.currency == "TRY"
    assert preview.invoice.partner is not None
    assert preview.invoice.partner.lookup_key == "tax_id"
    assert preview.invoice.partner.tax_id == "1111111111"
    assert preview.invoice.invoice_number == "SYN202600001"
    assert preview.invoice.ettn == "11111111-2222-3333-4444-555555555555"


def test_invoice_lines_are_mapped_with_sequence_quantity_price_and_uom() -> None:
    preview = OdooMappingPreviewService().build_preview(_invoice())

    assert len(preview.lines) == 2
    assert preview.lines[0].sequence == 1
    assert preview.lines[0].product is not None
    assert preview.lines[0].product.lookup_key == "name"
    assert preview.lines[0].product.name == "Consulting"
    assert preview.lines[0].description == "Consulting - Synthetic consulting service"
    assert preview.lines[0].quantity == Decimal("2.0000")
    assert preview.lines[0].unit_price == Decimal("100.0000")
    assert preview.lines[0].unit_of_measure == "C62"
    assert preview.lines[0].line_extension_amount == Decimal("200.00")


def test_taxes_currency_references_and_notes_are_mapped() -> None:
    preview = OdooMappingPreviewService().build_preview(_invoice())

    assert preview.invoice.currency == "TRY"
    assert preview.invoice.references == [
        "SYN202600001",
        "11111111-2222-3333-4444-555555555555",
        "ORD-001",
    ]
    assert preview.invoice.notes == ["Safe synthetic fixture."]
    assert preview.invoice.taxes[0].name == "KDV"
    assert preview.invoice.taxes[0].percent == Decimal("18")
    assert preview.invoice.taxes[1].exemption_reason_code == "351"
    assert preview.lines[1].taxes[0].percent == Decimal("0")


def test_missing_partner_is_reported() -> None:
    invoice = _invoice().model_copy(update={"supplier": NormalizedParty()})

    preview = OdooMappingPreviewService().build_preview(invoice)

    assert preview.mapping_status == "needs_review"
    assert preview.invoice.partner is None
    assert _issue_codes(preview.missing_fields) == {"missing_partner"}


def test_missing_partner_tax_id_is_reported_for_name_only_candidate() -> None:
    invoice = _invoice()
    supplier = invoice.supplier.model_copy(update={"tax_id": None})

    preview = OdooMappingPreviewService().build_preview(invoice.model_copy(update={"supplier": supplier}))

    assert preview.mapping_status == "needs_review"
    assert preview.invoice.partner is not None
    assert preview.invoice.partner.lookup_key == "name"
    assert "missing_partner_tax_id" in _issue_codes(preview.missing_fields)


def test_missing_tax_is_reported_for_invoice_and_lines() -> None:
    line = _invoice().lines[0].model_copy(update={"tax_totals": []})
    invoice = _invoice().model_copy(update={"tax_totals": [], "lines": [line]})

    preview = OdooMappingPreviewService().build_preview(invoice)

    assert preview.mapping_status == "needs_review"
    assert {"missing_taxes", "missing_line_tax"} <= _issue_codes(preview.missing_fields)


def test_missing_currency_and_mandatory_values_are_reported() -> None:
    invoice = _invoice().model_copy(update={"document_currency": " ", "invoice_number": "", "ettn": ""})

    preview = OdooMappingPreviewService().build_preview(invoice)

    assert preview.mapping_status == "needs_review"
    assert preview.invoice.currency is None
    assert {"missing_currency", "missing_invoice_number", "missing_ettn"} <= _issue_codes(preview.missing_fields)


def test_decimal_precision_and_derived_unit_price_warning() -> None:
    line = _invoice().lines[0].model_copy(update={"unit_price": None})
    invoice = _invoice().model_copy(update={"lines": [line]})

    preview = OdooMappingPreviewService().build_preview(invoice)

    assert preview.lines[0].unit_price == Decimal("100")
    assert preview.lines[0].quantity == Decimal("2.0000")
    assert "derived_unit_price" in _issue_codes(preview.warnings)


def test_timezone_aware_date_validation() -> None:
    invoice = _invoice().model_copy(update={"issue_datetime": datetime(2026, 7, 20, 10, 15, 30)})

    preview = OdooMappingPreviewService().build_preview(invoice)

    assert preview.invoice.invoice_date.isoformat() == "2026-07-20"
    assert "missing_timezone" in _issue_codes(preview.missing_fields)


def test_missing_line_mandatory_fields_are_reported() -> None:
    line = NormalizedInvoiceLine(
        line_id="1",
        item_name=None,
        description=None,
        quantity=None,
        unit_code=None,
        unit_price=None,
        line_extension_amount=Decimal("1.00"),
        allowance_charges=[],
        tax_totals=[],
    )
    invoice = _invoice().model_copy(update={"lines": [line]})

    preview = OdooMappingPreviewService().build_preview(invoice)

    assert preview.mapping_status == "needs_review"
    assert preview.lines[0].description == "Line 1"
    assert {
        "missing_line_description",
        "missing_product",
        "missing_line_quantity",
        "missing_unit_price",
    } <= _issue_codes(preview.missing_fields)


def test_preview_output_validation_is_strict() -> None:
    preview = OdooMappingPreviewService().build_preview(_invoice())
    dumped = preview.model_dump()

    assert set(dumped) == {"invoice", "lines", "warnings", "missing_fields", "mapping_status"}
    assert preview.invoice.model_config["extra"] == "forbid"


def test_structured_logging_is_safe(monkeypatch: Any) -> None:
    log_calls: list[dict[str, Any]] = []

    def capture_log(message: str, *args: Any, **kwargs: Any) -> None:
        log_calls.append({"message": message, **kwargs})

    monkeypatch.setattr(odoo_mapping_preview.logger, "info", capture_log)
    OdooMappingPreviewService().build_preview(_invoice())

    assert log_calls[0]["message"] == "odoo_mapping_preview_completed"
    assert log_calls[0]["extra"]["invoice_id"] == "SYN202600001"
    assert log_calls[0]["extra"]["line_count"] == 2
    assert "Synthetic Supplier" not in str(log_calls)
    assert "<Invoice" not in str(log_calls)


def test_mapper_is_provider_independent() -> None:
    source = (Path(__file__).resolve().parents[2] / "app" / "services" / "odoo_mapping_preview.py").read_text()

    assert "connectors" not in source
    assert "uyumsoft" not in source.lower()
    assert "xml" not in source.lower()
    assert "OdooJson2Client" not in source


async def test_mapping_preview_endpoint_returns_payload(api_client: AsyncClient) -> None:
    response = await api_client.post("/api/v1/odoo/mapping-preview", json=_invoice().model_dump(mode="json"))

    assert response.status_code == 200
    body = response.json()
    assert body["mapping_status"] == "ready"
    assert body["invoice"]["move_type"] == "in_invoice"
    assert body["invoice"]["currency"] == "TRY"
    assert body["lines"][0]["sequence"] == 1


def _invoice() -> NormalizedInvoice:
    return UblInvoiceParser().parse((FIXTURE_ROOT / "valid_invoice.xml").read_bytes())


def _issue_codes(issues: list[Any]) -> set[str]:
    return {issue.code for issue in issues}
