"""Persistence for append-only review source-invoice corrections.

Reads the facts a correction's preconditions need (downstream state, the stored
source document) and applies one correction atomically together with its
``SOURCE_IDENTITY_CORRECTED`` review version advance. Never commits: the calling use
case owns the transaction boundary.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.application.workbench.dto import ReviewStatus
from app.application.workbench.exceptions import (
    ReviewNotFoundError,
    ReviewPersistenceError,
    ReviewVersionConflictError,
    WorkbenchContractError,
)
from app.application.workbench.reclassification import ReviewReclassificationProposal, ReviewReclassificationTrigger
from app.application.workbench.source_identity_correction import (
    ReviewSourceInvoiceCorrection,
    SourceInvoiceCorrectionField,
)
from app.db.base import Base
from app.models.execution_customer_billing_evidence import ExecutionCustomerBillingEvidence
from app.models.execution_source_invoice_evidence import ExecutionSourceInvoiceEvidence
from app.models.invoice_document import InvoiceDocument
from app.models.quotation_scenario_evidence import QuotationScenarioEvidence
from app.models.uyumsoft_invoice import UyumsoftInvoiceMetadata
from app.models.workbench_review_accounting_resolution import WorkbenchReviewAccountingResolution
from app.models.workbench_review_billing_evidence import WorkbenchReviewBillingEvidence
from app.models.workbench_review_decision import WorkbenchReviewDecision
from app.models.workbench_review_item import WorkbenchReviewItem
from app.models.workbench_review_one_off_vendor_retirement import WorkbenchReviewOneOffVendorRetirement
from app.models.workbench_review_product_identity_claim import WorkbenchReviewProductIdentityClaim
from app.models.workbench_review_product_remediation_reservation import (
    WorkbenchReviewProductRemediationReservation,
)
from app.models.workbench_review_purchase_purpose_resolution import WorkbenchReviewPurchasePurposeResolution
from app.models.workbench_review_reclassification import WorkbenchReviewReclassification
from app.models.workbench_review_supplier_remediation_effect import WorkbenchReviewSupplierRemediationEffect
from app.models.workbench_review_supplier_resolution import WorkbenchReviewSupplierResolution
from app.models.workbench_review_write_authorization import WorkbenchReviewWriteAuthorization
from app.models.workflow_execution import WorkflowExecution
from app.persistence.workbench_review_repository import (
    _classification_evidence_model,
    _evidence_model_from_review_evidence,
    _serialize_reason,
)
from app.persistence.workbench_review_source_invoice_reader import source_invoice_correction_model

SAFE_CORRECTION_PERSISTENCE_ERROR = "Review source correction could not be persisted safely."
#: Mirrors ``app.services.document_service.DOCUMENT_TYPE_UBL_XML`` (not imported: that
#: module depends on the Uyumsoft connector, which persistence must not).
UBL_XML_DOCUMENT_TYPE = "UBL_XML"

#: Review-scoped state that makes rewriting the review's effective source identity
#: unsafe, grouped by the precondition that reports it, as ``(model, review column)``.
DOWNSTREAM_STATE_TABLES: dict[str, tuple[tuple[type[Base], str], ...]] = {
    "decision": ((WorkbenchReviewDecision, "review_id"),),
    "write_authorization": ((WorkbenchReviewWriteAuthorization, "review_id"),),
    "execution": (
        (WorkflowExecution, "review_id"),
        (ExecutionSourceInvoiceEvidence, "review_id"),
        (ExecutionCustomerBillingEvidence, "review_id"),
        (QuotationScenarioEvidence, "review_id"),
    ),
    "downstream_remediation": (
        (WorkbenchReviewSupplierResolution, "review_id"),
        (WorkbenchReviewSupplierRemediationEffect, "review_id"),
        (WorkbenchReviewOneOffVendorRetirement, "review_id"),
        (WorkbenchReviewAccountingResolution, "review_id"),
        (WorkbenchReviewPurchasePurposeResolution, "review_id"),
        (WorkbenchReviewProductIdentityClaim, "owner_review_id"),
        (WorkbenchReviewProductRemediationReservation, "review_id"),
        (WorkbenchReviewBillingEvidence, "review_id"),
    ),
}


@dataclass(frozen=True, slots=True)
class ReviewSourceDocument:
    """The stored provider document a review was imported from."""

    document_id: int
    storage_key: str
    content_sha256: str


class SqlAlchemyReviewSourceInvoiceCorrectionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    # ------------------------------------------------------------------ reads

    def downstream_state_counts(self, *, review_id: str) -> dict[str, dict[str, int]]:
        """``{group: {table: row_count}}`` for every review-scoped downstream table."""

        try:
            return {
                group: {
                    model.__tablename__: int(
                        self._session.scalar(
                            select(func.count()).select_from(model).where(getattr(model, column) == review_id)
                        )
                        or 0
                    )
                    for model, column in models
                }
                for group, models in DOWNSTREAM_STATE_TABLES.items()
            }
        except SQLAlchemyError as exc:
            raise ReviewPersistenceError(SAFE_CORRECTION_PERSISTENCE_ERROR) from exc

    def find_source_documents(self, *, review_id: str, company_id: int) -> tuple[ReviewSourceDocument, ...]:
        """The stored UBL document(s) of the provider invoice this review was imported from.

        The link is the review's import idempotency key
        ``{provider}:company:{company_id}:{direction}:{identity_key}`` -- the same
        identity the provider metadata row is unique on -- never invoice numbers.
        """

        try:
            idempotency_key = self._session.scalar(
                select(WorkbenchReviewItem.idempotency_key).where(
                    WorkbenchReviewItem.review_id == review_id,
                    WorkbenchReviewItem.company_id == company_id,
                )
            )
            if idempotency_key is None:
                raise ReviewNotFoundError("Review item was not found.")
            parts = idempotency_key.split(":", 4)
            if len(parts) != 5 or parts[1] != "company" or parts[2] != str(company_id):
                return ()
            provider, _, _, direction, identity_key = parts
            records = self._session.execute(
                select(InvoiceDocument.id, InvoiceDocument.storage_key, InvoiceDocument.content_hash_sha256)
                .join(UyumsoftInvoiceMetadata, UyumsoftInvoiceMetadata.id == InvoiceDocument.invoice_id)
                .where(
                    UyumsoftInvoiceMetadata.provider == provider,
                    func.lower(UyumsoftInvoiceMetadata.direction) == direction,
                    UyumsoftInvoiceMetadata.identity_key == identity_key,
                    InvoiceDocument.document_type == UBL_XML_DOCUMENT_TYPE,
                )
                .order_by(InvoiceDocument.id)
            ).all()
        except SQLAlchemyError as exc:
            raise ReviewPersistenceError(SAFE_CORRECTION_PERSISTENCE_ERROR) from exc
        return tuple(
            ReviewSourceDocument(
                document_id=row.id, storage_key=row.storage_key, content_sha256=row.content_hash_sha256
            )
            for row in records
        )

    # ------------------------------------------------------------------ write

    def apply_source_invoice_correction(
        self,
        correction: ReviewSourceInvoiceCorrection,
        proposal: ReviewReclassificationProposal,
    ) -> None:
        """Atomically append the correction and advance the review ``N -> N+1``.

        In one nested transaction: insert the correction; guarded-UPDATE the review
        (only if still ``pending_review`` at ``N`` with the corrected field still at
        its old value) to the new supplier tax number / recalculated workflow /
        reasons / warnings and version ``N+1``; append the ``N+1`` classification
        (and, when executable, execution) evidence and the
        ``SOURCE_IDENTITY_CORRECTED`` reclassification event. Unlike a plain
        reclassification the version always advances, even when the recalculated
        classification is unchanged, because the effective source changed.
        """

        _require_matching(correction, proposal)
        item_values: dict[str, object] = {
            "workflow": proposal.new_workflow.value,
            "review_reasons": [_serialize_reason(reason) for reason in proposal.new_review_reasons],
            "warnings": [str(warning) for warning in proposal.new_warnings],
            "version": proposal.to_version,
            "updated_at": func.now(),
        }
        guards = [
            WorkbenchReviewItem.review_id == proposal.review_id,
            WorkbenchReviewItem.company_id == proposal.company_id,
            WorkbenchReviewItem.status == ReviewStatus.PENDING_REVIEW.value,
            WorkbenchReviewItem.version == proposal.expected_version,
        ]
        if correction.field_path is SourceInvoiceCorrectionField.SUPPLIER_TAX_NUMBER:
            item_values["supplier_tax_number"] = correction.new_value
            guards.append(
                WorkbenchReviewItem.supplier_tax_number.is_(None)
                if correction.old_value is None
                else WorkbenchReviewItem.supplier_tax_number == correction.old_value
            )
        try:
            current = self._session.scalar(
                select(WorkbenchReviewItem).where(
                    WorkbenchReviewItem.review_id == proposal.review_id,
                    WorkbenchReviewItem.company_id == proposal.company_id,
                )
            )
            if current is None:
                raise ReviewNotFoundError("Review item was not found.")
            event = WorkbenchReviewReclassification(
                review_id=proposal.review_id,
                company_id=proposal.company_id,
                from_version=proposal.expected_version,
                to_version=proposal.to_version,
                source_invoice_id=proposal.source_invoice_id,
                trigger=proposal.trigger.value,
                note=proposal.note,
                previous_workflow=current.workflow,
                previous_review_reasons=list(current.review_reasons or []),
                new_workflow=proposal.new_workflow.value,
                new_review_reasons=item_values["review_reasons"],
                matched_rule_code=proposal.matched_rule_code,
                matched_rule_id=proposal.matched_rule_id,
                executable=proposal.executable,
            )
            with self._session.begin_nested():
                result = self._session.execute(
                    update(WorkbenchReviewItem)
                    .where(*guards)
                    .values(**item_values)
                    .execution_options(synchronize_session=False)
                )
                if int(result.rowcount or 0) != 1:
                    raise ReviewVersionConflictError(
                        "Review changed concurrently: it is no longer pending at the expected version with the "
                        "uncorrected source value."
                    )
                self._session.add(source_invoice_correction_model(correction))
                self._session.add(event)
                if proposal.new_classification_evidence is not None:
                    self._session.add(_classification_evidence_model(proposal.new_classification_evidence))
                if proposal.new_execution_evidence is not None:
                    self._session.add(_evidence_model_from_review_evidence(proposal.new_execution_evidence))
                self._session.flush()
        except IntegrityError as exc:
            self._session.expire_all()
            raise ReviewVersionConflictError(
                "A correction or reclassification from this review version already exists."
            ) from exc
        except (ReviewNotFoundError, ReviewVersionConflictError):
            self._session.expire_all()
            raise
        except SQLAlchemyError as exc:
            self._session.expire_all()
            raise ReviewPersistenceError(SAFE_CORRECTION_PERSISTENCE_ERROR) from exc


def _require_matching(correction: ReviewSourceInvoiceCorrection, proposal: ReviewReclassificationProposal) -> None:
    if not isinstance(correction, ReviewSourceInvoiceCorrection):
        raise WorkbenchContractError("ReviewSourceInvoiceCorrection is required.")
    if not isinstance(proposal, ReviewReclassificationProposal):
        raise WorkbenchContractError("ReviewReclassificationProposal is required.")
    if proposal.trigger is not ReviewReclassificationTrigger.SOURCE_IDENTITY_CORRECTED:
        raise WorkbenchContractError("A source correction is recorded only with the SOURCE_IDENTITY_CORRECTED trigger.")
    if (
        correction.review_id != proposal.review_id
        or correction.company_id != proposal.company_id
        or correction.from_version != proposal.expected_version
        or correction.to_version != proposal.to_version
        or correction.source_invoice_id != proposal.source_invoice_id
    ):
        raise WorkbenchContractError("The correction and its reclassification must target the same review step.")


__all__ = [
    "DOWNSTREAM_STATE_TABLES",
    "ReviewSourceDocument",
    "SqlAlchemyReviewSourceInvoiceCorrectionRepository",
]
