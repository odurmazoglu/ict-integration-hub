from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from app.application.commands import Command
from app.application.dto import ApplicationDTO
from app.application.exceptions import ApplicationError
from app.application.workbench.commands import ReviewDecisionCommand
from app.application.workbench.decision_use_cases import SubmitReviewDecisionUseCase
from app.application.workbench.dto import ReviewDecisionAcknowledgement
from app.application.workbench.exceptions import (
    WorkbenchCandidateNotFoundError,
    WorkbenchContractError,
    WorkbenchSubmissionCompanyMismatchError,
    WorkbenchSubmissionOrchestrationError,
)
from app.application.workbench.ports import WorkbenchDecisionCandidateReader
from app.application.workbench.projection import OdooWorkbenchDecisionCandidate

SAFE_SUBMISSION_ORCHESTRATION_ERROR = "Odoo Workbench decision submission orchestration failed."
SAFE_COMPANY_MISMATCH_ERROR = "Odoo Workbench decision candidate company scope mismatch."


class OdooWorkbenchSubmissionStatus(StrEnum):
    """Canonical result states for one Odoo Workbench candidate submission attempt."""

    SUBMITTED = "submitted"
    NOT_READY_OR_NOT_FOUND = "not_ready_or_not_found"


@dataclass(frozen=True, slots=True)
class SubmitOdooWorkbenchCandidateCommand(Command):
    """Request to submit one decision-ready Odoo Workbench candidate into Hub evidence."""

    review_id: str
    company_id: int

    def __post_init__(self) -> None:
        _require_text(self.review_id, "review_id is required.")
        _require_positive_int(self.company_id, "company_id must be positive.")


@dataclass(frozen=True, slots=True)
class OdooWorkbenchSubmissionResult(ApplicationDTO):
    """Safe immutable result for one Odoo Workbench candidate orchestration."""

    submitted: bool
    review_id: str
    status: OdooWorkbenchSubmissionStatus
    acknowledgement: ReviewDecisionAcknowledgement | None = None

    def __post_init__(self) -> None:
        if type(self.submitted) is not bool:
            raise WorkbenchContractError("submitted must be a boolean value.")
        _require_text(self.review_id, "review_id is required.")
        if not isinstance(self.status, OdooWorkbenchSubmissionStatus):
            raise WorkbenchContractError("status must be a canonical OdooWorkbenchSubmissionStatus.")
        if self.submitted != (self.status is OdooWorkbenchSubmissionStatus.SUBMITTED):
            raise WorkbenchContractError("submitted must match submission status.")
        if self.submitted and self.acknowledgement is None:
            raise WorkbenchContractError("acknowledgement is required for submitted results.")
        if not self.submitted and self.acknowledgement is not None:
            raise WorkbenchContractError("acknowledgement is only allowed for submitted results.")


class SubmitOdooWorkbenchCandidateUseCase:
    """Submit one decision-ready Odoo Workbench candidate through the Hub decision boundary."""

    def __init__(
        self,
        *,
        candidate_reader: WorkbenchDecisionCandidateReader,
        decision_submitter: SubmitReviewDecisionUseCase,
    ) -> None:
        self._candidate_reader = candidate_reader
        self._decision_submitter = decision_submitter

    def execute(self, command: SubmitOdooWorkbenchCandidateCommand) -> OdooWorkbenchSubmissionResult:
        if not isinstance(command, SubmitOdooWorkbenchCandidateCommand):
            raise WorkbenchContractError("SubmitOdooWorkbenchCandidateCommand is required.")

        candidate = _translate_submission_orchestration_failure(
            lambda: self._candidate_reader.get_ready_decision(
                review_id=command.review_id,
                company_id=command.company_id,
            )
        )
        if candidate is None:
            return _not_ready_or_not_found(command.review_id)
        _require_candidate_company_scope(candidate, requested_company_id=command.company_id)

        review_command = ReviewDecisionCommand(
            review_id=candidate.review_id,
            company_id=candidate.company_id,
            expected_version=candidate.expected_version,
            decision=candidate.decision,
            decided_by=f"odoo:{candidate.decided_by_odoo_user_id}",
            idempotency_key=candidate.idempotency_key,
            selected_workflow=candidate.selected_workflow,
            selected_partner_id=candidate.selected_partner_id,
            line_resolutions=candidate.line_resolutions,
            tax_resolutions=candidate.tax_resolutions,
            business_context_allocations=candidate.business_context_allocations,
            comment=candidate.comment,
        )
        acknowledgement = _translate_submission_orchestration_failure(
            lambda: self._decision_submitter.execute(review_command)
        )
        return OdooWorkbenchSubmissionResult(
            submitted=True,
            review_id=command.review_id,
            status=OdooWorkbenchSubmissionStatus.SUBMITTED,
            acknowledgement=acknowledgement,
        )


def _not_ready_or_not_found(review_id: str) -> OdooWorkbenchSubmissionResult:
    return OdooWorkbenchSubmissionResult(
        submitted=False,
        review_id=review_id,
        status=OdooWorkbenchSubmissionStatus.NOT_READY_OR_NOT_FOUND,
    )


def _require_candidate_company_scope(candidate: OdooWorkbenchDecisionCandidate, *, requested_company_id: int) -> None:
    if candidate.company_id == requested_company_id:
        return
    raise WorkbenchSubmissionCompanyMismatchError(SAFE_COMPANY_MISMATCH_ERROR)


def _translate_submission_orchestration_failure[ResultT](operation: Callable[[], ResultT]) -> ResultT | None:
    try:
        return operation()
    except WorkbenchCandidateNotFoundError:
        return None
    except ApplicationError:
        raise
    except Exception as exc:
        raise WorkbenchSubmissionOrchestrationError(SAFE_SUBMISSION_ORCHESTRATION_ERROR) from exc


def _require_text(value: str | None, message: str) -> None:
    if value is None or not value.strip():
        raise WorkbenchContractError(message)


def _require_positive_int(value: int, message: str) -> None:
    if type(value) is not int or value <= 0:
        raise WorkbenchContractError(message)
