"""P1: the invoice's own exchange rate is authoritative for a foreign-currency Vendor Bill.

Reference: J062026000000021 / Odoo move 76 -- UBL PricingExchangeRate USD->TRY 49.161600,
Odoo invoice_currency_rate 0.0203410792162989 (= 1/49.1616); lines 20.76 + 13.84 USD,
VAT 6.92 USD; TRY 1020.59 + 680.40, VAT 340.20, payable 2041.19.
"""

from __future__ import annotations

import hashlib
import inspect
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from app.api.routers.workbench import _fx_rate_plan_response, _fx_readback_response
from app.application.dto.vendor_bill import VendorBillWriteResult
from app.application.execution import vendor_bill_fx_guard
from app.application.execution.contracts import ExecutionMode, ExecutionStepStatus
from app.application.execution.planner import ExecutionPlanner
from app.application.execution.vendor_bill_fx_guard import (
    SourceFxFactsUnavailableError,
    StoredSourceFxFactsReader,
    VendorBillFxRatePolicy,
)
from app.application.execution.vendor_bill_preview import PreviewVendorBillUseCase
from app.application.execution.vendor_bill_strategy import (
    VendorBillExecutionStrategy,
    vendor_bill_write_idempotency_key,
)
from app.application.source_tax.exceptions import SourceDocumentUnreadableError, SourceTaxFactsUnavailableError
from app.application.source_tax.model import SourceDocumentIssue
from app.application.workbench.exceptions import ReviewPersistenceError
from app.application.workbench.vendor_bill_readback import VendorBillFxReadbackVerifier
from app.billing import VendorBillBuilder, to_odoo_account_move_payload
from app.billing.fx_rate import (
    FxRateStatus,
    FxVerificationStatus,
    OdooCurrencyContext,
    VendorBillCompanyAmounts,
    VendorBillCompanyLine,
    odoo_invoice_currency_rate,
    select_vendor_bill_rate,
    verify_vendor_bill_company_amounts,
)
from app.billing.money import MonetaryPrecision
from app.billing.reconciliation import line_currency_subtotal
from app.connectors.odoo.client import OdooJson2Client
from app.domain.invoice.fx_facts import SourceFxFacts, extract_source_fx_facts
from app.erp.exceptions import ErpRepositoryError, ErpRepositoryTimeoutError
from app.erp.odoo import vendor_bill_company_amounts_reader as amounts_module
from app.erp.odoo import vendor_bill_fx_rate_reader as fx_reader_module
from app.erp.odoo.vendor_bill_company_amounts_reader import OdooVendorBillCompanyAmountsReader
from app.erp.odoo.vendor_bill_fx_rate_reader import OdooVendorBillFxRateReader
from app.erp.write.account_move_repository import AccountMoveRepository
from app.erp.write.exceptions import (
    VendorBillWriteTransportError,
    VendorBillWriteUnexpectedErpError,
    VendorBillWriteValidationError,
)
from app.persistence.execution_source_invoice_reader import serialize_execution_source_invoice_payload
from tests.unit.fx_rate_support import (
    TRY_ONLY_FX_RATE_POLICY,
    FixedFxRateReader,
    FixedSourceFxFactsReader,
    pricing_rate,
)
from tests.unit.resale_execution_support import (
    NON_INTERNAL_USE_ACCOUNTING_CHECK,
    NON_RESALE_ACCOUNTING_CHECK,
    StaticCurrencyPrecisionReader,
)
from tests.unit.source_tax_support import ALLOW_SOURCE_TAX_SAFETY_CHECK
from tests.unit.test_p0_prod_19e_1_currency_rounding_reconciliation import _basic_source
from tests.unit.test_vendor_bill_execution_strategy import (
    RecordingSourceInvoiceReader,
    RecordingVendorBillWriter,
    _step_request,
)
from tests.unit.test_vendor_bill_preview import (
    StaticAcceptedDecisionReader,
    StaticCurrencyReader,
    StaticProductUomReader,
    StaticSourceInvoiceReader,
    _product_decision,
    _request,
)

D = Decimal
TRY2 = MonetaryPrecision(2)
MOVE_76_RATE = D("49.161600")
MOVE_76_ODOO_FLOAT = 0.0203410792162989
#: Move 67's invoice (I182026000000047): 2 x 59.7378 = 119.48 USD + 23.90 VAT = 143.38 USD.
MOVE_67_RATE = D("48.674900")


def _odoo(automatic: str | None = "49.1616", *, currency: str = "TRY", decimal_places: int = 2):
    return OdooCurrencyContext(
        company_currency=currency,
        company_currency_decimal_places=decimal_places,
        automatic_invoice_currency_rate=D(repr(float(1 / D(automatic)))) if automatic is not None else None,
    )


def _facts(*rates, currency: str = "USD") -> SourceFxFacts:
    return SourceFxFacts(invoice_uuid="uuid-1", document_currency=currency, exchange_rates=rates)


def _plan(
    *rates,
    currency: str = "USD",
    evidence: str | None = "49.161600",
    odoo=None,
    facts=...,
    lines=("20.76", "13.84"),
    tax: str | None = "6.92",
):
    return select_vendor_bill_rate(
        document_currency=currency,
        rate_date=date(2026, 10, 6),
        evidence_rate=D(evidence) if evidence is not None else None,
        facts=_facts(*(rates or (pricing_rate("49.161600"),)), currency=currency) if facts is ... else facts,
        odoo=odoo if odoo is not None else _odoo(),
        line_subtotals=tuple(D(line) for line in lines),
        tax_total=D(tax) if tax is not None else None,
    )


# ------------------------------------------------------------------ rate selection


