from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.billing import VendorBillBuilder, VendorBillBuildError, VendorBillLine, to_odoo_account_move_payload
from app.domain.invoice import Header, InternalInvoice, InvoiceLine, MonetaryTotals, Party, Tax
from app.matching import (
    InvoiceProductLineResult,
    InvoiceProductMatchResult,
    PartnerMatchResult,
    PartnerMatchStatus,
    ProductMatchResult,
    ProductMatchStatus,
)
from app.tax_mapping import InvoiceTaxLineResult, InvoiceTaxMappingResult, TaxMatchResult, TaxMatchStatus, TaxType


def test_successful_build() -> None:
    bill = VendorBillBuilder().build(_invoice([_line("1")]), _partner(), _products([_product_line("1", 501)]), _taxes())

    assert bill.supplier_id == 101
    assert bill.invoice_number == "INV-1"
    assert bill.invoice_date == date(2026, 7, 21)
    assert bill.currency == "TRY"
    assert bill.external_uuid == "uuid-1"
    assert bill.reference == "INV-1"
    assert bill.invoice_lines == (
        VendorBillLine(
            product_id=501,
            quantity=Decimal("2"),
            uom="NIU",
            unit_price=Decimal("10.50"),
            tax_ids=(401,),
            description="Line 1",
        ),
    )
    assert bill.notes == ("note one",)


def test_multiple_lines_and_multiple_taxes() -> None:
    invoice = _invoice(
        [
            _line("1", taxes=(Tax(tax_type="VAT", rate=Decimal("20")), Tax(tax_type="WITHHOLDING", rate=Decimal("5")))),
            _line("2", quantity=Decimal("1"), unit_price=Decimal("3.00")),
        ]
    )
    tax_match = InvoiceTaxMappingResult(
        line_results=(
            _tax_line("1", 0, 401),
            _tax_line("1", 1, 402),
            _tax_line("2", 0, 403),
        )
    )

    bill = VendorBillBuilder().build(
        invoice,
        _partner(),
        _products([_product_line("1", 501), _product_line("2", 502)]),
        tax_match,
    )

    assert [line.product_id for line in bill.invoice_lines] == [501, 502]
    assert [line.tax_ids for line in bill.invoice_lines] == [(401, 402), (403,)]


def test_missing_partner_is_rejected() -> None:
    with pytest.raises(VendorBillBuildError) as exc_info:
        VendorBillBuilder().build(
            _invoice([_line("1")]),
            _partner(status=PartnerMatchStatus.NOT_FOUND, partner_id=None),
            _products([_product_line("1", 501)]),
            _taxes(),
        )

    assert exc_info.value.errors == ("Supplier partner must be matched before building a vendor bill.",)


def test_missing_product_is_rejected() -> None:
    with pytest.raises(VendorBillBuildError) as exc_info:
        VendorBillBuilder().build(_invoice([_line("1")]), _partner(), _products([]), _taxes())

    assert "lines[0].product must be matched." in exc_info.value.errors


def test_unmatched_product_result_is_rejected() -> None:
    product_match = _products([_product_line("1", None, status=ProductMatchStatus.NOT_FOUND)])

    with pytest.raises(VendorBillBuildError) as exc_info:
        VendorBillBuilder().build(_invoice([_line("1")]), _partner(), product_match, _taxes())

    assert "Product mapping for line 1 is not matched." in exc_info.value.errors


def test_missing_tax_is_rejected() -> None:
    with pytest.raises(VendorBillBuildError) as exc_info:
        VendorBillBuilder().build(
            _invoice([_line("1")]), _partner(), _products([_product_line("1", 501)]), _empty_tax()
        )

    assert "lines[0].taxes[0] must be matched." in exc_info.value.errors


