from __future__ import annotations

from dataclasses import replace
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
from app.application.workbench.source_identity_correction import (
    ReviewSourceInvoiceCorrection,
    SourceInvoiceCorrectionField,
    SourceInvoiceCorrectionReason,
    apply_source_invoice_corrections,
)
from app.models.workbench_review_source_invoice_correction import WorkbenchReviewSourceInvoiceCorrection
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
    """Typed reader that reconstructs the *effective* source ``InternalInvoice`` for a review.

    The effective invoice is the immutable original snapshot with every audited
    ``workbench_review_source_invoice_corrections`` row of the review overlaid in
    ``to_version`` order. Every consumer (reclassification, supplier remediation,
    decisions, review evidence) therefore sees corrected source identity through this
    one reader; the original row itself is never modified. :meth:`get_original`
    returns the uncorrected snapshot.

    Reading has zero connector dependency: no Uyumsoft, no Odoo. Reviews created before this
    feature legitimately have no row and raise :class:`ReviewNotFoundError`.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, *, review_id: str, company_id: int) -> ReviewSourceInvoiceEvidence:
        original = self.get_original(review_id=review_id, company_id=company_id)
        corrections = self.find_corrections(review_id=review_id, company_id=company_id)
        if not corrections:
            return original
        return replace(original, invoice=apply_source_invoice_corrections(original.invoice, corrections))

    def find_corrections(self, *, review_id: str, company_id: int) -> tuple[ReviewSourceInvoiceCorrection, ...]:
        try:
            records = self._session.scalars(
                select(WorkbenchReviewSourceInvoiceCorrection)
                .where(
                    WorkbenchReviewSourceInvoiceCorrection.review_id == review_id,
                    WorkbenchReviewSourceInvoiceCorrection.company_id == company_id,
                )
                .order_by(WorkbenchReviewSourceInvoiceCorrection.to_version)
            ).all()
        except SQLAlchemyError as exc:
            raise ReviewPersistenceError(SAFE_SOURCE_INVOICE_ERROR) from exc
        try:
            return tuple(source_invoice_correction_from_model(record) for record in records)
        except (TypeError, ValueError, WorkbenchContractError) as exc:
            raise ReviewDataIntegrityError(SAFE_SOURCE_INVOICE_INTEGRITY_ERROR) from exc

    def get_original(self, *, review_id: str, company_id: int) -> ReviewSourceInvoiceEvidence:
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


def source_invoice_correction_from_model(
    record: WorkbenchReviewSourceInvoiceCorrection,
) -> ReviewSourceInvoiceCorrection:
    return ReviewSourceInvoiceCorrection(
        review_id=record.review_id,
        company_id=record.company_id,
        from_version=record.from_version,
        to_version=record.to_version,
        source_invoice_id=record.source_invoice_id,
        field_path=SourceInvoiceCorrectionField(record.field_path),
        old_value=record.old_value,
        new_value=record.new_value,
        source_document_id=record.source_document_id,
        source_document_sha256=record.source_document_sha256,
        reason=SourceInvoiceCorrectionReason(record.reason),
        approved_by=record.approved_by,
        created_at=record.created_at,
    )


def source_invoice_correction_model(
    correction: ReviewSourceInvoiceCorrection,
) -> WorkbenchReviewSourceInvoiceCorrection:
    return WorkbenchReviewSourceInvoiceCorrection(
        review_id=correction.review_id,
        company_id=correction.company_id,
        from_version=correction.from_version,
        to_version=correction.to_version,
        source_invoice_id=correction.source_invoice_id,
        field_path=correction.field_path.value,
        old_value=correction.old_value,
        new_value=correction.new_value,
        source_document_id=correction.source_document_id,
        source_document_sha256=correction.source_document_sha256,
        reason=correction.reason.value,
        approved_by=correction.approved_by,
    )


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return tuple(sorted((key, _canonical(item)) for key, item in value.items()))
    if isinstance(value, list | tuple):
        return tuple(_canonical(item) for item in value)
    return value
