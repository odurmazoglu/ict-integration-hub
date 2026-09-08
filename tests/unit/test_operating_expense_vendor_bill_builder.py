"""Account-only Vendor Bill builder + Odoo payload (P0-3C4 / PR 3).

Product-line behavior must stay byte-for-byte identical; a deterministic
operating-expense invoice now builds account-only lines with a pinned expense
account. No evidence, execution, or classification change.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date
from decimal import Decimal

import pytest

from app.application.expense_mapping import OperatingExpenseMatchResult, OperatingExpenseMatchStatus
from app.billing import VendorBillBuilder, VendorBillBuildError, VendorBillLine, to_odoo_account_move_payload
from app.billing.builder import validate_vendor_bill_inputs
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

EXPENSE_ACCOUNT_ID = 9001
TAX_ID = 401


# --------------------------------------------------------------------------- builders


def _line(
    line_number: str = "1",
    *,
    buyer_item_code: str | None = None,
    seller_item_code: str | None = None,
    barcode: str | None = None,
    quantity: Decimal = Decimal("2"),
    unit_price: Decimal = Decimal("10.50"),
) -> InvoiceLine:
    return InvoiceLine(
        line_number=line_number,
        description=f"Line {line_number}",
        buyer_item_code=buyer_item_code,
        seller_item_code=seller_item_code,
        barcode=barcode,
        quantity=quantity,
        unit_code="NIU",
        unit_price=unit_price,
        taxes=(Tax(tax_type="KDV", rate=Decimal("20")),),
    )


def _invoice(lines: list[InvoiceLine] | None = None) -> InternalInvoice:
    return InternalInvoice(
        header=Header(
            invoice_number="INV-1",
            invoice_uuid="uuid-1",
            ettn="uuid-1",
            issue_date=date(2026, 7, 21),
            currency_code="TRY",
        ),
        supplier=Party(name="Supplier", tax_number="0430367181"),
        customer=Party(name="ICT", tax_number="4651205941"),
        totals=MonetaryTotals(payable_amount=Decimal("21.00")),
        lines=tuple(lines if lines is not None else [_line("1")]),
    )


def _partner(status: PartnerMatchStatus = PartnerMatchStatus.MATCHED) -> PartnerMatchResult:
    matched = status is PartnerMatchStatus.MATCHED
    return PartnerMatchResult(
        status=status,
        partner_id=101 if matched else None,
        matched_by="tax_number" if matched else None,
        reason="matched" if matched else "unmatched",
        candidate_count=1 if matched else 0,
        confidence=Decimal("1.00") if matched else None,
    )


def _product_line(line_number: str, status: ProductMatchStatus, *, product_id: int | None) -> InvoiceProductLineResult:
    return InvoiceProductLineResult(
        line_number=line_number,
        result=ProductMatchResult(
            status=status,
            line_number=line_number,
            product_id=product_id,
            default_code="SKU-1" if status is ProductMatchStatus.MATCHED else None,
            barcode=None,
            seller_item_code=None,
            matched_by="default_code" if status is ProductMatchStatus.MATCHED else None,
            reason="At least one deterministic product identifier is required."
            if status is ProductMatchStatus.INVALID_INPUT
            else "result",
            candidate_count=1 if status is ProductMatchStatus.MATCHED else 0,
            confidence=Decimal("1.00") if status is ProductMatchStatus.MATCHED else None,
        ),
    )


def _matched_products(invoice: InternalInvoice) -> InvoiceProductMatchResult:
    return InvoiceProductMatchResult(
        line_results=tuple(
            _product_line(line.line_number, ProductMatchStatus.MATCHED, product_id=501) for line in invoice.lines
        )
    )


def _identifier_free_products(invoice: InternalInvoice) -> InvoiceProductMatchResult:
    return InvoiceProductMatchResult(
        line_results=tuple(
            _product_line(line.line_number, ProductMatchStatus.INVALID_INPUT, product_id=None) for line in invoice.lines
        )
    )


def _status_products(invoice: InternalInvoice, status: ProductMatchStatus) -> InvoiceProductMatchResult:
    return InvoiceProductMatchResult(
        line_results=tuple(_product_line(line.line_number, status, product_id=None) for line in invoice.lines)
    )


def _taxes(invoice: InternalInvoice, status: TaxMatchStatus = TaxMatchStatus.MATCHED) -> InvoiceTaxMappingResult:
    matched = status is TaxMatchStatus.MATCHED
    return InvoiceTaxMappingResult(
        line_results=tuple(
            InvoiceTaxLineResult(
                line_number=line.line_number,
                tax_index=tax_index,
                result=TaxMatchResult(
                    status=status,
                    tax_id=TAX_ID if matched else None,
                    company_id=1 if matched else None,
                    tax_type=TaxType.VAT,
                    tax_rate=Decimal("20"),
                    matched_by="company_type_rate" if matched else None,
                    confidence=Decimal("1.00") if matched else None,
                    reason="matched" if matched else "unmatched",
                    candidate_count=1 if matched else 2,
                ),
            )
            for line in invoice.lines
            for tax_index, _tax in enumerate(line.taxes)
        )
    )


def _expense_match(
    status: OperatingExpenseMatchStatus = OperatingExpenseMatchStatus.MATCHED,
    *,
    expense_account_id: int | None = EXPENSE_ACCOUNT_ID,
) -> OperatingExpenseMatchResult:
    if status is OperatingExpenseMatchStatus.MATCHED:
        return OperatingExpenseMatchResult(
            status=status,
            reason="mapped",
            candidate_count=1,
            mapping_id=7,
            company_id=1,
            vendor_partner_id=101,
            expense_account_id=expense_account_id,
            expense_category="OFFICE_BUILDING_EXPENSE",
            matched_by="company_partner",
            confidence=Decimal("1.00"),
        )
    return OperatingExpenseMatchResult(status=status, reason="not matched", candidate_count=0)


# --------------------------------------------------------------------------- VendorBillLine contract (1-8)


def test_product_line_valid_with_only_product_id() -> None:
    line = VendorBillLine(product_id=501, quantity=Decimal("1"), uom="NIU", unit_price=Decimal("10"))
    assert line.product_id == 501
    assert line.account_id is None


def test_expense_line_valid_with_only_account_id() -> None:
    line = VendorBillLine(product_id=None, account_id=9001, quantity=Decimal("1"), uom=None, unit_price=Decimal("10"))
    assert line.account_id == 9001
    assert line.product_id is None


def test_line_rejects_both_product_and_account() -> None:
    with pytest.raises(ValueError):
        VendorBillLine(product_id=501, account_id=9001, quantity=Decimal("1"), uom=None, unit_price=Decimal("10"))


def test_line_rejects_neither_product_nor_account() -> None:
    with pytest.raises(ValueError):
        VendorBillLine(product_id=None, account_id=None, quantity=Decimal("1"), uom=None, unit_price=Decimal("10"))


@pytest.mark.parametrize("bad", [0, -1])
def test_line_rejects_non_positive_account_id(bad: int) -> None:
    with pytest.raises(ValueError):
        VendorBillLine(product_id=None, account_id=bad, quantity=Decimal("1"), uom=None, unit_price=Decimal("10"))


@pytest.mark.parametrize("bad", [0, -5])
def test_line_rejects_non_positive_product_id(bad: int) -> None:
    with pytest.raises(ValueError):
        VendorBillLine(product_id=bad, quantity=Decimal("1"), uom=None, unit_price=Decimal("10"))


def test_line_rejects_bool_ids() -> None:
    with pytest.raises(ValueError):
        VendorBillLine(product_id=True, quantity=Decimal("1"), uom=None, unit_price=Decimal("10"))  # noqa: FBT003
    with pytest.raises(ValueError):
        VendorBillLine(product_id=None, account_id=True, quantity=Decimal("1"), uom=None, unit_price=Decimal("10"))  # noqa: FBT003


def test_line_is_frozen() -> None:
    line = VendorBillLine(product_id=501, quantity=Decimal("1"), uom="NIU", unit_price=Decimal("10"))
    with pytest.raises(FrozenInstanceError):
        line.account_id = 9001  # type: ignore[misc]


# --------------------------------------------------------------------------- product path unchanged (9-11, 27)


def test_existing_product_validation_still_valid() -> None:
    invoice = _invoice([_line("1", buyer_item_code="SKU-1")])
    result = validate_vendor_bill_inputs(invoice, _partner(), _matched_products(invoice), _taxes(invoice), company_id=1)
    assert result.is_valid


def test_product_builder_output_unchanged() -> None:
    invoice = _invoice([_line("1", buyer_item_code="SKU-1")])
    bill = VendorBillBuilder().build(invoice, _partner(), _matched_products(invoice), _taxes(invoice), company_id=1)

    assert bill.invoice_lines == (
        VendorBillLine(
            product_id=501,
            quantity=Decimal("2"),
            uom="NIU",
            unit_price=Decimal("10.50"),
            tax_ids=(TAX_ID,),
            description="Line 1",
        ),
    )
    assert bill.invoice_lines[0].account_id is None


def test_product_odoo_payload_snapshot_unchanged() -> None:
    invoice = _invoice([_line("1", buyer_item_code="SKU-1")])
    bill = VendorBillBuilder().build(invoice, _partner(), _matched_products(invoice), _taxes(invoice), company_id=1)

    payload = to_odoo_account_move_payload(bill, currency_id=31)

    assert payload["invoice_line_ids"] == (
        (
            0,
            0,
            {
                "product_id": 501,
                "quantity": "2",
                "price_unit": "10.50",
                "tax_ids": ((6, 0, (TAX_ID,)),),
                "name": "Line 1",
                "product_uom_id": "NIU",
            },
        ),
    )


def test_product_mode_wins_when_both_product_and_expense_match_present() -> None:
    invoice = _invoice([_line("1", buyer_item_code="SKU-1")])
    bill = VendorBillBuilder().build(
        invoice,
        _partner(),
        _matched_products(invoice),
        _taxes(invoice),
        company_id=1,
        operating_expense_match=_expense_match(),
    )
    assert bill.invoice_lines[0].product_id == 501
    assert bill.invoice_lines[0].account_id is None


# --------------------------------------------------------------------------- expense path valid (12-14, 28)


def test_identifier_free_invoice_with_expense_match_is_valid() -> None:
    invoice = _invoice()
    result = validate_vendor_bill_inputs(
        invoice,
        _partner(),
        _identifier_free_products(invoice),
        _taxes(invoice),
        company_id=1,
        operating_expense_match=_expense_match(),
    )
    assert result.is_valid


def test_expense_builder_produces_account_only_lines() -> None:
    invoice = _invoice()
    bill = VendorBillBuilder().build(
        invoice,
        _partner(),
        _identifier_free_products(invoice),
        _taxes(invoice),
        company_id=1,
        operating_expense_match=_expense_match(),
    )
    assert len(bill.invoice_lines) == 1
    line = bill.invoice_lines[0]
    assert line.product_id is None
    assert line.account_id == EXPENSE_ACCOUNT_ID
    assert line.tax_ids == (TAX_ID,)
    assert line.uom is None


def test_multi_line_expense_invoice_uses_same_pinned_account() -> None:
    invoice = _invoice([_line("1"), _line("2", quantity=Decimal("1"), unit_price=Decimal("3.00"))])
    bill = VendorBillBuilder().build(
        invoice,
        _partner(),
        _identifier_free_products(invoice),
        _taxes(invoice),
        company_id=1,
        operating_expense_match=_expense_match(),
    )
    assert [line.account_id for line in bill.invoice_lines] == [EXPENSE_ACCOUNT_ID, EXPENSE_ACCOUNT_ID]
    assert all(line.product_id is None for line in bill.invoice_lines)


# --------------------------------------------------------------------------- expense Odoo payload (14-17)


def test_expense_odoo_payload_has_account_id_and_no_product_id() -> None:
    invoice = _invoice()
    bill = VendorBillBuilder().build(
        invoice,
        _partner(),
        _identifier_free_products(invoice),
        _taxes(invoice),
        company_id=1,
        operating_expense_match=_expense_match(),
    )

    payload = to_odoo_account_move_payload(bill)
    line_payload = payload["invoice_line_ids"][0][2]

    assert line_payload == {
        "name": "Line 1",
        "quantity": "2",
        "price_unit": "10.50",
        "account_id": EXPENSE_ACCOUNT_ID,
        "tax_ids": ((6, 0, (TAX_ID,)),),
    }
    assert "product_id" not in line_payload
    assert "product_uom_id" not in line_payload


def test_expense_payload_passes_account_move_repository_forbidden_field_guard() -> None:
    from app.erp.write.account_move_repository import FORBIDDEN_ACCOUNT_MOVE_FIELDS

    invoice = _invoice()
    bill = VendorBillBuilder().build(
        invoice,
        _partner(),
        _identifier_free_products(invoice),
        _taxes(invoice),
        company_id=1,
        operating_expense_match=_expense_match(),
    )
    payload_text = str(to_odoo_account_move_payload(bill)).lower()
    assert not any(forbidden in payload_text for forbidden in FORBIDDEN_ACCOUNT_MOVE_FIELDS)


# --------------------------------------------------------------------------- fail-closed (18-26)


@pytest.mark.parametrize(
    "product_status",
    [ProductMatchStatus.NOT_FOUND, ProductMatchStatus.MULTIPLE_MATCHES, ProductMatchStatus.INVALID_INPUT],
)
def test_identifier_present_failed_product_never_falls_back_to_expense(product_status: ProductMatchStatus) -> None:
    invoice = _invoice([_line("1", buyer_item_code="SKU-1")])
    result = validate_vendor_bill_inputs(
        invoice,
        _partner(),
        _status_products(invoice, product_status),
        _taxes(invoice),
        company_id=1,
        operating_expense_match=_expense_match(),
    )
    assert not result.is_valid


@pytest.mark.parametrize(
    "status",
    [
        OperatingExpenseMatchStatus.NOT_FOUND,
        OperatingExpenseMatchStatus.MULTIPLE_MATCHES,
        OperatingExpenseMatchStatus.INVALID_INPUT,
    ],
)
def test_identifier_free_without_matched_expense_is_invalid(status: OperatingExpenseMatchStatus) -> None:
    invoice = _invoice()
    result = validate_vendor_bill_inputs(
        invoice,
        _partner(),
        _identifier_free_products(invoice),
        _taxes(invoice),
        company_id=1,
        operating_expense_match=_expense_match(status),
    )
    assert not result.is_valid


def test_identifier_free_with_no_expense_match_argument_is_invalid() -> None:
    invoice = _invoice()
    result = validate_vendor_bill_inputs(
        invoice, _partner(), _identifier_free_products(invoice), _taxes(invoice), company_id=1
    )
    assert not result.is_valid


def test_expense_match_with_non_positive_account_is_invalid() -> None:
    invoice = _invoice()
    result = validate_vendor_bill_inputs(
        invoice,
        _partner(),
        _identifier_free_products(invoice),
        _taxes(invoice),
        company_id=1,
        operating_expense_match=_expense_match(expense_account_id=0),
    )
    assert not result.is_valid


def test_supplier_not_matched_with_expense_match_is_invalid() -> None:
    invoice = _invoice()
    result = validate_vendor_bill_inputs(
        invoice,
        _partner(PartnerMatchStatus.NOT_FOUND),
        _identifier_free_products(invoice),
        _taxes(invoice),
        company_id=1,
        operating_expense_match=_expense_match(),
    )
    assert not result.is_valid


@pytest.mark.parametrize("tax_status", [TaxMatchStatus.NOT_FOUND, TaxMatchStatus.MULTIPLE_MATCHES])
def test_tax_mismatch_with_expense_match_is_invalid(tax_status: TaxMatchStatus) -> None:
    invoice = _invoice()
    result = validate_vendor_bill_inputs(
        invoice,
        _partner(),
        _identifier_free_products(invoice),
        _taxes(invoice, tax_status),
        company_id=1,
        operating_expense_match=_expense_match(),
    )
    assert not result.is_valid


def test_incomplete_product_result_coverage_fails_closed_even_with_expense_match() -> None:
    invoice = _invoice([_line("1"), _line("2")])
    # Product result covers only line "1".
    partial = InvoiceProductMatchResult(
        line_results=(_product_line("1", ProductMatchStatus.INVALID_INPUT, product_id=None),)
    )
    result = validate_vendor_bill_inputs(
        invoice,
        _partner(),
        partial,
        _taxes(invoice),
        company_id=1,
        operating_expense_match=_expense_match(),
    )
    assert not result.is_valid


def test_matched_product_result_for_identifier_free_invoice_fails_closed() -> None:
    invoice = _invoice()
    result = validate_vendor_bill_inputs(
        invoice,
        _partner(),
        _matched_products(invoice),
        _taxes(invoice),
        company_id=1,
        operating_expense_match=_expense_match(),
    )
    assert not result.is_valid


def test_expense_build_raises_when_validation_fails() -> None:
    invoice = _invoice()
    with pytest.raises(VendorBillBuildError):
        VendorBillBuilder().build(
            invoice,
            _partner(),
            _identifier_free_products(invoice),
            _taxes(invoice),
            company_id=1,
            operating_expense_match=_expense_match(OperatingExpenseMatchStatus.NOT_FOUND),
        )
