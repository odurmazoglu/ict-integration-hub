from __future__ import annotations

from dataclasses import FrozenInstanceError, asdict
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.application.workbench import (
    BusinessContextDecision,
    LineResolution,
    ReviewDecisionAcknowledgement,
    ReviewDecisionCommand,
    ReviewDecisionType,
    ReviewDetailQuery,
    ReviewItem,
    ReviewQueueQuery,
    ReviewQueueReader,
    ReviewQueueResult,
    ReviewStatus,
    TaxResolution,
    WorkbenchContractError,
)
from app.application.workbench.queries import DEFAULT_REVIEW_QUEUE_LIMIT, MAX_REVIEW_QUEUE_LIMIT
from app.application.workflow import ManualReviewReason, ManualReviewReasonCode, WorkflowType


def test_review_item_is_immutable_and_uses_workflow_and_review_reason_models() -> None:
    item = _review_item()

    assert item.workflow is WorkflowType.MANUAL_REVIEW
    assert item.status is ReviewStatus.PENDING_REVIEW
    assert item.review_reasons[0].code is ManualReviewReasonCode.PRODUCT_NOT_FOUND

    with pytest.raises(FrozenInstanceError):
        item.review_id = "changed"


def test_review_status_is_canonical_enum() -> None:
    assert {status.value for status in ReviewStatus} == {
        "pending_review",
        "decision_submitted",
        "resolved",
        "dismissed",
    }


def test_review_decision_type_contains_only_explicit_supported_decisions() -> None:
    assert {decision.value for decision in ReviewDecisionType} == {
        "select_workflow",
        "dismiss",
    }


def test_review_queue_query_defaults_are_safe() -> None:
    query = ReviewQueueQuery(company_id=7)

    assert query.status is ReviewStatus.PENDING_REVIEW
    assert query.limit == DEFAULT_REVIEW_QUEUE_LIMIT
    assert query.offset == 0
    assert query.workflow is None


@pytest.mark.parametrize("limit", [0, -1, MAX_REVIEW_QUEUE_LIMIT + 1])
def test_review_queue_query_rejects_invalid_limit(limit: int) -> None:
    with pytest.raises(WorkbenchContractError):
        ReviewQueueQuery(company_id=7, limit=limit)


def test_review_queue_query_rejects_invalid_offset() -> None:
    with pytest.raises(WorkbenchContractError):
        ReviewQueueQuery(company_id=7, offset=-1)


def test_review_queue_query_keeps_exact_supplier_tax_number_filter() -> None:
    query = ReviewQueueQuery(company_id=7, supplier_tax_number=" 1234567890 ")

    assert query.supplier_tax_number == " 1234567890 "


def test_review_queue_result_is_immutable() -> None:
    result = ReviewQueueResult(items=(_review_item(),), total_count=1, limit=10, offset=0)

    assert result.items[0].review_id == "review-1"
    with pytest.raises(FrozenInstanceError):
        result.total_count = 2


def test_review_detail_query_validates_required_values() -> None:
    assert ReviewDetailQuery(review_id="review-1", company_id=7).review_id == "review-1"

    with pytest.raises(WorkbenchContractError):
        ReviewDetailQuery(review_id="", company_id=7)
    with pytest.raises(WorkbenchContractError):
        ReviewDetailQuery(review_id="review-1", company_id=0)


def test_review_decision_command_is_immutable() -> None:
    command = _command(decision=ReviewDecisionType.SELECT_WORKFLOW, selected_workflow=WorkflowType.VENDOR_BILL)

    with pytest.raises(FrozenInstanceError):
        command.review_id = "changed"


def test_select_workflow_requires_selected_workflow() -> None:
    with pytest.raises(WorkbenchContractError):
        _command(decision=ReviewDecisionType.SELECT_WORKFLOW)

    command = _command(decision=ReviewDecisionType.SELECT_WORKFLOW, selected_workflow=WorkflowType.VENDOR_BILL)
    assert command.selected_workflow is WorkflowType.VENDOR_BILL


