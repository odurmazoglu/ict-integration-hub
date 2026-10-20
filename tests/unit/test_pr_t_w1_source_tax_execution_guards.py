"""PR T W1: the source-tax execution backstop.

Decision acceptance, Vendor Bill preview and every Vendor Bill-producing execution attempt
(DRY_RUN and EXECUTE, every accounting mode, retry and resume) re-verify the original UBL
document right before use -- independently of the review's reasons and of Stage-1/2
evidence that may predate the guard. All data is synthetic.
"""

from __future__ import annotations

from dataclasses import replace
from http import HTTPStatus

import pytest

import tests.unit.test_vendor_bill_production_wiring as _wiring
from app.api.routers.workbench import _status_code_for_exception
from app.application.execution import ExecutionMode
from app.application.execution.accepted_decision_use_cases import AcceptedDecisionExecutionStatus
from app.application.execution.contracts import ExecutionStepStatus
from app.application.execution.exceptions import ExecutionPlanningError, ExecutionSourceInvoiceError
from app.application.execution.existing_purchase_order_strategy import ExistingPurchaseOrderExecutionStrategy
from app.application.execution.runtime import ExecutionState
from app.application.execution.vendor_bill_preview import PreviewVendorBillUseCase
from app.application.execution.vendor_bill_strategy import (
    SOURCE_TAX_BLOCKED_ERROR_CODE,
    SOURCE_TAX_UNAVAILABLE_ERROR_CODE,
    VendorBillExecutionStrategy,
)
from app.application.source_tax import (
    SourceDocumentIssue,
    SourceTaxAssessment,
    SourceTaxFactsUnavailableError,
    SourceTaxSafetyBlockedError,
)
from app.application.source_tax.reader import SourceTaxSafetyCheck
from app.application.workbench.decision_use_cases import SubmitReviewDecisionUseCase
from app.application.workbench.exceptions import ReviewDecisionError
from app.application.workflow import ManualReviewReasonCode, WorkflowType
from app.domain.invoice.tax_facts import extract_source_tax_facts
from tests.unit.fx_rate_support import TRY_ONLY_FX_RATE_POLICY
from tests.unit.pr_t_w1_fixtures import LineSpec, TaxSpec, synthetic_ubl
from tests.unit.resale_execution_support import (
    NON_INTERNAL_USE_ACCOUNTING_CHECK,
    NON_RESALE_ACCOUNTING_CHECK,
    TWO_DECIMAL_CURRENCY_READER,
)
from tests.unit.source_tax_support import ALLOW_SOURCE_TAX_SAFETY_CHECK, FixedSourceTaxFactsReader
from tests.unit.test_existing_purchase_order_execution_strategy import (
    RecordingPurchaseOrderVendorBillRepository,
)
from tests.unit.test_existing_purchase_order_execution_strategy import (
    RecordingSourceInvoiceReader as PurchaseOrderSourceReader,
)
from tests.unit.test_existing_purchase_order_execution_strategy import _source as _po_source
from tests.unit.test_existing_purchase_order_execution_strategy import _step_request as _po_step_request
from tests.unit.test_fixed_asset_accounting import _invoice as _fixed_asset_invoice
from tests.unit.test_fixed_asset_accounting import _source as _fixed_asset_source
from tests.unit.test_operating_expense_immutable_evidence import _invoice as _opex_invoice
from tests.unit.test_operating_expense_immutable_evidence import _source as _opex_source
from tests.unit.test_review_decision_use_case import (
    RecordingDecisionWriter,
    RecordingEvidenceReader,
    _FakeUnitOfWork,
    _select_workflow_command,
    _source_evidence,
)
from tests.unit.test_vendor_bill_execution_strategy import (
    RecordingSourceInvoiceReader,
    RecordingVendorBillBuilder,
    RecordingVendorBillWriter,
    _source,
    _step_request,
)
from tests.unit.test_vendor_bill_preview import _d_market_source as _account_only_source
from tests.unit.test_vendor_bill_preview import _product_decision, _product_source, _request
from tests.unit.test_vendor_bill_preview import _use_case as _preview_use_case

#: The production-wiring harness fixture (in-memory SQLite Hub, fake Odoo client).
session = _wiring.session

