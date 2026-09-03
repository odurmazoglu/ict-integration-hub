from __future__ import annotations

import ast
import inspect
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.application.quotation import (
    CaptureAndPersistAcceptedQuotationScenariosCommand,
    CaptureAndPersistAcceptedQuotationScenariosUseCase,
    PersistQuotationScenarioEvidenceUseCase,
    QuotationEvidenceConflictError,
    QuotationScenarioLine,
    QuotationScenarioOrchestrationError,
    QuotationScenarioSnapshot,
)
from app.application.quotation.capture import CaptureQuotationScenarioCommand
from app.application.workbench import SubmitReviewDecisionUseCase
from app.application.workbench.exceptions import WorkbenchContractError
from app.db.base import Base
from app.models.quotation_scenario_evidence import QuotationScenarioEvidence
from app.persistence import SqlAlchemyQuotationScenarioEvidenceRepository, SqlAlchemyUnitOfWork


def _snapshot(
    scenario_id: str = "scenario-a",
    *,
    company_id: int = 7,
    review_id: str = "review-1",
    decision_id: str = "decision-1",
    decision_version: int = 2,
    sales_unit_price: str = "10.00",
    customer_id: int = 501,
) -> QuotationScenarioSnapshot:
    return QuotationScenarioSnapshot(
        scenario_id=scenario_id,
        scenario_name=f"Scenario {scenario_id}",
        company_id=company_id,
        customer_id=customer_id,
        currency="eur",
        lines=(
            QuotationScenarioLine(
                line_id="line-1",
                product_variant_id=10,
                quantity=Decimal("2"),
                sales_unit_price=Decimal(sales_unit_price),
            ),
        ),
        review_id=review_id,
        decision_id=decision_id,
        decision_version=decision_version,
    )


def _command(
    scenario_ids: tuple[str, ...] = ("scenario-a",),
    *,
    company_id: int = 7,
    review_id: str = "review-1",
    decision_id: str = "decision-1",
    decision_version: int = 2,
) -> CaptureAndPersistAcceptedQuotationScenariosCommand:
    return CaptureAndPersistAcceptedQuotationScenariosCommand(
        company_id=company_id,
        review_id=review_id,
        decision_id=decision_id,
        decision_version=decision_version,
        selected_quotation_scenario_ids=tuple(scenario_ids),
    )


class FakeCapturer:
    def __init__(
        self,
        *,
        snapshots: dict[str, QuotationScenarioSnapshot] | None = None,
        errors: dict[str, Exception] | None = None,
    ) -> None:
        self._snapshots = snapshots or {}
        self._errors = errors or {}
        self.commands: list[CaptureQuotationScenarioCommand] = []

    def execute(self, command: CaptureQuotationScenarioCommand) -> QuotationScenarioSnapshot:
        self.commands.append(command)
        if command.scenario_id in self._errors:
            raise self._errors[command.scenario_id]
        return self._snapshots[command.scenario_id]


class SpyUnitOfWork:
    def __init__(self, inner: SqlAlchemyUnitOfWork) -> None:
        self._inner = inner
        self.commits = 0
        self.rollbacks = 0

    def commit(self) -> None:
        self.commits += 1
        self._inner.commit()

    def rollback(self) -> None:
        self.rollbacks += 1
        self._inner.rollback()


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[QuotationScenarioEvidence.__table__])
    factory = sessionmaker(bind=engine)
    with factory() as db_session:
        yield db_session


@pytest.fixture()
def persist_use_case(session: Session) -> PersistQuotationScenarioEvidenceUseCase:
    return PersistQuotationScenarioEvidenceUseCase(
        repository=SqlAlchemyQuotationScenarioEvidenceRepository(session),
    )


def _use_case(
    *,
    session: Session,
    persist_use_case: PersistQuotationScenarioEvidenceUseCase,
    capturer: FakeCapturer,
) -> tuple[CaptureAndPersistAcceptedQuotationScenariosUseCase, SpyUnitOfWork]:
    spy = SpyUnitOfWork(SqlAlchemyUnitOfWork(session))
    use_case = CaptureAndPersistAcceptedQuotationScenariosUseCase(
        capture_use_case=capturer,
        persist_use_case=persist_use_case,
        unit_of_work=spy,
    )
    return use_case, spy


