"""PR S: invoice classification safety guards.

Part A -- a return invoice (``InvoiceTypeCode`` IADE family) never becomes a Vendor Bill:
the rule engine selects Manual Review with only RETURN_INVOICE_NOT_SUPPORTED, Stage-1
evidence is never pinned, and the shared ``validate_vendor_bill_inputs`` gate (decision
acceptance, preview, execution) rejects it in every mode.

Part B -- an identifier-free invoice from a registered product-source supplier (exact VKN
registry) never becomes an operating expense automatically: no supplier-wide mapping, no
per-line account-only override. It carries AMBIGUOUS_COMMERCIAL_PURCHASE until an explicit
review-scoped purchase purpose + accounting resolution is recorded for that review.

All supplier data here is synthetic: the registry is patched to a synthetic VKN.

Everything else -- normal purchases, genuine product-less expenses, product mapping,
supplier matching, fixed assets, import idempotency -- is unchanged.
"""

from __future__ import annotations

from dataclasses import replace
from enum import StrEnum

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.application.commands import ImportInvoiceCommand
from app.application.decision import DecisionEngine, ManualReviewStrategy, WorkflowStrategyResolver
from app.application.dto import DecisionResult
from app.application.exceptions import ApplicationError
from app.application.expense_mapping import (
    MATCHED_BY_REVIEW_ACCOUNTING_RESOLUTION,
    OperatingExpenseMatchResult,
    OperatingExpenseMatchStatus,
    invoice_is_ambiguous_commercial_purchase,
    invoice_is_product_identifier_free,
)
from app.application.rules.deterministic import (
    DIRECT_VENDOR_BILL_RULE_ID,
    MANUAL_REVIEW_RULE_ID,
    OPERATING_EXPENSE_VENDOR_BILL_RULE_ID,
)
from app.application.rules.deterministic import DeterministicRuleEngine as _DeterministicRuleEngine
from app.application.use_cases import ImportInvoiceUseCase
from app.application.use_cases.effective_decision import _effective_manual_review_reasons, _effective_workflow
from app.application.use_cases.review_classification_outcome import build_review_execution_evidence
from app.application.workbench import ReviewItemCreationService
from app.application.workbench.accounting_resolution import (
    AccountingTreatmentType,
    SubmitReviewAccountingResolutionCommand,
)
from app.application.workbench.commands import ReviewDecisionCommand
from app.application.workbench.decision_use_cases import SubmitReviewDecisionUseCase
from app.application.workbench.dto import LineResolution, ReviewDecisionType, ReviewStatus
from app.application.workbench.exceptions import PurchasePurposeEligibilityError, ReviewDecisionError
from app.application.workbench.operator_guidance import (
    REASON_TITLES,
    GuidanceInput,
    OperatorGuidanceFacts,
    OperatorNextAction,
    build_operator_guidance,
)
from app.application.workbench.purchase_purpose import PurchasePurpose, SubmitPurchasePurposeCommand
from app.application.workbench.purchase_purpose_use_cases import SubmitPurchasePurposeUseCase
from app.application.workbench.queries import ReviewDetailQuery
from app.application.workbench.selected_expense_account_resolution import ResolutionAccountRecord
from app.application.workflow import ManualReviewReason, ManualReviewReasonCode, WorkflowType
from app.billing import VendorBillBuilder
from app.billing.builder import validate_vendor_bill_inputs
from app.billing.exceptions import VendorBillBuildError
from app.db.base import Base
from app.domain.invoice import InternalInvoice, Party, parse_ubl_invoice, source_profiles
from app.domain.invoice.document_types import is_return_invoice, return_invoice_type_code
from app.domain.invoice.source_profiles import is_product_source_supplier
from app.erp.models import Product
from app.matching import InvoiceProductMatchResult, PartnerMatchStatus, ProductMatchingEngine, ProductMatchStatus
from app.models.execution_source_invoice_evidence import ExecutionSourceInvoiceEvidence
from app.models.import_receipt import ImportReceipt
from app.models.operating_expense_mapping import OperatingExpenseMappingRecord
from app.models.workbench_review_execution_evidence import WorkbenchReviewExecutionEvidence
from app.models.workbench_review_item import WorkbenchReviewItem
from app.persistence import (
    SqlAlchemyExecutionSourceInvoiceReader,
    SqlAlchemyImportHistory,
    SqlAlchemyOperatingExpenseMappingRepository,
    SqlAlchemyReviewExecutionEvidenceReader,
    SqlAlchemyReviewPurchasePurposeResolutionRepository,
    SqlAlchemyReviewRepository,
    SqlAlchemyReviewSourceInvoiceEvidenceReader,
    SqlAlchemyUnitOfWork,
)
from tests.unit.test_fixed_asset_accounting import ACTOR as FA_ACTOR
from tests.unit.test_fixed_asset_accounting import EXPENSE_ACCOUNT as FA_EXPENSE_ACCOUNT
from tests.unit.test_fixed_asset_accounting import PARTNER_ID as FA_PARTNER_ID
from tests.unit.test_fixed_asset_accounting import _accounting_use_case as _fa_accounting_use_case
from tests.unit.test_fixed_asset_accounting import _decide as _fa_decide
from tests.unit.test_fixed_asset_accounting import _decision_engine as _fa_decision_engine
from tests.unit.test_fixed_asset_accounting import _Facts as _FAFacts
from tests.unit.test_fixed_asset_accounting import _FakeImportHistory as _FAFakeImportHistory
from tests.unit.test_fixed_asset_accounting import _invoice as _two_line_invoice
from tests.unit.test_fixed_asset_accounting import _partner_matched as _fa_partner_matched
from tests.unit.test_fixed_asset_accounting import _products_invalid_input as _fa_products_invalid_input
from tests.unit.test_fixed_asset_accounting import _source as _fixed_asset_source
from tests.unit.test_fixed_asset_accounting import _taxes as _fa_taxes
from tests.unit.test_operating_expense_classification import (
    _command,
    _engine,
    _FakeMatcher,
    _FakeOperatingExpenseMatcher,
    _identifier_free_product_match,
    _invoice,
    _line,
    _matched_expense_result,
    _matched_product_match,
    _partner_match,
    _tax_match,
)
from tests.unit.test_operating_expense_vendor_bill_builder import (
    _expense_match,
    _identifier_free_products,
    _matched_products,
    _partner,
    _status_products,
    _taxes,
)
from tests.unit.test_operating_expense_vendor_bill_builder import _invoice as _builder_invoice
from tests.unit.test_operating_expense_vendor_bill_builder import _line as _builder_line
from tests.unit.test_operating_expense_vendor_bill_builder import _product_line as _builder_product_line

