from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from app.application.rules import InvoiceClassificationRuleEvidence, InvoiceClassificationStatus
from app.application.workbench import (
    ReviewClassificationEvidence,
    WorkbenchClassificationConflictRuleProjection,
    WorkbenchClassificationProjection,
    WorkbenchClassificationProjectionService,
    WorkbenchContractError,
)
from app.application.workbench.classification_projection import (
    MISSING_EVIDENCE_PLACEHOLDER,
    NO_MATCH_PLACEHOLDER,
)
from app.application.workbench.exceptions import ReviewDataIntegrityError, ReviewNotFoundError
from app.application.workflow import WorkflowType


def test_matched_classification_projection() -> None:
    projection = WorkbenchClassificationProjectionService(
        StaticClassificationEvidenceReader(_matched_evidence())
    ).get_projection(review_id="review-1", company_id=7, review_version=1)

    assert projection.status == "MATCHED"
    assert projection.status_label == "Matched"
    assert projection.status_badge == "success"
    assert projection.workflow is WorkflowType.VENDOR_BILL
    assert projection.workflow_display == "Vendor Bill"
    assert projection.classification_code == "CLOUD_COST"
    assert projection.matched_rule_name == "Cloud cost vendor bill"
    assert projection.matched_rule_code == "CLOUD_COST_VENDOR_BILL"
    assert projection.matched_rule_version == 3
    assert projection.require_review is False
    assert projection.require_review_label == "No"
    assert projection.require_review_badge == "muted"
    assert projection.require_business_context is True
    assert projection.require_business_context_label == "Required"
    assert projection.require_business_context_badge == "info"
    assert projection.conflict is False
    assert projection.conflicting_rules_summary == ()


def test_review_required_projection_uses_warning_badge() -> None:
    projection = WorkbenchClassificationProjectionService(
        StaticClassificationEvidenceReader(
            _matched_evidence(
                status=InvoiceClassificationStatus.REVIEW_REQUIRED,
                require_review=True,
                workflow=WorkflowType.EXPENSE,
                classification_code="EV_CHARGING",
            )
        )
    ).get_projection(review_id="review-1", company_id=7, review_version=1)

    assert projection.status == "REVIEW_REQUIRED"
    assert projection.status_label == "Review Required"
    assert projection.status_badge == "warning"
    assert projection.workflow_display == "Expense"
    assert projection.classification_code == "EV_CHARGING"
    assert projection.require_review_label == "Yes"
    assert projection.require_review_badge == "warning"


def test_no_match_projection_is_explicit() -> None:
    projection = WorkbenchClassificationProjectionService(
        StaticClassificationEvidenceReader(_no_match_evidence())
    ).get_projection(review_id="review-1", company_id=7, review_version=1)

    assert projection.status == "NO_MATCH"
    assert projection.status_label == "No Match"
    assert projection.status_badge == "muted"
    assert projection.placeholder == NO_MATCH_PLACEHOLDER
    assert projection.workflow is None
    assert projection.classification_code is None
    assert projection.matched_rule_name is None


def test_conflict_projection_summarizes_rules_deterministically() -> None:
    projection = WorkbenchClassificationProjectionService(
        StaticClassificationEvidenceReader(_conflict_evidence())
    ).get_projection(review_id="review-1", company_id=7, review_version=1)

    assert projection.status == "CONFLICT"
    assert projection.status_label == "Conflict"
    assert projection.status_badge == "danger"
    assert projection.conflict is True
    assert projection.conflict_label == "Conflict"
    assert projection.conflict_summary == "2 matching rules produced different actions."
    assert tuple(rule.rule_code for rule in projection.conflicting_rules_summary) == (
        "CLOUD_COST_VENDOR_BILL",
        "SOFTWARE_LICENSE_EXPENSE",
    )
    assert projection.conflicting_rules_summary[0].workflow_display == "Vendor Bill"
    assert projection.conflicting_rules_summary[0].classification_code == "CLOUD_COST"
    assert projection.conflicting_rules_summary[1].workflow_display == "Expense"
    assert projection.conflicting_rules_summary[1].classification_code == "SOFTWARE_LICENSE_COST"