WITHHOLDING = ManualReviewReasonCode.WITHHOLDING_TAX_NOT_SUPPORTED
UNVERIFIED = ManualReviewReasonCode.SOURCE_DOCUMENT_UNVERIFIED
UNSUPPORTED = ManualReviewReasonCode.UNSUPPORTED_INVOICE_CONTENT


def _verified(**spec) -> SourceTaxAssessment:
    return SourceTaxAssessment.verified(extract_source_tax_facts(synthetic_ubl(**spec)))


BLOCKING_ASSESSMENTS = {
    "withholding-type": (_verified(invoice_type="TEVKIFAT"), (WITHHOLDING,)),
    "withholding-line": (_verified(lines=(LineSpec(withholding=True),)), (WITHHOLDING,)),
    "withholding-invoice": (_verified(invoice_withholding=True), (WITHHOLDING,)),
    "non-vat": (_verified(lines=(LineSpec(taxes=(TaxSpec(code="4080"),)),)), (UNSUPPORTED,)),
    "unverified": (SourceTaxAssessment.unverified(SourceDocumentIssue.UUID_MISMATCH), (UNVERIFIED,)),
}


def _check(assessment: SourceTaxAssessment | None = None, *, error: Exception | None = None):
    reader = FixedSourceTaxFactsReader(assessment, error=error)
    return SourceTaxSafetyCheck(facts_reader=reader), reader


def _blocking_check(name: str = "withholding-type"):
    return _check(BLOCKING_ASSESSMENTS[name][0])


# ===================================================================== the check itself


@pytest.mark.parametrize("name", sorted(BLOCKING_ASSESSMENTS))
def test_safety_check_blocks_with_the_reason_codes(name: str) -> None:
    assessment, codes = BLOCKING_ASSESSMENTS[name]
    check, _ = _check(assessment)

    with pytest.raises(SourceTaxSafetyBlockedError) as raised:
        check.ensure_vendor_bill_allowed(review_id="r", company_id=7, invoice=_source().invoice)

    assert raised.value.reason_codes == codes
    assert raised.value.error_category == "source_tax_safety_blocked"


def test_safety_check_reads_the_review_document_with_the_invoice_uuid() -> None:
    check, reader = _check(_verified())
    invoice = _source().invoice

    facts = check.ensure_vendor_bill_allowed(review_id="review-1", company_id=7, invoice=invoice)

    assert not facts.has_withholding
    assert reader.calls == [("review-1", 7, invoice.header.invoice_uuid)]


def test_safety_check_propagates_the_retryable_error() -> None:
    check, _ = _check(error=SourceTaxFactsUnavailableError("offline"))

    with pytest.raises(SourceTaxFactsUnavailableError):
        check.ensure_vendor_bill_allowed(review_id="r", company_id=7, invoice=_source().invoice)


def test_api_maps_blocked_to_conflict_and_unavailable_to_service_unavailable() -> None:
    blocked = SourceTaxSafetyBlockedError("blocked", reason_codes=(WITHHOLDING,))

    assert _status_code_for_exception(blocked) == HTTPStatus.CONFLICT
    assert _status_code_for_exception(SourceTaxFactsUnavailableError("offline")) == HTTPStatus.SERVICE_UNAVAILABLE


# ===================================================================== decision acceptance


def _decision_use_case(check, writer: RecordingDecisionWriter) -> SubmitReviewDecisionUseCase:
    return SubmitReviewDecisionUseCase(
        review_decision_writer=writer,
        unit_of_work=_FakeUnitOfWork(),
        execution_evidence_reader=RecordingEvidenceReader(result=_source_evidence()),
        source_tax_safety_check=check,
    )


@pytest.mark.parametrize("name", sorted(BLOCKING_ASSESSMENTS))
def test_vendor_bill_decision_is_refused_for_a_blocked_document(name: str) -> None:
    writer = RecordingDecisionWriter()
    check, _ = _blocking_check(name)

    with pytest.raises(SourceTaxSafetyBlockedError):
        _decision_use_case(check, writer).execute(_select_workflow_command())

    assert writer.commands == () and writer.commands_with_evidence == ()


