from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from app.api import dependencies
from app.api.routers.workbench import router
from app.api.security import AuthenticationMethod, Permission, RequestContext
from app.application.execution import (
    AcceptedDecisionExecutionStatus,
    ExecutionApproval,
    ExecutionArtifactType,
    ExecutionEventType,
    ExecutionMode,
    ExecutionModeNotEnabledError,
    ExecutionPersistenceError,
    ExecutionRuntimeStepState,
    ExecutionSourceInvoice,
    ExecutionState,
    ExecutionUnsupportedStepError,
    RunAcceptedDecisionExecutionCommand,
)
from app.application.workbench import (
    AllocationCompleteness,
    BusinessContextAllocation,
    BusinessContextAllocationSet,
    BusinessContextAllocationType,
    ReviewDecisionCommand,
    ReviewDecisionType,
    ReviewItem,
    ReviewStatus,
)
from app.application.workflow import ManualReviewReason, ManualReviewReasonCode, WorkflowType
from app.composition import build_vendor_bill_execution_use_case
from app.connectors.exceptions import ConnectorTimeoutError
from app.connectors.odoo.client import OdooJson2Client
from app.core.config import Settings
from app.core.runtime_checks import PRODUCTION_APPROVAL_ACK
from app.db.base import Base
from app.domain.invoice import Header, InternalInvoice, InvoiceLine, MonetaryTotals, Party, Tax
from app.erp.write import VendorBillWriteSafetyGateError
from app.matching import (
    InvoiceProductLineResult,
    InvoiceProductMatchResult,
    PartnerMatchResult,
    PartnerMatchStatus,
    ProductMatchResult,
    ProductMatchStatus,
)
from app.models.workflow_execution import WorkflowExecution, WorkflowExecutionEvent, WorkflowExecutionStep
from app.persistence import SqlAlchemyExecutionRuntimeRepository, SqlAlchemyReviewRepository, SqlAlchemyUnitOfWork
from app.tax_mapping import InvoiceTaxLineResult, InvoiceTaxMappingResult, TaxMatchResult, TaxMatchStatus, TaxType


@pytest.fixture()
def session() -> Session:
    factory = sessionmaker(bind=create_engine("sqlite:///:memory:"))
    with factory() as db_session:
        Base.metadata.create_all(db_session.get_bind())
        yield db_session


def test_dry_run_uses_full_composition_without_odoo_write(session: Session) -> None:
    _submit_vendor_bill_decision(session)
    client = FakeOdooVendorBillClient()

    result = _use_case(session, client=client, settings=_settings()).execute(_command(mode=ExecutionMode.DRY_RUN))

    assert result.status is AcceptedDecisionExecutionStatus.DRY_RUN_COMPLETED
    assert result.runtime_state is ExecutionState.COMPLETED
    assert client.search_calls == []
    assert client.create_calls == []


def test_execute_disabled_fails_before_runtime_or_odoo_call(session: Session) -> None:
    _submit_vendor_bill_decision(session)
    client = FakeOdooVendorBillClient()

    with pytest.raises(ExecutionModeNotEnabledError):
        _use_case(session, client=client, settings=_settings(execution_execute_enabled=False)).execute(
            _command(mode=ExecutionMode.EXECUTE)
        )

    assert _runtime_count(session) == 0
    assert client.search_calls == []
    assert client.create_calls == []


@pytest.mark.parametrize(
    ("production_operations_enabled", "production_approval_ack"),
    [
        (False, PRODUCTION_APPROVAL_ACK),
        (True, ""),
    ],
)
def test_writer_safety_gate_failures_happen_before_runtime_or_odoo_call(
    session: Session,
    production_operations_enabled: bool,
    production_approval_ack: str,
) -> None:
    _submit_vendor_bill_decision(session)
    client = FakeOdooVendorBillClient()
    settings = _settings(
        execution_execute_enabled=True,
        production_operations_enabled=production_operations_enabled,
        production_approval_ack=production_approval_ack,
    )

    with pytest.raises(VendorBillWriteSafetyGateError):
        _use_case(session, client=client, settings=settings).execute(_command(mode=ExecutionMode.EXECUTE))

    assert _runtime_count(session) == 0
    assert client.search_calls == []
    assert client.create_calls == []


