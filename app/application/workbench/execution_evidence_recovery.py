"""Review-scoped Stage-1 execution-evidence recovery/repair (P0-PROD-15Z).

A pending review's *business* state can already be fully, correctly resolved
(supplier/purchase-purpose/accounting-resolution all accepted, effective reasons
empty, effective workflow ``vendor_bill``) while its derived
``WorkbenchReviewExecutionEvidence`` -- the pinned Stage-1 snapshot
``SubmitReviewDecisionUseCase`` requires before accepting a decision -- is missing
or stale. This happens whenever the review's current version was produced by a
reclassification that ran *before* a fix to the effective-decision computation
(e.g. P0-PROD-15X) was deployed: the review's reasons/workflow already reflect the
fixed computation (they are re-derived fresh every time), but the persisted
execution-evidence row was written by the old code and never automatically
refreshed.

This module is deliberately narrow: it repairs *derived* evidence only, for the
review's *current* version, and only when recomputing the review's effective state
right now reproduces exactly the persisted current reasons/workflow -- it never
changes what the review's business state *is*, only materializes the Stage-1
snapshot that state already implies. See
``app.application.workbench.execution_evidence_recovery_use_cases`` for the use
case, and ``app.application.use_cases.effective_decision`` for the shared
effective-decision computation this reuses (never duplicates).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.application.dto import ApplicationDTO
from app.application.workbench.exceptions import WorkbenchContractError


@dataclass(frozen=True, slots=True)
class RebuildExecutionEvidenceCommand(ApplicationDTO):
    """An authenticated operator's request to repair one review's Stage-1 evidence.

    ``company_id`` is never accepted from the caller -- it comes from the trusted
    request context. There is no field this command could use to change *what* the
    review's business state is; it only names *which* review's current, already
    fully-resolved state to materialize derived evidence for.
    """

    review_id: str
    company_id: int
    expected_version: int

    def __post_init__(self) -> None:
        _require_text(self.review_id, "review_id is required.")
        _require_positive_int(self.company_id, "company_id must be positive.")
        _require_positive_int(self.expected_version, "expected_version must be positive.")


@dataclass(frozen=True, slots=True)
class RebuildExecutionEvidenceResult(ApplicationDTO):
    """Typed result of :class:`RebuildReviewExecutionEvidenceUseCase`."""

    review_id: str
    company_id: int
    review_version: int
    already_applied: bool
    partner_id: int | None
    expense_account_id: int | None
    expense_category: str | None
    safe_message: str | None = None


def _require_text(value: str | None, message: str) -> None:
    if value is None or not isinstance(value, str) or not value.strip():
        raise WorkbenchContractError(message)


def _require_positive_int(value: int | None, message: str) -> None:
    if type(value) is not int or value <= 0:
        raise WorkbenchContractError(message)


__all__ = [
    "RebuildExecutionEvidenceCommand",
    "RebuildExecutionEvidenceResult",
]
