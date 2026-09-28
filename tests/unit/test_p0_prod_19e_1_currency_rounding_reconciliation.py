"""P0-PROD-19E-1: monetary reconciliation at the invoice currency's precision.

- ``currency_round(a) == currency_round(b)`` replaces "within an absolute tolerance".
- price_unit keeps the source precision (59.7378 / 119.4669); only monetary amounts are
  presented and compared at the currency's precision.
- A zero-amount AllowanceCharge causes no monetary mismatch; a real discount keeps its
  existing economics.
- Readback verifies every created bill's money and reports MISMATCH without writing;
  a retry can never create a second bill (Odoo idempotency key lookup).
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.application.commands import VendorBillWriteCommand
from app.application.execution.contracts import (
    ExecutionArtifact,
    ExecutionArtifactType,
    ExecutionStepStatus,
    ExecutionStepType,
)
from app.application.execution.runtime import ExecutionState
from app.application.execution.vendor_bill_preview import VendorBillPreview
from app.application.workbench.exceptions import ReviewNotFoundError
from app.application.workbench.vendor_bill_readback import (
    GetVendorBillReadbackUseCase,
    MonetaryReadbackStatus,
    VendorBillHeaderVerification,
    VendorBillLineVerification,
    VendorBillMonetaryReadbackVerifier,
    compare_vendor_bill_money,
)
from app.billing import VendorBillBuilder, to_odoo_account_move_payload
from app.billing.money import MonetaryPrecision, MonetaryPrecisionError, currency_round, monetary_equal
from app.domain.invoice import Discount, InvoiceLine, MonetaryTotals, Tax
from app.erp.write import AccountMoveDraft, OdooVendorBillWriter
from tests.unit.test_odoo_vendor_bill_writer import _enabled_policy
from tests.unit.test_vendor_bill_preview import (
    StaticCurrencyReader,
    _product_decision,
    _product_source,
    _request,
    _use_case,
)

USD = MonetaryPrecision(2)
MOVE_ID = 9001
PRODUCT_ID = 601  # the product the shared preview fixture's product match resolves to


# --------------------------------------------------------------------------- money rule


def test_basic_amount_reconciles_at_usd_precision() -> None:
    assert Decimal("2") * Decimal("59.7378") == Decimal("119.4756")
    assert currency_round(Decimal("119.4756"), USD) == Decimal("119.48")
    assert monetary_equal(Decimal("119.4756"), Decimal("119.48"), USD)


def test_standard_amount_reconciles_at_usd_precision() -> None:
    assert Decimal("3") * Decimal("119.4669") == Decimal("358.4007")
    assert monetary_equal(Decimal("358.4007"), Decimal("358.40"), USD)


@pytest.mark.parametrize("computed", ["119.46", "119.47", "119.4749", "119.49", "119.4850"])
def test_real_differences_do_not_reconcile(computed: str) -> None:
    assert not monetary_equal(Decimal(computed), Decimal("119.48"), USD)


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        ("0.005", "0.01"),
        ("0.0049999", "0.00"),
        ("1.125", "1.13"),
        ("1.135", "1.14"),
        ("-1.125", "-1.13"),
        ("119.475", "119.48"),
        ("119.4749999", "119.47"),
    ],
)
def test_half_cent_boundary_rounds_half_away_from_zero(amount: str, expected: str) -> None:
    assert currency_round(Decimal(amount), USD) == Decimal(expected)


def test_precision_comes_from_the_currency_not_an_assumption() -> None:
    assert currency_round(Decimal("100.5"), MonetaryPrecision(0)) == Decimal("101")
    assert currency_round(Decimal("1.2345"), MonetaryPrecision(3)) == Decimal("1.235")
    assert not monetary_equal(Decimal("1.2344"), Decimal("1.235"), MonetaryPrecision(3))


@pytest.mark.parametrize("bad", [-1, 7, True, 2.0, None])
def test_invalid_precision_is_rejected(bad: object) -> None:
    with pytest.raises(MonetaryPrecisionError):
        MonetaryPrecision(bad)  # type: ignore[arg-type]


def test_binary_floats_are_rejected() -> None:
    with pytest.raises(MonetaryPrecisionError):
        currency_round(119.4756, USD)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- fixtures


def _source(
    *,
    quantity: str,
    unit_price: str,
    line_extension: str,
    tax: str,
    untaxed: str,
    total: str,
    discounts: tuple[Discount, ...] = (Discount(amount=Decimal("0"), rate=Decimal("0")),),
):
    base = _product_source()
    line = InvoiceLine(
        line_number="1",
        description="Microsoft 365 Business Basic",
        seller_item_code="CFQ7TTC0LH18:0001",
        quantity=Decimal(quantity),
        unit_code="C62",
        unit_price=Decimal(unit_price),
        line_extension_amount=Decimal(line_extension),
        discounts=discounts,
        taxes=(Tax(tax_type="KDV", rate=Decimal("20"), base_amount=Decimal(untaxed), tax_amount=Decimal(tax)),),
    )
    invoice = replace(
        base.invoice,
        header=replace(base.invoice.header, currency_code="USD"),
        totals=MonetaryTotals(
            line_extension_amount=Decimal(line_extension),
            tax_exclusive_amount=Decimal(untaxed),
            tax_inclusive_amount=Decimal(total),
            allowance_total=Decimal("0.00"),
            payable_amount=Decimal(total),
        ),
        lines=(line,),
    )
    return replace(base, invoice=invoice)


def _basic_source():
    return _source(
        quantity="2", unit_price="59.7378", line_extension="119.48", tax="23.90", untaxed="119.48", total="143.38"
    )


def _standard_source():
    return _source(
        quantity="3", unit_price="119.4669", line_extension="358.40", tax="71.68", untaxed="358.40", total="430.08"
    )


def _preview(source) -> VendorBillPreview:
    use_case, _, _ = _use_case(decision=_product_decision(), source=source)
    return use_case.preview(_request(review_id="review-product-1", decision_version=1))


# --------------------------------------------------------------------------- preview


def test_basic_preview_presents_currency_money_and_keeps_price_precision() -> None:
    preview = _preview(_basic_source())
    line = preview.lines[0]

    assert line.quantity == Decimal("2")
    assert line.unit_price == Decimal("59.7378")
    assert line.computed_subtotal == Decimal("119.4756")
    assert line.currency_subtotal == Decimal("119.48")
    assert line.source_line_extension_amount == Decimal("119.48")
    assert preview.currency_decimal_places == 2
    assert preview.computed_untaxed == Decimal("119.4756")
    assert (preview.preview_untaxed, preview.preview_tax, preview.preview_total) == (
        Decimal("119.48"),
        Decimal("23.90"),
        Decimal("143.38"),
    )
    assert (preview.source_untaxed, preview.source_tax, preview.source_total) == (
        Decimal("119.48"),
        Decimal("23.90"),
        Decimal("143.38"),
    )
    assert preview.monetary_reconciles is True
    assert preview.monetary_mismatches == ()


def test_standard_preview_reconciles_at_usd_precision() -> None:
    preview = _preview(_standard_source())

    assert preview.lines[0].unit_price == Decimal("119.4669")
    assert preview.lines[0].computed_subtotal == Decimal("358.4007")
    assert (preview.preview_untaxed, preview.preview_tax, preview.preview_total) == (
        Decimal("358.40"),
        Decimal("71.68"),
        Decimal("430.08"),
    )
    assert preview.monetary_reconciles is True


def test_zero_allowance_causes_no_mismatch_and_leaves_payload_price_unchanged() -> None:
    source = _basic_source()
    assert source.invoice.lines[0].discounts == (Discount(amount=Decimal("0"), rate=Decimal("0")),)

    # Preview and execution always build with the currency's precision (P0-PROD-19E-2).
    bill = VendorBillBuilder().build(
        source.invoice,
        source.partner_match,
        source.product_match,
        source.tax_match,
        company_id=1,
        monetary_precision=USD,
    )
    payload = to_odoo_account_move_payload(bill, currency_id=1, product_uom_ids={PRODUCT_ID: 1})

    line_values = payload["invoice_line_ids"][0][2]
    assert line_values["price_unit"] == "59.7378"
    assert line_values["quantity"] == "2"
    assert _preview(source).monetary_reconciles is True


def test_genuine_discount_keeps_existing_economics_and_reconciles() -> None:
    source = _source(
        quantity="10",
        unit_price="10.00",
        line_extension="100.00",  # a pre-discount figure, as some historical sources transmit
        tax="19.00",
        untaxed="95.00",
        total="114.00",
        discounts=(Discount(amount=Decimal("5.00")),),
    )

    preview = _preview(source)

    assert preview.lines[0].unit_price == Decimal("9.5")
    assert preview.preview_untaxed == Decimal("95.00")
    assert preview.total_discount == Decimal("5.00")
    assert preview.monetary_reconciles is True


def test_preview_reports_a_source_total_that_does_not_reconcile() -> None:
    source = _source(
        quantity="2", unit_price="59.7378", line_extension="119.48", tax="23.90", untaxed="119.48", total="143.40"
    )

    preview = _preview(source)

    assert preview.monetary_reconciles is False
    assert preview.monetary_mismatches == ("total: 143.38 != 143.40 at 2 decimal places",)


def test_preview_uses_the_currencys_own_precision() -> None:
    use_case, _, _ = _use_case(
        decision=_product_decision(),
        source=_basic_source(),
        currency_reader=StaticCurrencyReader(decimal_places=0),
    )

    preview = use_case.preview(_request(review_id="review-product-1", decision_version=1))

    assert preview.currency_decimal_places == 0
    assert preview.preview_untaxed == Decimal("119")
    assert preview.lines[0].unit_price == Decimal("59.7378")


def test_vitel_acceptance_semantics_are_unchanged() -> None:
    source = _source(
        quantity="1",
        unit_price="708.75",
        line_extension="708.75",
        tax="141.75",
        untaxed="708.75",
        total="850.50",
        discounts=(),
    )

    preview = _preview(source)

    assert preview.lines[0].unit_price == Decimal("708.75")
    assert (preview.preview_untaxed, preview.preview_tax, preview.preview_total) == (
        Decimal("708.75"),
        Decimal("141.75"),
        Decimal("850.50"),
    )
    assert preview.monetary_reconciles is True
    verification = compare_vendor_bill_money(
        preview, _header("708.75", "141.75", "850.50"), (_line("1", "708.75", "708.75"),), USD
    )
    assert verification.status is MonetaryReadbackStatus.VERIFIED


# --------------------------------------------------------------------------- readback


def _header(untaxed: str, tax: str, total: str, *, currency: str = "USD", decimal_places: int | None = 2):
    return VendorBillHeaderVerification(
        move_id=MOVE_ID,
        company_id=1,
        state="draft",
        move_type="in_invoice",
        partner_id=450,
        currency=currency,
        amount_untaxed=Decimal(untaxed),
        amount_tax=Decimal(tax),
        amount_total=Decimal(total),
        currency_decimal_places=decimal_places,
    )


def _line(quantity: str, price_unit: str, subtotal: str, *, product_id: int | None = PRODUCT_ID):
    return VendorBillLineVerification(
        line_id=1,
        move_id=MOVE_ID,
        account_id=29,
        product_id=product_id,
        # Odoo returns floats; the reader turns str(float) into Decimal.
        quantity=Decimal(str(float(quantity))),
        price_unit=Decimal(str(float(price_unit))),
        tax_ids=(34,),
        price_subtotal=Decimal(str(float(subtotal))),
        price_total=Decimal("0"),
    )


def test_readback_verifies_the_correct_basic_bill() -> None:
    verification = compare_vendor_bill_money(
        _preview(_basic_source()), _header("119.48", "23.90", "143.38"), (_line("2", "59.7378", "119.48"),), USD
    )

    assert verification.status is MonetaryReadbackStatus.VERIFIED
    assert verification.mismatches == ()
    assert verification.lines[0].expected_subtotal == Decimal("119.48")
    assert (verification.expected_untaxed, verification.expected_tax, verification.expected_total) == (
        Decimal("119.48"),
        Decimal("23.90"),
        Decimal("143.38"),
    )


@pytest.mark.parametrize(
    ("header", "lines", "expected_fragment"),
    [
        (("119.47", "23.90", "143.37"), ("2", "59.7378", "119.47"), "subtotal"),
        (("119.48", "23.89", "143.37"), ("2", "59.7378", "119.48"), "tax vs source"),
        (("119.48", "23.90", "143.39"), ("2", "59.7378", "119.48"), "total vs source"),
        (("119.48", "23.90", "143.38"), ("2", "59.74", "119.48"), "no expected line"),
        (("119.48", "23.90", "143.38"), ("3", "59.7378", "119.48"), "no expected line"),
    ],
    ids=["subtotal", "tax", "total", "rounded-price-unit", "quantity"],
)
def test_readback_reports_monetary_mismatches(header, lines, expected_fragment) -> None:
    verification = compare_vendor_bill_money(_preview(_basic_source()), _header(*header), (_line(*lines),), USD)

    assert verification.status is MonetaryReadbackStatus.MISMATCH
    assert any(expected_fragment in mismatch for mismatch in verification.mismatches)


def test_readback_reports_a_currency_mismatch() -> None:
    verification = compare_vendor_bill_money(
        _preview(_basic_source()),
        _header("119.48", "23.90", "143.38", currency="TRY"),
        (_line("2", "59.7378", "119.48"),),
        USD,
    )

    assert verification.status is MonetaryReadbackStatus.MISMATCH
    assert "currency TRY != expected USD" in verification.mismatches


class _Expectation:
    def __init__(self, preview=None, error=None) -> None:
        self.preview, self.error, self.calls = preview, error, []

    def expected_vendor_bill(self, *, review_id, company_id, decision_version):
        self.calls.append((review_id, company_id, decision_version))
        if self.error is not None:
            raise self.error
        return self.preview


def test_unreadable_currency_precision_fails_closed_as_mismatch() -> None:
    verifier = VendorBillMonetaryReadbackVerifier(expectation_reader=_Expectation(_preview(_basic_source())))

    verification = verifier.verify(
        review_id="r",
        company_id=1,
        decision_version=3,
        header=_header("119.48", "23.90", "143.38", decimal_places=None),
        lines=(_line("2", "59.7378", "119.48"),),
    )

    assert verification.status is MonetaryReadbackStatus.MISMATCH
    assert verification.mismatches == ("bill currency precision unavailable",)


def test_unavailable_expectation_fails_closed_as_mismatch() -> None:
    verifier = VendorBillMonetaryReadbackVerifier(
        expectation_reader=_Expectation(error=ReviewNotFoundError("Accepted decision was not found."))
    )

    verification = verifier.verify(
        review_id="r",
        company_id=1,
        decision_version=3,
        header=_header("119.48", "23.90", "143.38"),
        lines=(_line("2", "59.7378", "119.48"),),
    )

    assert verification.status is MonetaryReadbackStatus.MISMATCH
    assert verification.mismatches[0].startswith("expected Vendor Bill unavailable")


class _Reader:
    def __init__(self, value) -> None:
        self.value = value

    def get_review_item(self, query):
        return object()

    def find_latest_snapshot_for_review(self, *, review_id, company_id):
        return self.value

    def read_vendor_bill(self, *, move_id, company_id):
        return self.value

    def read_invoice_lines_for_move(self, *, move_id):
        return self.value


def _completed_snapshot():
    artifact = ExecutionArtifact(
        artifact_type=ExecutionArtifactType.VENDOR_BILL, artifact_id=str(MOVE_ID), external_identity="k", created=True
    )
    result = SimpleNamespace(status=ExecutionStepStatus.EXECUTED, dry_run=False, produced_artifacts=(artifact,))
    step = SimpleNamespace(step_type=ExecutionStepType.VENDOR_BILL, last_result=result)
    return SimpleNamespace(execution_id="exec-1", decision_version=3, state=ExecutionState.COMPLETED, steps=(step,))


def test_readback_use_case_reports_mismatch_without_touching_the_execution() -> None:
    expectation = _Expectation(_preview(_basic_source()))
    snapshot = _completed_snapshot()
    use_case = GetVendorBillReadbackUseCase(
        review_reader=_Reader(None),
        execution_snapshot_reader=_Reader(snapshot),
        header_reader=_Reader(_header("119.47", "23.90", "143.37")),
        line_reader=_Reader((_line("2", "59.7378", "119.47"),)),
        monetary_verifier=VendorBillMonetaryReadbackVerifier(expectation_reader=expectation),
    )

    readback = use_case.execute(review_id="r", company_id=1)

    assert readback.monetary_verification is not None
    assert readback.monetary_verification.status is MonetaryReadbackStatus.MISMATCH
    assert readback.artifact_id == str(MOVE_ID)
    assert expectation.calls == [("r", 1, 3)]
    # Readback is read-only: the persisted execution stays COMPLETED with its one artifact.
    assert snapshot.state is ExecutionState.COMPLETED


# --------------------------------------------------------------------------- retry safety


class _StatefulDraftRepository:
    """An Odoo that remembers bills by idempotency key, like the real x_studio field lookup."""

    def __init__(self) -> None:
        self.bills: dict[str, AccountMoveDraft] = {}
        self.create_calls = 0

    async def find_existing_vendor_bill(self, *, vendor_bill, idempotency_key, company_id):
        return self.bills.get(idempotency_key)

    async def create_draft_vendor_bill(self, *, vendor_bill, idempotency_key, company_id):
        self.create_calls += 1
        self.bills[idempotency_key] = AccountMoveDraft(id=MOVE_ID)
        return self.bills[idempotency_key]


async def test_a_retry_after_a_monetary_mismatch_never_creates_a_second_bill() -> None:
    source = _basic_source()
    bill = VendorBillBuilder().build(
        source.invoice, source.partner_match, source.product_match, source.tax_match, company_id=1
    )
    repository = _StatefulDraftRepository()
    writer = OdooVendorBillWriter(repository=repository, policy=_enabled_policy())
    command = VendorBillWriteCommand(
        vendor_bill=bill, idempotency_key="vendor-bill-write:abc", company_id=1, dry_run=False, approved_by="op"
    )

    first = await writer.write_vendor_bill(command)
    mismatch = compare_vendor_bill_money(
        _preview(source), _header("119.47", "23.90", "143.37"), (_line("2", "59.7378", "119.47"),), USD
    )
    second = await writer.write_vendor_bill(command)

    assert first.status == "created"
    assert mismatch.status is MonetaryReadbackStatus.MISMATCH
    assert second.status == "existing"
    assert second.vendor_bill_id == first.vendor_bill_id == MOVE_ID
    assert repository.create_calls == 1


# --------------------------------------------------------------------------- API (additive fields)


def test_api_exposes_the_currency_money_additively() -> None:
    from app.api.routers.workbench import _monetary_readback_response, _vendor_bill_preview_response

    preview = _preview(_basic_source())
    response = _vendor_bill_preview_response(preview).model_dump()
    readback = _monetary_readback_response(
        compare_vendor_bill_money(
            preview, _header("119.48", "23.90", "143.38"), (_line("2", "59.7378", "119.48"),), USD
        )
    ).model_dump()

    assert (response["preview_untaxed"], response["preview_tax"], response["preview_total"]) == (
        "119.48",
        "23.90",
        "143.38",
    )
    assert response["computed_untaxed"] == "119.4756"
    assert response["currency_decimal_places"] == 2
    assert response["monetary_reconciles"] is True
    assert response["lines"][0]["unit_price"] == "59.7378"
    assert response["lines"][0]["currency_subtotal"] == "119.48"
    assert response["lines"][0]["source_line_extension_amount"] == "119.48"
    assert readback["status"] == "verified"
    assert readback["mismatches"] == []
