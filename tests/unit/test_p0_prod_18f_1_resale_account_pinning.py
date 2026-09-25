"""P0-PROD-18F-1: RESALE account pinning at decision acceptance + Vendor Bill preview.

Real ``SubmitReviewDecisionUseCase`` / ``SqlAlchemyReviewRepository`` /
``PreviewVendorBillUseCase`` against SQLite, with a fake P0-PROD-18D resolver standing
in for Odoo. Proves the pin:

* is exactly the evidence the 18E-1B gate accepted (no second Odoo read);
* is persisted atomically with the decision, is never re-read or rewritten on replay;
* drives preview independently of Odoo's *current* configuration, failing closed when
  missing/corrupt;
* leaves non-RESALE decisions, the Vendor Bill builder payload and execution untouched.

Account values are deliberately arbitrary -- nothing may depend on a specific account.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect as pyinspect
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.api import dependencies
from app.api.routers.workbench import router
from app.api.security import AuthenticationMethod, Permission, RequestContext
from app.application.execution.exceptions import ExecutionPreviewResaleAccountingError
from app.application.execution.planner import ExecutionPlanner
from app.application.execution.vendor_bill_preview import (
    PreviewVendorBillRequest,
    PreviewVendorBillUseCase,
    VendorBillPreview,
    VendorBillPreviewLine,
    VendorBillPreviewResaleAccounting,
)
from app.application.workbench.dto import LineResolution
from app.application.workbench.exceptions import (
    ResaleDecisionEligibilityError,
    ReviewDecisionDataIntegrityError,
    ReviewDecisionIdempotencyConflictError,
    WorkbenchContractError,
)
from app.application.workbench.purchase_account_discovery import FiscalPositionMapping
from app.application.workbench.purchase_purpose import PurchasePurpose
from app.application.workbench.resale_accounting_pin import (
    ResaleAccountingLinePin,
    ResaleAccountingPin,
    ResaleAccountingSource,
    resale_accounting_pin_from_data,
    resale_accounting_pin_to_data,
)
from app.application.workflow import WorkflowType
from app.billing import VendorBillBuilder
from app.matching import ProductMatchStatus
from app.models.execution_source_invoice_evidence import ExecutionSourceInvoiceEvidence
from app.models.workbench_review_decision import WorkbenchReviewDecision
from app.models.workbench_review_item import WorkbenchReviewItem
from app.persistence import SqlAlchemyReviewPurchasePurposeResolutionRepository, SqlAlchemyReviewRepository
from app.persistence.execution_source_invoice_reader import SqlAlchemyExecutionSourceInvoiceReader
from tests.unit.test_p0_prod_18e_1b_resale_workflow_wiring import (
    CHILD_CATEGORY_ID,
    COMPANY_ID,
    PRODUCT_A,
    PRODUCT_B,
    REVIEW_ID,
    VITEL_SELLER_CODE,
    _account,
    _decision,
    _discovery,
    _FakeProductAccountResolver,
    _line,
    _product_line,
    _record_purpose,
    _seed_review,
    _select,
    _use_case,
)
from tests.unit.test_p0_prod_18e_1b_resale_workflow_wiring import (
    session as _p0_prod_18e_1b_session,
)

OTHER_CATEGORY_ID = 71
# Arbitrary on purpose: nothing may assume a particular chart of accounts.
ACCOUNT_A = (8811, "ZZ-RESALE-A", "arbitrary_type_a")
ACCOUNT_B = (4242, "770123", "expense")
ACCOUNT_DRIFTED = (9999, "DRIFTED", "drifted_type")
DECISION_VERSION = 2


@pytest.fixture()
def session():
    yield from _p0_prod_18e_1b_session.__wrapped__()


def _resolution(product_id: int, account: tuple[int, str, str], *, category_id: int = CHILD_CATEGORY_ID):
    account_id, code, account_type = account
    return _discovery(
        product_id,
        category_id=category_id,
        category_account_id=account_id,
        accounts=(_account(account_id, code=code, account_type=account_type),),
    )


def _accept_resale(
    session: Session,
    resolver: _FakeProductAccountResolver,
    *line_resolutions: LineResolution,
    approved: frozenset[int] = frozenset({CHILD_CATEGORY_ID}),
    key: str = "decision:resale",
):
    _record_purpose(session, PurchasePurpose.RESALE)
    return _use_case(session, resolver, approved=approved).execute(
        _decision(*(line_resolutions or (_select(),)), key=key)
    )


def _stored_pin(session: Session) -> ResaleAccountingPin | None:
    return SqlAlchemyExecutionSourceInvoiceReader(session).get_resale_accounting_pin(
        review_id=REVIEW_ID, company_id=COMPANY_ID, decision_version=DECISION_VERSION
    )


class _Currency:
    def resolve_vendor_bill_currency_id(self, currency_code: str) -> int:
        return 2


class _Uom:
    def resolve_vendor_bill_product_uom_ids(self, product_ids: tuple[int, ...]) -> dict[int, int]:
        return dict.fromkeys(product_ids, 1)


def _preview_use_case(session: Session, *, with_pin_reader: bool = True) -> PreviewVendorBillUseCase:
    source_reader = SqlAlchemyExecutionSourceInvoiceReader(session)
    return PreviewVendorBillUseCase(
        accepted_decision_reader=SqlAlchemyReviewRepository(session),
        source_invoice_reader=source_reader,
        execution_planner=ExecutionPlanner(),
        vendor_bill_builder=VendorBillBuilder(),
        currency_reader=_Currency(),
        product_uom_reader=_Uom(),
        resale_accounting_pin_reader=source_reader if with_pin_reader else None,
        purchase_purpose_reader=SqlAlchemyReviewPurchasePurposeResolutionRepository(session),
    )


def _preview(session: Session) -> VendorBillPreview:
    return _preview_use_case(session).preview(
        PreviewVendorBillRequest(review_id=REVIEW_ID, company_id=COMPANY_ID, decision_version=DECISION_VERSION)
    )


def _set_stored_pin(session: Session, value: Any) -> None:
    session.execute(update(ExecutionSourceInvoiceEvidence).values(resale_accounting_pin=value))
    session.commit()


# =========================================================================== decision pinning


@pytest.mark.parametrize("account", [ACCOUNT_A, ACCOUNT_B, (3, "X", "asset_current")])
def test_resale_decision_pins_the_accepted_product_category_account(session: Session, account) -> None:
    _seed_review(session)
    resolver = _FakeProductAccountResolver({PRODUCT_A: _resolution(PRODUCT_A, account)})

    _accept_resale(session, resolver)

    pin = _stored_pin(session)
    assert pin == ResaleAccountingPin(
        review_version=1,
        lines=(
            ResaleAccountingLinePin(
                line_number="1",
                product_id=PRODUCT_A,
                product_categ_id=CHILD_CATEGORY_ID,
                product_categ_name=f"Category {CHILD_CATEGORY_ID}",
                pre_fiscal_position_account_id=account[0],
                pre_fiscal_position_account_code=account[1],
                pre_fiscal_position_account_name=f"Account {account[1]}",
                pre_fiscal_position_account_type=account[2],
            ),
        ),
    )
    assert pin.lines[0].account_source is ResaleAccountingSource.RESALE_PRODUCT_CATEGORY
    assert pin.lines[0].fiscal_position_mapping is FiscalPositionMapping.NOT_EVALUATED
    assert pin.purchase_purpose is PurchasePurpose.RESALE


def test_multiple_resale_lines_and_products_pin_independently(session: Session) -> None:
    _seed_review(
        session,
        lines=[_line("1", seller_item_code=VITEL_SELLER_CODE), _line("2", seller_item_code="OTHER-SKU")],
        product_results=[
            _product_line("1", ProductMatchStatus.NOT_FOUND),
            _product_line("2", ProductMatchStatus.MATCHED, product_id=PRODUCT_B),
        ],
    )
    resolver = _FakeProductAccountResolver(
        {
            PRODUCT_A: _resolution(PRODUCT_A, ACCOUNT_A),
            PRODUCT_B: _resolution(PRODUCT_B, ACCOUNT_B, category_id=OTHER_CATEGORY_ID),
        }
    )
    _accept_resale(session, resolver, _select("1"), approved=frozenset({CHILD_CATEGORY_ID, OTHER_CATEGORY_ID}))

    lines = _stored_pin(session).by_line_number()
    assert (lines["1"].product_id, lines["1"].product_categ_id, lines["1"].pre_fiscal_position_account_id) == (
        PRODUCT_A,
        CHILD_CATEGORY_ID,
        ACCOUNT_A[0],
    )
    assert (lines["2"].product_id, lines["2"].product_categ_id, lines["2"].pre_fiscal_position_account_id) == (
        PRODUCT_B,
        OTHER_CATEGORY_ID,
        ACCOUNT_B[0],
    )


def test_pin_is_exactly_the_gate_evidence_with_no_second_odoo_read(session: Session) -> None:
    _seed_review(
        session,
        lines=[_line("1", seller_item_code=VITEL_SELLER_CODE), _line("2", seller_item_code="SKU-2")],
        product_results=[
            _product_line("1", ProductMatchStatus.NOT_FOUND),
            _product_line("2", ProductMatchStatus.NOT_FOUND),
        ],
    )
    resolution = _resolution(PRODUCT_A, ACCOUNT_A)
    resolver = _FakeProductAccountResolver({PRODUCT_A: resolution})

    _accept_resale(session, resolver, _select("1"), _select("2"))

    # One discovery read per distinct product -- the same read the gate already made.
    assert len(resolver.queries) == 1
    for line in _stored_pin(session).lines:
        account = resolution.pre_fiscal_position_account
        assert (line.pre_fiscal_position_account_id, line.pre_fiscal_position_account_code) == (
            account.account_id,
            account.code,
        )
        assert (line.pre_fiscal_position_account_name, line.pre_fiscal_position_account_type) == (
            account.name,
            account.account_type,
        )
        assert line.product_categ_id == resolution.category.category_id


def test_rejected_resale_decision_persists_nothing(session: Session) -> None:
    _seed_review(session)
    resolver = _FakeProductAccountResolver({PRODUCT_A: _resolution(PRODUCT_A, ACCOUNT_A)})
    with pytest.raises(ResaleDecisionEligibilityError):
        _accept_resale(session, resolver, approved=frozenset({OTHER_CATEGORY_ID}))
    assert session.query(WorkbenchReviewDecision).count() == 0
    assert session.query(ExecutionSourceInvoiceEvidence).count() == 0


def test_pin_persistence_failure_does_not_partially_accept_the_decision(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.application.workbench import resale_decision_gate as gate_module

    _seed_review(session)
    resolver = _FakeProductAccountResolver({PRODUCT_A: _resolution(PRODUCT_A, ACCOUNT_A)})
    original = gate_module.ResaleDecisionGate.enforce

    def _pin_for_wrong_version(self, command, evidence):
        return dataclasses.replace(original(self, command, evidence), review_version=99)

    monkeypatch.setattr(gate_module.ResaleDecisionGate, "enforce", _pin_for_wrong_version)
    with pytest.raises(ReviewDecisionDataIntegrityError):
        _accept_resale(session, resolver)

    assert session.query(WorkbenchReviewDecision).count() == 0
    assert session.query(ExecutionSourceInvoiceEvidence).count() == 0
    assert session.query(WorkbenchReviewItem).filter_by(review_id=REVIEW_ID).one().version == 1


def test_replay_neither_rereads_odoo_nor_rewrites_the_pin(session: Session) -> None:
    _seed_review(session)
    resolver = _FakeProductAccountResolver({PRODUCT_A: _resolution(PRODUCT_A, ACCOUNT_A)})
    first = _accept_resale(session, resolver)
    reads_after_accept = len(resolver.queries)

    # Odoo configuration drifts after acceptance; an identical replay must not notice.
    drifted = _FakeProductAccountResolver({PRODUCT_A: _resolution(PRODUCT_A, ACCOUNT_DRIFTED)})
    replay = _use_case(session, drifted).execute(_decision(_select()))

    assert replay == first
    assert session.query(WorkbenchReviewDecision).count() == 1
    assert drifted.queries == []
    assert len(resolver.queries) == reads_after_accept
    assert _stored_pin(session).lines[0].pre_fiscal_position_account_id == ACCOUNT_A[0]


def test_conflicting_replay_fails_through_existing_idempotency_protection(session: Session) -> None:
    _seed_review(session)
    resolver = _FakeProductAccountResolver(
        {PRODUCT_A: _resolution(PRODUCT_A, ACCOUNT_A), PRODUCT_B: _resolution(PRODUCT_B, ACCOUNT_B)}
    )
    _accept_resale(session, resolver)
    with pytest.raises(ReviewDecisionIdempotencyConflictError):
        _use_case(session, resolver).execute(_decision(_select(product_id=PRODUCT_B)))
    assert _stored_pin(session).lines[0].product_id == PRODUCT_A


@pytest.mark.parametrize("purpose", [None, PurchasePurpose.INTERNAL_USE, PurchasePurpose.OTHER_OPERATING_EXPENSE])
def test_non_resale_decision_has_no_pin_no_accounting_read_and_unchanged_preview(
    session: Session, purpose: PurchasePurpose | None
) -> None:
    _seed_review(session)
    if purpose is not None:
        _record_purpose(session, purpose)
    resolver = _FakeProductAccountResolver({PRODUCT_A: _resolution(PRODUCT_A, ACCOUNT_A)})

    _use_case(session, resolver).execute(_decision(_select()))

    assert resolver.queries == []
    assert _stored_pin(session) is None
    preview = _preview(session)
    assert [line.resale_accounting for line in preview.lines] == [None]
    assert preview.lines[0].product_id == PRODUCT_A


# =========================================================================== preview


def test_preview_shows_the_pin_not_current_odoo_configuration(session: Session) -> None:
    _seed_review(session)
    resolver = _FakeProductAccountResolver({PRODUCT_A: _resolution(PRODUCT_A, ACCOUNT_A)})
    _accept_resale(session, resolver)
    reads_after_accept = len(resolver.queries)

    # "Odoo" now reports account B; preview is not wired to any Odoo accounting reader at all.
    resolver._by_product[PRODUCT_A] = _resolution(PRODUCT_A, ACCOUNT_B)
    preview = _preview(session)

    assert len(resolver.queries) == reads_after_accept
    assert preview.lines[0].resale_accounting == VendorBillPreviewResaleAccounting(
        product_id=PRODUCT_A,
        product_categ_id=CHILD_CATEGORY_ID,
        product_categ_name=f"Category {CHILD_CATEGORY_ID}",
        account_id=ACCOUNT_A[0],
        account_code=ACCOUNT_A[1],
        account_name=f"Account {ACCOUNT_A[1]}",
        account_type=ACCOUNT_A[2],
        accounting_source=ResaleAccountingSource.RESALE_PRODUCT_CATEGORY,
        fiscal_position_mapping=FiscalPositionMapping.NOT_EVALUATED,
    )
    # P0-PROD-18F-2: EXECUTE sends the pinned account, so preview shows it -- from the pin,
    # not from "Odoo"'s current account B.
    assert preview.lines[0].account_id == ACCOUNT_A[0]
    assert preview.lines[0].product_id == PRODUCT_A


@pytest.mark.parametrize(
    "stored",
    [
        None,
        {"schema_version": 1},
        {"schema_version": 2, "purchase_purpose": "resale", "review_version": 1, "lines": []},
        "not-a-dict",
    ],
    ids=["missing", "incomplete", "unsupported-version", "corrupt"],
)
def test_preview_of_resale_decision_with_invalid_pin_fails_closed(session: Session, stored: Any) -> None:
    _seed_review(session)
    _accept_resale(session, _FakeProductAccountResolver({PRODUCT_A: _resolution(PRODUCT_A, ACCOUNT_A)}))
    _set_stored_pin(session, stored)
    with pytest.raises(ExecutionPreviewResaleAccountingError):
        _preview(session)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: data["lines"][0].update(product_id=PRODUCT_B),
        lambda data: data["lines"][0].update(line_number="9"),
        lambda data: data.update(review_version=7),
        lambda data: data["lines"][0].update(fiscal_position_mapping="evaluated"),
        lambda data: data["lines"][0].update(extra="x"),
        lambda data: data["lines"][0].update(pre_fiscal_position_account_id=True),
    ],
    ids=["other-product", "other-line", "other-version", "fp-evaluated", "unknown-key", "bool-account"],
)
def test_preview_rejects_pin_inconsistent_with_the_decision(session: Session, mutate) -> None:
    _seed_review(session)
    _accept_resale(session, _FakeProductAccountResolver({PRODUCT_A: _resolution(PRODUCT_A, ACCOUNT_A)}))
    data = resale_accounting_pin_to_data(_stored_pin(session))
    mutate(data)
    _set_stored_pin(session, data)
    with pytest.raises(ExecutionPreviewResaleAccountingError):
        _preview(session)


def test_preview_rejects_a_pin_on_a_non_resale_decision(session: Session) -> None:
    _seed_review(session)
    _accept_resale(session, _FakeProductAccountResolver({PRODUCT_A: _resolution(PRODUCT_A, ACCOUNT_A)}))
    pin_data = resale_accounting_pin_to_data(_stored_pin(session))
    session.query(WorkbenchReviewDecision).delete()
    session.query(ExecutionSourceInvoiceEvidence).delete()
    session.commit()

    # Same review, re-accepted without RESALE at a *different* decision (purpose row removed).
    from app.models.workbench_review_purchase_purpose_resolution import WorkbenchReviewPurchasePurposeResolution

    session.query(WorkbenchReviewPurchasePurposeResolution).delete()
    session.query(WorkbenchReviewItem).filter_by(review_id=REVIEW_ID).update({"version": 1, "status": "pending_review"})
    session.commit()
    _use_case(session, _FakeProductAccountResolver({})).execute(_decision(_select(), key="decision:plain"))
    _set_stored_pin(session, pin_data)

    with pytest.raises(ExecutionPreviewResaleAccountingError):
        _preview(session)


def test_preview_api_exposes_pinned_resale_accounting_additively() -> None:
    line = VendorBillPreviewLine(
        line_number="1",
        description="d",
        quantity=Decimal("1"),
        unit_price=Decimal("50"),
        account_id=None,
        product_id=PRODUCT_A,
        tax_ids=(3,),
        resale_accounting=VendorBillPreviewResaleAccounting(
            product_id=PRODUCT_A,
            product_categ_id=CHILD_CATEGORY_ID,
            product_categ_name="All / Resale",
            account_id=ACCOUNT_A[0],
            account_code=ACCOUNT_A[1],
            account_name="Account A",
            account_type=ACCOUNT_A[2],
            accounting_source=ResaleAccountingSource.RESALE_PRODUCT_CATEGORY,
            fiscal_position_mapping=FiscalPositionMapping.NOT_EVALUATED,
        ),
    )
    plain = dataclasses.replace(line, line_number="2", resale_accounting=None)
    preview = VendorBillPreview(
        review_id=REVIEW_ID,
        company_id=COMPANY_ID,
        decision_version=DECISION_VERSION,
        decision_id="review-decision:1",
        selected_workflow=WorkflowType.VENDOR_BILL,
        move_type="in_invoice",
        partner_id=101,
        invoice_date=__import__("datetime").date(2026, 9, 1),
        reference="R",
        header_company_id=COMPANY_ID,
        currency_code="USD",
        currency_id=2,
        idempotency_key="k",
        lines=(line, plain),
        gross_source_amount=Decimal("100"),
        total_discount=Decimal("0"),
        preview_untaxed=Decimal("100"),
        preview_tax=Decimal("20"),
        preview_total=Decimal("120"),
    )

    class _Stub:
        def preview(self, request):
            return preview

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[dependencies.get_vendor_bill_preview_use_case] = lambda: _Stub()
    app.dependency_overrides[dependencies.get_request_context] = lambda: RequestContext(
        user_id="finance",
        user_name="Finance",
        company_id=COMPANY_ID,
        permissions=(Permission.WORKBENCH_EXECUTE,),
        trace_id="p0-prod-18f-1",
        authentication_method=AuthenticationMethod.JWT,
    )
    with TestClient(app) as client:
        response = client.get(f"/api/workbench/reviews/{REVIEW_ID}/vendor-bill-preview?decision_version=2")
        schema = client.get("/openapi.json").json()["components"]["schemas"]

    assert response.status_code == 200, response.text
    lines = response.json()["data"]["lines"]
    assert lines[0]["account_id"] is None
    assert lines[0]["resale_accounting"] == {
        "product_id": PRODUCT_A,
        "product_categ_id": CHILD_CATEGORY_ID,
        "product_categ_name": "All / Resale",
        "account_id": ACCOUNT_A[0],
        "account_code": ACCOUNT_A[1],
        "account_name": "Account A",
        "account_type": ACCOUNT_A[2],
        "accounting_source": "resale_product_category",
        "fiscal_position_mapping": "not_evaluated",
    }
    assert lines[1]["resale_accounting"] is None
    assert "resale_accounting" not in schema["VendorBillPreviewLineResponse"].get("required", [])


def test_preview_resale_error_maps_to_conflict() -> None:
    from http import HTTPStatus

    from app.api.routers.workbench import _status_code_for_exception

    assert _status_code_for_exception(ExecutionPreviewResaleAccountingError("x")) == HTTPStatus.CONFLICT


def test_preview_composition_wires_hub_only_pin_reader() -> None:
    from unittest.mock import MagicMock

    from app.composition.execution import build_vendor_bill_preview_use_case
    from app.core.config import Settings

    use_case = build_vendor_bill_preview_use_case(session=MagicMock(), settings=Settings())
    assert isinstance(use_case._resale_accounting_pin_reader, SqlAlchemyExecutionSourceInvoiceReader)
    assert isinstance(use_case._purchase_purpose_reader, SqlAlchemyReviewPurchasePurposeResolutionRepository)


# =========================================================================== execution boundary


def test_builder_without_validated_resale_accounts_still_derives_no_account(session: Session) -> None:
    """The pin alone never reaches a bill line: only 18F-2's execution-time validation
    (``validated_resale_accounts``) can add the account -- see test_p0_prod_18f_2."""

    _seed_review(session)
    _accept_resale(session, _FakeProductAccountResolver({PRODUCT_A: _resolution(PRODUCT_A, ACCOUNT_A)}))
    source = SqlAlchemyExecutionSourceInvoiceReader(session).get_source_invoice(
        review_id=REVIEW_ID, company_id=COMPANY_ID, decision_version=DECISION_VERSION
    )
    bill = VendorBillBuilder().build(
        source.invoice, source.partner_match, source.product_match, source.tax_match, company_id=COMPANY_ID
    )
    assert [(line.product_id, line.account_id) for line in bill.invoice_lines] == [(PRODUCT_A, None)]
    assert not any("resale" in field.name for field in dataclasses.fields(type(source)))


def test_builder_never_reads_the_pin_itself() -> None:
    import app.billing.builder as builder

    assert "resale_accounting_pin" not in pyinspect.getsource(builder)
    assert "ResaleAccountingPin" not in pyinspect.getsource(builder)


# =========================================================================== pin contract


def test_pin_round_trips_and_rejects_non_canonical_documents() -> None:
    pin = ResaleAccountingPin(
        review_version=3,
        lines=(
            ResaleAccountingLinePin(
                line_number="1",
                product_id=1,
                product_categ_id=2,
                pre_fiscal_position_account_id=3,
                pre_fiscal_position_account_code="C",
                pre_fiscal_position_account_name="N",
                pre_fiscal_position_account_type="T",
            ),
        ),
    )
    assert resale_accounting_pin_from_data(resale_accounting_pin_to_data(pin)) == pin
    for bad in (
        {**resale_accounting_pin_to_data(pin), "purchase_purpose": "internal_use"},
        {**resale_accounting_pin_to_data(pin), "lines": []},
        {**resale_accounting_pin_to_data(pin), "extra": 1},
    ):
        with pytest.raises(WorkbenchContractError):
            resale_accounting_pin_from_data(bad)
    with pytest.raises(WorkbenchContractError):
        ResaleAccountingPin(review_version=3, lines=(pin.lines[0], pin.lines[0]))


def test_no_account_values_are_hard_coded_in_pinning_or_preview_code() -> None:
    import app.application.execution.vendor_bill_preview as preview_module
    import app.application.workbench.resale_accounting_pin as pin_module
    import app.application.workbench.resale_decision_gate as gate_module

    for module in (pin_module, gate_module, preview_module):
        source = Path(module.__file__).read_text(encoding="utf-8")
        literals = {node.value for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Constant)}
        assert not literals & {29, "29", 150000, "150000", "asset_current", "Raw Materials And Supplies"}
