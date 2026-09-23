"""DTOs for the operator-facing operating-expense-mapping remediation (P0-PROD-15P).

Mirrors ``supplier_remediation.py``'s shape: an explicit operator command, and a
typed result that never claims success while the actionable reason it targets is
still present in the reclassified review.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from app.application.dto import ApplicationDTO
from app.application.expense_mapping.contracts import EXPENSE_CATEGORY_PATTERN
from app.application.expense_mapping.onboarding import OperatingExpenseMappingOnboardingOutcome
from app.application.workbench.exceptions import WorkbenchContractError
from app.application.workflow import ManualReviewReason, WorkflowType


class OperatingExpenseMappingSubmissionStatus(StrEnum):
    """Outcome of an operating-expense-mapping remediation request."""

    #: The mapping was onboarded (or already matched an existing one) and the review
    #: reclassified past OPERATING_EXPENSE_MAPPING_REQUIRED.
    RESOLVED = "resolved"
    #: The mapping was onboarded and the review reclassified, but the review still
    #: carries OPERATING_EXPENSE_MAPPING_REQUIRED (another blocker, e.g. a data race).
    REMEDIATION_INCOMPLETE = "remediation_incomplete"


@dataclass(frozen=True, slots=True)
class SubmitOperatingExpenseMappingCommand(ApplicationDTO):
    """An authenticated operator's explicit operating-expense-mapping selection for one review.

    ``vendor_partner_id`` is deliberately absent -- it is derived server-side only from
    the review's own accepted ``SupplierRemediationEffect``, never accepted from the
    caller (see ``SubmitOperatingExpenseMappingUseCase._require_resolved_supplier``).
    """

    review_id: str
    company_id: int
    expected_version: int
    expense_account_id: int
    expense_category: str
    approved_by: str
    note: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.review_id, "review_id is required.")
        _require_positive_int(self.company_id, "company_id must be positive.")
        _require_positive_int(self.expected_version, "expected_version must be positive.")
        _require_positive_int(self.expense_account_id, "expense_account_id must be positive.")
        if not isinstance(self.expense_category, str) or not EXPENSE_CATEGORY_PATTERN.match(
            self.expense_category.strip()
        ):
            raise WorkbenchContractError(
                "expense_category must be a short uppercase code matching ^[A-Z][A-Z0-9_]{0,63}$."
            )
        object.__setattr__(self, "expense_category", self.expense_category.strip())
        _require_text(self.approved_by, "approved_by (authenticated actor) is required.")
        if self.note is not None and (not isinstance(self.note, str) or not self.note.strip()):
            raise WorkbenchContractError("note must be non-empty text when provided.")


@dataclass(frozen=True, slots=True)
class OperatingExpenseMappingSubmissionResult(ApplicationDTO):
    """Typed result of :class:`SubmitOperatingExpenseMappingUseCase`."""

    review_id: str
    company_id: int
    status: OperatingExpenseMappingSubmissionStatus
    previous_version: int
    current_version: int
    current_workflow: WorkflowType
    vendor_partner_id: int
    expense_account_id: int
    expense_category: str
    mapping_outcome: OperatingExpenseMappingOnboardingOutcome
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
    "OperatingExpenseMappingSubmissionResult",
    "OperatingExpenseMappingSubmissionStatus",
    "SubmitOperatingExpenseMappingCommand",
]
