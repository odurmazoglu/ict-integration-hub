"""Explicit supplier remediation orchestration for a SUPPLIER_NOT_FOUND review.

Ties together the pieces built in P0-3D2A..C (+ the controlled writer from
PR #129) without ever manufacturing a matched ``PartnerMatchResult``:

    eligibility check
    -> load immutable ReviewSourceInvoiceEvidence
    -> reserve the operator's SupplierResolution intent (before any Odoo write)
    -> MATCH_EXISTING: validate the selected partner's exact VAT
       CREATE_PERMANENT_SUPPLIER: derive name/VAT from source, call the gated
       controlled writer, then validate the resulting partner
       ONE_OFF_VENDOR: same derive/create-or-reuse as CREATE_PERMANENT_SUPPLIER,
       plus a Hub-ownership check on any exact-VAT reuse and a retirement row for
       the archive-last lifecycle (see one_off_vendor_use_cases.ArchiveOneOffVendorUseCase)
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

from collections.abc import Awaitable, Callable
from typing import Protocol

from app.application.commands.supplier_partner import CreateSupplierPartnerCommand
from app.application.dto.supplier_partner import SupplierPartnerWriteStatus
from app.application.exceptions import ApplicationError
from app.application.ports.supplier_partner_writer import SupplierPartnerWriter
from app.application.services import UnitOfWork
from app.application.workbench.exceptions import (
    ReviewPersistenceError,
    ReviewStateConflictError,
    ReviewVersionConflictError,
    SupplierResolutionConflictError,
    SupplierResolutionContractError,
    SupplierResolutionDataIntegrityError,
    SupplierResolutionError,
    SupplierResolutionNotFoundError,
    SupplierResolutionOneOffVendorNotHubOwnedError,
    WorkbenchCandidateAmbiguityError,
    WorkbenchCandidateReadError,
    WorkbenchContractError,
    WorkbenchProjectionPublishError,
)
from app.application.workbench.one_off_vendor_retirement import OneOffVendorRetirement, OneOffVendorRetirementStatus
from app.application.workbench.ports import (
    OneOffVendorRetirementWriter,
    ReviewQueueReader,
    ReviewSourceInvoiceEvidenceReader,
    SupplierRemediationEffectWriter,
    SupplierResolutionWriter,
)
from app.application.workbench.projection import ProjectionPublishResult, WorkbenchProjection
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
from app.application.workbench.write_authorization import (
    WriteAuthorizationOperationType,
    WriteAuthorizationRepository,
    supplier_resolution_authorization_consumer_id,
)
from app.application.workflow import ManualReviewReason, ManualReviewReasonCode

SAFE_SUPPLIER_REMEDIATION_ERROR = "Supplier remediation failed."

# The Workbench republish stage runs *after* the remediation, effect and
# reclassification have been committed. Every safe application error it can raise is
# swallowed and reported as ``workbench_republished=False`` -- an already-committed
# remediation must never become an HTTP/application failure, and an exact retry can
# republish later. This deliberately covers both:
#   * the publisher lookup/write (WorkbenchProjectionPublishError /
#     WorkbenchCandidateReadError / WorkbenchCandidateAmbiguityError) and the
#     WorkbenchProjection construction (WorkbenchContractError);
#   * the post-commit re-read of the current ReviewItem. The production review
#     reader's ``get_review_item`` translates every failure into
#     ReviewPersistenceError or a subclass -- ReviewNotFoundError (row gone) and
#     ReviewDataIntegrityError (corrupt persisted row) both inherit from it, and any
#     lower-level query error is re-raised as ReviewPersistenceError.
# It is intentionally NOT ``except Exception`` -- only these precise safe types.
_BEST_EFFORT_REPUBLISH_EXCEPTIONS = (
    WorkbenchProjectionPublishError,
    WorkbenchCandidateReadError,
    WorkbenchCandidateAmbiguityError,
    WorkbenchContractError,
    ReviewPersistenceError,
)


class SupplierReclassifier(Protocol):
    """Structural type for the P0-3D2B ``ReclassifyWorkbenchReviewUseCase``.

    Kept structural so this module never imports ``app.application.use_cases``
    (which imports this package), avoiding a package-init import cycle.
    """

    async def execute(self, command: ReclassifyReviewCommand): ...


class WorkbenchReviewRepublisher(Protocol):
    """Update-only republish of an already-created Odoo Workbench projection row.

    Structural on purpose: the orchestration must never reach a generic
    "create projection" operation that could add a second Workbench row. The
    single method here resolves its target from the trusted ``(review_id,
    company_id)`` lookup and fails closed when no row exists.
    """

    def republish_projection(self, projection: WorkbenchProjection) -> ProjectionPublishResult: ...


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
        unit_of_work: UnitOfWork,
        workbench_republisher: WorkbenchReviewRepublisher | None = None,
        retirement_writer: OneOffVendorRetirementWriter | None = None,
        write_authorization_repository: WriteAuthorizationRepository | None = None,
        _after_precheck_hook: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._review_reader = review_reader
        self._source_invoice_reader = source_invoice_reader
        self._resolution_validator = resolution_validator
        self._resolution_writer = resolution_writer
        self._remediation_effect_writer = remediation_effect_writer
        self._supplier_partner_writer = supplier_partner_writer
        self._reclassifier = reclassifier
        self._unit_of_work = unit_of_work
        # Optional: present only when odoo_workbench_projection_publish_enabled is set.
        # None -> republish is not attempted and every result reports republished=False.
        self._workbench_republisher = workbench_republisher
        # Optional: required only for ONE_OFF_VENDOR (P0-PROD-08H); every other mode
        # never touches it. None -> ONE_OFF_VENDOR fails closed with a clear error.
        self._retirement_writer = retirement_writer
        # Optional (P0-PROD-09F): required only when command.authorization_id is
        # supplied for CREATE_PERMANENT_SUPPLIER/ONE_OFF_VENDOR. None -> an
        # authorization_id on the command fails closed with a clear error, exactly
        # mirroring the Vendor Bill execution runtime's own optionality for this.
        self._write_authorization_repository = write_authorization_repository
        # Test-only seam: invoked on the fresh path just before the reservation INSERT,
        # so a test can commit a competing reservation in another transaction in between.
        self._after_precheck_hook = _after_precheck_hook

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

        if self._after_precheck_hook is not None:
            await self._after_precheck_hook()

        if command.mode is SupplierResolutionMode.USE_ONE_OFF_SUPPLIER:
            return self._resolve_one_off(command, review, source)

        try:
            # The reservation is the cross-process single-winner barrier: it is committed
            # here, before any Odoo write. A concurrent INSERT-race loser is raised out of
            # _reserve_intent (SupplierResolutionRaceError / SupplierResolutionConflictError)
            # and never reaches the supplier writer.
            intent = self._reserve_intent(command, source)
            return await self._complete(command, review, source, intent, already_applied=False)
        except BaseException:
            # A committed reservation stays committed (it enables a safe resume); discard
            # any uncommitted work (a lost reservation INSERT, or effect / reclassification
            # after a partial failure) so no half-applied state is left behind.
            self._unit_of_work.rollback()
            raise

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
        self._resolution_writer.reserve_supplier_resolution(resolution)
        self._unit_of_work.commit()
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
        # Reserve the operator's intent and COMMIT it before any irreversible Odoo write.
        # A concurrent transaction that lost this UNIQUE(review_id, review_version) INSERT
        # is raised out here and never proceeds.
        reserved = self._resolution_writer.reserve_supplier_resolution(
            self._resolution(command, source, resolved_partner_id=resolved_partner_id)
        )
        self._unit_of_work.commit()
        return reserved

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
        elif command.mode is SupplierResolutionMode.ONE_OFF_VENDOR:
            write_result = await self._create_or_reuse_one_off_vendor_partner(command, source)
            effective_partner_id = write_result.partner_id
            write_status = (
                SupplierPartnerWriteEffectStatus.CREATED
                if write_result.status is SupplierPartnerWriteStatus.CREATED
                else SupplierPartnerWriteEffectStatus.ALREADY_EXISTS
            )
            # No MATCH_EXISTING-shaped re-validation here on purpose: that check
            # requires the partner to be *active*, which is correct for an operator
            # selecting an existing partner but wrong here -- reusing a partner this
            # Hub already archived from an earlier ONE_OFF_VENDOR lifecycle (case
            # B/F) is a legitimate, expected state, not an error. The exact-VAT
            # identity is already proven by the writer's own read-before-write (on
            # create) or by the exact-VAT lookup that found it (on reuse); ownership
            # is proven by _create_or_reuse_one_off_vendor_partner's own check above.
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

        retirement = None
        if command.mode is SupplierResolutionMode.ONE_OFF_VENDOR:
            if self._retirement_writer is None:
                raise SupplierResolutionError(SAFE_SUPPLIER_REMEDIATION_ERROR)
            # Idempotent by construction: a byte-identical retry (same partner_id)
            # returns the existing row; a genuinely different partner_id for the same
            # review version fails closed (OneOffVendorRetirementConflictError).
            retirement = self._retirement_writer.create_retirement(
                OneOffVendorRetirement(
                    review_id=command.review_id,
                    company_id=command.company_id,
                    review_version=command.expected_version,
                    resolved_partner_id=effective_partner_id,
                    status=OneOffVendorRetirementStatus.PENDING_VENDOR_BILL,
                )
            )

        reclass = await self._reclassify(command)
        self._unit_of_work.commit()
        # Best-effort, post-commit: the remediation + reclassification are already durable.
        republished = self._republish_workbench_projection(command)
        return self._result_from_reclass(
            command,
            effect,
            reclass,
            already_applied=already_applied,
            workbench_republished=republished,
            retirement=retirement,
        )

    async def _create_permanent_partner(
        self,
        command: ResolveWorkbenchSupplierCommand,
        source,
    ):
        authorization = self._claim_supplier_write_authorization(
            command, operation_type=WriteAuthorizationOperationType.CREATE_PERMANENT_SUPPLIER
        )
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
                authorization=authorization,
            )
        )

    def _claim_supplier_write_authorization(
        self,
        command: ResolveWorkbenchSupplierCommand,
        *,
        operation_type: WriteAuthorizationOperationType,
    ):
        """P0-PROD-09F: claim (and durably consume) the narrow write authorization for
        this exact supplier-resolution write attempt, if one was supplied. Consumption
        is flushed here and becomes durable with whatever commit follows in
        ``_complete`` -- exactly the same "commit consumption together with the
        write's own outcome" discipline the Vendor Bill execution runtime already uses
        for EXECUTE_VENDOR_BILL. The consumer id is deterministic from the command's
        own identity, so a legitimate crash-then-retry of the same resolution request
        resumes against its own already-consumed authorization rather than being
        rejected as a different attempt.
        """

        if command.authorization_id is None:
            return None
        if self._write_authorization_repository is None:
            raise SupplierResolutionContractError("Runtime authorization is not supported by this workflow.")
        return self._write_authorization_repository.claim_and_consume(
            company_id=command.company_id,
            review_id=command.review_id,
            operation_type=operation_type,
            target_version=command.expected_version,
            authorization_id=command.authorization_id,
            execution_id=supplier_resolution_authorization_consumer_id(
                company_id=command.company_id,
                review_id=command.review_id,
                expected_version=command.expected_version,
                mode=command.mode.value,
            ),
        )

    async def _create_or_reuse_one_off_vendor_partner(
        self,
        command: ResolveWorkbenchSupplierCommand,
        source,
    ):
        """Same minimal name+VAT create-or-reuse as CREATE_PERMANENT_SUPPLIER (P0-PROD-08H),
        now also reaching an *archived* Hub-owned partner (P0-PROD-09C).

        The underlying writer's exact-VAT read-before-write already covers idempotent
        replay (case B/F) and ambiguous-match fail-closed (case C). The one thing it
        cannot know is *ownership*: an exact-VAT match may be a pre-existing partner
        the Hub never created via ONE_OFF_VENDOR (e.g. a permanent supplier, case A),
        or an archived partner from a completed prior ONE_OFF_VENDOR lifecycle for
        this same VAT (case G, P0-PROD-09C) -- silently treating either as
        retirement-eligible/reusable without proof of ownership would be unsafe.
        Only a partner with its own prior ONE_OFF_VENDOR effect is ever reused here,
        active or archived.

        ``_authorize_inactive_reuse`` is passed to the writer so it never infers Hub
        ownership itself (see ``OdooSupplierPartnerWriter``/``CreateSupplierPartnerCommand``):
        it only asks this exact ownership-ledger question, identical to the one this
        method already asks below for the active-match case. CREATE_PERMANENT_SUPPLIER
        (``_create_permanent_partner``) passes no such predicate, so an archived
        exact-VAT match there still fails closed exactly as before this change.
        """

        def _authorize_inactive_reuse(partner_id: int) -> bool:
            return (
                self._remediation_effect_writer.find_one_off_vendor_effect_by_partner_id(
                    company_id=command.company_id,
                    resolved_partner_id=partner_id,
                )
                is not None
            )

        authorization = self._claim_supplier_write_authorization(
            command, operation_type=WriteAuthorizationOperationType.ONE_OFF_VENDOR_SUPPLIER
        )
        # Legal identity is derived ONLY from immutable source evidence, never from the request body.
        write_result = await self._supplier_partner_writer.create_supplier(
            CreateSupplierPartnerCommand(
                company_id=command.company_id,
                supplier_name=(source.invoice.supplier.name or "").strip() or _missing("supplier legal name"),
                supplier_tax_number=(source.invoice.supplier.tax_number or "").strip()
                or _missing("supplier tax number"),
                idempotency_key=(
                    f"one-off-vendor:{command.company_id}:{source.source_invoice_id}:{command.expected_version}"
                ),
                approved_by=command.approved_by,
                authorize_inactive_reuse=_authorize_inactive_reuse,
                authorization=authorization,
            )
        )
        if write_result.status is SupplierPartnerWriteStatus.ALREADY_EXISTS:
            owned = self._remediation_effect_writer.find_one_off_vendor_effect_by_partner_id(
                company_id=command.company_id,
                resolved_partner_id=write_result.partner_id,
            )
            if owned is None:
                raise SupplierResolutionOneOffVendorNotHubOwnedError(
                    "An existing Odoo partner for this exact VAT is not Hub-owned by ONE_OFF_VENDOR; "
                    "use MATCH_EXISTING or CREATE_PERMANENT_SUPPLIER for this vendor instead."
                )
        return write_result

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
        workbench_republished: bool = False,
        retirement: OneOffVendorRetirement | None = None,
    ) -> SupplierRemediationResult:
        supplier_still_missing = _has_supplier_not_found(reclass.new_review_reasons)
        status = (
            SupplierRemediationStatus.REMEDIATION_INCOMPLETE
            if supplier_still_missing
            else SupplierRemediationStatus.RESOLVED
        )
        is_one_off_vendor = command.mode is SupplierResolutionMode.ONE_OFF_VENDOR
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
            workbench_republished=workbench_republished,
            one_off_vendor_hub_owned=True if is_one_off_vendor else None,
            one_off_vendor_retirement_status=retirement.status if retirement is not None else None,
            safe_message=(
                "Supplier resolved; the review was reclassified."
                if status is SupplierRemediationStatus.RESOLVED
                else (
                    "The supplier resolution was recorded and the review reclassified, but the review still "
                    "requires a matched supplier. Another blocker may remain, or the selection did not match."
                )
            ),
        )

    # ------------------------------------------------------------------ workbench republish

    def _republish_workbench_projection(self, command: ResolveWorkbenchSupplierCommand) -> bool:
        """Update the existing Odoo Workbench projection row for this review.

        Post-commit and strictly best-effort: the supplier resolution, the effect
        and the reclassification are already durable. Returns ``True`` only when the
        publisher confirmed an update of the already-created row; any lookup /
        write / mapping failure (or a missing target row) is swallowed and reported
        as ``False`` so a Workbench outage never rolls back or falsifies a
        committed remediation. A retry re-enters here and updates the same row.
        The publisher is update-only -- it can never create a second Workbench row.
        """

        if self._workbench_republisher is None:
            return False
        try:
            review_item = self._review_reader.get_review_item(
                ReviewDetailQuery(review_id=command.review_id, company_id=command.company_id)
            )
            projection = WorkbenchProjection(
                review_id=review_item.review_id,
                company_id=command.company_id,
                invoice_id=review_item.invoice_id,
                version=review_item.version,
                status=review_item.status,
                invoice_number=review_item.invoice_number,
                supplier_name=review_item.supplier_name,
                supplier_tax_number=review_item.supplier_tax_number,
                invoice_date=review_item.invoice_date,
                currency=review_item.currency,
                total_amount=review_item.total_amount,
                workflow=review_item.workflow,
                review_reasons=review_item.review_reasons,
                warnings=review_item.warnings,
                updated_at=review_item.updated_at,
            )
            result = self._workbench_republisher.republish_projection(projection)
        except _BEST_EFFORT_REPUBLISH_EXCEPTIONS:
            return False
        # Truthful only: report success solely when the update-only publisher confirmed
        # an *existing* row was updated. ``ProjectionPublishResult`` already guarantees
        # exactly one of created/updated is True, and ``republish_projection`` has no
        # create branch, so this is belt-and-suspenders, not a behavior change.
        return result.updated is True and result.created is False

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
            # Full remediation already committed on an earlier attempt. Re-attempt the
            # (idempotent, update-only) Workbench republish so a retry after a prior
            # republish failure can still reflect the new review state in the UI.
            republished = self._republish_workbench_projection(command)
            is_one_off_vendor = command.mode is SupplierResolutionMode.ONE_OFF_VENDOR
            retirement = self._find_retirement(command)
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
                workbench_republished=republished,
                one_off_vendor_hub_owned=True if is_one_off_vendor else None,
                one_off_vendor_retirement_status=retirement.status if retirement is not None else None,
                safe_message="This supplier remediation was already applied.",
            )

        # The review is still at the pre-remediation version: resume the interrupted work.
        # The committed reservation already fixed the single winner for this version, so a
        # resume never creates a parallel reservation; the supplier writer's own exact-VAT
        # idempotency keeps a re-run from creating a second partner when one already exists.
        self._require_pending_supplier_not_found(review, command)
        source = self._source_invoice_reader.get(review_id=command.review_id, company_id=command.company_id)
        try:
            if effect is None:
                return await self._complete(command, review, source, existing, already_applied=True)
            reclass = await self._reclassify(command)
            self._unit_of_work.commit()
            republished = self._republish_workbench_projection(command)
            return self._result_from_reclass(
                command,
                effect,
                reclass,
                already_applied=True,
                workbench_republished=republished,
                retirement=self._find_retirement(command),
            )
        except BaseException:
            self._unit_of_work.rollback()
            raise

    # ------------------------------------------------------------------ helpers

    def _find_retirement(self, command: ResolveWorkbenchSupplierCommand) -> OneOffVendorRetirement | None:
        if command.mode is not SupplierResolutionMode.ONE_OFF_VENDOR or self._retirement_writer is None:
            return None
        return self._retirement_writer.find(
            review_id=command.review_id,
            company_id=command.company_id,
            review_version=command.expected_version,
        )

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
