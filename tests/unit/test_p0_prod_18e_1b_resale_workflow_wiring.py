"""P0-PROD-18E-1B: RESALE purchase-purpose eligibility and decision-acceptance gate.

Covers:
  * purpose: RESALE is accepted for a product-shaped review (any line with a buyer
    item code, seller item code or barcode) without a product match or an Odoo read,
    and rejected for an identifier-free (operating-expense-shaped) review; the
    INTERNAL_USE / OTHER_OPERATING_EXPENSE rules are unchanged;
  * decision: a fresh Vendor Bill decision whose current-version purpose is RESALE
    must be fully product-backed, and every product must pass the P0-PROD-18E-1A
    policy over P0-PROD-18D discovery evidence -- through the real
    ``SubmitReviewDecisionUseCase`` and SQLite persistence;
  * nothing is pinned, no account is added to the Vendor Bill, fiscal positions stay
    unevaluated, and both production compositions wire the gate.
"""

from __future__ import annotations

from dataclasses import fields
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.application.workbench import (
    LineResolution,
    ReviewDecisionCommand,
    ReviewDecisionType,
    ReviewExecutionEvidence,
    ReviewItem,
    ReviewStatus,
    SubmitReviewDecisionUseCase,
)
from app.application.workbench.evidence import ReviewSourceInvoiceEvidence
from app.application.workbench.exceptions import (
    PurchaseAccountCompanyContextError,
    PurchaseAccountProductNotFoundError,
    PurchasePurposeConflictError,
    PurchasePurposeEligibilityError,
    ResaleDecisionEligibilityError,
    ReviewDecisionError,
    ReviewVersionConflictError,
)
from app.application.workbench.purchase_account_discovery import (
    CategoryPurchaseAccountRecord,
    FiscalPositionMapping,
    GetProductPurchaseAccountQuery,
    ProductPurchaseAccountRecord,
    ProductPurchaseAccountResolution,
    PurchaseAccountRecord,
    resolve_product_purchase_account,
)
from app.application.workbench.purchase_purpose import (
    PurchasePurpose,
    PurchasePurposeResolution,
    SubmitPurchasePurposeCommand,
)
from app.application.workbench.purchase_purpose_use_cases import SubmitPurchasePurposeUseCase
from app.application.workbench.resale_decision_gate import ResaleDecisionGate
from app.application.workbench.selected_product_resolution import ResolutionProductRecord
from app.application.workflow import ManualReviewReason, ManualReviewReasonCode, WorkflowType
from app.billing import VendorBillBuilder
from app.db.base import Base
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
from app.models.workbench_review_decision import WorkbenchReviewDecision
from app.models.workbench_review_execution_evidence import WorkbenchReviewExecutionEvidence
from app.models.workbench_review_item import WorkbenchReviewItem
from app.models.workbench_review_purchase_purpose_resolution import WorkbenchReviewPurchasePurposeResolution
from app.persistence import (
    SqlAlchemyExecutionSourceInvoiceReader,
    SqlAlchemyReviewPurchasePurposeResolutionRepository,
    SqlAlchemyReviewRepository,
    SqlAlchemyUnitOfWork,
)
from app.persistence.review_execution_evidence_reader import SqlAlchemyReviewExecutionEvidenceReader
from app.tax_mapping import InvoiceTaxLineResult, InvoiceTaxMappingResult, TaxMatchResult, TaxMatchStatus, TaxType

COMPANY_ID = 1
OTHER_COMPANY_ID = 2
REVIEW_ID = "review-resale-1"
SOURCE_INVOICE_ID = "uuid-resale-1"
TAX_ID = 401
PRODUCT_A = 501
PRODUCT_B = 502
PARENT_CATEGORY_ID = 5
CHILD_CATEGORY_ID = 7
#: Deliberately arbitrary: nothing may depend on a specific account.
ACCOUNT_ID = 987
ACCOUNT_CODE = "SOME_FUTURE_ACCOUNT"
OVERRIDE_ACCOUNT_ID = 654
VITEL_SELLER_CODE = "1531012114"
LOGOSOFT_SELLER_CODE = "CFQ7TTC0LH18:0001"

# --------------------------------------------------------------------------- invoice builders


def _line(
    line_number: str,
    *,
    seller_item_code: str | None = None,
    buyer_item_code: str | None = None,
    barcode: str | None = None,
) -> InvoiceLine:
    return InvoiceLine(
        line_number=line_number,
        description=f"Line {line_number}",
        seller_item_code=seller_item_code,
        buyer_item_code=buyer_item_code,
        barcode=barcode,
        quantity=Decimal("1"),
        unit_code="C62",
        unit_price=Decimal("50.00"),
        taxes=(Tax(tax_type="KDV", rate=Decimal("20")),),
    )


