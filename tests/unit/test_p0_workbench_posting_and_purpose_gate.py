"""P0 Workbench safety: draft-only operator instruction and the product-coded purpose gate.

P0-1: after a draft Vendor Bill is created the operator is told to check it in Odoo and
NOT to post, pay or reconcile it without the accounting go-live decision.

P0-2: a fresh product-coded Vendor Bill decision is refused unless the current review
version carries an explicitly recorded RESALE or INTERNAL_USE purchase purpose, and that
purpose's own safeguard produced its accounting pin. Accepted decisions replay unchanged.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.application.execution import ExecutionMode
from app.application.execution.contracts import ExecutionArtifact, ExecutionArtifactType
from app.application.execution.workbench_vendor_bill import (
    WorkbenchVendorBillExecutionResult,
    WorkbenchVendorBillExecutionStatus,
)
from app.application.workbench import ReviewDecisionType, ReviewStatus, SubmitReviewDecisionUseCase
from app.application.workbench.exceptions import (
    InternalUseDecisionEligibilityError,
    ProductPurchasePurposeDecisionError,
    ReviewNotFoundError,
)
from app.application.workbench.operator_guidance import (
    GuidanceInput,
    OperatorGuidanceFacts,
    OperatorNextAction,
    build_operator_guidance,
    draft_vendor_bill_created_message,
)
from app.application.workbench.operator_request_handlers import _execution_outcome
from app.application.workbench.operator_request_ingestion import OperatorRequestOutcome
from app.application.workbench.product_purchase_purpose_gate import ProductPurchasePurposeGate
from app.application.workbench.purchase_purpose import PurchasePurpose
from app.application.workbench.resale_decision_gate import ResaleDecisionGate
from app.application.workflow import WorkflowType
from app.db.base import Base
from app.models.execution_source_invoice_evidence import ExecutionSourceInvoiceEvidence
from app.models.workbench_review_accounting_resolution import WorkbenchReviewAccountingResolution
from app.models.workbench_review_decision import WorkbenchReviewDecision
from app.models.workbench_review_execution_evidence import WorkbenchReviewExecutionEvidence
from app.models.workbench_review_item import WorkbenchReviewItem
from app.models.workbench_review_purchase_purpose_resolution import WorkbenchReviewPurchasePurposeResolution
from app.persistence import (
    SqlAlchemyReviewExecutionEvidenceReader,
    SqlAlchemyReviewPurchasePurposeResolutionRepository,
    SqlAlchemyReviewRepository,
    SqlAlchemyUnitOfWork,
)
from app.persistence.execution_source_invoice_reader import SqlAlchemyExecutionSourceInvoiceReader
from tests.unit.source_tax_support import ALLOW_SOURCE_TAX_SAFETY_CHECK
from tests.unit.test_p0_internal_use_vendor_bill import (
    DECISION_VERSION,
    INTERNAL_ACCOUNT,
    PRODUCT_A,
    PRODUCT_B,
    _decision,
    _FakeAccounts,
    _FakeProductAccountResolver,
    _gate,
    _invoice,
    _line,
    _record_expense_account,
    _record_purpose,
    _seed_unblocked_review,
)
from tests.unit.test_p0_prod_18e_1b_resale_workflow_wiring import (
    CHILD_CATEGORY_ID,
    COMPANY_ID,
    REVIEW_ID,
    _discovery,
)

EXPECTED_DRAFT_MESSAGE = (
    "Taslak alış faturası oluşturuldu. Faturayı Odoo'da kontrol edin. Muhasebe geçiş kararı olmadan "
    "ONAYLAMAYIN; ödeme veya mutabakat yapmayın."
)
APP_ROOT = Path(__file__).resolve().parents[2] / "app"


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            WorkbenchReviewItem.__table__,
            WorkbenchReviewExecutionEvidence.__table__,
            WorkbenchReviewDecision.__table__,
            ExecutionSourceInvoiceEvidence.__table__,
            WorkbenchReviewPurchasePurposeResolution.__table__,
            WorkbenchReviewAccountingResolution.__table__,
        ],
    )
    with sessionmaker(bind=engine)() as db_session:
        yield db_session


def _resolver() -> _FakeProductAccountResolver:
    return _FakeProductAccountResolver({PRODUCT_A: _discovery(PRODUCT_A), PRODUCT_B: _discovery(PRODUCT_B)})


def _use_case(
    session: Session,
    *,
    purpose_gate: bool = True,
    resale_gate: bool = True,
    internal_use_gate: bool = True,
    accounts: _FakeAccounts | None = None,
) -> SubmitReviewDecisionUseCase:
    purposes = SqlAlchemyReviewPurchasePurposeResolutionRepository(session)
    return SubmitReviewDecisionUseCase(
        source_tax_safety_check=ALLOW_SOURCE_TAX_SAFETY_CHECK,
        review_decision_writer=SqlAlchemyReviewRepository(session),
        unit_of_work=SqlAlchemyUnitOfWork(session),
        execution_evidence_reader=SqlAlchemyReviewExecutionEvidenceReader(session),
        resale_decision_gate=(
            ResaleDecisionGate(
                purpose_reader=purposes,
                product_account_resolver=_resolver(),
                approved_category_ids=frozenset({CHILD_CATEGORY_ID}),
            )
            if resale_gate
            else None
        ),
        internal_use_decision_gate=_gate(session, accounts) if internal_use_gate else None,
        product_purchase_purpose_gate=ProductPurchasePurposeGate(purpose_reader=purposes) if purpose_gate else None,
    )


def _assert_nothing_persisted(session: Session) -> None:
    assert session.query(WorkbenchReviewDecision).count() == 0
    assert session.query(ExecutionSourceInvoiceEvidence).count() == 0
    assert session.query(WorkbenchReviewItem).filter_by(review_id=REVIEW_ID).one().version == 1


def _evidence(session: Session):
    return SqlAlchemyReviewExecutionEvidenceReader(session).get_evidence(
        review_id=REVIEW_ID, company_id=COMPANY_ID, expected_version=1
    )


# =========================================================================== P0-1 draft-only instruction


def test_draft_message_forbids_posting_payment_and_reconciliation() -> None:
    assert draft_vendor_bill_created_message() == EXPECTED_DRAFT_MESSAGE
    assert draft_vendor_bill_created_message(" (Odoo kayıt 76)").startswith(
        "Taslak alış faturası oluşturuldu (Odoo kayıt 76). Faturayı Odoo'da kontrol edin."
    )


def _completed_guidance(execution_state: str | None):
    return build_operator_guidance(
        GuidanceInput(
            status=ReviewStatus.DECISION_SUBMITTED,
            reason_codes=(),
            supplier_name="LOGOSOFT",
            decision_type=ReviewDecisionType.SELECT_WORKFLOW,
            decision_workflow=WorkflowType.VENDOR_BILL,
            decision_version=2,
            execution_state=execution_state,
            vendor_bill_id=76,
        ),
        OperatorGuidanceFacts(),
    )


def test_guidance_after_draft_creation_says_do_not_post() -> None:
    guidance = _completed_guidance("completed")
    assert guidance.next_action is OperatorNextAction.DONE
    assert "ONAYLAMAYIN; ödeme veya mutabakat yapmayın." in guidance.todo_html
    assert "onaylayın" not in guidance.todo_html
    # The status / action values themselves are unchanged.
    assert _completed_guidance(None).next_action is OperatorNextAction.EXECUTE


def _execution_result(status: WorkbenchVendorBillExecutionStatus) -> WorkbenchVendorBillExecutionResult:
    return WorkbenchVendorBillExecutionResult(
        review_id=REVIEW_ID,
        company_id=COMPANY_ID,
        decision_version=2,
        mode=ExecutionMode.EXECUTE,
        status=status,
        artifacts=(
            ExecutionArtifact(
                artifact_type=ExecutionArtifactType.VENDOR_BILL,
                artifact_id="76",
                external_identity="hub:vendor-bill",
                created=True,
            ),
        ),
    )


def test_execute_request_outcome_says_do_not_post_and_keeps_status() -> None:
    outcome = _execution_outcome(_execution_result(WorkbenchVendorBillExecutionStatus.EXECUTED))
    assert outcome.outcome is OperatorRequestOutcome.COMPLETED
    assert outcome.message == draft_vendor_bill_created_message(" (Odoo kayıt 76)")
    already = _execution_outcome(_execution_result(WorkbenchVendorBillExecutionStatus.ALREADY_EXECUTED))
    assert already.outcome is OperatorRequestOutcome.ALREADY_COMPLETED
    assert "tekrar oluşturulmadı" in already.message


def test_no_operator_message_tells_the_operator_to_confirm_or_post() -> None:
    offenders = [
        str(path.relative_to(APP_ROOT))
        for path in APP_ROOT.rglob("*.py")
        if "onaylayın" in path.read_text(encoding="utf-8").lower()
    ]
    assert offenders == []


def test_hub_never_posts_vendor_bills() -> None:
    """``action_post`` only appears in the forbidden-method/field lists."""

    allowed = {"erp/write/account_move_repository.py", "erp/odoo/adapter.py", "services/odoo_staging_validation.py"}
    users = {
        str(path.relative_to(APP_ROOT))
        for path in APP_ROOT.rglob("*.py")
        if "action_post" in path.read_text(encoding="utf-8")
    }
    assert users <= allowed


# =========================================================================== P0-2 decision gate


def test_missing_purpose_rejects_product_coded_decision(session: Session) -> None:
    _seed_unblocked_review(session)
    with pytest.raises(ProductPurchasePurposeDecisionError, match="explicitly recorded purchase purpose"):
        _use_case(session).execute(_decision())
    _assert_nothing_persisted(session)


def test_purpose_for_another_version_does_not_carry_over(session: Session) -> None:
    _seed_unblocked_review(session)
    _record_purpose(session, PurchasePurpose.RESALE, review_version=7)
    with pytest.raises(ProductPurchasePurposeDecisionError, match="explicitly recorded purchase purpose"):
        _use_case(session).execute(_decision())
    _assert_nothing_persisted(session)


def test_resale_uses_the_resale_safeguards(session: Session) -> None:
    _seed_unblocked_review(session)
    _record_purpose(session, PurchasePurpose.RESALE)

    _use_case(session).execute(_decision())

    stored = session.query(ExecutionSourceInvoiceEvidence).one()
    assert stored.resale_accounting_pin is not None
    assert stored.internal_use_accounting_pin is None


def test_internal_use_uses_the_approved_accounting_resolution(session: Session) -> None:
    _seed_unblocked_review(session)
    _record_purpose(session, PurchasePurpose.INTERNAL_USE)
    _record_expense_account(session)

    _use_case(session).execute(_decision())

    stored = session.query(ExecutionSourceInvoiceEvidence).one()
    assert stored.internal_use_accounting_pin is not None
    assert stored.internal_use_accounting_pin["expense_account_id"] == INTERNAL_ACCOUNT.id


def test_internal_use_without_account_is_rejected(session: Session) -> None:
    _seed_unblocked_review(session)
    _record_purpose(session, PurchasePurpose.INTERNAL_USE)
    with pytest.raises(InternalUseDecisionEligibilityError, match="explicitly selected internal expense account"):
        _use_case(session).execute(_decision())
    _assert_nothing_persisted(session)


@pytest.mark.parametrize("purpose", [PurchasePurpose.CUSTOMER_PROJECT, PurchasePurpose.OTHER_OPERATING_EXPENSE])
def test_purposes_without_product_safeguards_are_rejected(session: Session, purpose: PurchasePurpose) -> None:
    _seed_unblocked_review(session)
    _record_purpose(session, purpose)
    _record_expense_account(session)  # even with an account, no fallback path exists
    with pytest.raises(ProductPurchasePurposeDecisionError, match="not supported for product-coded"):
        _use_case(session).execute(_decision())
    _assert_nothing_persisted(session)


@pytest.mark.parametrize(
    ("purpose", "kwargs", "fragment"),
    [
        (PurchasePurpose.RESALE, {"resale_gate": False}, "RESALE product-invoice safeguards"),
        (PurchasePurpose.INTERNAL_USE, {"internal_use_gate": False}, "INTERNAL_USE product-invoice accounting"),
    ],
)
def test_no_unintended_account_fallback_without_the_safeguard(
    session: Session, purpose: PurchasePurpose, kwargs: dict[str, Any], fragment: str
) -> None:
    _seed_unblocked_review(session)
    _record_purpose(session, purpose)
    _record_expense_account(session)
    with pytest.raises(ProductPurchasePurposeDecisionError, match=fragment):
        _use_case(session, **kwargs).execute(_decision())
    _assert_nothing_persisted(session)


def test_identifier_free_invoices_need_no_product_purpose(session: Session) -> None:
    _seed_unblocked_review(session)
    gate = ProductPurchasePurposeGate(purpose_reader=SqlAlchemyReviewPurchasePurposeResolutionRepository(session))
    identifier_free = replace(_evidence(session), invoice=_invoice([_line("1"), _line("2")]))
    assert gate.require(_decision(), identifier_free) is None
    with pytest.raises(ProductPurchasePurposeDecisionError):
        gate.require(_decision(), _evidence(session))


def test_existing_accepted_decision_replays_without_a_purpose(session: Session) -> None:
    """A decision accepted before the gate (no purpose, no pin) replays unchanged."""

    _seed_unblocked_review(session)
    _use_case(session, purpose_gate=False).execute(_decision())  # pre-gate acceptance
    assert session.query(WorkbenchReviewDecision).count() == 1

    replay = _use_case(session).execute(_decision())

    assert replay.version == DECISION_VERSION
    assert session.query(WorkbenchReviewDecision).count() == 1
    assert session.query(ExecutionSourceInvoiceEvidence).count() == 1


def test_legacy_accepted_decision_reads_back_without_pins(session: Session) -> None:
    _seed_unblocked_review(session)
    _use_case(session, purpose_gate=False).execute(_decision())
    reader = SqlAlchemyExecutionSourceInvoiceReader(session)
    query = {"review_id": REVIEW_ID, "company_id": COMPANY_ID, "decision_version": DECISION_VERSION}
    assert reader.get_resale_accounting_pin(**query) is None
    assert reader.get_internal_use_accounting_pin(**query) is None


# =========================================================================== guidance


def _pending_action(facts: OperatorGuidanceFacts) -> OperatorNextAction:
    return build_operator_guidance(
        GuidanceInput(
            status=ReviewStatus.PENDING_REVIEW,
            reason_codes=(),
            supplier_name="LOGOSOFT",
            decision_type=None,
            decision_workflow=None,
            decision_version=None,
            execution_state=None,
            vendor_bill_id=None,
        ),
        facts,
    ).next_action


def test_guidance_requires_purpose_for_product_coded_reviews() -> None:
    assert _pending_action(OperatorGuidanceFacts(product_coded=True)) is OperatorNextAction.PURPOSE
    assert (
        _pending_action(OperatorGuidanceFacts(product_coded=True, current_purchase_purpose=PurchasePurpose.RESALE))
        is OperatorNextAction.DECISION
    )
    assert (
        _pending_action(
            OperatorGuidanceFacts(product_coded=True, current_purchase_purpose=PurchasePurpose.INTERNAL_USE)
        )
        is OperatorNextAction.ACCOUNTING
    )
    assert (
        _pending_action(
            OperatorGuidanceFacts(
                product_coded=True,
                current_purchase_purpose=PurchasePurpose.INTERNAL_USE,
                current_accounting_resolution_recorded=True,
            )
        )
        is OperatorNextAction.DECISION
    )
    assert (
        _pending_action(
            OperatorGuidanceFacts(product_coded=True, current_purchase_purpose=PurchasePurpose.CUSTOMER_PROJECT)
        )
        is OperatorNextAction.TECHNICAL
    )
    # Identifier-free reviews keep their existing guidance.
    assert _pending_action(OperatorGuidanceFacts()) is OperatorNextAction.DECISION


class _FakeSourceReader:
    def __init__(self, invoice) -> None:
        self._invoice = invoice

    def get(self, *, review_id: str, company_id: int):
        if self._invoice is None:
            raise ReviewNotFoundError("missing")
        return SimpleNamespace(invoice=self._invoice)


def test_product_coded_fact_comes_from_the_source_invoice() -> None:
    from app.composition.imports import _is_product_coded

    review = SimpleNamespace(review_id=REVIEW_ID)
    coded = _invoice([_line("1", seller_item_code="SKU-1")])
    identifier_free = _invoice([_line("1")])
    assert _is_product_coded(review, COMPANY_ID, _FakeSourceReader(coded)) is True
    assert _is_product_coded(review, COMPANY_ID, _FakeSourceReader(identifier_free)) is False
    assert _is_product_coded(review, COMPANY_ID, _FakeSourceReader(None)) is False
    assert _is_product_coded(review, COMPANY_ID, None) is False


# =========================================================================== API + wiring


def test_purpose_gate_error_maps_to_http_conflict() -> None:
    from http import HTTPStatus

    from app.api.routers.workbench import _status_code_for_exception

    assert _status_code_for_exception(ProductPurchasePurposeDecisionError("x")) == HTTPStatus.CONFLICT


def test_api_decision_use_case_wires_the_purpose_gate() -> None:
    from unittest.mock import MagicMock

    from app.api.dependencies import get_submit_review_decision_use_case
    from app.connectors.odoo.client import OdooJson2Client
    from app.core.config import Settings

    settings = Settings()
    use_case = get_submit_review_decision_use_case(
        writer=MagicMock(), session=MagicMock(), odoo_client=OdooJson2Client.from_settings(settings), settings=settings
    )
    assert isinstance(use_case._product_purchase_purpose_gate, ProductPurchasePurposeGate)


def test_odoo_decision_submitter_wires_the_purpose_gate() -> None:
    from unittest.mock import MagicMock

    from app.composition.imports import build_odoo_decision_submitter
    from app.connectors.odoo.client import OdooJson2Client
    from app.core.config import Settings
    from app.erp.odoo.adapter import OdooReadOnlyAdapter

    settings = Settings()
    client = OdooJson2Client.from_settings(settings)
    use_case = build_odoo_decision_submitter(
        session=MagicMock(), settings=settings, odoo_client=client, read_adapter=OdooReadOnlyAdapter(client=client)
    )
    assert isinstance(use_case._product_purchase_purpose_gate, ProductPurchasePurposeGate)
