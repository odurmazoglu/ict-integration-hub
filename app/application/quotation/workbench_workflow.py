from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from app.application.dto import ApplicationDTO
from app.application.execution.ports import AcceptedReviewDecisionReader
from app.application.quotation.evidence import QuotationScenarioEvidenceRepository
from app.application.quotation.exceptions import (
    QuotationEvidenceConflictError,
    QuotationEvidenceError,
    QuotationEvidenceNotFoundError,
)
from app.application.quotation.orchestration import (
    CaptureAndPersistAcceptedQuotationScenariosCommand,
    CaptureAndPersistAcceptedQuotationScenariosUseCase,
)
from app.application.workbench.dto import ReviewDecisionType
from app.application.workbench.exceptions import (
    ReviewNotFoundError,
    WorkbenchCandidateReadError,
    WorkbenchContractError,
)
from app.application.workflow import WorkflowType


class WorkbenchQuotationScenarioEvidenceStatus(StrEnum):
    CAPTURED = "captured"
    ALREADY_CAPTURED = "already_captured"
    NOT_APPLICABLE = "not_applicable"
    NOT_FOUND = "not_found"
    EVIDENCE_CONFLICT = "evidence_conflict"
    CAPTURE_FAILED = "capture_failed"


@dataclass(frozen=True, slots=True)
class WorkbenchQuotationScenarioEvidenceResult(ApplicationDTO):
    review_id: str
    company_id: int
    decision_version: int
    status: WorkbenchQuotationScenarioEvidenceStatus
    decision_id: str | None = None
    persisted_scenario_ids: tuple[str, ...] = field(default_factory=tuple)
    message: str | None = None


