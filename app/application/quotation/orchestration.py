from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.application.dto import ApplicationDTO
from app.application.quotation.capture import CaptureQuotationScenarioCommand
from app.application.quotation.contracts import QuotationScenarioSnapshot
from app.application.quotation.exceptions import QuotationScenarioOrchestrationError
from app.application.services import UnitOfWork


class QuotationScenarioCapturer(Protocol):
    """Read-only capture port: one accepted scenario id to a canonical snapshot."""

    def execute(self, command: CaptureQuotationScenarioCommand) -> QuotationScenarioSnapshot:
        pass


class QuotationScenarioEvidencePersister(Protocol):
    """Persistence port: one canonical snapshot to durable immutable Hub evidence."""

    def execute(self, snapshot: QuotationScenarioSnapshot) -> QuotationScenarioSnapshot:
        pass


@dataclass(frozen=True, slots=True)
class CaptureAndPersistAcceptedQuotationScenariosCommand(ApplicationDTO):
    """Immutable accepted-decision identity plus the frozen selected scenario ids."""

    company_id: int
    review_id: str
    decision_id: str
    decision_version: int
    selected_quotation_scenario_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_positive_int(self.company_id, "company_id must be positive.")
        _require_text(self.review_id, "review_id is required.")
        _require_text(self.decision_id, "decision_id is required.")
        _require_positive_int(self.decision_version, "decision_version must be positive.")
        scenario_ids = tuple(self.selected_quotation_scenario_ids)
        object.__setattr__(self, "selected_quotation_scenario_ids", scenario_ids)
        if not scenario_ids:
            raise QuotationScenarioOrchestrationError("selected_quotation_scenario_ids must not be empty.")
        seen: set[str] = set()
        for scenario_id in scenario_ids:
            if not isinstance(scenario_id, str) or not scenario_id.strip():
                raise QuotationScenarioOrchestrationError(
                    "selected_quotation_scenario_ids must be non-empty identifiers."
                )
            if scenario_id in seen:
                raise QuotationScenarioOrchestrationError("selected_quotation_scenario_ids must be unique.")
            seen.add(scenario_id)


@dataclass(frozen=True, slots=True)
class AcceptedQuotationScenarioEvidenceResult(ApplicationDTO):
    """Typed outcome: which selected scenarios now have immutable Hub evidence."""

    company_id: int
    review_id: str
    decision_id: str
    decision_version: int
    persisted_scenario_ids: tuple[str, ...]


class CaptureAndPersistAcceptedQuotationScenariosUseCase:
    """Freeze an accepted customer-quotation decision as immutable Hub evidence.

    Selection intent (which scenarios were approved for customer presentation) is
    an explicit part of the accepted decision. This use case reads the current
    commercial contents of each selected scenario from Odoo *read-only*, then
    persists each resulting :class:`QuotationScenarioSnapshot` as immutable
    Hub-owned evidence. It performs no ``sale.order`` write and no Odoo authoring
    write.

    Guarantee: every selected scenario is captured from Odoo into memory first;
    only when *all* captures and identity checks succeed is *all* evidence
    persisted in a single Hub database transaction (one commit, rollback on any
    failure). Odoo network reads never happen inside the database transaction. A
    partial capture failure persists nothing, so downstream execution stays
    blocked until every expected evidence row exists. Replay is idempotent;
    a changed commercial snapshot under the same identity fails closed via the
    persistence layer's evidence-conflict semantics.
    """

    def __init__(
        self,
        *,
        capture_use_case: QuotationScenarioCapturer,
        persist_use_case: QuotationScenarioEvidencePersister,
        unit_of_work: UnitOfWork,
    ) -> None:
        self._capture_use_case = capture_use_case
        self._persist_use_case = persist_use_case
        self._unit_of_work = unit_of_work

    def execute(
        self,
        command: CaptureAndPersistAcceptedQuotationScenariosCommand,
    ) -> AcceptedQuotationScenarioEvidenceResult:
        if not isinstance(command, CaptureAndPersistAcceptedQuotationScenariosCommand):
            raise QuotationScenarioOrchestrationError("CaptureAndPersistAcceptedQuotationScenariosCommand is required.")

        snapshots = tuple(
            self._capture(command, scenario_id) for scenario_id in command.selected_quotation_scenario_ids
        )

        try:
            for snapshot in snapshots:
                self._persist_use_case.execute(snapshot)
            self._unit_of_work.commit()
        except BaseException:
            self._unit_of_work.rollback()
            raise

        return AcceptedQuotationScenarioEvidenceResult(
            company_id=command.company_id,
            review_id=command.review_id,
            decision_id=command.decision_id,
            decision_version=command.decision_version,
            persisted_scenario_ids=tuple(snapshot.scenario_id for snapshot in snapshots),
        )

    def _capture(
        self,
        command: CaptureAndPersistAcceptedQuotationScenariosCommand,
        scenario_id: str,
    ) -> QuotationScenarioSnapshot:
        snapshot = self._capture_use_case.execute(
            CaptureQuotationScenarioCommand(
                review_id=command.review_id,
                decision_id=command.decision_id,
                decision_version=command.decision_version,
                company_id=command.company_id,
                scenario_id=scenario_id,
            )
        )
        if not isinstance(snapshot, QuotationScenarioSnapshot):
            raise QuotationScenarioOrchestrationError("capture must return a canonical QuotationScenarioSnapshot.")
        identity = (
            snapshot.company_id,
            snapshot.review_id,
            snapshot.decision_id,
            snapshot.decision_version,
            snapshot.scenario_id,
        )
        expected = (
            command.company_id,
            command.review_id,
            command.decision_id,
            command.decision_version,
            scenario_id,
        )
        if identity != expected:
            raise QuotationScenarioOrchestrationError(
                "captured scenario identity does not match the accepted decision."
            )
        return snapshot


def _require_text(value: object, message: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise QuotationScenarioOrchestrationError(message)


def _require_positive_int(value: object, message: str) -> None:
    if type(value) is not int or value <= 0:
        raise QuotationScenarioOrchestrationError(message)


__all__ = [
    "AcceptedQuotationScenarioEvidenceResult",
    "CaptureAndPersistAcceptedQuotationScenariosCommand",
    "CaptureAndPersistAcceptedQuotationScenariosUseCase",
]
