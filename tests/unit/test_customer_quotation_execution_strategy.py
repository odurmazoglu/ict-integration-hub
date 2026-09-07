from __future__ import annotations

import ast
import dataclasses
from decimal import Decimal
from pathlib import Path

import pytest

from app.application.execution import (
    AcceptedReviewDecision,
    ExecutionApproval,
    ExecutionApprovalError,
    ExecutionArtifactType,
    ExecutionMode,
    ExecutionPlanner,
    ExecutionRequest,
    ExecutionStep,
    ExecutionStepRequest,
    ExecutionStepStatus,
    ExecutionStepType,
    ExecutionStrategyResolver,
    ExecutionUnsupportedStepError,
    WorkbenchCustomerQuotationExecutionWorkflow,
)
from app.application.execution.customer_quotation_strategy import CustomerQuotationExecutionStrategy
from app.application.execution.workbench_vendor_bill import WorkbenchVendorBillExecutionStatus
from app.application.quotation import (
    CustomerQuotationCreationResult,
    CustomerQuotationDraft,
    QuotationScenarioLine,
    QuotationScenarioSnapshot,
    customer_quotation_execution_key,
)
from app.application.quotation.exceptions import QuotationEvidenceNotFoundError
from app.application.workbench.dto import ReviewDecisionType
from app.application.workflow import WorkflowType
from app.erp.write.exceptions import CustomerQuotationWriteConfigurationError, CustomerQuotationWritePricelistError

COMPANY_ID = 7
REVIEW_ID = "review-1"
DECISION_ID = "decision-xyz"
DECISION_VERSION = 4


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
                cost_unit_price=Decimal("6.00"),
            ),
        ),
        review_id=REVIEW_ID,
        decision_id=DECISION_ID,
        decision_version=DECISION_VERSION,
    )


def _decision(*, scenario_ids: tuple[str, ...] = ("scn-a", "scn-b")) -> AcceptedReviewDecision:
    return AcceptedReviewDecision(
        review_id=REVIEW_ID,
        company_id=COMPANY_ID,
        decision_version=DECISION_VERSION,
        decision_id=DECISION_ID,
        selected_workflow=WorkflowType.CUSTOMER_QUOTATION,
        selected_quotation_scenario_ids=scenario_ids,
        decision_type=ReviewDecisionType.SELECT_WORKFLOW,
    )


def _request(
    *, scenario_ids: tuple[str, ...] = ("scn-a", "scn-b"), mode: ExecutionMode = ExecutionMode.EXECUTE
) -> ExecutionRequest:
    return ExecutionRequest(
        execution_id="execution-1",
        review_id=REVIEW_ID,
        company_id=COMPANY_ID,
        decision_version=DECISION_VERSION,
        decision_id=DECISION_ID,
        idempotency_key=None,
        mode=mode,
        selected_workflow=WorkflowType.CUSTOMER_QUOTATION,
        selected_quotation_scenario_ids=scenario_ids,
    )


def _step(scenario_id: str, *, sequence: int = 1) -> ExecutionStep:
    return ExecutionStep(
        step_key=f"{REVIEW_ID}:{DECISION_VERSION}:{ExecutionStepType.CREATE_CUSTOMER_QUOTATION.value}:{scenario_id}",
        step_type=ExecutionStepType.CREATE_CUSTOMER_QUOTATION,
        allocation_keys=(),
        sequence=sequence,
        dry_run_supported=True,
        execute_supported=True,
        writer_required=True,
        customer_quotation_scenario_id=scenario_id,
    )


def _step_request(
    step: ExecutionStep,
    *,
    mode: ExecutionMode = ExecutionMode.EXECUTE,
    approved_by: str | None = "controller",
) -> ExecutionStepRequest:
    return ExecutionStepRequest(
        execution_id="execution-1",
        review_id=REVIEW_ID,
        company_id=COMPANY_ID,
        decision_version=DECISION_VERSION,
        mode=mode,
        step=step,
        approval=ExecutionApproval(approved_by=approved_by) if approved_by is not None else None,
        decision_id=DECISION_ID,
    )