def test_vendor_bill_decision_storage_outage_is_retryable_and_writes_nothing() -> None:
    writer = RecordingDecisionWriter()
    check, _ = _check(error=SourceTaxFactsUnavailableError("offline"))

    with pytest.raises(SourceTaxFactsUnavailableError):
        _decision_use_case(check, writer).execute(_select_workflow_command())

    assert writer.commands_with_evidence == ()


def test_vendor_bill_decision_without_the_check_is_refused() -> None:
    writer = RecordingDecisionWriter()

    with pytest.raises(ReviewDecisionError, match="Source tax safety check is required"):
        _decision_use_case(None, writer).execute(_select_workflow_command())

    assert writer.commands_with_evidence == ()


def test_clean_vendor_bill_decision_is_accepted_and_checked_against_the_pinned_invoice() -> None:
    writer = RecordingDecisionWriter()
    check, reader = _check(_verified())

    _decision_use_case(check, writer).execute(_select_workflow_command())

    assert len(writer.commands_with_evidence) == 1
    assert reader.calls == [("review-1", 7, _source_evidence().invoice.header.invoice_uuid)]


def test_non_vendor_bill_decision_never_reads_the_document() -> None:
    writer = RecordingDecisionWriter()
    check, reader = _check(error=AssertionError("must not be read"))

    _decision_use_case(check, writer).execute(_select_workflow_command(selected_workflow=WorkflowType.RFQ))

    assert reader.calls == []
    assert len(writer.commands) == 1


# ===================================================================== preview


def _preview(check) -> PreviewVendorBillUseCase:
    use_case, _, _ = _preview_use_case(decision=_product_decision(), source=_product_source())
    use_case._source_tax_safety_check = check
    return use_case


def test_preview_refuses_a_blocked_document_before_any_build() -> None:
    check, _ = _blocking_check()

    with pytest.raises(SourceTaxSafetyBlockedError):
        _preview(check).preview(_request(review_id="review-product-1", decision_version=1))


def test_preview_storage_outage_is_retryable() -> None:
    check, _ = _check(error=SourceTaxFactsUnavailableError("offline"))

    with pytest.raises(SourceTaxFactsUnavailableError):
        _preview(check).preview(_request(review_id="review-product-1", decision_version=1))


def test_preview_of_a_clean_document_is_unchanged() -> None:
    check, _ = _check(_verified())

    assert _preview(check).preview(_request(review_id="review-product-1", decision_version=1)).lines


def test_preview_requires_the_check() -> None:
    with pytest.raises(TypeError):
        PreviewVendorBillUseCase(  # type: ignore[call-arg]
            accepted_decision_reader=object(),
            source_invoice_reader=object(),
            execution_planner=object(),
            vendor_bill_builder=object(),
            currency_reader=object(),
            product_uom_reader=object(),
        )
    with pytest.raises(ExecutionPlanningError):
        PreviewVendorBillUseCase(
            accepted_decision_reader=object(),  # type: ignore[arg-type]
            source_invoice_reader=object(),  # type: ignore[arg-type]
            execution_planner=object(),  # type: ignore[arg-type]
            vendor_bill_builder=object(),  # type: ignore[arg-type]
            currency_reader=object(),  # type: ignore[arg-type]
            product_uom_reader=object(),  # type: ignore[arg-type]
            source_tax_safety_check=None,  # type: ignore[arg-type]
        )


# ===================================================================== Vendor Bill execution strategy


class _RecordingResaleCheck:
    def __init__(self) -> None:
        self.calls = 0

    def validate(self, source):
        self.calls += 1
        return NON_RESALE_ACCOUNTING_CHECK.validate(source)


def _accounting_mode_source(mode: str):
    """A valid pinned source per accounting mode, taken from that mode's own test suite."""

    if mode == "operating_expense":
        return _opex_source(_opex_invoice())
    if mode == "fixed_asset":
        return _fixed_asset_source(_fixed_asset_invoice(ettn="W1-FA-1"))
    if mode == "account_only":
        return _account_only_source(account_only=True)
    return _source()  # direct product; RESALE shares the product-backed shape


