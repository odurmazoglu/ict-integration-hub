"""P0-PROD-15AD: exact source monetary amount preservation in the Vendor Bill builder.

Regression coverage for the real production reconciliation failure found in
P0-PROD-15AC: the source invoice's own authoritative ``line_extension_amount``
(UBL: cbc:LineExtensionAmount) differs from ``quantity * unit_price`` whenever the
transmitted ``unit_price`` is a rounded display figure -- independent of whether
the line carries any discount. CloudSpark's real production CPU/HDD/RAM lines
have zero discounts, yet ``quantity * unit_price`` overshoots or undershoots the
authoritative net by a few kuruş on every line, so the generated Vendor Bill
(and its preview, which shares the exact same ``line_gross_total``/
``line_net_total`` functions) posted TRY 5,951.81 instead of the source's true
TRY 5,951.76.

The fix (``app/billing/builder.py``) makes ``_line_net_total`` prefer the
source's transmitted ``line_extension_amount`` over the quantity/unit_price
reconstruction for an UNDISCOUNTED line only; a discounted line keeps the
pre-existing P0-PROD-08L reconstruction (gross computed from quantity *
unit_price, minus the discount amount) untouched, because at least one existing
real production fixture in this test suite (P0-PROD-10D/10C) carries a
``line_extension_amount`` that is pre-discount, not net-of-discount -- so the
scope is intentionally narrow rather than a blanket "always trust
line_extension_amount" rule.

``_net_unit_price`` (the actual Odoo ``price_unit``) is reconciled to this same
authoritative net, but returns ``line.unit_price`` completely unchanged --
including its original decimal precision -- whenever it already reproduces that
net exactly, so the pre-existing exact-arithmetic case is byte-for-byte
unaffected.

Because ``line_gross_total``/``line_net_total`` (the public reuse points) are the
exact same functions ``VendorBillBuilder.build`` and Vendor Bill preview both
consume, this one fix is shared by both -- there is no separate preview
arithmetic to fix independently (preview/execution parity is structural, not
merely tested).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.application.expense_mapping import OperatingExpenseMatchResult, OperatingExpenseMatchStatus
from app.billing import VendorBillBuilder, to_odoo_account_move_payload
from app.billing.builder import line_gross_total, line_net_total, line_total_discount, validate_vendor_bill_inputs
from app.domain.invoice import Discount, Header, InternalInvoice, InvoiceLine, MonetaryTotals, Party, Tax
from app.matching import (
    InvoiceProductLineResult,
    InvoiceProductMatchResult,
    PartnerMatchResult,
    PartnerMatchStatus,
    ProductMatchResult,
    ProductMatchStatus,
)
from app.tax_mapping import InvoiceTaxLineResult, InvoiceTaxMappingResult, TaxMatchResult, TaxMatchStatus, TaxType

COMPANY_ID = 1
CANONICAL_PARTNER_ID = 439
EXPENSE_ACCOUNT_ID = 247
TAX_ID = 34


# --------------------------------------------------------------------------- builders


def _cloudspark_lines() -> tuple[InvoiceLine, ...]:
    """The exact real production CloudSpark line shape (P0-PROD-15AC) -- a
    fixture only, never referenced from application code."""

    return (
        InvoiceLine(
            line_number="1",
            description="CPU",
            quantity=Decimal("20"),
            unit_code="C62",
            unit_price=Decimal("74.40"),
            line_extension_amount=Decimal("1487.94"),
            taxes=(Tax(tax_type="KDV", rate=Decimal("20")),),
        ),
        InvoiceLine(
            line_number="2",
            description="HDD",
            quantity=Decimal("320"),
            unit_code="C62",
            unit_price=Decimal("4.65"),
            line_extension_amount=Decimal("1487.94"),
            taxes=(Tax(tax_type="KDV", rate=Decimal("20")),),
        ),
        InvoiceLine(
            line_number="3",
            description="RAM",
            quantity=Decimal("24"),
            unit_code="C62",
            unit_price=Decimal("82.66"),
            line_extension_amount=Decimal("1983.92"),
            taxes=(Tax(tax_type="KDV", rate=Decimal("20")),),
        ),
    )


def _invoice(lines: tuple[InvoiceLine, ...], *, net_total: Decimal) -> InternalInvoice:
    tax_total = (net_total * Decimal("0.20")).quantize(Decimal("0.01"))
    return InternalInvoice(
        header=Header(
            invoice_number="I082026000000009",
            invoice_uuid="P0-PROD-15AD-ETTN",
            ettn="P0-PROD-15AD-ETTN",
            issue_date=date(2026, 9, 8),
            currency_code="TRY",
        ),
        supplier=Party(name="CLOUDSPARK BULUT TEKNOLOJILERI SAN. TIC. A.S.", tax_number="1760390647"),
        customer=Party(name="ICT Teknoloji", tax_number="4651205941"),
        totals=MonetaryTotals(
            line_extension_amount=net_total,
            tax_exclusive_amount=net_total,
            tax_inclusive_amount=net_total + tax_total,
            payable_amount=net_total + tax_total,
        ),
        lines=lines,
    )


def _partner() -> PartnerMatchResult:
    return PartnerMatchResult(
        status=PartnerMatchStatus.MATCHED,
        partner_id=CANONICAL_PARTNER_ID,
        matched_by="supplier_remediation_effect",
        reason="matched",
        candidate_count=1,
        confidence=Decimal("1.00"),
    )


def _identifier_free_products(invoice: InternalInvoice) -> InvoiceProductMatchResult:
    return InvoiceProductMatchResult(
        line_results=tuple(
            InvoiceProductLineResult(
                line_number=line.line_number,
                result=ProductMatchResult(
                    status=ProductMatchStatus.INVALID_INPUT,
                    line_number=line.line_number,
                    product_id=None,
                    default_code=None,
                    barcode=None,
                    seller_item_code=None,
                    matched_by=None,
                    reason="No deterministic product identifier present on this line.",
                    candidate_count=0,
                    confidence=None,
                ),
            )
            for line in invoice.lines
        )
    )


def _matched_products(invoice: InternalInvoice, *, product_ids: dict[str, int]) -> InvoiceProductMatchResult:
    return InvoiceProductMatchResult(
        line_results=tuple(
            InvoiceProductLineResult(
                line_number=line.line_number,
                result=ProductMatchResult(
                    status=ProductMatchStatus.MATCHED,
                    line_number=line.line_number,
                    product_id=product_ids[line.line_number],
                    default_code="SKU",
                    barcode=None,
                    seller_item_code=None,
                    matched_by="default_code",
                    reason="matched",
                    candidate_count=1,
                    confidence=Decimal("1.00"),
                ),
            )
            for line in invoice.lines
        )
    )


def _taxes(invoice: InternalInvoice) -> InvoiceTaxMappingResult:
    return InvoiceTaxMappingResult(
        line_results=tuple(
            InvoiceTaxLineResult(
                line_number=line.line_number,
                tax_index=tax_index,
                result=TaxMatchResult(
                    status=TaxMatchStatus.MATCHED,
                    tax_id=TAX_ID,
                    company_id=COMPANY_ID,
                    tax_type=TaxType.VAT,
                    tax_rate=Decimal("20"),
                    matched_by="company_type_rate",
                    confidence=Decimal("1.00"),
                    reason="matched",
                    candidate_count=1,
                ),
            )
            for line in invoice.lines
            for tax_index, _tax in enumerate(line.taxes)
        )
    )


def _expense_match() -> OperatingExpenseMatchResult:
    return OperatingExpenseMatchResult(
        status=OperatingExpenseMatchStatus.MATCHED,
        reason="Resolved via an accepted review-scoped accounting resolution for this review.",
        candidate_count=1,
        mapping_id=1,
        company_id=COMPANY_ID,
        vendor_partner_id=CANONICAL_PARTNER_ID,
        expense_account_id=EXPENSE_ACCOUNT_ID,
        expense_category="IT_HARDWARE_INTERNAL",
        matched_by="review_accounting_resolution",
        confidence=Decimal("1.00"),
    )


# =================================================================== local reproduction (pre-fix defect proof)


def test_naive_quantity_times_unit_price_does_not_reproduce_source_net() -> None:
    """Documents the exact pre-fix defect shape directly, independent of the
    builder: proves the source's own rounded display unit_price, multiplied by
    quantity, does NOT reproduce the authoritative net for any of the three real
    production lines -- which is exactly why trusting that arithmetic (the
    pre-P0-PROD-15AD behavior) was wrong. Reproduces the exact production
    discrepancy: naive gross totals TRY 4,959.84 against the source's true
    TRY 4,959.80 net (a +0.04 TRY drift that becomes +0.05 TRY gross once VAT is
    computed on the inflated base -- the P0-PROD-15AC finding).
    """

    naive_by_line = {line.description: line.unit_price * line.quantity for line in _cloudspark_lines()}
    assert naive_by_line == {
        "CPU": Decimal("1488.00"),
        "HDD": Decimal("1488.00"),
        "RAM": Decimal("1983.84"),
    }
    authoritative_by_line = {line.description: line.line_extension_amount for line in _cloudspark_lines()}
    assert authoritative_by_line == {
        "CPU": Decimal("1487.94"),
        "HDD": Decimal("1487.94"),
        "RAM": Decimal("1983.92"),
    }
    for description in naive_by_line:
        assert naive_by_line[description] != authoritative_by_line[description]

    naive_gross_total = sum(naive_by_line.values(), Decimal("0"))
    authoritative_net_total = sum(authoritative_by_line.values(), Decimal("0"))
    assert naive_gross_total == Decimal("4959.84")
    assert authoritative_net_total == Decimal("4959.80")
    assert naive_gross_total != authoritative_net_total


# =================================================================== A: CloudSpark-shaped reconciliation (fixed)


def test_cloudspark_shaped_lines_reconcile_exactly_net_vat_gross() -> None:
    lines = _cloudspark_lines()
    total_net = sum((line_net_total(line) for line in lines), Decimal("0"))
    total_vat = sum(
        (line_net_total(line) * (tax.rate / Decimal(100)) for line in lines for tax in line.taxes),
        Decimal("0"),
    ).quantize(Decimal("0.01"))
    total_gross = total_net + total_vat

    assert total_net == Decimal("4959.80")
    assert total_vat == Decimal("991.96")
    assert total_gross == Decimal("5951.76")


def test_cloudspark_shaped_account_only_vendor_bill_reconciles() -> None:
    lines = _cloudspark_lines()
    invoice = _invoice(lines, net_total=Decimal("4959.80"))
    bill = VendorBillBuilder().build(
        invoice,
        _partner(),
        _identifier_free_products(invoice),
        _taxes(invoice),
        company_id=COMPANY_ID,
        operating_expense_match=_expense_match(),
    )

    assert bill.supplier_id == CANONICAL_PARTNER_ID
    assert len(bill.invoice_lines) == 3
    for bill_line in bill.invoice_lines:
        assert bill_line.account_id == EXPENSE_ACCOUNT_ID
        assert bill_line.product_id is None

    total_net = sum((bill_line.unit_price * bill_line.quantity for bill_line in bill.invoice_lines), Decimal("0"))
    # Odoo itself rounds price_subtotal to currency precision (2dp) when posting;
    # the builder's own high-precision price_unit reconciles to the source net
    # once rounded the same way Odoo would (see the Odoo-payload test below for
    # the same reconciliation performed directly against the actual payload).
    assert total_net.quantize(Decimal("0.01")) == Decimal("4959.80")


# =================================================================== B: Odoo payload assertion


def test_cloudspark_shaped_odoo_payload_preserves_source_net_and_uses_account_only() -> None:
    lines = _cloudspark_lines()
    invoice = _invoice(lines, net_total=Decimal("4959.80"))
    bill = VendorBillBuilder().build(
        invoice,
        _partner(),
        _identifier_free_products(invoice),
        _taxes(invoice),
        company_id=COMPANY_ID,
        operating_expense_match=_expense_match(),
    )
    payload = to_odoo_account_move_payload(bill, currency_id=31, product_uom_ids={})

    assert payload["partner_id"] == CANONICAL_PARTNER_ID
    assert payload["move_type"] == "in_invoice"
    line_payloads = [entry[2] for entry in payload["invoice_line_ids"]]
    assert len(line_payloads) == 3

    expected = (
        {"quantity": Decimal("20"), "price_unit": Decimal("74.397")},
        {"quantity": Decimal("320"), "price_unit": Decimal("4.649813")},
        {"quantity": Decimal("24"), "price_unit": Decimal("82.663333")},
    )
    total_net = Decimal("0")
    for line_payload, expected_line in zip(line_payloads, expected, strict=True):
        assert line_payload["account_id"] == EXPENSE_ACCOUNT_ID
        assert "product_id" not in line_payload  # never fabricated for an account-only line
        assert line_payload["tax_ids"] == ((6, 0, (TAX_ID,)),)
        quantity = Decimal(line_payload["quantity"])
        price_unit = Decimal(line_payload["price_unit"])
        assert quantity == expected_line["quantity"]
        assert price_unit == expected_line["price_unit"]
        total_net += quantity * price_unit
    # Odoo itself rounds price_subtotal to currency precision (2dp) when posting;
    # the payload's own high-precision price_unit reconciles to the source net
    # once rounded the same way Odoo would.
    assert total_net.quantize(Decimal("0.01")) == Decimal("4959.80")


# =================================================================== C: discount path untouched (regression safety)


def test_discounted_line_reconstruction_is_unaffected_by_this_fix() -> None:
    """A discounted line keeps the pre-existing P0-PROD-08L reconstruction exactly
    -- this fix's line_extension_amount trust is scoped to undiscounted lines
    only (see module docstring for why)."""

    line = InvoiceLine(
        line_number="1",
        description="Discounted item",
        quantity=Decimal("2"),
        unit_price=Decimal("100.00"),
        line_extension_amount=Decimal("190.00"),  # NOT net-of-discount in this fixture's own convention
        discounts=(Discount(amount=Decimal("10.00")),),
        taxes=(Tax(tax_type="KDV", rate=Decimal("20")),),
    )
    # gross = unit_price * quantity = 200.00; net = gross - discount = 190.00
    assert line_gross_total(line) == Decimal("200.00")
    assert line_total_discount(line) == Decimal("10.00")
    assert line_net_total(line) == Decimal("190.00")


# =================================================================== D/E: quantity=1, fractional quantity


def test_quantity_one_line_reconciles_exactly() -> None:
    line = InvoiceLine(
        line_number="1",
        description="Single unit",
        quantity=Decimal("1"),
        unit_price=Decimal("100.33"),
        line_extension_amount=Decimal("100.33"),
        taxes=(Tax(tax_type="KDV", rate=Decimal("20")),),
    )
    assert line_net_total(line) == Decimal("100.33")


def test_fractional_quantity_line_reconciles_exactly() -> None:
    line = InvoiceLine(
        line_number="1",
        description="Fractional quantity",
        quantity=Decimal("2.5"),
        unit_price=Decimal("10.00"),
        line_extension_amount=Decimal("24.99"),  # deliberately not 2.5 * 10.00 = 25.00
        taxes=(Tax(tax_type="KDV", rate=Decimal("20")),),
    )
    assert line_net_total(line) == Decimal("24.99")


# =================================================================== F: exact-arithmetic case byte-identical


def test_exact_arithmetic_case_is_byte_for_byte_unchanged() -> None:
    """quantity * unit_price already equal to line_extension_amount -- the common,
    pre-existing case -- must produce the exact same price_unit representation
    (including decimal precision/formatting) as before this fix."""

    lines = (
        InvoiceLine(
            line_number="1",
            description="Line 1",
            quantity=Decimal("2"),
            unit_price=Decimal("10.50"),
            line_extension_amount=Decimal("21.00"),
            taxes=(Tax(tax_type="KDV", rate=Decimal("20")),),
        ),
    )
    invoice = _invoice(lines, net_total=Decimal("21.00"))
    bill = VendorBillBuilder().build(
        invoice,
        _partner(),
        _matched_products(invoice, product_ids={"1": 501}),
        _taxes(invoice),
        company_id=COMPANY_ID,
    )
    payload = to_odoo_account_move_payload(bill, currency_id=31, product_uom_ids={501: 1})
    line_payload = payload["invoice_line_ids"][0][2]
    assert line_payload["price_unit"] == "10.50"


# =================================================================== G: zero/invalid quantity fails closed


def test_zero_quantity_fails_closed_per_existing_domain_rule() -> None:
    line = InvoiceLine(
        line_number="1",
        description="Zero quantity",
        quantity=Decimal("0"),
        unit_price=Decimal("10.00"),
        line_extension_amount=Decimal("0.00"),
        taxes=(Tax(tax_type="KDV", rate=Decimal("20")),),
    )
    invoice = _invoice((line,), net_total=Decimal("0.00"))
    validation = validate_vendor_bill_inputs(
        invoice,
        _partner(),
        _identifier_free_products(invoice),
        _taxes(invoice),
        company_id=COMPANY_ID,
        operating_expense_match=_expense_match(),
    )
    assert not validation.is_valid
    assert any("quantity must be greater than zero" in error for error in validation.errors)


# =================================================================== H: source evidence preserves original unit price


def test_review_evidence_preserves_original_source_unit_price() -> None:
    """The fix changes only the Odoo posting price_unit derivation -- the
    immutable source evidence's own unit_price field must remain exactly what
    the source invoice transmitted, wherever source evidence is presented."""

    from app.application.workbench.review_evidence import SourceLineEvidence

    line = _cloudspark_lines()[0]
    source_line = SourceLineEvidence.from_line(line, None)
    assert source_line.unit_price == Decimal("74.40")
    assert source_line.net_amount == Decimal("1487.94")


# =================================================================== I: product-backed lines get the same fix


def test_product_backed_line_gets_the_same_net_amount_correction() -> None:
    """The same source-authoritative-amount principle applies to product-backed
    lines: VendorBillBuilder._vendor_bill_line calls the exact same
    _net_unit_price as the account-only path, so a product-backed line with the
    same CloudSpark-shaped rounding mismatch is corrected identically -- proving
    the fix lives at the narrowest shared abstraction, not duplicated per path.
    """

    line = InvoiceLine(
        line_number="1",
        description="Product-backed CPU",
        quantity=Decimal("20"),
        unit_price=Decimal("74.40"),
        line_extension_amount=Decimal("1487.94"),
        taxes=(Tax(tax_type="KDV", rate=Decimal("20")),),
    )
    invoice = _invoice((line,), net_total=Decimal("1487.94"))
    bill = VendorBillBuilder().build(
        invoice,
        _partner(),
        _matched_products(invoice, product_ids={"1": 501}),
        _taxes(invoice),
        company_id=COMPANY_ID,
    )
    payload = to_odoo_account_move_payload(bill, currency_id=31, product_uom_ids={501: 1})
    line_payload = payload["invoice_line_ids"][0][2]
    assert line_payload["product_id"] == 501
    assert Decimal(line_payload["price_unit"]) == Decimal("74.397")
    assert Decimal(line_payload["price_unit"]) * Decimal(line_payload["quantity"]) == Decimal("1487.940")
