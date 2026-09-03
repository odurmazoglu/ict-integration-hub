from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.application.execution import AcceptedReviewDecision
from app.application.execution.exceptions import ExecutionPlanningError
from app.application.quotation import (
    AcceptedQuotationScenarioEvidenceResult,
    CaptureAndPersistAcceptedQuotationScenariosCommand,
    PersistQuotationScenarioEvidenceUseCase,
    QuotationEvidenceConflictError,
    QuotationScenarioLine,
    QuotationScenarioOrchestrationError,
    QuotationScenarioSnapshot,
    WorkbenchQuotationScenarioEvidenceStatus,
    WorkbenchQuotationScenarioEvidenceWorkflow,
)
from app.application.workbench.dto import ReviewDecisionType
from app.application.workbench.exceptions import ReviewNotFoundError
from app.application.workflow import WorkflowType
from app.db.base import Base
from app.models.quotation_scenario_evidence import QuotationScenarioEvidence
from app.persistence import SqlAlchemyQuotationScenarioEvidenceRepository

COMPANY_ID = 7
REVIEW_ID = "review-1"
DECISION_ID = "decision-xyz"
DECISION_VERSION = 4


def _decision(
    *,
    selected_workflow: WorkflowType | None = WorkflowType.CUSTOMER_QUOTATION,
    scenario_ids: tuple[str, ...] = ("scenario-a", "scenario-b"),
    decision_id: str | None = DECISION_ID,
    decision_type: ReviewDecisionType = ReviewDecisionType.SELECT_WORKFLOW,
) -> AcceptedReviewDecision:
    return AcceptedReviewDecision(
        review_id=REVIEW_ID,
        company_id=COMPANY_ID,
        decision_version=DECISION_VERSION,
        decision_id=decision_id,
        selected_workflow=selected_workflow,
        selected_quotation_scenario_ids=scenario_ids,
        decision_type=decision_type,
    )


def _snapshot(scenario_id: str, *, sales_unit_price: str = "10.00") -> QuotationScenarioSnapshot:
    return QuotationScenarioSnapshot(
        scenario_id=scenario_id,
        scenario_name=f"Scenario {scenario_id}",
        company_id=COMPANY_ID,
        customer_id=501,
        currency="eur",
        lines=(
            QuotationScenarioLine(
                line_id="line-1",
                product_variant_id=10,
                quantity=Decimal("2"),
                sales_unit_price=Decimal(sales_unit_price),
            ),
        ),
        review_id=REVIEW_ID,
        decision_id=DECISION_ID,
        decision_version=DECISION_VERSION,
    )


class FakeAcceptedDecisionReader:
    def __init__(self, decision: AcceptedReviewDecision | Exception) -> None:
        self._decision = decision
        self.calls: list[dict[str, object]] = []

    def get_accepted_decision(
        self, *, review_id: str, company_id: int, decision_version: int
    ) -> AcceptedReviewDecision:
        self.calls.append({"review_id": review_id, "company_id": company_id, "decision_version": decision_version})
        if isinstance(self._decision, Exception):
            raise self._decision
        return self._decision


class FakeOrchestration:
    def __init__(self, *, error: Exception | None = None) -> None:
        self._error = error
        self.commands: list[CaptureAndPersistAcceptedQuotationScenariosCommand] = []

    def execute(
        self,
        command: CaptureAndPersistAcceptedQuotationScenariosCommand,
    ) -> AcceptedQuotationScenarioEvidenceResult:
        self.commands.append(command)
        if self._error is not None:
            raise self._error
        return AcceptedQuotationScenarioEvidenceResult(
            company_id=command.company_id,
            review_id=command.review_id,
            decision_id=command.decision_id,
            decision_version=command.decision_version,
            persisted_scenario_ids=command.selected_quotation_scenario_ids,
        )


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[QuotationScenarioEvidence.__table__])
    factory = sessionmaker(bind=engine)
    with factory() as db_session:
        yield db_session


@pytest.fixture()
def evidence_repository(session: Session) -> SqlAlchemyQuotationScenarioEvidenceRepository:
    return SqlAlchemyQuotationScenarioEvidenceRepository(session)


