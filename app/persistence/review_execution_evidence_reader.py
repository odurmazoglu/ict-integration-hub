from __future__ import annotations

from decimal import InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.application.execution.contracts import ExecutionSourceInvoice
from app.application.execution.exceptions import (
    ExecutionSourceInvoiceError,
    ExecutionSourceInvoiceIntegrityError,
    ExecutionSourceInvoiceNotFoundError,
)
from app.application.workbench.evidence import ReviewExecutionEvidence
from app.application.workbench.exceptions import WorkbenchContractError
from app.models.workbench_review_execution_evidence import WorkbenchReviewExecutionEvidence
from app.persistence.execution_source_invoice_reader import (
    EXECUTION_SOURCE_EVIDENCE_SCHEMA_VERSION,
    SAFE_SOURCE_ERROR,
    SAFE_SOURCE_INTEGRITY_ERROR,
    SAFE_SOURCE_NOT_FOUND,
    deserialize_execution_source_invoice_payload,
)


class SqlAlchemyReviewExecutionEvidenceReader:
    """Load Stage 1 review-version evidence for accepted decision submission."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_evidence(
        self,
        *,
        review_id: str,
        company_id: int,
        expected_version: int,
    ) -> ExecutionSourceInvoice:
        _validate_query(review_id=review_id, company_id=company_id, expected_version=expected_version)
        try:
            record = self._evidence_record(
                review_id=review_id,
                company_id=company_id,
                expected_version=expected_version,
            )
            source = _source_from_review_evidence(record, decision_version=expected_version + 1)
            _validate_stage_one_linkage(
                record=record,
                source=source,
                expected_version=expected_version,
            )
            return source
        except ExecutionSourceInvoiceError:
            raise
        except SQLAlchemyError as exc:
            raise ExecutionSourceInvoiceError(SAFE_SOURCE_ERROR) from exc
        except (InvalidOperation, KeyError, TypeError, ValueError, WorkbenchContractError) as exc:
            raise ExecutionSourceInvoiceIntegrityError(SAFE_SOURCE_INTEGRITY_ERROR) from exc

    def _evidence_record(
        self,
        *,
        review_id: str,
        company_id: int,
        expected_version: int,
    ) -> WorkbenchReviewExecutionEvidence:
        records = tuple(
            self._session.scalars(
                select(WorkbenchReviewExecutionEvidence)
                .where(
                    WorkbenchReviewExecutionEvidence.review_id == review_id,
                    WorkbenchReviewExecutionEvidence.company_id == company_id,
                    WorkbenchReviewExecutionEvidence.review_version == expected_version,
                )
                .order_by(WorkbenchReviewExecutionEvidence.id.asc())
                .limit(2)
            )
        )
        if not records:
            raise ExecutionSourceInvoiceNotFoundError(SAFE_SOURCE_NOT_FOUND)
        if len(records) > 1:
            raise ExecutionSourceInvoiceIntegrityError(SAFE_SOURCE_INTEGRITY_ERROR)
        return records[0]


def _validate_query(*, review_id: str, company_id: int, expected_version: int) -> None:
    if not isinstance(review_id, str) or not review_id.strip():
        raise ExecutionSourceInvoiceIntegrityError(SAFE_SOURCE_INTEGRITY_ERROR)
    if type(company_id) is not int or company_id <= 0:
        raise ExecutionSourceInvoiceIntegrityError(SAFE_SOURCE_INTEGRITY_ERROR)
    if type(expected_version) is not int or expected_version <= 0:
        raise ExecutionSourceInvoiceIntegrityError(SAFE_SOURCE_INTEGRITY_ERROR)


def _source_from_review_evidence(
    record: WorkbenchReviewExecutionEvidence,
    *,
    decision_version: int,
) -> ExecutionSourceInvoice:
    if record.schema_version != EXECUTION_SOURCE_EVIDENCE_SCHEMA_VERSION:
        raise ExecutionSourceInvoiceIntegrityError(SAFE_SOURCE_INTEGRITY_ERROR)
    return deserialize_execution_source_invoice_payload(
        {
            "review_id": record.review_id,
            "company_id": record.company_id,
            "decision_version": decision_version,
            "source_invoice_id": record.source_invoice_id,
            "invoice": _require_dict(record.invoice),
            "partner_match": _require_dict(record.partner_match),
            "product_match": _require_dict(record.product_match),
            "tax_match": _require_dict(record.tax_match),
        }
    )


def _validate_stage_one_linkage(
    *,
    record: WorkbenchReviewExecutionEvidence,
    source: ExecutionSourceInvoice,
    expected_version: int,
) -> None:
    if record.review_id != source.review_id:
        raise ExecutionSourceInvoiceIntegrityError(SAFE_SOURCE_INTEGRITY_ERROR)
    if record.company_id != source.company_id:
        raise ExecutionSourceInvoiceIntegrityError(SAFE_SOURCE_INTEGRITY_ERROR)
    if record.review_version != expected_version:
        raise ExecutionSourceInvoiceIntegrityError(SAFE_SOURCE_INTEGRITY_ERROR)
    if source.decision_version != expected_version + 1:
        raise ExecutionSourceInvoiceIntegrityError(SAFE_SOURCE_INTEGRITY_ERROR)
    ReviewExecutionEvidence(
        review_id=source.review_id,
        company_id=source.company_id,
        review_version=expected_version,
        source_invoice_id=source.source_invoice_id,
        invoice=source.invoice,
        partner_match=source.partner_match,
        product_match=source.product_match,
        tax_match=source.tax_match,
    )


def _require_dict(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ExecutionSourceInvoiceIntegrityError(SAFE_SOURCE_INTEGRITY_ERROR)
    return value