def _invoice(lines: list[InvoiceLine]) -> InternalInvoice:
    return InternalInvoice(
        header=Header(
            invoice_number="VTL2026000003029",
            invoice_uuid=SOURCE_INVOICE_ID,
            ettn=SOURCE_INVOICE_ID,
            issue_date=date(2026, 9, 1),
            currency_code="USD",
        ),
        supplier=Party(name="Resale Supplier", tax_number="9250026000"),
        customer=Party(name="ICT", tax_number="4651205941"),
        totals=MonetaryTotals(payable_amount=Decimal("50.00") * len(lines)),
        lines=tuple(lines),
    )


def _partner() -> PartnerMatchResult:
    return PartnerMatchResult(
        status=PartnerMatchStatus.MATCHED,
        partner_id=101,
        matched_by="tax_number",
        reason="matched",
        candidate_count=1,
        confidence=Decimal("1.00"),
    )


def _product_line(
    line_number: str, status: ProductMatchStatus, *, product_id: int | None = None
) -> InvoiceProductLineResult:
    matched = status is ProductMatchStatus.MATCHED
    return InvoiceProductLineResult(
        line_number=line_number,
        result=ProductMatchResult(
            status=status,
            line_number=line_number,
            product_id=product_id,
            default_code=None,
            barcode=None,
            seller_item_code=None,
            matched_by="default_code" if matched else None,
            reason="matched" if matched else "No Odoo product found.",
            candidate_count=1 if matched else (2 if status is ProductMatchStatus.MULTIPLE_MATCHES else 0),
            confidence=Decimal("1.00") if matched else None,
        ),
    )