class FakeEvidenceReader:
    def __init__(self, snapshots: dict[str, QuotationScenarioSnapshot] | None = None) -> None:
        self._snapshots = snapshots or {}
        self.calls: list[dict[str, object]] = []

    def get(self, *, company_id, review_id, decision_id, decision_version, scenario_id) -> QuotationScenarioSnapshot:
        self.calls.append(
            {
                "company_id": company_id,
                "review_id": review_id,
                "decision_id": decision_id,
                "decision_version": decision_version,
                "scenario_id": scenario_id,
            }
        )
        if scenario_id not in self._snapshots:
            raise QuotationEvidenceNotFoundError("Quotation scenario evidence was not found.")
        return self._snapshots[scenario_id]

    def persist(self, snapshot):  # pragma: no cover - port completeness
        raise AssertionError("execution must not persist evidence")


class FakeQuotationWriter:
    def __init__(self, *, error: Exception | None = None) -> None:
        self._error = error
        self.commands: list[object] = []
        self._by_key: dict[str, int] = {}
        self._next_id = 8000

    async def create_quotation(self, command) -> CustomerQuotationCreationResult:
        self.commands.append(command)
        if self._error is not None:
            raise self._error
        key = command.execution_key
        if key in self._by_key:
            return CustomerQuotationCreationResult(
                external_quotation_id=self._by_key[key],
                execution_key=key,
                created=False,
                external_reference="S00042",
            )
        self._next_id += 1
        self._by_key[key] = self._next_id
        return CustomerQuotationCreationResult(
            external_quotation_id=self._next_id,
            execution_key=key,
            created=True,
            external_reference="S00042",
        )


def _strategy(reader: FakeEvidenceReader, writer: FakeQuotationWriter) -> CustomerQuotationExecutionStrategy:
    return CustomerQuotationExecutionStrategy(quotation_evidence_reader=reader, customer_quotation_writer=writer)


# ----------------------------------------------------------------------------- planner


def test_planner_creates_one_step_per_selected_scenario_in_order() -> None:
    plan = ExecutionPlanner().plan(_request(scenario_ids=("scn-a", "scn-b", "scn-c")))

    assert [step.step_type for step in plan.steps] == [ExecutionStepType.CREATE_CUSTOMER_QUOTATION] * 3
    assert [step.customer_quotation_scenario_id for step in plan.steps] == ["scn-a", "scn-b", "scn-c"]
    assert [step.sequence for step in plan.steps] == [1, 2, 3]
    assert all(step.execute_supported and step.writer_required for step in plan.steps)
    assert all(step.allocation_keys == () for step in plan.steps)


def test_planner_step_identity_is_deterministic_and_free_of_runtime_noise() -> None:
    first = ExecutionPlanner().plan(_request(scenario_ids=("scn-a", "scn-b")))
    second = ExecutionPlanner().plan(
        ExecutionRequest(
            execution_id="a-totally-different-execution-id",
            review_id=REVIEW_ID,
            company_id=COMPANY_ID,
            decision_version=DECISION_VERSION,
            decision_id=DECISION_ID,
            idempotency_key=None,
            mode=ExecutionMode.DRY_RUN,
            selected_workflow=WorkflowType.CUSTOMER_QUOTATION,
            selected_quotation_scenario_ids=("scn-a", "scn-b"),
        )
    )

    assert [step.step_key for step in first.steps] == [step.step_key for step in second.steps]
    assert first.steps[0].step_key == f"{REVIEW_ID}:{DECISION_VERSION}:create_customer_quotation:scn-a"
    for token in ("uuid", "time", "timestamp"):
        assert token not in first.steps[0].step_key.lower()


def test_planner_idempotency_key_distinguishes_scenario_sets() -> None:
    plan_ab = ExecutionPlanner().plan(_request(scenario_ids=("scn-a", "scn-b")))
    plan_ac = ExecutionPlanner().plan(_request(scenario_ids=("scn-a", "scn-c")))

    assert plan_ab.idempotency_key != plan_ac.idempotency_key