def test_usd_with_matching_odoo_rate_selects_the_invoice_rate_and_reproduces_move_76() -> None:
    plan = _plan()

    assert plan.status is FxRateStatus.SOURCE_RATE_SELECTED
    assert plan.applies_rate and not plan.blocks_vendor_bill
    assert plan.selected_rate == plan.source_rate == D("49.161600")
    assert plan.odoo_automatic_rate == D("49.161600")
    assert plan.rate_difference == D("0.000000")
    assert plan.odoo_invoice_currency_rate == MOVE_76_ODOO_FLOAT
    assert (plan.expected_company_untaxed, plan.expected_company_tax, plan.expected_company_total) == (
        D("1700.99"),
        D("340.20"),
        D("2041.19"),
    )


def test_usd_with_a_different_odoo_rate_still_uses_the_invoice_rate() -> None:
    # Odoo's record named 2026-10-06 holds 49.1802 -- never substituted.
    plan = _plan(odoo=_odoo("49.1802"))

    assert plan.status is FxRateStatus.SOURCE_RATE_SELECTED
    assert plan.selected_rate == D("49.161600")
    assert plan.odoo_automatic_rate == D("49.180200")
    assert plan.rate_difference == D("-0.018600")
    assert plan.expected_company_total == D("2041.19")
    assert "it is not used" in plan.message


def test_eur_with_a_different_odoo_rate_uses_the_invoice_rate() -> None:
    plan = _plan(
        pricing_rate("55.10", source="EUR"),
        currency="EUR",
        evidence="55.10",
        odoo=_odoo("55.0757"),
        lines=("100.00",),
        tax="20.00",
    )

    assert plan.status is FxRateStatus.SOURCE_RATE_SELECTED
    assert plan.selected_rate == D("55.10")
    assert plan.odoo_automatic_rate == D("55.075700")
    assert (plan.expected_company_untaxed, plan.expected_company_tax, plan.expected_company_total) == (
        D("5510.00"),
        D("1102.00"),
        D("6612.00"),
    )


def test_the_selected_rate_keeps_its_exact_precision() -> None:
    plan = _plan(pricing_rate("49.16160049"), evidence="49.16160049")

    assert plan.selected_rate == D("49.16160049")
    assert plan.odoo_invoice_currency_rate == float(D(1) / D("49.16160049"))


def test_company_currency_bill_is_not_applicable() -> None:
    plan = _plan(currency="TRY", facts=None)

    assert plan.status is FxRateStatus.NOT_APPLICABLE
    assert plan.odoo_invoice_currency_rate is None


def test_missing_pricing_rate_is_never_inferred_from_another_rate_or_odoo() -> None:
    plan = _plan(pricing_rate("49.161600", element="PaymentExchangeRate"))

    assert plan.status is FxRateStatus.SOURCE_RATE_MISSING
    assert plan.blocks_vendor_bill
    assert plan.selected_rate is None
    assert "does not use Odoo's automatic rate" in plan.message


@pytest.mark.parametrize(
    ("source", "target"),
    [("TRY", "USD"), (None, "TRY"), ("USD", None), ("EUR", "TRY"), ("USD", "EUR")],
    ids=["reversed", "no-source", "no-target", "other-source", "other-target"],
)
def test_invalid_or_reversed_direction_blocks(source, target) -> None:
    plan = _plan(pricing_rate("0.020341" if source == "TRY" else "49.161600", source=source, target=target))

    assert plan.status is FxRateStatus.SOURCE_RATE_DIRECTION_INVALID
    assert plan.blocks_vendor_bill
    assert "never inverts" in plan.message


@pytest.mark.parametrize("rate", [None, "abc", "0", "-49.1616", "NaN"])
def test_invalid_rate_value_blocks(rate) -> None:
    assert _plan(pricing_rate(rate)).status is FxRateStatus.SOURCE_RATE_INVALID


@pytest.mark.parametrize(
    ("rates", "evidence", "currency_in_document"),
    [
        ((pricing_rate("49.161600"), pricing_rate("49.161600")), "49.161600", "USD"),
        ((pricing_rate("49.161600"), pricing_rate("49.20", element="PaymentExchangeRate")), "49.161600", "USD"),
        ((pricing_rate("49.161600"), pricing_rate("x", element="TaxExchangeRate")), "49.161600", "USD"),
        ((pricing_rate("49.161600"),), "49.20", "USD"),
        ((pricing_rate("49.161600"),), None, "USD"),
        ((pricing_rate("49.161600"),), "49.161600", "EUR"),
    ],
    ids=["two-pricing", "payment-differs", "tax-unparseable", "evidence-differs", "evidence-missing", "currency"],
)
def test_contradictory_evidence_blocks(rates, evidence, currency_in_document) -> None:
    plan = select_vendor_bill_rate(
        document_currency="USD",
        rate_date=date(2026, 10, 6),
        evidence_rate=D(evidence) if evidence is not None else None,
        facts=_facts(*rates, currency=currency_in_document),
        odoo=_odoo(),
    )

    assert plan.status is FxRateStatus.SOURCE_RATE_CONTRADICTORY
    assert plan.blocks_vendor_bill


def test_agreeing_or_unrelated_other_rates_do_not_block() -> None:
    plan = _plan(
        pricing_rate("49.161600"),
        pricing_rate("49.1616", element="PaymentExchangeRate"),
        pricing_rate("0.9", source="USD", target="EUR", element="TaxExchangeRate"),
    )

    assert plan.status is FxRateStatus.SOURCE_RATE_SELECTED


