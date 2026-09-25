from __future__ import annotations

from sqlalchemy.orm import Session

from app.application.execution import (
    CustomerInvoiceExecutionStrategy,
    CustomerQuotationExecutionStrategy,
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
    WorkbenchAcceptedDecisionExecutionDispatcher,
    WorkbenchCustomerQuotationExecutionWorkflow,
    WorkbenchVendorBillExecutionWorkflow,
)
from app.application.execution.contracts import ExecutionStepType
from app.application.execution.resale_execution_accounting import ResaleExecutionAccountingValidator
from app.application.execution.vendor_bill_preview import PreviewVendorBillUseCase
from app.application.workbench.execution_status_use_cases import GetWorkbenchExecutionStatusUseCase
from app.application.workbench.one_off_vendor_use_cases import OneOffVendorRetirementTrigger
from app.application.workbench.vendor_bill_readback import (
    GetVendorBillReadbackUseCase,
    VendorBillResaleReadbackVerifier,
)
from app.billing import CustomerInvoiceBuilder, VendorBillBuilder
from app.composition.purchase_account_discovery import build_get_product_purchase_account_use_case
from app.composition.supplier_remediation import build_archive_one_off_vendor_use_case
from app.connectors.odoo.client import OdooJson2Client
from app.core.config import Settings
from app.erp.odoo.account_move_line_verification_reader import OdooAccountMoveLineVerificationReader
from app.erp.odoo.adapter import OdooReadOnlyAdapter
from app.erp.odoo.fiscal_position_reader import OdooFiscalPositionReader
from app.erp.odoo.purchase_order_vendor_bill_repository import PurchaseOrderVendorBillRepository
from app.erp.odoo.vendor_bill_header_verification_reader import OdooVendorBillHeaderVerificationReader
from app.erp.odoo.vendor_bill_preview_currency_reader import OdooVendorBillPreviewCurrencyReader
from app.erp.odoo.vendor_bill_preview_product_uom_reader import OdooVendorBillPreviewProductUomReader
from app.erp.odoo.workbench_projection_publisher import (
    OdooWorkbenchJson2ProjectionAdapter,
    OdooWorkbenchProjectionFieldMapping,
    OdooWorkbenchProjectionPublisher,
)
from app.erp.write import (
    AccountMoveRepository,
    OdooCustomerInvoiceWritePolicy,
    OdooCustomerInvoiceWriter,
    OdooCustomerQuotationFieldMapping,
    OdooCustomerQuotationPricelistResolver,
    OdooCustomerQuotationRepository,
    OdooCustomerQuotationWritePolicy,
    OdooCustomerQuotationWriter,
    OdooVendorBillWritePolicy,
    OdooVendorBillWriter,
)
from app.persistence import (
    SqlAlchemyAcceptedBillingEvidenceReader,
    SqlAlchemyExecutionRuntimeRepository,
    SqlAlchemyExecutionSourceInvoiceReader,
    SqlAlchemyQuotationScenarioEvidenceRepository,
    SqlAlchemyReviewExecutionEvidenceReader,
    SqlAlchemyReviewOneOffVendorRetirementRepository,
    SqlAlchemyReviewPurchasePurposeResolutionRepository,
    SqlAlchemyReviewRepository,
    SqlAlchemyUnitOfWork,
    SqlAlchemyWriteAuthorizationRepository,
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
    resolved_odoo_client = odoo_client or OdooJson2Client.from_settings(settings)
    account_move_repository = AccountMoveRepository(client=resolved_odoo_client)
    vendor_bill_policy = OdooVendorBillWritePolicy.from_settings(settings)
    staging_vendor_bill_execute = vendor_bill_policy.staging_write_sanctioned
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
        resale_accounting_check=build_resale_execution_accounting_validator(
            session=session,
            settings=settings,
            odoo_client=resolved_odoo_client,
        ),
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
    # P0-PROD-08I: the narrow, best-effort post-Vendor-Bill ONE_OFF_VENDOR retirement
    # hook. Reuses the exact same gated archive-last orchestration composed for the
    # (currently unwired) manual archive path -- no separate authorization surface.
    retirement_trigger = OneOffVendorRetirementTrigger(
        retirement_writer=SqlAlchemyReviewOneOffVendorRetirementRepository(session),
        archive_use_case=build_archive_one_off_vendor_use_case(
            session=session,
            settings=settings,
            odoo_client=odoo_client,
            approved_by="system:vendor-bill-execution",
        ),
    )
    return RunAcceptedDecisionExecutionUseCase(
        unit_of_work=SqlAlchemyUnitOfWork(session),
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
            production_operations_enabled=settings.production_operations_enabled,
            real_write_gates={
                strategy.supported_step_types[0]: vendor_bill_policy,
                purchase_order_strategy.supported_step_types[0]: vendor_bill_policy,
                customer_recharge_strategy.supported_step_types[0]: customer_invoice_policy,
            },
            staging_execution_step_types=((ExecutionStepType.VENDOR_BILL,) if staging_vendor_bill_execute else ()),
        ),
        accepted_billing_evidence_reader=accepted_billing_reader,
        one_off_vendor_retirement_trigger=retirement_trigger,
        write_authorization_repository=SqlAlchemyWriteAuthorizationRepository(session),
    )


