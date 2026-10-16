"""PR T W0: rollback compatibility for the withholding-tax guard's reason codes.

This image only *understands* WITHHOLDING_TAX_NOT_SUPPORTED and SOURCE_DOCUMENT_UNVERIFIED:
a review persisted by the guard (W1) must still load, list, render in the Workbench and stay
blocked. Nothing here emits them, detects withholding, parses UBL differently or changes tax
mapping; the only behavior is that a review carrying either code is never executable.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import date
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.application.commands import ImportInvoiceCommand
from app.application.dto import DecisionResult
from app.application.use_cases import ImportInvoiceUseCase
from app.application.use_cases.effective_decision import _effective_manual_review_reasons, _effective_workflow
from app.application.use_cases.review_classification_outcome import build_review_execution_evidence
from app.application.workbench import ReviewItemCreationService, ReviewSourceInvoiceEvidence
from app.application.workbench.dto import ReviewStatus
from app.application.workbench.operator_guidance import (
    REASON_TITLES,
    GuidanceInput,
    OperatorGuidanceFacts,
    OperatorNextAction,
    build_operator_guidance,
)
from app.application.workbench.queries import ReviewDetailQuery, ReviewQueueQuery
from app.application.workflow import (
    SOURCE_TAX_SAFETY_REASON_CODES,
    ManualReviewReason,
    ManualReviewReasonCode,
    WorkflowType,
)
from app.db.base import Base
from app.domain.invoice import Header, InternalInvoice, InvoiceLine, MonetaryTotals, Party, Tax
from app.persistence import SqlAlchemyImportHistory, SqlAlchemyReviewRepository
from app.persistence.workbench_review_source_invoice_reader import review_source_invoice_evidence_fingerprint
from tests.unit.test_operating_expense_vendor_bill_builder import (
    _expense_match,
    _identifier_free_products,
    _matched_products,
    _partner,
    _taxes,
)
from tests.unit.test_operating_expense_vendor_bill_builder import _invoice as _builder_invoice
from tests.unit.test_operating_expense_vendor_bill_builder import _line as _builder_line

WITHHOLDING = ManualReviewReasonCode.WITHHOLDING_TAX_NOT_SUPPORTED
UNVERIFIED = ManualReviewReasonCode.SOURCE_DOCUMENT_UNVERIFIED
NEW_CODES = (WITHHOLDING, UNVERIFIED)

#: ``ManualReviewReasonCode`` exactly as released in 809c7f5 (the W0 baseline).
_809C7F5_REASON_CODES = (
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
    "RETURN_INVOICE_NOT_SUPPORTED",
    "AMBIGUOUS_COMMERCIAL_PURCHASE",
)

#: Titles of the 809c7f5 codes, unchanged by W0.
_809C7F5_TITLES = {
    "SUPPLIER_TAX_NUMBER_MISSING": "Faturada tedarikçi vergi numarası yok",
    "SUPPLIER_NOT_FOUND": "Tedarikçi Odoo'da bulunamadı",
    "SUPPLIER_AMBIGUOUS": "Tedarikçi eşleşmesi doğrulanmalı",
    "PRODUCT_IDENTIFIER_MISSING": "Fatura satırında ürün kodu yok",
    "PRODUCT_NOT_FOUND": "Ürün Odoo'da bulunamadı",
    "PRODUCT_AMBIGUOUS": "Ürün eşleşmesi doğrulanmalı",
    "PRODUCT_MAPPING_INCOMPLETE": "Ürün eşleşmesi eksik",
    "TAX_NOT_FOUND": "Vergi Odoo'da bulunamadı",
    "TAX_AMBIGUOUS": "Vergi eşleşmesi doğrulanmalı",
    "TAX_MAPPING_INCOMPLETE": "Vergi eşleşmesi eksik",
    "OPERATING_EXPENSE_MAPPING_REQUIRED": "Muhasebe işlemi seçilmeli",
    "OPERATING_EXPENSE_MAPPING_AMBIGUOUS": "Muhasebe işlemi netleştirilmeli",
    "UNSUPPORTED_INVOICE_CONTENT": "Fatura içeriği desteklenmiyor",
    "RETURN_INVOICE_NOT_SUPPORTED": "İade faturası — tedarikçi faturası oluşturulmaz",
    "AMBIGUOUS_COMMERCIAL_PURCHASE": "Ürün tedarikçisi faturasında ürün kodu yok — otomatik gider uygulanmaz",
}

#: sha256 of ``repr(review_source_invoice_evidence_fingerprint(...))`` for ``_fingerprint_evidence()``,
#: computed on 809c7f5. W0 must not change how any source evidence is serialized or compared.
_809C7F5_SOURCE_EVIDENCE_FINGERPRINT_SHA256 = "930ff0e6fe4132b2f1f141dac173cabe75166ce44a8d2db61cdabefcc64a04e6"


# ===================================================================== enum and titles


def test_reason_codes_are_append_only() -> None:
    values = tuple(code.value for code in ManualReviewReasonCode)

    assert values[: len(_809C7F5_REASON_CODES)] == _809C7F5_REASON_CODES
    assert values[len(_809C7F5_REASON_CODES) :] == ("WITHHOLDING_TAX_NOT_SUPPORTED", "SOURCE_DOCUMENT_UNVERIFIED")


def test_previous_image_cannot_parse_new_codes_so_rollback_needs_this_compat_build() -> None:
    previous_image_enum = StrEnum("PreviousReasonCode", {value: value for value in _809C7F5_REASON_CODES})

    for code in NEW_CODES:
        with pytest.raises(ValueError):
            previous_image_enum(code.value)


def test_hard_block_set_is_exactly_the_new_codes() -> None:
    assert SOURCE_TAX_SAFETY_REASON_CODES == frozenset(NEW_CODES)


def test_every_reason_code_has_a_workbench_title() -> None:
    assert set(REASON_TITLES) == set(ManualReviewReasonCode)
    assert all(REASON_TITLES[code].strip() for code in NEW_CODES)


def test_existing_titles_are_unchanged() -> None:
    assert {code.value: title for code, title in REASON_TITLES.items() if code not in NEW_CODES} == _809C7F5_TITLES


def test_new_codes_are_referenced_only_by_their_owning_modules() -> None:
    """Definition and display (W0) plus the Stage-1 evidence gate; no module emits them yet."""

    allowed = {
        Path("app/application/workflow.py"),
        Path("app/application/workbench/operator_guidance.py"),
    }
    for path in Path("app").rglob("*.py"):
        if path in allowed:
            continue
        source = path.read_text()
        for code in NEW_CODES:
            assert code.value not in source, f"{path} references {code.value}"
            assert code.name not in source, f"{path} references {code.name}"


# ===================================================================== persistence and reads


@pytest.mark.asyncio
@pytest.mark.parametrize("code", NEW_CODES)
async def test_review_persisted_with_new_code_loads_and_lists(session: Session, code: ManualReviewReasonCode) -> None:
    repository = SqlAlchemyReviewRepository(session)
    result = await ImportInvoiceUseCase(
        import_history=SqlAlchemyImportHistory(session),
        decision_engine=_ReviewRequired(code),
        review_item_creation_service=ReviewItemCreationService(repository),
    ).execute(
        ImportInvoiceCommand(
            invoice=_invoice(), idempotency_key=f"uyumsoft:company:1:inbox:ettn:{code.value}", company_id=1
        )
    )

    detail = repository.get_review_item(ReviewDetailQuery(review_id=result.review_id, company_id=1))
    queue = repository.list_review_items(ReviewQueueQuery(company_id=1))

    assert detail.workflow is WorkflowType.MANUAL_REVIEW
    assert tuple(reason.code for reason in detail.review_reasons) == (code, ManualReviewReasonCode.SUPPLIER_NOT_FOUND)
    assert [item.review_id for item in queue.items] == [result.review_id]
    assert queue.items[0].review_reasons[0].code is code


# ===================================================================== Workbench guidance


@pytest.mark.parametrize("code", NEW_CODES)
def test_workbench_shows_new_code_as_technical_blocker(code: ManualReviewReasonCode) -> None:
    guidance = _guidance((code,))

    assert guidance.next_action is OperatorNextAction.TECHNICAL
    assert REASON_TITLES[code] in guidance.todo_html


@pytest.mark.parametrize("code", NEW_CODES)
@pytest.mark.parametrize(
    "other",
    [
        ManualReviewReasonCode.SUPPLIER_NOT_FOUND,
        ManualReviewReasonCode.SUPPLIER_AMBIGUOUS,
        ManualReviewReasonCode.PRODUCT_NOT_FOUND,
        ManualReviewReasonCode.OPERATING_EXPENSE_MAPPING_REQUIRED,
        ManualReviewReasonCode.TAX_AMBIGUOUS,
    ],
)
def test_new_code_outranks_supplier_product_and_purpose_steps(
    code: ManualReviewReasonCode, other: ManualReviewReasonCode
) -> None:
    """No supplier/product/purpose step is offered: none could make the invoice executable."""

    guidance = _guidance((other, code))

    assert guidance.next_action is OperatorNextAction.TECHNICAL
    assert REASON_TITLES[code] in guidance.todo_html
    assert REASON_TITLES[other] in guidance.todo_html


@pytest.mark.parametrize("code", NEW_CODES)
def test_return_invoice_guidance_keeps_precedence(code: ManualReviewReasonCode) -> None:
    return_only = _guidance((ManualReviewReasonCode.RETURN_INVOICE_NOT_SUPPORTED,))
    combined = _guidance((code, ManualReviewReasonCode.RETURN_INVOICE_NOT_SUPPORTED))

    assert combined.next_action is OperatorNextAction.TECHNICAL
    assert "iade faturasıdır" in combined.todo_html
    assert "iade faturasıdır" in return_only.todo_html


@pytest.mark.parametrize(
    ("codes", "action"),
    [
        ((ManualReviewReasonCode.SUPPLIER_NOT_FOUND,), OperatorNextAction.SUPPLIER),
        ((ManualReviewReasonCode.SUPPLIER_AMBIGUOUS,), OperatorNextAction.SUPPLIER),
        ((ManualReviewReasonCode.OPERATING_EXPENSE_MAPPING_REQUIRED,), OperatorNextAction.PURPOSE),
        ((ManualReviewReasonCode.TAX_AMBIGUOUS,), OperatorNextAction.TECHNICAL),
        ((ManualReviewReasonCode.UNSUPPORTED_INVOICE_CONTENT,), OperatorNextAction.TECHNICAL),
        ((ManualReviewReasonCode.RETURN_INVOICE_NOT_SUPPORTED,), OperatorNextAction.TECHNICAL),
        ((), OperatorNextAction.DECISION),
    ],
)
def test_existing_code_guidance_is_unchanged(
    codes: tuple[ManualReviewReasonCode, ...], action: OperatorNextAction
) -> None:
    assert _guidance(codes).next_action is action


# ===================================================================== blocking


@pytest.mark.parametrize("code", NEW_CODES)
def test_new_code_is_never_stripped_so_review_stays_manual(code: ManualReviewReasonCode) -> None:
    reasons = (
        ManualReviewReason(code=code, message=code.value),
        ManualReviewReason(code=ManualReviewReasonCode.SUPPLIER_AMBIGUOUS, message="ambiguous"),
        ManualReviewReason(code=ManualReviewReasonCode.OPERATING_EXPENSE_MAPPING_REQUIRED, message="opex"),
        ManualReviewReason(code=ManualReviewReasonCode.AMBIGUOUS_COMMERCIAL_PURCHASE, message="commercial"),
    )

    # Every strip trigger at once: fixed-asset resolution strips the operating-expense and
    # commercial-purchase codes, but never a source-tax hard block.
    effective = _effective_manual_review_reasons(reasons, None, None, fixed_asset_resolved=True)

    assert reasons[0] in effective
    assert _effective_workflow(WorkflowType.MANUAL_REVIEW, effective) is WorkflowType.MANUAL_REVIEW


@pytest.mark.parametrize("code", NEW_CODES)
@pytest.mark.parametrize("mode", ["product", "expense"])
def test_new_code_gets_no_stage1_execution_evidence(code: ManualReviewReasonCode, mode: str) -> None:
    """Fully matched facts that would otherwise be pinned: the hard block alone withholds them."""

    invoice, decision = _executable(mode)
    assert _evidence(invoice, decision) is not None  # control: executable without the code

    blocked = replace(decision, review_reasons=(ManualReviewReason(code=code, message=code.value),))

    assert _evidence(invoice, blocked) is None


@pytest.mark.parametrize(
    "code",
    [code for code in ManualReviewReasonCode if code not in NEW_CODES and code.value != "RETURN_INVOICE_NOT_SUPPORTED"],
)
def test_existing_reason_codes_do_not_change_stage1_evidence(code: ManualReviewReasonCode) -> None:
    """The evidence gate reads only the two new codes; every other reason behaves as before."""

    invoice, decision = _executable("product")
    with_reason = replace(decision, review_reasons=(ManualReviewReason(code=code, message=code.value),))

    assert _evidence(invoice, with_reason) == _evidence(invoice, decision)


# ===================================================================== source evidence


def test_source_evidence_fingerprint_is_unchanged() -> None:
    fingerprint = review_source_invoice_evidence_fingerprint(_fingerprint_evidence())

    assert hashlib.sha256(repr(fingerprint).encode()).hexdigest() == _809C7F5_SOURCE_EVIDENCE_FINGERPRINT_SHA256


# ===================================================================== helpers


class _ReviewRequired:
    def __init__(self, code: ManualReviewReasonCode) -> None:
        self._code = code

    async def decide(self, command: ImportInvoiceCommand) -> DecisionResult:
        return DecisionResult(
            success=False,
            invoice_id=command.invoice.header.ettn,
            workflow=WorkflowType.MANUAL_REVIEW,
            strategy=WorkflowType.MANUAL_REVIEW.value,
            status="review_required",
            review_required=True,
            review_reasons=(
                ManualReviewReason(code=self._code, message="Persisted by W1.", source="source_tax_facts"),
                ManualReviewReason(code=ManualReviewReasonCode.SUPPLIER_NOT_FOUND, message="Supplier not found."),
            ),
        )


def _guidance(codes: tuple[ManualReviewReasonCode, ...]):
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
        OperatorGuidanceFacts(),
    )


def _executable(mode: str) -> tuple[InternalInvoice, DecisionResult]:
    if mode == "product":
        invoice = _builder_invoice([_builder_line("1", seller_item_code="S-1")])
        return invoice, _decision_result(invoice, _matched_products(invoice))
    invoice = _builder_invoice()
    return invoice, _decision_result(invoice, _identifier_free_products(invoice), expense=_expense_match())


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


def _evidence(invoice: InternalInvoice, decision: DecisionResult):
    return build_review_execution_evidence(
        review_id="review-pr-t-w0", company_id=1, review_version=1, invoice=invoice, decision_result=decision
    )


def _invoice() -> InternalInvoice:
    return InternalInvoice(
        header=Header(
            invoice_number="INV-W0",
            invoice_uuid="INV-W0-UUID",
            ettn="INV-W0-ETTN",
            issue_date=date(2026, 10, 16),
            currency_code="TRY",
            invoice_type="TEVKIFAT",
        ),
        supplier=Party(name="Supplier", tax_number="9000000001"),
        customer=Party(name="Customer", tax_number="9000000002"),
        totals=MonetaryTotals(payable_amount=Decimal("120")),
        lines=(InvoiceLine(line_number="1", description="Line"),),
    )


def _fingerprint_evidence() -> ReviewSourceInvoiceEvidence:
    invoice = InternalInvoice(
        header=Header(
            invoice_number="INV-W0",
            invoice_uuid="uuid-w0",
            ettn="uuid-w0",
            issue_date=date(2026, 10, 16),
            currency_code="TRY",
            invoice_type="SATIS",
        ),
        supplier=Party(name="Supplier", tax_number="9000000001"),
        customer=Party(name="Customer", tax_number="9000000002"),
        totals=MonetaryTotals(payable_amount=Decimal("120.00")),
        lines=(
            InvoiceLine(
                line_number="1",
                description="Line",
                quantity=Decimal("1"),
                unit_price=Decimal("100"),
                taxes=(
                    Tax(tax_type="KDV", rate=Decimal("20"), base_amount=Decimal("100"), tax_amount=Decimal("20")),
                    Tax(
                        tax_type="KDV",
                        rate=Decimal("0"),
                        base_amount=Decimal("0"),
                        tax_amount=Decimal("0"),
                        exemption_reason="351",
                    ),
                ),
            ),
        ),
    )
    return ReviewSourceInvoiceEvidence(
        review_id="review:w0", company_id=1, review_version=1, source_invoice_id="uuid-w0", invoice=invoice
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
