"""Composition of the ADR-0013 Workbench operator request tick.

Every handler is built from the *same* composer the corresponding REST endpoint uses,
so a request from Odoo runs exactly the code an authenticated API call would run.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Callable

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.application.workbench.exceptions import ReviewVersionConflictError
from app.application.workbench.operator_request_handlers import (
    AccountingResolutionRequestHandler,
    CompletedVendorBillExecutionProbe,
    DecisionRequestHandler,
    ExecuteVendorBillRequestHandler,
    PurchasePurposeRequestHandler,
    SupplierResolutionRequestHandler,
)
from app.application.workbench.operator_request_ingestion import (
    STALE_REQUEST_MESSAGE,
    OperatorActorDirectory,
    OperatorRequestAction,
    OperatorRequestIngestionResult,
    OperatorRequestIngestionWorkflow,
)
from app.application.workbench.queries import ReviewDetailQuery
from app.application.workbench.write_authorization import WriteAuthorizationOperationType
from app.application.workbench.write_authorization_use_cases import CreateWriteAuthorizationUseCase
from app.composition.execution import build_workbench_accepted_decision_execution_dispatcher
from app.composition.imports import (
    build_odoo_decision_submitter,
    build_runtime_workbench_projection_synchronizer,
    build_workbench_erp_reference_validator,
)
from app.composition.purchase_purpose_and_accounting_resolution import (
    build_submit_purchase_purpose_use_case,
    build_submit_review_accounting_resolution_use_case,
)
from app.composition.supplier_remediation import build_resolve_workbench_supplier_use_case
from app.composition.write_authorization import build_create_write_authorization_use_case
from app.connectors.exceptions import ConnectorError
from app.connectors.odoo.client import OdooJson2Client
from app.core.config import Settings
from app.erp.exceptions import ErpRepositoryError
from app.erp.odoo.adapter import OdooReadOnlyAdapter
from app.erp.odoo.workbench_candidate_reader import OdooWorkbenchDecisionCandidateReader, OdooWorkbenchFieldMapping
from app.erp.odoo.workbench_operator_request_reader import (
    OdooOperatorRequestAcknowledger,
    OdooOperatorRequestFieldMapping,
    OdooOperatorRequestReader,
)
from app.erp.odoo.workbench_projection_publisher import OdooWorkbenchJson2ProjectionAdapter
from app.persistence import SqlAlchemyReviewRepository, SqlAlchemyUnitOfWork
from app.persistence.execution_runtime_repository import SqlAlchemyExecutionRuntimeRepository
from app.persistence.workbench_operator_request_ledger import SqlAlchemyOperatorRequestLedger
from app.services.uyumsoft_inbound_poll import InProcessPollLock, PollLock, PostgresAdvisoryPollLock

logger = logging.getLogger(__name__)

#: Distinct from the Uyumsoft poll lock: the two ticks never block each other.
OPERATOR_REQUEST_ADVISORY_LOCK_KEY = -2752363236075536947
OPERATOR_REQUEST_JUSTIFICATION = "Odoo Workbench operator request (ADR-0013)"


class ReviewVersionCheckedAuthorizationIssuer:
    """Issue the existing narrow write authorization for exactly the version the operator saw.

    The version check is the request's own optimistic-concurrency contract: a moved
    review is reported as stale instead of as an authorization scope error.
    """

    def __init__(self, *, use_case: CreateWriteAuthorizationUseCase, review_reader: SqlAlchemyReviewRepository) -> None:
        self._use_case = use_case
        self._review_reader = review_reader

    def issue(
        self, *, review_id: str, company_id: int, target_version: int, operation_type: str, authorized_by: str
    ) -> str:
        review = self._review_reader.get_review_item(ReviewDetailQuery(review_id=review_id, company_id=company_id))
        if review.version != target_version:
            raise ReviewVersionConflictError(STALE_REQUEST_MESSAGE)
        record = self._use_case.execute(
            company_id=company_id,
            review_id=review_id,
            decision_version=target_version,
            operation_type=WriteAuthorizationOperationType(operation_type),
            authorized_by=authorized_by,
            justification=OPERATOR_REQUEST_JUSTIFICATION,
        )
        return record.authorization_id


def decision_mapping_for_requests(
    base: OdooWorkbenchFieldMapping, request: OdooOperatorRequestFieldMapping
) -> OdooWorkbenchFieldMapping:
    """The existing decision reader, tied to the request's snapshotted version/requester/ready flag."""

    return dataclasses.replace(
        base,
        parent=dataclasses.replace(
            base.parent,
            expected_version=request.expected_version,
            decision_ready=request.ready,
            decided_by=request.requested_by,
            decided_at=request.requested_at,
        ),
    )