def build_resale_execution_accounting_validator(
    *,
    session: Session,
    settings: Settings,
    odoo_client: OdooJson2Client,
) -> ResaleExecutionAccountingValidator:
    """Compose the P0-PROD-18F-2 pre-write RESALE account safety check.

    The pin and purchase purpose come from Hub persistence; current accounting
    configuration from P0-PROD-18D's read-only discovery (same use case as the 18E-1B
    decision gate); fiscal positions from a metadata-guarded read-only reader. The
    allowlist comes only from ``RESALE_PRODUCT_CATEGORY_IDS``. Nothing here can write.
    """

    return ResaleExecutionAccountingValidator(
        pin_reader=SqlAlchemyExecutionSourceInvoiceReader(session),
        purpose_reader=SqlAlchemyReviewPurchasePurposeResolutionRepository(session),
        product_account_resolver=build_get_product_purchase_account_use_case(
            settings=settings,
            odoo_client=odoo_client,
        ),
        fiscal_position_reader=OdooFiscalPositionReader(adapter=OdooReadOnlyAdapter(client=odoo_client)),
        approved_category_ids=settings.resale_product_category_ids,
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


def build_customer_quotation_execution_use_case(
    *,
    session: Session,
    settings: Settings,
    odoo_client: OdooJson2Client | None = None,
) -> RunAcceptedDecisionExecutionUseCase:
    """Compose accepted CUSTOMER_QUOTATION decisions into the shared execution runtime.

    Registers ``CustomerQuotationExecutionStrategy`` only for
    ``ExecutionStepType.CREATE_CUSTOMER_QUOTATION``; existing strategies are not
    touched. Execution consumes immutable ``quotation_scenario_evidence`` and
    never rereads Odoo Proposal Scenario authoring records.
    """

    resolved_odoo_client = odoo_client or OdooJson2Client.from_settings(settings)
    runtime_repository = SqlAlchemyExecutionRuntimeRepository(session)
    quotation_write_policy = OdooCustomerQuotationWritePolicy.from_settings(settings)
    quotation_field_mapping = OdooCustomerQuotationFieldMapping.from_environment()
    customer_quotation_writer = OdooCustomerQuotationWriter(
        repository=OdooCustomerQuotationRepository(
            client=resolved_odoo_client,
            mapping=quotation_field_mapping,
        ),
        pricelist_resolver=OdooCustomerQuotationPricelistResolver(client=resolved_odoo_client),
        policy=quotation_write_policy,
    )
    strategy = CustomerQuotationExecutionStrategy(
        quotation_evidence_reader=SqlAlchemyQuotationScenarioEvidenceRepository(session),
        customer_quotation_writer=customer_quotation_writer,
    )
    return RunAcceptedDecisionExecutionUseCase(
        unit_of_work=SqlAlchemyUnitOfWork(session),
        accepted_decision_reader=SqlAlchemyReviewRepository(session),
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
            real_write_gates={ExecutionStepType.CREATE_CUSTOMER_QUOTATION: quotation_write_policy},
            writer_step_types=(ExecutionStepType.CREATE_CUSTOMER_QUOTATION,),
        ),
    )


def build_workbench_customer_quotation_execution_workflow(
    *,
    session: Session,
    settings: Settings,
    odoo_client: OdooJson2Client | None = None,
) -> WorkbenchCustomerQuotationExecutionWorkflow:
    """Compose persisted Workbench CUSTOMER_QUOTATION decisions into the shared runtime."""

    resolved_odoo_client = odoo_client or OdooJson2Client.from_settings(settings)
    runtime_repository = SqlAlchemyExecutionRuntimeRepository(session)
    return WorkbenchCustomerQuotationExecutionWorkflow(
        accepted_decision_reader=SqlAlchemyReviewRepository(session),
        quotation_evidence_reader=SqlAlchemyQuotationScenarioEvidenceRepository(session),
        execution_use_case=build_customer_quotation_execution_use_case(
            session=session,
            settings=settings,
            odoo_client=resolved_odoo_client,
        ),
        runtime_repository=runtime_repository,
    )


