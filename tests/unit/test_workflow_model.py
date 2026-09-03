from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

import app.application
from app.application.dto import DecisionResult, RuleEvaluationResult
from app.application.workflow import (
    ManualReviewDecision,
    ManualReviewReason,
    ManualReviewReasonCode,
    WorkflowDecision,
    WorkflowType,
)


def test_workflow_type_defines_canonical_platform_vocabulary() -> None:
    assert {workflow.value for workflow in WorkflowType} == {
        "vendor_bill",
        "rfq",
        "expense",
        "asset",
        "subscription",
        "customer_quotation",
        "manual_review",
    }


def test_workflow_decision_is_immutable_and_explainable() -> None:
    decision = WorkflowDecision(
        workflow=WorkflowType.VENDOR_BILL,
        matched_rule="direct_vendor_bill_import",
        explanation="Direct vendor bill import selected.",
        warnings=("review vendor reference",),
        errors=(),
    )

    assert decision.workflow is WorkflowType.VENDOR_BILL
    assert decision.matched_rule == "direct_vendor_bill_import"
    assert decision.explanation == "Direct vendor bill import selected."
    assert decision.warnings == ("review vendor reference",)

    with pytest.raises(FrozenInstanceError):
        decision.workflow = WorkflowType.EXPENSE


def test_manual_review_models_are_immutable_and_structured() -> None:
    reason = ManualReviewReason(
        code=ManualReviewReasonCode.PRODUCT_NOT_FOUND,
        message="Product was not matched deterministically.",
        line_number="1",
        candidate_count=0,
        source="product_matching",
        details=(("reason", "No active deterministic product candidate found."),),
    )
    review = ManualReviewDecision(reasons=(reason,), summary="1 deterministic review reason(s) require manual review.")

    assert reason.code is ManualReviewReasonCode.PRODUCT_NOT_FOUND
    assert review.reasons == (reason,)

    with pytest.raises(FrozenInstanceError):
        reason.message = "changed"


def test_rule_and_decision_results_use_workflow_type() -> None:
    workflow_decision = WorkflowDecision(WorkflowType.VENDOR_BILL)
    rule_result = RuleEvaluationResult(workflow_decision=workflow_decision)
    decision_result = DecisionResult(
        success=True,
        invoice_id="INV-1",
        workflow=WorkflowType.VENDOR_BILL,
        strategy=WorkflowType.VENDOR_BILL.value,
        status="dry_run",
    )

    assert rule_result.workflow is WorkflowType.VENDOR_BILL
    assert decision_result.workflow is WorkflowType.VENDOR_BILL


def test_application_package_exports_workflow_model() -> None:
    assert app.application.WorkflowType is WorkflowType
    assert app.application.WorkflowDecision is WorkflowDecision


def test_application_layer_does_not_duplicate_workflow_string_literals() -> None:
    canonical_file = Path("app/application/workflow.py")
    workflow_literals = {f'"{workflow.value}"' for workflow in WorkflowType}

    for path in Path("app/application").rglob("*.py"):
        if path == canonical_file:
            continue
        content = path.read_text()
        for literal in workflow_literals:
            assert literal not in content, f"{path} duplicates workflow literal {literal}"
