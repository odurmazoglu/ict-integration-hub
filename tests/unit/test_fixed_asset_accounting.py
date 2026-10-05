"""CAPITALIZE_FIXED_ASSET accounting treatment (fixed-asset / demirbaş capitalization).

End to end through the REAL use cases on SQLite -- import -> INTERNAL_USE purpose ->
CAPITALIZE_FIXED_ASSET accounting resolution -> reclassification (Stage-1 evidence) ->
Vendor Bill decision (Stage-2 evidence) -> Vendor Bill build/payload -- with an
Apple-shaped two-line invoice (two company-owned phones, quantity 1 each, 20% VAT),
plus focused unit tests for every validation rule. Nothing here is Apple-specific in
the code under test, and nothing talks to Odoo: the read-only Odoo lookups are faked.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.application.commands import ImportInvoiceCommand
from app.application.decision import (
    DecisionEngine,
    ManualReviewStrategy,
    VendorBillReviewRecommendationStrategy,
    WorkflowStrategyResolver,
)
from app.application.execution.contracts import ExecutionSourceInvoice
from app.application.execution.exceptions import ExecutionPlanningError
from app.application.expense_mapping import OperatingExpenseMatchingEngine
from app.application.fixed_asset_accounting import (
    FixedAssetAccounting,
    FixedAssetAccountingContractError,
    fixed_asset_accounting_from_data,
    fixed_asset_accounting_to_data,
)
from app.application.rules.deterministic import DeterministicRuleEngine
from app.application.use_cases import ImportInvoiceUseCase
from app.application.use_cases.reclassify_review import ReclassifyWorkbenchReviewUseCase
from app.application.workbench import ReviewItemCreationService
from app.application.workbench.accounting_resolution import (
    AccountingTreatmentType,
    ReviewAccountingResolution,
    SubmitReviewAccountingResolutionCommand,
)
from app.application.workbench.accounting_resolution_use_cases import SubmitReviewAccountingResolutionUseCase
from app.application.workbench.commands import ReviewDecisionCommand
from app.application.workbench.decision_use_cases import SubmitReviewDecisionUseCase
from app.application.workbench.dto import LineResolution, ReviewDecisionType
from app.application.workbench.exceptions import (
    AccountingResolutionPurposeUnsupportedError,
    DepreciationModelInvalidError,
    FixedAssetAccountingUnavailableError,
    FixedAssetAccountInvalidError,
    WorkbenchContractError,
)
from app.application.workbench.expense_account_lookup import ExpenseAccountCandidate
from app.application.workbench.fixed_asset_lookup import (
    DepreciationModelRecord,
    FixedAssetAccountPolicy,
    FixedAssetAccountRecord,
)
from app.application.workbench.purchase_purpose import (
    PurchasePurpose,
    PurchasePurposeResolution,
    SubmitPurchasePurposeCommand,
)
from app.application.workbench.purchase_purpose_use_cases import SubmitPurchasePurposeUseCase
from app.application.workbench.review_evidence import EffectiveLineResolutionKind, effective_resolutions
from app.application.workflow import ManualReviewReasonCode, WorkflowType
from app.billing import VendorBillBuilder
from app.billing.builder import to_odoo_account_move_payload, validate_vendor_bill_inputs
from app.billing.dto import VendorBillLine
from app.domain.invoice import Header, InternalInvoice, InvoiceLine, MonetaryTotals, Party, Tax
from app.matching import (
    InvoiceProductLineResult,
    InvoiceProductMatchResult,
    PartnerMatchResult,
    PartnerMatchStatus,
    ProductMatchResult,
    ProductMatchStatus,
)
from app.models.execution_source_invoice_evidence import ExecutionSourceInvoiceEvidence
from app.models.operating_expense_mapping import OperatingExpenseMappingRecord
from app.models.workbench_review_accounting_resolution import WorkbenchReviewAccountingResolution
from app.models.workbench_review_classification_evidence import WorkbenchReviewClassificationEvidence
from app.models.workbench_review_decision import WorkbenchReviewDecision
from app.models.workbench_review_execution_evidence import WorkbenchReviewExecutionEvidence
from app.models.workbench_review_item import WorkbenchReviewItem
from app.models.workbench_review_purchase_purpose_resolution import WorkbenchReviewPurchasePurposeResolution
from app.models.workbench_review_reclassification import WorkbenchReviewReclassification
from app.models.workbench_review_source_invoice_correction import WorkbenchReviewSourceInvoiceCorrection
from app.models.workbench_review_source_invoice_evidence import WorkbenchReviewSourceInvoiceEvidence
from app.models.workbench_review_supplier_remediation_effect import WorkbenchReviewSupplierRemediationEffect
from app.persistence import (
    SqlAlchemyExecutionSourceInvoiceReader,
    SqlAlchemyOperatingExpenseMappingRepository,
    SqlAlchemyReviewAccountingResolutionRepository,
    SqlAlchemyReviewExecutionEvidenceReader,
    SqlAlchemyReviewPurchasePurposeResolutionRepository,
    SqlAlchemyReviewRepository,
    SqlAlchemyReviewSourceInvoiceEvidenceReader,
    SqlAlchemyReviewSupplierRemediationEffectRepository,
    SqlAlchemyUnitOfWork,
)
from app.persistence.execution_source_invoice_reader import (
    deserialize_execution_source_invoice_payload,
    serialize_execution_source_invoice_payload,
)
from app.tax_mapping import InvoiceTaxLineResult, InvoiceTaxMappingResult, TaxMatchResult, TaxMatchStatus, TaxType

COMPANY_ID = 1
OTHER_COMPANY_ID = 2
VAT = "0710414224"
PARTNER_ID = 451
TAX_ID = 34
ASSET_ACCOUNT = 74  # e.g. 255000 Furniture And Fixtures (demirbaş) -- any approved asset_fixed id
OTHER_ASSET_ACCOUNT = 72
ACCUMULATED_DEPRECIATION = 76  # asset_fixed in the TR chart, deliberately NOT allowlisted
EXPENSE_ACCOUNT = 247
DEPRECIATION_EXPENSE = 259  # e.g. 796000; configured on the asset account (saas~19.2+)
MODEL_GLOBAL = 3
MODEL_OTHER_COMPANY = 9
MODEL_INACTIVE = 8
ACTOR = "fixed-asset-test-operator"
CURRENCY_ID = 31


# --------------------------------------------------------------------------- fixtures / fakes


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    from app.db.base import Base

    Base.metadata.create_all(
        engine,
        tables=[
            WorkbenchReviewItem.__table__,
            WorkbenchReviewExecutionEvidence.__table__,
            WorkbenchReviewClassificationEvidence.__table__,
            WorkbenchReviewSourceInvoiceEvidence.__table__,
            WorkbenchReviewSourceInvoiceCorrection.__table__,
            WorkbenchReviewReclassification.__table__,
            WorkbenchReviewSupplierRemediationEffect.__table__,
            OperatingExpenseMappingRecord.__table__,
            WorkbenchReviewPurchasePurposeResolution.__table__,
            WorkbenchReviewAccountingResolution.__table__,
            WorkbenchReviewDecision.__table__,
            ExecutionSourceInvoiceEvidence.__table__,
        ],
    )
    with sessionmaker(bind=engine)() as db_session:
        yield db_session


def _invoice(*, ettn: str) -> InternalInvoice:
    """Two identifier-free equipment lines, quantity 1 each, 20% VAT (Apple-shaped)."""

    return InternalInvoice(
        header=Header(
            invoice_number="I102026000015045",
            invoice_uuid=ettn,
            ettn=ettn,
            issue_date=date(2026, 9, 30),
            currency_code="TRY",
        ),
        supplier=Party(name="APPLE Teknoloji ve Satış Limited Şirketi", tax_number=VAT),
        customer=Party(name="ICT TEK SAN VE AS", tax_number="4651205941"),
        totals=MonetaryTotals(
            line_extension_amount=Decimal("244165.00"),
            tax_exclusive_amount=Decimal("244165.00"),
            tax_inclusive_amount=Decimal("292998.00"),
            payable_amount=Decimal("292998.00"),
        ),
        lines=(
            InvoiceLine(
                line_number="000001",
                description="iPhone 18 Pro 256 GB Siyah",
                quantity=Decimal("1"),
                unit_code="C62",
                unit_price=Decimal("114999.17"),
                line_extension_amount=Decimal("114999.17"),
                taxes=(Tax(tax_type="KDV", rate=Decimal("20")),),
            ),
            InvoiceLine(
                line_number="000002",
                description="iPhone 18 Pro 512 GB Siyah",
                quantity=Decimal("1"),
                unit_code="C62",
                unit_price=Decimal("129165.83"),
                line_extension_amount=Decimal("129165.83"),
                taxes=(Tax(tax_type="KDV", rate=Decimal("20")),),
            ),
        ),
    )


class _FakeImportHistory:
    def find_imported_invoice(self, idempotency_key: str) -> None:
        return None

    def record_import_result(self, *, company_id: int, idempotency_key: str, result: object) -> None:
        return None


class _MatchingFacts:
    def __init__(self, result: object) -> None:
        self.result = result

    def match_invoice(self, invoice: InternalInvoice, *, company_id: int, partner_match: object = None) -> object:
        return self.result

    def map_invoice(self, invoice: InternalInvoice, *, company_id: int) -> object:
        return self.result


def _partner_matched() -> PartnerMatchResult:
    return PartnerMatchResult(
        status=PartnerMatchStatus.MATCHED,
        partner_id=PARTNER_ID,
        matched_by="tax_number",
        reason="Unique supplier partner match by tax number.",
        candidate_count=1,
        confidence=Decimal("1.00"),
    )


def _products_invalid_input(invoice: InternalInvoice) -> InvoiceProductMatchResult:
    return InvoiceProductMatchResult(
        line_results=tuple(
            InvoiceProductLineResult(
                line_number=line.line_number,
                result=ProductMatchResult(
                    status=ProductMatchStatus.INVALID_INPUT,
                    line_number=line.line_number,
                    product_id=None,
                    default_code=None,
                    barcode=None,
                    seller_item_code=None,
                    matched_by=None,
                    reason="No deterministic product identifier present on this line.",
                    candidate_count=0,
                    confidence=None,
                ),
            )
            for line in invoice.lines
        )
    )


def _taxes(invoice: InternalInvoice) -> InvoiceTaxMappingResult:
    return InvoiceTaxMappingResult(
        line_results=tuple(
            InvoiceTaxLineResult(
                line_number=line.line_number,
                tax_index=idx,
                result=TaxMatchResult(
                    status=TaxMatchStatus.MATCHED,
                    tax_id=TAX_ID,
                    company_id=COMPANY_ID,
                    tax_type=TaxType.VAT,
                    tax_rate=Decimal("20"),
                    matched_by="company_type_rate",
                    confidence=Decimal("1.00"),
                    reason="matched",
                    candidate_count=1,
                ),
            )
            for line in invoice.lines
            for idx, _t in enumerate(line.taxes)
        )
    )


@dataclass(frozen=True)
class _Facts:
    partner_match: PartnerMatchResult
    product_match: InvoiceProductMatchResult
    tax_match: InvoiceTaxMappingResult


def _decision_engine(facts: _Facts, mapping_repository) -> DecisionEngine:
    return DecisionEngine(
        rule_engine=DeterministicRuleEngine(
            partner_matcher=_MatchingFacts(facts.partner_match),
            product_matcher=_MatchingFacts(facts.product_match),
            tax_mapper=_MatchingFacts(facts.tax_match),
            operating_expense_matcher=OperatingExpenseMatchingEngine(mapping_repository),
        ),
        strategy_resolver=WorkflowStrategyResolver([VendorBillReviewRecommendationStrategy(), ManualReviewStrategy()]),
    )


class _FakeExpenseAccountReader:
    def find_candidates(self, *, company_id: int, query: str | None) -> tuple[ExpenseAccountCandidate, ...]:
        return ()

    def find_eligible_by_id(self, *, company_id: int, account_id: int) -> ExpenseAccountCandidate | None:
        if account_id != EXPENSE_ACCOUNT:
            return None
        return ExpenseAccountCandidate(id=account_id, code="770000", name="General Admin", account_type="expense")


def _account(account_id: int, **overrides) -> FixedAssetAccountRecord:
    values = {
        "id": account_id,
        "code": {74: "255000", 72: "253000", 76: "257000", 247: "770000"}.get(account_id, "999000"),
        "name": "Account",
        "account_type": "asset_fixed",
        "active": True,
        "company_ids": (COMPANY_ID,),
        "can_create_asset": True,
        "asset_posting_accounts_supported": True,
        "asset_depreciation_account_id": ACCUMULATED_DEPRECIATION,
        "asset_expense_account_id": DEPRECIATION_EXPENSE,
    }
    values.update(overrides)
    return FixedAssetAccountRecord(**values)


#: Accounts an asset account points at (accumulated depreciation / depreciation expense).
_POSTING_ACCOUNTS = {
    ACCUMULATED_DEPRECIATION: _account(ACCUMULATED_DEPRECIATION, name="Accumulated Depreciation"),
    DEPRECIATION_EXPENSE: _account(DEPRECIATION_EXPENSE, account_type="expense", can_create_asset=False),
}


class _FakeFixedAssetReader:
    """Read-only Odoo facts. Records every lookup so tests can prove nothing is written."""

    def __init__(self, accounts=None, models=None) -> None:
        self.accounts = (
            accounts
            if accounts is not None
            else {
                ASSET_ACCOUNT: _account(ASSET_ACCOUNT),
                OTHER_ASSET_ACCOUNT: _account(OTHER_ASSET_ACCOUNT),
                ACCUMULATED_DEPRECIATION: _account(ACCUMULATED_DEPRECIATION, name="Accumulated Depreciation"),
                EXPENSE_ACCOUNT: _account(EXPENSE_ACCOUNT, account_type="expense", can_create_asset=False),
            }
        )
        self.models = (
            models
            if models is not None
            else {
                MODEL_GLOBAL: DepreciationModelRecord(id=MODEL_GLOBAL, name="5 Year Linear", active=True),
                MODEL_OTHER_COMPANY: DepreciationModelRecord(
                    id=MODEL_OTHER_COMPANY, name="Other", active=True, company_id=OTHER_COMPANY_ID
                ),
                MODEL_INACTIVE: DepreciationModelRecord(id=MODEL_INACTIVE, name="Old", active=False),
            }
        )
        self.calls: list[tuple[str, int]] = []

    def read_account(self, *, account_id: int) -> FixedAssetAccountRecord | None:
        self.calls.append(("account", account_id))
        if account_id in self.accounts:
            return self.accounts[account_id]
        return _POSTING_ACCOUNTS.get(account_id)

    def read_depreciation_model(self, *, model_id: int) -> DepreciationModelRecord | None:
        self.calls.append(("model", model_id))
        return self.models.get(model_id)


def _reclassifier(session: Session, facts: _Facts, mapping_repository) -> ReclassifyWorkbenchReviewUseCase:
    review_repository = SqlAlchemyReviewRepository(session)
    return ReclassifyWorkbenchReviewUseCase(
        decision_engine=_decision_engine(facts, mapping_repository),
        source_invoice_reader=SqlAlchemyReviewSourceInvoiceEvidenceReader(session),
        reclassification_writer=review_repository,
        supplier_remediation_effect_reader=SqlAlchemyReviewSupplierRemediationEffectRepository(session),
        operating_expense_matcher=OperatingExpenseMatchingEngine(mapping_repository),
        review_accounting_resolution_reader=SqlAlchemyReviewAccountingResolutionRepository(session),
    )


def _accounting_use_case(
    session: Session,
    facts: _Facts,
    *,
    reader: _FakeFixedAssetReader | None = None,
    allowlist: frozenset[int] = frozenset({ASSET_ACCOUNT, OTHER_ASSET_ACCOUNT}),
    with_reader: bool = True,
) -> SubmitReviewAccountingResolutionUseCase:
    mapping_repository = SqlAlchemyOperatingExpenseMappingRepository(session)
    return SubmitReviewAccountingResolutionUseCase(
        review_reader=SqlAlchemyReviewRepository(session),
        purpose_reader=SqlAlchemyReviewPurchasePurposeResolutionRepository(session),
        expense_account_reader=_FakeExpenseAccountReader(),
        accounting_resolution_writer=SqlAlchemyReviewAccountingResolutionRepository(session),
        reclassifier=_reclassifier(session, facts, mapping_repository),
        unit_of_work=SqlAlchemyUnitOfWork(session),
        fixed_asset_reader=(reader or _FakeFixedAssetReader()) if with_reader else None,
        fixed_asset_account_policy=FixedAssetAccountPolicy(allowed_account_ids=allowlist),
    )


async def _imported_with_purpose(
    session: Session, *, ettn: str, purpose: PurchasePurpose = PurchasePurpose.INTERNAL_USE
) -> tuple[str, InternalInvoice, _Facts]:
    invoice = _invoice(ettn=ettn)
    facts = _Facts(_partner_matched(), _products_invalid_input(invoice), _taxes(invoice))
    result = await ImportInvoiceUseCase(
        import_history=_FakeImportHistory(),
        decision_engine=_decision_engine(facts, SqlAlchemyOperatingExpenseMappingRepository(session)),
        review_item_creation_service=ReviewItemCreationService(SqlAlchemyReviewRepository(session)),
        unit_of_work=SqlAlchemyUnitOfWork(session),
    ).execute(ImportInvoiceCommand(invoice=invoice, idempotency_key=f"uyumsoft:1:{ettn}", company_id=COMPANY_ID))
    review_id = result.review_id
    item = session.scalar(select(WorkbenchReviewItem).where(WorkbenchReviewItem.review_id == review_id))
    assert {r["code"] for r in item.review_reasons} == {ManualReviewReasonCode.OPERATING_EXPENSE_MAPPING_REQUIRED}
    if purpose is PurchasePurpose.RESALE:
        # RESALE cannot even be recorded through the use case for an identifier-free
        # invoice; seed it directly to prove the accounting stage rejects it on its own.
        SqlAlchemyReviewPurchasePurposeResolutionRepository(session).create_purchase_purpose_resolution(
            PurchasePurposeResolution(
                review_id=review_id,
                company_id=COMPANY_ID,
                review_version=1,
                source_invoice_id=ettn,
                purchase_purpose=purpose,
                approved_by=ACTOR,
            )
        )
    else:
        SubmitPurchasePurposeUseCase(
            review_reader=SqlAlchemyReviewRepository(session),
            source_invoice_reader=SqlAlchemyReviewSourceInvoiceEvidenceReader(session),
            purpose_writer=SqlAlchemyReviewPurchasePurposeResolutionRepository(session),
            unit_of_work=SqlAlchemyUnitOfWork(session),
        ).execute(
            SubmitPurchasePurposeCommand(
                review_id=review_id,
                company_id=COMPANY_ID,
                expected_version=1,
                purchase_purpose=purpose,
                approved_by=ACTOR,
            )
        )
    session.commit()
    return review_id, invoice, facts


def _capitalize(
    review_id: str, *, account: int = ASSET_ACCOUNT, model: int = MODEL_GLOBAL, version: int = 1
) -> SubmitReviewAccountingResolutionCommand:
    return SubmitReviewAccountingResolutionCommand(
        review_id=review_id,
        company_id=COMPANY_ID,
        expected_version=version,
        treatment_type=AccountingTreatmentType.CAPITALIZE_FIXED_ASSET,
        asset_account_id=account,
        depreciation_model_id=model,
        approved_by=ACTOR,
        note="Company-owned equipment; capitalize.",
    )


async def _resolved(session: Session, ettn: str, **kwargs):
    review_id, invoice, facts = await _imported_with_purpose(session, ettn=ettn)
    result = await _accounting_use_case(session, facts, **kwargs).execute(_capitalize(review_id))
    session.commit()
    return review_id, invoice, facts, result


def _decide(session: Session, review_id: str, *, version: int = 2, line_resolutions=()) -> None:
    SubmitReviewDecisionUseCase(
        review_decision_writer=SqlAlchemyReviewRepository(session),
        unit_of_work=SqlAlchemyUnitOfWork(session),
        execution_evidence_reader=SqlAlchemyReviewExecutionEvidenceReader(session),
    ).execute(
        ReviewDecisionCommand(
            review_id=review_id,
            company_id=COMPANY_ID,
            expected_version=version,
            decision=ReviewDecisionType.SELECT_WORKFLOW,
            decided_by=ACTOR,
            selected_workflow=WorkflowType.VENDOR_BILL,
            idempotency_key=f"fixed-asset-decision-{review_id}",
            line_resolutions=tuple(line_resolutions),
        )
    )
    session.commit()


def _build_payload(source: ExecutionSourceInvoice) -> dict:
    bill = VendorBillBuilder().build(
        source.invoice,
        source.partner_match,
        source.product_match,
        source.tax_match,
        company_id=COMPANY_ID,
        fixed_asset_accounting=source.fixed_asset_accounting,
    )
    return to_odoo_account_move_payload(bill, currency_id=CURRENCY_ID, product_uom_ids={})


EXPECTED_ASSET_LINES = [
    {
        "name": "iPhone 18 Pro 256 GB Siyah",
        "quantity": "1",
        "price_unit": "114999.17",
        "account_id": ASSET_ACCOUNT,
        "tax_ids": ((6, 0, (TAX_ID,)),),
        "depreciation_model_id": MODEL_GLOBAL,
    },
    {
        "name": "iPhone 18 Pro 512 GB Siyah",
        "quantity": "1",
        "price_unit": "129165.83",
        "account_id": ASSET_ACCOUNT,
        "tax_ids": ((6, 0, (TAX_ID,)),),
        "depreciation_model_id": MODEL_GLOBAL,
    },
]


# =================================================================== end to end (Apple-shaped two-line invoice)


async def test_valid_internal_use_fixed_asset_resolves_and_freezes_stage_one_evidence(session: Session) -> None:
    review_id, invoice, _facts, result = await _resolved(session, "FA-E2E-1")

    assert result.status.value == "resolved"
    assert result.treatment_type is AccountingTreatmentType.CAPITALIZE_FIXED_ASSET
    assert (result.asset_account_id, result.depreciation_model_id) == (ASSET_ACCOUNT, MODEL_GLOBAL)
    assert (result.expense_account_id, result.expense_category) == (None, None)
    assert result.current_review_reasons == ()  # OPERATING_EXPENSE_MAPPING_REQUIRED cleared
    assert result.current_workflow is WorkflowType.VENDOR_BILL

    row = session.scalar(select(WorkbenchReviewAccountingResolution))
    assert (row.treatment_type, row.asset_account_id, row.depreciation_model_id) == (
        "capitalize_fixed_asset",
        ASSET_ACCOUNT,
        MODEL_GLOBAL,
    )
    assert (row.expense_account_id, row.expense_category) == (None, None)

    evidence = session.scalar(
        select(WorkbenchReviewExecutionEvidence).where(WorkbenchReviewExecutionEvidence.review_version == 2)
    )
    assert evidence.operating_expense_match is None
    assert evidence.fixed_asset_accounting == {
        "schema_version": 1,
        "source": "review_accounting_resolution",
        "accounting_resolution_id": row.id,
        "asset_account_id": ASSET_ACCOUNT,
        "depreciation_model_id": MODEL_GLOBAL,
    }


async def test_decision_freezes_stage_two_evidence_and_payload_is_two_asset_lines(session: Session) -> None:
    review_id, invoice, _facts, _ = await _resolved(session, "FA-E2E-2")
    _decide(session, review_id)

    stage_two = session.scalar(select(ExecutionSourceInvoiceEvidence))
    assert stage_two.fixed_asset_accounting["asset_account_id"] == ASSET_ACCOUNT
    assert stage_two.fixed_asset_accounting["depreciation_model_id"] == MODEL_GLOBAL
    assert stage_two.operating_expense_match is None

    source = SqlAlchemyExecutionSourceInvoiceReader(session).get_source_invoice(
        review_id=review_id, company_id=COMPANY_ID, decision_version=stage_two.decision_version
    )
    payload = _build_payload(source)
    lines = [command[2] for command in payload["invoice_line_ids"]]
    assert lines == EXPECTED_ASSET_LINES  # two distinct lines, never merged
    assert payload["partner_id"] == PARTNER_ID
    assert "action_post" not in str(payload) and "asset_ids" not in str(payload)

    # Retry/resume rebuilds the exact same payload from the frozen evidence.
    assert _build_payload(source) == payload

    # The read model reports both lines as fixed-asset lines (never "unresolved").
    resolutions = effective_resolutions(source)
    assert {r.kind for r in resolutions.values()} == {EffectiveLineResolutionKind.FIXED_ASSET}
    assert {(r.asset_account_id, r.depreciation_model_id, r.expense_account_id) for r in resolutions.values()} == {
        (ASSET_ACCOUNT, MODEL_GLOBAL, None)
    }


async def test_later_odoo_default_change_cannot_alter_frozen_selection(session: Session) -> None:
    """The selection is copied from the Hub resolution, never from Odoo account defaults:
    after the resolution is accepted, the reader is never consulted again on execution."""

    reader = _FakeFixedAssetReader()
    review_id, *_ = await _resolved(session, "FA-E2E-3", reader=reader)
    calls_after_resolution = list(reader.calls)
    reader.models[MODEL_GLOBAL] = replace(reader.models[MODEL_GLOBAL], active=False)
    _decide(session, review_id)
    stage_two = session.scalar(select(ExecutionSourceInvoiceEvidence))
    source = SqlAlchemyExecutionSourceInvoiceReader(session).get_source_invoice(
        review_id=review_id, company_id=COMPANY_ID, decision_version=stage_two.decision_version
    )
    assert [line[2]["depreciation_model_id"] for line in _build_payload(source)["invoice_line_ids"]] == [3, 3]
    assert reader.calls == calls_after_resolution


async def test_fixed_asset_decision_rejects_per_line_account_only_resolutions(session: Session) -> None:
    review_id, *_ = await _resolved(session, "FA-E2E-4")
    with pytest.raises(Exception):  # noqa: B017 - any decision/evidence rejection is acceptable here
        _decide(
            session,
            review_id,
            line_resolutions=(
                LineResolution(line_number="000001", account_only=True, expense_account_id=EXPENSE_ACCOUNT),
            ),
        )
    session.rollback()
    assert session.scalar(select(ExecutionSourceInvoiceEvidence)) is None


async def test_resolution_retry_is_idempotent_and_different_selection_conflicts(session: Session) -> None:
    review_id, _invoice, facts = await _imported_with_purpose(session, ettn="FA-E2E-5")
    use_case = _accounting_use_case(session, facts)
    first = await use_case.execute(_capitalize(review_id))
    session.commit()
    replay = await use_case.execute(_capitalize(review_id))
    assert replay.already_applied is True
    assert (replay.asset_account_id, replay.depreciation_model_id) == (first.asset_account_id, MODEL_GLOBAL)
    assert session.query(WorkbenchReviewAccountingResolution).count() == 1
    with pytest.raises(Exception):  # noqa: B017 - version conflict for a different selection
        await use_case.execute(_capitalize(review_id, account=OTHER_ASSET_ACCOUNT))


# =================================================================== purpose gating


@pytest.mark.parametrize(
    "purpose",
    [PurchasePurpose.RESALE, PurchasePurpose.CUSTOMER_PROJECT, PurchasePurpose.OTHER_OPERATING_EXPENSE],
)
async def test_non_internal_use_purposes_are_rejected(session: Session, purpose: PurchasePurpose) -> None:
    review_id, _invoice, facts = await _imported_with_purpose(session, ettn=f"FA-P-{purpose.value}", purpose=purpose)
    with pytest.raises(AccountingResolutionPurposeUnsupportedError):
        await _accounting_use_case(session, facts).execute(_capitalize(review_id))
    assert session.query(WorkbenchReviewAccountingResolution).count() == 0


async def test_expense_account_treatment_semantics_are_unchanged(session: Session) -> None:
    review_id, _invoice, facts = await _imported_with_purpose(
        session, ettn="FA-EXP", purpose=PurchasePurpose.OTHER_OPERATING_EXPENSE
    )
    result = await _accounting_use_case(session, facts).execute(
        SubmitReviewAccountingResolutionCommand(
            review_id=review_id,
            company_id=COMPANY_ID,
            expected_version=1,
            treatment_type=AccountingTreatmentType.EXPENSE_ACCOUNT,
            expense_account_id=EXPENSE_ACCOUNT,
            expense_category="IT_HARDWARE_INTERNAL",
            approved_by=ACTOR,
        )
    )
    session.commit()
    assert (result.expense_account_id, result.asset_account_id, result.depreciation_model_id) == (
        EXPENSE_ACCOUNT,
        None,
        None,
    )
    evidence = session.scalar(
        select(WorkbenchReviewExecutionEvidence).where(WorkbenchReviewExecutionEvidence.review_version == 2)
    )
    assert evidence.fixed_asset_accounting is None
    assert evidence.operating_expense_match["expense_account_id"] == EXPENSE_ACCOUNT


# =================================================================== account / model validation


@pytest.mark.parametrize(
    ("account_id", "reader_accounts", "allowlist", "error"),
    [
        # account not found (allowlisted id that Odoo does not know)
        (ASSET_ACCOUNT, {}, frozenset({ASSET_ACCOUNT}), FixedAssetAccountInvalidError),
        # wrong company
        (
            ASSET_ACCOUNT,
            {ASSET_ACCOUNT: _account(ASSET_ACCOUNT, company_ids=(OTHER_COMPANY_ID,))},
            frozenset({ASSET_ACCOUNT}),
            FixedAssetAccountInvalidError,
        ),
        # non-asset account (even if someone allowlisted it)
        (
            EXPENSE_ACCOUNT,
            {EXPENSE_ACCOUNT: _account(EXPENSE_ACCOUNT, account_type="expense")},
            frozenset({EXPENSE_ACCOUNT}),
            FixedAssetAccountInvalidError,
        ),
        # accumulated depreciation is asset_fixed but excluded by the allowlist
        (ACCUMULATED_DEPRECIATION, None, frozenset({ASSET_ACCOUNT}), FixedAssetAccountInvalidError),
        # not in allowlist
        (OTHER_ASSET_ACCOUNT, None, frozenset({ASSET_ACCOUNT}), FixedAssetAccountInvalidError),
        # allowlist unset -> fail closed
        (ASSET_ACCOUNT, None, frozenset(), FixedAssetAccountingUnavailableError),
        # inactive account
        (
            ASSET_ACCOUNT,
            {ASSET_ACCOUNT: _account(ASSET_ACCOUNT, active=False)},
            frozenset({ASSET_ACCOUNT}),
            FixedAssetAccountInvalidError,
        ),
        # Odoo says the account cannot create assets
        (
            ASSET_ACCOUNT,
            {ASSET_ACCOUNT: _account(ASSET_ACCOUNT, can_create_asset=False)},
            frozenset({ASSET_ACCOUNT}),
            FixedAssetAccountInvalidError,
        ),
        # saas~19.2+: no accumulated depreciation account on the asset account
        (
            ASSET_ACCOUNT,
            {ASSET_ACCOUNT: _account(ASSET_ACCOUNT, asset_depreciation_account_id=None)},
            frozenset({ASSET_ACCOUNT}),
            FixedAssetAccountInvalidError,
        ),
        # saas~19.2+: no depreciation expense account on the asset account
        (
            ASSET_ACCOUNT,
            {ASSET_ACCOUNT: _account(ASSET_ACCOUNT, asset_expense_account_id=None)},
            frozenset({ASSET_ACCOUNT}),
            FixedAssetAccountInvalidError,
        ),
        # depreciation expense account configured but inactive (e.g. 796000 as shipped)
        (
            ASSET_ACCOUNT,
            {
                ASSET_ACCOUNT: _account(ASSET_ACCOUNT),
                DEPRECIATION_EXPENSE: _account(DEPRECIATION_EXPENSE, account_type="expense", active=False),
            },
            frozenset({ASSET_ACCOUNT}),
            FixedAssetAccountInvalidError,
        ),
        # accumulated depreciation account of another company
        (
            ASSET_ACCOUNT,
            {
                ASSET_ACCOUNT: _account(ASSET_ACCOUNT),
                ACCUMULATED_DEPRECIATION: _account(ACCUMULATED_DEPRECIATION, company_ids=(OTHER_COMPANY_ID,)),
            },
            frozenset({ASSET_ACCOUNT}),
            FixedAssetAccountInvalidError,
        ),
        # configured posting account does not exist
        (
            ASSET_ACCOUNT,
            {ASSET_ACCOUNT: _account(ASSET_ACCOUNT, asset_expense_account_id=999)},
            frozenset({ASSET_ACCOUNT}),
            FixedAssetAccountInvalidError,
        ),
        # asset account pointing at itself
        (
            ASSET_ACCOUNT,
            {ASSET_ACCOUNT: _account(ASSET_ACCOUNT, asset_depreciation_account_id=ASSET_ACCOUNT)},
            frozenset({ASSET_ACCOUNT}),
            FixedAssetAccountInvalidError,
        ),
    ],
)
async def test_asset_account_validation_fails_closed(
    session: Session, account_id, reader_accounts, allowlist, error
) -> None:
    review_id, _invoice, facts = await _imported_with_purpose(session, ettn=f"FA-A-{account_id}-{len(allowlist)}")
    reader = _FakeFixedAssetReader(accounts=reader_accounts)
    with pytest.raises(error):
        await _accounting_use_case(session, facts, reader=reader, allowlist=allowlist).execute(
            _capitalize(review_id, account=account_id)
        )
    assert session.query(WorkbenchReviewAccountingResolution).count() == 0


async def test_asset_posting_accounts_unsupported_by_odoo_version_are_not_required(session: Session) -> None:
    review_id, _invoice, facts = await _imported_with_purpose(session, ettn="FA-A-NOPOST")
    account = _account(
        ASSET_ACCOUNT,
        asset_posting_accounts_supported=False,
        asset_depreciation_account_id=None,
        asset_expense_account_id=None,
    )
    reader = _FakeFixedAssetReader(accounts={ASSET_ACCOUNT: account})
    result = await _accounting_use_case(session, facts, reader=reader).execute(_capitalize(review_id))
    assert result.asset_account_id == ASSET_ACCOUNT
    assert ("account", ACCUMULATED_DEPRECIATION) not in reader.calls


async def test_asset_posting_accounts_are_checked_read_only(session: Session) -> None:
    review_id, _invoice, facts = await _imported_with_purpose(session, ettn="FA-A-POST")
    reader = _FakeFixedAssetReader()
    await _accounting_use_case(session, facts, reader=reader).execute(_capitalize(review_id))
    assert {("account", ACCUMULATED_DEPRECIATION), ("account", DEPRECIATION_EXPENSE)} <= set(reader.calls)


async def test_can_create_asset_unsupported_by_odoo_version_is_not_required(session: Session) -> None:
    review_id, _invoice, facts = await _imported_with_purpose(session, ettn="FA-A-NOCAN")
    reader = _FakeFixedAssetReader(accounts={ASSET_ACCOUNT: _account(ASSET_ACCOUNT, can_create_asset=None)})
    result = await _accounting_use_case(session, facts, reader=reader).execute(_capitalize(review_id))
    assert result.asset_account_id == ASSET_ACCOUNT


@pytest.mark.parametrize(
    ("model_id", "error"),
    [
        (999, DepreciationModelInvalidError),  # missing
        (MODEL_OTHER_COMPANY, DepreciationModelInvalidError),  # wrong company
        (MODEL_INACTIVE, DepreciationModelInvalidError),  # inactive
    ],
)
async def test_depreciation_model_validation_fails_closed(session: Session, model_id: int, error) -> None:
    review_id, _invoice, facts = await _imported_with_purpose(session, ettn=f"FA-M-{model_id}")
    with pytest.raises(error):
        await _accounting_use_case(session, facts).execute(_capitalize(review_id, model=model_id))
    assert session.query(WorkbenchReviewAccountingResolution).count() == 0


async def test_company_scoped_and_global_models_are_valid(session: Session) -> None:
    review_id, _invoice, facts = await _imported_with_purpose(session, ettn="FA-M-OK")
    reader = _FakeFixedAssetReader()
    reader.models[20] = DepreciationModelRecord(id=20, name="Company model", active=True, company_id=COMPANY_ID)
    result = await _accounting_use_case(session, facts, reader=reader).execute(_capitalize(review_id, model=20))
    assert result.depreciation_model_id == 20


async def test_missing_reader_fails_closed(session: Session) -> None:
    review_id, _invoice, facts = await _imported_with_purpose(session, ettn="FA-NOREADER")
    with pytest.raises(FixedAssetAccountingUnavailableError):
        await _accounting_use_case(session, facts, with_reader=False).execute(_capitalize(review_id))


# =================================================================== DTO contracts


def _command(**overrides):
    values = {
        "review_id": "review:x",
        "company_id": COMPANY_ID,
        "expected_version": 1,
        "treatment_type": AccountingTreatmentType.CAPITALIZE_FIXED_ASSET,
        "asset_account_id": ASSET_ACCOUNT,
        "depreciation_model_id": MODEL_GLOBAL,
        "approved_by": ACTOR,
    }
    values.update(overrides)
    return SubmitReviewAccountingResolutionCommand(**values)


@pytest.mark.parametrize(
    "overrides",
    [
        {"asset_account_id": None},
        {"depreciation_model_id": None},
        {"asset_account_id": 0},
        {"depreciation_model_id": True},
        {"expense_account_id": EXPENSE_ACCOUNT},  # mixed: expense field on fixed asset
        {"expense_category": "IT_HARDWARE_INTERNAL"},
        # mixed the other way: asset fields on an expense resolution
        {
            "treatment_type": AccountingTreatmentType.EXPENSE_ACCOUNT,
            "expense_account_id": EXPENSE_ACCOUNT,
            "expense_category": "IT",
        },
        {"treatment_type": AccountingTreatmentType.EXPENSE_ACCOUNT, "asset_account_id": None},
    ],
)
def test_command_rejects_missing_or_mixed_fields(overrides) -> None:
    with pytest.raises(WorkbenchContractError):
        _command(**overrides)


def test_persisted_dto_enforces_the_same_shapes() -> None:
    ok = ReviewAccountingResolution(
        review_id="r",
        company_id=1,
        review_version=1,
        treatment_type=AccountingTreatmentType.CAPITALIZE_FIXED_ASSET,
        asset_account_id=ASSET_ACCOUNT,
        depreciation_model_id=MODEL_GLOBAL,
    )
    assert ok.expense_account_id is None
    with pytest.raises(WorkbenchContractError):
        replace(ok, expense_account_id=EXPENSE_ACCOUNT)
    with pytest.raises(WorkbenchContractError):
        ReviewAccountingResolution(
            review_id="r",
            company_id=1,
            review_version=1,
            treatment_type=AccountingTreatmentType.EXPENSE_ACCOUNT,
            expense_account_id=EXPENSE_ACCOUNT,
            expense_category="IT",
            depreciation_model_id=MODEL_GLOBAL,
        )


def test_database_check_constraint_rejects_mixed_rows(session: Session) -> None:
    session.add(
        WorkbenchReviewItem(
            review_id="r-db",
            company_id=COMPANY_ID,
            invoice_id="e",
            invoice_number="n",
            supplier_tax_number=VAT,
            supplier_name="s",
            invoice_date=date(2026, 9, 30),
            currency="TRY",
            total_amount=Decimal("1"),
            workflow="manual_review",
            status="pending_review",
            review_reasons=[],
            warnings=[],
            version=1,
            idempotency_key="k",
        )
    )
    session.flush()
    session.add(
        WorkbenchReviewAccountingResolution(
            review_id="r-db",
            company_id=COMPANY_ID,
            review_version=1,
            treatment_type="capitalize_fixed_asset",
            asset_account_id=ASSET_ACCOUNT,
            depreciation_model_id=MODEL_GLOBAL,
            expense_account_id=EXPENSE_ACCOUNT,
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()


def test_historical_expense_resolution_rows_stay_readable(session: Session) -> None:
    repository = SqlAlchemyReviewAccountingResolutionRepository(session)
    session.add(
        WorkbenchReviewItem(
            review_id="r-hist",
            company_id=COMPANY_ID,
            invoice_id="e",
            invoice_number="n",
            supplier_tax_number=VAT,
            supplier_name="s",
            invoice_date=date(2026, 9, 30),
            currency="TRY",
            total_amount=Decimal("1"),
            workflow="manual_review",
            status="pending_review",
            review_reasons=[],
            warnings=[],
            version=1,
            idempotency_key="k-hist",
        )
    )
    session.add(
        WorkbenchReviewAccountingResolution(
            review_id="r-hist",
            company_id=COMPANY_ID,
            review_version=1,
            treatment_type="expense_account",
            expense_account_id=EXPENSE_ACCOUNT,
            expense_category="IT_HARDWARE_INTERNAL",
        )
    )
    session.commit()
    resolution = repository.find_accounting_resolution(review_id="r-hist", company_id=COMPANY_ID, review_version=1)
    assert resolution.treatment_type is AccountingTreatmentType.EXPENSE_ACCOUNT
    assert (resolution.expense_account_id, resolution.expense_category) == (EXPENSE_ACCOUNT, "IT_HARDWARE_INTERNAL")
    assert (resolution.asset_account_id, resolution.depreciation_model_id) == (None, None)


# =================================================================== evidence + builder units


def _source(invoice: InternalInvoice, **overrides) -> ExecutionSourceInvoice:
    values = {
        "review_id": "r",
        "company_id": COMPANY_ID,
        "decision_version": 3,
        "source_invoice_id": invoice.header.ettn,
        "invoice": invoice,
        "partner_match": _partner_matched(),
        "product_match": _products_invalid_input(invoice),
        "tax_match": _taxes(invoice),
        "fixed_asset_accounting": FixedAssetAccounting(
            accounting_resolution_id=7, asset_account_id=ASSET_ACCOUNT, depreciation_model_id=MODEL_GLOBAL
        ),
    }
    values.update(overrides)
    return ExecutionSourceInvoice(**values)


def test_evidence_round_trip_and_absent_key_for_non_fixed_asset_payloads() -> None:
    invoice = _invoice(ettn="FA-U-1")
    source = _source(invoice)
    payload = serialize_execution_source_invoice_payload(source)
    assert payload["fixed_asset_accounting"]["depreciation_model_id"] == MODEL_GLOBAL
    assert deserialize_execution_source_invoice_payload(payload) == source
    plain = serialize_execution_source_invoice_payload(replace(source, fixed_asset_accounting=None))
    assert "fixed_asset_accounting" not in plain  # historical payloads/fingerprints unchanged


def test_fixed_asset_evidence_contradictions_fail_closed() -> None:
    invoice = _invoice(ettn="FA-U-2")
    with pytest.raises(ExecutionPlanningError):
        _source(
            invoice,
            partner_match=replace(_partner_matched(), status=PartnerMatchStatus.NOT_FOUND, partner_id=None),
        )
    with pytest.raises(FixedAssetAccountingContractError):
        fixed_asset_accounting_from_data({"schema_version": 1, "asset_account_id": 1})
    with pytest.raises(FixedAssetAccountingContractError):
        FixedAssetAccounting(accounting_resolution_id=1, asset_account_id=0, depreciation_model_id=1)
    data = fixed_asset_accounting_to_data(
        FixedAssetAccounting(accounting_resolution_id=1, asset_account_id=2, depreciation_model_id=3)
    )
    assert fixed_asset_accounting_from_data(data) == FixedAssetAccounting(
        accounting_resolution_id=1, asset_account_id=2, depreciation_model_id=3
    )


def test_builder_rejects_fixed_asset_combined_with_other_modes() -> None:
    invoice = _invoice(ettn="FA-U-3")
    source = _source(invoice)
    fixed = source.fixed_asset_accounting
    common = (invoice, source.partner_match, source.product_match, source.tax_match)
    assert validate_vendor_bill_inputs(*common, fixed_asset_accounting=fixed).is_valid
    assert not validate_vendor_bill_inputs(
        *common, fixed_asset_accounting=fixed, account_only_line_numbers=frozenset({"000001"})
    ).is_valid
    with_code = replace(invoice, lines=(replace(invoice.lines[0], buyer_item_code="SKU-1"), invoice.lines[1]))
    assert not validate_vendor_bill_inputs(
        with_code, source.partner_match, source.product_match, source.tax_match, fixed_asset_accounting=fixed
    ).is_valid
    unlabeled = replace(invoice, lines=(replace(invoice.lines[0], description="  "), invoice.lines[1]))
    result = validate_vendor_bill_inputs(
        unlabeled, source.partner_match, source.product_match, source.tax_match, fixed_asset_accounting=fixed
    )
    assert "lines[0].description is required for a fixed-asset line." in result.errors


def test_depreciation_model_only_on_account_only_lines() -> None:
    with pytest.raises(ValueError):
        VendorBillLine(product_id=5, quantity=Decimal("1"), unit_price=Decimal("1"), depreciation_model_id=3)
    with pytest.raises(ValueError):
        VendorBillLine(product_id=None, quantity=Decimal("1"), unit_price=Decimal("1"), depreciation_model_id=3)
    line = VendorBillLine(
        product_id=None, quantity=Decimal("1"), unit_price=Decimal("1"), account_id=74, depreciation_model_id=3
    )
    assert line.depreciation_model_id == 3


def test_expense_payload_is_unchanged_and_carries_no_depreciation_model() -> None:
    from app.application.expense_mapping import OperatingExpenseMatchResult, OperatingExpenseMatchStatus

    invoice = _invoice(ettn="FA-U-4")
    match = OperatingExpenseMatchResult(
        status=OperatingExpenseMatchStatus.MATCHED,
        reason="r",
        candidate_count=1,
        mapping_id=1,
        company_id=COMPANY_ID,
        vendor_partner_id=PARTNER_ID,
        expense_account_id=EXPENSE_ACCOUNT,
        expense_category="IT",
        matched_by="review_accounting_resolution",
        confidence=Decimal("1.00"),
    )
    bill = VendorBillBuilder().build(
        invoice,
        _partner_matched(),
        _products_invalid_input(invoice),
        _taxes(invoice),
        company_id=COMPANY_ID,
        operating_expense_match=match,
    )
    lines = [
        c[2]
        for c in to_odoo_account_move_payload(bill, currency_id=CURRENCY_ID, product_uom_ids={})["invoice_line_ids"]
    ]
    assert lines == [
        {key: value for key, value in expected.items() if key != "depreciation_model_id"} | {"account_id": 247}
        for expected in EXPECTED_ASSET_LINES
    ]


def test_hub_never_creates_assets_or_posts() -> None:
    from app.connectors.odoo.client import READ_ONLY_MODELS, OdooJson2Client

    for path in Path("app").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "account.asset/" not in text, path  # no account.asset create/write route anywhere
        assert "/action_post" not in text and ".action_post(" not in text, path  # never posts
    assert "account.asset" not in READ_ONLY_MODELS  # only the depreciation model is readable
    assert "account.depreciation.model" in READ_ONLY_MODELS
    assert not any("asset" in name for name in dir(OdooJson2Client))


# =================================================================== execution strategy


def test_execution_strategy_sends_the_frozen_asset_lines_and_retry_is_identical() -> None:
    from app.application.execution import ExecutionApproval, ExecutionMode, ExecutionStep, ExecutionStepRequest
    from app.application.execution.contracts import ExecutionStepType
    from app.application.execution.vendor_bill_strategy import VendorBillExecutionStrategy
    from tests.unit.resale_execution_support import NON_RESALE_ACCOUNTING_CHECK, TWO_DECIMAL_CURRENCY_READER

    invoice = _invoice(ettn="FA-S-1")
    source = _source(invoice)

    class _Reader:
        def get_source_invoice(self, *, review_id, company_id, decision_version):
            return source

    class _Writer:
        def __init__(self) -> None:
            self.commands = []

        async def write_vendor_bill(self, command):
            from app.application.dto import VendorBillWriteResult

            self.commands.append(command)
            return VendorBillWriteResult(
                status="dry_run", idempotency_key=command.idempotency_key, safe_message="ok", success=True
            )

    writer = _Writer()
    strategy = VendorBillExecutionStrategy(
        source_invoice_reader=_Reader(),
        vendor_bill_builder=VendorBillBuilder(),
        vendor_bill_writer=writer,
        resale_accounting_check=NON_RESALE_ACCOUNTING_CHECK,
        currency_reader=TWO_DECIMAL_CURRENCY_READER,
    )
    request = ExecutionStepRequest(
        execution_id="execution-fa",
        review_id="r",
        company_id=COMPANY_ID,
        decision_version=3,
        mode=ExecutionMode.DRY_RUN,
        step=ExecutionStep(
            step_key="r:3:vendor_bill:workflow",
            step_type=ExecutionStepType.VENDOR_BILL,
            allocation_keys=(),
            sequence=1,
            execute_supported=True,
        ),
        approval=ExecutionApproval(approved_by=ACTOR),
    )
    strategy.execute(request)
    strategy.execute(request)

    first, second = (command.vendor_bill for command in writer.commands)
    assert first == second  # retry/resume reproduces the exact same draft bill
    assert [(line.account_id, line.depreciation_model_id, line.tax_ids) for line in first.invoice_lines] == [
        (ASSET_ACCOUNT, MODEL_GLOBAL, (TAX_ID,)),
        (ASSET_ACCOUNT, MODEL_GLOBAL, (TAX_ID,)),
    ]


# =================================================================== Odoo read-only reader


class _FakeJson2:
    def __init__(
        self,
        *,
        records,
        fields=(
            "can_create_asset",
            "active",
            "company_id",
            "asset_depreciation_account_id",
            "asset_expense_account_id",
        ),
    ) -> None:
        self.records = records
        self.fields = set(fields)
        self.calls: list[tuple[str, str]] = []

    async def search_read(self, *, model, domain, fields, limit=20, offset=0):
        self.calls.append(("search_read", model))
        assert ["active", "in", [True, False]] in domain
        return [dict(r) for r in self.records.get(model, []) if r["id"] == domain[0][2]]

    async def read_model_field_metadata(self, *, model, field_name):
        self.calls.append(("metadata", f"{model}.{field_name}"))
        return [{"name": field_name}] if field_name in self.fields else []


def _reader(client):
    from app.erp.odoo.adapter import OdooReadOnlyAdapter
    from app.erp.odoo.fixed_asset_accounting_reader import OdooFixedAssetAccountingReader

    return OdooFixedAssetAccountingReader(adapter=OdooReadOnlyAdapter(client=client, retry_backoff_seconds=0))


ODOO_ACCOUNT = {
    "id": 74,
    "code": "255000",
    "name": "Furniture And Fixtures",
    "account_type": "asset_fixed",
    "active": True,
    "company_ids": [1],
    "can_create_asset": True,
    "asset_depreciation_account_id": [76, "257000 Accumulated Depreciation"],
    "asset_expense_account_id": False,
}
ODOO_MODEL = {
    "id": 3,
    "display_name": "5 Year Linear",
    "active": True,
    "company_id": False,
    "method": "linear",
    "method_number": 5.0,
    "method_period": "12",
}


def test_odoo_reader_reads_account_and_model_only_via_search_read() -> None:
    client = _FakeJson2(records={"account.account": [ODOO_ACCOUNT], "account.depreciation.model": [ODOO_MODEL]})
    reader = _reader(client)
    account = reader.read_account(account_id=74)
    model = reader.read_depreciation_model(model_id=3)
    assert (account.account_type, account.can_create_asset, account.company_ids) == ("asset_fixed", True, (1,))
    assert (
        account.asset_posting_accounts_supported,
        account.asset_depreciation_account_id,
        account.asset_expense_account_id,
    ) == (True, 76, None)
    assert (model.company_id, model.method, model.method_number, model.active) == (None, "linear", 5.0, True)
    assert reader.read_account(account_id=999) is None
    assert {kind for kind, _ in client.calls} == {"search_read", "metadata"}


def test_odoo_reader_without_can_create_asset_field_reports_none() -> None:
    record = {k: v for k, v in ODOO_ACCOUNT.items() if k != "can_create_asset"}
    client = _FakeJson2(records={"account.account": [record]}, fields=("active", "company_id"))
    assert _reader(client).read_account(account_id=74).can_create_asset is None


def test_odoo_reader_without_asset_posting_account_fields_reports_unsupported() -> None:
    record = {
        k: v for k, v in ODOO_ACCOUNT.items() if k not in ("asset_depreciation_account_id", "asset_expense_account_id")
    }
    client = _FakeJson2(records={"account.account": [record]}, fields=("can_create_asset", "active", "company_id"))
    account = _reader(client).read_account(account_id=74)
    assert (account.asset_posting_accounts_supported, account.asset_depreciation_account_id) == (False, None)


def test_odoo_reader_without_depreciation_model_support_fails_closed() -> None:
    client = _FakeJson2(records={}, fields=())
    with pytest.raises(FixedAssetAccountingUnavailableError):
        _reader(client).read_depreciation_model(model_id=3)


# =================================================================== API


def _api(captured: list):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import dependencies
    from app.api.error_handling import install_api_exception_handlers
    from app.api.routers.workbench import router
    from app.api.security import AuthenticationMethod, Permission, RequestContext
    from app.application.workbench.accounting_resolution import (
        AccountingResolutionStatus,
        ReviewAccountingResolutionSubmissionResult,
    )

    class _UseCase:
        async def execute(self, command):
            captured.append(command)
            return ReviewAccountingResolutionSubmissionResult(
                review_id=command.review_id,
                company_id=command.company_id,
                status=AccountingResolutionStatus.RESOLVED,
                previous_version=1,
                current_version=2,
                current_workflow=WorkflowType.VENDOR_BILL,
                treatment_type=command.treatment_type,
                asset_account_id=command.asset_account_id,
                depreciation_model_id=command.depreciation_model_id,
                expense_account_id=command.expense_account_id,
                expense_category=command.expense_category,
                reclassified=True,
            )

    app = FastAPI()
    install_api_exception_handlers(app)
    app.include_router(router)
    app.dependency_overrides[dependencies.get_submit_review_accounting_resolution_use_case] = lambda: _UseCase()
    app.dependency_overrides[dependencies.get_request_context] = lambda: RequestContext(
        user_id="op",
        user_name="Operator",
        company_id=COMPANY_ID,
        permissions=(Permission.WORKBENCH_REVIEW_DECIDE,),
        trace_id="t",
        authentication_method=AuthenticationMethod.JWT,
    )
    return TestClient(app)


def test_api_accepts_capitalize_fixed_asset_and_returns_the_selection() -> None:
    captured: list = []
    with _api(captured) as client:
        response = client.post(
            "/api/workbench/reviews/review:x/accounting-resolution",
            json={
                "expected_version": 1,
                "treatment_type": "capitalize_fixed_asset",
                "asset_account_id": ASSET_ACCOUNT,
                "depreciation_model_id": MODEL_GLOBAL,
                "note": "company equipment",
            },
        )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert (data["treatment_type"], data["asset_account_id"], data["depreciation_model_id"]) == (
        "capitalize_fixed_asset",
        ASSET_ACCOUNT,
        MODEL_GLOBAL,
    )
    assert (data["expense_account_id"], data["expense_category"]) == (None, None)
    assert captured[0].approved_by == "Operator"  # never from the body


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"treatment_type": "capitalize_fixed_asset", "asset_account_id": ASSET_ACCOUNT}, 400),  # model missing
        (
            {
                "treatment_type": "capitalize_fixed_asset",
                "asset_account_id": ASSET_ACCOUNT,
                "depreciation_model_id": MODEL_GLOBAL,
                "expense_account_id": EXPENSE_ACCOUNT,
            },
            400,
        ),
        # unknown treatment / identity injection: request validation (mapped to 400 here)
        ({"treatment_type": "inventory_resale", "asset_account_id": ASSET_ACCOUNT}, 400),
        ({"treatment_type": "capitalize_fixed_asset", "asset_account_id": 74, "company_id": 2}, 400),
    ],
)
def test_api_rejects_ambiguous_or_unknown_payloads(body: dict, status: int) -> None:
    captured: list = []
    with _api(captured) as client:
        response = client.post(
            "/api/workbench/reviews/review:x/accounting-resolution", json={"expected_version": 1, **body}
        )
    assert response.status_code == status, response.text
    assert captured == []


def test_api_maps_fixed_asset_errors() -> None:
    from app.api.routers.workbench import _status_code_for_exception

    assert _status_code_for_exception(FixedAssetAccountInvalidError("x")) == 400
    assert _status_code_for_exception(DepreciationModelInvalidError("x")) == 400
    assert _status_code_for_exception(FixedAssetAccountingUnavailableError("x")) == 503