def _taxes(invoice: InternalInvoice) -> InvoiceTaxMappingResult:
    return InvoiceTaxMappingResult(
        line_results=tuple(
            InvoiceTaxLineResult(
                line_number=line.line_number,
                tax_index=tax_index,
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
            for tax_index, _tax in enumerate(line.taxes)
        )
    )


# --------------------------------------------------------------------------- 18D evidence builders


def _account(
    id: int = ACCOUNT_ID, *, code: str = ACCOUNT_CODE, account_type: str = "some_future_type", **kw: Any
) -> PurchaseAccountRecord:
    values: dict[str, Any] = {"company_ids": (COMPANY_ID,), "deprecated": False}
    values.update(kw)
    return PurchaseAccountRecord(id=id, code=code, name=f"Account {code}", account_type=account_type, **values)


def _discovery(
    product_id: int,
    *,
    category_id: int = CHILD_CATEGORY_ID,
    category_account_id: int | None = ACCOUNT_ID,
    accounts: tuple[PurchaseAccountRecord, ...] | None = None,
    **product_overrides: Any,
) -> ProductPurchaseAccountResolution:
    product_values: dict[str, Any] = {
        "product_id": product_id,
        "product_template_id": product_id + 1000,
        "name": f"Product {product_id}",
        "active": True,
        "company_id": COMPANY_ID,
        "product_type": "service",
        "category_id": category_id,
        "override_account_id": None,
        "is_storable": False,
    }
    product_values.update(product_overrides)
    category = CategoryPurchaseAccountRecord(
        id=category_id, name=f"Category {category_id}", expense_account_id=category_account_id
    )
    account_records = accounts if accounts is not None else (_account(),)
    return resolve_product_purchase_account(
        ProductPurchaseAccountRecord(**product_values),
        category=category,
        company_id=COMPANY_ID,
        accounts_by_id={account.id: account for account in account_records},
    )


class _FakeProductAccountResolver:
    """Stands in for P0-PROD-18D's ``GetProductPurchaseAccountUseCase``; records calls."""

    def __init__(self, by_product: dict[int, ProductPurchaseAccountResolution | Exception]) -> None:
        self._by_product = by_product
        self.queries: list[GetProductPurchaseAccountQuery] = []

    def execute(self, query: GetProductPurchaseAccountQuery) -> ProductPurchaseAccountResolution:
        self.queries.append(query)
        outcome = self._by_product.get(query.product_id)
        if outcome is None:
            raise PurchaseAccountProductNotFoundError("The product was not found for this company.")
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _StubSelectedProductReader:
    def __init__(self, *product_ids: int) -> None:
        self._by_id = {
            product_id: ResolutionProductRecord(
                id=product_id, name=f"P{product_id}", default_code=None, barcode=None, active=True, company_id=None
            )
            for product_id in product_ids
        }

    def find_products_by_ids(self, product_ids: tuple[int, ...]) -> tuple[ResolutionProductRecord, ...]:
        return tuple(self._by_id[product_id] for product_id in product_ids if product_id in self._by_id)


# =================================================================== purpose (fakes, no Odoo)


class _FakeReviewReader:
    def __init__(self, review: ReviewItem) -> None:
        self.review = review

    def get_review_item(self, query) -> ReviewItem:
        return self.review


class _FakeSourceReader:
    def __init__(self, invoice: InternalInvoice) -> None:
        self.invoice = invoice

    def get(self, *, review_id: str, company_id: int) -> ReviewSourceInvoiceEvidence:
        return ReviewSourceInvoiceEvidence(
            review_id=review_id,
            company_id=company_id,
            review_version=1,
            source_invoice_id=SOURCE_INVOICE_ID,
            invoice=self.invoice,
        )


class _FakePurposeWriter:
    def __init__(self) -> None:
        self.rows: dict[int, PurchasePurposeResolution] = {}

    def create_purchase_purpose_resolution(self, resolution: PurchasePurposeResolution) -> PurchasePurposeResolution:
        self.rows[resolution.review_version] = resolution
        return resolution

    def find_purchase_purpose_resolution(
        self, *, review_id: str, company_id: int, review_version: int
    ) -> PurchasePurposeResolution | None:
        return self.rows.get(review_version)


class _FakeUnitOfWork:
    def __init__(self) -> None:
        self.commits = 0

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        pass


def _reason(code: ManualReviewReasonCode) -> ManualReviewReason:
    return ManualReviewReason(code=code, message=code.value)


def _review(*reasons: ManualReviewReasonCode, version: int = 1) -> ReviewItem:
    return ReviewItem(
        review_id=REVIEW_ID,
        invoice_id=SOURCE_INVOICE_ID,
        invoice_number="VTL2026000003029",
        supplier_tax_number="9250026000",
        supplier_name="Resale Supplier",
        invoice_date=date(2026, 9, 1),
        currency="USD",
        total_amount=Decimal("50.00"),
        workflow=WorkflowType.VENDOR_BILL,
        status=ReviewStatus.PENDING_REVIEW,
        review_reasons=tuple(_reason(code) for code in reasons),
        version=version,
    )


def _purpose_setup(
    invoice: InternalInvoice, *reasons: ManualReviewReasonCode, version: int = 1
) -> tuple[SubmitPurchasePurposeUseCase, _FakePurposeWriter, _FakeUnitOfWork]:
    writer = _FakePurposeWriter()
    unit_of_work = _FakeUnitOfWork()
    use_case = SubmitPurchasePurposeUseCase(
        review_reader=_FakeReviewReader(_review(*reasons, version=version)),
        source_invoice_reader=_FakeSourceReader(invoice),
        purpose_writer=writer,
        unit_of_work=unit_of_work,
    )
    return use_case, writer, unit_of_work


def _purpose_command(purpose: PurchasePurpose, *, expected_version: int = 1) -> SubmitPurchasePurposeCommand:
    return SubmitPurchasePurposeCommand(
        review_id=REVIEW_ID,
        company_id=COMPANY_ID,
        expected_version=expected_version,
        purchase_purpose=purpose,
        approved_by="operator",
    )


PRODUCT_REASONS = (ManualReviewReasonCode.PRODUCT_NOT_FOUND,)
EXPENSE_REASONS = (ManualReviewReasonCode.OPERATING_EXPENSE_MAPPING_REQUIRED,)


@pytest.mark.parametrize(
    "line",
    [
        _line("1", seller_item_code=VITEL_SELLER_CODE),
        _line("1", seller_item_code=LOGOSOFT_SELLER_CODE),
        _line("1", buyer_item_code="ICT-001"),
        _line("1", barcode="8690000000001"),
    ],
    ids=["vitel-seller-code", "logosoft-seller-sku", "buyer-code", "barcode"],
)
def test_resale_purpose_accepted_for_product_shaped_review(line: InvoiceLine) -> None:
    use_case, writer, unit_of_work = _purpose_setup(_invoice([line]), *PRODUCT_REASONS)

    result = use_case.execute(_purpose_command(PurchasePurpose.RESALE))

    assert result.purchase_purpose is PurchasePurpose.RESALE
    assert result.already_applied is False
    assert writer.rows[1].purchase_purpose is PurchasePurpose.RESALE
    assert unit_of_work.commits == 1


def test_resale_purpose_needs_only_one_identified_line_and_no_product_match() -> None:
    invoice = _invoice([_line("1"), _line("2", seller_item_code=VITEL_SELLER_CODE)])
    # Supplier and product both unresolved: RESALE is still just a statement of purpose.
    use_case, writer, _ = _purpose_setup(
        invoice, ManualReviewReasonCode.SUPPLIER_NOT_FOUND, ManualReviewReasonCode.PRODUCT_NOT_FOUND
    )
    use_case.execute(_purpose_command(PurchasePurpose.RESALE))
    assert writer.rows[1].purchase_purpose is PurchasePurpose.RESALE


@pytest.mark.parametrize("reasons", [EXPENSE_REASONS, PRODUCT_REASONS, ()])
def test_resale_purpose_rejected_for_identifier_free_review(reasons: tuple[ManualReviewReasonCode, ...]) -> None:
    invoice = _invoice([_line("1", seller_item_code="  "), _line("2")])
    use_case, writer, unit_of_work = _purpose_setup(invoice, *reasons)

    with pytest.raises(PurchasePurposeEligibilityError, match="product-shaped"):
        use_case.execute(_purpose_command(PurchasePurpose.RESALE))
    assert writer.rows == {}
    assert unit_of_work.commits == 0


def test_resale_purpose_rejected_for_invoice_without_lines() -> None:
    use_case, writer, _ = _purpose_setup(_invoice([]), *PRODUCT_REASONS)
    with pytest.raises(PurchasePurposeEligibilityError):
        use_case.execute(_purpose_command(PurchasePurpose.RESALE))
    assert writer.rows == {}


@pytest.mark.parametrize("purpose", [PurchasePurpose.INTERNAL_USE, PurchasePurpose.OTHER_OPERATING_EXPENSE])
def test_non_resale_purposes_still_require_an_operating_expense_reason(purpose: PurchasePurpose) -> None:
    product_shaped = _invoice([_line("1", seller_item_code=VITEL_SELLER_CODE)])
    use_case, writer, _ = _purpose_setup(product_shaped, *PRODUCT_REASONS)
    with pytest.raises(PurchasePurposeEligibilityError, match="operating-expense-shaped"):
        use_case.execute(_purpose_command(purpose))
    assert writer.rows == {}

    expense_shaped = _invoice([_line("1")])
    use_case, writer, _ = _purpose_setup(expense_shaped, *EXPENSE_REASONS)
    assert use_case.execute(_purpose_command(purpose)).purchase_purpose is purpose


def test_resale_purpose_stale_version_fails_as_before() -> None:
    use_case, writer, _ = _purpose_setup(_invoice([_line("1", seller_item_code=VITEL_SELLER_CODE)]), version=2)
    with pytest.raises(ReviewVersionConflictError):
        use_case.execute(_purpose_command(PurchasePurpose.RESALE, expected_version=3))
    with pytest.raises(ReviewVersionConflictError):
        use_case.execute(_purpose_command(PurchasePurpose.RESALE, expected_version=1))
    assert writer.rows == {}


def test_resale_purpose_replay_and_conflict_behave_as_before() -> None:
    use_case, writer, _ = _purpose_setup(_invoice([_line("1", seller_item_code=VITEL_SELLER_CODE)]), *PRODUCT_REASONS)
    use_case.execute(_purpose_command(PurchasePurpose.RESALE))
    assert use_case.execute(_purpose_command(PurchasePurpose.RESALE)).already_applied is True
    # A non-RESALE purpose is still judged by the unchanged operating-expense rule first.
    with pytest.raises(PurchasePurposeEligibilityError):
        use_case.execute(_purpose_command(PurchasePurpose.INTERNAL_USE))
    assert writer.rows[1].purchase_purpose is PurchasePurpose.RESALE


def test_resale_after_another_purpose_conflicts_before_shape_check() -> None:
    use_case, writer, _ = _purpose_setup(_invoice([_line("1")]), *EXPENSE_REASONS)
    use_case.execute(_purpose_command(PurchasePurpose.INTERNAL_USE))
    with pytest.raises(PurchasePurposeConflictError):
        use_case.execute(_purpose_command(PurchasePurpose.RESALE))
    assert writer.rows[1].purchase_purpose is PurchasePurpose.INTERNAL_USE


def test_recording_resale_purpose_composes_no_odoo_client() -> None:
    import inspect

    from app.composition.purchase_purpose_and_accounting_resolution import build_submit_purchase_purpose_use_case

    assert list(inspect.signature(build_submit_purchase_purpose_use_case).parameters) == ["session"]


# =================================================================== decision (real use case, SQLite)


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
        ],
    )
    factory = sessionmaker(bind=engine)
    with factory() as db_session:
        yield db_session