def test_unmatched_tax_result_is_rejected() -> None:
    tax_match = InvoiceTaxMappingResult(line_results=(_tax_line("1", 0, None, status=TaxMatchStatus.NOT_FOUND),))

    with pytest.raises(VendorBillBuildError) as exc_info:
        VendorBillBuilder().build(_invoice([_line("1")]), _partner(), _products([_product_line("1", 501)]), tax_match)

    assert "Tax mapping for line 1 tax 0 is not matched." in exc_info.value.errors


def test_negative_quantity_and_negative_price_are_rejected() -> None:
    invoice = _invoice([_line("1", quantity=Decimal("-1"), unit_price=Decimal("-0.01"))])

    with pytest.raises(VendorBillBuildError) as exc_info:
        VendorBillBuilder().build(invoice, _partner(), _products([_product_line("1", 501)]), _taxes())

    assert "lines[0].quantity must be greater than zero." in exc_info.value.errors
    assert "lines[0].unit_price must not be negative." in exc_info.value.errors


def test_missing_invoice_header_fields_are_rejected() -> None:
    invoice = InternalInvoice(
        header=Header(invoice_number=" ", invoice_uuid="uuid-1", issue_date=None, currency_code=" "),
        supplier=Party(name="Supplier"),
        customer=Party(name="Customer"),
        totals=MonetaryTotals(payable_amount=Decimal("21.00")),
        lines=(_line("1"),),
    )

    with pytest.raises(VendorBillBuildError) as exc_info:
        VendorBillBuilder().build(invoice, _partner(), _products([_product_line("1", 501)]), _taxes())

    assert "Invoice number is required." in exc_info.value.errors
    assert "Invoice date is required." in exc_info.value.errors
    assert "Invoice currency is required." in exc_info.value.errors


def test_duplicate_mappings_are_rejected() -> None:
    product_match = _products([_product_line("1", 501), _product_line("1", 502)])
    tax_match = InvoiceTaxMappingResult(line_results=(_tax_line("1", 0, 401), _tax_line("1", 0, 402)))

    with pytest.raises(VendorBillBuildError) as exc_info:
        VendorBillBuilder().build(_invoice([_line("1")]), _partner(), product_match, tax_match)

    assert "Duplicate product mapping for line 1." in exc_info.value.errors
    assert "Duplicate tax mapping for line 1 tax 0." in exc_info.value.errors


def test_payload_generation_is_deterministic() -> None:
    bill = VendorBillBuilder().build(_invoice([_line("1")]), _partner(), _products([_product_line("1", 501)]), _taxes())

    payload = to_odoo_account_move_payload(bill, currency_id=31)

    assert payload == {
        "move_type": "in_invoice",
        "partner_id": 101,
        "invoice_date": "2026-07-21",
        "ref": "INV-1",
        "currency": "TRY",
        "invoice_line_ids": (
            (
                0,
                0,
                {
                    "product_id": 501,
                    "quantity": "2",
                    "price_unit": "10.50",
                    "tax_ids": ((6, 0, (401,)),),
                    "name": "Line 1",
                    "product_uom_id": "NIU",
                },
            ),
        ),
        "currency_id": 31,
        "narration": "note one",
    }


def test_payload_does_not_invent_currency_id() -> None:
    bill = VendorBillBuilder().build(_invoice([_line("1")]), _partner(), _products([_product_line("1", 501)]), _taxes())

    payload = to_odoo_account_move_payload(bill)

    assert "currency_id" not in payload
    assert payload["currency"] == "TRY"


def test_vendor_bill_dtos_are_immutable() -> None:
    bill = VendorBillBuilder().build(_invoice([_line("1")]), _partner(), _products([_product_line("1", 501)]), _taxes())

    with pytest.raises(FrozenInstanceError):
        bill.supplier_id = 202  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        bill.invoice_lines[0].product_id = 999  # type: ignore[misc]


