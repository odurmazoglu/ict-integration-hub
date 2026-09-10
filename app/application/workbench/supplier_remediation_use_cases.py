"""Explicit supplier remediation orchestration for a SUPPLIER_NOT_FOUND review.

Ties together the pieces built in P0-3D2A..C (+ the controlled writer from
PR #129) without ever manufacturing a matched ``PartnerMatchResult``:

    eligibility check
    -> load immutable ReviewSourceInvoiceEvidence
    -> reserve the operator's SupplierResolution intent (before any Odoo write)
    -> MATCH_EXISTING: validate the selected partner's exact VAT
       CREATE_PERMANENT_SUPPLIER: derive name/VAT from source, call the gated
       controlled writer, then validate the resulting partner
       USE_ONE_OFF_SUPPLIER: record intent only; no partner, no reclassification
    -> persist the immutable remediation effect (effective partner + write status)
    -> trigger the existing SUPPLIER_RESOLUTION reclassification (N -> N+1), which
       reruns the real deterministic PartnerMatchingEngine against current Odoo state
    -> report the post-reclassification workflow/reasons; never claim success while
       SUPPLIER_NOT_FOUND is still present.

This use case depends only on ports / other use cases -- never on a concrete
Odoo client.
"""

from __future__ import annotations

from typing import Protocol

from app.application.commands.supplier_partner import CreateSupplierPartnerCommand
from app.application.dto.supplier_partner import SupplierPartnerWriteStatus
from app.application.exceptions import ApplicationError
from app.application.ports.supplier_partner_writer import SupplierPartnerWriter
from app.application.workbench.exceptions import (
    ReviewStateConflictError,
    ReviewVersionConflictError,
    SupplierResolutionConflictError,
    SupplierResolutionContractError,
    SupplierResolutionDataIntegrityError,
    SupplierResolutionError,
    SupplierResolutionNotFoundError,
)
from app.application.workbench.ports import (
    ReviewQueueReader,
    ReviewSourceInvoiceEvidenceReader,
    SupplierRemediationEffectWriter,
    SupplierResolutionWriter,
)
from app.application.workbench.queries import ReviewDetailQuery
from app.application.workbench.reclassification import ReclassifyReviewCommand, ReviewReclassificationTrigger
from app.application.workbench.supplier_remediation import (
    ResolveWorkbenchSupplierCommand,
    SupplierPartnerWriteEffectStatus,
    SupplierRemediationEffect,
    SupplierRemediationResult,
    SupplierRemediationStatus,
)
from app.application.workbench.supplier_resolution import (
    SupplierResolution,
    SupplierResolutionMode,
    SupplierResolutionValidationStatus,
    normalize_supplier_vat,
)
from app.application.workbench.supplier_resolution_use_cases import ValidateSupplierResolutionUseCase
from app.application.workflow import ManualReviewReason, ManualReviewReasonCode

SAFE_SUPPLIER_REMEDIATION_ERROR = "Supplier remediation failed."


class SupplierReclassifier(Protocol):
    """Structural type for the P0-3D2B ``ReclassifyWorkbenchReviewUseCase``.

    Kept structural so this module never imports ``app.application.use_cases``
    (which imports this package), avoiding a package-init import cycle.
    """

    async def execute(self, command: ReclassifyReviewCommand): ...