def _seed_review(
    session: Session,
    *,
    lines: list[InvoiceLine] | None = None,
    product_results: list[InvoiceProductLineResult] | None = None,
) -> None:
    invoice = _invoice(lines or [_line("1", seller_item_code=VITEL_SELLER_CODE)])
    evidence = ReviewExecutionEvidence(
        review_id=REVIEW_ID,
        company_id=COMPANY_ID,
        review_version=1,
        source_invoice_id=SOURCE_INVOICE_ID,
        invoice=invoice,
        partner_match=_partner(),
        product_match=InvoiceProductMatchResult(
            line_results=tuple(product_results or [_product_line("1", ProductMatchStatus.NOT_FOUND)])
        ),
        tax_match=_taxes(invoice),
    )
    SqlAlchemyReviewRepository(session).create_review_item_with_execution_evidence(
        _review(*PRODUCT_REASONS), company_id=COMPANY_ID, idempotency_key="review-key-1", evidence=evidence
    )


def _record_purpose(session: Session, purpose: PurchasePurpose, *, review_version: int = 1) -> None:
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


def _decision(*line_resolutions: LineResolution, key: str = "decision:resale") -> ReviewDecisionCommand:
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


def _select(line_number: str = "1", product_id: int = PRODUCT_A) -> LineResolution:
    return LineResolution(line_number=line_number, selected_product_id=product_id)