RETURN = ManualReviewReasonCode.RETURN_INVOICE_NOT_SUPPORTED
AMBIGUOUS = ManualReviewReasonCode.AMBIGUOUS_COMMERCIAL_PURCHASE
OPEX_REQUIRED = ManualReviewReasonCode.OPERATING_EXPENSE_MAPPING_REQUIRED
#: Synthetic VKNs only.
PRODUCT_SOURCE_VKN = "9000000001"
OTHER_VKN = "1000000009"
#: The production registry as shipped, captured before the autouse patch below.
_SHIPPED_PRODUCT_SOURCE_REGISTRY = source_profiles.PRODUCT_SOURCE_SUPPLIER_VKNS


@pytest.fixture(autouse=True)
def _synthetic_product_source_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(source_profiles, "PRODUCT_SOURCE_SUPPLIER_VKNS", frozenset({PRODUCT_SOURCE_VKN}))


def test_shipped_registry_is_an_exact_vkn_set() -> None:
    assert isinstance(_SHIPPED_PRODUCT_SOURCE_REGISTRY, frozenset)
    assert len(_SHIPPED_PRODUCT_SOURCE_REGISTRY) == 1
    assert all(vkn.isdigit() and len(vkn) == 10 for vkn in _SHIPPED_PRODUCT_SOURCE_REGISTRY)


def _typed(invoice: InternalInvoice, invoice_type: str | None) -> InternalInvoice:
    return replace(invoice, header=replace(invoice.header, invoice_type=invoice_type))


def _from_supplier(invoice: InternalInvoice, vkn: str | None, *, name: str = "Supplier") -> InternalInvoice:
    return replace(invoice, supplier=Party(name=name, tax_number=vkn))


def _codes(result) -> tuple[ManualReviewReasonCode, ...]:
    assert result.workflow_decision.manual_review is not None
    return tuple(reason.code for reason in result.workflow_decision.manual_review.reasons)


def _assert_manual_review(result, codes: tuple[ManualReviewReasonCode, ...]) -> None:
    assert result.workflow is WorkflowType.MANUAL_REVIEW
    assert result.matched_rule == MANUAL_REVIEW_RULE_ID
    assert _codes(result) == codes


# ===================================================================== Part A: IADE


@pytest.mark.parametrize("code", ["IADE", " iade ", "TEVKIFATIADE", "YTBIADE", "YTBTEVKIFATIADE"])
def test_return_type_codes_are_detected_exactly(code: str) -> None:
    invoice = _typed(_invoice(), code)

    assert is_return_invoice(invoice)
    assert return_invoice_type_code(invoice) == code.strip().upper()


@pytest.mark.parametrize("code", [None, "SATIS", "TEVKIFAT", "ISTISNA", "IHRACKAYITLI", "OZELMATRAH", "SATIS IADE"])
def test_non_return_type_codes_are_not_returns(code: str | None) -> None:
    assert not is_return_invoice(_typed(_invoice(), code))


def test_parser_preserves_iade_invoice_type_code() -> None:
    invoice = parse_ubl_invoice(_ubl(invoice_type="IADE", item_xml="<cbc:Name>Returned goods</cbc:Name>"))

    assert invoice.header.invoice_type == "IADE"
    assert is_return_invoice(invoice)


def test_fully_matched_iade_is_hard_blocked_with_only_the_return_reason() -> None:
    invoice = _typed(_invoice(lines=(_line(seller_item_code="S-1"),)), "IADE")
    engine = _engine(invoice=invoice, product_match=_matched_product_match(invoice))

    result = engine.evaluate(_command(invoice))

    _assert_manual_review(result, (RETURN,))
    reason = result.workflow_decision.manual_review.reasons[0]
    assert reason.source == "document_type"
    assert ("invoice_type_code", "IADE") in reason.details
    # Facts are preserved, never reinterpreted: the match results are still recorded.
    assert result.partner_match.status is PartnerMatchStatus.MATCHED
    assert result.product_match.line_results[0].result.status is ProductMatchStatus.MATCHED


def test_iade_never_takes_the_operating_expense_branch() -> None:
    invoice = _typed(_invoice(), "IADE")
    engine = _engine(
        invoice=invoice,
        product_match=_identifier_free_product_match(invoice),
        operating_expense_match=_matched_expense_result(),
    )

    _assert_manual_review(engine.evaluate(_command(invoice)), (RETURN,))


def test_iade_with_unmatched_supplier_still_reports_only_the_return_reason() -> None:
    invoice = _typed(_invoice(), "IADE")
    engine = _engine(invoice=invoice, partner_match=_partner_match(_status_not_found()))

    _assert_manual_review(engine.evaluate(_command(invoice)), (RETURN,))


