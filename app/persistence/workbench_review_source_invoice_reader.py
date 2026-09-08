from __future__ import annotations

from decimal import InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.application.execution.exceptions import ExecutionSourceInvoiceError
from app.application.workbench.evidence import (
    REVIEW_SOURCE_INVOICE_EVIDENCE_SCHEMA_VERSION,
    ReviewSourceInvoiceEvidence,
)
from app.application.workbench.exceptions import (
    ReviewDataIntegrityError,
    ReviewNotFoundError,
    ReviewPersistenceError,
    WorkbenchContractError,
)
from app.models.workbench_review_source_invoice_evidence import WorkbenchReviewSourceInvoiceEvidence
from app.persistence.execution_source_invoice_reader import _invoice_from_data, _invoice_to_data

SAFE_SOURCE_INVOICE_ERROR = "Review source invoice evidence could not be loaded safely."
SAFE_SOURCE_INVOICE_NOT_FOUND = "Review source invoice evidence was not found."
SAFE_SOURCE_INVOICE_INTEGRITY_ERROR = "Review source invoice evidence is invalid."


def serialize_review_source_invoice_evidence(evidence: ReviewSourceInvoiceEvidence) -> dict[str, Any]:
    """Canonical persistence payload. ``invoice`` reuses the single lossless InternalInvoice serializer."""

    return {
        "review_id": evidence.review_id,
        "company_id": evidence.company_id,
        "review_version": evidence.review_version,
        "source_invoice_id": evidence.source_invoice_id,
        "schema_version": REVIEW_SOURCE_INVOICE_EVIDENCE_SCHEMA_VERSION,
        "invoice": _invoice_to_data(evidence.invoice),
    }


def deserialize_review_source_invoice_evidence(data: dict[str, Any]) -> ReviewSourceInvoiceEvidence:
    return ReviewSourceInvoiceEvidence(
        review_id=str(data["review_id"]),
        company_id=int(data["company_id"]),
        review_version=int(data["review_version"]),
        source_invoice_id=str(data["source_invoice_id"]),
        invoice=_invoice_from_data(data["invoice"]),
    )


def review_source_invoice_evidence_fingerprint(evidence: ReviewSourceInvoiceEvidence) -> tuple[Any, ...]:
    payload = serialize_review_source_invoice_evidence(evidence)
    return (
        payload["review_id"],
        payload["company_id"],
        payload["review_version"],
        payload["source_invoice_id"],
        _canonical(payload["invoice"]),
    )


def review_source_invoice_evidence_fingerprint_from_model(
    record: WorkbenchReviewSourceInvoiceEvidence,
) -> tuple[Any, ...]:
    return (
        record.review_id,
        record.company_id,
        record.review_version,
        record.source_invoice_id,
        _canonical(record.invoice),
    )


def model_from_review_source_invoice_evidence(
    evidence: ReviewSourceInvoiceEvidence,
) -> WorkbenchReviewSourceInvoiceEvidence:
    return WorkbenchReviewSourceInvoiceEvidence(
        review_id=evidence.review_id,
        company_id=evidence.company_id,
        review_version=evidence.review_version,
        source_invoice_id=evidence.source_invoice_id,
        schema_version=REVIEW_SOURCE_INVOICE_EVIDENCE_SCHEMA_VERSION,
        invoice=_invoice_to_data(evidence.invoice),
    )


class SqlAlchemyReviewSourceInvoiceEvidenceReader:
    """Typed reader that reconstructs the immutable source ``InternalInvoice`` for a review.

    Reading has zero connector dependency: no Uyumsoft, no Odoo. Reviews created before this
    feature legitimately have no row and raise :class:`ReviewNotFoundError`.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, *, review_id: str, company_id: int) -> ReviewSourceInvoiceEvidence:
        if not isinstance(review_id, str) or not review_id.strip():
            raise WorkbenchContractError("review_id is required.")
        if type(company_id) is not int or company_id <= 0:
            raise WorkbenchContractError("company_id must be positive.")
        try:
            record = self._session.scalar(
                select(WorkbenchReviewSourceInvoiceEvidence).where(
                    WorkbenchReviewSourceInvoiceEvidence.review_id == review_id,
                    WorkbenchReviewSourceInvoiceEvidence.company_id == company_id,
                )
            )
        except SQLAlchemyError as exc:
            raise ReviewPersistenceError(SAFE_SOURCE_INVOICE_ERROR) from exc
        if record is None:
            raise ReviewNotFoundError(SAFE_SOURCE_INVOICE_NOT_FOUND)
        if record.schema_version != REVIEW_SOURCE_INVOICE_EVIDENCE_SCHEMA_VERSION:
            raise ReviewDataIntegrityError(SAFE_SOURCE_INVOICE_INTEGRITY_ERROR)
        try:
            return deserialize_review_source_invoice_evidence(
                {
                    "review_id": record.review_id,
                    "company_id": record.company_id,
                    "review_version": record.review_version,
                    "source_invoice_id": record.source_invoice_id,
                    "invoice": record.invoice,
                }
            )
        except (
            KeyError,
            TypeError,
            ValueError,
            InvalidOperation,
            WorkbenchContractError,
            ExecutionSourceInvoiceError,
        ) as exc:
            raise ReviewDataIntegrityError(SAFE_SOURCE_INVOICE_INTEGRITY_ERROR) from exc

    def get_invoice(self, *, review_id: str, company_id: int):
        return self.get(review_id=review_id, company_id=company_id).invoice


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return tuple(sorted((key, _canonical(item)) for key, item in value.items()))
    if isinstance(value, list | tuple):
        return tuple(_canonical(item) for item in value)
    return value