def _use_case(
    session: Session,
    resolver: _FakeProductAccountResolver,
    *,
    approved: frozenset[int] = frozenset({CHILD_CATEGORY_ID}),
    selectable: tuple[int, ...] = (PRODUCT_A, PRODUCT_B),
    gate: bool = True,
) -> SubmitReviewDecisionUseCase:
    return SubmitReviewDecisionUseCase(
        review_decision_writer=SqlAlchemyReviewRepository(session),
        unit_of_work=SqlAlchemyUnitOfWork(session),
        execution_evidence_reader=SqlAlchemyReviewExecutionEvidenceReader(session),
        selected_product_reader=_StubSelectedProductReader(*selectable),
        resale_decision_gate=(
            ResaleDecisionGate(
                purpose_reader=SqlAlchemyReviewPurchasePurposeResolutionRepository(session),
                product_account_resolver=resolver,
                approved_category_ids=approved,
            )
            if gate
            else None
        ),
    )


def _assert_rejected(session: Session, use_case: SubmitReviewDecisionUseCase, command, *fragments: str) -> None:
    with pytest.raises(ResaleDecisionEligibilityError) as excinfo:
        use_case.execute(command)
    for fragment in fragments:
        assert fragment in str(excinfo.value)
    assert session.query(WorkbenchReviewDecision).count() == 0
    assert session.query(ExecutionSourceInvoiceEvidence).count() == 0
    assert session.query(WorkbenchReviewItem).filter_by(review_id=REVIEW_ID).one().version == 1


def _reloaded_source(session: Session):
    return SqlAlchemyExecutionSourceInvoiceReader(session).get_source_invoice(
        review_id=REVIEW_ID, company_id=COMPANY_ID, decision_version=2
    )


def test_resale_decision_with_selected_eligible_product_is_accepted(session: Session) -> None:
    _seed_review(session)
    _record_purpose(session, PurchasePurpose.RESALE)
    resolver = _FakeProductAccountResolver({PRODUCT_A: _discovery(PRODUCT_A)})

    acknowledgement = _use_case(session, resolver).execute(_decision(_select()))

    assert acknowledgement.accepted is True
    assert resolver.queries == [GetProductPurchaseAccountQuery(company_id=COMPANY_ID, product_id=PRODUCT_A)]
    reloaded = _reloaded_source(session)
    assert reloaded.product_match.line_results[0].result.product_id == PRODUCT_A


def test_resale_decision_with_deterministic_match_is_accepted(session: Session) -> None:
    _seed_review(session, product_results=[_product_line("1", ProductMatchStatus.MATCHED, product_id=PRODUCT_B)])
    _record_purpose(session, PurchasePurpose.RESALE)
    resolver = _FakeProductAccountResolver({PRODUCT_B: _discovery(PRODUCT_B)})

    assert _use_case(session, resolver).execute(_decision()).accepted is True
    assert [query.product_id for query in resolver.queries] == [PRODUCT_B]


@pytest.mark.parametrize(
    ("account_id", "code", "account_type"),
    [(987, "SOME_FUTURE_ACCOUNT", "some_future_type"), (4242, "770123", "expense"), (3, "X", "asset_current")],
)
def test_resale_decision_accepts_any_valid_account(session: Session, account_id: int, code: str, account_type) -> None:
    _seed_review(session)
    _record_purpose(session, PurchasePurpose.RESALE)
    discovery = _discovery(
        PRODUCT_A,
        category_account_id=account_id,
        accounts=(_account(account_id, code=code, account_type=account_type),),
    )
    assert _use_case(session, _FakeProductAccountResolver({PRODUCT_A: discovery})).execute(_decision(_select()))


