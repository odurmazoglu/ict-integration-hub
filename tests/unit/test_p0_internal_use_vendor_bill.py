"""P0: INTERNAL_USE Vendor Bills for technically unblocked product invoices.

Business case: J062026000000005 (LOGOSOFT, Microsoft 365 for ICT's own use). The
products (393/394) live in a category whose purchase account is meant for RESALE, so an
INTERNAL_USE bill must carry the operator's explicitly selected internal expense account
on every product line -- never the category account.

Covers purpose eligibility, the review-scoped expense account without reclassification,
the decision-time pin (real ``SubmitReviewDecisionUseCase`` + SQLite), execution through
the real ``VendorBillExecutionStrategy`` / builder / writer with only the Odoo boundary
faked, FX/tax/draft preservation, RESALE regression, duplicate prevention, readback,
operator guidance and production wiring.
"""

from __future__ import annotations

import ast
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.application.execution import (
    ExecutionApproval,
    ExecutionMode,
    ExecutionStep,
    ExecutionStepRequest,
    ExecutionStepStatus,
    ExecutionStepType,
    VendorBillExecutionStrategy,
)
from app.application.execution.exceptions import InternalUseExecutionAccountingError
from app.application.execution.internal_use_execution_accounting import InternalUseExecutionAccountingValidator
from app.application.execution.resale_execution_accounting import ResaleExecutionAccountingValidator
from app.application.workbench import (
    LineResolution,
    ReviewDecisionCommand,
    ReviewDecisionType,
    ReviewExecutionEvidence,
    ReviewStatus,
    SubmitReviewDecisionUseCase,
)
from app.application.workbench.accounting_resolution import (
    AccountingResolutionStatus,
    AccountingTreatmentType,
    ReviewAccountingResolution,
    SubmitReviewAccountingResolutionCommand,
)
from app.application.workbench.accounting_resolution_use_cases import SubmitReviewAccountingResolutionUseCase
from app.application.workbench.exceptions import (
    AccountingResolutionEligibilityError,
    AccountingResolutionPurposeUnsupportedError,
    InternalUseDecisionEligibilityError,
    OperatingExpenseMappingAccountInvalidError,
    PurchasePurposeEligibilityError,
    ReviewDecisionError,
)
from app.application.workbench.expense_account_lookup import ExpenseAccountCandidate
from app.application.workbench.internal_use_accounting_pin import (
    INTERNAL_USE_ROLLBACK_GUARD,
    internal_use_accounting_pin_from_data,
    internal_use_accounting_pin_to_data,
)
from app.application.workbench.internal_use_decision_gate import InternalUseDecisionGate
from app.application.workbench.operator_guidance import (
    GuidanceInput,
    OperatorGuidanceFacts,
    OperatorNextAction,
    build_operator_guidance,
)
from app.application.workbench.purchase_purpose import PurchasePurpose
from app.application.workbench.resale_decision_gate import ResaleDecisionGate
from app.application.workbench.vendor_bill_readback import (
    InternalUseReadbackStatus,
    VendorBillInternalUseReadbackVerifier,
    VendorBillLineVerification,
    compare_internal_use_lines,
)
from app.application.workflow import ManualReviewReasonCode, WorkflowType
from app.billing import (
    ValidatedInternalUseLineAccount,
    VendorBillBuilder,
    VendorBillLine,
    issue_validated_internal_use_line_account,
    issue_validated_resale_line_account,
)
from app.billing.exceptions import VendorBillBuildError
from app.core.runtime_checks import PRODUCTION_APPROVAL_ACK
from app.db.base import Base
from app.erp.write import AccountMoveRepository, OdooVendorBillWritePolicy, OdooVendorBillWriter
from app.matching import InvoiceProductMatchResult, ProductMatchStatus
from app.models.execution_source_invoice_evidence import ExecutionSourceInvoiceEvidence
from app.models.workbench_review_accounting_resolution import WorkbenchReviewAccountingResolution
from app.models.workbench_review_decision import WorkbenchReviewDecision
from app.models.workbench_review_execution_evidence import WorkbenchReviewExecutionEvidence
from app.models.workbench_review_item import WorkbenchReviewItem
from app.models.workbench_review_purchase_purpose_resolution import WorkbenchReviewPurchasePurposeResolution
from app.persistence import (
    SqlAlchemyReviewAccountingResolutionRepository,
    SqlAlchemyReviewExecutionEvidenceReader,
    SqlAlchemyReviewPurchasePurposeResolutionRepository,
    SqlAlchemyReviewRepository,
    SqlAlchemyUnitOfWork,
)
from app.persistence.execution_source_invoice_reader import SqlAlchemyExecutionSourceInvoiceReader
from tests.unit.fx_rate_support import MATCHING_FX_RATE_POLICY, TEST_UBL_RATE, with_ubl_rate
from tests.unit.resale_execution_support import (
    NON_INTERNAL_USE_ACCOUNTING_CHECK,
    NON_RESALE_ACCOUNTING_CHECK,
    TWO_DECIMAL_CURRENCY_READER,
)
from tests.unit.source_tax_support import ALLOW_SOURCE_TAX_SAFETY_CHECK
from tests.unit.test_p0_prod_18e_1b_resale_workflow_wiring import (
    CHILD_CATEGORY_ID,
    COMPANY_ID,
    LOGOSOFT_SELLER_CODE,
    PRODUCT_A,
    PRODUCT_B,
    REVIEW_ID,
    SOURCE_INVOICE_ID,
    _discovery,
    _FakeProductAccountResolver,
    _FakeReviewReader,
    _FakeSourceReader,
    _FakeUnitOfWork,
    _invoice,
    _line,
    _partner,
    _product_line,
    _review,
    _taxes,
)
from tests.unit.test_p0_prod_18e_1b_resale_workflow_wiring import (
    _FakePurposeWriter as _PurposeWriter,
)
from tests.unit.test_p0_prod_18e_1b_resale_workflow_wiring import (
    _purpose_command as _purpose,
)

#: Arbitrary internal expense account (prod's 770000 is id 247; nothing depends on it).
INTERNAL_ACCOUNT = ExpenseAccountCandidate(
    id=247, code="770000", name="General Administrative Expenses", account_type="expense"
)
#: The RESALE category account that must never reach an INTERNAL_USE line (prod: 294 / 153200).
CATEGORY_ACCOUNT_ID = 294
EXPENSE_CATEGORY = "SOFTWARE_SUBSCRIPTION_INTERNAL"
DECISION_VERSION = 2
MOVE_ID = 9100
CURRENCY_ID = 2
UOM_ID = 1
SECOND_SELLER_CODE = "CFQ7TTC0LDPB:0001"


# =========================================================================== fixtures / builders


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
    factory = sessionmaker(bind=engine)
    with factory() as db_session:
        yield db_session


class _FakeAccounts:
    """``ExpenseAccountCandidateReader.find_eligible_by_id``: only listed accounts are eligible."""

    def __init__(self, *accounts: ExpenseAccountCandidate, error: Exception | None = None) -> None:
        self.accounts = {account.id: account for account in accounts}
        self.error = error
        self.calls: list[tuple[int, int]] = []

    def find_eligible_by_id(self, *, company_id: int, account_id: int) -> ExpenseAccountCandidate | None:
        self.calls.append((company_id, account_id))
        if self.error is not None:
            raise self.error
        return self.accounts.get(account_id)