@pytest.mark.parametrize("mode", ["product", "expense", "account_only"])
def test_bill_validation_rejects_iade_in_every_non_asset_mode(mode: str) -> None:
    invoice = _typed(_builder_invoice(), "IADE")
    baseline = _typed(invoice, "SATIS")
    kwargs, products = _mode_inputs(mode, invoice)
    assert validate_vendor_bill_inputs(baseline, _partner(), products, _taxes(baseline), **kwargs).is_valid

    result = validate_vendor_bill_inputs(invoice, _partner(), products, _taxes(invoice), **kwargs)

    assert not result.is_valid
    assert any("Return invoice" in error for error in result.errors)


def test_bill_validation_rejects_iade_in_fixed_asset_mode() -> None:
    invoice = _two_line_invoice(ettn="PR-S-FA-1")
    source = _fixed_asset_source(invoice)
    args = (source.partner_match, source.product_match, source.tax_match)
    assert validate_vendor_bill_inputs(invoice, *args, fixed_asset_accounting=source.fixed_asset_accounting).is_valid

    result = validate_vendor_bill_inputs(
        _typed(invoice, "IADE"), *args, fixed_asset_accounting=source.fixed_asset_accounting
    )

    assert not result.is_valid


@pytest.mark.parametrize("shape", ["matched", "mixed"])
def test_iade_never_gets_stage_one_execution_evidence(shape: str) -> None:
    if shape == "matched":
        invoice = _builder_invoice()
        products = _matched_products(invoice)
    else:  # identifier present, no Odoo product: would pin account-only evidence
        invoice = _builder_invoice([_builder_line("1", seller_item_code="S-1")])
        products = _status_products_not_found(invoice)
    decision = _decision_result(invoice, products)
    assert _evidence(invoice, decision) is not None

    assert _evidence(_typed(invoice, "IADE"), decision) is None


def test_no_review_scoped_resolution_strips_the_return_reason() -> None:
    reasons = (_reason(RETURN),)

    effective = _effective_manual_review_reasons(reasons, None, _matched_expense_result(), fixed_asset_resolved=True)

    assert tuple(reason.code for reason in effective) == (RETURN,)
    assert _effective_workflow(WorkflowType.MANUAL_REVIEW, effective) is WorkflowType.MANUAL_REVIEW


def test_workbench_shows_return_block_before_any_other_step() -> None:
    guidance = _guidance((RETURN, ManualReviewReasonCode.SUPPLIER_NOT_FOUND))

    assert guidance.next_action is OperatorNextAction.TECHNICAL
    assert "iade faturasıdır" in guidance.todo_html
    assert REASON_TITLES[RETURN] in guidance.todo_html


@pytest.mark.asyncio
async def test_iade_import_creates_one_review_with_preserved_source_and_is_idempotent(session: Session) -> None:
    invoice = _typed(_invoice(lines=(_line(seller_item_code="S-1"),)), "IADE")
    rule_engine = _engine(invoice=invoice, product_match=_matched_product_match(invoice))
    repository = SqlAlchemyReviewRepository(session)
    use_case = ImportInvoiceUseCase(
        import_history=SqlAlchemyImportHistory(session),
        decision_engine=DecisionEngine(
            rule_engine=rule_engine,
            strategy_resolver=WorkflowStrategyResolver([ManualReviewStrategy()]),
        ),
        review_item_creation_service=ReviewItemCreationService(repository),
    )
    command = ImportInvoiceCommand(
        invoice=invoice, idempotency_key="uyumsoft:company:1:inbox:ettn:CD3E75D2", company_id=1, dry_run=True
    )

    first = await use_case.execute(command)
    second = await use_case.execute(command)

    assert first.status == "review_required"
    assert second.status == "already_imported"
    assert len(session.scalars(select(WorkbenchReviewItem)).all()) == 1
    assert session.scalars(select(ImportReceipt)).all() == []
    review = repository.get_review_item(ReviewDetailQuery(review_id=first.review_id, company_id=1))
    assert tuple(reason.code for reason in review.review_reasons) == (RETURN,)
    assert session.scalars(select(WorkbenchReviewExecutionEvidence)).all() == []


# ===================================================================== Part B: product-less commercial purchases


def test_product_less_commercial_shape_is_identifier_free_and_ambiguous_commercial() -> None:
    invoice = _product_less_commercial_invoice()

    assert invoice.supplier.tax_number == PRODUCT_SOURCE_VKN
    assert invoice_is_product_identifier_free(invoice)
    assert is_product_source_supplier(invoice)
    assert invoice_is_ambiguous_commercial_purchase(invoice)


def test_product_less_commercial_real_product_matcher_reports_missing_identifier() -> None:
    invoice = _product_less_commercial_invoice()

    result = ProductMatchingEngine(FakeProvider(RecordingProductRepository())).match_invoice(invoice)

    assert [line.result.status for line in result.line_results] == [ProductMatchStatus.INVALID_INPUT]


@pytest.mark.parametrize("expense", ["matched", "not_found", "ambiguous"])
def test_product_less_commercial_never_become_operating_expense_automatically(expense: str) -> None:
    invoice = _product_less_commercial_invoice()
    expense_matcher = _FakeOperatingExpenseMatcher(_expense_result(expense))
    engine = _DeterministicRuleEngine(
        partner_matcher=_FakeMatcher(_partner_match()),
        product_matcher=_FakeMatcher(_real_product_match(invoice)),
        tax_mapper=_FakeMatcher(_tax_match(invoice)),
        operating_expense_matcher=expense_matcher,
    )

    result = engine.evaluate(_command(invoice))

    _assert_manual_review(result, (AMBIGUOUS, OPEX_REQUIRED))
    assert expense_matcher.calls == []  # a supplier-wide mapping is never even consulted
    assert result.operating_expense_match.status is OperatingExpenseMatchStatus.NOT_FOUND