def test_resale_decision_every_line_must_be_eligible(session: Session) -> None:
    _seed_review(
        session,
        lines=[_line("1", seller_item_code=VITEL_SELLER_CODE), _line("2", seller_item_code=LOGOSOFT_SELLER_CODE)],
        product_results=[
            _product_line("1", ProductMatchStatus.NOT_FOUND),
            _product_line("2", ProductMatchStatus.NOT_FOUND),
        ],
    )
    _record_purpose(session, PurchasePurpose.RESALE)
    resolver = _FakeProductAccountResolver(
        {PRODUCT_A: _discovery(PRODUCT_A), PRODUCT_B: _discovery(PRODUCT_B, category_id=PARENT_CATEGORY_ID)}
    )
    use_case = _use_case(session, resolver)
    command = _decision(_select("1", PRODUCT_A), _select("2", PRODUCT_B))

    with pytest.raises(ResaleDecisionEligibilityError) as excinfo:
        use_case.execute(command)
    message = str(excinfo.value)
    assert "line 2 (product 502): category_not_approved_for_resale" in message
    assert "line 1" not in message
    assert session.query(WorkbenchReviewDecision).count() == 0


def test_resale_decision_shared_product_is_checked_once(session: Session) -> None:
    _seed_review(
        session,
        lines=[_line("1", seller_item_code=LOGOSOFT_SELLER_CODE), _line("2", seller_item_code=LOGOSOFT_SELLER_CODE)],
        product_results=[
            _product_line("1", ProductMatchStatus.NOT_FOUND),
            _product_line("2", ProductMatchStatus.NOT_FOUND),
        ],
    )
    _record_purpose(session, PurchasePurpose.RESALE)
    resolver = _FakeProductAccountResolver({PRODUCT_A: _discovery(PRODUCT_A)})
    assert _use_case(session, resolver).execute(_decision(_select("1"), _select("2"))).accepted is True
    assert len(resolver.queries) == 1


@pytest.mark.parametrize(
    ("discovery_kwargs", "approved", "blocker"),
    [
        ({}, frozenset(), "resale_category_allowlist_empty"),
        ({}, frozenset({999}), "category_not_approved_for_resale"),
        ({}, frozenset({PARENT_CATEGORY_ID}), "category_not_approved_for_resale"),
        ({"active": False}, frozenset({CHILD_CATEGORY_ID}), "product_inactive"),
        ({"company_id": OTHER_COMPANY_ID}, frozenset({CHILD_CATEGORY_ID}), "product_company_mismatch"),
        ({"is_storable": True, "product_type": "consu"}, frozenset({CHILD_CATEGORY_ID}), "product_storable"),
        ({"is_storable": None}, frozenset({CHILD_CATEGORY_ID}), "product_storability_unknown"),
        (
            {"override_account_id": OVERRIDE_ACCOUNT_ID, "accounts": (_account(), _account(OVERRIDE_ACCOUNT_ID))},
            frozenset({CHILD_CATEGORY_ID}),
            "product_account_override_configured",
        ),
        ({"category_account_id": None}, frozenset({CHILD_CATEGORY_ID}), "category_account_not_configured"),
        ({"accounts": ()}, frozenset({CHILD_CATEGORY_ID}), "category_account_unavailable"),
        (
            {"accounts": (_account(company_ids=(OTHER_COMPANY_ID,)),)},
            frozenset({CHILD_CATEGORY_ID}),
            "category_account_unavailable",
        ),
        ({"accounts": (_account(deprecated=True),)}, frozenset({CHILD_CATEGORY_ID}), "category_account_deprecated"),
        ({"active": False}, frozenset({CHILD_CATEGORY_ID}), "pre_fiscal_position_account_not_determinable"),
    ],
)
def test_resale_decision_rejects_ineligible_product(
    session: Session, discovery_kwargs: dict[str, Any], approved: frozenset[int], blocker: str
) -> None:
    _seed_review(session)
    _record_purpose(session, PurchasePurpose.RESALE)
    resolver = _FakeProductAccountResolver({PRODUCT_A: _discovery(PRODUCT_A, **discovery_kwargs)})
    _assert_rejected(session, _use_case(session, resolver, approved=approved), _decision(_select()), blocker)


def test_resale_decision_product_not_visible_to_company_rejects(session: Session) -> None:
    _seed_review(session)
    _record_purpose(session, PurchasePurpose.RESALE)
    use_case = _use_case(session, _FakeProductAccountResolver({}))
    _assert_rejected(session, use_case, _decision(_select()), "product_unknown")


def test_resale_decision_unverified_odoo_company_context_fails_closed(session: Session) -> None:
    _seed_review(session)
    _record_purpose(session, PurchasePurpose.RESALE)
    resolver = _FakeProductAccountResolver({PRODUCT_A: PurchaseAccountCompanyContextError("unverified")})
    with pytest.raises(PurchaseAccountCompanyContextError):
        _use_case(session, resolver).execute(_decision(_select()))
    assert session.query(WorkbenchReviewDecision).count() == 0


