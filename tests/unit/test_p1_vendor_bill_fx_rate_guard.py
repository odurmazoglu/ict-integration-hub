"""P1 Vendor Bill FX rate guard.

Reference: J062026000000021 / Odoo move 76 -- UBL PricingExchangeRate USD->TRY 49.161600,
Odoo invoice_currency_rate 0.0203410792162989 (= 1/49.1616), 41.52 USD = 2041.19 TRY.
"""

from __future__ import annotations

import inspect
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from app.api.routers.workbench import _fx_rate_check_response, _fx_readback_response
from app.application.dto.vendor_bill import VendorBillWriteResult
from app.application.execution.contracts import ExecutionMode, ExecutionStepStatus
from app.application.execution.planner import ExecutionPlanner
from app.application.execution.vendor_bill_fx_guard import (
    VendorBillFxRateBlockedError,
    ensure_vendor_bill_fx_rate_allowed,
    vendor_bill_fx_rate_check,
)
from app.application.execution.vendor_bill_preview import PreviewVendorBillUseCase
from app.application.execution.vendor_bill_strategy import (
    VendorBillExecutionStrategy,
    vendor_bill_write_idempotency_key,
)
from app.application.workbench.vendor_bill_readback import (
    FxReadbackStatus,
    VendorBillHeaderVerification,
    compare_vendor_bill_fx,
)
from app.billing import VendorBillBuilder, to_odoo_account_move_payload
from app.billing.fx_rate import (
    FxRateCheckStatus,
    OdooDocumentRate,
    company_rate_from_odoo,
    evaluate_vendor_bill_fx_rate,
)
from app.connectors.odoo.client import OdooJson2Client
from app.erp.exceptions import ErpRepositoryError, ErpRepositoryTimeoutError
from app.erp.odoo import vendor_bill_fx_rate_reader as fx_reader_module
from app.erp.odoo.vendor_bill_fx_rate_reader import OdooVendorBillFxRateReader
from app.erp.write.exceptions import VendorBillWriteTransportError, VendorBillWriteUnexpectedErpError
from app.persistence.execution_source_invoice_reader import serialize_execution_source_invoice_payload
from tests.unit.fx_rate_support import TRY_ONLY_FX_RATE_READER, FixedFxRateReader
from tests.unit.resale_execution_support import NON_RESALE_ACCOUNTING_CHECK, StaticCurrencyPrecisionReader
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

MOVE_76_ODOO_RATE = Decimal("0.0203410792162989")
MOVE_67_UBL_RATE = Decimal("48.674900")
D = Decimal


def _check(
    *,
    currency: str = "USD",
    source_rate: str | None = "49.161600",
    total: str | None = "41.52",
    odoo: OdooDocumentRate | None = None,
    odoo_float: Decimal | None = MOVE_76_ODOO_RATE,
    decimal_places: int = 2,
):
    return evaluate_vendor_bill_fx_rate(
        document_currency=currency,
        rate_date=date(2026, 10, 6),
        source_rate=D(source_rate) if source_rate is not None else None,
        document_total=D(total) if total is not None else None,
        odoo_rate=odoo
        if odoo is not None
        else OdooDocumentRate(
            company_currency="TRY", company_currency_decimal_places=decimal_places, invoice_currency_rate=odoo_float
        ),
    )


# ------------------------------------------------------------------ evaluator


def test_exact_rate_match_reproduces_move_76() -> None:
    check = _check()

    assert check.status is FxRateCheckStatus.MATCH
    assert not check.blocks_vendor_bill
    assert check.odoo_rate == D("49.161600")
    assert check.source_company_total == check.odoo_company_total == D("2041.19")
    assert check.company_total_difference == D("0.00")


