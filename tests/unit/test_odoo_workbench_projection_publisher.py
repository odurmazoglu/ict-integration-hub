from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.application.rules import InvoiceClassificationStatus
from app.application.workbench import (
    ReviewDecisionAcknowledgement,
    ReviewStatus,
    WorkbenchCandidateAmbiguityError,
    WorkbenchClassificationConflictRuleProjection,
    WorkbenchClassificationProjection,
    WorkbenchProjection,
)
from app.application.workflow import ManualReviewReason, ManualReviewReasonCode, WorkflowType
from app.erp.odoo.workbench_projection_publisher import (
    ODOO_BUSINESS_CONTEXT_REQUIRED_BY_CANONICAL,
    ODOO_REVIEW_REQUIRED_BY_CANONICAL,
    ODOO_REVIEW_STATUS_BY_CANONICAL,
    ODOO_WORKFLOW_BY_CANONICAL,
    OdooWorkbenchProjectionFieldMapping,
    OdooWorkbenchProjectionPublisher,
)


def test_canonical_review_status_mapping_is_explicit() -> None:
    assert ODOO_REVIEW_STATUS_BY_CANONICAL == {
        ReviewStatus.PENDING_REVIEW: "Pending Review",
        ReviewStatus.DECISION_SUBMITTED: "Decision Submitted",
        ReviewStatus.RESOLVED: "Resolved",
        ReviewStatus.DISMISSED: "Dismissed",
    }


def test_canonical_workflow_mapping_is_explicit() -> None:
    assert ODOO_WORKFLOW_BY_CANONICAL == {
        WorkflowType.VENDOR_BILL: "Vendor Bill",
        WorkflowType.RFQ: "RFQ",
        WorkflowType.EXPENSE: "Expense",
        WorkflowType.ASSET: "Asset",
        WorkflowType.SUBSCRIPTION: "Subscription",
        WorkflowType.MANUAL_REVIEW: "Manual Review",
    }


def test_review_and_business_context_required_label_mappings_are_explicit() -> None:
    assert ODOO_REVIEW_REQUIRED_BY_CANONICAL[True] == "Yes"
    assert ODOO_REVIEW_REQUIRED_BY_CANONICAL[False] == "No"
    assert ODOO_BUSINESS_CONTEXT_REQUIRED_BY_CANONICAL[True] == "Required"
    assert ODOO_BUSINESS_CONTEXT_REQUIRED_BY_CANONICAL[False] == "Not Required"


def test_create_when_no_odoo_row_exists_and_uses_exact_company_lookup() -> None:
    adapter = RecordingProjectionAdapter(search_records=[], created_id=91)
    result = OdooWorkbenchProjectionPublisher(
        adapter=adapter,
        mapping=_mapping(),
        classification_service=StaticClassificationService(_matched_classification()),
    ).publish_projection(_projection())

    assert result.created is True
    assert result.odoo_record_id == 91
    assert adapter.calls[0] == {
        "method": "search_read",
        "model": "x_ipp_import_workbench",
        "domain": [["x_studio_review_id", "=", "review-1"], ["x_studio_company", "=", 7]],
        "fields": ["id", "x_studio_review_id", "x_studio_company", "x_studio_review_version"],
        "limit": 2,
        "offset": 0,
    }
    assert adapter.create_values["x_studio_review_id"] == "review-1"
    assert adapter.create_values["x_studio_company"] == 7


def test_update_when_exactly_one_row_exists_updates_only_hub_owned_fields() -> None:
    adapter = RecordingProjectionAdapter(search_records=[{"id": 42}])
    OdooWorkbenchProjectionPublisher(
        adapter=adapter,
        mapping=_mapping(),
        classification_service=StaticClassificationService(_matched_classification()),
    ).publish_projection(_projection())

    assert adapter.write_record_id == 42
    assert adapter.write_values["x_studio_invoice_number"] == "INV-1"
    assert adapter.write_values["x_studio_invoice_total"] == 259.2
    assert adapter.write_values["x_studio_review_status"] == "Pending Review"
    assert adapter.write_values["x_studio_workflow"] == "Manual Review"
    assert adapter.write_values["x_studio_classification"] == "CLOUD_COST"
    assert adapter.write_values["x_studio_matched_rule"] == "Cloud cost vendor bill"
    assert adapter.write_values["x_studio_rule_version"] == 3
    assert adapter.write_values["x_studio_review_required"] == "No"
    assert adapter.write_values["x_studio_business_context_required"] == "Required"
    assert "x_studio_total_amount" not in adapter.write_values
    assert "x_studio_decision" not in adapter.write_values
    assert "x_studio_selected_workflow" not in adapter.write_values
    assert "x_studio_decision_comment" not in adapter.write_values
    assert "x_studio_ready_for_hub_processing" not in adapter.write_values
    assert "x_studio_allocation_list" not in adapter.write_values


def test_duplicate_parent_projection_rows_fail_closed() -> None:
    publisher = OdooWorkbenchProjectionPublisher(
        adapter=RecordingProjectionAdapter(search_records=[{"id": 42}, {"id": 43}]),
        mapping=_mapping(),
    )

    with pytest.raises(WorkbenchCandidateAmbiguityError):
        publisher.publish_projection(_projection())


