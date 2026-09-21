"""Crash-safe, retry-safe, concurrency-safe CREATE_NEW_PRODUCT orchestration (P0-PROD-07G).

Reuses the PR #140 narrow Odoo write infrastructure (``ProductWriter``,
``SupplierInfoWriter``, gated by ``PRODUCT_REMEDIATION_WRITE_ENABLED``) exactly as
designed -- this module only adds the durable orchestration on top of it. See
``app.application.workbench.product_remediation`` for the state machine and the two
independent DB-enforced identities this protects:

* review-line ownership -- ``(review_id, company_id, review_version, line_number)``.
* supplier-product identity -- ``(company_id, resolved_supplier_partner_id,
  normalized seller_item_code)``.

The hardest edge (STEP 8 of the P0-PROD-07G brief) is an Odoo ``product.template``
create whose remote outcome is lost before the Hub can persist the identity.
``product.template`` carries no durable Hub idempotency key, so its existence after
such a crash cannot be deterministically proven from here. Rather than guess, the
reservation is committed to ``CREATE_ATTEMPTED`` immediately *before* the Odoo call;
a resume that finds this status transitions to ``NEEDS_RECONCILIATION`` and never
attempts another create. This is intentionally conservative: it is explicit,
residual ambiguity, not a solved problem -- see the PR description.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from app.application.commands.product_remediation import CreateProductCommand, CreateSupplierInfoCommand
from app.application.dto.product_remediation import SupplierInfoWriteStatus
from app.application.exceptions.product_remediation import (
    ProductWriteAuthenticationError,
    ProductWriteAuthorizationError,
    ProductWriteSafetyGateError,
    ProductWriteValidationError,
)
from app.application.ports.existing_supplier_info_reader import ExistingSupplierInfoReader
from app.application.ports.product_writer import ProductWriter
from app.application.ports.supplier_info_writer import SupplierInfoWriter
from app.application.services import UnitOfWork
from app.application.workbench.dto import ReviewStatus
from app.application.workbench.exceptions import (
    ProductRemediationConflictError,
    ProductRemediationContractError,
    ProductRemediationDataIntegrityError,
    ProductRemediationEligibilityError,
    ProductRemediationIdentityAmbiguousError,
    ProductRemediationRaceError,
    ProductRemediationSupplierUnresolvedError,
)
from app.application.workbench.ports import (
    ProductIdentityClaimWriter,
    ProductRemediationReservationWriter,
    ReviewQueueReader,
    ReviewSourceInvoiceEvidenceReader,
    SupplierRemediationEffectWriter,
)
from app.application.workbench.product_remediation import (
    CreateNewProductCommand,
    CreateNewProductResult,
    ExistingSupplierInfo,
    ProductIdentityClaim,
    ProductRemediationReservation,
    ProductRemediationStatus,
    ProductReservationStatus,
    normalize_seller_item_code,
)
from app.application.workbench.queries import ReviewDetailQuery
from app.application.workbench.write_authorization import (
    WriteAuthorizationOperationType,
    WriteAuthorizationRepository,
    product_remediation_authorization_consumer_id,
)
from app.application.workflow import ManualReviewReason, ManualReviewReasonCode

# Exceptions from OdooProductWriter.create_product() that are CERTAIN to mean no Odoo
# product.template was created: the gate/policy check and payload validation all run
# before the network call, and an auth/authz rejection means Odoo refused the request
# outright. Safe to revert the reservation back to RESERVED and let a normal retry
# reattempt cleanly. Every other exception (timeout, data-integrity/read-back failure,
# variant-resolution failure, or anything unexpected) is treated as an UNCERTAIN
# remote outcome -- see module docstring.
_CERTAIN_NO_WRITE_EXCEPTIONS = (
    ProductWriteSafetyGateError,
    ProductWriteValidationError,
    ProductWriteAuthenticationError,
    ProductWriteAuthorizationError,
)


class CreateNewProductUseCase:
    """Application boundary for one authenticated CREATE_NEW_PRODUCT decision."""

    def __init__(
        self,
        *,
        review_reader: ReviewQueueReader,
        source_invoice_reader: ReviewSourceInvoiceEvidenceReader,
        remediation_effect_reader: SupplierRemediationEffectWriter,
        reservation_writer: ProductRemediationReservationWriter,
        identity_claim_writer: ProductIdentityClaimWriter,
        existing_supplier_info_reader: ExistingSupplierInfoReader,
        product_writer: ProductWriter,
        supplier_info_writer: SupplierInfoWriter,
        unit_of_work: UnitOfWork,
        write_authorization_repository: WriteAuthorizationRepository | None = None,
        _after_precheck_hook: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._review_reader = review_reader
        self._source_invoice_reader = source_invoice_reader
        self._remediation_effect_reader = remediation_effect_reader
        self._reservation_writer = reservation_writer
        self._identity_claim_writer = identity_claim_writer
        self._existing_supplier_info_reader = existing_supplier_info_reader
        self._product_writer = product_writer
        self._supplier_info_writer = supplier_info_writer
        self._unit_of_work = unit_of_work
        # Optional (P0-PROD-09G): only set when narrow runtime write authorization is
        # wired in; a command without an authorization_id never touches this.
        self._write_authorization_repository = write_authorization_repository
        # Test-only seam: invoked on the fresh path just before the reservation INSERT,
        # so a test can commit a competing reservation/claim in another transaction in between.
        self._after_precheck_hook = _after_precheck_hook

    async def execute(self, command: CreateNewProductCommand) -> CreateNewProductResult:
        if not isinstance(command, CreateNewProductCommand):
            raise ProductRemediationContractError("A canonical CreateNewProductCommand is required.")

        existing = self._reservation_writer.find(
            review_id=command.review_id,
            company_id=command.company_id,
            review_version=command.expected_version,
            line_number=command.line_number,
        )
        if existing is not None:
            self._require_matching_intent(existing, command)
            return await self._resume(command, existing)

        review = self._review_reader.get_review_item(
            ReviewDetailQuery(review_id=command.review_id, company_id=command.company_id)
        )
        self._require_eligible(review, command)
        source = self._source_invoice_reader.get(review_id=command.review_id, company_id=command.company_id)
        seller_item_code = self._require_seller_item_code(source, command)
        resolved_partner_id = self._require_resolved_supplier(command)

        if self._after_precheck_hook is not None:
            await self._after_precheck_hook()

        try:
            # The reservation is the cross-process single-winner barrier for the review-line
            # identity: committed here, before any Odoo write (mirrors ResolveWorkbenchSupplierUseCase).
            reservation = self._reservation_writer.reserve(
                ProductRemediationReservation(
                    review_id=command.review_id,
                    company_id=command.company_id,
                    review_version=command.expected_version,
                    line_number=command.line_number,
                    status=ProductReservationStatus.RESERVED,
                    resolved_supplier_partner_id=resolved_partner_id,
                    seller_item_code=seller_item_code,
                    product_name=command.product_name.strip(),
                    is_storable=command.is_storable,
                    internal_reference=_normalize_optional(command.internal_reference),
                    approved_by=command.approved_by,
                    note=_normalize_optional(command.note),
                    idempotency_key=command.idempotency_key,
                )
            )
            self._unit_of_work.commit()
        except BaseException:
            self._unit_of_work.rollback()
            raise

        if reservation.status is not ProductReservationStatus.RESERVED:
            # A concurrent identical request already advanced this reservation past
            # RESERVED between our pre-check and our INSERT; resume from its real state.
            return await self._resume(command, reservation)
        return await self._proceed(command, reservation)

    # ------------------------------------------------------------------ eligibility

    def _require_eligible(self, review, command: CreateNewProductCommand) -> None:
        if review.status is not ReviewStatus.PENDING_REVIEW:
            raise ProductRemediationEligibilityError("The review is not pending review.")
        if review.version != command.expected_version:
            raise ProductRemediationEligibilityError("The review version does not match expected_version.")
        if not _has_product_not_found_for_line(review.review_reasons, command.line_number):
            raise ProductRemediationEligibilityError(
                "The review line no longer carries PRODUCT_NOT_FOUND; there is nothing to remediate."
            )

    def _require_seller_item_code(self, source, command: CreateNewProductCommand) -> str:
        line = next((line for line in source.invoice.lines if line.line_number == command.line_number), None)
        if line is None:
            raise ProductRemediationEligibilityError("The review line does not exist on the immutable source invoice.")
        normalized = normalize_seller_item_code(line.seller_item_code)
        if normalized is None:
            raise ProductRemediationEligibilityError(
                "The source invoice line has no usable seller_item_code; CREATE_NEW_PRODUCT v1 requires one."
            )
        return normalized

    def _require_resolved_supplier(self, command: CreateNewProductCommand) -> int:
        effect = self._remediation_effect_reader.find_latest_remediation_effect(
            review_id=command.review_id,
            company_id=command.company_id,
        )
        if effect is None:
            raise ProductRemediationSupplierUnresolvedError(
                "No accepted supplier resolution exists for this review; resolve the supplier first."
            )
        return effect.resolved_partner_id

    def _require_matching_intent(
        self,
        existing: ProductRemediationReservation,
        command: CreateNewProductCommand,
    ) -> None:
        if (
            existing.product_name != command.product_name.strip()
            or existing.is_storable != command.is_storable
            or existing.internal_reference != _normalize_optional(command.internal_reference)
            or existing.note != _normalize_optional(command.note)
        ):
            raise ProductRemediationConflictError(
                "A different CREATE_NEW_PRODUCT decision already exists for this review line."
            )

    # ------------------------------------------------------------------ fresh path

    async def _proceed(
        self,
        command: CreateNewProductCommand,
        reservation: ProductRemediationReservation,
    ) -> CreateNewProductResult:
        # STEP 5: read-before-write natural-identity pre-check against Odoo, before any
        # DB claim or Odoo write. A hit here means Odoo already has a supplierinfo for
        # this exact identity (prior manual data entry, or an earlier uncertain outcome).
        existing_info = await self._find_existing_supplier_info(reservation)
        if existing_info is not None:
            return self._reuse(command, reservation, existing_info)

        try:
            # The real concurrency barrier for the supplier-product identity: committed
            # before any Odoo write. The race loser never creates a product.
            self._identity_claim_writer.claim(
                ProductIdentityClaim(
                    company_id=reservation.company_id,
                    resolved_supplier_partner_id=reservation.resolved_supplier_partner_id,
                    seller_item_code=reservation.seller_item_code,
                    owner_review_id=reservation.review_id,
                    owner_company_id=reservation.company_id,
                    owner_review_version=reservation.review_version,
                    owner_line_number=reservation.line_number,
                )
            )
            self._unit_of_work.commit()
        except ProductRemediationRaceError:
            self._unit_of_work.rollback()
            return self._resolve_identity_race(command, reservation)

        return await self._create_product_and_supplierinfo(command, reservation)

    async def _find_existing_supplier_info(
        self,
        reservation: ProductRemediationReservation,
    ) -> ExistingSupplierInfo | None:
        records = await self._existing_supplier_info_reader.find_existing(
            partner_id=reservation.resolved_supplier_partner_id,
            product_code=reservation.seller_item_code,
            company_id=reservation.company_id,
        )
        if len(records) > 1:
            raise ProductRemediationIdentityAmbiguousError(
                "Multiple existing Odoo supplierinfo records share this exact supplier/item identity."
            )
        if not records:
            return None
        existing_info = records[0]
        if existing_info.product_tmpl_id is None or existing_info.product_id is None:
            raise ProductRemediationIdentityAmbiguousError(
                "An existing supplierinfo for this identity has no resolvable linked product; cannot reuse safely."
            )
        return existing_info

    def _reuse(
        self,
        command: CreateNewProductCommand,
        reservation: ProductRemediationReservation,
        existing_info: ExistingSupplierInfo,
    ) -> CreateNewProductResult:
        # Best-effort: also claim the identity so future lookups (this or another
        # review) resolve it from the DB without another Odoo round trip. A race here
        # only means another request is doing the same thing concurrently -- harmless.
        try:
            self._identity_claim_writer.claim(
                ProductIdentityClaim(
                    company_id=reservation.company_id,
                    resolved_supplier_partner_id=reservation.resolved_supplier_partner_id,
                    seller_item_code=reservation.seller_item_code,
                    owner_review_id=reservation.review_id,
                    owner_company_id=reservation.company_id,
                    owner_review_version=reservation.review_version,
                    owner_line_number=reservation.line_number,
                )
            )
            self._unit_of_work.commit()
        except ProductRemediationRaceError:
            self._unit_of_work.rollback()

        updated = self._reservation_writer.advance(
            reservation,
            expected_status=ProductReservationStatus.RESERVED,
            new_status=ProductReservationStatus.REUSED_EXISTING_PRODUCT,
            product_template_id=existing_info.product_tmpl_id,
            product_id=existing_info.product_id,
            supplierinfo_id=existing_info.id,
        )
        self._unit_of_work.commit()
        return self._success_result(
            updated,
            created_product=False,
            created_supplierinfo=False,
            reused_existing_product=True,
            already_applied=False,
            safe_message=(
                "An existing Odoo product/supplierinfo already covers this exact supplier/item identity; it "
                "was reused. No product was created."
            ),
        )

    def _resolve_identity_race(
        self,
        command: CreateNewProductCommand,
        reservation: ProductRemediationReservation,
    ) -> CreateNewProductResult:
        claim = self._identity_claim_writer.find(
            company_id=reservation.company_id,
            resolved_supplier_partner_id=reservation.resolved_supplier_partner_id,
            seller_item_code=reservation.seller_item_code,
        )
        owner = (
            self._reservation_writer.find(
                review_id=claim.owner_review_id,
                company_id=claim.owner_company_id,
                review_version=claim.owner_review_version,
                line_number=claim.owner_line_number,
            )
            if claim is not None
            else None
        )
        if owner is None or owner.product_template_id is None or owner.product_id is None:
            # Genuinely in-flight: another request currently owns this identity and has not
            # yet resolved a product. Never create a duplicate -- surface a retryable error.
            raise ProductRemediationRaceError(
                "Another request is currently creating a product for this exact supplier/item identity; retry."
            )

        updated = self._reservation_writer.advance(
            reservation,
            expected_status=ProductReservationStatus.RESERVED,
            new_status=ProductReservationStatus.REUSED_EXISTING_PRODUCT,
            product_template_id=owner.product_template_id,
            product_id=owner.product_id,
            supplierinfo_id=owner.supplierinfo_id,
        )
        self._unit_of_work.commit()
        return self._success_result(
            updated,
            created_product=False,
            created_supplierinfo=False,
            reused_existing_product=True,
            already_applied=False,
            safe_message=(
                "Another review already created a product for this exact supplier/item identity; it was reused."
            ),
        )

    def _claim_write_authorization(self, command: CreateNewProductCommand):
        """P0-PROD-09G: claim (and durably consume) the narrow write authorization for
        this exact CREATE_NEW_PRODUCT write, if one was supplied. Deterministic
        consumer id from the command's own identity (keyed by line_number, not by
        which of the two underlying Odoo writes is in flight), so this is safe to
        call again, idempotently, immediately before the product.template create and
        again before the product.supplierinfo create/link -- and so a legitimate
        crash-then-retry of either step resumes against its own already-consumed
        authorization."""

        if command.authorization_id is None:
            return None
        if self._write_authorization_repository is None:
            raise ProductRemediationContractError("Runtime authorization is not supported by this workflow.")
        return self._write_authorization_repository.claim_and_consume(
            company_id=command.company_id,
            review_id=command.review_id,
            operation_type=WriteAuthorizationOperationType.CREATE_NEW_PRODUCT,
            target_version=command.expected_version,
            authorization_id=command.authorization_id,
            execution_id=product_remediation_authorization_consumer_id(
                company_id=command.company_id,
                review_id=command.review_id,
                expected_version=command.expected_version,
                line_number=command.line_number,
            ),
        )

    async def _create_product_and_supplierinfo(
        self,
        command: CreateNewProductCommand,
        reservation: ProductRemediationReservation,
    ) -> CreateNewProductResult:
        # Persisted and COMMITTED immediately before the Odoo call: if the process
        # crashes anywhere from here until PRODUCT_CREATED is persisted, the remote
        # outcome is unknown and a resume must never blindly retry -- see module docstring.
        attempted = self._reservation_writer.advance(
            reservation,
            expected_status=ProductReservationStatus.RESERVED,
            new_status=ProductReservationStatus.CREATE_ATTEMPTED,
        )
        self._unit_of_work.commit()

        authorization = self._claim_write_authorization(command)
        try:
            write_result = await self._product_writer.create_product(
                CreateProductCommand(
                    name=attempted.product_name,
                    type=command.product_type,
                    uom_id=command.uom_id,
                    is_storable=attempted.is_storable,
                    default_code=attempted.internal_reference,
                    approved_by=attempted.approved_by,
                    authorization=authorization,
                )
            )
        except _CERTAIN_NO_WRITE_EXCEPTIONS:
            # P0-PROD-09G: discard the flushed-but-uncommitted authorization claim
            # (if any) before the reservation revert below commits -- a certain
            # no-write failure here always means one of the *unconditional* checks
            # (master kill switch, approval ack, named approver) failed despite a
            # valid authorization; none of those are fixed by retrying with the same
            # authorization, but the authorization itself must remain usable once the
            # real misconfiguration is fixed (mirrors ArchiveOneOffVendorUseCase).
            self._unit_of_work.rollback()
            self._reservation_writer.advance(
                attempted,
                expected_status=ProductReservationStatus.CREATE_ATTEMPTED,
                new_status=ProductReservationStatus.RESERVED,
            )
            self._unit_of_work.commit()
            raise
        except BaseException:
            # Uncertain remote outcome (transport failure, read-back/variant-resolution
            # failure, or anything unexpected). Leave CREATE_ATTEMPTED committed so a
            # resume requires reconciliation -- never convert this into a blind retry.
            # This also discards any flushed-but-uncommitted authorization claim.
            self._unit_of_work.rollback()
            raise

        created = self._reservation_writer.advance(
            attempted,
            expected_status=ProductReservationStatus.CREATE_ATTEMPTED,
            new_status=ProductReservationStatus.PRODUCT_CREATED,
            product_template_id=write_result.template_id,
            product_id=write_result.product_id,
        )
        self._unit_of_work.commit()
        return await self._create_supplierinfo(created, created_product=True, command=command)

    async def _create_supplierinfo(
        self,
        reservation: ProductRemediationReservation,
        *,
        created_product: bool,
        command: CreateNewProductCommand,
    ) -> CreateNewProductResult:
        if reservation.product_template_id is None or reservation.product_id is None:
            raise ProductRemediationDataIntegrityError(
                "Cannot create supplierinfo before the product identity is persisted."
            )
        authorization = self._claim_write_authorization(command)
        write_result = await self._supplier_info_writer.create_supplier_info(
            CreateSupplierInfoCommand(
                company_id=reservation.company_id,
                partner_id=reservation.resolved_supplier_partner_id,
                product_tmpl_id=reservation.product_template_id,
                product_id=reservation.product_id,
                product_code=reservation.seller_item_code,
                idempotency_key=reservation.idempotency_key or _default_idempotency_key(reservation),
                approved_by=reservation.approved_by,
                authorization=authorization,
            )
        )
        completed = self._reservation_writer.advance(
            reservation,
            expected_status=ProductReservationStatus.PRODUCT_CREATED,
            new_status=ProductReservationStatus.COMPLETED,
            supplierinfo_id=write_result.supplierinfo_id,
        )
        self._unit_of_work.commit()
        return self._success_result(
            completed,
            created_product=created_product,
            created_supplierinfo=write_result.status is SupplierInfoWriteStatus.CREATED,
            reused_existing_product=False,
            already_applied=False,
            safe_message=(
                "Product created and linked to the supplier."
                if created_product
                else "The product from an earlier attempt was resumed and linked to the supplier."
            ),
        )

    # ------------------------------------------------------------------ resume / idempotency

    async def _resume(
        self,
        command: CreateNewProductCommand,
        existing: ProductRemediationReservation,
    ) -> CreateNewProductResult:
        if existing.status is ProductReservationStatus.RESERVED:
            return await self._proceed(command, existing)
        if existing.status is ProductReservationStatus.CREATE_ATTEMPTED:
            return self._reconcile_uncertain_create(existing)
        if existing.status is ProductReservationStatus.PRODUCT_CREATED:
            return await self._create_supplierinfo(existing, created_product=False, command=command)
        if existing.status is ProductReservationStatus.COMPLETED:
            return self._success_result(
                existing,
                created_product=False,
                created_supplierinfo=False,
                reused_existing_product=False,
                already_applied=True,
                safe_message="This CREATE_NEW_PRODUCT decision was already completed.",
            )
        if existing.status is ProductReservationStatus.REUSED_EXISTING_PRODUCT:
            return self._success_result(
                existing,
                created_product=False,
                created_supplierinfo=False,
                reused_existing_product=True,
                already_applied=True,
                safe_message="This review line already reused an existing product for this identity.",
            )
        if existing.status is ProductReservationStatus.NEEDS_RECONCILIATION:
            return self._reconciliation_result(existing, already_applied=True)
        raise ProductRemediationDataIntegrityError("Unexpected product remediation reservation status.")

    def _reconcile_uncertain_create(
        self,
        reservation: ProductRemediationReservation,
    ) -> CreateNewProductResult:
        # v1 deliberately performs no deterministic Odoo read-back here: product.template
        # carries no durable Hub idempotency key, and neither name nor default_code (when
        # provided) is a safe existence proof (names are not unique; default_code is
        # optional and this exact command may not have supplied one). The prior attempt's
        # remote outcome cannot be proven from here -- see module docstring and PR
        # description for the residual ambiguity this leaves.
        updated = self._reservation_writer.advance(
            reservation,
            expected_status=ProductReservationStatus.CREATE_ATTEMPTED,
            new_status=ProductReservationStatus.NEEDS_RECONCILIATION,
        )
        self._unit_of_work.commit()
        return self._reconciliation_result(updated, already_applied=True)

    def _reconciliation_result(
        self,
        reservation: ProductRemediationReservation,
        *,
        already_applied: bool,
    ) -> CreateNewProductResult:
        return CreateNewProductResult(
            review_id=reservation.review_id,
            company_id=reservation.company_id,
            review_version=reservation.review_version,
            line_number=reservation.line_number,
            status=ProductRemediationStatus.RECONCILIATION_REQUIRED,
            product_template_id=reservation.product_template_id,
            product_id=reservation.product_id,
            supplierinfo_id=reservation.supplierinfo_id,
            created_product=False,
            created_supplierinfo=False,
            reused_existing_product=False,
            already_applied=already_applied,
            safe_message=(
                "A prior Odoo product creation attempt's outcome could not be verified. A human must reconcile "
                "Odoo state for this review line before it can be retried."
            ),
        )

    def _success_result(
        self,
        reservation: ProductRemediationReservation,
        *,
        created_product: bool,
        created_supplierinfo: bool,
        reused_existing_product: bool,
        already_applied: bool,
        safe_message: str,
    ) -> CreateNewProductResult:
        return CreateNewProductResult(
            review_id=reservation.review_id,
            company_id=reservation.company_id,
            review_version=reservation.review_version,
            line_number=reservation.line_number,
            status=ProductRemediationStatus.COMPLETED,
            product_template_id=reservation.product_template_id,
            product_id=reservation.product_id,
            supplierinfo_id=reservation.supplierinfo_id,
            created_product=created_product,
            created_supplierinfo=created_supplierinfo,
            reused_existing_product=reused_existing_product,
            already_applied=already_applied,
            safe_message=safe_message,
        )


def _has_product_not_found_for_line(reasons: tuple[ManualReviewReason, ...], line_number: str) -> bool:
    return any(
        reason.code is ManualReviewReasonCode.PRODUCT_NOT_FOUND and reason.line_number == line_number
        for reason in reasons
    )


def _normalize_optional(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _default_idempotency_key(reservation: ProductRemediationReservation) -> str:
    return (
        f"create-new-product:{reservation.company_id}:{reservation.review_id}:"
        f"{reservation.review_version}:{reservation.line_number}"
    )


__all__ = ["CreateNewProductUseCase"]