def test_planner_does_not_reuse_new_rfq_purchase_for_customer_quotation() -> None:
    plan = ExecutionPlanner().plan(_request(scenario_ids=("scn-a",)))

    assert all(step.step_type is ExecutionStepType.CREATE_CUSTOMER_QUOTATION for step in plan.steps)
    assert all(step.step_type is not ExecutionStepType.NEW_RFQ_PURCHASE for step in plan.steps)


def test_planner_rejects_customer_quotation_request_with_no_scenarios() -> None:
    from app.application.execution.exceptions import ExecutionPlanningError

    with pytest.raises(ExecutionPlanningError):
        ExecutionPlanner().plan(_request(scenario_ids=()))


# ----------------------------------------------------------------------------- strategy


def test_strategy_supports_only_create_customer_quotation() -> None:
    strategy = _strategy(FakeEvidenceReader(), FakeQuotationWriter())
    assert strategy.supported_step_types == (ExecutionStepType.CREATE_CUSTOMER_QUOTATION,)
    assert strategy.supports_mode(ExecutionMode.DRY_RUN)
    assert strategy.supports_mode(ExecutionMode.EXECUTE)


def test_strategy_loads_snapshot_from_evidence_and_invokes_writer_once() -> None:
    reader = FakeEvidenceReader({"scn-a": _snapshot("scn-a")})
    writer = FakeQuotationWriter()

    result = _strategy(reader, writer).execute(_step_request(_step("scn-a")))

    assert reader.calls == [
        {
            "company_id": COMPANY_ID,
            "review_id": REVIEW_ID,
            "decision_id": DECISION_ID,
            "decision_version": DECISION_VERSION,
            "scenario_id": "scn-a",
        }
    ]
    assert len(writer.commands) == 1
    command = writer.commands[0]
    assert command.draft == CustomerQuotationDraft.from_snapshot(_snapshot("scn-a"))
    assert command.approved_by == "controller"
    assert result.status is ExecutionStepStatus.EXECUTED


def test_strategy_created_and_existing_results_both_produce_executed_step() -> None:
    reader = FakeEvidenceReader({"scn-a": _snapshot("scn-a")})
    writer = FakeQuotationWriter()
    strategy = _strategy(reader, writer)

    first = strategy.execute(_step_request(_step("scn-a")))
    second = strategy.execute(_step_request(_step("scn-a")))

    assert first.status is second.status is ExecutionStepStatus.EXECUTED
    assert first.produced_artifacts[0].created is True
    assert second.produced_artifacts[0].created is False
    assert first.produced_artifacts[0].artifact_id == second.produced_artifacts[0].artifact_id
    assert "already exists" in (second.message or "")


def test_strategy_dry_run_never_calls_writer() -> None:
    reader = FakeEvidenceReader({"scn-a": _snapshot("scn-a")})
    writer = FakeQuotationWriter()

    result = _strategy(reader, writer).execute(
        _step_request(_step("scn-a"), mode=ExecutionMode.DRY_RUN, approved_by=None)
    )

    assert result.status is ExecutionStepStatus.DRY_RUN_OK
    assert writer.commands == []


def test_strategy_execute_requires_approval() -> None:
    reader = FakeEvidenceReader({"scn-a": _snapshot("scn-a")})
    with pytest.raises(ExecutionApprovalError):
        _strategy(reader, FakeQuotationWriter()).execute(_step_request(_step("scn-a"), approved_by=None))


def test_strategy_missing_evidence_fails_closed_without_writer_call() -> None:
    reader = FakeEvidenceReader({})
    writer = FakeQuotationWriter()

    result = _strategy(reader, writer).execute(_step_request(_step("scn-a")))

    assert result.status is ExecutionStepStatus.FAILED
    assert result.error_code == "quotation_evidence_not_found"
    assert writer.commands == []


def test_strategy_evidence_identity_mismatch_fails_closed() -> None:
    reader = FakeEvidenceReader({"scn-a": _snapshot("scn-b")})
    writer = FakeQuotationWriter()

    result = _strategy(reader, writer).execute(_step_request(_step("scn-a")))

    assert result.status is ExecutionStepStatus.FAILED
    assert result.error_code == "quotation_evidence_data_integrity_error"
    assert writer.commands == []