def test_unverified_document_and_unknown_company_currency_block() -> None:
    assert _plan(facts=None).status is FxRateStatus.UNVERIFIED_SOURCE_DOCUMENT
    no_company = select_vendor_bill_rate(
        document_currency="USD", rate_date=None, evidence_rate=D("1"), facts=_facts(), odoo=None
    )
    assert no_company.status is FxRateStatus.EVIDENCE_UNAVAILABLE


def test_odoo_without_any_rate_does_not_matter_for_the_invoice_rate() -> None:
    plan = _plan(odoo=_odoo("1"))

    assert plan.status is FxRateStatus.SOURCE_RATE_SELECTED
    assert plan.odoo_automatic_rate is None and plan.rate_difference is None


def test_tax_rounding_is_half_up_per_amount_at_the_company_precision() -> None:
    # 0.25 x 49.1 = 12.275 -> 12.28 ; VAT 0.05 x 49.1 = 2.455 -> 2.46.
    plan = _plan(pricing_rate("49.1"), evidence="49.1", lines=("0.25",), tax="0.05")

    assert (plan.expected_company_untaxed, plan.expected_company_tax) == (D("12.28"), D("2.46"))


def test_multi_line_expected_amounts_round_each_line() -> None:
    plan = _plan(lines=("0.01", "0.01", "0.01"), tax="0.01")

    # Each 0.01 USD = 0.491616 -> 0.49 TRY; summing unrounded would give 1.47 too, but per line.
    assert plan.expected_company_untaxed == D("1.47")
    assert plan.expected_company_total == D("1.96")


def test_historical_move_67_totals_reproduce_production() -> None:
    plan = _plan(pricing_rate("48.674900"), evidence="48.674900", lines=("119.48",), tax="23.90", odoo=_odoo("48.6749"))

    assert plan.expected_company_total == D("6979.01")  # production amount_total_signed


# ------------------------------------------------------------------ Odoo payload and repository backstop


def _bill(*, currency: str = "USD", rate: Decimal | None = MOVE_76_RATE):
    source = _basic_source()
    bill = VendorBillBuilder().build(
        source.invoice, source.partner_match, source.product_match, source.tax_match, company_id=7
    )
    return replace(bill, currency=currency, company_currency_rate=rate)


def _uoms(bill):
    return {line.product_id: 1 for line in bill.invoice_lines if line.product_id is not None}


def test_payload_carries_the_invoice_rate_in_odoos_direction_only_for_the_new_bill() -> None:
    bill = _bill()
    payload = to_odoo_account_move_payload(bill, currency_id=1, product_uom_ids=_uoms(bill))

    assert payload["invoice_currency_rate"] == MOVE_76_ODOO_FLOAT
    assert payload["move_type"] == "in_invoice"
    text = str(payload)
    assert "res.currency.rate" not in text and "action_post" not in text


def test_try_payload_has_no_rate() -> None:
    bill = _bill(currency="TRY", rate=None)

    assert "invoice_currency_rate" not in to_odoo_account_move_payload(
        bill, currency_id=31, product_uom_ids=_uoms(bill)
    )


@pytest.mark.parametrize(("currency", "rate"), [("USD", None), ("EUR", D("0")), ("TRY", D("1"))])
def test_repository_refuses_a_bill_without_the_right_rate(currency: str, rate) -> None:
    bill = _bill(currency=currency, rate=rate)

    with pytest.raises(VendorBillWriteValidationError):
        AccountMoveRepository(client=object())._draft_payload(
            vendor_bill=bill,
            idempotency_key="vendor-bill-write:x",
            company_id=7,
            currency_id=1,
            product_uom_ids=_uoms(bill),
        )


def test_repository_sends_the_rate_with_the_draft() -> None:
    bill = _bill()
    payload = AccountMoveRepository(client=object())._draft_payload(
        vendor_bill=bill,
        idempotency_key="vendor-bill-write:x",
        company_id=7,
        currency_id=1,
        product_uom_ids=_uoms(bill),
    )

    assert payload["invoice_currency_rate"] == MOVE_76_ODOO_FLOAT
    assert payload["invoice_origin"] == "vendor-bill-write:x"


# ------------------------------------------------------------------ created-bill verification


def _line(line_id: int, kind: str, currency: str, balance: str) -> VendorBillCompanyLine:
    return VendorBillCompanyLine(line_id=line_id, display_type=kind, amount_currency=D(currency), balance=D(balance))


MOVE_76_LINES = (
    _line(168, "product", "20.76", "1020.59"),
    _line(169, "product", "13.84", "680.40"),
    _line(170, "tax", "6.92", "340.20"),
    _line(171, "payment_term", "-41.52", "-2041.19"),
)


def _amounts(lines=MOVE_76_LINES, *, rate: float = MOVE_76_ODOO_FLOAT, state: str = "draft", currency: str = "USD"):
    return VendorBillCompanyAmounts(
        move_id=76, state=state, currency=currency, invoice_currency_rate=D(repr(rate)), lines=tuple(lines)
    )


def test_move_76_verifies_rate_lines_tax_and_payable() -> None:
    verification = verify_vendor_bill_company_amounts(_plan(), _amounts())

    assert verification.status is FxVerificationStatus.VERIFIED
    assert (verification.company_untaxed, verification.company_tax, verification.company_payable) == (
        D("1700.99"),
        D("340.20"),
        D("2041.19"),
    )
    assert verification.mismatches == () and verification.notes == ()