def test_execute_enabled_creates_one_draft_vendor_bill_and_persists_artifact(session: Session) -> None:
    _submit_vendor_bill_decision(session)
    client = FakeOdooVendorBillClient(created_id=9001)
    repository = SqlAlchemyExecutionRuntimeRepository(session)

    result = _use_case(session, client=client, settings=_execute_settings()).execute(
        _command(mode=ExecutionMode.EXECUTE)
    )

    assert result.status is AcceptedDecisionExecutionStatus.EXECUTED
    assert result.runtime_state is ExecutionState.COMPLETED
    assert len(client.search_calls) == 1
    assert len(client.create_calls) == 1
    payload = client.create_calls[0]
    assert payload["move_type"] == "in_invoice"
    assert "action_post" not in str(payload).lower()
    assert "payment" not in str(payload).lower()
    snapshot = repository.get_snapshot(execution_id=result.execution_id or "")
    assert snapshot is not None
    assert snapshot.state is ExecutionState.COMPLETED
    assert snapshot.steps[0].state is ExecutionRuntimeStepState.COMPLETED
    artifact = snapshot.steps[0].last_result.produced_artifacts[0]  # type: ignore[union-attr]
    assert artifact.artifact_type is ExecutionArtifactType.VENDOR_BILL
    assert artifact.artifact_id == "9001"
    assert artifact.created is True
    history = repository.history(execution_id=result.execution_id or "")
    assert history.events[-1].event_type is ExecutionEventType.EXECUTION_COMPLETED