def _workflow(
    *,
    decision: AcceptedReviewDecision | Exception,
    evidence_repository: SqlAlchemyQuotationScenarioEvidenceRepository,
    orchestration: FakeOrchestration,
) -> tuple[WorkbenchQuotationScenarioEvidenceWorkflow, FakeAcceptedDecisionReader]:
    reader = FakeAcceptedDecisionReader(decision)
    workflow = WorkbenchQuotationScenarioEvidenceWorkflow(
        accepted_decision_reader=reader,
        evidence_repository=evidence_repository,
        orchestration_use_case=orchestration,
    )
    return workflow, reader


def _capture(workflow: WorkbenchQuotationScenarioEvidenceWorkflow):
    return workflow.capture(review_id=REVIEW_ID, company_id=COMPANY_ID, decision_version=DECISION_VERSION)


def test_accepted_customer_quotation_invokes_orchestration(
    evidence_repository: SqlAlchemyQuotationScenarioEvidenceRepository,
) -> None:
    orchestration = FakeOrchestration()
    workflow, _reader = _workflow(
        decision=_decision(), evidence_repository=evidence_repository, orchestration=orchestration
    )

    result = _capture(workflow)

    assert result.status is WorkbenchQuotationScenarioEvidenceStatus.CAPTURED
    assert result.persisted_scenario_ids == ("scenario-a", "scenario-b")
    assert len(orchestration.commands) == 1


def test_orchestration_command_comes_from_persisted_decision_not_request(
    evidence_repository: SqlAlchemyQuotationScenarioEvidenceRepository,
) -> None:
    orchestration = FakeOrchestration()
    workflow, _reader = _workflow(
        decision=_decision(scenario_ids=("scenario-c", "scenario-a")),
        evidence_repository=evidence_repository,
        orchestration=orchestration,
    )

    _capture(workflow)

    command = orchestration.commands[0]
    assert command.company_id == COMPANY_ID
    assert command.review_id == REVIEW_ID
    assert command.decision_id == DECISION_ID
    assert command.decision_version == DECISION_VERSION
    assert command.selected_quotation_scenario_ids == ("scenario-c", "scenario-a")


def test_non_customer_quotation_workflow_does_not_invoke_orchestration(
    evidence_repository: SqlAlchemyQuotationScenarioEvidenceRepository,
) -> None:
    orchestration = FakeOrchestration()
    workflow, _reader = _workflow(
        decision=_decision(selected_workflow=WorkflowType.RFQ, scenario_ids=()),
        evidence_repository=evidence_repository,
        orchestration=orchestration,
    )

    result = _capture(workflow)

    assert result.status is WorkbenchQuotationScenarioEvidenceStatus.NOT_APPLICABLE
    assert orchestration.commands == []


def test_orchestration_failure_blocks_with_capture_failed_status(
    evidence_repository: SqlAlchemyQuotationScenarioEvidenceRepository,
) -> None:
    orchestration = FakeOrchestration(error=QuotationScenarioOrchestrationError("odoo capture failed"))
    workflow, _reader = _workflow(
        decision=_decision(), evidence_repository=evidence_repository, orchestration=orchestration
    )

    result = _capture(workflow)

    assert result.status is WorkbenchQuotationScenarioEvidenceStatus.CAPTURE_FAILED
    assert result.status is not WorkbenchQuotationScenarioEvidenceStatus.CAPTURED
    assert result.message == "odoo capture failed"


def test_evidence_conflict_is_surfaced_and_blocks(
    evidence_repository: SqlAlchemyQuotationScenarioEvidenceRepository,
) -> None:
    orchestration = FakeOrchestration(error=QuotationEvidenceConflictError("evidence conflict"))
    workflow, _reader = _workflow(
        decision=_decision(), evidence_repository=evidence_repository, orchestration=orchestration
    )

    result = _capture(workflow)

    assert result.status is WorkbenchQuotationScenarioEvidenceStatus.EVIDENCE_CONFLICT