def test_single_selected_scenario_is_captured_and_persisted(
    session: Session,
    persist_use_case: PersistQuotationScenarioEvidenceUseCase,
) -> None:
    capturer = FakeCapturer(snapshots={"scenario-a": _snapshot("scenario-a")})
    use_case, spy = _use_case(session=session, persist_use_case=persist_use_case, capturer=capturer)

    result = use_case.execute(_command(("scenario-a",)))

    assert result.persisted_scenario_ids == ("scenario-a",)
    assert session.query(QuotationScenarioEvidence).count() == 1
    assert spy.commits == 1
    assert spy.rollbacks == 0


def test_multiple_selected_scenarios_preserve_declared_order(
    session: Session,
    persist_use_case: PersistQuotationScenarioEvidenceUseCase,
) -> None:
    capturer = FakeCapturer(
        snapshots={name: _snapshot(name) for name in ("scenario-a", "scenario-b", "scenario-c")},
    )
    use_case, _spy = _use_case(session=session, persist_use_case=persist_use_case, capturer=capturer)

    result = use_case.execute(_command(("scenario-c", "scenario-a", "scenario-b")))

    assert result.persisted_scenario_ids == ("scenario-c", "scenario-a", "scenario-b")
    assert [command.scenario_id for command in capturer.commands] == ["scenario-c", "scenario-a", "scenario-b"]
    assert session.query(QuotationScenarioEvidence).count() == 3


def test_each_capture_command_carries_accepted_decision_identity(
    session: Session,
    persist_use_case: PersistQuotationScenarioEvidenceUseCase,
) -> None:
    capturer = FakeCapturer(snapshots={name: _snapshot(name) for name in ("scenario-a", "scenario-b")})
    use_case, _spy = _use_case(session=session, persist_use_case=persist_use_case, capturer=capturer)

    use_case.execute(_command(("scenario-a", "scenario-b")))

    for command in capturer.commands:
        assert command.company_id == 7
        assert command.review_id == "review-1"
        assert command.decision_id == "decision-1"
        assert command.decision_version == 2


def test_duplicate_selected_scenario_ids_rejected() -> None:
    with pytest.raises(QuotationScenarioOrchestrationError):
        _command(("scenario-a", "scenario-a"))


def test_empty_selected_scenario_ids_rejected() -> None:
    with pytest.raises(QuotationScenarioOrchestrationError):
        _command(())


def test_capture_failure_produces_no_evidence_and_no_commit(
    session: Session,
    persist_use_case: PersistQuotationScenarioEvidenceUseCase,
) -> None:
    capturer = FakeCapturer(errors={"scenario-a": WorkbenchContractError("odoo capture failed")})
    use_case, spy = _use_case(session=session, persist_use_case=persist_use_case, capturer=capturer)

    with pytest.raises(WorkbenchContractError):
        use_case.execute(_command(("scenario-a",)))

    assert session.query(QuotationScenarioEvidence).count() == 0
    assert spy.commits == 0


def test_second_scenario_capture_failure_prevents_partial_write(
    session: Session,
    persist_use_case: PersistQuotationScenarioEvidenceUseCase,
) -> None:
    capturer = FakeCapturer(
        snapshots={"scenario-a": _snapshot("scenario-a")},
        errors={"scenario-b": WorkbenchContractError("odoo capture failed")},
    )
    use_case, spy = _use_case(session=session, persist_use_case=persist_use_case, capturer=capturer)

    with pytest.raises(WorkbenchContractError):
        use_case.execute(_command(("scenario-a", "scenario-b")))

    assert session.query(QuotationScenarioEvidence).count() == 0
    assert spy.commits == 0


def test_persistence_conflict_fails_closed(
    session: Session,
    persist_use_case: PersistQuotationScenarioEvidenceUseCase,
) -> None:
    persist_use_case.execute(_snapshot("scenario-a", sales_unit_price="10.00"))
    session.commit()

    capturer = FakeCapturer(snapshots={"scenario-a": _snapshot("scenario-a", sales_unit_price="99.00")})
    use_case, spy = _use_case(session=session, persist_use_case=persist_use_case, capturer=capturer)

    with pytest.raises(QuotationEvidenceConflictError):
        use_case.execute(_command(("scenario-a",)))

    assert session.query(QuotationScenarioEvidence).count() == 1
    record = session.scalar(select(QuotationScenarioEvidence))
    assert record is not None
    assert record.scenario_snapshot["lines"][0]["sales_unit_price"] == "10.00"
    assert spy.commits == 0
    assert spy.rollbacks == 1


