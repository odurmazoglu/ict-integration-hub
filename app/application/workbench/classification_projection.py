from __future__ import annotations

from app.application.rules import InvoiceClassificationStatus
from app.application.workbench.evidence import ReviewClassificationEvidence
from app.application.workbench.exceptions import ReviewNotFoundError
from app.application.workbench.ports import ReviewClassificationEvidenceReader
from app.application.workbench.projection import (
    BUSINESS_CONTEXT_BADGES,
    CLASSIFICATION_STATUS_BADGES,
    REVIEW_REQUIRED_BADGES,
    WORKFLOW_DISPLAY_NAMES,
    WorkbenchClassificationConflictRuleProjection,
    WorkbenchClassificationProjection,
)
from app.application.workflow import WorkflowType

NO_MATCH_PLACEHOLDER = "No Decision Rule matched."
MISSING_EVIDENCE_PLACEHOLDER = "Classification evidence is not available for this review version."


class WorkbenchClassificationProjectionService:
    """Project pinned review classification evidence into a user-safe Workbench view."""

    def __init__(self, reader: ReviewClassificationEvidenceReader) -> None:
        self._reader = reader

    def get_projection(
        self,
        *,
        review_id: str,
        company_id: int,
        review_version: int,
    ) -> WorkbenchClassificationProjection:
        try:
            evidence = self._reader.get_classification_evidence(
                review_id=review_id,
                company_id=company_id,
                review_version=review_version,
            )
        except ReviewNotFoundError:
            return _missing_projection()
        return _projection_from_evidence(evidence)


def _projection_from_evidence(evidence: ReviewClassificationEvidence) -> WorkbenchClassificationProjection:
    if evidence.status is InvoiceClassificationStatus.NO_MATCH:
        return WorkbenchClassificationProjection(
            status=evidence.status.value,
            status_label="No Match",
            status_badge=CLASSIFICATION_STATUS_BADGES[evidence.status.value],
            placeholder=NO_MATCH_PLACEHOLDER,
        )
    if evidence.status is InvoiceClassificationStatus.CONFLICT:
        rules = tuple(
            WorkbenchClassificationConflictRuleProjection(
                rule_name=rule.rule_name,
                rule_code=rule.rule_code,
                rule_version=rule.rule_version,
                workflow=rule.workflow,
                workflow_display=_workflow_display(rule.workflow),
                classification_code=rule.classification_code,
            )
            for rule in evidence.conflicting_rules
        )
        return WorkbenchClassificationProjection(
            status=evidence.status.value,
            status_label="Conflict",
            status_badge=CLASSIFICATION_STATUS_BADGES[evidence.status.value],
            conflict=True,
            conflict_label="Conflict",
            conflict_summary=f"{len(rules)} matching rules produced different actions.",
            conflicting_rules_summary=rules,
        )
    return WorkbenchClassificationProjection(
        status=evidence.status.value,
        status_label=_status_label(evidence.status),
        status_badge=CLASSIFICATION_STATUS_BADGES[evidence.status.value],
        workflow=evidence.workflow,
        workflow_display=_workflow_display(evidence.workflow),
        classification_code=evidence.classification_code,
        matched_rule_name=evidence.matched_rule_name,
        matched_rule_code=evidence.matched_rule_code,
        matched_rule_version=evidence.matched_rule_version,
        require_review=evidence.require_review,
        require_review_label=_yes_no(evidence.require_review),
        require_review_badge=REVIEW_REQUIRED_BADGES[evidence.require_review],
        require_business_context=evidence.require_business_context,
        require_business_context_label=_required_label(evidence.require_business_context),
        require_business_context_badge=BUSINESS_CONTEXT_BADGES[evidence.require_business_context],
    )


def _missing_projection() -> WorkbenchClassificationProjection:
    return WorkbenchClassificationProjection(
        status="UNAVAILABLE",
        status_label="Unavailable",
        status_badge=CLASSIFICATION_STATUS_BADGES["UNAVAILABLE"],
        placeholder=MISSING_EVIDENCE_PLACEHOLDER,
    )


def _status_label(status: InvoiceClassificationStatus) -> str:
    if status is InvoiceClassificationStatus.MATCHED:
        return "Matched"
    if status is InvoiceClassificationStatus.REVIEW_REQUIRED:
        return "Review Required"
    if status is InvoiceClassificationStatus.NO_MATCH:
        return "No Match"
    if status is InvoiceClassificationStatus.CONFLICT:
        return "Conflict"
    raise AssertionError("unsupported classification status")


def _workflow_display(workflow: WorkflowType | None) -> str | None:
    if workflow is None:
        return None
    return WORKFLOW_DISPLAY_NAMES[workflow]


def _yes_no(value: bool) -> str:
    return "Yes" if value else "No"


def _required_label(value: bool) -> str:
    return "Required" if value else "Not Required"