class _RecordingAccountMoveClient:
    """The Odoo JSON-2 boundary of ``AccountMoveRepository``; records every call."""

    def __init__(self, *, existing: list[dict[str, Any]] | None = None) -> None:
        self._existing = existing or []
        self.search_models: list[str] = []
        self.searched_domains: list[Any] = []
        self.created: list[dict[str, Any]] = []

    async def search_read(self, *, model, domain, fields, limit=20, offset=0):
        self.search_models.append(model)
        if model == "account.move":
            self.searched_domains.append(domain)
            return list(self._existing)
        if model == "res.currency":
            return [{"id": CURRENCY_ID, "name": "USD", "active": True}]
        if model == "product.product":
            ids = next(clause[2] for clause in domain if clause[0] == "id")
            return [{"id": product_id, "uom_id": [UOM_ID, "Units"]} for product_id in ids]
        raise AssertionError(f"unexpected model {model}")

    async def create_account_move(self, payload):
        self.created.append(payload)
        return MOVE_ID

    @property
    def odoo_calls(self) -> int:
        return len(self.search_models) + len(self.created)


def _two_line_invoice():
    return _invoice(
        [_line("1", seller_item_code=LOGOSOFT_SELLER_CODE), _line("2", seller_item_code=SECOND_SELLER_CODE)]
    )


def _seed_unblocked_review(session: Session, *, invoice=None, product_results=None) -> None:
    """A technically unblocked review: no reasons, VENDOR_BILL, every line product-matched."""

    invoice = invoice or _two_line_invoice()
    results = product_results or [
        _product_line(line.line_number, ProductMatchStatus.MATCHED, product_id=product_id)
        for line, product_id in zip(invoice.lines, (PRODUCT_A, PRODUCT_B), strict=False)
    ]
    evidence = ReviewExecutionEvidence(
        review_id=REVIEW_ID,
        company_id=COMPANY_ID,
        review_version=1,
        source_invoice_id=SOURCE_INVOICE_ID,
        invoice=invoice,
        partner_match=_partner(),
        product_match=InvoiceProductMatchResult(line_results=tuple(results)),
        tax_match=_taxes(invoice),
    )
    SqlAlchemyReviewRepository(session).create_review_item_with_execution_evidence(
        _review(), company_id=COMPANY_ID, idempotency_key="review-key-internal", evidence=evidence
    )


def _record_purpose(session: Session, purpose: PurchasePurpose, *, review_version: int = 1) -> None:
    from app.application.workbench.purchase_purpose import PurchasePurposeResolution

    SqlAlchemyReviewPurchasePurposeResolutionRepository(session).create_purchase_purpose_resolution(
        PurchasePurposeResolution(
            review_id=REVIEW_ID,
            company_id=COMPANY_ID,
            review_version=review_version,
            source_invoice_id=SOURCE_INVOICE_ID,
            purchase_purpose=purpose,
            approved_by="operator",
        )
    )


def _record_expense_account(
    session: Session,
    *,
    account_id: int = INTERNAL_ACCOUNT.id,
    review_version: int = 1,
    treatment: AccountingTreatmentType = AccountingTreatmentType.EXPENSE_ACCOUNT,
) -> ReviewAccountingResolution:
    fields: dict[str, Any] = (
        {"expense_account_id": account_id, "expense_category": EXPENSE_CATEGORY}
        if treatment is AccountingTreatmentType.EXPENSE_ACCOUNT
        else {"asset_account_id": account_id, "depreciation_model_id": 5}
    )
    return SqlAlchemyReviewAccountingResolutionRepository(session).create_accounting_resolution(
        ReviewAccountingResolution(
            review_id=REVIEW_ID,
            company_id=COMPANY_ID,
            review_version=review_version,
            treatment_type=treatment,
            approved_by="operator",
            **fields,
        )
    )


def _decision(*line_resolutions: LineResolution, key: str = "decision:internal-use") -> ReviewDecisionCommand:
    return ReviewDecisionCommand(
        review_id=REVIEW_ID,
        company_id=COMPANY_ID,
        expected_version=1,
        decision=ReviewDecisionType.SELECT_WORKFLOW,
        selected_workflow=WorkflowType.VENDOR_BILL,
        line_resolutions=line_resolutions,
        decided_by="finance.user",
        idempotency_key=key,
    )


def _gate(session: Session, accounts: _FakeAccounts | None = None) -> InternalUseDecisionGate:
    return InternalUseDecisionGate(
        purpose_reader=SqlAlchemyReviewPurchasePurposeResolutionRepository(session),
        accounting_resolution_reader=SqlAlchemyReviewAccountingResolutionRepository(session),
        expense_account_reader=accounts or _FakeAccounts(INTERNAL_ACCOUNT),
    )


def _decision_use_case(
    session: Session,
    accounts: _FakeAccounts | None = None,
    *,
    resale_resolver: _FakeProductAccountResolver | None = None,
) -> SubmitReviewDecisionUseCase:
    return SubmitReviewDecisionUseCase(
        source_tax_safety_check=ALLOW_SOURCE_TAX_SAFETY_CHECK,
        review_decision_writer=SqlAlchemyReviewRepository(session),
        unit_of_work=SqlAlchemyUnitOfWork(session),
        execution_evidence_reader=SqlAlchemyReviewExecutionEvidenceReader(session),
        resale_decision_gate=ResaleDecisionGate(
            purpose_reader=SqlAlchemyReviewPurchasePurposeResolutionRepository(session),
            product_account_resolver=resale_resolver or _FakeProductAccountResolver({}),
            approved_category_ids=frozenset({CHILD_CATEGORY_ID}),
        ),
        internal_use_decision_gate=_gate(session, accounts),
    )


def _stored_evidence(session: Session) -> ExecutionSourceInvoiceEvidence:
    return session.query(ExecutionSourceInvoiceEvidence).one()


def _accept_internal_use(session: Session, accounts: _FakeAccounts | None = None) -> None:
    _seed_unblocked_review(session)
    _record_purpose(session, PurchasePurpose.INTERNAL_USE)
    _record_expense_account(session)
    _decision_use_case(session, accounts).execute(_decision())


def _validator(session: Session, accounts: _FakeAccounts | None = None) -> InternalUseExecutionAccountingValidator:
    return InternalUseExecutionAccountingValidator(
        pin_reader=SqlAlchemyExecutionSourceInvoiceReader(session),
        purpose_reader=SqlAlchemyReviewPurchasePurposeResolutionRepository(session),
        expense_account_reader=accounts or _FakeAccounts(INTERNAL_ACCOUNT),
    )


