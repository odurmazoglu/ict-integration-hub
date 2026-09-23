"""Review-scoped accounting resolution (P0-PROD-15T).

Records *how this specific review's purchase should be posted* -- distinct from
``PurchasePurposeResolution`` (*why* it was purchased). A supplier-wide
``OperatingExpenseMapping`` (see ``app.application.expense_mapping``) is unsafe
for a mixed-purpose supplier, since committing one review's account to the
shared ``(company_id, vendor_partner_id)`` table would silently apply it to
every future invoice from that supplier too. This module is the review-scoped
escape hatch: an immutable, per-review-version accounting decision that never
writes to the supplier-wide table.

Only one treatment is implemented in this PR: ``EXPENSE_ACCOUNT``.
``CAPITALIZE_FIXED_ASSET``/``INVENTORY_RESALE``/``PROJECT_DIRECT_COST`` are
explicitly not represented here at all -- adding them is a deliberate future
change, not a value this module or its API contract silently accepts today.
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
    """How a review-scoped purchase should be posted.

    Exactly one member exists in this PR on purpose -- see module docstring.
    """

    EXPENSE_ACCOUNT = "expense_account"


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
    command never touches the supplier-wide mapping table.
    """

    review_id: str
    company_id: int
    expected_version: int
    treatment_type: AccountingTreatmentType
    expense_account_id: int
    expense_category: str
    approved_by: str
    note: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.review_id, "review_id is required.")
        _require_positive_int(self.company_id, "company_id must be positive.")
        _require_positive_int(self.expected_version, "expected_version must be positive.")
        if not isinstance(self.treatment_type, AccountingTreatmentType):
            raise WorkbenchContractError("A canonical AccountingTreatmentType is required.")
        _require_positive_int(self.expense_account_id, "expense_account_id must be positive.")
        if not isinstance(self.expense_category, str) or not EXPENSE_CATEGORY_PATTERN.match(
            self.expense_category.strip()
        ):
            raise WorkbenchContractError(
                "expense_category must be a short uppercase code matching ^[A-Z][A-Z0-9_]{0,63}$."
            )
        object.__setattr__(self, "expense_category", self.expense_category.strip())
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
    expense_account_id: int
    expense_category: str
    approved_by: str | None = None
    note: str | None = None
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
        _require_positive_int(self.expense_account_id, "expense_account_id must be positive.")
        if not isinstance(self.expense_category, str) or not EXPENSE_CATEGORY_PATTERN.match(self.expense_category):
            raise WorkbenchContractError(
                "expense_category must be a short uppercase code matching ^[A-Z][A-Z0-9_]{0,63}$."
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
    expense_account_id: int
    expense_category: str
    current_review_reasons: tuple[ManualReviewReason, ...] = field(default_factory=tuple)
    reclassified: bool = False
    already_applied: bool = False
    safe_message: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "current_review_reasons", tuple(self.current_review_reasons))


def _require_text(value: str | None, message: str) -> None:
    if value is None or not isinstance(value, str) or not value.strip():
        raise WorkbenchContractError(message)


def _require_positive_int(value: int | None, message: str) -> None:
    if type(value) is not int or value <= 0:
        raise WorkbenchContractError(message)


__all__ = [
    "ACCOUNTING_RESOLUTION_NOTE_MAX_LENGTH",
    "AccountingResolutionStatus",
    "AccountingTreatmentType",
    "ReviewAccountingResolution",
    "ReviewAccountingResolutionSubmissionResult",
    "SubmitReviewAccountingResolutionCommand",
]
