from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.application.rules import InvoiceClassificationRuleEvidence, InvoiceClassificationStatus
from app.application.workbench.evidence import (
    REVIEW_CLASSIFICATION_EVIDENCE_SCHEMA_VERSION,
    ReviewClassificationEvidence,
)
from app.application.workbench.exceptions import (
    ReviewDataIntegrityError,
    ReviewNotFoundError,
    ReviewPersistenceError,
    WorkbenchContractError,
)
from app.application.workflow import WorkflowType
from app.models.workbench_review_classification_evidence import WorkbenchReviewClassificationEvidence

SAFE_CLASSIFICATION_ERROR = "Review classification evidence could not be loaded safely."
SAFE_CLASSIFICATION_NOT_FOUND = "Review classification evidence was not found."
SAFE_CLASSIFICATION_INTEGRITY_ERROR = "Review classification evidence is invalid."


class SqlAlchemyReviewClassificationEvidenceReader:
    """Load version-pinned classification evidence from Hub persistence."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_classification_evidence(
        self,
        *,
        review_id: str,
        company_id: int,
        review_version: int,
    ) -> ReviewClassificationEvidence:
        _validate_query(review_id=review_id, company_id=company_id, review_version=review_version)
        try:
            records = tuple(
                self._session.scalars(
                    select(WorkbenchReviewClassificationEvidence)
                    .where(
                        WorkbenchReviewClassificationEvidence.review_id == review_id,
                        WorkbenchReviewClassificationEvidence.company_id == company_id,
                        WorkbenchReviewClassificationEvidence.review_version == review_version,
                    )
                    .order_by(WorkbenchReviewClassificationEvidence.id.asc())
                    .limit(2)
                )
            )
            if not records:
                raise ReviewNotFoundError(SAFE_CLASSIFICATION_NOT_FOUND)
            if len(records) > 1:
                raise ReviewDataIntegrityError(SAFE_CLASSIFICATION_INTEGRITY_ERROR)
            return classification_evidence_from_record(records[0])
        except (ReviewNotFoundError, ReviewDataIntegrityError):
            raise
        except SQLAlchemyError as exc:
            raise ReviewPersistenceError(SAFE_CLASSIFICATION_ERROR) from exc
        except (KeyError, TypeError, ValueError, WorkbenchContractError) as exc:
            raise ReviewDataIntegrityError(SAFE_CLASSIFICATION_INTEGRITY_ERROR) from exc


def serialize_classification_rule_evidence(rule: InvoiceClassificationRuleEvidence) -> dict[str, Any]:
    if not isinstance(rule, InvoiceClassificationRuleEvidence):
        raise ReviewDataIntegrityError(SAFE_CLASSIFICATION_INTEGRITY_ERROR)
    return {
        "rule_id": rule.rule_id,
        "rule_code": rule.rule_code,
        "rule_version": rule.rule_version,
        "rule_name": rule.rule_name,
        "workflow": rule.workflow.value if rule.workflow is not None else None,
        "classification_code": rule.classification_code,
        "require_review": rule.require_review,
        "require_business_context": rule.require_business_context,
    }


def deserialize_classification_rule_evidence(data: dict[str, Any]) -> InvoiceClassificationRuleEvidence:
    return InvoiceClassificationRuleEvidence(
        rule_id=_required_text(data.get("rule_id")),
        rule_code=_required_text(data.get("rule_code")),
        rule_version=_required_int(data.get("rule_version")),
        rule_name=_required_text(data.get("rule_name")),
        workflow=_optional_workflow(data.get("workflow")),
        classification_code=_optional_text(data.get("classification_code")),
        require_review=_required_bool(data.get("require_review")),
        require_business_context=_required_bool(data.get("require_business_context")),
    )


def classification_evidence_payload(evidence: ReviewClassificationEvidence) -> dict[str, Any]:
    if not isinstance(evidence, ReviewClassificationEvidence):
        raise ReviewDataIntegrityError(SAFE_CLASSIFICATION_INTEGRITY_ERROR)
    return {
        "review_id": evidence.review_id,
        "company_id": evidence.company_id,
        "review_version": evidence.review_version,
        "schema_version": evidence.schema_version,
        "status": evidence.status.value,
        "matched_rule_id": evidence.matched_rule_id,
        "matched_rule_code": evidence.matched_rule_code,
        "matched_rule_version": evidence.matched_rule_version,
        "matched_rule_name": evidence.matched_rule_name,
        "workflow": evidence.workflow.value if evidence.workflow is not None else None,
        "classification_code": evidence.classification_code,
        "require_review": evidence.require_review,
        "require_business_context": evidence.require_business_context,
        "conflicting_rules": [serialize_classification_rule_evidence(rule) for rule in evidence.conflicting_rules],
    }


def classification_evidence_from_record(
    record: WorkbenchReviewClassificationEvidence,
) -> ReviewClassificationEvidence:
    if record.schema_version != REVIEW_CLASSIFICATION_EVIDENCE_SCHEMA_VERSION:
        raise ReviewDataIntegrityError(SAFE_CLASSIFICATION_INTEGRITY_ERROR)
    return ReviewClassificationEvidence(
        review_id=record.review_id,
        company_id=record.company_id,
        review_version=record.review_version,
        schema_version=record.schema_version,
        status=InvoiceClassificationStatus(record.status),
        matched_rule_id=_optional_text(record.matched_rule_id),
        matched_rule_code=_optional_text(record.matched_rule_code),
        matched_rule_version=_optional_int(record.matched_rule_version),
        matched_rule_name=_optional_text(record.matched_rule_name),
        workflow=_optional_workflow(record.workflow),
        classification_code=_optional_text(record.classification_code),
        require_review=_required_bool(record.require_review),
        require_business_context=_required_bool(record.require_business_context),
        conflicting_rules=tuple(
            deserialize_classification_rule_evidence(_require_dict(rule))
            for rule in _require_list(record.conflicting_rules)
        ),
    )


def _validate_query(*, review_id: str, company_id: int, review_version: int) -> None:
    _required_text(review_id)
    if type(company_id) is not int or company_id <= 0:
        raise ReviewDataIntegrityError(SAFE_CLASSIFICATION_INTEGRITY_ERROR)
    if type(review_version) is not int or review_version <= 0:
        raise ReviewDataIntegrityError(SAFE_CLASSIFICATION_INTEGRITY_ERROR)


def _required_text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReviewDataIntegrityError(SAFE_CLASSIFICATION_INTEGRITY_ERROR)
    return value


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    return _required_text(value)


def _required_int(value: Any) -> int:
    if type(value) is not int:
        raise ReviewDataIntegrityError(SAFE_CLASSIFICATION_INTEGRITY_ERROR)
    return value


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return _required_int(value)


def _required_bool(value: Any) -> bool:
    if type(value) is not bool:
        raise ReviewDataIntegrityError(SAFE_CLASSIFICATION_INTEGRITY_ERROR)
    return value


def _optional_workflow(value: Any) -> WorkflowType | None:
    if value is None:
        return None
    return WorkflowType(_required_text(value))


def _require_dict(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReviewDataIntegrityError(SAFE_CLASSIFICATION_INTEGRITY_ERROR)
    return value


def _require_list(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise ReviewDataIntegrityError(SAFE_CLASSIFICATION_INTEGRITY_ERROR)
    return value