def test_billing_package_has_no_write_repository_http_or_db_dependency() -> None:
    package_root = Path(__file__).resolve().parents[2] / "app" / "billing"
    combined_source = "\n".join(path.read_text() for path in package_root.rglob("*.py"))

    assert "OdooJson2Client" not in combined_source
    assert "OdooReadOnlyAdapter" not in combined_source
    assert "RepositoryProvider" not in combined_source
    assert "search_read" not in combined_source
    assert "httpx" not in combined_source
    assert "requests" not in combined_source
    assert "sqlalchemy" not in combined_source.lower()
    assert "app.db" not in combined_source
    assert "create_account_move" not in combined_source
    assert ".create(" not in combined_source
    assert ".write(" not in combined_source
    assert "unlink" not in combined_source
    assert "action_post" not in combined_source


def _invoice(lines: list[InvoiceLine]) -> InternalInvoice:
    return InternalInvoice(
        header=Header(
            invoice_number="INV-1",
            invoice_uuid="uuid-1",
            issue_date=date(2026, 7, 21),
            currency_code="TRY",
            notes=(" note one ",),
        ),
        supplier=Party(name="Supplier"),
        customer=Party(name="Customer"),
        totals=MonetaryTotals(payable_amount=Decimal("21.00")),
        lines=tuple(lines),
    )


def _line(
    line_number: str | None,
    *,
    quantity: Decimal = Decimal("2"),
    unit_price: Decimal = Decimal("10.50"),
    taxes: tuple[Tax, ...] = (Tax(tax_type="VAT", rate=Decimal("20")),),
) -> InvoiceLine:
    return InvoiceLine(
        line_number=line_number,
        description=f"Line {line_number}",
        quantity=quantity,
        unit_code="NIU",
        unit_price=unit_price,
        taxes=taxes,
    )


def _partner(
    *,
    status: PartnerMatchStatus = PartnerMatchStatus.MATCHED,
    partner_id: int | None = 101,
) -> PartnerMatchResult:
    return PartnerMatchResult(
        status=status,
        partner_id=partner_id,
        matched_by="tax_number" if status is PartnerMatchStatus.MATCHED else None,
        reason="matched",
        candidate_count=1 if status is PartnerMatchStatus.MATCHED else 0,
        confidence=Decimal("1.00") if status is PartnerMatchStatus.MATCHED else None,
    )


def _products(line_results: list[InvoiceProductLineResult]) -> InvoiceProductMatchResult:
    return InvoiceProductMatchResult(line_results=tuple(line_results))


def _product_line(
    line_number: str,
    product_id: int | None,
    *,
    status: ProductMatchStatus = ProductMatchStatus.MATCHED,
) -> InvoiceProductLineResult:
    return InvoiceProductLineResult(
        line_number=line_number,
        result=ProductMatchResult(
            status=status,
            line_number=line_number,
            product_id=product_id,
            default_code="SKU-1",
            barcode=None,
            seller_item_code=None,
            matched_by="default_code" if status is ProductMatchStatus.MATCHED else None,
            reason="matched",
            candidate_count=1 if status is ProductMatchStatus.MATCHED else 0,
            confidence=Decimal("1.00") if status is ProductMatchStatus.MATCHED else None,
        ),
    )


def _taxes() -> InvoiceTaxMappingResult:
    return InvoiceTaxMappingResult(line_results=(_tax_line("1", 0, 401),))


def _empty_tax() -> InvoiceTaxMappingResult:
    return InvoiceTaxMappingResult()


def _tax_line(
    line_number: str,
    tax_index: int,
    tax_id: int | None,
    *,
    status: TaxMatchStatus = TaxMatchStatus.MATCHED,
) -> InvoiceTaxLineResult:
    return InvoiceTaxLineResult(
        line_number=line_number,
        tax_index=tax_index,
        result=TaxMatchResult(
            status=status,
            tax_id=tax_id,
            company_id=7,
            tax_type=TaxType.VAT,
            tax_rate=Decimal("20"),
            matched_by="company_type_rate" if status is TaxMatchStatus.MATCHED else None,
            confidence=Decimal("1.00") if status is TaxMatchStatus.MATCHED else None,
            reason="matched",
            candidate_count=1 if status is TaxMatchStatus.MATCHED else 0,
        ),
    )