def test_different_supplier_rate_is_a_mismatch_with_an_actionable_explanation() -> None:
    check = _check(source_rate="49.16")

    assert check.status is FxRateCheckStatus.MISMATCH
    assert check.blocks_vendor_bill
    assert check.source_company_total == D("2041.12")
    assert check.odoo_company_total == D("2041.19")
    assert check.company_total_difference == D("0.07")
    assert "49.16 TRY differs from Odoo's rate 1 USD = 49.161600 TRY" in check.message
    assert "No Vendor Bill was created" in check.message
    assert "does not change exchange rates" in check.message


def test_a_rate_difference_below_six_decimals_that_changes_no_cent_matches() -> None:
    assert _check(source_rate="49.16160004").status is FxRateCheckStatus.MATCH


def test_a_six_decimal_rate_difference_is_a_mismatch_even_without_a_cent_difference() -> None:
    check = _check(source_rate="49.161601")

    assert check.status is FxRateCheckStatus.MISMATCH
    assert check.company_total_difference == D("0.00")


def test_a_cent_difference_from_a_large_total_is_a_mismatch() -> None:
    # 1,000,000 USD x 0.0000004 = 0.40 TRY: below RATE_QUANTUM, but not the same TRY amount.
    assert _check(source_rate="49.1616004", total="1000000.00").status is FxRateCheckStatus.MISMATCH


@pytest.mark.parametrize("reversed_rate", ["0.020341", "0.0203410792162989", "0.02034"])
def test_reversed_currency_direction_is_rejected_not_inverted(reversed_rate: str) -> None:
    check = _check(source_rate=reversed_rate)

    assert check.status is FxRateCheckStatus.SOURCE_RATE_DIRECTION_INVALID
    assert check.blocks_vendor_bill
    assert "reversed direction (1 TRY = " in check.message
    assert "does not invert rates" in check.message


def test_a_rate_for_another_currency_is_a_mismatch_not_a_reversal() -> None:
    # A EUR rate (55.0757) stated on a USD bill: neither equal nor reciprocal to 49.1616.
    assert _check(source_rate="55.0757").status is FxRateCheckStatus.MISMATCH


@pytest.mark.parametrize("rate", [None, "0", "-49.1616"])
def test_missing_ubl_rate_is_distinct_from_a_mismatch(rate: str | None) -> None:
    check = _check(source_rate=rate)

    assert check.status is FxRateCheckStatus.SOURCE_RATE_MISSING
    assert check.blocks_vendor_bill
    assert check.odoo_rate == D("49.161600")


@pytest.mark.parametrize("odoo_float", [None, D("1"), D("0"), D("-0.02")])
def test_missing_odoo_rate_is_unavailable(odoo_float: Decimal | None) -> None:
    # 1.0 is Odoo's fallback when it holds no rate at all for the currency.
    check = _check(odoo_float=odoo_float)

    assert check.status is FxRateCheckStatus.ODOO_RATE_UNAVAILABLE
    assert check.blocks_vendor_bill
    assert "could not be determined" in check.message


def test_no_odoo_resolution_at_all_is_unavailable() -> None:
    check = evaluate_vendor_bill_fx_rate(
        document_currency="USD",
        rate_date=date(2026, 10, 6),
        source_rate=D("49.1616"),
        document_total=D("41.52"),
        odoo_rate=None,
    )

    assert check.status is FxRateCheckStatus.ODOO_RATE_UNAVAILABLE


def test_eur_rate_matches_with_odoo_float_noise() -> None:
    # Odoo EUR 2026-10-06: 0.01815682778430415 = 1/55.0757.
    check = _check(currency="EUR", source_rate="55.0757", total="100.00", odoo_float=D("0.01815682778430415"))

    assert check.status is FxRateCheckStatus.MATCH
    assert check.odoo_rate == D("55.075700")
    assert check.odoo_company_total == D("5507.57")


def test_eur_bill_with_a_usd_rate_is_a_mismatch() -> None:
    check = _check(currency="EUR", source_rate="49.1616", total="100.00", odoo_float=D("0.01815682778430415"))

    assert check.status is FxRateCheckStatus.MISMATCH