def _strategy(
    session: Session,
    client: _RecordingAccountMoveClient,
    *,
    internal_use_check=None,
    resale_check=None,
) -> VendorBillExecutionStrategy:
    return VendorBillExecutionStrategy(
        source_tax_safety_check=ALLOW_SOURCE_TAX_SAFETY_CHECK,
        # USD fixture: the invoice states the authoritative UBL rate (P1 FX guard passes).
        source_invoice_reader=with_ubl_rate(SqlAlchemyExecutionSourceInvoiceReader(session)),
        vendor_bill_builder=VendorBillBuilder(),
        vendor_bill_writer=OdooVendorBillWriter(
            repository=AccountMoveRepository(client=client),
            policy=OdooVendorBillWritePolicy(
                production_operations_enabled=True, production_approval_ack=PRODUCTION_APPROVAL_ACK
            ),
        ),
        resale_accounting_check=resale_check or NON_RESALE_ACCOUNTING_CHECK,
        internal_use_accounting_check=internal_use_check if internal_use_check is not None else _validator(session),
        currency_reader=TWO_DECIMAL_CURRENCY_READER,
        fx_rate_policy=MATCHING_FX_RATE_POLICY,
    )


def _request(mode: ExecutionMode = ExecutionMode.EXECUTE) -> ExecutionStepRequest:
    return ExecutionStepRequest(
        execution_id="execution-internal-use",
        review_id=REVIEW_ID,
        company_id=COMPANY_ID,
        decision_version=DECISION_VERSION,
        mode=mode,
        step=ExecutionStep(
            step_key=f"{REVIEW_ID}:{DECISION_VERSION}:vendor_bill:workflow",
            step_type=ExecutionStepType.VENDOR_BILL,
            allocation_keys=(),
            sequence=1,
            execute_supported=True,
        ),
        approval=ExecutionApproval(approved_by="finance.lead") if mode is ExecutionMode.EXECUTE else None,
    )


def _line_payloads(client: _RecordingAccountMoveClient) -> list[dict[str, Any]]:
    assert len(client.created) == 1
    return [entry[2] for entry in client.created[0]["invoice_line_ids"]]


def _source(session: Session):
    return SqlAlchemyExecutionSourceInvoiceReader(session).get_source_invoice(
        review_id=REVIEW_ID, company_id=COMPANY_ID, decision_version=DECISION_VERSION
    )


# =========================================================================== purpose eligibility


def _purpose_use_case(invoice, *reasons, workflow: WorkflowType = WorkflowType.VENDOR_BILL):
    from app.application.workbench.purchase_purpose_use_cases import SubmitPurchasePurposeUseCase

    writer = _PurposeWriter()
    review = replace(_review(*reasons), workflow=workflow)
    use_case = SubmitPurchasePurposeUseCase(
        review_reader=_FakeReviewReader(review),
        source_invoice_reader=_FakeSourceReader(invoice),
        purpose_writer=writer,
        unit_of_work=_FakeUnitOfWork(),
    )
    return use_case, writer


def test_internal_use_purpose_accepted_on_unblocked_product_review() -> None:
    use_case, writer = _purpose_use_case(_two_line_invoice())

    result = use_case.execute(_purpose(PurchasePurpose.INTERNAL_USE))

    assert result.purchase_purpose is PurchasePurpose.INTERNAL_USE
    assert writer.rows[1].purchase_purpose is PurchasePurpose.INTERNAL_USE


def test_resale_purpose_still_accepted_on_unblocked_product_review() -> None:
    use_case, writer = _purpose_use_case(_two_line_invoice())
    assert use_case.execute(_purpose(PurchasePurpose.RESALE)).purchase_purpose is PurchasePurpose.RESALE


@pytest.mark.parametrize("purpose", [PurchasePurpose.OTHER_OPERATING_EXPENSE, PurchasePurpose.CUSTOMER_PROJECT])
def test_other_purposes_still_rejected_on_unblocked_review(purpose: PurchasePurpose) -> None:
    use_case, writer = _purpose_use_case(_two_line_invoice())
    with pytest.raises(PurchasePurposeEligibilityError, match="operating-expense-shaped"):
        use_case.execute(_purpose(purpose))
    assert writer.rows == {}


def test_internal_use_rejected_on_unblocked_identifier_free_invoice() -> None:
    use_case, writer = _purpose_use_case(_invoice([_line("1")]))
    with pytest.raises(PurchasePurposeEligibilityError, match="INTERNAL_USE requires a product-shaped"):
        use_case.execute(_purpose(PurchasePurpose.INTERNAL_USE))
    assert writer.rows == {}


@pytest.mark.parametrize(
    ("reasons", "workflow"),
    [
        ((ManualReviewReasonCode.PRODUCT_NOT_FOUND,), WorkflowType.MANUAL_REVIEW),
        ((ManualReviewReasonCode.SUPPLIER_NOT_FOUND,), WorkflowType.MANUAL_REVIEW),
        ((), WorkflowType.MANUAL_REVIEW),
    ],
)
def test_internal_use_rejected_while_review_is_blocked(reasons, workflow) -> None:
    use_case, writer = _purpose_use_case(_two_line_invoice(), *reasons, workflow=workflow)
    with pytest.raises(PurchasePurposeEligibilityError):
        use_case.execute(_purpose(PurchasePurpose.INTERNAL_USE))
    assert writer.rows == {}


# =========================================================================== accounting resolution


class _NoReclassifier:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, command):
        self.calls += 1
        raise AssertionError("an unblocked review must never be reclassified")


def _accounting_use_case(session: Session, *, accounts: _FakeAccounts | None = None):
    reclassifier = _NoReclassifier()
    use_case = SubmitReviewAccountingResolutionUseCase(
        review_reader=SqlAlchemyReviewRepository(session),
        purpose_reader=SqlAlchemyReviewPurchasePurposeResolutionRepository(session),
        expense_account_reader=accounts or _FakeAccounts(INTERNAL_ACCOUNT),
        accounting_resolution_writer=SqlAlchemyReviewAccountingResolutionRepository(session),
        reclassifier=reclassifier,
        unit_of_work=SqlAlchemyUnitOfWork(session),
    )
    return use_case, reclassifier


def _expense_command(
    account_id: int = INTERNAL_ACCOUNT.id, **overrides: Any
) -> SubmitReviewAccountingResolutionCommand:
    values: dict[str, Any] = {
        "review_id": REVIEW_ID,
        "company_id": COMPANY_ID,
        "expected_version": 1,
        "treatment_type": AccountingTreatmentType.EXPENSE_ACCOUNT,
        "approved_by": "operator",
        "expense_account_id": account_id,
        "expense_category": EXPENSE_CATEGORY,
    }
    values.update(overrides)
    return SubmitReviewAccountingResolutionCommand(**values)


async def test_unblocked_internal_use_expense_account_recorded_without_reclassification(session: Session) -> None:
    _seed_unblocked_review(session)
    _record_purpose(session, PurchasePurpose.INTERNAL_USE)
    use_case, reclassifier = _accounting_use_case(session)

    result = await use_case.execute(_expense_command())

    assert result.status is AccountingResolutionStatus.RESOLVED
    assert (result.previous_version, result.current_version) == (1, 1)
    assert result.reclassified is False and reclassifier.calls == 0
    assert result.expense_account_id == INTERNAL_ACCOUNT.id
    review = session.query(WorkbenchReviewItem).filter_by(review_id=REVIEW_ID).one()
    assert (review.version, review.status) == (1, ReviewStatus.PENDING_REVIEW.value)

    retry = await use_case.execute(_expense_command())
    assert retry.already_applied is True
    assert session.query(WorkbenchReviewAccountingResolution).count() == 1