def test_select_workflow_rejects_manual_review_as_resolution() -> None:
    with pytest.raises(WorkbenchContractError):
        _command(decision=ReviewDecisionType.SELECT_WORKFLOW, selected_workflow=WorkflowType.MANUAL_REVIEW)


@pytest.mark.parametrize(
    "workflow",
    [
        WorkflowType.VENDOR_BILL,
        WorkflowType.RFQ,
        WorkflowType.EXPENSE,
        WorkflowType.ASSET,
        WorkflowType.SUBSCRIPTION,
    ],
)
def test_select_workflow_accepts_canonical_resolution_workflows(workflow: WorkflowType) -> None:
    command = _command(decision=ReviewDecisionType.SELECT_WORKFLOW, selected_workflow=workflow)

    assert command.selected_workflow is workflow


def test_dismiss_rejects_incompatible_workflow_specific_fields() -> None:
    assert _command(decision=ReviewDecisionType.DISMISS).decision is ReviewDecisionType.DISMISS

    with pytest.raises(WorkbenchContractError):
        _command(decision=ReviewDecisionType.DISMISS, selected_workflow=WorkflowType.VENDOR_BILL)
    with pytest.raises(WorkbenchContractError):
        _command(decision=ReviewDecisionType.DISMISS, selected_partner_id=10)


def test_duplicate_line_resolutions_are_rejected() -> None:
    with pytest.raises(WorkbenchContractError):
        _command(
            decision=ReviewDecisionType.SELECT_WORKFLOW,
            selected_workflow=WorkflowType.VENDOR_BILL,
            line_resolutions=(LineResolution("1", 10), LineResolution("1", 11)),
        )


def test_duplicate_tax_resolutions_are_rejected() -> None:
    with pytest.raises(WorkbenchContractError):
        _command(
            decision=ReviewDecisionType.SELECT_WORKFLOW,
            selected_workflow=WorkflowType.VENDOR_BILL,
            tax_resolutions=(TaxResolution("1", 0, 10), TaxResolution("1", 0, 11)),
        )


@pytest.mark.parametrize(
    "factory",
    [
        lambda: LineResolution("1", 0),
        lambda: TaxResolution("1", 0, 0),
        lambda: _command(
            decision=ReviewDecisionType.SELECT_WORKFLOW,
            selected_workflow=WorkflowType.VENDOR_BILL,
            selected_partner_id=0,
        ),
    ],
)
def test_positive_erp_ids_are_required(factory: object) -> None:
    with pytest.raises(WorkbenchContractError):
        factory()


def test_business_context_decision_requires_positive_ids() -> None:
    context = BusinessContextDecision(
        opportunity_id=1,
        sales_order_id=2,
        proposal_scenario_id=3,
        purchase_order_id=4,
        project_id=5,
        analytic_account_id=6,
    )
    assert context.purchase_order_id == 4

    with pytest.raises(WorkbenchContractError):
        BusinessContextDecision(purchase_order_id=0)


def test_decision_command_requires_decided_by() -> None:
    with pytest.raises(WorkbenchContractError):
        _command(decision=ReviewDecisionType.DISMISS, decided_by=" ")


def test_decision_command_requires_idempotency_key() -> None:
    with pytest.raises(WorkbenchContractError):
        _command(decision=ReviewDecisionType.DISMISS, idempotency_key="")


def test_decision_command_rejects_unsafe_comment_length() -> None:
    with pytest.raises(WorkbenchContractError):
        _command(decision=ReviewDecisionType.DISMISS, comment="x" * 1001)


def test_decision_acknowledgement_is_immutable_and_serializable_by_future_adapter() -> None:
    acknowledgement = ReviewDecisionAcknowledgement(
        accepted=True,
        review_id="review-1",
        status=ReviewStatus.DECISION_SUBMITTED,
        version=2,
        decision=ReviewDecisionType.SELECT_WORKFLOW,
        selected_workflow=WorkflowType.VENDOR_BILL,
        warnings=("safe warning",),
    )

    assert asdict(acknowledgement)["review_id"] == "review-1"
    with pytest.raises(FrozenInstanceError):
        acknowledgement.accepted = False