def test_company_currency_bill_is_not_applicable() -> None:
    check = _check(currency="TRY", source_rate=None, odoo_float=None)

    assert check.status is FxRateCheckStatus.NOT_APPLICABLE
    assert not check.blocks_vendor_bill


def test_odoo_float_noise_is_normalized_not_compared_raw() -> None:
    # Odoo 2026-09-17: 0.020544469531524463 inverts to 48.674899999999994...
    assert company_rate_from_odoo(D("0.020544469531524463")) == D("48.674900")


def test_company_currency_rounding_is_half_up_at_the_company_precision() -> None:
    # 0.25 x 49.1 = 12.275 -> 12.28 (half away from zero, like Odoo's float_round).
    check = _check(source_rate="49.1", total="0.25", odoo_float=D(1) / D("49.1"))
    assert check.source_company_total == D("12.28")
    # A zero-decimal company currency rounds to whole units.
    whole = _check(source_rate="49.1", total="0.25", odoo_float=D(1) / D("49.1"), decimal_places=0)
    assert whole.source_company_total == D("12")


# Production evidence 2026-10-20: UBL rate, Odoo invoice_currency_rate, amount_total_signed.
HISTORICAL_BILLS = [
    pytest.param("850.50", "48.6654", "0.02054848002893226", "41389.92", 1, id="move-66-VTL2026000003029"),
    pytest.param("143.38", "48.674900", "0.020544469531524463", "6979.01", 1, id="move-67-I182026000000047"),
    pytest.param("430.08", "48.674900", "0.020544469531524463", "20934.10", 1, id="move-68-I182026000000037"),
    pytest.param("41.52", "49.161600", "0.0203410792162989", "2041.19", 2, id="move-76-J062026000000021"),
]


@pytest.mark.parametrize(("total", "ubl_rate", "odoo_rate", "signed_total", "line_count"), HISTORICAL_BILLS)
def test_existing_historical_bills_match_before_and_verify_after(
    total: str, ubl_rate: str, odoo_rate: str, signed_total: str, line_count: int
) -> None:
    check = _check(source_rate=ubl_rate, total=total, odoo_float=D(odoo_rate))
    assert check.status is FxRateCheckStatus.MATCH
    assert check.odoo_company_total == D(signed_total)

    verification = compare_vendor_bill_fx(
        check, _header(odoo_rate=D(odoo_rate), signed_total=f"-{signed_total}"), line_count=line_count
    )
    assert verification.status is FxReadbackStatus.VERIFIED
    assert verification.company_total_difference == D("0.00")
    assert verification.mismatches == ()


# ------------------------------------------------------------------ guard


def test_try_bill_never_reads_odoo() -> None:
    check = vendor_bill_fx_rate_check(
        TRY_ONLY_FX_RATE_READER, _invoice(currency="TRY"), company_id=7, rate_date=date(2026, 10, 6)
    )

    assert check.status is FxRateCheckStatus.NOT_APPLICABLE


def test_guard_reads_odoo_for_the_bill_date_and_raises_on_mismatch() -> None:
    reader = FixedFxRateReader(D("49.1802"))

    with pytest.raises(VendorBillFxRateBlockedError) as raised:
        ensure_vendor_bill_fx_rate_allowed(reader, _invoice(), company_id=7, rate_date=date(2026, 10, 6))

    assert raised.value.error_code == "vendor_bill_fx_mismatch"
    assert reader.calls == [(7, "USD", date(2026, 10, 6))]


# ------------------------------------------------------------------ execution


