"""Review-scoped accounting resolution (P0-PROD-15T).

Records *how this specific review's purchase should be posted* -- distinct from
``PurchasePurposeResolution`` (*why* it was purchased). A supplier-wide
``OperatingExpenseMapping`` (see ``app.application.expense_mapping``) is unsafe
for a mixed-purpose supplier, since committing one review's account to the
shared ``(company_id, vendor_partner_id)`` table would silently apply it to
every future invoice from that supplier too. This module is the review-scoped
escape hatch: an immutable, per-review-version accounting decision that never
writes to the supplier-wide table.

Two treatments are implemented, each with its own exact field set:

* ``EXPENSE_ACCOUNT`` -- ``expense_account_id`` + ``expense_category`` (P0-PROD-15T);
* ``CAPITALIZE_FIXED_ASSET`` -- ``asset_account_id`` + ``depreciation_model_id``: the
  operator's explicit Odoo fixed-asset account and depreciation model. Allowed only for
  the INTERNAL_USE purchase purpose. The Hub persists the selection and freezes it into
  execution evidence; Odoo creates/depreciates the asset when a human posts the bill
  (see ``docs/FIXED_ASSET_ACCOUNTING.md``).

A payload carrying the other treatment's fields is rejected, never reinterpreted.
``INVENTORY_RESALE``/``PROJECT_DIRECT_COST`` are still not represented at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from app.application.dto import ApplicationDTO
from app.application.expense_mapping.contracts import EXPENSE_CATEGORY_PATTERN
from app.application.workbench.exceptions import WorkbenchContractError
from app.application.workflow import ManualReviewReason, WorkflowType

ACCOUNTING_RESOLUTION_NOTE_MAX_LENGTH = 1024


class AccountingTreatmentType(StrEnum):
    """How a review-scoped purchase should be posted -- see module docstring."""

    EXPENSE_ACCOUNT = "expense_account"
    CAPITALIZE_FIXED_ASSET = "capitalize_fixed_asset"


class AccountingResolutionStatus(StrEnum):
    """Outcome of an accounting-resolution submission request."""

    #: The resolution was accepted and the review reclassified past the
    #: operating-expense-shaped blocker it targets.
    RESOLVED = "resolved"
    #: The resolution was accepted and the review reclassified, but the review
    #: still carries an operating-expense-shaped reason (another blocker).
    REMEDIATION_INCOMPLETE = "remediation_incomplete"


@dataclass(frozen=True, slots=True)
class SubmitReviewAccountingResolutionCommand(ApplicationDTO):
    """An authenticated operator's explicit accounting decision for one review.

    ``approved_by``/``company_id`` are never accepted from the caller -- both come
    from the trusted request context. There is no ``vendor_partner_id`` field: this
    command never touches the supplier-wide mapping table. Exactly the field set of
    ``treatment_type`` must be present (see :func:`validate_treatment_fields`).
    """

    review_id: str
    company_id: int
    expected_version: int
    treatment_type: AccountingTreatmentType
    approved_by: str
    expense_account_id: int | None = None
    expense_category: str | None = None
    asset_account_id: int | None = None
    depreciation_model_id: int | None = None
    note: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.review_id, "review_id is required.")
        _require_positive_int(self.company_id, "company_id must be positive.")
        _require_positive_int(self.expected_version, "expected_version must be positive.")
        if not isinstance(self.treatment_type, AccountingTreatmentType):
            raise WorkbenchContractError("A canonical AccountingTreatmentType is required.")
        if isinstance(self.expense_category, str):
            object.__setattr__(self, "expense_category", self.expense_category.strip())
        validate_treatment_fields(
            self.treatment_type,
            expense_account_id=self.expense_account_id,
            expense_category=self.expense_category,
            asset_account_id=self.asset_account_id,
            depreciation_model_id=self.depreciation_model_id,
        )
        _require_text(self.approved_by, "approved_by (authenticated actor) is required.")
        if self.note is not None:
            if not isinstance(self.note, str) or not self.note.strip():
                raise WorkbenchContractError("note must be non-empty text when provided.")
            if len(self.note) > ACCOUNTING_RESOLUTION_NOTE_MAX_LENGTH:
                raise WorkbenchContractError(
                    f"note must be {ACCOUNTING_RESOLUTION_NOTE_MAX_LENGTH} characters or fewer."
                )


@dataclass(frozen=True, slots=True)
class ReviewAccountingResolution(ApplicationDTO):
    """The durable, immutable, review-scoped accounting decision.

    Identity is ``(review_id, company_id, review_version)`` -- append-only, one per
    review version, mirrors ``SupplierRemediationEffect``/``PurchasePurposeResolution``.
    """

    review_id: str
    company_id: int
    review_version: int
    treatment_type: AccountingTreatmentType
    expense_account_id: int | None = None
    expense_category: str | None = None
    approved_by: str | None = None
    note: str | None = None
    #: CAPITALIZE_FIXED_ASSET only: the operator-selected Odoo fixed-asset account and
    #: depreciation model, persisted verbatim (never derived from Odoo account defaults).
    asset_account_id: int | None = None
    depreciation_model_id: int | None = None
    #: The persisted row's own id -- reused by ``ReclassifyWorkbenchReviewUseCase``
    #: as the synthesized ``OperatingExpenseMatchResult.mapping_id`` (there is no
    #: real ``OperatingExpenseMapping`` row for a review-scoped resolution, but a
    #: positive, traceable id is still required). ``None`` only for an in-memory
    #: instance not yet persisted.
    id: int | None = None

    def __post_init__(self) -> None:
        _require_text(self.review_id, "review_id is required.")
        _require_positive_int(self.company_id, "company_id must be positive.")
        _require_positive_int(self.review_version, "review_version must be positive.")
        if not isinstance(self.treatment_type, AccountingTreatmentType):
            raise WorkbenchContractError("A canonical AccountingTreatmentType is required.")
        validate_treatment_fields(
            self.treatment_type,
            expense_account_id=self.expense_account_id,
            expense_category=self.expense_category,
            asset_account_id=self.asset_account_id,
            depreciation_model_id=self.depreciation_model_id,
        )
        if self.id is not None:
            _require_positive_int(self.id, "id must be positive when set.")


@dataclass(frozen=True, slots=True)
class ReviewAccountingResolutionSubmissionResult(ApplicationDTO):
    """Typed result of :class:`SubmitReviewAccountingResolutionUseCase`."""

    review_id: str
    company_id: int
    status: AccountingResolutionStatus
    previous_version: int
    current_version: int
    current_workflow: WorkflowType
    treatment_type: AccountingTreatmentType
    expense_account_id: int | None = None
    expense_category: str | None = None
    asset_account_id: int | None = None
    depreciation_model_id: int | None = None
    current_review_reasons: tuple[ManualReviewReason, ...] = field(default_factory=tuple)
    reclassified: bool = False
    already_applied: bool = False
    safe_message: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "current_review_reasons", tuple(self.current_review_reasons))


def validate_treatment_fields(
    treatment_type: AccountingTreatmentType,
    *,
    expense_account_id: object,
    expense_category: object,
    asset_account_id: object,
    depreciation_model_id: object,
) -> None:
    """Exactly the treatment's own fields, never the other treatment's.

    EXPENSE_ACCOUNT: ``expense_account_id`` + ``expense_category`` required, asset fields
    absent. CAPITALIZE_FIXED_ASSET: ``asset_account_id`` + ``depreciation_model_id``
    required, expense fields absent. Anything else is ambiguous and rejected.
    """

    if treatment_type is AccountingTreatmentType.EXPENSE_ACCOUNT:
        if asset_account_id is not None or depreciation_model_id is not None:
            raise WorkbenchContractError(
                "asset_account_id/depreciation_model_id are not valid for treatment_type=expense_account."
            )
        _require_positive_int(expense_account_id, "expense_account_id must be positive.")
        if not isinstance(expense_category, str) or not EXPENSE_CATEGORY_PATTERN.match(expense_category):
            raise WorkbenchContractError(
                "expense_category must be a short uppercase code matching ^[A-Z][A-Z0-9_]{0,63}$."
            )
        return
    if treatment_type is AccountingTreatmentType.CAPITALIZE_FIXED_ASSET:
        if expense_account_id is not None or expense_category is not None:
            raise WorkbenchContractError(
                "expense_account_id/expense_category are not valid for treatment_type=capitalize_fixed_asset."
            )
        _require_positive_int(asset_account_id, "asset_account_id must be positive.")
        _require_positive_int(depreciation_model_id, "depreciation_model_id must be positive.")
        return
    raise WorkbenchContractError("A canonical AccountingTreatmentType is required.")


def _require_text(value: str | None, message: str) -> None:
    if value is None or not isinstance(value, str) or not value.strip():
        raise WorkbenchContractError(message)


def _require_positive_int(value: object, message: str) -> None:
    if type(value) is not int or value <= 0:
        raise WorkbenchContractError(message)


__all__ = [
    "ACCOUNTING_RESOLUTION_NOTE_MAX_LENGTH",
    "AccountingResolutionStatus",
    "AccountingTreatmentType",
    "ReviewAccountingResolution",
    "ReviewAccountingResolutionSubmissionResult",
    "SubmitReviewAccountingResolutionCommand",
    "validate_treatment_fields",
]
