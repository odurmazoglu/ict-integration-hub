"""P0-PROD-19E-2: currency-aware Vendor Bill build validation and zero-allowance semantics.

- An AllowanceCharge with ``Amount = 0`` (and no or a zero rate) is economically neutral:
  it never routes a line down the discounted path. A real discount still does; a charge
  never becomes a discount.
- The exact source ``price_unit`` is kept whenever ``quantity x price_unit`` reproduces the
  line amount at the currency's precision (59.7378 / 119.4669, never 59.74 / 119.47).
- Execution resolves the currency's precision read-only and fails closed before any Odoo
  write when the bill's money does not reconcile; preview reports the same findings.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from app.application.execution.contracts import ExecutionMode, ExecutionStepStatus
from app.application.execution.vendor_bill_strategy import (
    VendorBillExecutionStrategy,
    vendor_bill_write_idempotency_key,
)
from app.application.workbench.vendor_bill_readback import MonetaryReadbackStatus, compare_vendor_bill_money
from app.billing import (
    VendorBillBuilder,
    economic_discounts,
    is_economically_neutral,
    to_odoo_account_move_payload,
    vendor_bill_monetary_errors,
    vendor_bill_money,
)
from app.billing.builder import validate_vendor_bill_inputs
from app.billing.exceptions import VendorBillBuildError
from app.billing.money import MonetaryPrecision, currency_round, monetary_equal
from app.domain.invoice import Discount, InvoiceLine, parse_ubl_invoice
from app.erp.write.exceptions import VendorBillWriteTransportError
from tests.unit.resale_execution_support import NON_RESALE_ACCOUNTING_CHECK, StaticCurrencyPrecisionReader
from tests.unit.test_p0_prod_19e_1_currency_rounding_reconciliation import (
    PRODUCT_ID,
    _basic_source,
    _header,
    _line,
    _preview,
    _source,
    _standard_source,
)
from tests.unit.test_vendor_bill_execution_strategy import (
    RecordingSourceInvoiceReader,
    RecordingVendorBillWriter,
    _step_request,
)
from tests.unit.test_vendor_bill_preview import StaticCurrencyReader, _product_decision, _request, _use_case

USD = MonetaryPrecision(2)
ZERO_ALLOWANCE = Discount(amount=Decimal("0"), rate=Decimal("0"))
UBL_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "ubl" / "valid_invoice.xml"


def _build(source, precision: MonetaryPrecision | None = USD):
    return VendorBillBuilder().build(
        source.invoice,
        source.partner_match,
        source.product_match,
        source.tax_match,
        company_id=1,
        monetary_precision=precision,
    )


def _payload_line(bill) -> dict:
    return to_odoo_account_move_payload(bill, currency_id=1, product_uom_ids={PRODUCT_ID: 1})["invoice_line_ids"][0][2]


# --------------------------------------------------------------------------- AllowanceCharge semantics


@pytest.mark.parametrize(
    "discount",
    [
        Discount(amount=Decimal("0"), rate=Decimal("0")),
        Discount(amount=Decimal("0.00")),
        Discount(amount=Decimal("0"), rate=Decimal("0.00"), reason="İskonto"),
    ],
)
def test_zero_amount_allowance_is_economically_neutral(discount: Discount) -> None:
    line = InvoiceLine(discounts=(discount,))

    assert is_economically_neutral(discount)
    assert economic_discounts(line) == ()


@pytest.mark.parametrize(
    "discount",
    [
        Discount(amount=Decimal("5.00")),
        Discount(amount=Decimal("0.01"), rate=Decimal("0")),
        Discount(amount=None, rate=Decimal("10")),  # rate-only: still fails closed as before
        Discount(amount=Decimal("0"), rate=Decimal("10")),  # contradicted zero: never assumed neutral
    ],
)
def test_non_neutral_allowance_remains_a_discount(discount: Discount) -> None:
    line = InvoiceLine(discounts=(ZERO_ALLOWANCE, discount))

    assert not is_economically_neutral(discount)
    assert economic_discounts(line) == (discount,)


def test_a_charge_is_never_parsed_as_a_discount() -> None:
    xml = UBL_FIXTURE.read_text(encoding="utf-8").replace(
        """    <cac:AllowanceCharge>
      <cbc:ChargeIndicator>false</cbc:ChargeIndicator>
      <cbc:AllowanceChargeReason>Discount</cbc:AllowanceChargeReason>
      <cbc:Amount currencyID="TRY">5.50</cbc:Amount>
    </cac:AllowanceCharge>""",
        """    <cac:AllowanceCharge>
      <cbc:ChargeIndicator>false</cbc:ChargeIndicator>
      <cbc:MultiplierFactorNumeric>0</cbc:MultiplierFactorNumeric>
      <cbc:Amount currencyID="TRY">0</cbc:Amount>
      <cbc:BaseAmount currencyID="TRY">200.00</cbc:BaseAmount>
    </cac:AllowanceCharge>
    <cac:AllowanceCharge>
      <cbc:ChargeIndicator>true</cbc:ChargeIndicator>
      <cbc:Amount currencyID="TRY">7.00</cbc:Amount>
    </cac:AllowanceCharge>""",
    )

    line = parse_ubl_invoice(xml).lines[0]

    assert line.discounts == (Discount(amount=Decimal("0"), rate=Decimal("0")),)
    assert economic_discounts(line) == ()


# --------------------------------------------------------------------------- LOGOSOFT / VİTEL payloads


def test_logosoft_basic_keeps_exact_source_price_unit() -> None:
    source = _basic_source()
    bill = _build(source)
    line = bill.invoice_lines[0]

    assert line.unit_price == Decimal("59.7378")
    assert line.quantity * line.unit_price == Decimal("119.4756")
    assert currency_round(line.quantity * line.unit_price, USD) == Decimal("119.48")
    assert vendor_bill_monetary_errors(source.invoice, bill.invoice_lines, USD) == ()
    money = vendor_bill_money(source.invoice, bill.invoice_lines, USD)
    assert (money.untaxed, money.tax, money.total) == (Decimal("119.48"), Decimal("23.90"), Decimal("143.38"))
    # account.move 67 was written with exactly these line values.
    assert (_payload_line(bill)["quantity"], _payload_line(bill)["price_unit"]) == ("2", "59.7378")


def test_logosoft_standard_keeps_exact_source_price_unit() -> None:
    source = _standard_source()
    bill = _build(source)
    line = bill.invoice_lines[0]

    assert line.unit_price == Decimal("119.4669")
    assert line.quantity * line.unit_price == Decimal("358.4007")
    assert vendor_bill_monetary_errors(source.invoice, bill.invoice_lines, USD) == ()
    money = vendor_bill_money(source.invoice, bill.invoice_lines, USD)
    assert (money.untaxed, money.tax, money.total) == (Decimal("358.40"), Decimal("71.68"), Decimal("430.08"))
    # account.move 68 was written with exactly these line values.
    assert (_payload_line(bill)["quantity"], _payload_line(bill)["price_unit"]) == ("3", "119.4669")


@pytest.mark.parametrize("source_factory", [_basic_source, _standard_source])
def test_logosoft_preview_reconciles_with_the_same_money_as_the_build(source_factory) -> None:
    source = source_factory()
    bill = _build(source)
    preview = _preview(source)
    money = vendor_bill_money(source.invoice, bill.invoice_lines, USD)

    assert preview.lines[0].unit_price == bill.invoice_lines[0].unit_price
    assert (preview.preview_untaxed, preview.preview_tax, preview.preview_total) == (
        money.untaxed,
        money.tax,
        money.total,
    )
    assert preview.computed_untaxed == money.computed_untaxed
    assert preview.monetary_reconciles is True
    assert preview.total_discount == Decimal("0")


def test_vitel_payload_is_unchanged() -> None:
    source = _source(
        quantity="1",
        unit_price="708.75",
        line_extension="708.75",
        tax="141.75",
        untaxed="708.75",
        total="850.50",
        discounts=(),
    )

    with_precision, legacy = _build(source), _build(source, precision=None)

    assert _payload_line(with_precision) == _payload_line(legacy)
    assert _payload_line(with_precision)["price_unit"] == "708.75"
    money = vendor_bill_money(source.invoice, with_precision.invoice_lines, USD)
    assert (money.untaxed, money.tax, money.total) == (Decimal("708.75"), Decimal("141.75"), Decimal("850.50"))
    assert vendor_bill_monetary_errors(source.invoice, with_precision.invoice_lines, USD) == ()


def test_standard_readback_still_verifies_the_created_bill() -> None:
    verification = compare_vendor_bill_money(
        _preview(_standard_source()),
        _header("358.40", "71.68", "430.08"),
        (_line("3", "119.4669", "358.40"),),
        USD,
    )

    assert verification.status is MonetaryReadbackStatus.VERIFIED


# --------------------------------------------------------------------------- real discounts / other lines


def test_real_discount_keeps_the_discounted_line_behaviour() -> None:
    source = _source(
        quantity="10",
        unit_price="10.00",
        line_extension="100.00",  # pre-discount, as some historical sources transmit
        tax="19.00",
        untaxed="95.00",
        total="114.00",
        discounts=(ZERO_ALLOWANCE, Discount(amount=Decimal("5.00"))),
    )

    bill = _build(source)

    assert bill.invoice_lines[0].unit_price == Decimal("9.5")
    assert _build(source, precision=None).invoice_lines[0].unit_price == Decimal("9.5")
    assert vendor_bill_monetary_errors(source.invoice, bill.invoice_lines, USD) == ()
    assert _preview(source).total_discount == Decimal("5.00")


def test_zero_allowance_no_longer_hides_the_source_line_amount() -> None:
    # 3 x 33.33 = 99.99 is not the source's 100.00: the zero allowance used to force the
    # discounted path (price 33.33, bill 99.99); the line amount is now authoritative.
    source = _source(
        quantity="3",
        unit_price="33.33",
        line_extension="100.00",
        tax="20.00",
        untaxed="100.00",
        total="120.00",
    )

    bill = _build(source)

    assert bill.invoice_lines[0].unit_price == Decimal("33.333333")
    assert vendor_bill_monetary_errors(source.invoice, bill.invoice_lines, USD) == ()


def test_no_currency_context_keeps_exact_equality_only() -> None:
    # Builds without a currency (no production caller) never guess a precision.
    assert _build(_basic_source(), precision=None).invoice_lines[0].unit_price == Decimal("59.74")


def test_decision_time_validation_still_accepts_logosoft_without_currency() -> None:
    for source in (_basic_source(), _standard_source()):
        assert validate_vendor_bill_inputs(
            source.invoice, source.partner_match, source.product_match, source.tax_match, company_id=1
        ).is_valid


# --------------------------------------------------------------------------- currency precision


@pytest.mark.parametrize(
    ("amount", "decimal_places", "expected"),
    [
        ("119.4756", 2, "119.48"),
        ("-119.4756", 2, "-119.48"),
        ("-0.005", 2, "-0.01"),
        ("100.5", 0, "101"),
        ("-100.5", 0, "-101"),
        ("1.2345", 3, "1.235"),
        ("-1.2345", 3, "-1.235"),
    ],
)
def test_currency_round_uses_the_currencys_decimal_places(amount: str, decimal_places: int, expected: str) -> None:
    assert currency_round(Decimal(amount), MonetaryPrecision(decimal_places)) == Decimal(expected)


@pytest.mark.parametrize(
    ("decimal_places", "source_untaxed", "reconciles"),
    [
        (2, "119.48", True),
        (2, "119.49", False),  # one cent
        (2, "119.47", False),
        (0, "119", True),
        (0, "120", False),  # one whole unit
        (3, "119.480", True),
        (3, "119.481", False),  # one thousandth
    ],
)
def test_one_minor_unit_mismatch_fails_at_every_precision(
    decimal_places: int, source_untaxed: str, reconciles: bool
) -> None:
    precision = MonetaryPrecision(decimal_places)
    source = _basic_source()
    source = replace(
        source,
        invoice=replace(
            source.invoice, totals=replace(source.invoice.totals, tax_exclusive_amount=Decimal(source_untaxed))
        ),
    )
    bill = _build(source, precision=precision)

    untaxed_errors = [
        error
        for error in vendor_bill_monetary_errors(source.invoice, bill.invoice_lines, precision)
        if "untaxed" in error
    ]

    assert (untaxed_errors == []) is reconciles


def test_three_decimal_currency_derives_price_when_the_source_price_does_not_reproduce_the_amount() -> None:
    # 119.4756 is 119.476 at 3 decimals, not the source's 119.480: the exact price does not
    # reproduce the line amount there, so the existing LineExtensionAmount derivation applies.
    precision = MonetaryPrecision(3)
    bill = _build(_basic_source(), precision=precision)

    assert not monetary_equal(Decimal("119.4756"), Decimal("119.48"), precision)
    assert bill.invoice_lines[0].unit_price == Decimal("59.74")


def test_zero_decimal_currency_keeps_the_exact_source_price() -> None:
    assert _build(_basic_source(), precision=MonetaryPrecision(0)).invoice_lines[0].unit_price == Decimal("59.7378")


# --------------------------------------------------------------------------- fail closed before the write


def _unreconcilable_source():
    # 1,000,000 x 1.00000001 cannot be represented at price_unit's 6-decimal precision:
    # the derived price reproduces 1,000,000.00, not the source's 1,000,000.01.
    return _source(
        quantity="1000000",
        unit_price="1.00",
        line_extension="1000000.01",
        tax="200000.00",
        untaxed="1000000.01",
        total="1200000.01",
        discounts=(),
    )


def test_line_amount_that_cannot_be_reproduced_is_a_monetary_error() -> None:
    source = _unreconcilable_source()
    bill = _build(source)

    errors = vendor_bill_monetary_errors(source.invoice, bill.invoice_lines, USD)

    assert errors[0] == (
        "lines[0]: quantity x price_unit (1000000.00) does not match the source line amount (1000000.01) "
        "at 2 decimal places."
    )


def test_preview_reports_the_same_line_mismatch_without_raising() -> None:
    preview = _preview(_unreconcilable_source())

    assert preview.monetary_reconciles is False
    assert preview.monetary_mismatches[0].startswith("lines[0]: quantity x price_unit (1000000.00)")


def _execution_source(source):
    return replace(source, review_id="review-1", company_id=7, decision_version=2)


def _execute(source, *, currency_reader=None):
    writer = RecordingVendorBillWriter()
    currency_reader = currency_reader or StaticCurrencyPrecisionReader(2)
    strategy = VendorBillExecutionStrategy(
        source_invoice_reader=RecordingSourceInvoiceReader(source=_execution_source(source)),
        vendor_bill_builder=VendorBillBuilder(),
        vendor_bill_writer=writer,
        resale_accounting_check=NON_RESALE_ACCOUNTING_CHECK,
        currency_reader=currency_reader,
    )
    result = strategy.execute(_step_request(mode=ExecutionMode.EXECUTE, approved_by="finance"))
    return result, writer, currency_reader


def test_monetary_mismatch_fails_execution_before_any_odoo_write() -> None:
    result, writer, currency_reader = _execute(_unreconcilable_source())

    assert result.status is ExecutionStepStatus.FAILED
    assert result.error_code == "vendor_bill_build_error"
    assert "lines[0]: quantity x price_unit" in result.message
    assert writer.calls == 0
    assert currency_reader.calls == ["USD"]


def test_source_total_mismatch_fails_execution_before_any_odoo_write() -> None:
    source = _source(
        quantity="2", unit_price="59.7378", line_extension="119.48", tax="23.90", untaxed="119.48", total="143.40"
    )

    result, writer, _ = _execute(source)

    assert result.status is ExecutionStepStatus.FAILED
    assert "total: 143.38 != 143.40 at 2 decimal places" in result.message
    assert writer.calls == 0


def test_unreadable_currency_fails_execution_before_any_odoo_write() -> None:
    reader = StaticCurrencyPrecisionReader(error=VendorBillWriteTransportError("Odoo request timed out."))

    result, writer, _ = _execute(_basic_source(), currency_reader=reader)

    assert result.status is ExecutionStepStatus.FAILED
    assert result.error_code == "vendor_bill_transport_failure"
    assert writer.calls == 0


@pytest.mark.parametrize(("source_factory", "price_unit"), [(_basic_source, "59.7378"), (_standard_source, "119.4669")])
def test_logosoft_executes_with_the_exact_source_price_unit(source_factory, price_unit: str) -> None:
    result, writer, _ = _execute(source_factory())

    assert result.status is ExecutionStepStatus.EXECUTED
    assert writer.calls == 1
    assert writer.commands[0].vendor_bill.invoice_lines[0].unit_price == Decimal(price_unit)


# --------------------------------------------------------------------------- idempotency


def test_odoo_idempotency_key_is_unchanged() -> None:
    request = _step_request(mode=ExecutionMode.EXECUTE, approved_by="finance")
    identity = {
        "company_id": request.company_id,
        "review_id": request.review_id,
        "decision_version": request.decision_version,
        "step_key": request.step.step_key,
        "step_type": request.step.step_type.value,
    }
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    assert vendor_bill_write_idempotency_key(request) == f"vendor-bill-write:{digest}"
    result, writer, _ = _execute(_basic_source())
    assert writer.commands[0].idempotency_key == f"vendor-bill-write:{digest}"
    assert result.produced_artifacts[0].external_identity == f"vendor-bill-write:{digest}"


def test_preview_resolves_the_currency_before_building() -> None:
    use_case, _, currency = _use_case(
        decision=_product_decision(), source=_basic_source(), currency_reader=StaticCurrencyReader(decimal_places=2)
    )

    preview = use_case.preview(_request(review_id="review-product-1", decision_version=1))

    assert currency.calls == ["USD"]
    assert preview.lines[0].unit_price == Decimal("59.7378")


def test_build_error_type_is_unchanged_for_invalid_inputs() -> None:
    source = _basic_source()
    broken = replace(source, invoice=replace(source.invoice, lines=()))

    with pytest.raises(VendorBillBuildError):
        _build(broken)