def _strategy(fx_reader, writer=None):
    writer = writer or RecordingVendorBillWriter()
    source = _execution_source()
    strategy = VendorBillExecutionStrategy(
        source_tax_safety_check=ALLOW_SOURCE_TAX_SAFETY_CHECK,
        source_invoice_reader=RecordingSourceInvoiceReader(source=source),
        vendor_bill_builder=VendorBillBuilder(),
        vendor_bill_writer=writer,
        resale_accounting_check=NON_RESALE_ACCOUNTING_CHECK,
        currency_reader=StaticCurrencyPrecisionReader(2),
        fx_rate_reader=fx_reader,
    )
    return strategy, writer, source


@pytest.mark.parametrize("mode", [ExecutionMode.DRY_RUN, ExecutionMode.EXECUTE])
def test_matching_rate_executes_once(mode: ExecutionMode) -> None:
    reader = FixedFxRateReader(D("48.6749"))
    strategy, writer, source = _strategy(reader)

    result = strategy.execute(_step_request(mode=mode, approved_by="finance"))

    assert result.status in {ExecutionStepStatus.EXECUTED, ExecutionStepStatus.DRY_RUN_OK}
    assert writer.calls == 1
    assert reader.calls == [(7, "USD", source.invoice.header.issue_date)]


@pytest.mark.parametrize(
    ("reader", "error_code"),
    [
        (FixedFxRateReader(D("48.6992")), "vendor_bill_fx_mismatch"),
        (FixedFxRateReader(None), "vendor_bill_fx_odoo_rate_unavailable"),
        (FixedFxRateReader(D(1) / D("48.6749")), "vendor_bill_fx_source_rate_direction_invalid"),
    ],
)
@pytest.mark.parametrize("mode", [ExecutionMode.DRY_RUN, ExecutionMode.EXECUTE])
def test_blocked_rate_fails_closed_before_any_write(reader, error_code: str, mode: ExecutionMode) -> None:
    strategy, writer, _ = _strategy(reader)

    result = strategy.execute(_step_request(mode=mode, approved_by="finance"))

    assert result.status is ExecutionStepStatus.FAILED
    assert result.error_code == error_code
    assert result.produced_artifacts == ()
    assert writer.calls == 0


def test_missing_ubl_rate_fails_closed_before_any_write() -> None:
    writer = RecordingVendorBillWriter()
    source = _execution_source(rate=None)
    strategy = VendorBillExecutionStrategy(
        source_tax_safety_check=ALLOW_SOURCE_TAX_SAFETY_CHECK,
        source_invoice_reader=RecordingSourceInvoiceReader(source=source),
        vendor_bill_builder=VendorBillBuilder(),
        vendor_bill_writer=writer,
        resale_accounting_check=NON_RESALE_ACCOUNTING_CHECK,
        currency_reader=StaticCurrencyPrecisionReader(2),
        fx_rate_reader=FixedFxRateReader(D("48.6749")),
    )

    result = strategy.execute(_step_request(mode=ExecutionMode.EXECUTE, approved_by="finance"))

    assert result.error_code == "vendor_bill_fx_source_rate_missing"
    assert writer.calls == 0


def test_mismatch_retries_never_write_and_keep_the_same_write_identity() -> None:
    reader = FixedFxRateReader(D("48.6992"))
    strategy, writer, _ = _strategy(reader)
    request = _step_request(mode=ExecutionMode.EXECUTE, approved_by="finance")

    first = strategy.execute(request)
    second = strategy.execute(request)

    assert first.error_code == second.error_code == "vendor_bill_fx_mismatch"
    assert writer.calls == 0
    # Once the rate is corrected the one write uses the unchanged idempotency identity, so a
    # bill recovered by the writer is never duplicated.
    reader.company_rate = D("48.6749")
    existing = RecordingVendorBillWriter(
        result=VendorBillWriteResult(
            status="existing", idempotency_key=vendor_bill_write_idempotency_key(request), external_id=67
        )
    )
    strategy, _, _ = _strategy(reader, writer=existing)
    recovered = strategy.execute(request)
    assert recovered.status is ExecutionStepStatus.EXECUTED
    assert existing.commands[0].idempotency_key == vendor_bill_write_idempotency_key(request)
    assert [artifact.created for artifact in recovered.produced_artifacts] == [False]