def test_review_queue_reader_port_shape_is_read_only() -> None:
    assert "list_review_items" in ReviewQueueReader.__dict__
    assert "get_review_item" in ReviewQueueReader.__dict__
    assert "save" not in ReviewQueueReader.__dict__


def test_workbench_contracts_do_not_import_odoo_or_provider_layers() -> None:
    source = _workbench_source()
    forbidden = ("app.connectors", "app.models", "app.db", "odoo", "uyumsoft")

    for token in forbidden:
        assert token not in source.lower()


def test_workbench_contracts_do_not_use_http_soap_sql_or_persistence() -> None:
    source = _workbench_source()
    forbidden = ("fastapi", "requests", "httpx", "soap", "zeep", "sqlalchemy", "database", "session")

    for token in forbidden:
        assert token not in source.lower()


def test_workbench_contracts_do_not_perform_erp_writes_or_workflow_execution() -> None:
    source = _workbench_source()
    forbidden = ("create_draft", "vendorbillwriter", "account.move", "action_post", "unlink")

    for token in forbidden:
        assert token not in source.lower()


def test_workbench_contracts_do_not_use_ai_or_fuzzy_matching() -> None:
    source = _workbench_source()
    forbidden = ("ai_advisor", "ollama", "fuzzy", "levenshtein", "embedding", "similarity")

    for token in forbidden:
        assert token not in source.lower()


def test_workbench_contracts_have_no_recommendation_acceptance_contract() -> None:
    source = _workbench_source() + Path(__file__).read_text(encoding="utf-8").lower()
    removed_decision_value = "accept" + "_recommendation"

    assert removed_decision_value not in source


def _review_item() -> ReviewItem:
    return ReviewItem(
        review_id="review-1",
        invoice_id="invoice-1",
        invoice_number="INV-1",
        supplier_tax_number="1234567890",
        supplier_name="Supplier Display",
        invoice_date=date(2026, 8, 2),
        currency="TRY",
        total_amount=Decimal("120.00"),
        workflow=WorkflowType.MANUAL_REVIEW,
        status=ReviewStatus.PENDING_REVIEW,
        review_reasons=(
            ManualReviewReason(
                code=ManualReviewReasonCode.PRODUCT_NOT_FOUND,
                message="Product was not matched deterministically.",
                line_number="1",
                source="product_matching",
            ),
        ),
        warnings=("safe warning",),
        created_at=datetime(2026, 8, 2, tzinfo=UTC),
        updated_at=datetime(2026, 8, 2, tzinfo=UTC),
        version=1,
    )


def _command(
    *,
    decision: ReviewDecisionType,
    selected_workflow: WorkflowType | None = None,
    selected_partner_id: int | None = None,
    line_resolutions: tuple[LineResolution, ...] = (),
    tax_resolutions: tuple[TaxResolution, ...] = (),
    business_context: BusinessContextDecision | None = None,
    comment: str | None = None,
    decided_by: str = "user-1",
    idempotency_key: str = "review-1:v1:user-1",
) -> ReviewDecisionCommand:
    return ReviewDecisionCommand(
        review_id="review-1",
        company_id=7,
        expected_version=1,
        decision=decision,
        selected_workflow=selected_workflow,
        selected_partner_id=selected_partner_id,
        line_resolutions=line_resolutions,
        tax_resolutions=tax_resolutions,
        business_context=business_context,
        comment=comment,
        decided_by=decided_by,
        idempotency_key=idempotency_key,
    )


def _workbench_source() -> str:
    package_root = Path(__file__).resolve().parents[2] / "app" / "application" / "workbench"
    return "\n".join(path.read_text(encoding="utf-8") for path in package_root.rglob("*.py")).lower()