async def test_unblocked_expense_account_requires_internal_use_purpose(session: Session) -> None:
    _seed_unblocked_review(session)
    _record_purpose(session, PurchasePurpose.RESALE)
    use_case, _ = _accounting_use_case(session)
    with pytest.raises(AccountingResolutionPurposeUnsupportedError):
        await use_case.execute(_expense_command())
    assert session.query(WorkbenchReviewAccountingResolution).count() == 0


async def test_unblocked_review_rejects_fixed_asset_treatment(session: Session) -> None:
    _seed_unblocked_review(session)
    _record_purpose(session, PurchasePurpose.INTERNAL_USE)
    use_case, _ = _accounting_use_case(session)
    command = _expense_command(
        expense_account_id=None,
        expense_category=None,
        treatment_type=AccountingTreatmentType.CAPITALIZE_FIXED_ASSET,
        asset_account_id=255,
        depreciation_model_id=5,
    )
    with pytest.raises(AccountingResolutionPurposeUnsupportedError, match="expense_account treatment"):
        await use_case.execute(command)
    assert session.query(WorkbenchReviewAccountingResolution).count() == 0


async def test_unblocked_expense_account_must_be_an_eligible_expense_account(session: Session) -> None:
    _seed_unblocked_review(session)
    _record_purpose(session, PurchasePurpose.INTERNAL_USE)
    use_case, _ = _accounting_use_case(session, accounts=_FakeAccounts(INTERNAL_ACCOUNT))
    # The RESALE category account (asset_current) is not an eligible expense account.
    with pytest.raises(OperatingExpenseMappingAccountInvalidError):
        await use_case.execute(_expense_command(CATEGORY_ACCOUNT_ID))
    assert session.query(WorkbenchReviewAccountingResolution).count() == 0


async def test_blocked_non_expense_review_still_rejects_accounting_resolution(session: Session) -> None:
    invoice = _two_line_invoice()
    evidence_results = [
        _product_line("1", ProductMatchStatus.NOT_FOUND),
        _product_line("2", ProductMatchStatus.NOT_FOUND),
    ]
    _seed_unblocked_review(session, invoice=invoice, product_results=evidence_results)
    session.query(WorkbenchReviewItem).update({"review_reasons": [{"code": "PRODUCT_NOT_FOUND", "message": "x"}]})
    session.query(WorkbenchReviewItem).update({"workflow": WorkflowType.MANUAL_REVIEW.value})
    _record_purpose(session, PurchasePurpose.INTERNAL_USE)
    use_case, _ = _accounting_use_case(session)
    with pytest.raises(AccountingResolutionEligibilityError):
        await use_case.execute(_expense_command())


# =========================================================================== decision gate + pin


def test_internal_use_decision_pins_the_selected_expense_account(session: Session) -> None:
    accounts = _FakeAccounts(INTERNAL_ACCOUNT)
    _accept_internal_use(session, accounts)

    stored = _stored_evidence(session)
    assert stored.resale_accounting_pin == INTERNAL_USE_ROLLBACK_GUARD
    pin = internal_use_accounting_pin_from_data(stored.internal_use_accounting_pin)
    assert pin.purchase_purpose is PurchasePurpose.INTERNAL_USE
    assert pin.review_version == 1
    assert (pin.expense_account_id, pin.expense_account_code, pin.expense_account_type) == (247, "770000", "expense")
    assert pin.expense_category == EXPENSE_CATEGORY
    assert [(line.line_number, line.product_id) for line in pin.lines] == [("1", PRODUCT_A), ("2", PRODUCT_B)]
    assert accounts.calls == [(COMPANY_ID, INTERNAL_ACCOUNT.id)]
    assert session.query(WorkbenchReviewItem).filter_by(review_id=REVIEW_ID).one().version == DECISION_VERSION


def _assert_rejected(session: Session, use_case, command, fragment: str) -> None:
    with pytest.raises(InternalUseDecisionEligibilityError, match=fragment):
        use_case.execute(command)
    assert session.query(WorkbenchReviewDecision).count() == 0
    assert session.query(ExecutionSourceInvoiceEvidence).count() == 0
    assert session.query(WorkbenchReviewItem).filter_by(review_id=REVIEW_ID).one().version == 1


def test_internal_use_decision_without_expense_account_is_rejected(session: Session) -> None:
    _seed_unblocked_review(session)
    _record_purpose(session, PurchasePurpose.INTERNAL_USE)
    _assert_rejected(session, _decision_use_case(session), _decision(), "explicitly selected internal expense account")


def test_internal_use_decision_with_resolution_for_another_version_is_rejected(session: Session) -> None:
    _seed_unblocked_review(session)
    _record_purpose(session, PurchasePurpose.INTERNAL_USE)
    _record_expense_account(session, review_version=7)
    _assert_rejected(session, _decision_use_case(session), _decision(), "explicitly selected internal expense account")


def test_internal_use_decision_rejects_fixed_asset_resolution(session: Session) -> None:
    _seed_unblocked_review(session)
    _record_purpose(session, PurchasePurpose.INTERNAL_USE)
    _record_expense_account(session, treatment=AccountingTreatmentType.CAPITALIZE_FIXED_ASSET)
    _assert_rejected(session, _decision_use_case(session), _decision(), "expense_account accounting treatment")


@pytest.mark.parametrize(
    "accounts",
    [_FakeAccounts(), _FakeAccounts(error=RuntimeError("odoo down"))],
    ids=["no-longer-eligible", "read-failure"],
)
def test_internal_use_decision_rejects_unverifiable_account(session: Session, accounts: _FakeAccounts) -> None:
    _seed_unblocked_review(session)
    _record_purpose(session, PurchasePurpose.INTERNAL_USE)
    _record_expense_account(session)
    _assert_rejected(session, _decision_use_case(session, accounts), _decision(), "internal expense account")


def test_internal_use_decision_rejects_unmatched_product_line(session: Session) -> None:
    _seed_unblocked_review(
        session,
        product_results=[
            _product_line("1", ProductMatchStatus.MATCHED, product_id=PRODUCT_A),
            _product_line("2", ProductMatchStatus.NOT_FOUND),
        ],
    )
    _record_purpose(session, PurchasePurpose.INTERNAL_USE)
    _record_expense_account(session)
    with pytest.raises(ReviewDecisionError):
        _decision_use_case(session).execute(_decision())
    assert session.query(ExecutionSourceInvoiceEvidence).count() == 0


def test_internal_use_gate_rejects_account_only_lines(session: Session) -> None:
    _seed_unblocked_review(session)
    _record_purpose(session, PurchasePurpose.INTERNAL_USE)
    _record_expense_account(session)
    command = _decision(LineResolution(line_number="2", account_only=True, expense_account_id=INTERNAL_ACCOUNT.id))
    evidence = SqlAlchemyReviewExecutionEvidenceReader(session).get_evidence(
        review_id=REVIEW_ID, company_id=COMPANY_ID, expected_version=1
    )
    with pytest.raises(InternalUseDecisionEligibilityError, match="account-only"):
        _gate(session).enforce(command, evidence)