def _request_for(source, mode: ExecutionMode):
    return replace(
        _step_request(mode=mode, approved_by="finance.lead"),
        review_id=source.review_id,
        company_id=source.company_id,
        decision_version=source.decision_version,
    )


ACCOUNTING_MODES = ["direct_product", "resale", "operating_expense", "account_only", "fixed_asset"]


def _strategy(check, source):
    builder = RecordingVendorBillBuilder()
    writer = RecordingVendorBillWriter()
    resale = _RecordingResaleCheck()
    strategy = VendorBillExecutionStrategy(
        internal_use_accounting_check=NON_INTERNAL_USE_ACCOUNTING_CHECK,
        source_invoice_reader=RecordingSourceInvoiceReader(source=source),
        vendor_bill_builder=builder,
        vendor_bill_writer=writer,
        resale_accounting_check=resale,
        currency_reader=TWO_DECIMAL_CURRENCY_READER,
        source_tax_safety_check=check,
        fx_rate_policy=TRY_ONLY_FX_RATE_POLICY,
    )
    return strategy, builder, writer, resale


@pytest.mark.parametrize("mode", [ExecutionMode.DRY_RUN, ExecutionMode.EXECUTE])
@pytest.mark.parametrize("accounting_mode", ACCOUNTING_MODES)
@pytest.mark.parametrize("name", sorted(BLOCKING_ASSESSMENTS))
def test_execution_is_blocked_before_any_build_or_write(mode, accounting_mode: str, name: str) -> None:
    check, reader = _blocking_check(name)
    source = _accounting_mode_source(accounting_mode)
    strategy, builder, writer, resale = _strategy(check, source)

    result = strategy.execute(_request_for(source, mode))

    assert result.status is ExecutionStepStatus.FAILED
    assert result.error_code == SOURCE_TAX_BLOCKED_ERROR_CODE
    assert builder.calls == 0
    assert writer.commands == ()
    assert resale.calls == 0
    assert len(reader.calls) == 1


@pytest.mark.parametrize("mode", [ExecutionMode.DRY_RUN, ExecutionMode.EXECUTE])
def test_execution_storage_outage_is_a_retryable_failure(mode) -> None:
    check, _ = _check(error=SourceTaxFactsUnavailableError("offline"))
    strategy, builder, writer, _ = _strategy(check, _source())

    result = strategy.execute(_step_request(mode=mode, approved_by="finance.lead"))

    assert result.status is ExecutionStepStatus.FAILED
    assert result.error_code == SOURCE_TAX_UNAVAILABLE_ERROR_CODE
    assert builder.calls == 0 and writer.commands == ()


def test_every_attempt_re_verifies_the_document() -> None:
    check, reader = _check(_verified())
    strategy, _, writer, _ = _strategy(check, _source())

    first = strategy.execute(_step_request(mode=ExecutionMode.DRY_RUN))
    reader.assessment = BLOCKING_ASSESSMENTS["withholding-line"][0]
    second = strategy.execute(_step_request(mode=ExecutionMode.DRY_RUN))

    assert first.status is ExecutionStepStatus.DRY_RUN_OK
    assert second.status is ExecutionStepStatus.FAILED
    assert len(reader.calls) == 2
    assert len(writer.commands) == 1


def test_strategy_requires_the_check() -> None:
    with pytest.raises(ExecutionSourceInvoiceError):
        _strategy(None, _source())


# ===================================================================== Existing Purchase Order billing


@pytest.mark.parametrize("mode", [ExecutionMode.DRY_RUN, ExecutionMode.EXECUTE])
def test_purchase_order_billing_is_blocked_before_any_odoo_call(mode) -> None:
    check, _ = _blocking_check()
    repository = RecordingPurchaseOrderVendorBillRepository()
    strategy = ExistingPurchaseOrderExecutionStrategy(
        source_invoice_reader=PurchaseOrderSourceReader(_po_source()),
        purchase_order_vendor_bill_repository=repository,
        source_tax_safety_check=check,
    )

    result = strategy.execute(_po_step_request(mode=mode))

    assert result.status is ExecutionStepStatus.FAILED
    assert result.error_code == SOURCE_TAX_BLOCKED_ERROR_CODE
    assert repository.calls == []