def test_product_less_commercial_with_unmatched_supplier_resolves_supplier_first() -> None:
    invoice = _product_less_commercial_invoice()
    engine = _engine(
        invoice=invoice,
        partner_match=_partner_match(_status_not_found()),
        product_match=_real_product_match(invoice),
        tax_match=_tax_match(invoice),
    )

    result = engine.evaluate(_command(invoice))

    assert _codes(result) == (AMBIGUOUS, ManualReviewReasonCode.SUPPLIER_NOT_FOUND, OPEX_REQUIRED)
    assert _guidance(_codes(result)).next_action is OperatorNextAction.SUPPLIER


def test_workbench_asks_for_an_explicit_purpose_with_a_product_supplier_warning() -> None:
    guidance = _guidance((AMBIGUOUS, OPEX_REQUIRED))

    assert guidance.next_action is OperatorNextAction.PURPOSE
    assert "ürün tedarikçisi" in guidance.todo_html
    assert "gider otomatik uygulanmaz" in guidance.todo_html
    assert REASON_TITLES[AMBIGUOUS] in guidance.todo_html


def test_workbench_moves_to_accounting_only_after_an_explicit_purpose() -> None:
    guidance = _guidance((AMBIGUOUS, OPEX_REQUIRED), purpose=PurchasePurpose.INTERNAL_USE)

    assert guidance.next_action is OperatorNextAction.ACCOUNTING


def test_workbench_ambiguous_purchase_without_expense_reason_is_technical() -> None:
    assert _guidance((AMBIGUOUS,)).next_action is OperatorNextAction.TECHNICAL


def test_supplier_wide_mapping_never_clears_the_ambiguous_reason() -> None:
    reasons = (_reason(AMBIGUOUS), _reason(OPEX_REQUIRED))

    effective = _effective_manual_review_reasons(reasons, None, _matched_expense_result())

    assert tuple(reason.code for reason in effective) == (AMBIGUOUS,)
    assert _effective_workflow(WorkflowType.MANUAL_REVIEW, effective) is WorkflowType.MANUAL_REVIEW


@pytest.mark.parametrize("resolution", ["expense_account", "fixed_asset"])
def test_explicit_review_scoped_accounting_decision_clears_the_ambiguous_reason(resolution: str) -> None:
    reasons = (_reason(AMBIGUOUS), _reason(OPEX_REQUIRED))

    if resolution == "expense_account":
        effective = _effective_manual_review_reasons(reasons, None, _review_scoped(_matched_expense_result()))
    else:
        effective = _effective_manual_review_reasons(reasons, None, None, fixed_asset_resolved=True)

    assert effective == ()
    assert _effective_workflow(WorkflowType.MANUAL_REVIEW, effective) is WorkflowType.VENDOR_BILL


def test_bill_validation_rejects_supplier_wide_expense_for_product_source_supplier() -> None:
    invoice = _product_source(_builder_invoice())

    result = validate_vendor_bill_inputs(
        invoice,
        _partner(),
        _identifier_free_products(invoice),
        _taxes(invoice),
        operating_expense_match=_expense_match(),
    )

    assert not result.is_valid
    assert any("review-scoped accounting resolution" in error for error in result.errors)


def test_bill_validation_accepts_review_scoped_expense_for_product_source_supplier() -> None:
    invoice = _product_source(_builder_invoice())

    assert validate_vendor_bill_inputs(
        invoice,
        _partner(),
        _identifier_free_products(invoice),
        _taxes(invoice),
        operating_expense_match=_review_scoped(_expense_match()),
    ).is_valid


@pytest.mark.parametrize("shape", ["identifier_free", "mixed"])
def test_bill_validation_rejects_account_only_for_product_source_supplier(shape: str) -> None:
    invoice = _product_source(_mixed_builder_invoice() if shape == "mixed" else _builder_invoice())
    products = _mixed_products(invoice) if shape == "mixed" else _identifier_free_products(invoice)

    result = validate_vendor_bill_inputs(invoice, _partner(), products, _taxes(invoice), **_account_only(invoice))

    assert not result.is_valid
    assert "Account-only line overrides are not allowed for a product-source supplier." in result.errors


def test_mixed_invoice_from_other_supplier_keeps_account_only() -> None:
    invoice = _mixed_builder_invoice()

    assert validate_vendor_bill_inputs(
        invoice, _partner(), _mixed_products(invoice), _taxes(invoice), **_account_only(invoice)
    ).is_valid


def test_mixed_product_source_invoice_still_bills_with_full_product_resolution() -> None:
    invoice = _product_source(_mixed_builder_invoice())

    assert validate_vendor_bill_inputs(invoice, _partner(), _matched_products(invoice), _taxes(invoice)).is_valid


def test_product_less_commercial_get_stage_one_evidence_only_from_a_review_scoped_resolution() -> None:
    invoice = _product_source(_builder_invoice())
    products = _identifier_free_products(invoice)

    assert _evidence(invoice, _decision_result(invoice, products, expense=_expense_match())) is None
    review_scoped = _review_scoped(_expense_match())
    evidence = _evidence(invoice, _decision_result(invoice, products, expense=review_scoped))
    assert evidence is not None
    assert evidence.operating_expense_match == review_scoped


# ===================================================================== end to end (real use cases + SQLite)