def test_projection_dto_is_immutable() -> None:
    projection = WorkbenchClassificationProjectionService(
        StaticClassificationEvidenceReader(_matched_evidence())
    ).get_projection(review_id="review-1", company_id=7, review_version=1)

    with pytest.raises(FrozenInstanceError):
        projection.status = "NO_MATCH"  # type: ignore[misc]


def test_conflict_rule_projection_rejects_noncanonical_workflow_display() -> None:
    with pytest.raises(WorkbenchContractError, match="workflow_display"):
        WorkbenchClassificationConflictRuleProjection(
            rule_name="Cloud cost vendor bill",
            rule_code="CLOUD_COST_VENDOR_BILL",
            rule_version=3,
            workflow=WorkflowType.VENDOR_BILL,
            workflow_display="vendor_bill",
            classification_code="CLOUD_COST",
        )


def test_projection_rejects_wrong_badge_mapping() -> None:
    with pytest.raises(WorkbenchContractError, match="status_badge"):
        WorkbenchClassificationProjection(
            status="MATCHED",
            status_label="Matched",
            status_badge="danger",
        )


def test_missing_evidence_projects_safe_placeholder() -> None:
    projection = WorkbenchClassificationProjectionService(MissingClassificationEvidenceReader()).get_projection(
        review_id="review-1",
        company_id=7,
        review_version=1,
    )

    assert projection.status == "UNAVAILABLE"
    assert projection.status_label == "Unavailable"
    assert projection.status_badge == "muted"
    assert projection.placeholder == MISSING_EVIDENCE_PLACEHOLDER


def test_malformed_evidence_fails_closed() -> None:
    with pytest.raises(ReviewDataIntegrityError):
        WorkbenchClassificationProjectionService(MalformedClassificationEvidenceReader()).get_projection(
            review_id="review-1",
            company_id=7,
            review_version=1,
        )


def test_projection_service_uses_exact_reader_query() -> None:
    reader = StaticClassificationEvidenceReader(_matched_evidence())

    WorkbenchClassificationProjectionService(reader).get_projection(
        review_id="review-1",
        company_id=7,
        review_version=1,
    )

    assert reader.calls == (("review-1", 7, 1),)


def test_historical_review_projection_uses_persisted_evidence_only() -> None:
    persisted = _matched_evidence(workflow=WorkflowType.VENDOR_BILL, classification_code="CLOUD_COST")
    changed_current_rules_would_say = _matched_evidence(
        workflow=WorkflowType.EXPENSE,
        classification_code="SOFTWARE_LICENSE_COST",
    )

    projection = WorkbenchClassificationProjectionService(StaticClassificationEvidenceReader(persisted)).get_projection(
        review_id="review-1", company_id=7, review_version=1
    )

    assert changed_current_rules_would_say.classification_code == "SOFTWARE_LICENSE_COST"
    assert projection.workflow is WorkflowType.VENDOR_BILL
    assert projection.classification_code == "CLOUD_COST"


def test_projection_exposes_no_internal_raw_fields_or_ids() -> None:
    projection = WorkbenchClassificationProjectionService(
        StaticClassificationEvidenceReader(_conflict_evidence())
    ).get_projection(review_id="review-1", company_id=7, review_version=1)

    assert not hasattr(projection, "serialized_evidence")
    assert not hasattr(projection, "fingerprint")
    assert not hasattr(projection, "raw_json")
    assert not hasattr(projection, "database_id")
    assert all(not hasattr(rule, "rule_id") for rule in projection.conflicting_rules_summary)


def test_projection_architecture_depends_only_on_classification_evidence_reader() -> None:
    source = Path("app/application/workbench/classification_projection.py").read_text(encoding="utf-8")

    assert "ReviewClassificationEvidenceReader" in source
    forbidden = (
        "DecisionRuleRepository",
        "InvoiceDecisionRuleEngine",
        "OdooDecisionRuleRepository",
        "OdooDecisionRule",
        "DeterministicRuleEngine",
        "RuleEngine",
        "app.erp",
        "app.connectors",
        "uyumsoft",
        "runtime",
        "VendorBill",
        "CustomerInvoice",
        "search_read",
        ".classify(",
        "fuzzy",
        "levenshtein",
        "openai",
        "anthropic",
    )
    for token in forbidden:
        assert token not in source