def test_no_match_does_not_fabricate_matched_rule() -> None:
    adapter = RecordingProjectionAdapter(search_records=[{"id": 42}])
    OdooWorkbenchProjectionPublisher(
        adapter=adapter,
        mapping=_mapping(),
        classification_service=StaticClassificationService(
            WorkbenchClassificationProjection(
                status=InvoiceClassificationStatus.NO_MATCH.value,
                status_label="No Match",
                status_badge="muted",
                placeholder="No Decision Rule matched.",
            )
        ),
    ).publish_projection(_projection())

    assert adapter.write_values["x_studio_classification"] == "No Match"
    assert adapter.write_values["x_studio_matched_rule"] is None
    assert adapter.write_values["x_studio_rule_version"] is None


def test_conflict_projection_is_deterministic() -> None:
    adapter = RecordingProjectionAdapter(search_records=[{"id": 42}])
    OdooWorkbenchProjectionPublisher(
        adapter=adapter,
        mapping=_mapping(),
        classification_service=StaticClassificationService(_conflict_classification()),
    ).publish_projection(_projection())

    assert adapter.write_values["x_studio_classification"] == "Conflict"
    assert adapter.write_values["x_studio_conflict"] == "2 matching rules produced different actions."


def test_review_reasons_and_warnings_html_are_escaped_and_empty_is_safe() -> None:
    adapter = RecordingProjectionAdapter(search_records=[{"id": 42}])
    projection = _projection(
        review_reasons=(
            ManualReviewReason(
                code=ManualReviewReasonCode.PRODUCT_NOT_FOUND,
                message="<b>unsafe</b>",
                source="test",
            ),
        ),
        warnings=("<script>alert(1)</script>",),
    )

    OdooWorkbenchProjectionPublisher(adapter=adapter, mapping=_mapping()).publish_projection(projection)

    assert "&lt;b&gt;unsafe&lt;/b&gt;" in adapter.write_values["x_studio_review_reasons"]
    assert "<b>unsafe</b>" not in adapter.write_values["x_studio_review_reasons"]
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in adapter.write_values["x_studio_warnings"]
    assert _rendered_empty_values()["x_studio_review_reasons"] == ""
    assert _rendered_empty_values()["x_studio_warnings"] == ""


def test_acknowledge_decision_updates_only_acknowledgement_owned_fields() -> None:
    adapter = RecordingProjectionAdapter(search_records=[])
    acknowledgement = ReviewDecisionAcknowledgement(
        accepted=True,
        review_id="review-1",
        status=ReviewStatus.DECISION_SUBMITTED,
        version=5,
        decision="select_workflow",
        selected_workflow=WorkflowType.VENDOR_BILL,
        warnings=("Accepted.",),
    )

    result = OdooWorkbenchProjectionPublisher(adapter=adapter, mapping=_mapping()).acknowledge_decision(
        acknowledgement,
        odoo_record_id=42,
        trace_id="trace-ack",
    )

    assert result.updated is True
    assert adapter.write_values == {
        "x_studio_review_status": "Decision Submitted",
        "x_studio_review_version": 5,
        "x_studio_trace_id": "trace-ack",
    }


def test_idempotent_repeat_publish_updates_existing_row_without_duplicate_create() -> None:
    adapter = RecordingProjectionAdapter(search_records=[{"id": 42}])
    publisher = OdooWorkbenchProjectionPublisher(adapter=adapter, mapping=_mapping())

    publisher.publish_projection(_projection())
    first_payload = dict(adapter.write_values)
    publisher.publish_projection(_projection())

    assert adapter.create_calls == 0
    assert adapter.write_calls == 2
    assert adapter.write_values == first_payload


def test_no_domain_or_application_imports_odoo_field_names() -> None:
    source = "\n".join(
        Path(path).read_text(encoding="utf-8")
        for path in (
            "app/application/workbench/projection.py",
            "app/application/workbench/classification_projection.py",
            "app/application/workbench/ports.py",
        )
    )

    assert "x_studio_" not in source
    assert "OdooJson2Client" not in source


def test_publisher_does_not_call_ai_fuzzy_provider_or_workflow_execution() -> None:
    source = Path("app/erp/odoo/workbench_projection_publisher.py").read_text(encoding="utf-8")

    for forbidden in ("openai", "anthropic", "levenshtein", "fuzzy", "uyumsoft", "account.move", "action_post"):
        assert forbidden not in source.lower()


class StaticClassificationService:
    def __init__(self, projection: WorkbenchClassificationProjection) -> None:
        self.projection = projection
        self.calls: list[tuple[str, int, int]] = []

    def get_projection(
        self,
        *,
        review_id: str,
        company_id: int,
        review_version: int,
    ) -> WorkbenchClassificationProjection:
        self.calls.append((review_id, company_id, review_version))
        return self.projection