def test_gate_is_inert_for_identifier_free_internal_use_and_other_purposes(session: Session) -> None:
    _seed_unblocked_review(session)
    evidence = SqlAlchemyReviewExecutionEvidenceReader(session).get_evidence(
        review_id=REVIEW_ID, company_id=COMPANY_ID, expected_version=1
    )
    accounts = _FakeAccounts(INTERNAL_ACCOUNT)
    assert _gate(session, accounts).enforce(_decision(), evidence) is None  # no purpose at all
    _record_purpose(session, PurchasePurpose.INTERNAL_USE)
    identifier_free = replace(evidence, invoice=_invoice([_line("1"), _line("2")]))
    # Existing operating-expense / fixed-asset path owns identifier-free INTERNAL_USE.
    assert _gate(session, accounts).enforce(_decision(), identifier_free) is None
    assert accounts.calls == []


def test_resale_decision_is_unchanged_by_the_internal_use_gate(session: Session) -> None:
    _seed_unblocked_review(session)
    _record_purpose(session, PurchasePurpose.RESALE)
    _record_expense_account(session)  # even a stray resolution never turns RESALE into INTERNAL_USE
    resolver = _FakeProductAccountResolver({PRODUCT_A: _discovery(PRODUCT_A), PRODUCT_B: _discovery(PRODUCT_B)})
    accounts = _FakeAccounts(INTERNAL_ACCOUNT)

    _decision_use_case(session, accounts, resale_resolver=resolver).execute(_decision())

    stored = _stored_evidence(session)
    assert stored.resale_accounting_pin is not None
    assert stored.internal_use_accounting_pin is None
    assert accounts.calls == []


def test_decision_replay_never_repins_and_creates_no_duplicate(session: Session) -> None:
    _accept_internal_use(session)
    first = _stored_evidence(session).internal_use_accounting_pin
    # Same idempotency key again: the accepted decision is returned, nothing is re-pinned.
    accounts = _FakeAccounts(INTERNAL_ACCOUNT)
    replay = _decision_use_case(session, accounts).execute(_decision())

    assert replay.version == DECISION_VERSION
    assert session.query(WorkbenchReviewDecision).count() == 1
    assert session.query(ExecutionSourceInvoiceEvidence).count() == 1
    assert _stored_evidence(session).internal_use_accounting_pin == first
    assert accounts.calls == []


def test_pin_round_trip_is_strict() -> None:
    data = {
        "schema_version": 1,
        "purchase_purpose": "internal_use",
        "review_version": 1,
        "accounting_resolution_id": 3,
        "account_source": "review_accounting_resolution",
        "expense_account_id": 247,
        "expense_account_code": "770000",
        "expense_account_name": "General Administrative Expenses",
        "expense_account_type": "expense",
        "expense_category": EXPENSE_CATEGORY,
        "lines": [{"line_number": "1", "product_id": PRODUCT_A}],
    }
    assert internal_use_accounting_pin_to_data(internal_use_accounting_pin_from_data(data)) == data
    from app.application.workbench.exceptions import WorkbenchContractError

    for broken in (
        {**data, "purchase_purpose": "resale"},
        {**data, "extra": 1},
        {**data, "expense_account_id": "247"},
        {**data, "lines": []},
        {**data, "lines": [{"line_number": "1", "product_id": PRODUCT_A, "account_id": 294}]},
    ):
        with pytest.raises(WorkbenchContractError):
            internal_use_accounting_pin_from_data(broken)


# =========================================================================== execution


def test_execution_sends_products_on_the_pinned_internal_account(session: Session) -> None:
    _accept_internal_use(session)
    client = _RecordingAccountMoveClient()

    result = _strategy(session, client).execute(_request())

    assert result.status is ExecutionStepStatus.EXECUTED
    lines = _line_payloads(client)
    assert [(line["product_id"], line["account_id"]) for line in lines] == [
        (PRODUCT_A, INTERNAL_ACCOUNT.id),
        (PRODUCT_B, INTERNAL_ACCOUNT.id),
    ]
    assert all(line["account_id"] != CATEGORY_ACCOUNT_ID for line in lines)
    # Tax and draft-only behaviour preserved: same tax ids, never posted.
    assert all(line["tax_ids"] == ((6, 0, (401,)),) for line in lines)
    payload = client.created[0]
    assert payload["move_type"] == "in_invoice"
    assert "state" not in payload and "action_post" not in str(payload)
    # Authoritative UBL FX rate preserved (company -> bill currency, the inverse).
    assert payload["invoice_currency_rate"] == pytest.approx(float(1 / TEST_UBL_RATE))


def test_execution_without_pin_fails_closed_before_any_odoo_call(session: Session) -> None:
    _accept_internal_use(session)
    session.query(ExecutionSourceInvoiceEvidence).update({"internal_use_accounting_pin": None})
    client = _RecordingAccountMoveClient()

    result = _strategy(session, client).execute(_request())

    assert result.status is ExecutionStepStatus.FAILED
    assert result.error_code == "internal_use_execution_accounting_error"
    assert "category" in (result.message or "")
    assert client.odoo_calls == 0


@pytest.mark.parametrize(
    ("accounts", "fragment"),
    [
        (_FakeAccounts(), "no longer an eligible"),
        (_FakeAccounts(replace(INTERNAL_ACCOUNT, code="770999")), "account_code"),
        (_FakeAccounts(replace(INTERNAL_ACCOUNT, account_type="asset_current")), "account_type"),
        (_FakeAccounts(error=RuntimeError("odoo down")), "could not be read safely"),
    ],
    ids=["ineligible", "code-drift", "type-drift", "read-failure"],
)
def test_execution_blocks_on_account_drift(session: Session, accounts: _FakeAccounts, fragment: str) -> None:
    _accept_internal_use(session)
    client = _RecordingAccountMoveClient()

    result = _strategy(session, client, internal_use_check=_validator(session, accounts)).execute(_request())

    assert result.status is ExecutionStepStatus.FAILED
    assert result.error_code == "internal_use_execution_accounting_error"
    assert fragment in (result.message or "")
    assert client.odoo_calls == 0


def test_execution_pin_for_non_internal_use_decision_fails_closed(session: Session) -> None:
    _accept_internal_use(session)
    session.query(WorkbenchReviewPurchasePurposeResolution).update({"purchase_purpose": "resale"})
    with pytest.raises(InternalUseExecutionAccountingError, match="not accepted under an INTERNAL_USE purpose"):
        _validator(session).validate(_source(session))


def test_validator_is_inert_for_decisions_without_internal_use(session: Session) -> None:
    _seed_unblocked_review(session)
    _decision_use_case(session).execute(_decision())
    accounts = _FakeAccounts(INTERNAL_ACCOUNT)
    assert _validator(session, accounts).validate(_source(session)) is None
    assert accounts.calls == []


