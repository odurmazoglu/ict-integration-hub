from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.application.workbench import (
    LineResolution,
    OdooWorkbenchDecisionCandidate,
    ProjectionPublishResult,
    ReviewDecisionType,
    ReviewStatus,
    TaxResolution,
    WorkbenchContractError,
    WorkbenchDecisionCandidateReader,
    WorkbenchProjection,
    WorkbenchProjectionPublisher,
)
from app.application.workbench.dto import BusinessContextDecision
from app.application.workflow import ManualReviewReason, ManualReviewReasonCode, WorkflowType


def test_workbench_projection_is_immutable() -> None:
    projection = _projection()

    with pytest.raises(FrozenInstanceError):
        projection.review_id = "changed"  # type: ignore[misc]


def test_workbench_projection_requires_positive_company_id() -> None:
    with pytest.raises(WorkbenchContractError, match="company_id must be positive"):
        _projection(company_id=0)


def test_workbench_projection_requires_positive_version() -> None:
    with pytest.raises(WorkbenchContractError, match="version must be positive"):
        _projection(version=0)


def test_workbench_projection_requires_review_id() -> None:
    with pytest.raises(WorkbenchContractError, match="review_id is required"):
        _projection(review_id=" ")


def test_workbench_projection_requires_canonical_workflow_and_status_values() -> None:
    with pytest.raises(WorkbenchContractError, match="workflow must be a canonical WorkflowType"):
        _projection(workflow="vendor_bill")  # type: ignore[arg-type]
    with pytest.raises(WorkbenchContractError, match="status must be a canonical ReviewStatus"):
        _projection(status="pending_review")  # type: ignore[arg-type]


def test_workbench_projection_retains_decimal_without_float_conversion() -> None:
    amount = Decimal("259.2000")
    projection = _projection(total_amount=amount)

    assert projection.total_amount is amount
    assert projection.total_amount == Decimal("259.2000")
    assert "float(" not in Path("app/application/workbench/projection.py").read_text(encoding="utf-8")


def test_workbench_projection_retains_structured_review_reasons() -> None:
    reason = ManualReviewReason(
        code=ManualReviewReasonCode.PRODUCT_NOT_FOUND,
        message="Product was not found.",
        line_number="1",
        source="rule_engine",
        details=(("identifier", "SKU-1"),),
    )

    projection = _projection(review_reasons=[reason], warnings=["Check product mapping."])

    assert projection.review_reasons == (reason,)
    assert projection.review_reasons[0].details == (("identifier", "SKU-1"),)
    assert projection.warnings == ("Check product mapping.",)


def test_odoo_workbench_decision_candidate_is_immutable() -> None:
    candidate = _candidate()

    with pytest.raises(FrozenInstanceError):
        candidate.review_id = "changed"  # type: ignore[misc]


def test_decision_candidate_requires_positive_odoo_record_id() -> None:
    with pytest.raises(WorkbenchContractError, match="odoo_record_id must be a positive ERP id"):
        _candidate(odoo_record_id=0)


def test_decision_candidate_requires_positive_expected_version() -> None:
    with pytest.raises(WorkbenchContractError, match="expected_version must be positive"):
        _candidate(expected_version=0)


def test_decision_candidate_requires_idempotency_key() -> None:
    with pytest.raises(WorkbenchContractError, match="idempotency_key is required"):
        _candidate(idempotency_key=" ")


def test_decision_candidate_must_be_ready() -> None:
    with pytest.raises(WorkbenchContractError, match="decision_ready must be true"):
        _candidate(decision_ready=False)


def test_decision_candidate_validates_selected_workflow() -> None:
    with pytest.raises(WorkbenchContractError, match="selected_workflow must be a canonical WorkflowType"):
        _candidate(selected_workflow="vendor_bill")  # type: ignore[arg-type]


def test_decision_candidate_rejects_manual_review_as_resolution() -> None:
    with pytest.raises(WorkbenchContractError, match="MANUAL_REVIEW cannot be selected"):
        _candidate(selected_workflow=WorkflowType.MANUAL_REVIEW)


def test_decision_candidate_rejects_duplicate_line_resolutions_through_current_contract() -> None:
    with pytest.raises(WorkbenchContractError, match="line_resolutions must have unique"):
        _candidate(
            line_resolutions=(
                LineResolution(line_number="1", selected_product_id=10),
                LineResolution(line_number="1", selected_product_id=11),
            )
        )


def test_decision_candidate_rejects_duplicate_tax_resolutions_through_current_contract() -> None:
    with pytest.raises(WorkbenchContractError, match="tax_resolutions must have unique"):
        _candidate(
            tax_resolutions=(
                TaxResolution(line_number="1", tax_index=0, selected_tax_id=20),
                TaxResolution(line_number="1", tax_index=0, selected_tax_id=21),
            )
        )