def test_odoo_read_timeout_is_a_retryable_transport_failure_without_write() -> None:
    strategy, writer, _ = _strategy(FixedFxRateReader(None, error=VendorBillWriteTransportError("Odoo timed out.")))

    result = strategy.execute(_step_request(mode=ExecutionMode.EXECUTE, approved_by="finance"))

    assert result.error_code == "vendor_bill_transport_failure"
    assert writer.calls == 0


def test_strategy_requires_the_fx_rate_reader() -> None:
    with pytest.raises(Exception, match="Exchange-rate reader is required"):
        VendorBillExecutionStrategy(
            source_tax_safety_check=ALLOW_SOURCE_TAX_SAFETY_CHECK,
            source_invoice_reader=RecordingSourceInvoiceReader(source=_execution_source()),
            vendor_bill_builder=VendorBillBuilder(),
            vendor_bill_writer=RecordingVendorBillWriter(),
            resale_accounting_check=NON_RESALE_ACCOUNTING_CHECK,
            currency_reader=StaticCurrencyPrecisionReader(2),
            fx_rate_reader=None,
        )


def test_composition_wires_the_fx_rate_reader_everywhere() -> None:
    from app.composition import execution

    source = inspect.getsource(execution)
    assert source.count("fx_rate_reader=OdooVendorBillFxRateReader(") == 2
    assert "fx_verifier=VendorBillFxReadbackVerifier(" in source


def test_the_odoo_payload_never_carries_an_exchange_rate() -> None:
    strategy, writer, _ = _strategy(FixedFxRateReader(D("48.6749")))
    strategy.execute(_step_request(mode=ExecutionMode.EXECUTE, approved_by="finance"))

    bill = writer.commands[0].vendor_bill
    uoms = {line.product_id: 1 for line in bill.invoice_lines if line.product_id is not None}
    payload = to_odoo_account_move_payload(bill, currency_id=1, product_uom_ids=uoms)
    assert not {key for key in payload if "rate" in key}


# ------------------------------------------------------------------ preview


def _preview(fx_reader):
    use_case = PreviewVendorBillUseCase(
        source_tax_safety_check=ALLOW_SOURCE_TAX_SAFETY_CHECK,
        accepted_decision_reader=StaticAcceptedDecisionReader(_product_decision()),
        source_invoice_reader=StaticSourceInvoiceReader(_rated(_basic_source())),
        execution_planner=ExecutionPlanner(),
        vendor_bill_builder=VendorBillBuilder(),
        currency_reader=StaticCurrencyReader(),
        product_uom_reader=StaticProductUomReader(),
        fx_rate_reader=fx_reader,
    )
    return use_case.preview(_request(review_id="review-product-1", decision_version=1))


def test_preview_reports_the_match_and_the_try_totals() -> None:
    check = _preview(FixedFxRateReader(D("48.6749"))).fx_rate_check

    assert check.status is FxRateCheckStatus.MATCH
    assert check.odoo_company_total == D("6979.01")
    response = _fx_rate_check_response(check)
    assert response.status == "match" and response.blocks_vendor_bill is False
    assert response.odoo_rate == "48.674900"


def test_preview_reports_a_mismatch_instead_of_raising() -> None:
    preview = _preview(FixedFxRateReader(D("48.6992")))

    assert preview.fx_rate_check.status is FxRateCheckStatus.MISMATCH
    assert preview.monetary_reconciles is True


def test_preview_reports_an_odoo_read_failure_as_unavailable() -> None:
    preview = _preview(FixedFxRateReader(None, error=VendorBillWriteUnexpectedErpError("Odoo failed.")))

    assert preview.fx_rate_check.status is FxRateCheckStatus.ODOO_RATE_UNAVAILABLE