def test_retry_reuses_the_same_write_idempotency_and_finds_the_existing_draft(session: Session) -> None:
    """Duplicate prevention: a retry looks the draft up first and never creates a second one."""

    _accept_internal_use(session)
    first = _RecordingAccountMoveClient()
    assert _strategy(session, first).execute(_request()).status is ExecutionStepStatus.EXECUTED
    origin = first.created[0]["invoice_origin"]

    existing = {
        "id": MOVE_ID,
        "invoice_origin": origin,
        "partner_id": [101, "LOGOSOFT"],
        "company_id": [COMPANY_ID, "ICT"],
        "move_type": "in_invoice",
        "state": "draft",
    }
    retry = _RecordingAccountMoveClient(existing=[existing])
    result = _strategy(session, retry).execute(_request())

    assert result.status is ExecutionStepStatus.EXECUTED
    assert retry.created == []
    assert any(origin in str(domain) for domain in retry.searched_domains)


# =========================================================================== builder / DTO invariants


def test_product_line_cannot_carry_an_unsealed_internal_use_account() -> None:
    forged = ValidatedInternalUseLineAccount(line_number="1", product_id=PRODUCT_A, account_id=INTERNAL_ACCOUNT.id)
    with pytest.raises(ValueError, match="issued by INTERNAL_USE"):
        VendorBillLine(
            product_id=PRODUCT_A,
            account_id=INTERNAL_ACCOUNT.id,
            internal_use_account=forged,
            quantity=Decimal("1"),
            unit_price=Decimal("1"),
        )
    with pytest.raises(ValueError, match="exactly one of product_id or account_id"):
        VendorBillLine(
            product_id=PRODUCT_A, account_id=CATEGORY_ACCOUNT_ID, quantity=Decimal("1"), unit_price=Decimal("1")
        )


def test_builder_rejects_incomplete_or_mixed_internal_use_accounts(session: Session) -> None:
    _accept_internal_use(session)
    source = _source(session)
    accounts = _validator(session).validate(source)
    assert accounts is not None
    builder = VendorBillBuilder()

    def build(**kwargs):
        return builder.build(
            source.invoice,
            source.partner_match,
            source.product_match,
            source.tax_match,
            company_id=COMPANY_ID,
            **kwargs,
        )

    def build_errors(**kwargs) -> str:
        with pytest.raises(VendorBillBuildError) as excinfo:
            build(**kwargs)
        return excinfo.value.safe_message

    bill = build(validated_internal_use_accounts=accounts)
    assert [line.account_id for line in bill.invoice_lines] == [INTERNAL_ACCOUNT.id, INTERNAL_ACCOUNT.id]
    assert "cover exactly every invoice line" in build_errors(validated_internal_use_accounts={"1": accounts["1"]})
    resale = {
        number: issue_validated_resale_line_account(line_number=number, product_id=account.product_id, account_id=1)
        for number, account in accounts.items()
    }
    assert "RESALE" in build_errors(validated_internal_use_accounts=accounts, validated_resale_accounts=resale)
    swapped = {
        "1": issue_validated_internal_use_line_account(line_number="1", product_id=PRODUCT_B, account_id=247),
        "2": accounts["2"],
    }
    assert "different product" in build_errors(validated_internal_use_accounts=swapped)


def test_seal_is_issued_only_by_the_internal_use_validator() -> None:
    callers = sorted(
        str(path)
        for path in Path("app").rglob("*.py")
        if "issue_validated_internal_use_line_account(" in path.read_text(encoding="utf-8") and path.name != "dto.py"
    )
    assert callers == ["app/application/execution/internal_use_execution_accounting.py"]


def test_internal_use_modules_never_read_category_or_product_accounts() -> None:
    for module in (
        "app/application/workbench/internal_use_decision_gate.py",
        "app/application/execution/internal_use_execution_accounting.py",
    ):
        tree = ast.parse(Path(module).read_text(encoding="utf-8"))
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        names |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        assert not names & {
            "ProductPurchaseAccountResolver",
            "GetProductPurchaseAccountQuery",
            "pre_fiscal_position_account",
        }


# =========================================================================== readback


def _odoo_line(line_id: int, product_id: int | None, account_id: int | None) -> VendorBillLineVerification:
    return VendorBillLineVerification(
        line_id=line_id,
        move_id=MOVE_ID,
        account_id=account_id,
        product_id=product_id,
        quantity=Decimal("1"),
        price_unit=Decimal("50"),
        tax_ids=(401,),
        price_subtotal=Decimal("50"),
        price_total=Decimal("60"),
    )


def test_readback_verifies_every_line_on_the_pinned_account(session: Session) -> None:
    _accept_internal_use(session)
    verifier = VendorBillInternalUseReadbackVerifier(pin_reader=SqlAlchemyExecutionSourceInvoiceReader(session))

    ok = verifier.verify(
        review_id=REVIEW_ID,
        company_id=COMPANY_ID,
        decision_version=DECISION_VERSION,
        lines=(_odoo_line(1, PRODUCT_A, 247), _odoo_line(2, PRODUCT_B, 247)),
    )
    assert ok is not None and ok.status is InternalUseReadbackStatus.VERIFIED
    assert ok.expected_account_code == "770000"

    bad = verifier.verify(
        review_id=REVIEW_ID,
        company_id=COMPANY_ID,
        decision_version=DECISION_VERSION,
        lines=(_odoo_line(1, PRODUCT_A, CATEGORY_ACCOUNT_ID), _odoo_line(2, PRODUCT_B, 247)),
    )
    assert bad is not None and bad.status is InternalUseReadbackStatus.MISMATCH
    assert any(str(CATEGORY_ACCOUNT_ID) in mismatch for mismatch in bad.mismatches)


def test_readback_detects_missing_or_foreign_products() -> None:
    pin = internal_use_accounting_pin_from_data(
        {
            "schema_version": 1,
            "purchase_purpose": "internal_use",
            "review_version": 1,
            "accounting_resolution_id": 3,
            "account_source": "review_accounting_resolution",
            "expense_account_id": 247,
            "expense_account_code": "770000",
            "expense_account_name": "x",
            "expense_account_type": "expense",
            "expense_category": EXPENSE_CATEGORY,
            "lines": [{"line_number": "1", "product_id": PRODUCT_A}, {"line_number": "2", "product_id": PRODUCT_B}],
        }
    )
    _, mismatches = compare_internal_use_lines(pin, (_odoo_line(1, PRODUCT_A, 247),))
    assert "line count 1 != pinned 2" in mismatches
    _, mismatches = compare_internal_use_lines(pin, (_odoo_line(1, PRODUCT_A, 247), _odoo_line(2, 999, 247)))
    assert any("not pinned" in mismatch for mismatch in mismatches)


def test_readback_is_absent_for_bills_without_a_pin(session: Session) -> None:
    _seed_unblocked_review(session)
    _decision_use_case(session).execute(_decision())
    verifier = VendorBillInternalUseReadbackVerifier(pin_reader=SqlAlchemyExecutionSourceInvoiceReader(session))
    assert verifier.verify(review_id=REVIEW_ID, company_id=COMPANY_ID, decision_version=2, lines=()) is None


# =========================================================================== operator guidance