def test_decision_candidate_uses_safe_structured_resolution_contracts() -> None:
    candidate = _candidate(
        line_resolutions=(LineResolution(line_number="1", selected_product_id=10),),
        tax_resolutions=(TaxResolution(line_number="1", tax_index=0, selected_tax_id=20),),
        business_context=BusinessContextDecision(sales_order_id=30, project_id=31),
    )

    assert candidate.line_resolutions[0].selected_product_id == 10
    assert candidate.tax_resolutions[0].selected_tax_id == 20
    assert candidate.business_context.sales_order_id == 30
    assert candidate.comment == "Reviewed in Odoo."


def test_projection_publish_result_is_immutable() -> None:
    result = ProjectionPublishResult(
        review_id="review-1",
        odoo_record_id=42,
        created=True,
        updated=False,
        version=1,
        warnings=("Created projection.",),
    )

    with pytest.raises(FrozenInstanceError):
        result.version = 2  # type: ignore[misc]
    assert result.warnings == ("Created projection.",)


def test_projection_ports_are_application_protocols_without_odoo_dependency() -> None:
    ports_source = Path("app/application/workbench/ports.py").read_text(encoding="utf-8")

    assert "class WorkbenchProjectionPublisher(Protocol)" in ports_source
    assert "class WorkbenchDecisionCandidateReader(Protocol)" in ports_source
    assert "app.connectors.odoo" not in ports_source
    assert "OdooJson2Client" not in ports_source
    assert hasattr(WorkbenchProjectionPublisher, "publish_projection")
    assert hasattr(WorkbenchProjectionPublisher, "acknowledge_decision")
    assert hasattr(WorkbenchDecisionCandidateReader, "list_ready_decisions")
    assert hasattr(WorkbenchDecisionCandidateReader, "get_ready_decision")


def test_application_projection_contracts_import_no_infrastructure() -> None:
    combined_source = "\n".join(
        Path(path).read_text(encoding="utf-8")
        for path in (
            "app/application/workbench/projection.py",
            "app/application/workbench/ports.py",
        )
    )

    for forbidden in (
        "app.connectors",
        "app.erp",
        "sqlalchemy",
        "httpx",
        "OdooJson2Client",
        "JSON-2",
        "account.move",
        "action_post",
        "unlink",
        "Keycloak",
        "client_secret",
    ):
        assert forbidden not in combined_source


def test_projection_pr_adds_no_runtime_odoo_write_or_custom_addon() -> None:
    changed_paths = {
        "app/application/workbench/projection.py",
        "app/application/workbench/ports.py",
    }
    for path in changed_paths:
        source = Path(path).read_text(encoding="utf-8")
        assert "/json/2/" not in source
        assert "create_account_move" not in source
        assert "search_read(" not in source

    assert not Path("odoo").exists()
    assert not Path("addons").exists()


def test_projection_contracts_do_not_perform_workflow_execution() -> None:
    source = Path("app/application/workbench/projection.py").read_text(encoding="utf-8")

    for forbidden in ("WorkflowStrategy", "DecisionEngine", "VendorBillStrategy", "execute("):
        assert forbidden not in source


def _projection(**overrides) -> WorkbenchProjection:
    values = {
        "review_id": "review-1",
        "company_id": 7,
        "invoice_id": "invoice-1",
        "version": 1,
        "status": ReviewStatus.PENDING_REVIEW,
        "invoice_number": "INV-1",
        "supplier_name": "Supplier A",
        "supplier_tax_number": "1234567890",
        "invoice_date": datetime(2026, 7, 17, tzinfo=UTC).date(),
        "currency": "TRY",
        "total_amount": Decimal("259.2000"),
        "workflow": WorkflowType.MANUAL_REVIEW,
        "review_summary": "Product resolution is required.",
        "review_reasons": (),
        "warnings": (),
        "trace_id": "trace-123",
        "updated_at": datetime(2026, 7, 17, 9, 35, tzinfo=UTC),
    }
    values.update(overrides)
    return WorkbenchProjection(**values)


def _candidate(**overrides) -> OdooWorkbenchDecisionCandidate:
    values = {
        "odoo_record_id": 42,
        "review_id": "review-1",
        "company_id": 7,
        "expected_version": 1,
        "decision": ReviewDecisionType.SELECT_WORKFLOW,
        "selected_workflow": WorkflowType.VENDOR_BILL,
        "selected_partner_id": 700,
        "line_resolutions": (),
        "tax_resolutions": (),
        "business_context": None,
        "comment": "Reviewed in Odoo.",
        "idempotency_key": "odoo-decision-key-1",
        "decided_by_odoo_user_id": 11,
        "decided_at": datetime(2026, 7, 17, 10, 0, tzinfo=UTC),
        "decision_ready": True,
    }
    values.update(overrides)
    return OdooWorkbenchDecisionCandidate(**values)