def test_purchase_order_billing_requires_the_check() -> None:
    with pytest.raises(ExecutionSourceInvoiceError):
        ExistingPurchaseOrderExecutionStrategy(
            source_invoice_reader=PurchaseOrderSourceReader(_po_source()),
            purchase_order_vendor_bill_repository=RecordingPurchaseOrderVendorBillRepository(),
            source_tax_safety_check=None,  # type: ignore[arg-type]
        )


# ===================================================================== production runtime: retry and resume


def _patch_check(monkeypatch: pytest.MonkeyPatch, check) -> None:
    monkeypatch.setattr("app.composition.execution.build_source_tax_safety_check", lambda **_kwargs: check)


def test_runtime_with_the_real_reader_blocks_a_review_without_a_stored_document(session) -> None:
    """No patching: the production composition reads the Hub; this review has no UBL row."""

    _wiring._submit_vendor_bill_decision(session)
    client = _wiring.FakeOdooVendorBillClient()

    result = _wiring._use_case(session, client=client, settings=_wiring._execute_settings()).execute(
        _wiring._command(mode=ExecutionMode.EXECUTE)
    )

    assert result.status is not AcceptedDecisionExecutionStatus.EXECUTED
    assert client.search_calls == [] and client.create_calls == []


def test_runtime_retry_and_resume_never_write_a_withholding_invoice(session, monkeypatch) -> None:
    _wiring._submit_vendor_bill_decision(session)
    check, reader = _blocking_check()
    _patch_check(monkeypatch, check)
    client = _wiring.FakeOdooVendorBillClient()
    use_case = _wiring._use_case(session, client=client, settings=_wiring._execute_settings())

    first = use_case.execute(_wiring._command(mode=ExecutionMode.EXECUTE))
    resumed = use_case.execute(_wiring._command(mode=ExecutionMode.EXECUTE))

    assert first.runtime_state is ExecutionState.WAITING_RETRY
    assert resumed.status is not AcceptedDecisionExecutionStatus.EXECUTED
    assert len(reader.calls) == 2  # every attempt re-verified
    assert client.search_calls == [] and client.create_calls == []


def test_runtime_storage_outage_retries_into_a_clean_execution(session, monkeypatch) -> None:
    _wiring._submit_vendor_bill_decision(session)
    check, reader = _check(error=SourceTaxFactsUnavailableError("offline"))
    _patch_check(monkeypatch, check)
    client = _wiring.FakeOdooVendorBillClient(created_id=9001)
    use_case = _wiring._use_case(session, client=client, settings=_wiring._execute_settings())

    first = use_case.execute(_wiring._command(mode=ExecutionMode.EXECUTE))
    assert first.runtime_state is ExecutionState.WAITING_RETRY
    assert client.create_calls == []

    reader.error = None
    reader.assessment = _verified()
    second = use_case.execute(_wiring._command(mode=ExecutionMode.EXECUTE))

    assert second.status is AcceptedDecisionExecutionStatus.EXECUTED
    assert len(client.create_calls) == 1


def test_runtime_resume_after_a_timeout_re_verifies_before_recovering(session, monkeypatch) -> None:
    """A step that timed out after create is resumed; the resume attempt re-checks first."""

    _wiring._submit_vendor_bill_decision(session)
    check, reader = _check(_verified())
    _patch_check(monkeypatch, check)
    client = _wiring.FakeOdooVendorBillClient(timeout_after_create=True, created_id=9001)
    use_case = _wiring._use_case(session, client=client, settings=_wiring._execute_settings())

    first = use_case.execute(_wiring._command(mode=ExecutionMode.EXECUTE))
    second = use_case.execute(_wiring._command(mode=ExecutionMode.EXECUTE))

    assert first.runtime_state is ExecutionState.WAITING_RETRY
    assert second.status is AcceptedDecisionExecutionStatus.EXECUTED
    assert len(client.create_calls) == 1
    assert len(reader.calls) == 2


def test_allow_check_used_by_other_tests_is_the_real_check() -> None:
    assert isinstance(ALLOW_SOURCE_TAX_SAFETY_CHECK, SourceTaxSafetyCheck)