def test_a_bill_at_odoos_automatic_rate_is_a_mismatch() -> None:
    rate = D("49.1802")
    lines = (
        _line(1, "product", "20.76", str((D("20.76") * rate).quantize(D("0.01")))),
        _line(2, "product", "13.84", str((D("13.84") * rate).quantize(D("0.01")))),
        _line(3, "tax", "6.92", str((D("6.92") * rate).quantize(D("0.01")))),
        _line(4, "payment_term", "-41.52", "-2041.96"),
    )
    verification = verify_vendor_bill_company_amounts(_plan(), _amounts(lines, rate=float(1 / rate)))

    assert verification.status is FxVerificationStatus.MISMATCH
    assert any("bill rate 49.180200 != invoice rate 49.161600" in item for item in verification.mismatches)


def test_tax_rounding_within_tolerance_is_a_note_not_a_mismatch() -> None:
    lines = MOVE_76_LINES[:2] + (_line(170, "tax", "6.92", "340.21"), _line(171, "payment_term", "-41.52", "-2041.20"))
    verification = verify_vendor_bill_company_amounts(_plan(), _amounts(lines))

    assert verification.status is FxVerificationStatus.VERIFIED
    assert any("rounding" in note for note in verification.notes)


@pytest.mark.parametrize(
    ("lines", "kwargs", "expected"),
    [
        (MOVE_76_LINES[:3] + (_line(171, "payment_term", "-41.52", "-2041.00"),), {}, "does not balance"),
        (MOVE_76_LINES[:1] + (_line(169, "product", "13.84", "690.40"),) + MOVE_76_LINES[2:], {}, "TRY 690.40"),
        (MOVE_76_LINES + (_line(172, "rounding", "0.00", "0.00"),), {}, "unverified line type rounding"),
        (MOVE_76_LINES, {"state": "posted"}, "state posted"),
        (MOVE_76_LINES, {"currency": "EUR"}, "bill currency EUR"),
    ],
    ids=["unbalanced", "line-off", "unknown-line", "posted", "currency"],
)
def test_bill_mismatches_fail_verification(lines, kwargs, expected) -> None:
    verification = verify_vendor_bill_company_amounts(_plan(), _amounts(lines, **kwargs))

    assert verification.status is FxVerificationStatus.MISMATCH
    assert any(expected in item for item in verification.mismatches)


def test_missing_bill_or_plan_is_unverified() -> None:
    assert verify_vendor_bill_company_amounts(_plan(), None).status is FxVerificationStatus.UNVERIFIED
    assert verify_vendor_bill_company_amounts(None, _amounts()).status is FxVerificationStatus.UNVERIFIED
    assert verify_vendor_bill_company_amounts(_plan(facts=None), _amounts()).status is FxVerificationStatus.UNVERIFIED


# ------------------------------------------------------------------ execution


class OdooLikeAmountsReader:
    """Builds the journal items Odoo computes for the last written bill at ``rate_used``."""

    def __init__(self, writer: RecordingVendorBillWriter, *, rate_used: Decimal | None = None, error=None) -> None:
        self.writer = writer
        self.rate_used = rate_used
        self.error = error
        self.calls: list[tuple[int, int]] = []

    def read_vendor_bill_company_amounts(self, *, move_id: int, company_id: int):
        self.calls.append((move_id, company_id))
        if self.error is not None:
            raise self.error
        bill = self.writer.commands[-1].vendor_bill
        rate = self.rate_used or bill.company_currency_rate
        subtotals = [line_currency_subtotal(line.quantity, line.unit_price, TRY2) for line in bill.invoice_lines]
        tax = D("23.90")
        balances = [(subtotal * rate).quantize(D("0.01")) for subtotal in subtotals] + [
            (tax * rate).quantize(D("0.01"))
        ]
        pairs = zip(subtotals, balances, strict=False)
        lines = [_line(index + 1, "product", str(s), str(b)) for index, (s, b) in enumerate(pairs)]
        lines.append(_line(90, "tax", str(tax), str(balances[-1])))
        lines.append(_line(91, "payment_term", str(-(sum(subtotals) + tax)), str(-sum(balances))))
        return VendorBillCompanyAmounts(
            move_id=move_id,
            state="draft",
            currency="USD",
            invoice_currency_rate=D(repr(float(1 / rate))),
            lines=tuple(lines),
        )


def _source(*, rate: Decimal | None = MOVE_67_RATE, currency: str = "USD"):
    source = _basic_source()
    header = replace(source.invoice.header, exchange_rate=rate, currency_code=currency)
    return replace(
        source, invoice=replace(source.invoice, header=header), review_id="review-1", company_id=7, decision_version=2
    )


def _strategy(*, facts=None, automatic="48.6992", writer=None, amounts=None, source=None, policy=None):
    writer = writer or RecordingVendorBillWriter()
    amounts = amounts or OdooLikeAmountsReader(writer)
    facts = facts or FixedSourceFxFactsReader(pricing_rate("48.674900"))
    policy = policy or VendorBillFxRatePolicy(
        facts_reader=facts, currency_reader=FixedFxRateReader(D(automatic)), amounts_reader=amounts
    )
    strategy = VendorBillExecutionStrategy(
        internal_use_accounting_check=NON_INTERNAL_USE_ACCOUNTING_CHECK,
        source_tax_safety_check=ALLOW_SOURCE_TAX_SAFETY_CHECK,
        source_invoice_reader=RecordingSourceInvoiceReader(source=source or _source()),
        vendor_bill_builder=VendorBillBuilder(),
        vendor_bill_writer=writer,
        resale_accounting_check=NON_RESALE_ACCOUNTING_CHECK,
        currency_reader=StaticCurrencyPrecisionReader(2),
        fx_rate_policy=policy,
    )
    return strategy, writer, amounts


def _execute(strategy, mode=ExecutionMode.EXECUTE):
    return strategy.execute(_step_request(mode=mode, approved_by="finance"))