def test_retry_is_idempotent_and_does_not_reread_odoo_when_evidence_exists(
    session: Session,
    evidence_repository: SqlAlchemyQuotationScenarioEvidenceRepository,
) -> None:
    persist = PersistQuotationScenarioEvidenceUseCase(repository=evidence_repository)
    persist.execute(_snapshot("scenario-a"))
    persist.execute(_snapshot("scenario-b"))
    session.commit()

    orchestration = FakeOrchestration()
    workflow, _reader = _workflow(
        decision=_decision(), evidence_repository=evidence_repository, orchestration=orchestration
    )

    result = _capture(workflow)

    assert result.status is WorkbenchQuotationScenarioEvidenceStatus.ALREADY_CAPTURED
    assert result.persisted_scenario_ids == ("scenario-a", "scenario-b")
    assert orchestration.commands == []


def test_partial_existing_evidence_fails_closed_without_odoo_reread(
    session: Session,
    evidence_repository: SqlAlchemyQuotationScenarioEvidenceRepository,
) -> None:
    persist = PersistQuotationScenarioEvidenceUseCase(repository=evidence_repository)
    persist.execute(_snapshot("scenario-a"))
    session.commit()

    orchestration = FakeOrchestration()
    workflow, _reader = _workflow(
        decision=_decision(), evidence_repository=evidence_repository, orchestration=orchestration
    )

    result = _capture(workflow)

    assert result.status is WorkbenchQuotationScenarioEvidenceStatus.CAPTURE_FAILED
    assert orchestration.commands == []


def test_missing_accepted_decision_returns_not_found(
    evidence_repository: SqlAlchemyQuotationScenarioEvidenceRepository,
) -> None:
    orchestration = FakeOrchestration()
    workflow, _reader = _workflow(
        decision=ReviewNotFoundError("not found"),
        evidence_repository=evidence_repository,
        orchestration=orchestration,
    )

    result = _capture(workflow)

    assert result.status is WorkbenchQuotationScenarioEvidenceStatus.NOT_FOUND
    assert orchestration.commands == []


def test_missing_durable_decision_id_fails_closed(
    evidence_repository: SqlAlchemyQuotationScenarioEvidenceRepository,
) -> None:
    orchestration = FakeOrchestration()
    workflow, _reader = _workflow(
        decision=_decision(decision_id=None),
        evidence_repository=evidence_repository,
        orchestration=orchestration,
    )

    result = _capture(workflow)

    assert result.status is WorkbenchQuotationScenarioEvidenceStatus.CAPTURE_FAILED
    assert orchestration.commands == []


def test_accepted_decision_rejects_scenario_ids_for_non_customer_quotation_workflow() -> None:
    with pytest.raises(ExecutionPlanningError):
        AcceptedReviewDecision(
            review_id=REVIEW_ID,
            company_id=COMPANY_ID,
            decision_version=DECISION_VERSION,
            decision_id=DECISION_ID,
            selected_workflow=WorkflowType.RFQ,
            selected_quotation_scenario_ids=("scenario-a",),
        )


def test_accepted_decision_rejects_customer_quotation_without_scenario_ids() -> None:
    with pytest.raises(ExecutionPlanningError):
        AcceptedReviewDecision(
            review_id=REVIEW_ID,
            company_id=COMPANY_ID,
            decision_version=DECISION_VERSION,
            decision_id=DECISION_ID,
            selected_workflow=WorkflowType.CUSTOMER_QUOTATION,
            selected_quotation_scenario_ids=(),
        )


def test_workflow_has_no_odoo_write_or_sale_order_path() -> None:
    tree = ast.parse(Path("app/application/quotation/workbench_workflow.py").read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)

    assert not any(module.startswith(("sqlalchemy", "app.models", "app.erp", "app.connectors")) for module in modules)

    body = list(tree.body)
    if body and isinstance(body[0], ast.Expr):
        body = body[1:]
    for node in ast.walk(ast.Module(body=body, type_ignores=[])):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            node.value.value = ""
    code = ast.unparse(ast.Module(body=body, type_ignores=[]))
    for token in ("sale.order", ".write(", ".create(", ".unlink(", "Writer", "WritePolicy"):
        assert token not in code