def test_preview_without_a_reader_is_unchanged() -> None:
    assert _preview(None).fx_rate_check is None


# ------------------------------------------------------------------ readback


def _header(*, odoo_rate: Decimal | None = MOVE_76_ODOO_RATE, signed_total: str | None = "-2041.19", currency="USD"):
    return VendorBillHeaderVerification(
        move_id=76,
        company_id=1,
        state="draft",
        move_type="in_invoice",
        partner_id=450,
        currency=currency,
        amount_untaxed=D("34.60"),
        amount_tax=D("6.92"),
        amount_total=D("41.52"),
        currency_decimal_places=2,
        invoice_currency_rate=odoo_rate,
        amount_total_signed=D(signed_total) if signed_total is not None else None,
    )


def test_readback_verifies_move_76() -> None:
    verification = compare_vendor_bill_fx(_check(), _header(), line_count=2)

    assert verification.status is FxReadbackStatus.VERIFIED
    assert verification.odoo_bill_rate == D("49.161600")
    assert verification.expected_company_total == verification.odoo_company_total == D("2041.19")
    assert _fx_readback_response(verification).status == "verified"


def test_readback_reports_an_unexpected_bill_rate() -> None:
    verification = compare_vendor_bill_fx(
        _check(), _header(odoo_rate=D(1) / D("49.1802"), signed_total="-2041.96"), line_count=2
    )

    assert verification.status is FxReadbackStatus.MISMATCH
    assert "bill rate 49.180200 != invoice rate 49.161600" in verification.mismatches
    assert any("differs from Odoo's current rate" in note for note in verification.notes)


def test_readback_accepts_per_line_rounding_within_tolerance_and_says_so() -> None:
    verification = compare_vendor_bill_fx(_check(), _header(signed_total="-2041.21"), line_count=2)

    assert verification.status is FxReadbackStatus.VERIFIED
    assert verification.rounding_tolerance == D("0.03")
    assert verification.notes == ("company-currency total differs by 0.02 (per-line rounding, within 0.03)",)


def test_readback_reports_a_total_beyond_rounding() -> None:
    verification = compare_vendor_bill_fx(_check(), _header(signed_total="-2041.50"), line_count=2)

    assert verification.status is FxReadbackStatus.MISMATCH


def test_readback_of_a_try_bill_is_not_applicable() -> None:
    check = _check(currency="TRY", source_rate=None)

    assert (
        compare_vendor_bill_fx(check, _header(currency="TRY"), line_count=1).status is FxReadbackStatus.NOT_APPLICABLE
    )


@pytest.mark.parametrize(
    ("check", "header"),
    [
        (None, _header()),
        (_check(), _header(odoo_rate=None)),
        (_check(source_rate=None), _header()),
        (_check(source_rate="0.020341"), _header()),
    ],
    ids=["no-pre-write-check", "odoo-rate-unread", "no-ubl-rate", "reversed-ubl-rate"],
)
def test_readback_without_both_rates_is_unverified_not_verified(check, header) -> None:
    verification = compare_vendor_bill_fx(check, header, line_count=2)

    assert verification.status is FxReadbackStatus.UNVERIFIED
    assert verification.mismatches


# ------------------------------------------------------------------ Odoo reader (read-only)


class _Adapter:
    def __init__(self, *, rate: object = 0.0203410792162989, company: object = (31, "TRY"), error=None) -> None:
        self.rate = rate
        self.company = company
        self.error = error
        self.calls: list[dict] = []

    def search_read(self, *, model, domain, fields, limit=None, offset=0, context=None):
        self.calls.append({"model": model, "domain": domain, "fields": fields, "context": context})
        if self.error is not None:
            raise self.error
        if model == "res.company":
            return ({"id": 1, "currency_id": list(self.company)},)
        if domain[0][0] == "id":
            return ({"id": 31, "name": "TRY", "decimal_places": 2},)
        return ({"id": 1, "name": "USD", "rate": self.rate},)