def test_execute_applies_the_invoice_rate_despite_a_different_odoo_rate_and_verifies_it() -> None:
    strategy, writer, amounts = _strategy(automatic="48.6992")

    result = _execute(strategy)

    assert result.status is ExecutionStepStatus.EXECUTED
    assert writer.calls == 1
    assert writer.commands[0].vendor_bill.company_currency_rate == MOVE_67_RATE
    assert len(amounts.calls) == 1 and amounts.calls[0][1] == 7


def test_execute_with_matching_odoo_rate_executes_once() -> None:
    strategy, writer, amounts = _strategy(automatic="48.6749")

    assert _execute(strategy).status is ExecutionStepStatus.EXECUTED
    assert writer.calls == 1 and len(amounts.calls) == 1


def test_dry_run_plans_but_never_reads_the_bill() -> None:
    strategy, writer, amounts = _strategy()

    result = _execute(strategy, ExecutionMode.DRY_RUN)

    assert result.status is ExecutionStepStatus.DRY_RUN_OK
    assert writer.real_write_count == 0
    assert amounts.calls == []


@pytest.mark.parametrize(
    ("facts", "error_code"),
    [
        (FixedSourceFxFactsReader(), "vendor_bill_fx_source_rate_missing"),
        (
            FixedSourceFxFactsReader(pricing_rate("0.020544", source="TRY", target="USD")),
            "vendor_bill_fx_source_rate_direction_invalid",
        ),
        (FixedSourceFxFactsReader(pricing_rate("49.0")), "vendor_bill_fx_source_rate_contradictory"),
        (FixedSourceFxFactsReader(pricing_rate("-1")), "vendor_bill_fx_source_rate_invalid"),
        (FixedSourceFxFactsReader(unverified=True), "vendor_bill_fx_unverified_source_document"),
        (
            FixedSourceFxFactsReader(error=SourceFxFactsUnavailableError("storage down")),
            "source_fx_facts_unavailable",
        ),
    ],
)
@pytest.mark.parametrize("mode", [ExecutionMode.DRY_RUN, ExecutionMode.EXECUTE])
def test_invalid_source_evidence_fails_closed_before_any_write(facts, error_code: str, mode) -> None:
    strategy, writer, amounts = _strategy(facts=facts)

    result = _execute(strategy, mode)

    assert result.status is ExecutionStepStatus.FAILED
    assert result.error_code == error_code
    assert result.produced_artifacts == ()
    assert writer.calls == 0 and amounts.calls == []


def test_missing_evidence_rate_fails_closed() -> None:
    strategy, writer, _ = _strategy(source=_source(rate=None))

    assert _execute(strategy).error_code == "vendor_bill_fx_source_rate_contradictory"
    assert writer.calls == 0


def test_a_bill_odoo_did_not_compute_at_the_invoice_rate_fails_and_is_reused_on_retry() -> None:
    writer = RecordingVendorBillWriter()
    amounts = OdooLikeAmountsReader(writer, rate_used=D("48.6992"))
    strategy, _, _ = _strategy(writer=writer, amounts=amounts)
    request = _step_request(mode=ExecutionMode.EXECUTE, approved_by="finance")

    failed = strategy.execute(request)

    assert failed.status is ExecutionStepStatus.FAILED
    assert failed.error_code == "vendor_bill_fx_verification_failed"
    assert "do not post it" in failed.message
    assert writer.calls == 1

    # The operator corrects the draft's rate in Odoo; the retry finds the same draft through
    # the unchanged idempotency identity -- no second bill -- and verifies it again.
    existing = RecordingVendorBillWriter(
        result=VendorBillWriteResult(
            status="existing", idempotency_key=vendor_bill_write_idempotency_key(request), external_id=101
        )
    )
    existing.commands = writer.commands
    retry, _, _ = _strategy(writer=existing, amounts=OdooLikeAmountsReader(existing))
    recovered = retry.execute(request)

    assert recovered.status is ExecutionStepStatus.EXECUTED
    assert [artifact.created for artifact in recovered.produced_artifacts] == [False]
    assert existing.commands[-1].idempotency_key == vendor_bill_write_idempotency_key(request)


def test_existing_draft_at_the_invoice_rate_is_compatible() -> None:
    writer = RecordingVendorBillWriter(
        result=VendorBillWriteResult(status="existing", idempotency_key="k", external_id=67)
    )
    strategy, _, amounts = _strategy(writer=writer, automatic="48.6749")

    result = _execute(strategy)

    assert result.status is ExecutionStepStatus.EXECUTED
    assert amounts.calls[0][0] == 67


@pytest.mark.parametrize(
    "error", [VendorBillWriteTransportError("Odoo request timed out."), VendorBillWriteUnexpectedErpError("bad")]
)
def test_bill_read_failure_after_write_names_the_existing_draft(error) -> None:
    writer = RecordingVendorBillWriter()
    strategy, _, _ = _strategy(writer=writer, amounts=OdooLikeAmountsReader(writer, error=error))

    result = _execute(strategy)

    # Distinct from every pre-write failure: the bill exists, so the result must say so.
    assert result.status is ExecutionStepStatus.FAILED
    assert result.error_code == "vendor_bill_fx_verification_unavailable"
    assert "Draft Vendor Bill 9001 exists in Odoo" in result.message
    assert "do not create this bill again by hand" in result.message
    assert "No Vendor Bill was created" not in result.message
    assert writer.calls == 1