@pytest.mark.asyncio
async def test_authorized_genuine_expense_from_product_source_end_to_end(session: Session) -> None:
    review_id, facts = await _import_product_source(session, ettn="PR-S-E2E-1", with_supplier_wide_mapping=True)

    # Automatic: even with an enabled supplier-wide mapping the review stays blocked.
    assert _stored_codes(session, review_id) == [AMBIGUOUS, OPEX_REQUIRED]
    assert session.scalars(select(WorkbenchReviewExecutionEvidence)).all() == []
    with pytest.raises(ApplicationError):
        _fa_decide(session, review_id, version=1)
    session.rollback()
    # RESALE needs a product-shaped invoice; it can never be recorded here.
    with pytest.raises(PurchasePurposeEligibilityError):
        _submit_purpose(session, review_id, PurchasePurpose.RESALE)
    session.rollback()

    # Explicit, authorized accounting decision: purpose, then review-scoped expense account.
    _submit_purpose(session, review_id, PurchasePurpose.INTERNAL_USE)
    result = await _fa_accounting_use_case(session, facts).execute(_expense_command(review_id))
    session.commit()

    assert result.current_review_reasons == ()
    assert result.current_workflow is WorkflowType.VENDOR_BILL
    _fa_decide(session, review_id)
    bill = _build_from_stage_two(session, review_id)
    assert {line.account_id for line in bill.invoice_lines} == {FA_EXPENSE_ACCOUNT}
    assert all(line.product_id is None for line in bill.invoice_lines)


@pytest.mark.asyncio
async def test_api_account_only_override_cannot_bypass_mixed_commercial_invoice(session: Session) -> None:
    review_id, _facts = await _import_product_source(session, ettn="PR-S-MIX-1", mixed=True)
    assert set(_stored_codes(session, review_id)) == {
        ManualReviewReasonCode.PRODUCT_NOT_FOUND,
        ManualReviewReasonCode.PRODUCT_IDENTIFIER_MISSING,
    }
    assert session.scalars(select(WorkbenchReviewExecutionEvidence)).all() != []  # mixed Stage-1 pinned

    with pytest.raises(ReviewDecisionError):
        _decide_account_only(session, review_id)


@pytest.mark.asyncio
async def test_account_only_override_unchanged_for_other_suppliers(session: Session) -> None:
    review_id, _facts = await _import_product_source(session, ettn="PR-S-MIX-2", mixed=True, vkn=OTHER_VKN)

    _decide_account_only(session, review_id)

    assert session.scalars(select(ExecutionSourceInvoiceEvidence)).all() != []


# ===================================================================== previously accepted decisions


def test_execution_rejects_previously_accepted_iade_decision() -> None:
    invoice = _typed(_builder_invoice(), "IADE")

    with pytest.raises(VendorBillBuildError):
        VendorBillBuilder().build(invoice, _partner(), _matched_products(invoice), _taxes(invoice), company_id=1)


def test_execution_rejects_previously_accepted_product_source_account_only_decision() -> None:
    invoice = _product_source(_mixed_builder_invoice())

    with pytest.raises(VendorBillBuildError):
        VendorBillBuilder().build(
            invoice, _partner(), _mixed_products(invoice), _taxes(invoice), company_id=1, **_account_only(invoice)
        )


def test_execution_rejects_previously_pinned_supplier_wide_expense_for_product_source() -> None:
    invoice = _product_source(_builder_invoice())

    with pytest.raises(VendorBillBuildError):
        VendorBillBuilder().build(
            invoice,
            _partner(),
            _identifier_free_products(invoice),
            _taxes(invoice),
            company_id=1,
            operating_expense_match=_expense_match(),
        )


def test_execution_rejects_iade_with_previously_pinned_resale_accounts() -> None:
    invoice = _typed(_builder_invoice(), "IADE")

    result = validate_vendor_bill_inputs(
        invoice, _partner(), _matched_products(invoice), _taxes(invoice), validated_resale_accounts={}
    )

    assert not result.is_valid


# ===================================================================== rollback compatibility


#: ``ManualReviewReasonCode`` exactly as released in eee18a2 (the previous production image).
_EEE18A2_REASON_CODES = (
    "SUPPLIER_TAX_NUMBER_MISSING",
    "SUPPLIER_NOT_FOUND",
    "SUPPLIER_AMBIGUOUS",
    "PRODUCT_IDENTIFIER_MISSING",
    "PRODUCT_NOT_FOUND",
    "PRODUCT_AMBIGUOUS",
    "PRODUCT_MAPPING_INCOMPLETE",
    "TAX_NOT_FOUND",
    "TAX_AMBIGUOUS",
    "TAX_MAPPING_INCOMPLETE",
    "OPERATING_EXPENSE_MAPPING_REQUIRED",
    "OPERATING_EXPENSE_MAPPING_AMBIGUOUS",
    "UNSUPPORTED_INVOICE_CONTENT",
)


def test_reason_codes_are_append_only_relative_to_the_previous_image() -> None:
    values = tuple(code.value for code in ManualReviewReasonCode)

    assert values[: len(_EEE18A2_REASON_CODES)] == _EEE18A2_REASON_CODES
    assert values[len(_EEE18A2_REASON_CODES) :] == (RETURN.value, AMBIGUOUS.value)


def test_previous_image_cannot_parse_new_codes_so_rollback_needs_the_enum_only_compat_build() -> None:
    previous_image_enum = StrEnum("PreviousReasonCode", {value: value for value in _EEE18A2_REASON_CODES})

    for code in (RETURN, AMBIGUOUS):
        with pytest.raises(ValueError):
            previous_image_enum(code.value)
    # The compat build = previous image + exactly these two members; nothing else changed.
    compat = StrEnum("CompatReasonCode", {v: v for v in (*_EEE18A2_REASON_CODES, RETURN.value, AMBIGUOUS.value)})
    assert {code.value for code in ManualReviewReasonCode} == {code.value for code in compat}


# ===================================================================== unchanged behavior


