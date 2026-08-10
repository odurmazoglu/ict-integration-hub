from __future__ import annotations

from sqlalchemy.orm import Session

from app.application.execution import (
    ExecutionPlanner,
    ExecutionPreflightPolicy,
    ExecutionRetryPolicy,
    ExecutionRuntimeCoordinator,
    ExecutionRuntimeService,
    ExecutionStrategyResolver,
    RunAcceptedDecisionExecutionUseCase,
    StaticRetryPolicyResolver,
    VendorBillExecutionStrategy,
)
from app.billing import VendorBillBuilder
from app.connectors.odoo.client import OdooJson2Client
from app.core.config import Settings
from app.erp.write import AccountMoveRepository, OdooVendorBillWritePolicy, OdooVendorBillWriter
from app.persistence import (
    SqlAlchemyExecutionRuntimeRepository,
    SqlAlchemyExecutionSourceInvoiceReader,
    SqlAlchemyReviewRepository,
)


def build_vendor_bill_execution_use_case(
    *,
    session: Session,
    settings: Settings,
    odoo_client: OdooJson2Client | None = None,
) -> RunAcceptedDecisionExecutionUseCase:
    """Compose accepted Vendor Bill decisions into the durable production runtime."""

    review_repository = SqlAlchemyReviewRepository(session)
    runtime_repository = SqlAlchemyExecutionRuntimeRepository(session)
    source_invoice_reader = SqlAlchemyExecutionSourceInvoiceReader(session)
    writer_policy = OdooVendorBillWritePolicy.from_settings(settings)
    writer = OdooVendorBillWriter(
        repository=AccountMoveRepository(client=odoo_client or OdooJson2Client.from_settings(settings)),
        policy=writer_policy,
    )
    strategy = VendorBillExecutionStrategy(
        source_invoice_reader=source_invoice_reader,
        vendor_bill_builder=VendorBillBuilder(),
        vendor_bill_writer=writer,
    )
    return RunAcceptedDecisionExecutionUseCase(
        accepted_decision_reader=review_repository,
        execution_planner=ExecutionPlanner(),
        runtime_service=ExecutionRuntimeService(
            runtime_repository=runtime_repository,
            event_repository=runtime_repository,
        ),
        runtime_coordinator=ExecutionRuntimeCoordinator(
            runtime_repository=runtime_repository,
            event_repository=runtime_repository,
            strategy_resolver=ExecutionStrategyResolver((strategy,)),
        ),
        runtime_repository=runtime_repository,
        retry_policy_resolver=StaticRetryPolicyResolver(ExecutionRetryPolicy.immediate(max_attempts=2)),
        execution_preflight=ExecutionPreflightPolicy(
            production_execution_enabled=settings.execution_execute_enabled,
            real_write_gate=writer_policy,
        ),
    )