def build_operator_request_workflow(
    *,
    business_session: Session,
    ledger_session: Session,
    settings: Settings,
    odoo_client: OdooJson2Client | None = None,
    request_mapping: OdooOperatorRequestFieldMapping | None = None,
    decision_mapping: OdooWorkbenchFieldMapping | None = None,
) -> OperatorRequestIngestionWorkflow:
    client = odoo_client or OdooJson2Client.from_settings(settings)
    mapping = request_mapping or OdooOperatorRequestFieldMapping.from_environment()
    projection_adapter = OdooWorkbenchJson2ProjectionAdapter(client=client)
    read_adapter = OdooReadOnlyAdapter(client=client)
    review_repository = SqlAlchemyReviewRepository(business_session)
    handlers = {
        OperatorRequestAction.SUPPLIER_RESOLUTION: SupplierResolutionRequestHandler(
            use_case=build_resolve_workbench_supplier_use_case(
                session=business_session, settings=settings, odoo_client=client
            )
        ),
        OperatorRequestAction.PURCHASE_PURPOSE: PurchasePurposeRequestHandler(
            use_case=build_submit_purchase_purpose_use_case(session=business_session)
        ),
        OperatorRequestAction.ACCOUNTING_RESOLUTION: AccountingResolutionRequestHandler(
            use_case=build_submit_review_accounting_resolution_use_case(
                session=business_session, settings=settings, odoo_client=client
            )
        ),
        OperatorRequestAction.DECISION: DecisionRequestHandler(
            candidate_reader=OdooWorkbenchDecisionCandidateReader(
                adapter=read_adapter,
                mapping=decision_mapping
                or decision_mapping_for_requests(OdooWorkbenchFieldMapping.from_environment(), mapping),
            ),
            erp_reference_validator=build_workbench_erp_reference_validator(read_adapter),
            decision_submitter=build_odoo_decision_submitter(
                session=business_session, settings=settings, odoo_client=client, read_adapter=read_adapter
            ),
            unit_of_work=SqlAlchemyUnitOfWork(business_session),
        ),
        OperatorRequestAction.EXECUTE_VENDOR_BILL: ExecuteVendorBillRequestHandler(
            dispatcher=build_workbench_accepted_decision_execution_dispatcher(
                session=business_session, settings=settings, odoo_client=client
            ),
            completion_probe=CompletedVendorBillExecutionProbe(
                accepted_decision_reader=review_repository,
                snapshot_reader=SqlAlchemyExecutionRuntimeRepository(business_session),
            ),
        ),
    }
    return OperatorRequestIngestionWorkflow(
        reader=OdooOperatorRequestReader(adapter=projection_adapter, mapping=mapping),
        acknowledger=OdooOperatorRequestAcknowledger(adapter=projection_adapter, mapping=mapping),
        ledger=SqlAlchemyOperatorRequestLedger(ledger_session),
        actors=OperatorActorDirectory.from_json(settings.odoo_operator_request_actors),
        handlers=handlers,
        authorization_issuer=ReviewVersionCheckedAuthorizationIssuer(
            use_case=build_create_write_authorization_use_case(session=business_session),
            review_reader=review_repository,
        ),
        projection_refresher=build_runtime_workbench_projection_synchronizer(
            session=business_session, settings=settings, odoo_client=client
        ),
        transient_errors=(ErpRepositoryError, ConnectorError),
    )


class OperatorRequestTick:
    """One single-flight tick: fresh sessions and workflow, one run, everything closed."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        lock: PollLock,
        company_id: int,
        workflow_factory: Callable[[Session, Session], OperatorRequestIngestionWorkflow],
    ) -> None:
        self._session_factory = session_factory
        self._lock = lock
        self._company_id = company_id
        self._workflow_factory = workflow_factory

    def run(self) -> OperatorRequestIngestionResult | None:
        with self._lock.hold() as acquired:
            if not acquired:
                logger.info("workbench.operator_requests.skipped_locked")
                return None
            business_session = self._session_factory()
            ledger_session = self._session_factory()
            try:
                return self._workflow_factory(business_session, ledger_session).run(company_id=self._company_id)
            finally:
                business_session.close()
                ledger_session.close()


def build_operator_request_tick(
    *, settings: Settings, engine: Engine, odoo_client: OdooJson2Client | None = None
) -> OperatorRequestTick:
    if settings.odoo_workbench_operator_requests_company_id is None:
        raise ValueError("ODOO_WORKBENCH_OPERATOR_REQUESTS_COMPANY_ID is required.")
    # Fail at startup, not on the first request, if the mapping or actor JSON is wrong.
    mapping = OdooOperatorRequestFieldMapping.from_environment()
    OperatorActorDirectory.from_json(settings.odoo_operator_request_actors)
    session_factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    lock: PollLock = (
        PostgresAdvisoryPollLock(engine, key=OPERATOR_REQUEST_ADVISORY_LOCK_KEY)
        if engine.dialect.name == "postgresql"
        else InProcessPollLock()
    )
    return OperatorRequestTick(
        session_factory=session_factory,
        lock=lock,
        company_id=settings.odoo_workbench_operator_requests_company_id,
        workflow_factory=lambda business, ledger: build_operator_request_workflow(
            business_session=business,
            ledger_session=ledger,
            settings=settings,
            odoo_client=odoo_client,
            request_mapping=mapping,
        ),
    )