class RecordingProjectionAdapter:
    def __init__(self, *, search_records: list[dict[str, Any]], created_id: int = 42) -> None:
        self.search_records = search_records
        self.created_id = created_id
        self.calls: list[dict[str, Any]] = []
        self.create_values: dict[str, Any] = {}
        self.write_values: dict[str, Any] = {}
        self.write_record_id: int | None = None
        self.create_calls = 0
        self.write_calls = 0

    def search_read(
        self,
        *,
        model: str,
        domain: list[Any],
        fields: list[str],
        limit: int,
        offset: int = 0,
    ) -> tuple[dict[str, Any], ...]:
        self.calls.append(
            {
                "method": "search_read",
                "model": model,
                "domain": domain,
                "fields": fields,
                "limit": limit,
                "offset": offset,
            }
        )
        return tuple(self.search_records[:limit])

    def create(self, *, model: str, values: dict[str, Any]) -> int:
        self.create_calls += 1
        self.calls.append({"method": "create", "model": model, "values": values})
        self.create_values = values
        return self.created_id

    def write(self, *, model: str, record_id: int, values: dict[str, Any]) -> None:
        self.write_calls += 1
        self.calls.append({"method": "write", "model": model, "record_id": record_id, "values": values})
        self.write_record_id = record_id
        self.write_values = values


def _mapping() -> OdooWorkbenchProjectionFieldMapping:
    return OdooWorkbenchProjectionFieldMapping(
        model="x_ipp_import_workbench",
        review_id="x_studio_review_id",
        company_id="x_studio_company",
        invoice_number="x_studio_invoice_number",
        supplier="x_studio_supplier",
        supplier_tax_number="x_studio_supplier_tax_number",
        invoice_date="x_studio_invoice_date",
        currency="x_studio_currency",
        invoice_total="x_studio_invoice_total",
        review_status="x_studio_review_status",
        workflow="x_studio_workflow",
        review_version="x_studio_review_version",
        last_sync_at="x_studio_last_sync_at",
        classification="x_studio_classification",
        matched_rule="x_studio_matched_rule",
        rule_version="x_studio_rule_version",
        review_required="x_studio_review_required",
        business_context_required="x_studio_business_context_required",
        conflict="x_studio_conflict",
        trace_id="x_studio_trace_id",
        review_reasons="x_studio_review_reasons",
        warnings="x_studio_warnings",
    )


def _projection(**overrides: Any) -> WorkbenchProjection:
    values = {
        "review_id": "review-1",
        "company_id": 7,
        "invoice_id": "invoice-1",
        "version": 4,
        "status": ReviewStatus.PENDING_REVIEW,
        "invoice_number": "INV-1",
        "supplier_name": "Supplier A",
        "supplier_tax_number": "1234567890",
        "invoice_date": datetime(2026, 8, 17, tzinfo=UTC).date(),
        "currency": "TRY",
        "total_amount": Decimal("259.20"),
        "workflow": WorkflowType.MANUAL_REVIEW,
        "review_reasons": (),
        "warnings": (),
        "trace_id": "trace-123",
        "updated_at": datetime(2026, 8, 17, 10, 0, tzinfo=UTC),
    }
    values.update(overrides)
    return WorkbenchProjection(**values)


def _matched_classification() -> WorkbenchClassificationProjection:
    return WorkbenchClassificationProjection(
        status=InvoiceClassificationStatus.MATCHED.value,
        status_label="Matched",
        status_badge="success",
        workflow=WorkflowType.VENDOR_BILL,
        workflow_display="Vendor Bill",
        classification_code="CLOUD_COST",
        matched_rule_name="Cloud cost vendor bill",
        matched_rule_code="CLOUD_COST_VENDOR_BILL",
        matched_rule_version=3,
        require_review=False,
        require_review_label="No",
        require_review_badge="muted",
        require_business_context=True,
        require_business_context_label="Required",
        require_business_context_badge="info",
    )


def _conflict_classification() -> WorkbenchClassificationProjection:
    return WorkbenchClassificationProjection(
        status=InvoiceClassificationStatus.CONFLICT.value,
        status_label="Conflict",
        status_badge="danger",
        conflict=True,
        conflict_label="Conflict",
        conflict_summary="2 matching rules produced different actions.",
        conflicting_rules_summary=(
            WorkbenchClassificationConflictRuleProjection(
                rule_name="Cloud cost vendor bill",
                rule_code="CLOUD_COST_VENDOR_BILL",
                rule_version=3,
                workflow=WorkflowType.VENDOR_BILL,
                workflow_display="Vendor Bill",
                classification_code="CLOUD_COST",
            ),
            WorkbenchClassificationConflictRuleProjection(
                rule_name="Software license expense",
                rule_code="SOFTWARE_LICENSE_EXPENSE",
                rule_version=2,
                workflow=WorkflowType.EXPENSE,
                workflow_display="Expense",
                classification_code="SOFTWARE_LICENSE_COST",
            ),
        ),
    )


def _rendered_empty_values() -> dict[str, Any]:
    adapter = RecordingProjectionAdapter(search_records=[{"id": 42}])
    OdooWorkbenchProjectionPublisher(adapter=adapter, mapping=_mapping()).publish_projection(_projection())
    return adapter.write_values