def _guidance(facts: OperatorGuidanceFacts) -> OperatorNextAction:
    source = GuidanceInput(
        status=ReviewStatus.PENDING_REVIEW,
        reason_codes=(),
        supplier_name="LOGOSOFT",
        decision_type=None,
        decision_workflow=None,
        decision_version=None,
        execution_state=None,
        vendor_bill_id=None,
    )
    return build_operator_guidance(source, facts).next_action


def test_guidance_asks_for_the_internal_expense_account_before_the_decision() -> None:
    assert _guidance(OperatorGuidanceFacts()) is OperatorNextAction.DECISION
    assert (
        _guidance(OperatorGuidanceFacts(current_purchase_purpose=PurchasePurpose.RESALE)) is OperatorNextAction.DECISION
    )
    assert (
        _guidance(OperatorGuidanceFacts(current_purchase_purpose=PurchasePurpose.INTERNAL_USE))
        is OperatorNextAction.ACCOUNTING
    )
    assert (
        _guidance(
            OperatorGuidanceFacts(
                current_purchase_purpose=PurchasePurpose.INTERNAL_USE, current_accounting_resolution_recorded=True
            )
        )
        is OperatorNextAction.DECISION
    )


# =========================================================================== API + production wiring


def test_internal_use_errors_map_to_http_conflict() -> None:
    from http import HTTPStatus

    from app.api.routers.workbench import _status_code_for_exception
    from app.application.execution.exceptions import ExecutionPreviewInternalUseAccountingError

    assert _status_code_for_exception(InternalUseDecisionEligibilityError("x")) == HTTPStatus.CONFLICT
    assert _status_code_for_exception(ExecutionPreviewInternalUseAccountingError("x")) == HTTPStatus.CONFLICT


def _settings(**values: Any):
    from app.core.config import Settings

    return Settings(**values)


def test_api_decision_use_case_wires_internal_use_gate() -> None:
    from unittest.mock import MagicMock

    from app.api.dependencies import get_submit_review_decision_use_case
    from app.connectors.odoo.client import OdooJson2Client

    settings = _settings()
    use_case = get_submit_review_decision_use_case(
        writer=MagicMock(), session=MagicMock(), odoo_client=OdooJson2Client.from_settings(settings), settings=settings
    )
    assert isinstance(use_case._internal_use_decision_gate, InternalUseDecisionGate)


def test_odoo_decision_ingestion_wires_internal_use_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import MagicMock

    import app.composition.imports as imports_module
    from app.composition.imports import build_odoo_workbench_decision_ingestion_workflow
    from app.connectors.odoo.client import OdooJson2Client

    for mapping in (imports_module.OdooWorkbenchFieldMapping, imports_module.OdooWorkbenchProjectionFieldMapping):
        monkeypatch.setattr(mapping, "from_environment", lambda *args, **kwargs: MagicMock())
    settings = _settings()
    workflow = build_odoo_workbench_decision_ingestion_workflow(
        session=MagicMock(), settings=settings, odoo_client=OdooJson2Client.from_settings(settings)
    )
    submitters = [value for value in vars(workflow).values() if isinstance(value, SubmitReviewDecisionUseCase)] or [
        getattr(workflow, slot) for slot in getattr(type(workflow), "__slots__", ())
    ]
    submitter = next(value for value in submitters if isinstance(value, SubmitReviewDecisionUseCase))
    assert isinstance(submitter._internal_use_decision_gate, InternalUseDecisionGate)


def test_execution_preview_and_readback_compositions_wire_internal_use() -> None:
    from unittest.mock import MagicMock

    from app.composition.execution import (
        build_get_vendor_bill_readback_use_case,
        build_vendor_bill_preview_use_case,
    )
    from app.connectors.odoo.client import OdooJson2Client

    source = Path("app/composition/execution.py").read_text(encoding="utf-8")
    assert "internal_use_accounting_check=build_internal_use_execution_accounting_validator(" in source

    settings = _settings()
    client = OdooJson2Client.from_settings(settings)
    preview = build_vendor_bill_preview_use_case(session=MagicMock(), settings=settings, odoo_client=client)
    assert preview._internal_use_accounting_pin_reader is not None
    readback = build_get_vendor_bill_readback_use_case(session=MagicMock(), settings=settings, odoo_client=client)
    assert isinstance(readback._internal_use_verifier, VendorBillInternalUseReadbackVerifier)


def test_resale_validator_still_wired_alongside() -> None:
    source = Path("app/composition/execution.py").read_text(encoding="utf-8")
    assert "resale_accounting_check=build_resale_execution_accounting_validator(" in source
    assert ResaleExecutionAccountingValidator.__name__ in source


# =========================================================================== rollback safety


class _PreInternalUseSourceReader(SqlAlchemyExecutionSourceInvoiceReader):
    """The RESALE pin read exactly as code before INTERNAL_USE pinning (def60a1) does it:
    any stored value is hydrated strictly as a RESALE pin; there is no guard awareness and
    ``internal_use_accounting_pin`` is unknown."""

    def get_resale_accounting_pin(self, *, review_id: str, company_id: int, decision_version: int):
        from app.application.execution.exceptions import ExecutionSourceInvoiceIntegrityError
        from app.application.workbench.exceptions import WorkbenchContractError
        from app.application.workbench.resale_accounting_pin import resale_accounting_pin_from_data

        stored = self._session.query(ExecutionSourceInvoiceEvidence).filter_by(review_id=review_id).one()
        if stored.resale_accounting_pin is None:
            return None
        try:
            return resale_accounting_pin_from_data(stored.resale_accounting_pin)
        except WorkbenchContractError as exc:
            raise ExecutionSourceInvoiceIntegrityError("integrity") from exc


def _pre_internal_use_strategy(session: Session, client: _RecordingAccountMoveClient) -> VendorBillExecutionStrategy:
    """Execution as the rolled-back image composes it: RESALE check only, no INTERNAL_USE check."""

    return VendorBillExecutionStrategy(
        source_tax_safety_check=ALLOW_SOURCE_TAX_SAFETY_CHECK,
        source_invoice_reader=with_ubl_rate(SqlAlchemyExecutionSourceInvoiceReader(session)),
        vendor_bill_builder=VendorBillBuilder(),
        vendor_bill_writer=OdooVendorBillWriter(
            repository=AccountMoveRepository(client=client),
            policy=OdooVendorBillWritePolicy(
                production_operations_enabled=True, production_approval_ack=PRODUCTION_APPROVAL_ACK
            ),
        ),
        resale_accounting_check=ResaleExecutionAccountingValidator(
            pin_reader=_PreInternalUseSourceReader(session),
            purpose_reader=SqlAlchemyReviewPurchasePurposeResolutionRepository(session),
            product_account_resolver=_FakeProductAccountResolver({}),
            fiscal_position_reader=object(),
            approved_category_ids=frozenset({CHILD_CATEGORY_ID}),
        ),
        internal_use_accounting_check=NON_INTERNAL_USE_ACCOUNTING_CHECK,
        currency_reader=TWO_DECIMAL_CURRENCY_READER,
        fx_rate_policy=MATCHING_FX_RATE_POLICY,
    )