def test_normal_purchase_invoice_unchanged() -> None:
    invoice = _typed(_invoice(lines=(_line(seller_item_code="S-1"),)), "SATIS")
    engine = _engine(invoice=invoice, product_match=_matched_product_match(invoice))

    result = engine.evaluate(_command(invoice))

    assert result.workflow is WorkflowType.VENDOR_BILL
    assert result.matched_rule == DIRECT_VENDOR_BILL_RULE_ID
    builder_invoice = _typed(_builder_invoice(), "SATIS")
    assert validate_vendor_bill_inputs(
        builder_invoice, _partner(), _matched_products(builder_invoice), _taxes(builder_invoice)
    ).is_valid


def test_product_less_genuine_expense_with_mapping_unchanged() -> None:
    invoice = _invoice()
    assert not invoice_is_ambiguous_commercial_purchase(invoice)
    engine = _engine(
        invoice=invoice,
        product_match=_identifier_free_product_match(invoice),
        operating_expense_match=_matched_expense_result(),
    )

    result = engine.evaluate(_command(invoice))

    assert result.workflow is WorkflowType.VENDOR_BILL
    assert result.matched_rule == OPERATING_EXPENSE_VENDOR_BILL_RULE_ID
    builder_invoice = _builder_invoice()
    assert validate_vendor_bill_inputs(
        builder_invoice,
        _partner(),
        _identifier_free_products(builder_invoice),
        _taxes(builder_invoice),
        operating_expense_match=_expense_match(),
    ).is_valid


def test_product_less_genuine_expense_without_mapping_unchanged() -> None:
    invoice = _invoice()
    engine = _engine(invoice=invoice, product_match=_identifier_free_product_match(invoice))

    _assert_manual_review(
        engine.evaluate(_command(invoice)), (ManualReviewReasonCode.OPERATING_EXPENSE_MAPPING_REQUIRED,)
    )


def test_guard_is_vkn_keyed_not_name_keyed() -> None:
    named_like_source = _from_supplier(_product_less_commercial_invoice(), OTHER_VKN, name="Product Source Supplier")
    assert not invoice_is_ambiguous_commercial_purchase(named_like_source)
    engine = _engine(
        invoice=named_like_source,
        product_match=_identifier_free_product_match(named_like_source),
        tax_match=_tax_match(named_like_source),
        operating_expense_match=_matched_expense_result(),
    )

    assert engine.evaluate(_command(named_like_source)).matched_rule == OPERATING_EXPENSE_VENDOR_BILL_RULE_ID


def test_product_source_invoice_with_identifier_keeps_product_mapping() -> None:
    item = (
        "<cbc:Name>Licence</cbc:Name>"
        "<cac:ManufacturersItemIdentification><cbc:ID>MFR-0001</cbc:ID></cac:ManufacturersItemIdentification>"
    )
    invoice = parse_ubl_invoice(_ubl(invoice_type="SATIS", item_xml=item))
    assert not invoice_is_ambiguous_commercial_purchase(invoice)
    repository = RecordingProductRepository({"MFR-0001": [_product(395, "MFR-0001")]})

    product_match = ProductMatchingEngine(FakeProvider(repository)).match_invoice(invoice)
    result = _engine(invoice=invoice, product_match=product_match, tax_match=_tax_match(invoice)).evaluate(
        _command(invoice)
    )

    assert product_match.line_results[0].result.product_id == 395
    assert result.workflow is WorkflowType.VENDOR_BILL
    assert result.matched_rule == DIRECT_VENDOR_BILL_RULE_ID


def test_product_source_mixed_invoice_keeps_existing_product_reasons() -> None:
    invoice = _from_supplier(_invoice(lines=(_line(seller_item_code="SELLER-0001"),)), PRODUCT_SOURCE_VKN)
    engine = _engine(invoice=invoice, product_match=_status_products_not_found(invoice))

    _assert_manual_review(engine.evaluate(_command(invoice)), (ManualReviewReasonCode.PRODUCT_NOT_FOUND,))


def test_supplier_matching_inputs_unchanged() -> None:
    invoice = _product_less_commercial_invoice()
    partner = _partner_match()
    engine = _engine(invoice=invoice, partner_match=partner, product_match=_real_product_match(invoice))

    result = engine.evaluate(_command(invoice))

    assert result.partner_match is partner


def test_fixed_asset_behavior_unchanged() -> None:
    invoice = _two_line_invoice(ettn="PR-S-FA-2")
    source = _fixed_asset_source(invoice)

    assert not invoice_is_ambiguous_commercial_purchase(invoice)
    assert validate_vendor_bill_inputs(
        invoice,
        source.partner_match,
        source.product_match,
        source.tax_match,
        fixed_asset_accounting=source.fixed_asset_accounting,
    ).is_valid


def test_every_reason_code_has_a_workbench_title() -> None:
    assert set(REASON_TITLES) == set(ManualReviewReasonCode)


# ===================================================================== helpers


class RecordingProductRepository:
    """Read-only product lookups by exact default_code / barcode (synthetic records)."""

    def __init__(self, default_code_records: dict[str, list[Product]] | None = None) -> None:
        self.default_code_records = default_code_records or {}

    def find_by_default_code(self, default_code: str, *, company_id: int | None = None) -> tuple[Product, ...]:
        return tuple(self.default_code_records.get(default_code, ()))

    def find_by_barcode(self, barcode: str, *, company_id: int | None = None) -> tuple[Product, ...]:
        return ()

    def find_by_ids(self, ids) -> tuple[Product, ...]:
        return ()


class FakeProvider:
    def __init__(self, product_repository: RecordingProductRepository) -> None:
        self.product_repository = product_repository


def _product(product_id: int, default_code: str) -> Product:
    return Product(id=product_id, name=f"P{product_id}", default_code=default_code, barcode=None, active=True)


