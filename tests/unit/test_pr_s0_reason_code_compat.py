"""PR S0: rollback compatibility for the PR S reason codes.

This image only *understands* RETURN_INVOICE_NOT_SUPPORTED and
AMBIGUOUS_COMMERCIAL_PURCHASE: a review persisted by PR S must still load, list, render
in the Workbench and stay blocked. Nothing here emits, strips or acts on them -- no
classification, decision, builder or execution behavior changes.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.application.commands import ImportInvoiceCommand
from app.application.dto import DecisionResult
from app.application.use_cases import ImportInvoiceUseCase
from app.application.use_cases.effective_decision import _effective_manual_review_reasons, _effective_workflow
from app.application.workbench import ReviewItemCreationService
from app.application.workbench.dto import ReviewStatus
from app.application.workbench.operator_guidance import (
    REASON_TITLES,
    GuidanceInput,
    OperatorGuidanceFacts,
    OperatorNextAction,
    build_operator_guidance,
)
from app.application.workbench.queries import ReviewDetailQuery, ReviewQueueQuery
from app.application.workflow import ManualReviewReason, ManualReviewReasonCode, WorkflowType
from app.db.base import Base
from app.domain.invoice import Header, InternalInvoice, InvoiceLine, MonetaryTotals, Party
from app.persistence import SqlAlchemyImportHistory, SqlAlchemyReviewRepository

RETURN = ManualReviewReasonCode.RETURN_INVOICE_NOT_SUPPORTED
AMBIGUOUS = ManualReviewReasonCode.AMBIGUOUS_COMMERCIAL_PURCHASE
NEW_CODES = (RETURN, AMBIGUOUS)

#: ``ManualReviewReasonCode`` exactly as released in eee18a2.
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


def test_reason_codes_are_append_only() -> None:
    values = tuple(code.value for code in ManualReviewReasonCode)

    assert values[: len(_EEE18A2_REASON_CODES)] == _EEE18A2_REASON_CODES
    # S0 appended exactly these two right after eee18a2; later members (PR T W0) follow them.
    assert values[len(_EEE18A2_REASON_CODES) : len(_EEE18A2_REASON_CODES) + 2] == (
        "RETURN_INVOICE_NOT_SUPPORTED",
        "AMBIGUOUS_COMMERCIAL_PURCHASE",
    )


def test_every_reason_code_has_a_workbench_title() -> None:
    assert set(REASON_TITLES) == set(ManualReviewReasonCode)


def test_new_codes_are_referenced_only_by_their_owning_modules() -> None:
    """Definition and titles (S0), emission (rule engine) and resolution (effective decision, PR S)."""

    allowed = {
        Path("app/application/workflow.py"),
        Path("app/application/workbench/operator_guidance.py"),
        Path("app/application/rules/deterministic.py"),
        Path("app/application/use_cases/effective_decision.py"),
    }
    for path in Path("app").rglob("*.py"):
        if path in allowed:
            continue
        source = path.read_text()
        for code in NEW_CODES:
            assert code.value not in source, f"{path} references {code.value}"


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

    assert tuple(reason.code for reason in detail.review_reasons) == (code,)
    assert [item.review_id for item in queue.items] == [result.review_id]
    assert queue.items[0].review_reasons[0].code is code


@pytest.mark.parametrize("code", NEW_CODES)
def test_workbench_shows_new_code_as_technical_blocker(code: ManualReviewReasonCode) -> None:
    guidance = build_operator_guidance(
        GuidanceInput(
            status=ReviewStatus.PENDING_REVIEW,
            reason_codes=(code,),
            supplier_name="Supplier",
            decision_type=None,
            decision_workflow=None,
            decision_version=None,
            execution_state=None,
            vendor_bill_id=None,
        ),
        OperatorGuidanceFacts(),
    )

    assert guidance.next_action is OperatorNextAction.TECHNICAL
    assert REASON_TITLES[code] in guidance.todo_html


def test_return_code_is_never_stripped_so_review_stays_manual() -> None:
    reasons = (ManualReviewReason(code=RETURN, message=RETURN.value),)

    effective = _effective_manual_review_reasons(reasons, None, None, fixed_asset_resolved=True)

    assert effective == reasons
    assert _effective_workflow(WorkflowType.MANUAL_REVIEW, effective) is WorkflowType.MANUAL_REVIEW


def test_ambiguous_code_stays_without_an_explicit_resolution() -> None:
    """PR S: only an explicit review-scoped accounting resolution clears it (tested in PR S)."""

    reasons = (ManualReviewReason(code=AMBIGUOUS, message=AMBIGUOUS.value),)

    effective = _effective_manual_review_reasons(reasons, None, None)

    assert effective == reasons
    assert _effective_workflow(WorkflowType.MANUAL_REVIEW, effective) is WorkflowType.MANUAL_REVIEW


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
            review_reasons=(ManualReviewReason(code=self._code, message="Persisted by PR S.", source="pr_s"),),
        )


def _invoice() -> InternalInvoice:
    return InternalInvoice(
        header=Header(
            invoice_number="INV-S0",
            invoice_uuid="INV-S0-UUID",
            ettn="INV-S0-ETTN",
            issue_date=date(2026, 10, 15),
            currency_code="TRY",
            invoice_type="IADE",
        ),
        supplier=Party(name="Supplier", tax_number="1111111111"),
        customer=Party(name="Customer", tax_number="2222222222"),
        totals=MonetaryTotals(payable_amount=Decimal("120")),
        lines=(InvoiceLine(line_number="1", description="Line"),),
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