def test_internal_use_decision_writes_the_rollback_guard(session: Session) -> None:
    _accept_internal_use(session)
    stored = _stored_evidence(session)
    assert stored.resale_accounting_pin == INTERNAL_USE_ROLLBACK_GUARD
    assert stored.internal_use_accounting_pin is not None
    # The current reader recognises the guard: no RESALE pin, the INTERNAL_USE pin is intact.
    reader = SqlAlchemyExecutionSourceInvoiceReader(session)
    query = {"review_id": REVIEW_ID, "company_id": COMPANY_ID, "decision_version": DECISION_VERSION}
    assert reader.get_resale_accounting_pin(**query) is None
    assert reader.get_internal_use_accounting_pin(**query) is not None


def test_rolled_back_code_fails_closed_on_an_internal_use_decision(session: Session) -> None:
    """After a rollback, execution must never reach Odoo with the category's RESALE account."""

    _accept_internal_use(session)
    from app.application.workbench.exceptions import WorkbenchContractError
    from app.application.workbench.resale_accounting_pin import resale_accounting_pin_from_data

    with pytest.raises(WorkbenchContractError):
        resale_accounting_pin_from_data(_stored_evidence(session).resale_accounting_pin)

    client = _RecordingAccountMoveClient()
    result = _pre_internal_use_strategy(session, client).execute(_request())

    assert result.status is ExecutionStepStatus.FAILED
    assert result.error_code == "resale_execution_accounting_error"
    assert client.odoo_calls == 0


def test_without_the_guard_rolled_back_code_would_book_the_category_account(session: Session) -> None:
    """The risk the guard removes: the same decision with no guard is billed product-only."""

    _accept_internal_use(session)
    session.query(ExecutionSourceInvoiceEvidence).update({"resale_accounting_pin": None})
    client = _RecordingAccountMoveClient()

    result = _pre_internal_use_strategy(session, client).execute(_request())

    assert result.status is ExecutionStepStatus.EXECUTED
    assert all("account_id" not in line for line in _line_payloads(client))


def test_current_code_rejects_a_pin_without_its_guard_and_a_lone_guard(session: Session) -> None:
    _accept_internal_use(session)
    reader = SqlAlchemyExecutionSourceInvoiceReader(session)
    query = {"review_id": REVIEW_ID, "company_id": COMPANY_ID, "decision_version": DECISION_VERSION}
    from app.application.execution.exceptions import ExecutionSourceInvoiceIntegrityError

    session.query(ExecutionSourceInvoiceEvidence).update({"resale_accounting_pin": None})
    with pytest.raises(ExecutionSourceInvoiceIntegrityError):
        reader.get_internal_use_accounting_pin(**query)
    client = _RecordingAccountMoveClient()
    result = _strategy(session, client).execute(_request())
    assert result.status is ExecutionStepStatus.FAILED
    assert result.error_code == "internal_use_execution_accounting_error"
    assert client.odoo_calls == 0

    session.query(ExecutionSourceInvoiceEvidence).update(
        {"resale_accounting_pin": dict(INTERNAL_USE_ROLLBACK_GUARD), "internal_use_accounting_pin": None}
    )
    with pytest.raises(ExecutionSourceInvoiceIntegrityError):
        reader.get_resale_accounting_pin(**query)


@pytest.mark.parametrize(
    "tampered",
    [
        {**INTERNAL_USE_ROLLBACK_GUARD, "extra": 1},
        {key: value for key, value in INTERNAL_USE_ROLLBACK_GUARD.items() if key != "rollback_guard"},
        {**INTERNAL_USE_ROLLBACK_GUARD, "purchase_purpose": "resale"},
    ],
)
def test_only_the_exact_guard_document_is_recognised(session: Session, tampered: dict[str, Any]) -> None:
    _accept_internal_use(session)
    session.query(ExecutionSourceInvoiceEvidence).update({"resale_accounting_pin": tampered})
    reader = SqlAlchemyExecutionSourceInvoiceReader(session)
    query = {"review_id": REVIEW_ID, "company_id": COMPANY_ID, "decision_version": DECISION_VERSION}
    from app.application.execution.exceptions import ExecutionSourceInvoiceIntegrityError

    with pytest.raises(ExecutionSourceInvoiceIntegrityError):
        reader.get_resale_accounting_pin(**query)
    with pytest.raises(ExecutionSourceInvoiceIntegrityError):
        reader.get_internal_use_accounting_pin(**query)


def test_resale_decision_keeps_a_real_resale_pin(session: Session) -> None:
    _seed_unblocked_review(session)
    _record_purpose(session, PurchasePurpose.RESALE)
    resolver = _FakeProductAccountResolver({PRODUCT_A: _discovery(PRODUCT_A), PRODUCT_B: _discovery(PRODUCT_B)})
    _decision_use_case(session, resale_resolver=resolver).execute(_decision())

    stored = _stored_evidence(session)
    assert stored.resale_accounting_pin != INTERNAL_USE_ROLLBACK_GUARD
    assert "rollback_guard" not in stored.resale_accounting_pin
    pin = SqlAlchemyExecutionSourceInvoiceReader(session).get_resale_accounting_pin(
        review_id=REVIEW_ID, company_id=COMPANY_ID, decision_version=DECISION_VERSION
    )
    assert pin is not None and pin.purchase_purpose is PurchasePurpose.RESALE


def test_strategy_requires_the_internal_use_check(session: Session) -> None:
    from app.application.execution.exceptions import ExecutionSourceInvoiceError

    with pytest.raises(ExecutionSourceInvoiceError, match="INTERNAL_USE accounting check is required"):
        VendorBillExecutionStrategy(
            source_tax_safety_check=ALLOW_SOURCE_TAX_SAFETY_CHECK,
            source_invoice_reader=SqlAlchemyExecutionSourceInvoiceReader(session),
            vendor_bill_builder=VendorBillBuilder(),
            vendor_bill_writer=object(),
            resale_accounting_check=NON_RESALE_ACCOUNTING_CHECK,
            internal_use_accounting_check=None,
            currency_reader=TWO_DECIMAL_CURRENCY_READER,
            fx_rate_policy=MATCHING_FX_RATE_POLICY,
        )


def test_studio_view_xml_sets_no_account_and_leaves_4217_alone() -> None:
    """The prepared Studio extension only adds hints/required modifiers; it never fixes,
    defaults or pre-selects an expense account and never targets view 4217's own nodes."""

    import xml.etree.ElementTree as ElementTree

    path = Path("docs/odoo_studio/internal_use_workbench_view.xml")
    root = ElementTree.parse(path).getroot()
    assert root.tag == "data"
    anchors = {xpath.get("expr") for xpath in root}
    assert anchors <= {
        "//field[@name='x_studio_ipp_req_purpose']",
        "//field[@name='x_studio_ipp_req_expense_account']",
        "//field[@name='x_studio_ipp_req_expense_category']",
    }
    body = ElementTree.tostring(root, encoding="unicode")
    for forbidden in ("default", "context", "770000", "ipp_golive_", "domain", "readonly"):
        assert forbidden not in body, forbidden
    set_attributes = {node.get("name") for node in root.iter("attribute")}
    assert set_attributes == {"required"}