def test_resale_decision_unexpected_discovery_failure_fails_closed(session: Session) -> None:
    _seed_review(session)
    _record_purpose(session, PurchasePurpose.RESALE)
    resolver = _FakeProductAccountResolver({PRODUCT_A: RuntimeError("boom")})
    with pytest.raises(ReviewDecisionError, match="RESALE product eligibility could not be checked safely"):
        _use_case(session, resolver).execute(_decision(_select()))
    assert session.query(WorkbenchReviewDecision).count() == 0


@pytest.mark.parametrize("status", [ProductMatchStatus.NOT_FOUND, ProductMatchStatus.MULTIPLE_MATCHES])
def test_resale_decision_unresolved_or_ambiguous_product_rejects(session: Session, status) -> None:
    _seed_review(
        session,
        lines=[_line("1", seller_item_code=VITEL_SELLER_CODE), _line("2", seller_item_code=LOGOSOFT_SELLER_CODE)],
        product_results=[_product_line("1", ProductMatchStatus.NOT_FOUND), _product_line("2", status)],
    )
    _record_purpose(session, PurchasePurpose.RESALE)
    resolver = _FakeProductAccountResolver({PRODUCT_A: _discovery(PRODUCT_A)})
    # Line 2 is left unresolved by the operator; the generic completeness check or the
    # RESALE gate must reject it -- never a partial RESALE decision.
    with pytest.raises(ReviewDecisionError):
        _use_case(session, resolver).execute(_decision(_select("1")))
    assert session.query(WorkbenchReviewDecision).count() == 0
    assert resolver.queries == []


def test_resale_decision_rejects_account_only_line(session: Session) -> None:
    _seed_review(
        session,
        lines=[_line("1", seller_item_code=VITEL_SELLER_CODE), _line("2", seller_item_code=LOGOSOFT_SELLER_CODE)],
        product_results=[
            _product_line("1", ProductMatchStatus.NOT_FOUND),
            _product_line("2", ProductMatchStatus.NOT_FOUND),
        ],
    )
    _record_purpose(session, PurchasePurpose.RESALE)
    resolver = _FakeProductAccountResolver({PRODUCT_A: _discovery(PRODUCT_A)})
    command = _decision(_select("1"), LineResolution(line_number="2", account_only=True, expense_account_id=ACCOUNT_ID))

    class _AccountReader:
        def find_accounts_by_ids(self, account_ids):
            from app.application.workbench.selected_expense_account_resolution import ResolutionAccountRecord

            return tuple(
                ResolutionAccountRecord(id=account_id, company_ids=(COMPANY_ID,)) for account_id in account_ids
            )

    use_case = _use_case(session, resolver)
    use_case._selected_account_reader = _AccountReader()
    _assert_rejected(session, use_case, command, "account-only")
    assert resolver.queries == []


def test_resale_decision_rejects_identifier_free_invoice(session: Session) -> None:
    # A RESALE purpose recorded before 18E-1B on an operating-expense-shaped review.
    _seed_review(
        session,
        lines=[_line("1")],
        product_results=[_product_line("1", ProductMatchStatus.MATCHED, product_id=PRODUCT_A)],
    )
    _record_purpose(session, PurchasePurpose.RESALE)
    resolver = _FakeProductAccountResolver({PRODUCT_A: _discovery(PRODUCT_A)})
    _assert_rejected(session, _use_case(session, resolver), _decision(), "product-shaped")


# --------------------------------------------------------------------------- version / purpose consistency


def test_resale_purpose_for_another_version_fails_closed(session: Session) -> None:
    _seed_review(session)
    _record_purpose(session, PurchasePurpose.RESALE, review_version=2)
    resolver = _FakeProductAccountResolver({PRODUCT_A: _discovery(PRODUCT_A)})
    _assert_rejected(session, _use_case(session, resolver), _decision(_select()), "another review version")
    assert resolver.queries == []


def test_current_version_non_resale_purpose_is_not_gated(session: Session) -> None:
    _seed_review(session)
    _record_purpose(session, PurchasePurpose.RESALE, review_version=2)
    _record_purpose(session, PurchasePurpose.INTERNAL_USE, review_version=1)
    resolver = _FakeProductAccountResolver({})
    assert _use_case(session, resolver).execute(_decision(_select())).accepted is True
    assert resolver.queries == []


def test_decision_without_any_purpose_is_unchanged(session: Session) -> None:
    _seed_review(session)
    resolver = _FakeProductAccountResolver({})
    assert _use_case(session, resolver, approved=frozenset()).execute(_decision(_select())).accepted is True
    assert resolver.queries == []


def test_stale_expected_version_still_conflicts(session: Session) -> None:
    _seed_review(session)
    _record_purpose(session, PurchasePurpose.RESALE)
    resolver = _FakeProductAccountResolver({PRODUCT_A: _discovery(PRODUCT_A)})
    use_case = _use_case(session, resolver)
    use_case.execute(_decision(_select()))
    with pytest.raises(ReviewDecisionError):
        use_case.execute(_decision(_select(), key="decision:resale:second"))
    assert session.query(WorkbenchReviewDecision).count() == 1