def test_odoo_create_success_then_hub_finalization_failure_does_not_retry_create(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _submit_vendor_bill_decision(session)
    client = FakeOdooVendorBillClient(created_id=9001)
    original_persist_transition = SqlAlchemyExecutionRuntimeRepository.persist_transition

    def fail_after_writer_returns(
        self: SqlAlchemyExecutionRuntimeRepository,
        **kwargs: Any,
    ):
        events = kwargs["events"]
        if any(event.event_type is ExecutionEventType.STEP_COMPLETED for event in events):
            raise ExecutionPersistenceError("Execution runtime persistence failed.")
        return original_persist_transition(self, **kwargs)

    monkeypatch.setattr(SqlAlchemyExecutionRuntimeRepository, "persist_transition", fail_after_writer_returns)

    with pytest.raises(ExecutionPersistenceError):
        _use_case(session, client=client, settings=_execute_settings()).execute(_command(mode=ExecutionMode.EXECUTE))

    assert len(client.search_calls) == 1
    assert len(client.create_calls) == 1


def test_transport_timeout_retry_recovers_existing_vendor_bill_without_duplicate(session: Session) -> None:
    _submit_vendor_bill_decision(session)
    client = FakeOdooVendorBillClient(timeout_after_create=True, created_id=9001)
    use_case = _use_case(session, client=client, settings=_execute_settings())

    first = use_case.execute(_command(mode=ExecutionMode.EXECUTE))
    second = use_case.execute(_command(mode=ExecutionMode.EXECUTE))

    assert first.runtime_state is ExecutionState.WAITING_RETRY
    assert second.status is AcceptedDecisionExecutionStatus.EXECUTED
    assert second.runtime_state is ExecutionState.COMPLETED
    assert len(client.create_calls) == 1
    assert len(client.search_calls) == 2
    snapshot = SqlAlchemyExecutionRuntimeRepository(session).get_snapshot(execution_id=second.execution_id or "")
    assert snapshot is not None
    artifact = snapshot.steps[0].last_result.produced_artifacts[0]  # type: ignore[union-attr]
    assert artifact.artifact_id == "9001"
    assert artifact.created is False


def test_completed_runtime_never_replays_vendor_bill_write(session: Session) -> None:
    _submit_vendor_bill_decision(session)
    client = FakeOdooVendorBillClient(created_id=9001)
    use_case = _use_case(session, client=client, settings=_execute_settings())

    first = use_case.execute(_command(mode=ExecutionMode.EXECUTE))
    second = use_case.execute(_command(mode=ExecutionMode.EXECUTE))

    assert second.execution_id == first.execution_id
    assert len(client.search_calls) == 1
    assert len(client.create_calls) == 1
    assert _runtime_count(session) == 1


def test_customer_invoice_creation_without_billing_instruction_fails_before_runtime_or_odoo_call(
    session: Session,
) -> None:
    _submit_customer_invoice_decision(session)
    client = FakeOdooVendorBillClient()

    with pytest.raises(ExecutionUnsupportedStepError):
        _use_case(session, client=client, settings=_execute_settings(customer_invoice_execute_enabled=True)).execute(
            _command(mode=ExecutionMode.EXECUTE)
        )

    assert _runtime_count(session) == 0
    assert client.search_calls == []
    assert client.create_calls == []


def test_staging_gate_creates_one_draft_vendor_bill_without_production_flags(session: Session) -> None:
    _submit_vendor_bill_decision(session)
    client = FakeOdooVendorBillClient(created_id=9100)
    repository = SqlAlchemyExecutionRuntimeRepository(session)

    result = _use_case(session, client=client, settings=_staging_settings()).execute(
        _command(mode=ExecutionMode.EXECUTE)
    )

    assert result.status is AcceptedDecisionExecutionStatus.EXECUTED
    assert result.runtime_state is ExecutionState.COMPLETED
    assert len(client.create_calls) == 1
    payload = client.create_calls[0]
    assert payload["move_type"] == "in_invoice"
    assert "action_post" not in str(payload).lower()
    assert "payment" not in str(payload).lower()
    snapshot = repository.get_snapshot(execution_id=result.execution_id or "")
    assert snapshot is not None
    artifact = snapshot.steps[0].last_result.produced_artifacts[0]  # type: ignore[union-attr]
    assert artifact.artifact_type is ExecutionArtifactType.VENDOR_BILL
    assert artifact.artifact_id == "9100"


def test_staging_gate_on_unapproved_host_fails_closed_before_runtime_or_odoo_call(session: Session) -> None:
    _submit_vendor_bill_decision(session)
    client = FakeOdooVendorBillClient()

    with pytest.raises(VendorBillWriteSafetyGateError):
        _use_case(
            session,
            client=client,
            settings=_staging_settings(odoo_base_url="https://not-approved.odoo.com"),
        ).execute(_command(mode=ExecutionMode.EXECUTE))

    assert _runtime_count(session) == 0
    assert client.search_calls == []
    assert client.create_calls == []


def test_staging_gate_retry_recovers_existing_vendor_bill_without_duplicate(session: Session) -> None:
    _submit_vendor_bill_decision(session)
    client = FakeOdooVendorBillClient(timeout_after_create=True, created_id=9100)
    use_case = _use_case(session, client=client, settings=_staging_settings())

    first = use_case.execute(_command(mode=ExecutionMode.EXECUTE))
    second = use_case.execute(_command(mode=ExecutionMode.EXECUTE))

    assert first.runtime_state is ExecutionState.WAITING_RETRY
    assert second.status is AcceptedDecisionExecutionStatus.EXECUTED
    assert len(client.create_calls) == 1
    assert len(client.search_calls) == 2


def test_staging_gate_blocks_customer_recharge_creation_step_before_runtime_or_odoo_call(session: Session) -> None:
    _submit_customer_invoice_decision(session)
    client = FakeOdooVendorBillClient()

    with pytest.raises(ExecutionModeNotEnabledError):
        _use_case(session, client=client, settings=_staging_settings()).execute(_command(mode=ExecutionMode.EXECUTE))

    assert _runtime_count(session) == 0
    assert client.search_calls == []
    assert client.create_calls == []


def test_production_composition_keeps_infrastructure_out_of_application_layer() -> None:
    application_source = "\n".join(path.read_text(encoding="utf-8") for path in Path("app/application").rglob("*.py"))
    composition_source = Path("app/composition/execution.py").read_text(encoding="utf-8")

    assert "OdooVendorBillWriter" not in application_source
    assert "OdooCustomerInvoiceWriter" not in application_source
    assert "AccountMoveRepository" not in application_source
    assert "sqlalchemy" not in application_source.lower()
    assert "OdooVendorBillWriter" in composition_source
    assert "OdooCustomerInvoiceWriter" in composition_source
    assert "AccountMoveRepository" in composition_source
    assert "ExecutionRetryPolicy.immediate(max_attempts=2)" in composition_source


class FakeOdooVendorBillClient:
    def __init__(
        self,
        *,
        created_id: int = 9001,
        timeout_after_create: bool = False,
        timeout_move_type: str | None = None,
    ) -> None:
        self.created_id = created_id
        self.timeout_after_create = timeout_after_create
        self.timeout_move_type = timeout_move_type
        self.created_move_types: set[str] = set()
        self.search_calls: list[list[Any]] = []
        self.create_calls: list[dict[str, Any]] = []

    async def search_read(
        self,
        *,
        model: str,
        domain: list[Any],
        fields: list[str],
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        if model == "res.currency":
            return [{"id": 31, "name": "TRY", "active": True}]
        self.search_calls.append(domain)
        move_type = _domain_value(domain, "move_type") or "in_invoice"
        if move_type not in self.created_move_types:
            return []
        partner_id = _domain_value(domain, "partner_id")
        return [{"id": self.created_id, "name": "MOVE/2026/001", "move_type": move_type, "partner_id": partner_id}]

    async def create_account_move(self, payload: dict[str, Any]) -> int:
        self.create_calls.append(payload)
        move_type = str(payload.get("move_type"))
        self.created_move_types.add(move_type)
        if self.timeout_after_create and (
            self.timeout_move_type is None and len(self.create_calls) == 1 or self.timeout_move_type == move_type
        ):
            self.timeout_after_create = False
            raise ConnectorTimeoutError("Odoo request timed out.")
        return self.created_id


def _use_case(session: Session, *, client: FakeOdooVendorBillClient, settings: Settings):
    return build_vendor_bill_execution_use_case(session=session, settings=settings, odoo_client=client)  # type: ignore[arg-type]


def _command(*, mode: ExecutionMode) -> RunAcceptedDecisionExecutionCommand:
    return RunAcceptedDecisionExecutionCommand(
        review_id="review-1",
        company_id=7,
        decision_version=2,
        mode=mode,
        approval=ExecutionApproval(approved_by="finance.lead") if mode is ExecutionMode.EXECUTE else None,
    )


def _submit_vendor_bill_decision(session: Session) -> None:
    repository = SqlAlchemyReviewRepository(session)
    repository.create_review_item(_review_item(), company_id=7, idempotency_key="review:item")
    repository.submit_review_decision_with_execution_evidence(
        ReviewDecisionCommand(
            review_id="review-1",
            company_id=7,
            expected_version=1,
            decision=ReviewDecisionType.SELECT_WORKFLOW,
            selected_workflow=WorkflowType.VENDOR_BILL,
            decided_by="finance.user",
            idempotency_key="review:decision",
        ),
        _source(),
    )


def _submit_customer_invoice_decision(session: Session) -> None:
    repository = SqlAlchemyReviewRepository(session)
    repository.create_review_item(_review_item(), company_id=7, idempotency_key="review:item")
    repository.submit_review_decision_with_execution_evidence(
        ReviewDecisionCommand(
            review_id="review-1",
            company_id=7,
            expected_version=1,
            decision=ReviewDecisionType.SELECT_WORKFLOW,
            selected_workflow=WorkflowType.VENDOR_BILL,
            business_context_allocations=BusinessContextAllocationSet(
                allocations=(
                    BusinessContextAllocation(
                        allocation_key="A",
                        allocation_type=BusinessContextAllocationType.CUSTOMER_RECHARGE,
                        source_line_number="1",
                        amount=Decimal("120.00"),
                        currency="TRY",
                        recharge_partner_id=701,
                    ),
                ),
                completeness=AllocationCompleteness.PARTIAL,
                invoice_total=Decimal("120.00"),
                currency="TRY",
            ),
            decided_by="finance.user",
            idempotency_key="review:decision",
        ),
        _source(),
    )


def _review_item() -> ReviewItem:
    return ReviewItem(
        review_id="review-1",
        invoice_id="ETTN-1",
        invoice_number="INV-1",
        supplier_tax_number="1234567890",
        supplier_name="Supplier",
        invoice_date=date(2026, 8, 1),
        currency="TRY",
        total_amount=Decimal("120.00"),
        workflow=WorkflowType.MANUAL_REVIEW,
        status=ReviewStatus.PENDING_REVIEW,
        review_reasons=(
            ManualReviewReason(
                code=ManualReviewReasonCode.PRODUCT_NOT_FOUND,
                message="Product was not matched deterministically.",
                line_number="1",
                source="product_matching",
            ),
        ),
    )


def _source() -> ExecutionSourceInvoice:
    invoice = _invoice()
    return ExecutionSourceInvoice(
        review_id="review-1",
        company_id=7,
        decision_version=2,
        source_invoice_id="ETTN-1",
        invoice=invoice,
        partner_match=PartnerMatchResult(
            status=PartnerMatchStatus.MATCHED,
            partner_id=1001,
            matched_by="tax_number",
            reason="Unique supplier partner match by tax number.",
            candidate_count=1,
            confidence=Decimal("1.00"),
        ),
        product_match=InvoiceProductMatchResult(
            line_results=(
                InvoiceProductLineResult(
                    line_number="1",
                    result=ProductMatchResult(
                        status=ProductMatchStatus.MATCHED,
                        line_number="1",
                        product_id=2001,
                        default_code="SKU-1",
                        barcode=None,
                        seller_item_code=None,
                        matched_by="default_code",
                        reason="matched",
                        candidate_count=1,
                        confidence=Decimal("1.00"),
                    ),
                ),
            )
        ),
        tax_match=InvoiceTaxMappingResult(
            line_results=(
                InvoiceTaxLineResult(
                    line_number="1",
                    tax_index=0,
                    result=TaxMatchResult(
                        status=TaxMatchStatus.MATCHED,
                        tax_id=3001,
                        company_id=7,
                        tax_type=TaxType.VAT,
                        tax_rate=Decimal("20"),
                        matched_by="rate",
                        confidence=Decimal("1.00"),
                        reason="matched",
                        candidate_count=1,
                    ),
                ),
            )
        ),
    )


def _invoice() -> InternalInvoice:
    return InternalInvoice(
        header=Header(
            invoice_number="INV-1",
            invoice_uuid="ETTN-1",
            ettn="ETTN-1",
            issue_date=date(2026, 8, 1),
            currency_code="TRY",
        ),
        supplier=Party(name="Supplier", tax_number="1234567890"),
        customer=Party(name="ICT", tax_number="9876543210"),
        totals=MonetaryTotals(payable_amount=Decimal("120.00")),
        lines=(
            InvoiceLine(
                line_number="1",
                description="Service",
                buyer_item_code="SKU-1",
                quantity=Decimal("1"),
                unit_price=Decimal("100.00"),
                taxes=(Tax(tax_type="VAT", rate=Decimal("20")),),
            ),
        ),
    )


def _settings(
    *,
    execution_execute_enabled: bool = False,
    production_operations_enabled: bool = False,
    production_approval_ack: str = "",
    customer_invoice_execute_enabled: bool = False,
) -> Settings:
    return Settings(
        execution_execute_enabled=execution_execute_enabled,
        production_operations_enabled=production_operations_enabled,
        production_approval_ack=production_approval_ack,
        customer_invoice_execute_enabled=customer_invoice_execute_enabled,
    )


def _execute_settings(*, customer_invoice_execute_enabled: bool = False) -> Settings:
    return _settings(
        execution_execute_enabled=True,
        production_operations_enabled=True,
        production_approval_ack=PRODUCTION_APPROVAL_ACK,
        customer_invoice_execute_enabled=customer_invoice_execute_enabled,
    )


def _staging_settings(*, odoo_base_url: str = "https://test-ictteknoloji.odoo.com") -> Settings:
    return Settings(
        app_env="development",
        odoo_base_url=odoo_base_url,
        staging_vendor_bill_execute_enabled=True,
        execution_execute_enabled=True,
        production_operations_enabled=False,
        production_approval_ack="",
    )


def _runtime_count(session: Session) -> int:
    from app.models.workflow_execution import WorkflowExecution

    return session.query(WorkflowExecution).count()


def _domain_value(domain: list[Any], field: str) -> Any:
    for item in domain:
        if isinstance(item, list) and len(item) >= 3 and item[0] == field:
            return item[2]
    return None


@pytest.fixture()
def transaction_engine(tmp_path: Path):
    database_url = os.environ.get("TEST_EXECUTION_TRANSACTION_DATABASE_URL")
    if database_url:
        url = make_url(database_url)
        if url.database != "ict_execution_transaction_test" or url.host not in {"localhost", "127.0.0.1", "db"}:
            raise ValueError("Transaction tests require the isolated local test database.")
        # Match the existing Alembic PostgreSQL identifier-length configuration.
        engine = create_engine(url, max_identifier_length=128)
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            _submit_vendor_bill_decision(session)
            session.commit()
        yield engine
        Base.metadata.drop_all(engine)
        engine.dispose()
        return
    engine = create_engine(f"sqlite:///{tmp_path / 'execution.db'}", connect_args={"check_same_thread": False})

    # SQLite's legacy transaction mode otherwise lets a first SAVEPOINT commit
    # independently. Explicit BEGIN makes rollback tests exercise the real boundary.
    @event.listens_for(engine, "connect")
    def disable_legacy_transactions(connection, _record):
        connection.isolation_level = None

    @event.listens_for(engine, "begin")
    def begin_transaction(connection):
        connection.exec_driver_sql("BEGIN")

    Base.metadata.create_all(engine)
    with Session(engine) as session:
        _submit_vendor_bill_decision(session)
        session.commit()
    yield engine
    engine.dispose()


class LookupFailureClient(FakeOdooVendorBillClient):
    async def search_read(self, **kwargs):
        self.search_calls.append(kwargs["domain"])
        raise ConnectorTimeoutError("Odoo request timed out.")


def _normal_execution_api(monkeypatch, engine: Engine, client, *, settings=None):
    # Preserve the actual get_db_session, lazy dispatcher and composition path.
    monkeypatch.setattr(dependencies, "SessionLocal", sessionmaker(bind=engine))
    monkeypatch.setattr(OdooJson2Client, "from_settings", classmethod(lambda cls, settings: client))
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[dependencies.get_settings] = lambda: settings or _execute_settings()
    app.dependency_overrides[dependencies.get_request_context] = lambda: RequestContext(
        user_id="finance",
        user_name="Finance",
        company_id=7,
        permissions=(Permission.WORKBENCH_EXECUTE,),
        trace_id="transaction-test",
        authentication_method=AuthenticationMethod.JWT,
    )
    return TestClient(app)


def _api_execute(client):
    return client.post(
        "/api/workbench/reviews/review-1/execute",
        json={"decision_version": 2, "mode": "execute", "approval": {"approved_by": "controller"}},
    )


@pytest.mark.parametrize("failed", [False, True])
def test_normal_api_commits_success_artifact_or_waiting_retry_diagnostics(
    transaction_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    failed: bool,
) -> None:
    odoo = LookupFailureClient() if failed else FakeOdooVendorBillClient()
    with _normal_execution_api(monkeypatch, transaction_engine, odoo) as api:
        response = _api_execute(api)
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == ("execution_failed" if failed else "executed")
    with Session(transaction_engine) as fresh:
        repository = SqlAlchemyExecutionRuntimeRepository(fresh)
        snapshot = repository.get_snapshot(execution_id=data["execution_id"])
        assert snapshot is not None
        assert snapshot.state is (ExecutionState.WAITING_RETRY if failed else ExecutionState.COMPLETED)
        step = snapshot.steps[0]
        assert step.last_result is not None
        history = repository.history(execution_id=snapshot.execution_id)
        assert history.events
        if failed:
            assert step.retry_count == 1
            assert step.last_result.message == "Odoo request timed out."
            assert step.last_result.error_code
            assert step.last_result.produced_artifacts == ()
            assert data["artifacts"] == []
            assert odoo.create_calls == []
        else:
            assert step.last_result.produced_artifacts[0].artifact_id == "9001"
            assert data["artifacts"][0]["artifact_id"] == "9001"
            assert len(odoo.create_calls) == 1
    if not failed:
        with _normal_execution_api(monkeypatch, transaction_engine, odoo) as api:
            replay = _api_execute(api)
        assert replay.json()["data"]["status"] == "already_executed"
        assert len(odoo.create_calls) == 1
        assert len(odoo.search_calls) == 1


@pytest.mark.parametrize("failure_point", ["commit", "finalization"])
def test_normal_api_rolls_back_entire_hub_outcome_on_unexpected_failure(
    transaction_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    odoo = FakeOdooVendorBillClient()
    rollbacks = []
    original_rollback = SqlAlchemyUnitOfWork.rollback

    def record_rollback(self):
        rollbacks.append(True)
        original_rollback(self)

    monkeypatch.setattr(SqlAlchemyUnitOfWork, "rollback", record_rollback)
    if failure_point == "commit":

        def fail_commit(self):
            raise RuntimeError("Injected commit failure")

        monkeypatch.setattr(SqlAlchemyUnitOfWork, "commit", fail_commit)
    else:
        original_transition = SqlAlchemyExecutionRuntimeRepository.persist_transition

        def fail_finalization(self, **kwargs):
            if any(e.event_type is ExecutionEventType.STEP_COMPLETED for e in kwargs["events"]):
                raise ExecutionPersistenceError("Injected finalization failure")
            return original_transition(self, **kwargs)

        monkeypatch.setattr(SqlAlchemyExecutionRuntimeRepository, "persist_transition", fail_finalization)
    with _normal_execution_api(monkeypatch, transaction_engine, odoo) as api:
        response = _api_execute(api)
    assert rollbacks == [True]
    if failure_point == "commit":
        assert response.status_code == 500
    else:
        assert response.status_code == 200
        assert response.json()["data"]["status"] == "execution_failed"
        assert response.json()["data"]["artifacts"] == []
    assert len(odoo.create_calls) == 1  # remote success cannot be undone by Hub rollback
    with Session(transaction_engine) as fresh:
        for model in (WorkflowExecution, WorkflowExecutionStep, WorkflowExecutionEvent):
            assert fresh.scalars(select(model)).all() == []
        assert (
            SqlAlchemyReviewRepository(fresh)
            .get_accepted_decision(
                review_id="review-1",
                company_id=7,
                decision_version=2,
            )
            .decision_version
            == 2
        )


def test_runtime_is_committed_before_retirement_and_projection(
    transaction_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.application.workbench.exceptions import WorkbenchProjectionPublishError
    from app.application.workbench.one_off_vendor_use_cases import OneOffVendorRetirementTrigger
    from app.erp.odoo.workbench_projection_publisher import (
        OdooWorkbenchProjectionFieldMapping,
        OdooWorkbenchProjectionPublisher,
    )

    mapping = OdooWorkbenchProjectionFieldMapping(
        model="x_test",
        name="name",
        review_id="x_review",
        company_id="x_company",
        invoice_number="x_invoice",
        supplier="x_supplier",
        supplier_tax_number="x_vat",
        invoice_date="x_date",
        currency="x_currency",
        invoice_total="x_total",
        review_status="x_status",
        workflow="x_workflow",
        review_version="x_version",
        last_sync_at="x_sync",
    )
    monkeypatch.setattr(OdooWorkbenchProjectionFieldMapping, "from_environment", classmethod(lambda cls: mapping))
    observed = []

    def assert_durable(label):
        with Session(transaction_engine) as independent:
            record = independent.scalars(select(WorkflowExecution)).one()
            snapshot = SqlAlchemyExecutionRuntimeRepository(independent).get_snapshot(execution_id=record.execution_id)
            assert snapshot is not None and snapshot.state is ExecutionState.COMPLETED
            assert snapshot.steps[0].last_result.produced_artifacts[0].artifact_id == "9001"
        observed.append(label)

    def retirement(self, **kwargs):
        assert_durable("retirement")

    def projection(self, result, **kwargs):
        assert_durable("projection")
        raise WorkbenchProjectionPublishError("Injected projection failure")

    monkeypatch.setattr(OneOffVendorRetirementTrigger, "try_retire_after_execution", retirement)
    monkeypatch.setattr(OdooWorkbenchProjectionPublisher, "project_vendor_bill_execution_result", projection)
    settings = _execute_settings().model_copy(update={"odoo_workbench_projection_publish_enabled": True})
    with _normal_execution_api(monkeypatch, transaction_engine, FakeOdooVendorBillClient(), settings=settings) as api:
        response = _api_execute(api)
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "executed"
    assert observed == ["retirement", "projection"]