@pytest.mark.parametrize(
    ("error", "expected_code"),
    (
        (CustomerQuotationWritePricelistError("no pricelist"), "customer_quotation_pricelist_resolution_failure"),
        (CustomerQuotationWriteConfigurationError("no field"), "customer_quotation_configuration_failure"),
    ),
)
def test_strategy_translates_writer_errors_to_failed_step(error: Exception, expected_code: str) -> None:
    reader = FakeEvidenceReader({"scn-a": _snapshot("scn-a")})
    writer = FakeQuotationWriter(error=error)

    result = _strategy(reader, writer).execute(_step_request(_step("scn-a")))

    assert result.status is ExecutionStepStatus.FAILED
    assert result.error_code == expected_code


def test_strategy_rejects_non_quotation_step_type() -> None:
    strategy = _strategy(FakeEvidenceReader(), FakeQuotationWriter())
    bad_step = ExecutionStep(
        step_key="k",
        step_type=ExecutionStepType.VENDOR_BILL,
        allocation_keys=(),
        sequence=1,
    )
    with pytest.raises(ExecutionUnsupportedStepError):
        strategy.execute(_step_request(bad_step))


# ----------------------------------------------------------------------------- artifact


def test_artifact_represents_sale_order_and_deterministic_execution_key() -> None:
    reader = FakeEvidenceReader({"scn-a": _snapshot("scn-a"), "scn-b": _snapshot("scn-b")})
    writer = FakeQuotationWriter()
    strategy = _strategy(reader, writer)

    result_a = strategy.execute(_step_request(_step("scn-a")))
    result_b = strategy.execute(_step_request(_step("scn-b")))

    artifact_a = result_a.produced_artifacts[0]
    assert artifact_a.artifact_type is ExecutionArtifactType.CUSTOMER_QUOTATION
    assert artifact_a.artifact_id == "8001"
    assert artifact_a.created is True
    assert artifact_a.external_identity == customer_quotation_execution_key(
        company_id=COMPANY_ID,
        review_id=REVIEW_ID,
        decision_id=DECISION_ID,
        decision_version=DECISION_VERSION,
        scenario_id="scn-a",
    )
    assert artifact_a.external_identity != result_b.produced_artifacts[0].external_identity
    assert {field.name for field in dataclasses.fields(artifact_a)} == {
        "artifact_type",
        "artifact_id",
        "external_identity",
        "created",
    }


def test_artifact_carries_no_mutable_scenario_commercial_data() -> None:
    reader = FakeEvidenceReader({"scn-a": _snapshot("scn-a", sales_unit_price="123.45")})
    writer = FakeQuotationWriter()

    artifact = _strategy(reader, writer).execute(_step_request(_step("scn-a"))).produced_artifacts[0]

    blob = repr(artifact)
    for token in ("123.45", "Scenario scn-a", "product_variant", "cost_unit_price"):
        assert token not in blob


# ----------------------------------------------------------------------------- workbench workflow / evidence gate


class FakeAcceptedDecisionReader:
    def __init__(self, decision: AcceptedReviewDecision | Exception) -> None:
        self._decision = decision

    def get_accepted_decision(self, *, review_id, company_id, decision_version) -> AcceptedReviewDecision:
        if isinstance(self._decision, Exception):
            raise self._decision
        return self._decision


class FakeExecutionUseCase:
    def __init__(self) -> None:
        self.calls: list[object] = []

    def execute(self, command):
        self.calls.append(command)
        from app.application.execution import AcceptedDecisionExecutionResult, AcceptedDecisionExecutionStatus

        return AcceptedDecisionExecutionResult(
            review_id=command.review_id,
            company_id=command.company_id,
            decision_version=command.decision_version,
            status=AcceptedDecisionExecutionStatus.EXECUTED,
            execution_id="execution-1",
        )


class FakeRuntimeRepository:
    def get_snapshot(self, *, execution_id):
        return None