class WorkbenchQuotationScenarioEvidenceWorkflow:
    """Post-acceptance boundary that freezes a CUSTOMER_QUOTATION decision as evidence.

    This mirrors :class:`WorkbenchVendorBillExecutionWorkflow`: it reads the
    durably persisted :class:`AcceptedReviewDecision`, discriminates the workflow,
    and only then runs the read-only Odoo capture + immutable evidence
    persistence. Every identity value comes from the persisted accepted decision,
    never from a request payload.

    Retries are safe. When immutable evidence already exists for every selected
    scenario the workflow returns ``ALREADY_CAPTURED`` without touching Odoo, so
    the mutable Proposal Scenario records are never re-read once evidence exists.
    A partial-evidence state (some scenarios persisted, some not) is impossible
    under normal operation because the orchestration is atomic and the selection
    is frozen in the decision fingerprint; if it is nonetheless observed the
    workflow fails closed.
    """

    def __init__(
        self,
        *,
        accepted_decision_reader: AcceptedReviewDecisionReader,
        evidence_repository: QuotationScenarioEvidenceRepository,
        orchestration_use_case: CaptureAndPersistAcceptedQuotationScenariosUseCase,
    ) -> None:
        self._accepted_decision_reader = accepted_decision_reader
        self._evidence_repository = evidence_repository
        self._orchestration_use_case = orchestration_use_case

    def capture(
        self,
        *,
        review_id: str,
        company_id: int,
        decision_version: int,
        trace_id: str | None = None,
    ) -> WorkbenchQuotationScenarioEvidenceResult:
        try:
            decision = self._accepted_decision_reader.get_accepted_decision(
                review_id=review_id,
                company_id=company_id,
                decision_version=decision_version,
            )
        except ReviewNotFoundError:
            return self._result(
                review_id=review_id,
                company_id=company_id,
                decision_version=decision_version,
                status=WorkbenchQuotationScenarioEvidenceStatus.NOT_FOUND,
            )

        if (
            decision.decision_type is not ReviewDecisionType.SELECT_WORKFLOW
            or decision.selected_workflow is not WorkflowType.CUSTOMER_QUOTATION
        ):
            return self._result(
                review_id=review_id,
                company_id=company_id,
                decision_version=decision_version,
                status=WorkbenchQuotationScenarioEvidenceStatus.NOT_APPLICABLE,
                decision_id=decision.decision_id,
                message="Accepted decision is not a customer quotation workflow selection.",
            )

        if decision.decision_id is None:
            return self._result(
                review_id=review_id,
                company_id=company_id,
                decision_version=decision_version,
                status=WorkbenchQuotationScenarioEvidenceStatus.CAPTURE_FAILED,
                message="Accepted customer quotation decision is missing its durable decision_id.",
            )

        selected_scenario_ids = decision.selected_quotation_scenario_ids
        already_persisted = self._already_persisted_scenario_ids(decision)
        if already_persisted == set(selected_scenario_ids):
            return self._result(
                review_id=review_id,
                company_id=company_id,
                decision_version=decision_version,
                status=WorkbenchQuotationScenarioEvidenceStatus.ALREADY_CAPTURED,
                decision_id=decision.decision_id,
                persisted_scenario_ids=selected_scenario_ids,
                message="Immutable quotation scenario evidence already exists for this accepted decision.",
            )
        if already_persisted:
            return self._result(
                review_id=review_id,
                company_id=company_id,
                decision_version=decision_version,
                status=WorkbenchQuotationScenarioEvidenceStatus.CAPTURE_FAILED,
                decision_id=decision.decision_id,
                message="Partial quotation scenario evidence detected for this accepted decision.",
            )

        command = CaptureAndPersistAcceptedQuotationScenariosCommand(
            company_id=decision.company_id,
            review_id=decision.review_id,
            decision_id=decision.decision_id,
            decision_version=decision.decision_version,
            selected_quotation_scenario_ids=selected_scenario_ids,
        )
        try:
            orchestration_result = self._orchestration_use_case.execute(command)
        except QuotationEvidenceConflictError as exc:
            return self._result(
                review_id=review_id,
                company_id=company_id,
                decision_version=decision_version,
                status=WorkbenchQuotationScenarioEvidenceStatus.EVIDENCE_CONFLICT,
                decision_id=decision.decision_id,
                message=exc.safe_message,
            )
        except (QuotationEvidenceError, WorkbenchCandidateReadError, WorkbenchContractError) as exc:
            return self._result(
                review_id=review_id,
                company_id=company_id,
                decision_version=decision_version,
                status=WorkbenchQuotationScenarioEvidenceStatus.CAPTURE_FAILED,
                decision_id=decision.decision_id,
                message=exc.safe_message,
            )

        return self._result(
            review_id=review_id,
            company_id=company_id,
            decision_version=decision_version,
            status=WorkbenchQuotationScenarioEvidenceStatus.CAPTURED,
            decision_id=decision.decision_id,
            persisted_scenario_ids=orchestration_result.persisted_scenario_ids,
        )

    def _already_persisted_scenario_ids(self, decision) -> set[str]:
        persisted: set[str] = set()
        for scenario_id in decision.selected_quotation_scenario_ids:
            try:
                self._evidence_repository.get(
                    company_id=decision.company_id,
                    review_id=decision.review_id,
                    decision_id=decision.decision_id,
                    decision_version=decision.decision_version,
                    scenario_id=scenario_id,
                )
            except QuotationEvidenceNotFoundError:
                continue
            persisted.add(scenario_id)
        return persisted

    @staticmethod
    def _result(
        *,
        review_id: str,
        company_id: int,
        decision_version: int,
        status: WorkbenchQuotationScenarioEvidenceStatus,
        decision_id: str | None = None,
        persisted_scenario_ids: tuple[str, ...] = (),
        message: str | None = None,
    ) -> WorkbenchQuotationScenarioEvidenceResult:
        return WorkbenchQuotationScenarioEvidenceResult(
            review_id=review_id,
            company_id=company_id,
            decision_version=decision_version,
            status=status,
            decision_id=decision_id,
            persisted_scenario_ids=persisted_scenario_ids,
            message=message,
        )
