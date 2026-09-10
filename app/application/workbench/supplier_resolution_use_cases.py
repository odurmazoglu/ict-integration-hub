"""Application-level validation of an explicit supplier-resolution decision.

This is deliberately read-only and side-effect free. It exists so a future
remediation flow (P0-3D2D) can prove a chosen resolution is safe *before*
persisting it and triggering a ``SUPPLIER_RESOLUTION`` reclassification.

- ``MATCH_EXISTING``: the selected Odoo partner must exist, be active, be
  company-compatible, and its VAT must exactly equal the immutable source
  invoice supplier VAT (same normalization as ``PartnerMatchingEngine``).
- ``CREATE_PERMANENT_SUPPLIER``: returns ``PENDING_PERMANENT_SUPPLIER`` -- the
  controlled supplier-partner writer is a separate capability and is NOT called
  here.
- ``USE_ONE_OFF_SUPPLIER``: returns ``ONE_OFF_EXECUTION_NOT_SUPPORTED`` -- the
  decision is recordable, but Vendor Bill execution against a shared one-off
  partner is deferred (see the PR's accounting audit).
"""

from __future__ import annotations

from app.application.exceptions import ApplicationError
from app.application.workbench.exceptions import (
    SupplierResolutionContractError,
    SupplierResolutionError,
    SupplierResolutionPartnerInactiveError,
    SupplierResolutionPartnerMismatchError,
    SupplierResolutionPartnerNotFoundError,
)
from app.application.workbench.ports import (
    ReviewSourceInvoiceEvidenceReader,
    SupplierResolutionPartnerReader,
)
from app.application.workbench.supplier_resolution import (
    SupplierResolution,
    SupplierResolutionMode,
    SupplierResolutionValidation,
    SupplierResolutionValidationStatus,
    normalize_supplier_vat,
)

SAFE_SUPPLIER_RESOLUTION_ERROR = "Supplier resolution validation failed."


class ValidateSupplierResolutionUseCase:
    """Read-only validator for a chosen :class:`SupplierResolution`."""

    def __init__(
        self,
        *,
        source_invoice_reader: ReviewSourceInvoiceEvidenceReader,
        partner_reader: SupplierResolutionPartnerReader,
    ) -> None:
        self._source_invoice_reader = source_invoice_reader
        self._partner_reader = partner_reader

    def execute(self, resolution: SupplierResolution) -> SupplierResolutionValidation:
        if not isinstance(resolution, SupplierResolution):
            raise SupplierResolutionContractError("A canonical SupplierResolution is required.")

        source = self._load_source(resolution)
        if source.source_invoice_id != resolution.source_invoice_id:
            raise SupplierResolutionContractError(
                "SupplierResolution source_invoice_id does not match the review's immutable source evidence."
            )
        source_vat = normalize_supplier_vat(source.invoice.supplier.tax_number)

        if resolution.mode is SupplierResolutionMode.CREATE_PERMANENT_SUPPLIER:
            return SupplierResolutionValidation(
                status=SupplierResolutionValidationStatus.PENDING_PERMANENT_SUPPLIER,
                mode=resolution.mode,
                review_id=resolution.review_id,
                company_id=resolution.company_id,
                source_supplier_tax_number=source_vat,
                safe_message=(
                    "Permanent supplier creation is a separate controlled capability; no partner is written here."
                ),
            )

        if resolution.mode is SupplierResolutionMode.USE_ONE_OFF_SUPPLIER:
            return SupplierResolutionValidation(
                status=SupplierResolutionValidationStatus.ONE_OFF_EXECUTION_NOT_SUPPORTED,
                mode=resolution.mode,
                review_id=resolution.review_id,
                company_id=resolution.company_id,
                source_supplier_tax_number=source_vat,
                safe_message=(
                    "One-off supplier resolution is recorded as intent; Vendor Bill execution against a shared "
                    "one-off partner is deferred pending an Odoo accounting model that does not pool payables."
                ),
            )

        return self._validate_match_existing(resolution, source_vat=source_vat)

    def _validate_match_existing(
        self,
        resolution: SupplierResolution,
        *,
        source_vat: str | None,
    ) -> SupplierResolutionValidation:
        if source_vat is None:
            raise SupplierResolutionPartnerMismatchError(
                "The immutable source invoice supplier has no tax number to validate against."
            )
        partner_id = resolution.resolved_partner_id
        assert partner_id is not None  # guaranteed by SupplierResolution.__post_init__
        partner = self._load_partner(partner_id)
        if partner is None:
            raise SupplierResolutionPartnerNotFoundError("The selected partner does not exist.")
        if type(partner.id) is not int or partner.id <= 0 or partner.id != partner_id:
            raise SupplierResolutionContractError("The selected partner id is invalid.")
        if not partner.active:
            raise SupplierResolutionPartnerInactiveError("The selected partner is archived/inactive.")
        if partner.company_id not in (None, resolution.company_id):
            raise SupplierResolutionContractError("The selected partner is scoped to a different company.")
        partner_vat = normalize_supplier_vat(partner.vat)
        if partner_vat is None:
            raise SupplierResolutionPartnerMismatchError("The selected partner has no tax number.")
        if partner_vat != source_vat:
            raise SupplierResolutionPartnerMismatchError(
                "The selected partner tax number does not exactly match the source invoice supplier tax number."
            )
        return SupplierResolutionValidation(
            status=SupplierResolutionValidationStatus.VALID,
            mode=resolution.mode,
            review_id=resolution.review_id,
            company_id=resolution.company_id,
            source_supplier_tax_number=source_vat,
            effective_partner_id=partner.id,
            safe_message="The selected partner exactly matches the source invoice supplier tax number.",
        )

    def _load_source(self, resolution: SupplierResolution):
        try:
            return self._source_invoice_reader.get(
                review_id=resolution.review_id,
                company_id=resolution.company_id,
            )
        except ApplicationError:
            raise
        except Exception as exc:  # noqa: BLE001 - translated to a safe supplier-resolution error
            raise SupplierResolutionError(SAFE_SUPPLIER_RESOLUTION_ERROR) from exc

    def _load_partner(self, partner_id: int):
        try:
            return self._partner_reader.find_partner_by_id(partner_id)
        except ApplicationError:
            raise
        except Exception as exc:  # noqa: BLE001 - translated to a safe supplier-resolution error
            raise SupplierResolutionError(SAFE_SUPPLIER_RESOLUTION_ERROR) from exc