def test_bill_read_failure_then_retry_reuses_the_same_draft() -> None:
    writer = RecordingVendorBillWriter()
    amounts = OdooLikeAmountsReader(writer, error=VendorBillWriteTransportError("Odoo request timed out."))
    strategy, _, _ = _strategy(writer=writer, amounts=amounts)
    request = _step_request(mode=ExecutionMode.EXECUTE, approved_by="finance")

    assert strategy.execute(request).error_code == "vendor_bill_fx_verification_unavailable"

    existing = RecordingVendorBillWriter(
        result=VendorBillWriteResult(
            status="existing", idempotency_key=vendor_bill_write_idempotency_key(request), external_id=9001
        )
    )
    existing.commands = writer.commands
    retry, _, _ = _strategy(writer=existing, amounts=OdooLikeAmountsReader(existing))
    recovered = retry.execute(request)

    assert recovered.status is ExecutionStepStatus.EXECUTED
    assert [(a.artifact_id, a.created) for a in recovered.produced_artifacts] == [("9001", False)]
    assert {command.idempotency_key for command in existing.commands} == {vendor_bill_write_idempotency_key(request)}


def test_pre_write_odoo_rate_read_timeout_stays_a_transport_failure() -> None:
    writer = RecordingVendorBillWriter()
    amounts = OdooLikeAmountsReader(writer)
    policy = VendorBillFxRatePolicy(
        facts_reader=FixedSourceFxFactsReader(pricing_rate("48.674900")),
        currency_reader=FixedFxRateReader(D("48.6992"), error=VendorBillWriteTransportError("timeout")),
        amounts_reader=amounts,
    )
    strategy, _, _ = _strategy(writer=writer, amounts=amounts, policy=policy)

    result = _execute(strategy)

    assert result.error_code == "vendor_bill_transport_failure"
    assert writer.calls == 0 and amounts.calls == []


def test_try_bill_reads_nothing_and_sends_no_rate() -> None:
    strategy, writer, _ = _strategy(source=_source(rate=None, currency="TRY"), policy=TRY_ONLY_FX_RATE_POLICY)

    assert _execute(strategy).status is ExecutionStepStatus.EXECUTED
    assert writer.commands[0].vendor_bill.company_currency_rate is None


def test_strategy_requires_the_policy() -> None:
    with pytest.raises(Exception, match="Exchange-rate policy is required"):
        VendorBillExecutionStrategy(
            internal_use_accounting_check=NON_INTERNAL_USE_ACCOUNTING_CHECK,
            source_tax_safety_check=ALLOW_SOURCE_TAX_SAFETY_CHECK,
            source_invoice_reader=RecordingSourceInvoiceReader(source=_source()),
            vendor_bill_builder=VendorBillBuilder(),
            vendor_bill_writer=RecordingVendorBillWriter(),
            resale_accounting_check=NON_RESALE_ACCOUNTING_CHECK,
            currency_reader=StaticCurrencyPrecisionReader(2),
            fx_rate_policy=None,
        )


def test_composition_wires_the_policy_everywhere() -> None:
    from app.composition import execution

    source = inspect.getsource(execution)
    assert source.count("fx_rate_policy=build_vendor_bill_fx_rate_policy(") == 2
    assert "amounts_reader=OdooVendorBillCompanyAmountsReader(adapter=adapter)" in source


def test_no_global_rate_write_and_no_posting_anywhere_in_the_fx_path() -> None:
    for module in (vendor_bill_fx_guard, fx_reader_module, amounts_module):
        text = inspect.getsource(module)
        for forbidden in ("create(", ".write(", "unlink(", "call_model_method", "action_post", "button_validate"):
            assert forbidden not in text, (module.__name__, forbidden)


# ------------------------------------------------------------------ preview


def _preview(policy):
    use_case = PreviewVendorBillUseCase(
        source_tax_safety_check=ALLOW_SOURCE_TAX_SAFETY_CHECK,
        accepted_decision_reader=StaticAcceptedDecisionReader(_product_decision()),
        source_invoice_reader=StaticSourceInvoiceReader(_source()),
        execution_planner=ExecutionPlanner(),
        vendor_bill_builder=VendorBillBuilder(),
        currency_reader=StaticCurrencyReader(),
        product_uom_reader=StaticProductUomReader(),
        fx_rate_policy=policy,
    )
    return use_case.preview(_request(review_id="review-product-1", decision_version=1))


def _policy(*, automatic="48.6992", facts=None):
    return VendorBillFxRatePolicy(
        facts_reader=facts or FixedSourceFxFactsReader(pricing_rate("48.674900")),
        currency_reader=FixedFxRateReader(D(automatic)),
        amounts_reader=None,
    )


def test_preview_shows_invoice_rate_odoo_rate_selected_rate_and_expected_try() -> None:
    plan = _preview(_policy()).fx_rate_plan

    assert plan.status is FxRateStatus.SOURCE_RATE_SELECTED
    assert (plan.source_rate, plan.odoo_automatic_rate, plan.selected_rate) == (
        D("48.674900"),
        D("48.699200"),
        D("48.674900"),
    )
    assert plan.expected_company_total == D("6979.01")
    assert plan.expected_company_tax == D("1163.33")
    response = _fx_rate_plan_response(plan)
    assert response.selected_rate == "48.674900" and response.blocks_vendor_bill is False
    assert response.expected_company_tax == "1163.33"


def test_preview_reports_blocking_and_unavailable_evidence_instead_of_raising() -> None:
    assert _preview(_policy(facts=FixedSourceFxFactsReader())).fx_rate_plan.status is FxRateStatus.SOURCE_RATE_MISSING
    outage = FixedSourceFxFactsReader(error=SourceFxFactsUnavailableError("storage down"))
    assert _preview(_policy(facts=outage)).fx_rate_plan.status is FxRateStatus.EVIDENCE_UNAVAILABLE