def test_reader_asks_odoo_for_the_bill_date_rate_with_read_only_calls() -> None:
    adapter = _Adapter()

    result = OdooVendorBillFxRateReader(adapter=adapter).resolve_vendor_bill_document_rate(
        company_id=1, currency_code="usd", rate_date=date(2026, 10, 6)
    )

    assert result == OdooDocumentRate(
        company_currency="TRY", company_currency_decimal_places=2, invoice_currency_rate=MOVE_76_ODOO_RATE
    )
    assert [call["model"] for call in adapter.calls] == ["res.company", "res.currency", "res.currency"]
    assert adapter.calls[-1]["context"] == {
        "date": "2026-10-06",
        "to_currency": 31,
        "company_id": 1,
        "allowed_company_ids": [1],
    }
    assert adapter.calls[-1]["domain"][0] == ["name", "=", "USD"]


@pytest.mark.parametrize("rate", [0, -0.02, False, None, "0.02", float("nan")])
def test_reader_returns_no_rate_for_an_unusable_odoo_value(rate) -> None:
    result = OdooVendorBillFxRateReader(adapter=_Adapter(rate=rate)).resolve_vendor_bill_document_rate(
        company_id=1, currency_code="USD", rate_date=date(2026, 10, 6)
    )

    assert result.invoice_currency_rate is None


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ErpRepositoryTimeoutError("timeout"), VendorBillWriteTransportError),
        (ErpRepositoryError("failed"), VendorBillWriteUnexpectedErpError),
    ],
)
def test_reader_classifies_read_failures(error, expected) -> None:
    with pytest.raises(expected):
        OdooVendorBillFxRateReader(adapter=_Adapter(error=error)).resolve_vendor_bill_document_rate(
            company_id=1, currency_code="USD", rate_date=date(2026, 10, 6)
        )


def test_reader_rejects_a_malformed_company_currency() -> None:
    with pytest.raises(VendorBillWriteUnexpectedErpError):
        OdooVendorBillFxRateReader(adapter=_Adapter(company=(0, ""))).resolve_vendor_bill_document_rate(
            company_id=1, currency_code="USD", rate_date=date(2026, 10, 6)
        )


def test_no_exchange_rate_is_ever_written() -> None:
    reader_source = inspect.getsource(fx_reader_module)
    assert "res.currency.rate" not in reader_source.replace("``res.currency.rate``", "")
    for forbidden in ("create(", "write(", "unlink(", "call_model_method"):
        assert forbidden not in reader_source
    from app.application.execution import vendor_bill_fx_guard

    assert "res.currency.rate" not in inspect.getsource(vendor_bill_fx_guard).replace("``res.currency.rate``", "")


@pytest.mark.asyncio
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


# ------------------------------------------------------------------ evidence shape


def test_persisted_invoice_evidence_shape_is_unchanged() -> None:
    """Pending reviews re-import by fingerprint (review-required imports keep no receipt), so the
    guard reads only fields already persisted -- it adds nothing to the evidence payload."""

    payload = serialize_execution_source_invoice_payload(_execution_source())
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


# ------------------------------------------------------------------ fixtures


def _rated(source, *, rate: Decimal | None = MOVE_67_UBL_RATE):
    return replace(source, invoice=replace(source.invoice, header=replace(source.invoice.header, exchange_rate=rate)))


def _execution_source(*, rate: Decimal | None = MOVE_67_UBL_RATE):
    """Move 67's invoice (I182026000000047: 143.38 USD, UBL rate 48.674900)."""

    return replace(_rated(_basic_source(), rate=rate), review_id="review-1", company_id=7, decision_version=2)


def _invoice(currency: str = "USD"):
    invoice = _rated(_basic_source()).invoice
    return replace(invoice, header=replace(invoice.header, currency_code=currency))