def test_exact_replay_is_idempotent(
    session: Session,
    persist_use_case: PersistQuotationScenarioEvidenceUseCase,
) -> None:
    capturer = FakeCapturer(snapshots={name: _snapshot(name) for name in ("scenario-a", "scenario-b")})
    use_case, spy = _use_case(session=session, persist_use_case=persist_use_case, capturer=capturer)

    first = use_case.execute(_command(("scenario-a", "scenario-b")))
    second = use_case.execute(_command(("scenario-a", "scenario-b")))

    assert second == first
    assert session.query(QuotationScenarioEvidence).count() == 2
    assert spy.commits == 2


def test_different_decision_version_persists_independently(
    session: Session,
    persist_use_case: PersistQuotationScenarioEvidenceUseCase,
) -> None:
    capturer_v2 = FakeCapturer(snapshots={"scenario-a": _snapshot("scenario-a", decision_version=2)})
    use_case_v2, _spy_v2 = _use_case(session=session, persist_use_case=persist_use_case, capturer=capturer_v2)
    use_case_v2.execute(_command(("scenario-a",), decision_version=2))

    capturer_v3 = FakeCapturer(snapshots={"scenario-a": _snapshot("scenario-a", decision_version=3)})
    use_case_v3, _spy_v3 = _use_case(session=session, persist_use_case=persist_use_case, capturer=capturer_v3)
    use_case_v3.execute(_command(("scenario-a",), decision_version=3))

    assert session.query(QuotationScenarioEvidence).count() == 2


def test_captured_identity_mismatch_fails_closed(
    session: Session,
    persist_use_case: PersistQuotationScenarioEvidenceUseCase,
) -> None:
    capturer = FakeCapturer(snapshots={"scenario-a": _snapshot("scenario-b")})
    use_case, spy = _use_case(session=session, persist_use_case=persist_use_case, capturer=capturer)

    with pytest.raises(QuotationScenarioOrchestrationError):
        use_case.execute(_command(("scenario-a",)))

    assert session.query(QuotationScenarioEvidence).count() == 0
    assert spy.commits == 0


def test_submit_review_decision_use_case_does_not_depend_on_quotation_capture() -> None:
    parameters = set(inspect.signature(SubmitReviewDecisionUseCase.__init__).parameters)

    assert not any("quotation" in name for name in parameters)
    assert not any("capture" in name for name in parameters)
    assert not any("persist" in name for name in parameters)


def _imported_modules(path: str) -> set[str]:
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    return modules


def _code_without_docstrings(path: str) -> str:
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    docstring = ast.get_docstring(tree, clean=False)
    body = list(tree.body)
    if docstring is not None and body and isinstance(body[0], ast.Expr):
        body = body[1:]
    for node in ast.walk(ast.Module(body=body, type_ignores=[])):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            node.value.value = ""
    return ast.unparse(ast.Module(body=body, type_ignores=[]))


def test_application_orchestration_has_no_infra_or_odoo_dependency_leak() -> None:
    modules = _imported_modules("app/application/quotation/orchestration.py")

    assert not any(
        module.startswith(("sqlalchemy", "app.models", "app.erp", "app.connectors", "app.composition"))
        for module in modules
    )
    code = _code_without_docstrings("app/application/quotation/orchestration.py").lower()
    for token in ("sqlalchemy", "session", "sale.order", "search_read"):
        assert token not in code


def test_no_odoo_write_path_reachable_from_orchestration_or_composition() -> None:
    orchestration_code = _code_without_docstrings("app/application/quotation/orchestration.py")
    composition_code = _code_without_docstrings("app/composition/quotation.py")

    for token in (".write(", ".create(", ".unlink(", "AccountMoveRepository", "VendorBillWriter", "InvoiceWriter"):
        assert token not in orchestration_code
        assert token not in composition_code
    composition_modules = _imported_modules("app/composition/quotation.py")
    assert not any("write" in module for module in composition_modules)
    assert "app.erp.odoo.adapter" in composition_modules
