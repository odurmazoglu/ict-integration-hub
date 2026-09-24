"""Operator-facing review-scoped purchase-purpose orchestration (P0-PROD-15T).

Records *why* a purchase was made -- an immutable business fact, pinned to one
exact review version. Deliberately inert with respect to classification: unlike
``SubmitReviewAccountingResolutionUseCase``, recording a purpose never triggers
reclassification and never advances the review version by itself. It exists
only so ``SubmitReviewAccountingResolutionUseCase`` has an explicit, auditable
precondition to check before accepting an accounting treatment for a
mixed-purpose supplier's review.
"""

from __future__ import annotations

from app.application.expense_mapping.predicates import invoice_has_product_identifier
from app.application.services import UnitOfWork
from app.application.workbench.exceptions import (
    PurchasePurposeConflictError,
    PurchasePurposeEligibilityError,
    ReviewStateConflictError,
    ReviewVersionConflictError,
    WorkbenchContractError,
)
from app.application.workbench.ports import (
    PurchasePurposeResolutionWriter,
    ReviewQueueReader,
    ReviewSourceInvoiceEvidenceReader,
)
from app.application.workbench.purchase_purpose import (
    PurchasePurpose,
    PurchasePurposeResolution,
    PurchasePurposeSubmissionResult,
    SubmitPurchasePurposeCommand,
)
from app.application.workbench.queries import ReviewDetailQuery
from app.application.workflow import ManualReviewReason, ManualReviewReasonCode


class SubmitPurchasePurposeUseCase:
    """Application boundary for one authenticated purchase-purpose statement."""

    def __init__(
        self,
        *,
        review_reader: ReviewQueueReader,
        source_invoice_reader: ReviewSourceInvoiceEvidenceReader,
        purpose_writer: PurchasePurposeResolutionWriter,
        unit_of_work: UnitOfWork,
    ) -> None:
        self._review_reader = review_reader
        self._source_invoice_reader = source_invoice_reader
        self._purpose_writer = purpose_writer
        self._unit_of_work = unit_of_work

    def execute(self, command: SubmitPurchasePurposeCommand) -> PurchasePurposeSubmissionResult:
        if not isinstance(command, SubmitPurchasePurposeCommand):
            raise WorkbenchContractError("A canonical SubmitPurchasePurposeCommand is required.")

        review = self._review_reader.get_review_item(
            ReviewDetailQuery(review_id=command.review_id, company_id=command.company_id)
        )

        if review.version > command.expected_version:
            resumed = self._resume_if_already_applied(command)
            if resumed is not None:
                return resumed
            raise ReviewVersionConflictError("The review version does not match expected_version.")
        if review.version != command.expected_version:
            raise ReviewVersionConflictError("The review version does not match expected_version.")
        self._require_eligible(review, command.purchase_purpose)

        existing = self._purpose_writer.find_purchase_purpose_resolution(
            review_id=command.review_id,
            company_id=command.company_id,
            review_version=command.expected_version,
        )
        if existing is not None:
            if existing.purchase_purpose != command.purchase_purpose:
                raise PurchasePurposeConflictError(
                    "A different purchase-purpose resolution already exists for this review version."
                )
            return self._result(existing, already_applied=True)

        source = self._source_invoice_reader.get(review_id=command.review_id, company_id=command.company_id)
        if command.purchase_purpose is PurchasePurpose.RESALE:
            _require_product_shaped(source.invoice)
        resolution = PurchasePurposeResolution(
            review_id=command.review_id,
            company_id=command.company_id,
            review_version=command.expected_version,
            source_invoice_id=source.source_invoice_id,
            purchase_purpose=command.purchase_purpose,
            approved_by=command.approved_by,
            note=command.note,
        )
        try:
            created = self._purpose_writer.create_purchase_purpose_resolution(resolution)
            self._unit_of_work.commit()
        except BaseException:
            self._unit_of_work.rollback()
            raise
        return self._result(created, already_applied=False)

    def _require_eligible(self, review, purchase_purpose: PurchasePurpose) -> None:
        from app.application.workbench.dto import ReviewStatus

        if review.status is not ReviewStatus.PENDING_REVIEW:
            raise ReviewStateConflictError("The review is not pending review.")
        if purchase_purpose is PurchasePurpose.RESALE:
            # P0-PROD-18E-1B: RESALE is product-shaped, never operating-expense-shaped.
            # Its product identity is checked at decision acceptance, not here.
            return
        if not _has_operating_expense_reason(review.review_reasons):
            raise PurchasePurposeEligibilityError(
                "The review does not currently carry an operating-expense-shaped reason; "
                "there is nothing to explain a purchase purpose for."
            )

    def _resume_if_already_applied(
        self, command: SubmitPurchasePurposeCommand
    ) -> PurchasePurposeSubmissionResult | None:
        existing = self._purpose_writer.find_purchase_purpose_resolution(
            review_id=command.review_id,
            company_id=command.company_id,
            review_version=command.expected_version,
        )
        if existing is None or existing.purchase_purpose != command.purchase_purpose:
            return None
        return self._result(existing, already_applied=True)

    def _result(
        self, resolution: PurchasePurposeResolution, *, already_applied: bool
    ) -> PurchasePurposeSubmissionResult:
        return PurchasePurposeSubmissionResult(
            review_id=resolution.review_id,
            company_id=resolution.company_id,
            review_version=resolution.review_version,
            purchase_purpose=resolution.purchase_purpose,
            already_applied=already_applied,
            safe_message=(
                "This purchase purpose was already recorded for this review version."
                if already_applied
                else "Purchase purpose recorded. No classification changed."
            ),
        )


def _require_product_shaped(invoice) -> None:
    """RESALE needs at least one line carrying a product identifier (P0-PROD-18E-1B).

    Uses only the review's own immutable source invoice -- no product match, no
    selected product and no Odoo read are required to record the purpose.
    """

    if not invoice_has_product_identifier(invoice):
        raise PurchasePurposeEligibilityError(
            "RESALE requires a product-shaped review: no invoice line carries a buyer item code, seller item code "
            "or barcode."
        )


def _has_operating_expense_reason(reasons: tuple[ManualReviewReason, ...]) -> bool:
    return any(
        reason.code
        in (
            ManualReviewReasonCode.OPERATING_EXPENSE_MAPPING_REQUIRED,
            ManualReviewReasonCode.OPERATING_EXPENSE_MAPPING_AMBIGUOUS,
        )
        for reason in reasons
    )


__all__ = ["SubmitPurchasePurposeUseCase"]