def test_preview_without_a_policy_is_unchanged() -> None:
    assert _preview(None).fx_rate_plan is None


# ------------------------------------------------------------------ readback


class _Expected:
    def __init__(self, plan) -> None:
        self.fx_rate_plan = plan

    def expected_vendor_bill(self, **kwargs):
        return self


class _Amounts:
    def __init__(self, amounts) -> None:
        self.amounts = amounts

    def read_vendor_bill_company_amounts(self, *, move_id, company_id):
        return self.amounts


def _readback(plan, amounts):
    return VendorBillFxReadbackVerifier(expectation_reader=_Expected(plan), amounts_reader=_Amounts(amounts)).verify(
        review_id="r", company_id=1, decision_version=2, move_id=76
    )


def test_readback_verifies_move_76() -> None:
    verification = _readback(_plan(), _amounts())

    assert verification.status is FxVerificationStatus.VERIFIED
    assert verification.source_rate == D("49.161600") and verification.odoo_bill_rate == D("49.161600")
    assert verification.company_payable == verification.expected_company_total == D("2041.19")
    assert _fx_readback_response(verification).status == "verified"


def test_readback_reports_a_mismatch_and_a_blocked_plan() -> None:
    assert _readback(_plan(), _amounts(rate=float(1 / D("49.1802")))).status is FxVerificationStatus.MISMATCH
    blocked = _readback(_plan(facts=None), _amounts())
    assert blocked.status is FxVerificationStatus.UNVERIFIED
    assert "unverified_source_document" in blocked.mismatches[0]


def test_readback_of_a_try_bill_is_not_applicable() -> None:
    assert _readback(_plan(currency="TRY", facts=None), _amounts()).status is FxVerificationStatus.NOT_APPLICABLE


# ------------------------------------------------------------------ source document facts


UBL = """<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
  xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
  xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"
  xmlns:x="urn:example:other">
  <cbc:ID>J062026000000021</cbc:ID>
  <cbc:UUID>16a0a31e-e964-4515-ae91-13265828e63d</cbc:UUID>
  <cbc:DocumentCurrencyCode>usd</cbc:DocumentCurrencyCode>
  <cac:PricingExchangeRate>
    <cbc:SourceCurrencyCode>USD</cbc:SourceCurrencyCode>
    <cbc:TargetCurrencyCode>TRY</cbc:TargetCurrencyCode>
    <cbc:CalculationRate>49.161600</cbc:CalculationRate>
  </cac:PricingExchangeRate>
  {extra}
</Invoice>"""


def test_facts_record_every_invoice_level_rate_exactly() -> None:
    facts = extract_source_fx_facts(
        UBL.format(
            extra="<cac:PaymentExchangeRate><cbc:CalculationRate>4x</cbc:CalculationRate></cac:PaymentExchangeRate>"
            "<x:PricingExchangeRate><cbc:CalculationRate>50</cbc:CalculationRate></x:PricingExchangeRate>"
        )
    )

    assert facts.invoice_uuid == "16a0a31e-e964-4515-ae91-13265828e63d"
    assert facts.document_currency == "USD"
    pricing = facts.by_element("PricingExchangeRate")
    assert len(pricing) == 2  # a foreign namespace cannot hide a second pricing rate
    assert (pricing[0].source_currency, pricing[0].target_currency, pricing[0].calculation_rate) == (
        "USD",
        "TRY",
        D("49.161600"),
    )
    assert pricing[0].calculation_rate_text == "49.161600"
    payment = facts.by_element("PaymentExchangeRate")[0]
    assert payment.calculation_rate is None and payment.calculation_rate_text == "4x"


class _Document:
    def __init__(self, content: bytes, sha: str | None = None) -> None:
        self.storage_key = "uyumsoft/inbox/55/ubl_xml/x.xml"
        self.content_sha256 = sha if sha is not None else hashlib.sha256(content).hexdigest()


class _Locator:
    def __init__(self, *documents, error=None) -> None:
        self.documents = documents
        self.error = error

    def find_source_documents(self, *, review_id, company_id):
        if self.error is not None:
            raise self.error
        return self.documents


class _Storage:
    def __init__(self, content: bytes, error=None) -> None:
        self.content = content
        self.error = error

    def read(self, storage_key):
        if self.error is not None:
            raise self.error
        return self.content


UUID = "16a0a31e-e964-4515-ae91-13265828e63d"


def _read(locator, storage, uuid=UUID):
    return StoredSourceFxFactsReader(document_locator=locator, document_reader=storage).read_source_fx_facts(
        review_id="r", company_id=1, invoice_uuid=uuid
    )


def test_stored_reader_trusts_only_the_verified_document() -> None:
    content = UBL.format(extra="").encode()

    assert _read(_Locator(_Document(content)), _Storage(content)).document_currency == "USD"
    assert _read(_Locator(_Document(content, sha="0" * 64)), _Storage(content)) is None
    assert _read(_Locator(_Document(content)), _Storage(content), uuid="other") is None
    assert _read(_Locator(), _Storage(content)) is None
    assert _read(_Locator(_Document(content), _Document(content)), _Storage(content)) is None
    unreadable = SourceDocumentUnreadableError("gone", issue=SourceDocumentIssue.DOCUMENT_FILE_MISSING)
    assert _read(_Locator(_Document(content)), _Storage(content, error=unreadable)) is None
    assert _read(_Locator(_Document(b"<x/>")), _Storage(b"<x/>")) is None