def _ubl(*, invoice_type: str, item_xml: str, currency: str = "TRY") -> bytes:
    return f"""<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
         xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
         xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
  <cbc:ProfileID>TICARIFATURA</cbc:ProfileID>
  <cbc:ID>SYN2026PRS0001</cbc:ID><cbc:UUID>5a5a0000-0000-4000-8000-000000000024</cbc:UUID>
  <cbc:IssueDate>2026-09-15</cbc:IssueDate>
  <cbc:InvoiceTypeCode>{invoice_type}</cbc:InvoiceTypeCode>
  <cbc:DocumentCurrencyCode>{currency}</cbc:DocumentCurrencyCode>
  <cac:AccountingSupplierParty><cac:Party>
    <cac:PartyIdentification><cbc:ID schemeID="VKN">{PRODUCT_SOURCE_VKN}</cbc:ID></cac:PartyIdentification>
    <cac:PartyName><cbc:Name>PRODUCT SOURCE TEST SUPPLIER</cbc:Name></cac:PartyName>
  </cac:Party></cac:AccountingSupplierParty>
  <cac:InvoiceLine>
    <cbc:ID>1</cbc:ID>
    <cbc:InvoicedQuantity unitCode="C62">1</cbc:InvoicedQuantity>
    <cbc:LineExtensionAmount currencyID="{currency}">100.00</cbc:LineExtensionAmount>
    <cac:TaxTotal><cac:TaxSubtotal><cbc:Percent>20</cbc:Percent>
      <cac:TaxCategory><cac:TaxScheme><cbc:Name>KDV</cbc:Name></cac:TaxScheme></cac:TaxCategory>
    </cac:TaxSubtotal></cac:TaxTotal>
    <cac:Item>{item_xml}</cac:Item>
    <cac:Price><cbc:PriceAmount currencyID="{currency}">100.00</cbc:PriceAmount></cac:Price>
  </cac:InvoiceLine>
</Invoice>""".encode()


def _product_less_commercial_invoice() -> InternalInvoice:
    """Product-source supplier, foreign currency, one line described only by prose, no identifiers."""

    return parse_ubl_invoice(
        _ubl(invoice_type="SATIS", item_xml="<cbc:Description>Software subscription</cbc:Description>", currency="USD")
    )


def _product_source(invoice: InternalInvoice) -> InternalInvoice:
    return _from_supplier(invoice, PRODUCT_SOURCE_VKN, name="Product Source Supplier")


def _review_scoped(match: OperatingExpenseMatchResult) -> OperatingExpenseMatchResult:
    return replace(match, matched_by=MATCHED_BY_REVIEW_ACCOUNTING_RESOLUTION)


def _mixed_builder_invoice() -> InternalInvoice:
    """Line 1 carries a seller item code (no Odoo product); line 2 has no identifier."""

    return _builder_invoice([_builder_line("1", seller_item_code="SELLER-0001"), _builder_line("2")])


def _mixed_products(invoice: InternalInvoice):
    statuses = (ProductMatchStatus.NOT_FOUND, ProductMatchStatus.INVALID_INPUT)
    return InvoiceProductMatchResult(
        line_results=tuple(
            _builder_product_line(line.line_number, status, product_id=None)
            for line, status in zip(invoice.lines, statuses, strict=True)
        )
    )


def _account_only(invoice: InternalInvoice) -> dict:
    lines = frozenset(line.line_number for line in invoice.lines)
    return {"account_only_line_numbers": lines, "explicit_account_only_accounts": dict.fromkeys(lines, 9001)}


async def _import_product_source(
    session: Session,
    *,
    ettn: str,
    mixed: bool = False,
    with_supplier_wide_mapping: bool = False,
    vkn: str = PRODUCT_SOURCE_VKN,
) -> tuple[str, _FAFacts]:
    invoice = replace(_two_line_invoice(ettn=ettn), supplier=Party(name="Product Source Supplier", tax_number=vkn))
    if mixed:
        first, second = invoice.lines
        invoice = replace(invoice, lines=(replace(first, seller_item_code="SELLER-0001"), second))
        product_lines = _fa_products_invalid_input(invoice).line_results
        products = InvoiceProductMatchResult(
            line_results=(
                replace(
                    product_lines[0],
                    result=replace(product_lines[0].result, status=ProductMatchStatus.NOT_FOUND, reason="not found"),
                ),
                product_lines[1],
            )
        )
    else:
        products = _fa_products_invalid_input(invoice)
    facts = _FAFacts(_fa_partner_matched(), products, _fa_taxes(invoice))
    if with_supplier_wide_mapping:
        session.add(
            OperatingExpenseMappingRecord(
                company_id=1,
                vendor_partner_id=FA_PARTNER_ID,
                expense_account_id=FA_EXPENSE_ACCOUNT,
                expense_category="SUPPLIER_WIDE",
                enabled=True,
            )
        )
        session.flush()
    result = await ImportInvoiceUseCase(
        import_history=_FAFakeImportHistory(),
        decision_engine=_fa_decision_engine(facts, SqlAlchemyOperatingExpenseMappingRepository(session)),
        review_item_creation_service=ReviewItemCreationService(SqlAlchemyReviewRepository(session)),
        unit_of_work=SqlAlchemyUnitOfWork(session),
    ).execute(ImportInvoiceCommand(invoice=invoice, idempotency_key=f"uyumsoft:1:{ettn}", company_id=1))
    session.commit()
    assert result.status == "review_required"
    return result.review_id, facts


def _stored_codes(session: Session, review_id: str) -> list[ManualReviewReasonCode]:
    item = session.scalar(select(WorkbenchReviewItem).where(WorkbenchReviewItem.review_id == review_id))
    return [ManualReviewReasonCode(reason["code"]) for reason in item.review_reasons]


