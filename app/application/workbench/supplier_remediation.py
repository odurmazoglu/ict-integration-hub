from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from app.application.dto import ApplicationDTO
from app.application.workbench.exceptions import SupplierResolutionContractError
from app.application.workbench.supplier_resolution import SupplierResolutionMode
from app.application.workflow import ManualReviewReason, WorkflowType


class SupplierPartnerWriteEffectStatus(StrEnum):
    """How the effective accounting partner came to be for a completed remediation."""

    CREATED = "created"
    ALREADY_EXISTS = "already_exists"
    SELECTED = "selected"


class SupplierRemediationStatus(StrEnum):
    """Outcome of a supplier remediation orchestration request."""

    #: MATCH_EXISTING / CREATE_PERMANENT completed and the review reclassified past SUPPLIER_NOT_FOUND.
    RESOLVED = "resolved"
    #: The reclassification ran but the supplier is still not matched (another blocker, or a bad selection).
    REMEDIATION_INCOMPLETE = "remediation_incomplete"
    #: USE_ONE_OFF_SUPPLIER -- the decision is recorded; execution against a shared one-off partner is deferred.
    ONE_OFF_EXECUTION_NOT_SUPPORTED = "one_off_execution_not_supported"


@dataclass(frozen=True, slots=True)
class ResolveWorkbenchSupplierCommand(ApplicationDTO):
    """An authenticated operator's supplier-resolution choice for one review.

    Legal supplier identity (name, VAT) is never carried -- it comes only from the
    review's immutable ``ReviewSourceInvoiceEvidence``. ``approved_by`` is the
    authenticated actor supplied by the API security context, never a body field.
    """

    review_id: str
    company_id: int
    expected_version: int
    mode: SupplierResolutionMode
    approved_by: str
    resolved_partner_id: int | None = None
    note: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.review_id, "review_id is required.")
        _require_positive_int(self.company_id, "company_id must be positive.")
        _require_positive_int(self.expected_version, "expected_version must be positive.")
        if not isinstance(self.mode, SupplierResolutionMode):
            raise SupplierResolutionContractError("A canonical SupplierResolutionMode is required.")
        _require_text(self.approved_by, "approved_by (authenticated actor) is required.")
        if self.mode is SupplierResolutionMode.MATCH_EXISTING:
            _require_positive_int(self.resolved_partner_id, "MATCH_EXISTING requires a positive partner_id.")
        elif self.resolved_partner_id is not None:
            raise SupplierResolutionContractError(
                "partner_id is only valid for MATCH_EXISTING; CREATE_PERMANENT_SUPPLIER derives the partner "
                "from the immutable source invoice and USE_ONE_OFF_SUPPLIER never yields a partner."
            )
        if self.note is not None and (not isinstance(self.note, str) or not self.note.strip()):
            raise SupplierResolutionContractError("note must be non-empty text when provided.")


@dataclass(frozen=True, slots=True)
class SupplierRemediationEffect(ApplicationDTO):
    """The completed effect of a supplier remediation for one review version."""

    review_id: str
    company_id: int
    review_version: int
    source_invoice_id: str
    mode: SupplierResolutionMode
    resolved_partner_id: int
    partner_write_status: SupplierPartnerWriteEffectStatus
    source_supplier_tax_number: str | None = None
    approved_by: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.review_id, "review_id is required.")
        _require_positive_int(self.company_id, "company_id must be positive.")
        _require_positive_int(self.review_version, "review_version must be positive.")
        _require_text(self.source_invoice_id, "source_invoice_id is required.")
        if not isinstance(self.mode, SupplierResolutionMode):
            raise SupplierResolutionContractError("A canonical SupplierResolutionMode is required.")
        if self.mode is SupplierResolutionMode.USE_ONE_OFF_SUPPLIER:
            raise SupplierResolutionContractError("USE_ONE_OFF_SUPPLIER has no completed remediation effect.")
        _require_positive_int(self.resolved_partner_id, "resolved_partner_id must be positive.")
        if not isinstance(self.partner_write_status, SupplierPartnerWriteEffectStatus):
            raise SupplierResolutionContractError("A canonical SupplierPartnerWriteEffectStatus is required.")
        if self.mode is SupplierResolutionMode.MATCH_EXISTING and (
            self.partner_write_status is not SupplierPartnerWriteEffectStatus.SELECTED
        ):
            raise SupplierResolutionContractError("MATCH_EXISTING remediation effect must be SELECTED.")
        if self.mode is SupplierResolutionMode.CREATE_PERMANENT_SUPPLIER and (
            self.partner_write_status is SupplierPartnerWriteEffectStatus.SELECTED
        ):
            raise SupplierResolutionContractError(
                "CREATE_PERMANENT_SUPPLIER remediation effect must be CREATED or ALREADY_EXISTS."
            )


@dataclass(frozen=True, slots=True)
class SupplierRemediationResult(ApplicationDTO):
    """Typed result of :class:`ResolveWorkbenchSupplierUseCase`."""

    review_id: str
    company_id: int
    mode: SupplierResolutionMode
    status: SupplierRemediationStatus
    previous_version: int
    current_version: int
    current_workflow: WorkflowType
    current_review_reasons: tuple[ManualReviewReason, ...] = field(default_factory=tuple)
    effective_partner_id: int | None = None
    partner_write_status: SupplierPartnerWriteEffectStatus | None = None
    reclassified: bool = False
    already_applied: bool = False
    workbench_republished: bool = False
    safe_message: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "current_review_reasons", tuple(self.current_review_reasons))


def _require_text(value: str | None, message: str) -> None:
    if value is None or not isinstance(value, str) or not value.strip():
        raise SupplierResolutionContractError(message)


def _require_positive_int(value: int | None, message: str) -> None:
    if type(value) is not int or value <= 0:
        raise SupplierResolutionContractError(message)