@pytest.mark.parametrize(
    ("locator_error", "storage_error"),
    [(ReviewPersistenceError("db down"), None), (None, SourceTaxFactsUnavailableError("disk down"))],
)
def test_stored_reader_outages_are_retryable(locator_error, storage_error) -> None:
    content = UBL.format(extra="").encode()

    with pytest.raises(SourceFxFactsUnavailableError):
        _read(_Locator(_Document(content), error=locator_error), _Storage(content, error=storage_error))


def test_persisted_invoice_evidence_shape_is_unchanged() -> None:
    payload = serialize_execution_source_invoice_payload(_source())
    assert set(payload["invoice"]["header"]) == {
        "invoice_number",
        "invoice_uuid",
        "ettn",
        "invoice_type",
        "profile_id",
        "issue_date",
        "issue_time",
        "currency_code",
        "exchange_rate",
        "notes",
    }


# ------------------------------------------------------------------ Odoo readers (read-only)


class _Adapter:
    def __init__(self, responses: dict, *, error=None) -> None:
        self.responses = responses
        self.error = error
        self.calls: list[dict] = []

    def search_read(self, *, model, domain, fields, limit=None, offset=0, context=None):
        self.calls.append({"model": model, "domain": domain, "context": context})
        if self.error is not None:
            raise self.error
        key = model if model != "res.currency" else ("res.currency.id" if domain[0][0] == "id" else "res.currency")
        return self.responses[key]


CURRENCY_RESPONSES = {
    "res.company": ({"id": 1, "currency_id": [31, "TRY"]},),
    "res.currency.id": ({"id": 31, "name": "TRY", "decimal_places": 2},),
    "res.currency": ({"id": 1, "name": "USD", "rate": MOVE_76_ODOO_FLOAT},),
}


def test_currency_reader_reads_the_company_currency_and_odoos_automatic_rate() -> None:
    adapter = _Adapter(CURRENCY_RESPONSES)

    context = OdooVendorBillFxRateReader(adapter=adapter).resolve_vendor_bill_document_rate(
        company_id=1, currency_code="usd", rate_date=date(2026, 10, 6)
    )

    assert context == OdooCurrencyContext("TRY", 2, D("0.0203410792162989"))
    assert adapter.calls[-1]["context"] == {
        "date": "2026-10-06",
        "to_currency": 31,
        "company_id": 1,
        "allowed_company_ids": [1],
    }


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ErpRepositoryTimeoutError("timeout"), VendorBillWriteTransportError),
        (ErpRepositoryError("failed"), VendorBillWriteUnexpectedErpError),
    ],
)
def test_readers_classify_read_failures(error, expected) -> None:
    with pytest.raises(expected):
        OdooVendorBillFxRateReader(adapter=_Adapter({}, error=error)).resolve_vendor_bill_document_rate(
            company_id=1, currency_code="USD", rate_date=date(2026, 10, 6)
        )
    with pytest.raises(expected):
        OdooVendorBillCompanyAmountsReader(adapter=_Adapter({}, error=error)).read_vendor_bill_company_amounts(
            move_id=76, company_id=1
        )


def test_amounts_reader_reads_move_76() -> None:
    adapter = _Adapter(
        {
            "account.move": (
                {"id": 76, "state": "draft", "currency_id": [1, "USD"], "invoice_currency_rate": MOVE_76_ODOO_FLOAT},
            ),
            "account.move.line": tuple(
                {
                    "id": line.line_id,
                    "move_id": [76, "draft"],
                    "display_type": line.display_type,
                    "amount_currency": float(line.amount_currency),
                    "balance": float(line.balance),
                }
                for line in MOVE_76_LINES
            ),
        }
    )

    amounts = OdooVendorBillCompanyAmountsReader(adapter=adapter).read_vendor_bill_company_amounts(
        move_id=76, company_id=1
    )

    assert verify_vendor_bill_company_amounts(_plan(), amounts).status is FxVerificationStatus.VERIFIED
    assert [call["model"] for call in adapter.calls] == ["account.move", "account.move.line"]


def test_amounts_reader_returns_none_for_a_missing_bill_and_rejects_foreign_lines() -> None:
    reader = OdooVendorBillCompanyAmountsReader(adapter=_Adapter({"account.move": ()}))
    assert reader.read_vendor_bill_company_amounts(move_id=76, company_id=1) is None

    foreign = _Adapter(
        {
            "account.move": ({"id": 76, "state": "draft", "currency_id": [1, "USD"], "invoice_currency_rate": 0.02},),
            "account.move.line": (
                {"id": 1, "move_id": [77, "x"], "display_type": "product", "amount_currency": 1.0, "balance": 49.0},
            ),
        }
    )
    with pytest.raises(VendorBillWriteUnexpectedErpError):
        OdooVendorBillCompanyAmountsReader(adapter=foreign).read_vendor_bill_company_amounts(move_id=76, company_id=1)


async def test_client_search_read_sends_context_only_when_given() -> None:
    sent: list[dict] = []

    class _Client(OdooJson2Client):
        async def _post_json(self, path, payload):
            sent.append(payload)
            return []

    client = _Client(base_url="https://odoo.test", database="db", api_key="k", timeout_seconds=1)
    await client.search_read(model="res.currency", domain=[], fields=["id"])
    await client.search_read(model="res.currency", domain=[], fields=["id"], context={"date": "2026-10-06"})

    assert "context" not in sent[0]
    assert sent[1]["context"] == {"date": "2026-10-06"}


def test_odoo_direction_is_the_inverse_of_the_invoice_rate() -> None:
    assert odoo_invoice_currency_rate(D("49.1616")) == MOVE_76_ODOO_FLOAT
    with pytest.raises(ValueError):
        odoo_invoice_currency_rate(D("0"))