class ResolveWorkbenchSupplierUseCase:
    """Application boundary for one authenticated supplier-remediation decision."""

    def __init__(
        self,
        *,
        review_reader: ReviewQueueReader,
        source_invoice_reader: ReviewSourceInvoiceEvidenceReader,
        resolution_validator: ValidateSupplierResolutionUseCase,
        resolution_writer: SupplierResolutionWriter,
        remediation_effect_writer: SupplierRemediationEffectWriter,
        supplier_partner_writer: SupplierPartnerWriter,
        reclassifier: SupplierReclassifier,
    ) -> None:
        self._review_reader = review_reader
        self._source_invoice_reader = source_invoice_reader
        self._resolution_validator = resolution_validator
        self._resolution_writer = resolution_writer
        self._remediation_effect_writer = remediation_effect_writer
        self._supplier_partner_writer = supplier_partner_writer
        self._reclassifier = reclassifier

    async def execute(self, command: ResolveWorkbenchSupplierCommand) -> SupplierRemediationResult:
        if not isinstance(command, ResolveWorkbenchSupplierCommand):
            raise SupplierResolutionContractError("A canonical ResolveWorkbenchSupplierCommand is required.")

        review = self._review_reader.get_review_item(
            ReviewDetailQuery(review_id=command.review_id, company_id=command.company_id)
        )

        existing = self._existing_resolution(command)
        if existing is not None:
            self._require_matching_intent(existing, command)
            return await self._resume_or_return_applied(command, review, existing)

        self._require_pending_supplier_not_found(review, command)
        source = self._source_invoice_reader.get(review_id=command.review_id, company_id=command.company_id)

        if command.mode is SupplierResolutionMode.USE_ONE_OFF_SUPPLIER:
            return self._resolve_one_off(command, review, source)

        intent = self._reserve_intent(command, source)
        return await self._complete(command, review, source, intent, already_applied=False)

    # ------------------------------------------------------------------ eligibility

    def _require_pending_supplier_not_found(
        self,
        review,
        command: ResolveWorkbenchSupplierCommand,
    ) -> None:
        from app.application.workbench.dto import ReviewStatus

        if review.status is not ReviewStatus.PENDING_REVIEW:
            raise ReviewStateConflictError("The review is not pending review.")
        if review.version != command.expected_version:
            raise ReviewVersionConflictError("The review version does not match expected_version.")
        if not _has_supplier_not_found(review.review_reasons):
            raise SupplierResolutionContractError(
                "The review no longer carries SUPPLIER_NOT_FOUND; there is nothing to remediate."
            )

    # ------------------------------------------------------------------ one-off

    def _resolve_one_off(
        self,
        command: ResolveWorkbenchSupplierCommand,
        review,
        source,
    ) -> SupplierRemediationResult:
        resolution = self._resolution(command, source, resolved_partner_id=None)
        validation = self._resolution_validator.execute(resolution)
        if validation.status is not SupplierResolutionValidationStatus.ONE_OFF_EXECUTION_NOT_SUPPORTED:
            raise SupplierResolutionDataIntegrityError("Unexpected one-off supplier resolution validation status.")
        self._resolution_writer.create_supplier_resolution(resolution)
        return SupplierRemediationResult(
            review_id=command.review_id,
            company_id=command.company_id,
            mode=command.mode,
            status=SupplierRemediationStatus.ONE_OFF_EXECUTION_NOT_SUPPORTED,
            previous_version=review.version,
            current_version=review.version,
            current_workflow=review.workflow,
            current_review_reasons=review.review_reasons,
            effective_partner_id=None,
            partner_write_status=None,
            reclassified=False,
            already_applied=False,
            workbench_republished=False,
            safe_message=(
                "One-off supplier decision recorded. Execution against a shared one-off partner is deferred; "
                "the review is not reclassified."
            ),
        )

    # ------------------------------------------------------------------ reservation

    def _reserve_intent(
        self,
        command: ResolveWorkbenchSupplierCommand,
        source,
    ) -> SupplierResolution:
        resolved_partner_id = (
            command.resolved_partner_id if command.mode is SupplierResolutionMode.MATCH_EXISTING else None
        )
        if command.mode is SupplierResolutionMode.MATCH_EXISTING:
            validation = self._resolution_validator.execute(
                self._resolution(command, source, resolved_partner_id=resolved_partner_id)
            )
            if validation.status is not SupplierResolutionValidationStatus.VALID:
                raise SupplierResolutionDataIntegrityError("Unexpected MATCH_EXISTING resolution validation status.")
        # Reserve the operator's intent BEFORE any irreversible Odoo write.
        return self._resolution_writer.create_supplier_resolution(
            self._resolution(command, source, resolved_partner_id=resolved_partner_id)
        )

    # ------------------------------------------------------------------ completion

    async def _complete(
        self,
        command: ResolveWorkbenchSupplierCommand,
        review,
        source,
        intent: SupplierResolution,
        *,
        already_applied: bool,
    ) -> SupplierRemediationResult:
        source_vat = normalize_supplier_vat(source.invoice.supplier.tax_number)

        if command.mode is SupplierResolutionMode.CREATE_PERMANENT_SUPPLIER:
            write_result = await self._create_permanent_partner(command, source)
            effective_partner_id = write_result.partner_id
            write_status = (
                SupplierPartnerWriteEffectStatus.CREATED
                if write_result.status is SupplierPartnerWriteStatus.CREATED
                else SupplierPartnerWriteEffectStatus.ALREADY_EXISTS
            )
            # Re-validate the created/existing partner against the immutable source (belt and suspenders).
            self._resolution_validator.execute(
                self._resolution(command, source, resolved_partner_id=effective_partner_id, force_mode_match=True)
            )
        else:
            effective_partner_id = command.resolved_partner_id
            write_status = SupplierPartnerWriteEffectStatus.SELECTED

        effect = self._remediation_effect_writer.create_remediation_effect(
            SupplierRemediationEffect(
                review_id=command.review_id,
                company_id=command.company_id,
                review_version=command.expected_version,
                source_invoice_id=source.source_invoice_id,
                mode=command.mode,
                resolved_partner_id=effective_partner_id,
                partner_write_status=write_status,
                source_supplier_tax_number=source_vat,
                approved_by=command.approved_by,
            )
        )

        reclass = await self._reclassify(command)
        return self._result_from_reclass(command, effect, reclass, already_applied=already_applied)

    async def _create_permanent_partner(
        self,
        command: ResolveWorkbenchSupplierCommand,
        source,
    ):
        # Legal identity is derived ONLY from immutable source evidence, never from the request body.
        return await self._supplier_partner_writer.create_supplier(
            CreateSupplierPartnerCommand(
                company_id=command.company_id,
                supplier_name=(source.invoice.supplier.name or "").strip() or _missing("supplier legal name"),
                supplier_tax_number=(source.invoice.supplier.tax_number or "").strip()
                or _missing("supplier tax number"),
                idempotency_key=(
                    f"supplier-remediation:{command.company_id}:{source.source_invoice_id}:{command.expected_version}"
                ),
                approved_by=command.approved_by,
            )
        )

    async def _reclassify(self, command: ResolveWorkbenchSupplierCommand):
        try:
            return await self._reclassifier.execute(
                ReclassifyReviewCommand(
                    review_id=command.review_id,
                    company_id=command.company_id,
                    expected_version=command.expected_version,
                    trigger=ReviewReclassificationTrigger.SUPPLIER_RESOLUTION,
                    note=command.note,
                )
            )
        except ApplicationError:
            raise
        except Exception as exc:  # noqa: BLE001 - translated to a safe supplier-remediation error
            raise SupplierResolutionError(SAFE_SUPPLIER_REMEDIATION_ERROR) from exc

    def _result_from_reclass(
        self,
        command: ResolveWorkbenchSupplierCommand,
        effect: SupplierRemediationEffect,
        reclass,
        *,
        already_applied: bool,
    ) -> SupplierRemediationResult:
        supplier_still_missing = _has_supplier_not_found(reclass.new_review_reasons)
        status = (
            SupplierRemediationStatus.REMEDIATION_INCOMPLETE
            if supplier_still_missing
            else SupplierRemediationStatus.RESOLVED
        )
        return SupplierRemediationResult(
            review_id=command.review_id,
            company_id=command.company_id,
            mode=command.mode,
            status=status,
            previous_version=reclass.from_version,
            current_version=reclass.to_version,
            current_workflow=reclass.new_workflow,
            current_review_reasons=reclass.new_review_reasons,
            effective_partner_id=effect.resolved_partner_id,
            partner_write_status=effect.partner_write_status,
            reclassified=bool(reclass.changed),
            already_applied=already_applied,
            workbench_republished=False,
            safe_message=(
                "Supplier resolved; the review was reclassified."
                if status is SupplierRemediationStatus.RESOLVED
                else (
                    "The supplier resolution was recorded and the review reclassified, but the review still "
                    "requires a matched supplier. Another blocker may remain, or the selection did not match."
                )
            ),
        )

    # ------------------------------------------------------------------ resume / idempotency

    def _existing_resolution(self, command: ResolveWorkbenchSupplierCommand) -> SupplierResolution | None:
        try:
            return self._resolution_writer.get_supplier_resolution(
                review_id=command.review_id,
                company_id=command.company_id,
                review_version=command.expected_version,
            )
        except SupplierResolutionNotFoundError:
            return None

    def _require_matching_intent(
        self,
        existing: SupplierResolution,
        command: ResolveWorkbenchSupplierCommand,
    ) -> None:
        expected_partner = (
            command.resolved_partner_id if command.mode is SupplierResolutionMode.MATCH_EXISTING else None
        )
        if (
            existing.mode is not command.mode
            or existing.resolved_partner_id != expected_partner
            or (existing.note or None) != (command.note or None)
        ):
            raise SupplierResolutionConflictError(
                "A different supplier resolution was already recorded for this review version."
            )

    async def _resume_or_return_applied(
        self,
        command: ResolveWorkbenchSupplierCommand,
        review,
        existing: SupplierResolution,
    ) -> SupplierRemediationResult:
        if command.mode is SupplierResolutionMode.USE_ONE_OFF_SUPPLIER:
            return SupplierRemediationResult(
                review_id=command.review_id,
                company_id=command.company_id,
                mode=command.mode,
                status=SupplierRemediationStatus.ONE_OFF_EXECUTION_NOT_SUPPORTED,
                previous_version=review.version,
                current_version=review.version,
                current_workflow=review.workflow,
                current_review_reasons=review.review_reasons,
                effective_partner_id=None,
                partner_write_status=None,
                reclassified=False,
                already_applied=True,
                workbench_republished=False,
                safe_message="One-off supplier decision was already recorded; execution remains deferred.",
            )

        effect = self._remediation_effect_writer.find_remediation_effect(
            review_id=command.review_id,
            company_id=command.company_id,
            review_version=command.expected_version,
        )

        if review.version > command.expected_version:
            if effect is None:
                raise SupplierResolutionDataIntegrityError(
                    "The review advanced past this version but no remediation effect was recorded."
                )
            return SupplierRemediationResult(
                review_id=command.review_id,
                company_id=command.company_id,
                mode=command.mode,
                status=(
                    SupplierRemediationStatus.REMEDIATION_INCOMPLETE
                    if _has_supplier_not_found(review.review_reasons)
                    else SupplierRemediationStatus.RESOLVED
                ),
                previous_version=command.expected_version,
                current_version=review.version,
                current_workflow=review.workflow,
                current_review_reasons=review.review_reasons,
                effective_partner_id=effect.resolved_partner_id,
                partner_write_status=effect.partner_write_status,
                reclassified=True,
                already_applied=True,
                workbench_republished=False,
                safe_message="This supplier remediation was already applied.",
            )

        # The review is still at the pre-remediation version: resume the interrupted work.
        self._require_pending_supplier_not_found(review, command)
        source = self._source_invoice_reader.get(review_id=command.review_id, company_id=command.company_id)
        if effect is None:
            return await self._complete(command, review, source, existing, already_applied=True)
        reclass = await self._reclassify(command)
        return self._result_from_reclass(command, effect, reclass, already_applied=True)

    # ------------------------------------------------------------------ helpers

    def _resolution(
        self,
        command: ResolveWorkbenchSupplierCommand,
        source,
        *,
        resolved_partner_id: int | None,
        force_mode_match: bool = False,
    ) -> SupplierResolution:
        mode = SupplierResolutionMode.MATCH_EXISTING if force_mode_match else command.mode
        return SupplierResolution(
            mode=mode,
            review_id=command.review_id,
            company_id=command.company_id,
            review_version=command.expected_version,
            source_invoice_id=source.source_invoice_id,
            resolved_partner_id=resolved_partner_id,
            approved_by=command.approved_by,
            note=command.note,
        )


def _has_supplier_not_found(reasons: tuple[ManualReviewReason, ...]) -> bool:
    return any(reason.code is ManualReviewReasonCode.SUPPLIER_NOT_FOUND for reason in reasons)


def _missing(what: str) -> str:
    raise SupplierResolutionContractError(f"The immutable source invoice has no {what} to create a supplier from.")


__all__ = ["ResolveWorkbenchSupplierUseCase", "SAFE_SUPPLIER_REMEDIATION_ERROR"]