def _workflow(reader: FakeEvidenceReader, execution_use_case: FakeExecutionUseCase, decision: AcceptedReviewDecision):
    return WorkbenchCustomerQuotationExecutionWorkflow(
        accepted_decision_reader=FakeAcceptedDecisionReader(decision),
        quotation_evidence_reader=reader,
        execution_use_case=execution_use_case,
        runtime_repository=FakeRuntimeRepository(),
    )


def test_workflow_proceeds_when_all_scenario_evidence_present() -> None:
    reader = FakeEvidenceReader({"scn-a": _snapshot("scn-a"), "scn-b": _snapshot("scn-b")})
    use_case = FakeExecutionUseCase()

    result = _workflow(reader, use_case, _decision()).execute(
        review_id=REVIEW_ID,
        company_id=COMPANY_ID,
        decision_version=DECISION_VERSION,
        mode=ExecutionMode.EXECUTE,
        approval=ExecutionApproval(approved_by="controller"),
    )

    assert result.status is WorkbenchVendorBillExecutionStatus.EXECUTED
    assert len(use_case.calls) == 1


@pytest.mark.parametrize("present", [(), ("scn-a",)])
def test_workflow_fails_closed_when_evidence_missing_or_partial(present: tuple[str, ...]) -> None:
    reader = FakeEvidenceReader({scenario_id: _snapshot(scenario_id) for scenario_id in present})
    use_case = FakeExecutionUseCase()

    result = _workflow(reader, use_case, _decision(scenario_ids=("scn-a", "scn-b"))).execute(
        review_id=REVIEW_ID,
        company_id=COMPANY_ID,
        decision_version=DECISION_VERSION,
        mode=ExecutionMode.EXECUTE,
        approval=ExecutionApproval(approved_by="controller"),
    )

    assert result.status is WorkbenchVendorBillExecutionStatus.MISSING_QUOTATION_EVIDENCE
    assert use_case.calls == []


def test_workflow_non_customer_quotation_decision_is_not_executable_here() -> None:
    reader = FakeEvidenceReader({"scn-a": _snapshot("scn-a")})
    use_case = FakeExecutionUseCase()
    vendor_bill_decision = AcceptedReviewDecision(
        review_id=REVIEW_ID,
        company_id=COMPANY_ID,
        decision_version=DECISION_VERSION,
        decision_id=DECISION_ID,
        selected_workflow=WorkflowType.VENDOR_BILL,
    )

    result = _workflow(reader, use_case, vendor_bill_decision).execute(
        review_id=REVIEW_ID,
        company_id=COMPANY_ID,
        decision_version=DECISION_VERSION,
    )

    assert result.status is WorkbenchVendorBillExecutionStatus.NOT_EXECUTABLE
    assert use_case.calls == []


# ----------------------------------------------------------------------------- composition / architecture


def test_strategy_is_registered_only_for_create_customer_quotation() -> None:
    strategy = _strategy(FakeEvidenceReader(), FakeQuotationWriter())
    resolver = ExecutionStrategyResolver((strategy,))

    assert resolver.resolve(ExecutionStepType.CREATE_CUSTOMER_QUOTATION) is strategy
    with pytest.raises(ExecutionUnsupportedStepError):
        resolver.resolve(ExecutionStepType.VENDOR_BILL)


def test_strategy_module_has_no_odoo_client_or_infra_or_forbidden_coupling() -> None:
    path = "app/application/execution/customer_quotation_strategy.py"
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    assert not any(
        module.startswith(("app.connectors", "app.erp", "sqlalchemy", "app.models", "app.composition"))
        for module in modules
    )

    body = list(tree.body)
    if body and isinstance(body[0], ast.Expr):
        body = body[1:]
    for node in ast.walk(ast.Module(body=body, type_ignores=[])):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            node.value.value = ""
    code = ast.unparse(ast.Module(body=body, type_ignores=[])).lower()
    for token in (
        "new_rfq_purchase",
        "proposal",
        "get_scenario",
        "action_confirm",
        "subscription",
        "plan_id",
        "recurring",
        "cost_unit_price",
        "tax_ids",
        "create_studio_record",
        "ir.model",
        "sourcereader",
    ):
        assert token not in code