def _submit_purpose(session: Session, review_id: str, purpose: PurchasePurpose) -> None:
    SubmitPurchasePurposeUseCase(
        review_reader=SqlAlchemyReviewRepository(session),
        source_invoice_reader=SqlAlchemyReviewSourceInvoiceEvidenceReader(session),
        purpose_writer=SqlAlchemyReviewPurchasePurposeResolutionRepository(session),
        unit_of_work=SqlAlchemyUnitOfWork(session),
    ).execute(
        SubmitPurchasePurposeCommand(
            review_id=review_id,
            company_id=1,
            expected_version=1,
            purchase_purpose=purpose,
            approved_by=FA_ACTOR,
        )
    )


def _expense_command(review_id: str) -> SubmitReviewAccountingResolutionCommand:
    return SubmitReviewAccountingResolutionCommand(
        review_id=review_id,
        company_id=1,
        expected_version=1,
        treatment_type=AccountingTreatmentType.EXPENSE_ACCOUNT,
        expense_account_id=FA_EXPENSE_ACCOUNT,
        expense_category="SOFTWARE_INTERNAL_USE",
        approved_by=FA_ACTOR,
    )


class _Accounts:
    def find_accounts_by_ids(self, account_ids: tuple[int, ...]) -> tuple[ResolutionAccountRecord, ...]:
        return tuple(ResolutionAccountRecord(id=account_id, company_ids=(1,)) for account_id in account_ids)


def _decide_account_only(session: Session, review_id: str) -> None:
    """An API Vendor Bill decision with explicit per-line account-only overrides (P0-PROD-08G)."""

    SubmitReviewDecisionUseCase(
        review_decision_writer=SqlAlchemyReviewRepository(session),
        unit_of_work=SqlAlchemyUnitOfWork(session),
        execution_evidence_reader=SqlAlchemyReviewExecutionEvidenceReader(session),
        selected_account_reader=_Accounts(),
    ).execute(
        ReviewDecisionCommand(
            review_id=review_id,
            company_id=1,
            expected_version=1,
            decision=ReviewDecisionType.SELECT_WORKFLOW,
            decided_by=FA_ACTOR,
            selected_workflow=WorkflowType.VENDOR_BILL,
            idempotency_key=f"pr-s-account-only-{review_id}",
            line_resolutions=tuple(
                LineResolution(line_number=number, account_only=True, expense_account_id=FA_EXPENSE_ACCOUNT)
                for number in ("000001", "000002")
            ),
        )
    )
    session.commit()


def _build_from_stage_two(session: Session, review_id: str):
    stage_two = session.scalar(select(ExecutionSourceInvoiceEvidence))
    source = SqlAlchemyExecutionSourceInvoiceReader(session).get_source_invoice(
        review_id=review_id, company_id=1, decision_version=stage_two.decision_version
    )
    assert source.operating_expense_match.matched_by == MATCHED_BY_REVIEW_ACCOUNTING_RESOLUTION
    return VendorBillBuilder().build(
        source.invoice,
        source.partner_match,
        source.product_match,
        source.tax_match,
        company_id=1,
        operating_expense_match=source.operating_expense_match,
    )


def _real_product_match(invoice: InternalInvoice):
    return ProductMatchingEngine(FakeProvider(RecordingProductRepository())).match_invoice(invoice)


def _status_not_found() -> PartnerMatchStatus:
    return PartnerMatchStatus.NOT_FOUND


def _status_products_not_found(invoice: InternalInvoice):
    return _status_products(invoice, ProductMatchStatus.NOT_FOUND)


def _expense_result(kind: str) -> OperatingExpenseMatchResult:
    if kind == "matched":
        return _matched_expense_result()
    if kind == "ambiguous":
        return OperatingExpenseMatchResult(
            status=OperatingExpenseMatchStatus.MULTIPLE_MATCHES, reason="two", candidate_count=2
        )
    return OperatingExpenseMatchResult(status=OperatingExpenseMatchStatus.NOT_FOUND, reason="none")


def _mode_inputs(mode: str, invoice: InternalInvoice):
    if mode == "product":
        return {}, _matched_products(invoice)
    if mode == "expense":
        return {"operating_expense_match": _expense_match()}, _identifier_free_products(invoice)
    lines = frozenset(line.line_number for line in invoice.lines)
    return (
        {
            "account_only_line_numbers": lines,
            "explicit_account_only_accounts": dict.fromkeys(lines, 9001),
        },
        _identifier_free_products(invoice),
    )


def _decision_result(invoice: InternalInvoice, products, *, expense=None) -> DecisionResult:
    return DecisionResult(
        success=False,
        invoice_id=invoice.header.ettn,
        workflow=WorkflowType.MANUAL_REVIEW,
        strategy=WorkflowType.MANUAL_REVIEW.value,
        status="review_required",
        review_required=True,
        partner_match=_partner(),
        product_match=products,
        tax_match=_taxes(invoice),
        operating_expense_match=expense,
    )


def _evidence(invoice: InternalInvoice, decision):
    return build_review_execution_evidence(
        review_id="review-pr-s", company_id=1, review_version=1, invoice=invoice, decision_result=decision
    )


def _reason(code: ManualReviewReasonCode) -> ManualReviewReason:
    return ManualReviewReason(code=code, message=code.value)


def _guidance(codes: tuple[ManualReviewReasonCode, ...], *, purpose: PurchasePurpose | None = None):
    return build_operator_guidance(
        GuidanceInput(
            status=ReviewStatus.PENDING_REVIEW,
            reason_codes=codes,
            supplier_name="Supplier",
            decision_type=None,
            decision_workflow=None,
            decision_version=None,
            execution_state=None,
            vendor_bill_id=None,
        ),
        OperatorGuidanceFacts(product_mapping_enabled=True, current_purchase_purpose=purpose),
    )


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    db = factory()
    try:
        yield db
    finally:
        db.close()