def test_resale_decision_replay_does_not_recheck(session: Session) -> None:
    _seed_review(session)
    _record_purpose(session, PurchasePurpose.RESALE)
    resolver = _FakeProductAccountResolver({PRODUCT_A: _discovery(PRODUCT_A)})
    _use_case(session, resolver).execute(_decision(_select()))
    replayed = _use_case(session, resolver, approved=frozenset()).execute(_decision(_select()))
    assert replayed.accepted is True
    assert len(resolver.queries) == 1


# --------------------------------------------------------------------------- nothing pinned / no payload change


def test_resale_decision_pins_no_account_and_bill_line_stays_product_backed(session: Session) -> None:
    _seed_review(session)
    _record_purpose(session, PurchasePurpose.RESALE)
    resolver = _FakeProductAccountResolver({PRODUCT_A: _discovery(PRODUCT_A)})
    _use_case(session, resolver).execute(_decision(_select()))

    decision = session.query(WorkbenchReviewDecision).one()
    assert all(resolution.get("expense_account_id") is None for resolution in decision.line_resolutions)
    reloaded = _reloaded_source(session)
    assert all(resolution.expense_account_id is None for resolution in reloaded.line_resolutions)
    bill = VendorBillBuilder().build(
        reloaded.invoice,
        reloaded.partner_match,
        reloaded.product_match,
        reloaded.tax_match,
        company_id=COMPANY_ID,
    )
    assert [(line.product_id, line.account_id) for line in bill.invoice_lines] == [(PRODUCT_A, None)]
    assert str(ACCOUNT_ID) not in repr(decision.line_resolutions)


def test_gate_result_keeps_fiscal_position_not_evaluated() -> None:
    discovery = _discovery(PRODUCT_A)
    assert discovery.fiscal_position_mapping is FiscalPositionMapping.NOT_EVALUATED
    assert set(FiscalPositionMapping) == {FiscalPositionMapping.NOT_EVALUATED}


def test_gate_source_names_no_account_id_or_code() -> None:
    import ast
    from pathlib import Path

    import app.application.workbench.resale_decision_gate as gate_module

    source = Path(gate_module.__file__).read_text(encoding="utf-8")
    literals = {node.value for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Constant)}
    assert not literals & {29, "29", 150000, "150000"}
    assert "account_id=" not in source


# --------------------------------------------------------------------------- non-RESALE regression


def test_non_resale_decision_without_gate_is_unchanged(session: Session) -> None:
    _seed_review(session)
    _record_purpose(session, PurchasePurpose.RESALE)
    # Direct constructions that pass no gate keep their exact pre-18E-1B behaviour.
    use_case = _use_case(session, _FakeProductAccountResolver({}), gate=False)
    assert use_case.execute(_decision(_select())).accepted is True


def test_resale_error_maps_to_http_conflict() -> None:
    from http import HTTPStatus

    from app.api.routers.workbench import _status_code_for_exception

    assert _status_code_for_exception(ResaleDecisionEligibilityError("x")) == HTTPStatus.CONFLICT
    assert _status_code_for_exception(ReviewDecisionError("x")) == HTTPStatus.INTERNAL_SERVER_ERROR


# --------------------------------------------------------------------------- production wiring


def _settings(**values: Any):
    from app.core.config import Settings

    return Settings(**values)


def test_api_decision_use_case_wires_gate_from_settings() -> None:
    from unittest.mock import MagicMock

    from app.api.dependencies import get_submit_review_decision_use_case
    from app.connectors.odoo.client import OdooJson2Client

    settings = _settings(resale_product_category_ids=[CHILD_CATEGORY_ID, PARENT_CATEGORY_ID])
    use_case = get_submit_review_decision_use_case(
        writer=MagicMock(),
        session=MagicMock(),
        odoo_client=OdooJson2Client.from_settings(settings),
        settings=settings,
    )
    gate = use_case._resale_decision_gate
    assert isinstance(gate, ResaleDecisionGate)
    assert gate._approved_category_ids == frozenset({CHILD_CATEGORY_ID, PARENT_CATEGORY_ID})


def test_odoo_decision_ingestion_wires_gate_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
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
    assert isinstance(submitter._resale_decision_gate, ResaleDecisionGate)
    assert submitter._resale_decision_gate._approved_category_ids == frozenset()


def test_eligibility_result_type_has_no_final_account_field() -> None:
    from app.application.workbench.resale_product_eligibility import ResaleProductEligibility

    names = {field.name for field in fields(ResaleProductEligibility)}
    assert "pre_fiscal_position_account" in names
    assert not {"account_id", "final_account", "final_account_id"} & names