class _PermissiveGate:
    def ensure_real_write_allowed(self, *, approved_by: str | None) -> None:
        return None


def test_end_to_end_runtime_executes_one_step_per_scenario_independently() -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.application.execution import (
        ExecutionPreflightPolicy,
        ExecutionRetryPolicy,
        ExecutionRuntimeCoordinator,
        ExecutionRuntimeService,
        RunAcceptedDecisionExecutionCommand,
        RunAcceptedDecisionExecutionUseCase,
        StaticRetryPolicyResolver,
    )
    from app.db.base import Base
    from app.models.workflow_execution import WorkflowExecution, WorkflowExecutionEvent, WorkflowExecutionStep
    from app.persistence.execution_runtime_repository import SqlAlchemyExecutionRuntimeRepository

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            WorkflowExecution.__table__,
            WorkflowExecutionStep.__table__,
            WorkflowExecutionEvent.__table__,
        ],
    )
    session = sessionmaker(bind=engine)()

    reader = FakeEvidenceReader({"scn-a": _snapshot("scn-a"), "scn-b": _snapshot("scn-b")})
    writer = FakeQuotationWriter()
    repository = SqlAlchemyExecutionRuntimeRepository(session)
    use_case = RunAcceptedDecisionExecutionUseCase(
        accepted_decision_reader=FakeAcceptedDecisionReader(_decision(scenario_ids=("scn-a", "scn-b"))),
        execution_planner=ExecutionPlanner(),
        runtime_service=ExecutionRuntimeService(runtime_repository=repository, event_repository=repository),
        runtime_coordinator=ExecutionRuntimeCoordinator(
            runtime_repository=repository,
            event_repository=repository,
            strategy_resolver=ExecutionStrategyResolver((_strategy(reader, writer),)),
        ),
        runtime_repository=repository,
        retry_policy_resolver=StaticRetryPolicyResolver(ExecutionRetryPolicy.immediate(max_attempts=1)),
        execution_preflight=ExecutionPreflightPolicy(
            production_execution_enabled=True,
            real_write_gates={ExecutionStepType.CREATE_CUSTOMER_QUOTATION: _PermissiveGate()},
            writer_step_types=(ExecutionStepType.CREATE_CUSTOMER_QUOTATION,),
        ),
    )

    result = use_case.execute(
        RunAcceptedDecisionExecutionCommand(
            review_id=REVIEW_ID,
            company_id=COMPANY_ID,
            decision_version=DECISION_VERSION,
            mode=ExecutionMode.EXECUTE,
            approval=ExecutionApproval(approved_by="controller"),
        )
    )

    snapshot = repository.get_snapshot(execution_id=result.execution_id)
    step_results = [step.last_result for step in snapshot.steps]
    assert [step.step_key for step in snapshot.plan.steps] == [
        f"{REVIEW_ID}:{DECISION_VERSION}:create_customer_quotation:scn-a",
        f"{REVIEW_ID}:{DECISION_VERSION}:create_customer_quotation:scn-b",
    ]
    assert all(step_result.status is ExecutionStepStatus.EXECUTED for step_result in step_results)
    artifact_ids = {artifact.artifact_id for step_result in step_results for artifact in step_result.produced_artifacts}
    assert artifact_ids == {"8001", "8002"}
    assert len(writer.commands) == 2

    session.close()


def test_composition_wires_phase_3a_writer_and_keeps_safety_gate() -> None:
    source = Path("app/composition/execution.py").read_text(encoding="utf-8")
    for token in (
        "OdooCustomerQuotationWriter",
        "OdooCustomerQuotationRepository",
        "OdooCustomerQuotationPricelistResolver",
        "OdooCustomerQuotationFieldMapping",
        "OdooCustomerQuotationWritePolicy.from_settings",
        "ExecutionPreflightPolicy(",
        "real_write_gates={ExecutionStepType.CREATE_CUSTOMER_QUOTATION",
        "ExecutionStrategyResolver((strategy,))",
    ):
        assert token in source
    assert "new_rfq_purchase" not in source.lower()
