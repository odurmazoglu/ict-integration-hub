from __future__ import annotations

from sqlalchemy.orm import Session

from app.application.execution import (
    CustomerInvoiceExecutionStrategy,
    CustomerRechargeExecutionRouter,
    CustomerRechargeExecutionStrategy,
    ExecutionPlanner,
    ExecutionPreflightPolicy,
    ExecutionRetryPolicy,
    ExecutionRuntimeCoordinator,
    ExecutionRuntimeService,
    ExecutionStrategyResolver,
    ExistingPurchaseOrderExecutionStrategy,
    RunAcceptedDecisionExecutionUseCase,
    StaticRetryPolicyResolver,
    VendorBillExecutionStrategy,
    WorkbenchVendorBillExecutionWorkflow,
)
from app.billing import CustomerInvoiceBuilder, VendorBillBuilder
from app.connectors.odoo.client import OdooJson2Client
from app.core.config import Settings
from app.erp.odoo.purchase_order_vendor_bill_repository import PurchaseOrderVendorBillRepository
from app.erp.odoo.workbench_projection_publisher import (
    OdooWorkbenchJson2ProjectionAdapter,
    OdooWorkbenchProjectionFieldMapping,
    OdooWorkbenchProjectionPublisher,
)
from app.erp.write import (
    AccountMoveRepository,
    OdooCustomerInvoiceWritePolicy,
    OdooCustomerInvoiceWriter,
    OdooVendorBillWritePolicy,
    OdooVendorBillWriter,
)
from app.persistence import (
    SqlAlchemyAcceptedBillingEvidenceReader,
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
    accepted_billing_reader = SqlAlchemyAcceptedBillingEvidenceReader(session)
    account_move_repository = AccountMoveRepository(client=odoo_client or OdooJson2Client.from_settings(settings))
    vendor_bill_policy = OdooVendorBillWritePolicy.from_settings(settings)
    customer_invoice_policy = OdooCustomerInvoiceWritePolicy.from_settings(settings)
    writer = OdooVendorBillWriter(
        repository=account_move_repository,
        policy=vendor_bill_policy,
    )
    customer_invoice_writer = OdooCustomerInvoiceWriter(
        repository=account_move_repository,
        policy=customer_invoice_policy,
    )
    strategy = VendorBillExecutionStrategy(
        source_invoice_reader=source_invoice_reader,
        vendor_bill_builder=VendorBillBuilder(),
        vendor_bill_writer=writer,
    )
    purchase_order_vendor_bill_repository = PurchaseOrderVendorBillRepository(
        client=odoo_client or OdooJson2Client.from_settings(settings),
    )
    purchase_order_strategy = ExistingPurchaseOrderExecutionStrategy(
        source_invoice_reader=source_invoice_reader,
        purchase_order_vendor_bill_repository=purchase_order_vendor_bill_repository,
    )
    customer_recharge_strategy = CustomerRechargeExecutionRouter(
        (
            CustomerRechargeExecutionStrategy(),
            CustomerInvoiceExecutionStrategy(
                source_invoice_reader=source_invoice_reader,
                customer_invoice_builder=CustomerInvoiceBuilder(),
                customer_invoice_writer=customer_invoice_writer,
            ),
        )
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
            strategy_resolver=ExecutionStrategyResolver(
                (strategy, purchase_order_strategy, customer_recharge_strategy)
            ),
        ),
        runtime_repository=runtime_repository,
        retry_policy_resolver=StaticRetryPolicyResolver(ExecutionRetryPolicy.immediate(max_attempts=2)),
        execution_preflight=ExecutionPreflightPolicy(
            production_execution_enabled=settings.execution_execute_enabled,
            real_write_gates={
                strategy.supported_step_types[0]: vendor_bill_policy,
                purchase_order_strategy.supported_step_types[0]: vendor_bill_policy,
                customer_recharge_strategy.supported_step_types[0]: customer_invoice_policy,
            },
        ),
        accepted_billing_evidence_reader=accepted_billing_reader,
    )


def build_workbench_vendor_bill_execution_workflow(
    *,
    session: Session,
    settings: Settings,
    odoo_client: OdooJson2Client | None = None,
) -> WorkbenchVendorBillExecutionWorkflow:
    """Compose persisted Workbench Vendor Bill decisions into the existing runtime."""

    review_repository = SqlAlchemyReviewRepository(session)
    source_invoice_reader = SqlAlchemyExecutionSourceInvoiceReader(session)
    runtime_repository = SqlAlchemyExecutionRuntimeRepository(session)
    resolved_odoo_client = odoo_client or OdooJson2Client.from_settings(settings)
    execution_result_publisher = (
        OdooWorkbenchProjectionPublisher(
            adapter=OdooWorkbenchJson2ProjectionAdapter(client=resolved_odoo_client),
            mapping=OdooWorkbenchProjectionFieldMapping.from_environment(),
        )
        if settings.odoo_workbench_projection_publish_enabled
        else None
    )
    return WorkbenchVendorBillExecutionWorkflow(
        accepted_decision_reader=review_repository,
        source_invoice_reader=source_invoice_reader,
        execution_use_case=build_vendor_bill_execution_use_case(
            session=session,
            settings=settings,
            odoo_client=resolved_odoo_client,
        ),
        runtime_repository=runtime_repository,
        execution_result_publisher=execution_result_publisher,
    )