def build_vendor_bill_preview_use_case(
    *,
    session: Session,
    settings: Settings,
    odoo_client: OdooJson2Client | None = None,
) -> PreviewVendorBillUseCase:
    """Compose the zero-write Vendor Bill preview (P0-PROD-09B).

    Deliberately does NOT depend on ``AccountMoveRepository``/``OdooVendorBillWriter``
    or any other write-capable port -- its only ERP dependencies are
    ``OdooVendorBillPreviewCurrencyReader`` and (P0-PROD-10E)
    ``OdooVendorBillPreviewProductUomReader``, both built on the same structurally
    read-only ``OdooReadOnlyAdapter`` used throughout the read/matching layer (see
    ``OdooSelectedAccountReader``/``OdooSelectedProductReader`` for the established
    precedent). No write gate is read or checked anywhere in this composition --
    preview is available regardless of EXECUTION_EXECUTE_ENABLED or
    SUPPLIER_REMEDIATION_WRITE_ENABLED.
    """

    read_only_adapter = OdooReadOnlyAdapter(client=odoo_client or OdooJson2Client.from_settings(settings))
    source_invoice_reader = SqlAlchemyExecutionSourceInvoiceReader(session)
    return PreviewVendorBillUseCase(
        accepted_decision_reader=SqlAlchemyReviewRepository(session),
        source_invoice_reader=source_invoice_reader,
        execution_planner=ExecutionPlanner(),
        vendor_bill_builder=VendorBillBuilder(),
        currency_reader=OdooVendorBillPreviewCurrencyReader(adapter=read_only_adapter),
        product_uom_reader=OdooVendorBillPreviewProductUomReader(adapter=read_only_adapter),
        # P0-PROD-18F-1: RESALE accounting comes from the decision's own pin in Hub
        # persistence -- no Odoo accounting reader is composed into preview at all.
        resale_accounting_pin_reader=source_invoice_reader,
        purchase_purpose_reader=SqlAlchemyReviewPurchasePurposeResolutionRepository(session),
    )


def build_get_workbench_execution_status_use_case(*, session: Session) -> GetWorkbenchExecutionStatusUseCase:
    """Compose the zero-write operator execution/recovery status read (P0-PROD-12A).

    Every dependency is an existing read-only reader/repository already used
    elsewhere in this module for preview/execution composition -- this adds no
    new Odoo dependency and no new write-capable port.
    """

    return GetWorkbenchExecutionStatusUseCase(
        review_reader=SqlAlchemyReviewRepository(session),
        accepted_decision_reader=SqlAlchemyReviewRepository(session),
        execution_snapshot_reader=SqlAlchemyExecutionRuntimeRepository(session),
        stage_one_evidence_reader=SqlAlchemyReviewExecutionEvidenceReader(session),
        stage_two_evidence_reader=SqlAlchemyExecutionSourceInvoiceReader(session),
        write_authorization_repository=SqlAlchemyWriteAuthorizationRepository(session),
    )


def build_get_vendor_bill_readback_use_case(
    *,
    session: Session,
    settings: Settings,
    odoo_client: OdooJson2Client | None = None,
) -> GetVendorBillReadbackUseCase:
    """Compose artifact-derived verification from read-only Hub and Odoo readers."""

    adapter = OdooReadOnlyAdapter(client=odoo_client or OdooJson2Client.from_settings(settings))
    return GetVendorBillReadbackUseCase(
        review_reader=SqlAlchemyReviewRepository(session),
        execution_snapshot_reader=SqlAlchemyExecutionRuntimeRepository(session),
        header_reader=OdooVendorBillHeaderVerificationReader(adapter=adapter),
        line_reader=OdooAccountMoveLineVerificationReader(adapter=adapter),
        # P0-PROD-18F-2: RESALE bills are also checked against their immutable pin.
        resale_verifier=VendorBillResaleReadbackVerifier(
            pin_reader=SqlAlchemyExecutionSourceInvoiceReader(session),
            purpose_reader=SqlAlchemyReviewPurchasePurposeResolutionRepository(session),
            fiscal_position_reader=OdooFiscalPositionReader(adapter=adapter),
        ),
    )


def build_workbench_accepted_decision_execution_dispatcher(
    *,
    session: Session,
    settings: Settings,
    odoo_client: OdooJson2Client | None = None,
) -> WorkbenchAcceptedDecisionExecutionDispatcher:
    """Compose the ``/reviews/{review_id}/execute`` dispatcher over both sub-workflows."""

    resolved_odoo_client = odoo_client or OdooJson2Client.from_settings(settings)
    return WorkbenchAcceptedDecisionExecutionDispatcher(
        accepted_decision_reader=SqlAlchemyReviewRepository(session),
        vendor_bill_workflow=build_workbench_vendor_bill_execution_workflow(
            session=session,
            settings=settings,
            odoo_client=resolved_odoo_client,
        ),
        customer_quotation_workflow=build_workbench_customer_quotation_execution_workflow(
            session=session,
            settings=settings,
            odoo_client=resolved_odoo_client,
        ),
    )