def test_projection_contracts_import_no_infrastructure_or_rule_sources() -> None:
    source = "\n".join(
        Path(path).read_text(encoding="utf-8")
        for path in (
            "app/application/workbench/projection.py",
            "app/application/workbench/classification_projection.py",
            "app/application/workbench/__init__.py",
        )
    )

    forbidden = (
        "sqlalchemy",
        "app.models",
        "app.persistence",
        "OdooJson2Client",
        "DecisionRuleRepository",
        "InvoiceDecisionRuleEngine",
        "OdooDecisionRuleRepository",
        "action_post",
        "account.move",
    )
    for token in forbidden:
        assert token not in source


class StaticClassificationEvidenceReader:
    def __init__(self, evidence: ReviewClassificationEvidence) -> None:
        self._evidence = evidence
        self.calls: tuple[tuple[str, int, int], ...] = ()

    def get_classification_evidence(
        self,
        *,
        review_id: str,
        company_id: int,
        review_version: int,
    ) -> ReviewClassificationEvidence:
        self.calls = (*self.calls, (review_id, company_id, review_version))
        return self._evidence


class MissingClassificationEvidenceReader:
    def get_classification_evidence(
        self,
        *,
        review_id: str,
        company_id: int,
        review_version: int,
    ) -> ReviewClassificationEvidence:
        raise ReviewNotFoundError("Review classification evidence was not found.")


class MalformedClassificationEvidenceReader:
    def get_classification_evidence(
        self,
        *,
        review_id: str,
        company_id: int,
        review_version: int,
    ) -> ReviewClassificationEvidence:
        raise ReviewDataIntegrityError("Review classification evidence is invalid.")


def _matched_evidence(
    *,
    status: InvoiceClassificationStatus = InvoiceClassificationStatus.MATCHED,
    workflow: WorkflowType = WorkflowType.VENDOR_BILL,
    classification_code: str = "CLOUD_COST",
    require_review: bool = False,
    require_business_context: bool = True,
) -> ReviewClassificationEvidence:
    return ReviewClassificationEvidence(
        review_id="review-1",
        company_id=7,
        review_version=1,
        status=status,
        matched_rule_id="rule-cloud",
        matched_rule_code="CLOUD_COST_VENDOR_BILL",
        matched_rule_version=3,
        matched_rule_name="Cloud cost vendor bill",
        workflow=workflow,
        classification_code=classification_code,
        require_review=require_review,
        require_business_context=require_business_context,
    )


def _no_match_evidence() -> ReviewClassificationEvidence:
    return ReviewClassificationEvidence(
        review_id="review-1",
        company_id=7,
        review_version=1,
        status=InvoiceClassificationStatus.NO_MATCH,
    )


def _conflict_evidence() -> ReviewClassificationEvidence:
    return ReviewClassificationEvidence(
        review_id="review-1",
        company_id=7,
        review_version=1,
        status=InvoiceClassificationStatus.CONFLICT,
        conflicting_rules=(
            _rule_evidence(),
            _rule_evidence(
                rule_id="rule-software",
                rule_code="SOFTWARE_LICENSE_EXPENSE",
                rule_name="Software license expense",
                workflow=WorkflowType.EXPENSE,
                classification_code="SOFTWARE_LICENSE_COST",
                require_review=True,
                require_business_context=False,
            ),
        ),
    )


def _rule_evidence(
    *,
    rule_id: str = "rule-cloud",
    rule_code: str = "CLOUD_COST_VENDOR_BILL",
    rule_version: int = 3,
    rule_name: str = "Cloud cost vendor bill",
    workflow: WorkflowType = WorkflowType.VENDOR_BILL,
    classification_code: str = "CLOUD_COST",
    require_review: bool = False,
    require_business_context: bool = True,
) -> InvoiceClassificationRuleEvidence:
    return InvoiceClassificationRuleEvidence(
        rule_id=rule_id,
        rule_code=rule_code,
        rule_version=rule_version,
        rule_name=rule_name,
        workflow=workflow,
        classification_code=classification_code,
        require_review=require_review,
        require_business_context=require_business_context,
    )
