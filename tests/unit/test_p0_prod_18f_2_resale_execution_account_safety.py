"""P0-PROD-18F-2: RESALE Vendor Bill execution account safety.

PINNED DECISION -> PRE-EXECUTION DRIFT CHECK -> FISCAL-POSITION SAFETY CHECK -> EXPLICIT
PINNED account_id IN THE ODOO PAYLOAD -> INDEPENDENT READBACK VERIFICATION.

Real ``SubmitReviewDecisionUseCase`` (18E-1B gate + 18F-1 pin) / SQLite persistence /
``VendorBillExecutionStrategy`` / ``VendorBillBuilder`` / ``OdooVendorBillWriter`` /
``AccountMoveRepository``, with only the Odoo boundary faked: a recording JSON-2 client
for the Vendor Bill write, a fake P0-PROD-18D resolver for current accounting
configuration, and a fake (or metadata-faked real) fiscal-position reader.

Account values are deliberately arbitrary -- nothing may depend on a specific account.
"""

from __future__ import annotations

import ast
import dataclasses
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api import dependencies
from app.api.routers.workbench import router
from app.api.security import AuthenticationMethod, Permission, RequestContext
from app.application.execution import (
    ExecutionApproval,
    ExecutionMode,
    ExecutionStep,
    ExecutionStepRequest,
    ExecutionStepStatus,
    ExecutionStepType,
    VendorBillExecutionStrategy,
)
from app.application.execution.contracts import ExecutionArtifact, ExecutionArtifactType
from app.application.execution.resale_execution_accounting import ResaleExecutionAccountingValidator
from app.application.execution.resale_fiscal_position import (
    FiscalPositionAccountMappingRecord,
    FiscalPositionRecord,
    PartnerFiscalPositionRecord,
)
from app.application.execution.runtime import ExecutionState
from app.application.execution.workbench_vendor_bill import (
    WorkbenchVendorBillExecutionStatus,
    WorkbenchVendorBillExecutionWorkflow,
)
from app.application.workbench.purchase_account_discovery import (
    CategoryPurchaseAccountRecord,
    ProductPurchaseAccountRecord,
    PurchaseAccountRecord,
    resolve_product_purchase_account,
)
from app.application.workbench.purchase_purpose import PurchasePurpose
from app.application.workbench.vendor_bill_readback import (
    GetVendorBillReadbackUseCase,
    ResaleReadbackStatus,
    VendorBillHeaderVerification,
    VendorBillLineVerification,
    VendorBillReadback,
    VendorBillReadbackIntegrityError,
    VendorBillResaleReadbackVerifier,
)
from app.billing import (
    ValidatedResaleLineAccount,
    VendorBillBuilder,
    VendorBillLine,
    issue_validated_resale_line_account,
    to_odoo_account_move_payload,
)
from app.billing.exceptions import VendorBillBuildError
from app.core.runtime_checks import PRODUCTION_APPROVAL_ACK
from app.erp.exceptions import ErpRepositoryResponseError
from app.erp.odoo.fiscal_position_reader import OdooFiscalPositionReader
from app.erp.write import AccountMoveRepository, OdooVendorBillWritePolicy, OdooVendorBillWriter
from app.matching import ProductMatchStatus
from app.models.execution_source_invoice_evidence import ExecutionSourceInvoiceEvidence
from app.persistence import SqlAlchemyReviewPurchasePurposeResolutionRepository, SqlAlchemyReviewRepository
from app.persistence.execution_source_invoice_reader import SqlAlchemyExecutionSourceInvoiceReader
from tests.unit.test_p0_prod_15ad_source_amount_preservation import (
    _cloudspark_lines,
    _expense_match,
    _identifier_free_products,
)
from tests.unit.test_p0_prod_15ad_source_amount_preservation import _invoice as _cloudspark_invoice
from tests.unit.test_p0_prod_15ad_source_amount_preservation import _partner as _cloudspark_partner
from tests.unit.test_p0_prod_15ad_source_amount_preservation import _taxes as _cloudspark_taxes
from tests.unit.test_p0_prod_18e_1b_resale_workflow_wiring import (
    CHILD_CATEGORY_ID,
    COMPANY_ID,
    OTHER_COMPANY_ID,
    PRODUCT_A,
    PRODUCT_B,
    REVIEW_ID,
    TAX_ID,
    VITEL_SELLER_CODE,
    _decision,
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
from tests.unit.test_p0_prod_18f_1_resale_account_pinning import (
    ACCOUNT_A,
    ACCOUNT_B,
    DECISION_VERSION,
    OTHER_CATEGORY_ID,
    _accept_resale,
    _resolution,
    _set_stored_pin,
)

PARTNER_ID = 101  # the 18E-1B fixture's matched supplier
OVERRIDE_ACCOUNT = (6543, "OVR-1", "override_type")
MOVE_ID = 777
CURRENCY_ID = 2
UOM_ID = 1
FORBIDDEN_LITERALS = {29, "29", 150000, "150000", "asset_current", "Raw Materials And Supplies"}


@pytest.fixture()
def session():
    yield from _p0_prod_18e_1b_session.__wrapped__()


# =========================================================================== builders


def _current(
    product_id: int = PRODUCT_A,
    *,
    account: tuple[int, str, str] = ACCOUNT_A,
    account_name: str | None = None,
    deprecated: bool = False,
    category_id: int = CHILD_CATEGORY_ID,
    category_name: str | None = None,
    extra_accounts: tuple[PurchaseAccountRecord, ...] = (),
    **product_overrides: Any,
):
    """What the P0-PROD-18D resolver reports for ``product_id`` *now* (at execution time)."""

    account_id, code, account_type = account
    records = (
        PurchaseAccountRecord(
            id=account_id,
            code=code,
            name=account_name or f"Account {code}",
            account_type=account_type,
            company_ids=(COMPANY_ID,),
            deprecated=deprecated,
        ),
        *extra_accounts,
    )
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
    return resolve_product_purchase_account(
        ProductPurchaseAccountRecord(**product_values),
        category=CategoryPurchaseAccountRecord(
            id=category_id, name=category_name or f"Category {category_id}", expense_account_id=account_id
        ),
        company_id=COMPANY_ID,
        accounts_by_id={record.id: record for record in records},
    )


def _fp(position_id: int, *, auto_apply: bool = True, active: bool = True, company_id=COMPANY_ID, mappings=()):
    return FiscalPositionRecord(
        id=position_id,
        active=active,
        company_id=company_id,
        auto_apply=auto_apply,
        account_mapping_ids=tuple(mappings),
    )


def _mapping(mapping_id: int, position_id: int, src: int, dest: int) -> FiscalPositionAccountMappingRecord:
    return FiscalPositionAccountMappingRecord(
        id=mapping_id, position_id=position_id, account_src_id=src, account_dest_id=dest
    )


class _FakeFiscalPositions:
    """Application-level fake ``FiscalPositionReader``; read methods only."""

    def __init__(
        self,
        *,
        companies: tuple[int, ...] = (COMPANY_ID,),
        partner_found: bool = True,
        partner_company_id: int | None = COMPANY_ID,
        partner_fp: int | None = None,
        explicit: FiscalPositionRecord | None = None,
        auto: tuple[FiscalPositionRecord, ...] = (),
        mappings: tuple[FiscalPositionAccountMappingRecord, ...] = (),
        error: Exception | None = None,
    ) -> None:
        self._companies = companies
        self._partner_found = partner_found
        self._partner_company_id = partner_company_id
        self._partner_fp = partner_fp
        self._explicit = explicit
        self._auto = auto
        self._mappings = mappings
        self._error = error
        self.calls: list[str] = []

    def accessible_company_ids(self):
        self.calls.append("accessible_company_ids")
        if self._error is not None:
            raise self._error
        return self._companies

    def find_partner(self, *, company_id, partner_id):
        self.calls.append("find_partner")
        if not self._partner_found:
            return None
        return PartnerFiscalPositionRecord(
            partner_id=partner_id, company_id=self._partner_company_id, fiscal_position_id=self._partner_fp
        )

    def find_fiscal_position(self, *, fiscal_position_id):
        self.calls.append("find_fiscal_position")
        return self._explicit if self._explicit is not None and self._explicit.id == fiscal_position_id else None

    def list_auto_apply_fiscal_positions(self, *, company_id):
        self.calls.append("list_auto_apply_fiscal_positions")
        return self._auto

    def list_account_mappings(self, *, fiscal_position_ids):
        self.calls.append("list_account_mappings")
        return tuple(mapping for mapping in self._mappings if mapping.position_id in fiscal_position_ids)


class _RecordingAccountMoveClient:
    """The Odoo JSON-2 boundary of ``AccountMoveRepository``; records every call."""

    def __init__(self, *, existing: list[dict[str, Any]] | None = None) -> None:
        self._existing = existing or []
        self.search_models: list[str] = []
        self.created: list[dict[str, Any]] = []

    async def search_read(self, *, model, domain, fields, limit=20, offset=0):
        self.search_models.append(model)
        if model == "account.move":
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


def _validator(
    session: Session,
    current: dict[int, Any] | None = None,
    *,
    fiscal_positions: _FakeFiscalPositions | None = None,
    approved: frozenset[int] = frozenset({CHILD_CATEGORY_ID}),
    resolver: _FakeProductAccountResolver | None = None,
) -> ResaleExecutionAccountingValidator:
    return ResaleExecutionAccountingValidator(
        pin_reader=SqlAlchemyExecutionSourceInvoiceReader(session),
        purpose_reader=SqlAlchemyReviewPurchasePurposeResolutionRepository(session),
        product_account_resolver=resolver
        or _FakeProductAccountResolver(current if current is not None else {PRODUCT_A: _current()}),
        fiscal_position_reader=fiscal_positions or _FakeFiscalPositions(),
        approved_category_ids=approved,
    )


def _strategy(session: Session, validator, client: _RecordingAccountMoveClient) -> VendorBillExecutionStrategy:
    return VendorBillExecutionStrategy(
        source_invoice_reader=SqlAlchemyExecutionSourceInvoiceReader(session),
        vendor_bill_builder=VendorBillBuilder(),
        vendor_bill_writer=OdooVendorBillWriter(
            repository=AccountMoveRepository(client=client),
            policy=OdooVendorBillWritePolicy(
                production_operations_enabled=True, production_approval_ack=PRODUCTION_APPROVAL_ACK
            ),
        ),
        resale_accounting_check=validator,
    )


def _request(mode: ExecutionMode = ExecutionMode.EXECUTE) -> ExecutionStepRequest:
    return ExecutionStepRequest(
        execution_id="execution-18f2",
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


def _source(session: Session):
    return SqlAlchemyExecutionSourceInvoiceReader(session).get_source_invoice(
        review_id=REVIEW_ID, company_id=COMPANY_ID, decision_version=DECISION_VERSION
    )


def _accepted(session: Session, account: tuple[int, str, str] = ACCOUNT_A) -> None:
    """A fresh current-version RESALE decision for PRODUCT_A pinned to ``account``."""

    _seed_review(session)
    _accept_resale(session, _FakeProductAccountResolver({PRODUCT_A: _resolution(PRODUCT_A, account)}))


def _line_payloads(client: _RecordingAccountMoveClient) -> list[dict[str, Any]]:
    assert len(client.created) == 1
    return [entry[2] for entry in client.created[0]["invoice_line_ids"]]


def _assert_blocked(session: Session, validator, *fragments: str) -> None:
    client = _RecordingAccountMoveClient()
    result = _strategy(session, validator, client).execute(_request())
    assert result.status is ExecutionStepStatus.FAILED
    assert result.error_code == "resale_execution_accounting_error"
    for fragment in fragments:
        assert fragment in (result.message or "")
    # Zero Odoo Vendor Bill traffic: not even the duplicate lookup, let alone create.
    assert client.odoo_calls == 0
    assert result.produced_artifacts == ()


# =========================================================================== A: drift check


def test_unchanged_configuration_passes_and_sends_the_pinned_account(session: Session) -> None:
    _accepted(session)
    client = _RecordingAccountMoveClient()
    result = _strategy(session, _validator(session), client).execute(_request())

    assert result.status is ExecutionStepStatus.EXECUTED
    assert [line["account_id"] for line in _line_payloads(client)] == [ACCOUNT_A[0]]


def test_account_and_category_display_name_changes_alone_pass(session: Session) -> None:
    _accepted(session)
    renamed = _current(account_name="Totally Renamed Account", category_name="All / Renamed")
    client = _RecordingAccountMoveClient()
    result = _strategy(session, _validator(session, {PRODUCT_A: renamed}), client).execute(_request())

    assert result.status is ExecutionStepStatus.EXECUTED
    assert _line_payloads(client)[0]["account_id"] == ACCOUNT_A[0]


@pytest.mark.parametrize(
    ("current", "approved", "fragment"),
    [
        (
            lambda: _current(category_id=OTHER_CATEGORY_ID),
            frozenset({CHILD_CATEGORY_ID, OTHER_CATEGORY_ID}),
            "product_categ_id",
        ),
        (lambda: _current(account=(7001, ACCOUNT_A[1], ACCOUNT_A[2])), None, "account_id"),
        (lambda: _current(account=(ACCOUNT_A[0], "CHANGED-CODE", ACCOUNT_A[2])), None, "account_code"),
        (lambda: _current(account=(ACCOUNT_A[0], ACCOUNT_A[1], "changed_type")), None, "account_type"),
        (lambda: _current(is_storable=True), None, "product_storable"),
        (lambda: _current(is_storable=None), None, "product_storability_unknown"),
        (
            lambda: _current(
                override_account_id=OVERRIDE_ACCOUNT[0],
                extra_accounts=(
                    PurchaseAccountRecord(
                        id=OVERRIDE_ACCOUNT[0],
                        code=OVERRIDE_ACCOUNT[1],
                        name="Override",
                        account_type=OVERRIDE_ACCOUNT[2],
                        company_ids=(COMPANY_ID,),
                        deprecated=False,
                    ),
                ),
            ),
            None,
            "product_account_override_configured",
        ),
        (lambda: _current(active=False), None, "product_inactive"),
        (lambda: _current(company_id=OTHER_COMPANY_ID), None, "product_company_mismatch"),
        (lambda: _current(deprecated=True), None, "category_account_deprecated"),
        (lambda: _current(category_id=CHILD_CATEGORY_ID), frozenset({OTHER_CATEGORY_ID}), "category_not_approved"),
        (lambda: _current(), frozenset(), "resale_category_allowlist_empty"),
    ],
    ids=[
        "category-changed",
        "account-id-changed",
        "account-code-changed",
        "account-type-changed",
        "became-storable",
        "storability-unknown",
        "override-appeared",
        "inactive",
        "company-mismatch",
        "account-deprecated",
        "category-removed-from-allowlist",
        "allowlist-emptied",
    ],
)
def test_accounting_drift_fails_closed_before_any_odoo_write(session: Session, current, approved, fragment) -> None:
    _accepted(session)
    validator = _validator(
        session,
        {PRODUCT_A: current()},
        approved=approved if approved is not None else frozenset({CHILD_CATEGORY_ID}),
    )
    _assert_blocked(session, validator, fragment, "accept a new decision")


def test_category_account_unavailable_for_company_fails_closed(session: Session) -> None:
    _accepted(session)
    unavailable = resolve_product_purchase_account(
        ProductPurchaseAccountRecord(
            product_id=PRODUCT_A,
            product_template_id=PRODUCT_A + 1000,
            name="P",
            active=True,
            company_id=COMPANY_ID,
            product_type="service",
            category_id=CHILD_CATEGORY_ID,
            override_account_id=None,
            is_storable=False,
        ),
        category=CategoryPurchaseAccountRecord(id=CHILD_CATEGORY_ID, name="C", expense_account_id=ACCOUNT_A[0]),
        company_id=COMPANY_ID,
        accounts_by_id={},
    )
    _assert_blocked(session, _validator(session, {PRODUCT_A: unavailable}), "category_account_unavailable")


def test_product_no_longer_visible_or_unreadable_fails_closed(session: Session) -> None:
    _accepted(session)
    _assert_blocked(session, _validator(session, {}), "no longer visible")
    _assert_blocked(
        session,
        _validator(session, {PRODUCT_A: RuntimeError("odoo down")}),
        "could not be read safely",
    )


def test_drift_is_never_auto_accepted_and_the_pin_is_never_updated(session: Session) -> None:
    _accepted(session)
    stored_before = session.query(ExecutionSourceInvoiceEvidence).one().resale_accounting_pin
    _assert_blocked(session, _validator(session, {PRODUCT_A: _current(account=ACCOUNT_B)}), "account_id")
    session.expire_all()
    assert session.query(ExecutionSourceInvoiceEvidence).one().resale_accounting_pin == stored_before


@pytest.mark.parametrize(
    "stored",
    [
        None,
        {"schema_version": 1},
        {"schema_version": 2, "purchase_purpose": "resale", "review_version": 1, "lines": []},
        "not-a-dict",
    ],
    ids=["missing-historical", "incomplete", "unsupported-version", "corrupt"],
)
def test_missing_or_corrupt_pin_fails_closed_without_odoo_reads(session: Session, stored: Any) -> None:
    _accepted(session)
    _set_stored_pin(session, stored)
    resolver = _FakeProductAccountResolver({PRODUCT_A: _current()})
    fiscal_positions = _FakeFiscalPositions()
    _assert_blocked(session, _validator(session, resolver=resolver, fiscal_positions=fiscal_positions))
    assert resolver.queries == [] and fiscal_positions.calls == []


def test_historical_resale_decision_without_pin_requires_a_new_decision(session: Session) -> None:
    _accepted(session)
    _set_stored_pin(session, None)
    _assert_blocked(session, _validator(session), "no pinned accounting evidence", "accept a new decision")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: data["lines"][0].update(product_id=PRODUCT_B),
        lambda data: data["lines"][0].update(line_number="9"),
        lambda data: data.update(review_version=7),
    ],
    ids=["other-product", "other-line", "other-version"],
)
def test_pin_inconsistent_with_the_decision_fails_closed(session: Session, mutate) -> None:
    from app.application.workbench.resale_accounting_pin import resale_accounting_pin_to_data

    _accepted(session)
    data = resale_accounting_pin_to_data(
        SqlAlchemyExecutionSourceInvoiceReader(session).get_resale_accounting_pin(
            review_id=REVIEW_ID, company_id=COMPANY_ID, decision_version=DECISION_VERSION
        )
    )
    mutate(data)
    _set_stored_pin(session, data)
    _assert_blocked(session, _validator(session, {PRODUCT_A: _current(), PRODUCT_B: _current(PRODUCT_B)}))


def test_pin_on_a_non_resale_decision_fails_closed(session: Session) -> None:
    from app.models.workbench_review_purchase_purpose_resolution import WorkbenchReviewPurchasePurposeResolution

    _accepted(session)
    session.query(WorkbenchReviewPurchasePurposeResolution).delete()
    session.commit()
    _assert_blocked(session, _validator(session), "not accepted under a RESALE purpose")


def test_multi_line_multi_product_pins_are_each_checked_and_sent(session: Session) -> None:
    _seed_review(
        session,
        lines=[_line("1", seller_item_code=VITEL_SELLER_CODE), _line("2", seller_item_code="X2")],
        product_results=[
            _product_line("1", ProductMatchStatus.NOT_FOUND),
            _product_line("2", ProductMatchStatus.NOT_FOUND),
        ],
    )
    _accept_resale(
        session,
        _FakeProductAccountResolver(
            {PRODUCT_A: _resolution(PRODUCT_A, ACCOUNT_A), PRODUCT_B: _resolution(PRODUCT_B, ACCOUNT_B)}
        ),
        _select("1", PRODUCT_A),
        _select("2", PRODUCT_B),
    )
    resolver = _FakeProductAccountResolver({PRODUCT_A: _current(), PRODUCT_B: _current(PRODUCT_B, account=ACCOUNT_B)})
    client = _RecordingAccountMoveClient()
    result = _strategy(session, _validator(session, resolver=resolver), client).execute(_request())

    assert result.status is ExecutionStepStatus.EXECUTED
    assert [(line["product_id"], line["account_id"]) for line in _line_payloads(client)] == [
        (PRODUCT_A, ACCOUNT_A[0]),
        (PRODUCT_B, ACCOUNT_B[0]),
    ]
    assert sorted(query.product_id for query in resolver.queries) == [PRODUCT_A, PRODUCT_B]

    # Drift on only one of the two products still blocks the whole bill.
    drifted = _FakeProductAccountResolver(
        {PRODUCT_A: _current(), PRODUCT_B: _current(PRODUCT_B, account=(ACCOUNT_B[0], "X", ACCOUNT_B[2]))}
    )
    _assert_blocked(session, _validator(session, resolver=drifted), "line 2", "account_code")


# =========================================================================== B: fiscal positions


def test_no_fiscal_position_mapping_of_the_pinned_account_passes(session: Session) -> None:
    _accepted(session)
    fiscal_positions = _FakeFiscalPositions(
        partner_fp=41,
        explicit=_fp(41, auto_apply=False, mappings=(1,)),
        auto=(_fp(42, mappings=(2, 3)), _fp(43)),
        mappings=(
            _mapping(1, 41, src=ACCOUNT_B[0], dest=5001),
            _mapping(2, 42, src=5002, dest=5003),
            _mapping(3, 42, src=5004, dest=ACCOUNT_A[0]),  # maps *to* the pinned account: harmless
        ),
    )
    client = _RecordingAccountMoveClient()
    result = _strategy(session, _validator(session, fiscal_positions=fiscal_positions), client).execute(_request())

    assert result.status is ExecutionStepStatus.EXECUTED
    assert _line_payloads(client)[0]["account_id"] == ACCOUNT_A[0]


def test_supplier_explicit_fiscal_position_mapping_the_pinned_account_fails(session: Session) -> None:
    _accepted(session)
    fiscal_positions = _FakeFiscalPositions(
        partner_fp=41,
        explicit=_fp(41, auto_apply=False, active=False, mappings=(1,)),  # archived still applies when explicit
        mappings=(_mapping(1, 41, src=ACCOUNT_A[0], dest=5001),),
    )
    _assert_blocked(session, _validator(session, fiscal_positions=fiscal_positions), "pinned_account_mapped", "41")


def test_auto_applicable_fiscal_position_mapping_the_pinned_account_fails(session: Session) -> None:
    _accepted(session)
    fiscal_positions = _FakeFiscalPositions(
        auto=(_fp(42, mappings=(2,)), _fp(43, company_id=None, mappings=(3,))),
        mappings=(_mapping(2, 42, src=5002, dest=5003), _mapping(3, 43, src=ACCOUNT_A[0], dest=ACCOUNT_A[0])),
    )
    _assert_blocked(session, _validator(session, fiscal_positions=fiscal_positions), "pinned_account_mapped", "43")


@pytest.mark.parametrize(
    ("fiscal_positions", "fragment"),
    [
        (lambda: _FakeFiscalPositions(companies=(COMPANY_ID, OTHER_COMPANY_ID)), "company_context_unverified"),
        (lambda: _FakeFiscalPositions(partner_found=False), "partner_not_found"),
        (lambda: _FakeFiscalPositions(partner_company_id=OTHER_COMPANY_ID), "partner_company_mismatch"),
        (lambda: _FakeFiscalPositions(partner_fp=41, explicit=None), "explicit_fiscal_position_unreadable"),
        (
            lambda: _FakeFiscalPositions(partner_fp=41, explicit=_fp(41, company_id=OTHER_COMPANY_ID)),
            "fiscal_position_out_of_scope",
        ),
        (lambda: _FakeFiscalPositions(auto=(_fp(42, company_id=OTHER_COMPANY_ID),)), "fiscal_position_out_of_scope"),
        (lambda: _FakeFiscalPositions(auto=(_fp(42, mappings=(9,)),)), "account_mappings_inconsistent"),
        (
            lambda: _FakeFiscalPositions(auto=(_fp(42),), mappings=(_mapping(9, 42, src=1, dest=2),)),
            "account_mappings_inconsistent",
        ),
        (lambda: _FakeFiscalPositions(error=RuntimeError("down")), "could not be read safely"),
    ],
    ids=[
        "ambiguous-company-context",
        "partner-missing",
        "partner-other-company",
        "explicit-fp-unreadable",
        "explicit-fp-other-company",
        "auto-fp-other-company",
        "mapping-hidden-from-read",
        "mapping-not-on-position",
        "read-failure",
    ],
)
def test_fiscal_position_applicability_that_cannot_be_proven_fails_closed(
    session: Session, fiscal_positions, fragment
) -> None:
    _accepted(session)
    _assert_blocked(session, _validator(session, fiscal_positions=fiscal_positions()), fragment)


class _MetadataAdapter:
    """Fake ``OdooReadOnlyAdapter`` for the real ``OdooFiscalPositionReader``."""

    def __init__(self, *, missing: tuple[tuple[str, str], ...] = (), records: dict[str, list] | None = None):
        self._missing = missing
        self._records = records or {}
        self.methods: list[str] = []

    def read_model_field_metadata(self, *, model, field_name):
        self.methods.append("read_model_field_metadata")
        if (model, field_name) in self._missing:
            return ()
        from app.erp.odoo.fiscal_position_reader import REQUIRED_FIELDS

        spec = next(spec for spec in REQUIRED_FIELDS[model] if spec[0] == field_name)
        return ({"name": spec[0], "ttype": spec[1], "relation": spec[2] or False},)

    def search_read(self, *, model, domain, fields, limit=None, offset=0):
        self.methods.append("search_read")
        return tuple(self._records.get(model, []))

    def search_read_all(self, *, model, domain, fields, page_size=None, max_records=None):
        self.methods.append("search_read_all")
        return tuple(self._records.get(model, []))


_ODOO_RECORDS = {
    "res.company": [{"id": COMPANY_ID}],
    "res.partner": [{"id": PARTNER_ID, "company_id": False, "property_account_position_id": False}],
    "account.fiscal.position": [
        {"id": 42, "active": True, "company_id": [COMPANY_ID, "C"], "auto_apply": True, "account_ids": [2]}
    ],
    "account.fiscal.position.account": [
        {"id": 2, "position_id": [42, "FP"], "account_src_id": [5002, "a"], "account_dest_id": [5003, "b"]}
    ],
}


def test_real_fiscal_position_reader_passes_with_well_formed_metadata(session: Session) -> None:
    _accepted(session)
    adapter = _MetadataAdapter(records=_ODOO_RECORDS)
    validator = _validator(session, fiscal_positions=OdooFiscalPositionReader(adapter=adapter))  # type: ignore[arg-type]
    client = _RecordingAccountMoveClient()

    assert _strategy(session, validator, client).execute(_request()).status is ExecutionStepStatus.EXECUTED
    assert set(adapter.methods) <= {"read_model_field_metadata", "search_read", "search_read_all"}


@pytest.mark.parametrize(
    "missing",
    [
        ("account.fiscal.position", "auto_apply"),
        ("account.fiscal.position", "account_ids"),
        ("account.fiscal.position.account", "account_src_id"),
        ("res.partner", "property_account_position_id"),
    ],
)
def test_missing_fiscal_position_metadata_fails_closed(session: Session, missing) -> None:
    _accepted(session)
    adapter = _MetadataAdapter(missing=(missing,), records=_ODOO_RECORDS)
    _assert_blocked(
        session, _validator(session, fiscal_positions=OdooFiscalPositionReader(adapter=adapter)), "read safely"
    )  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("model", "record"),
    [
        (
            "account.fiscal.position",
            {"id": 42, "active": True, "company_id": False, "auto_apply": True, "account_ids": "x"},
        ),
        (
            "account.fiscal.position",
            {"id": 42, "active": "yes", "company_id": False, "auto_apply": True, "account_ids": []},
        ),
        (
            "account.fiscal.position.account",
            {"id": 2, "position_id": [42, "FP"], "account_src_id": False, "account_dest_id": 1},
        ),
        ("res.partner", {"id": PARTNER_ID, "company_id": False, "property_account_position_id": True}),
    ],
    ids=["mapping-ids-not-list", "active-not-bool", "src-account-missing", "partner-fp-bool"],
)
def test_structurally_unexpected_fiscal_position_records_fail_closed(session: Session, model, record) -> None:
    _accepted(session)
    records = {**_ODOO_RECORDS, model: [record]}
    adapter = _MetadataAdapter(records=records)
    _assert_blocked(
        session, _validator(session, fiscal_positions=OdooFiscalPositionReader(adapter=adapter)), "read safely"
    )  # type: ignore[arg-type]


def test_metadata_with_wrong_relation_fails_closed() -> None:
    class _WrongRelation(_MetadataAdapter):
        def read_model_field_metadata(self, *, model, field_name):
            return ({"name": field_name, "ttype": "many2one", "relation": "res.users"},)

    reader = OdooFiscalPositionReader(adapter=_WrongRelation(records=_ODOO_RECORDS))  # type: ignore[arg-type]
    with pytest.raises(ErpRepositoryResponseError):
        reader.find_partner(company_id=COMPANY_ID, partner_id=PARTNER_ID)


# =========================================================================== C/D: representation + payload


@pytest.mark.parametrize(
    "account",
    [ACCOUNT_A, ACCOUNT_B, (1, "X", "y"), (987654321, "9.9.9-ÇĞİ", "some_future_type")],
    ids=["a", "b", "tiny", "unicode-code"],
)
def test_arbitrary_pinned_accounts_are_sent_verbatim_with_the_product(session: Session, account) -> None:
    _accepted(session, account)
    client = _RecordingAccountMoveClient()
    result = _strategy(session, _validator(session, {PRODUCT_A: _current(account=account)}), client).execute(_request())

    assert result.status is ExecutionStepStatus.EXECUTED
    assert _line_payloads(client) == [
        {
            "product_id": PRODUCT_A,
            "quantity": "1",
            "price_unit": "50.00",
            "tax_ids": ((6, 0, (TAX_ID,)),),
            "name": "Line 1",
            "product_uom_id": UOM_ID,
            "account_id": account[0],
        }
    ]
    header = client.created[0]
    assert header["move_type"] == "in_invoice" and "fiscal_position_id" not in header


def test_dry_run_validates_too_but_writes_nothing(session: Session) -> None:
    _accepted(session)
    client = _RecordingAccountMoveClient()
    ok = _strategy(session, _validator(session), client).execute(_request(ExecutionMode.DRY_RUN))
    blocked = _strategy(session, _validator(session, {PRODUCT_A: _current(account=ACCOUNT_B)}), client).execute(
        _request(ExecutionMode.DRY_RUN)
    )

    assert ok.status is ExecutionStepStatus.DRY_RUN_OK
    assert blocked.status is ExecutionStepStatus.FAILED
    assert client.odoo_calls == 0


def test_product_line_cannot_carry_an_arbitrary_account() -> None:
    with pytest.raises(ValueError):
        VendorBillLine(product_id=PRODUCT_A, account_id=ACCOUNT_A[0], quantity=Decimal(1), unit_price=Decimal(1))
    forged = ValidatedResaleLineAccount(line_number="1", product_id=PRODUCT_A, account_id=ACCOUNT_A[0])
    with pytest.raises(ValueError):
        VendorBillLine(
            product_id=PRODUCT_A,
            account_id=ACCOUNT_A[0],
            resale_account=forged,
            quantity=Decimal(1),
            unit_price=Decimal(1),
        )
    issued = issue_validated_resale_line_account(line_number="1", product_id=PRODUCT_A, account_id=ACCOUNT_A[0])
    with pytest.raises(ValueError):  # replace() drops the seal
        VendorBillLine(
            product_id=PRODUCT_A,
            account_id=999,
            resale_account=replace(issued, account_id=999),
            quantity=Decimal(1),
            unit_price=Decimal(1),
        )
    with pytest.raises(ValueError):  # a sealed proof for another account
        VendorBillLine(
            product_id=PRODUCT_A, account_id=999, resale_account=issued, quantity=Decimal(1), unit_price=Decimal(1)
        )
    with pytest.raises(ValueError):  # a sealed proof for another product
        VendorBillLine(
            product_id=PRODUCT_B,
            account_id=ACCOUNT_A[0],
            resale_account=issued,
            quantity=Decimal(1),
            unit_price=Decimal(1),
        )


def test_builder_rejects_forged_or_mismatched_resale_accounts(session: Session) -> None:
    _accepted(session)
    source = _source(session)

    def build(accounts):
        return VendorBillBuilder().build(
            source.invoice,
            source.partner_match,
            source.product_match,
            source.tax_match,
            company_id=COMPANY_ID,
            validated_resale_accounts=accounts,
        )

    forged = ValidatedResaleLineAccount(line_number="1", product_id=PRODUCT_A, account_id=ACCOUNT_A[0])
    other_product = issue_validated_resale_line_account(line_number="1", product_id=PRODUCT_B, account_id=1)
    other_line = issue_validated_resale_line_account(line_number="2", product_id=PRODUCT_A, account_id=1)
    for accounts in ({}, {"1": forged}, {"1": other_product}, {"2": other_line}, {"1": other_line}):
        with pytest.raises(VendorBillBuildError):
            build(accounts)
    assert build(None).invoice_lines[0].account_id is None


def test_account_is_never_accepted_from_the_request_or_other_callers() -> None:
    from app.schemas.workbench import WorkbenchVendorBillExecutionRequest

    assert not any("account" in name for name in WorkbenchVendorBillExecutionRequest.model_fields)
    issuers = [
        path
        for path in Path("app").rglob("*.py")
        if "issue_validated_resale_line_account(" in path.read_text(encoding="utf-8")
    ]
    assert sorted(str(path) for path in issuers) == [
        "app/application/execution/resale_execution_accounting.py",
        "app/billing/dto.py",
    ]


def test_non_resale_product_payload_is_unchanged(session: Session) -> None:
    _seed_review(session)
    _use_case(session, _FakeProductAccountResolver({})).execute(_decision(_select(), key="decision:plain"))
    resolver = _FakeProductAccountResolver({PRODUCT_A: _current()})
    fiscal_positions = _FakeFiscalPositions()
    client = _RecordingAccountMoveClient()
    result = _strategy(
        session, _validator(session, resolver=resolver, fiscal_positions=fiscal_positions), client
    ).execute(_request())

    assert result.status is ExecutionStepStatus.EXECUTED
    assert _line_payloads(client) == [
        {
            "product_id": PRODUCT_A,
            "quantity": "1",
            "price_unit": "50.00",
            "tax_ids": ((6, 0, (TAX_ID,)),),
            "name": "Line 1",
            "product_uom_id": UOM_ID,
        }
    ]
    # No RESALE read of any kind for a non-RESALE decision.
    assert resolver.queries == [] and fiscal_positions.calls == []


@pytest.mark.parametrize("purpose", [PurchasePurpose.INTERNAL_USE, PurchasePurpose.OTHER_OPERATING_EXPENSE])
def test_non_resale_purposes_are_untouched(session: Session, purpose: PurchasePurpose) -> None:
    _seed_review(session)
    _record_purpose(session, purpose)
    _use_case(session, _FakeProductAccountResolver({})).execute(_decision(_select(), key="decision:plain"))
    resolver = _FakeProductAccountResolver({})
    fiscal_positions = _FakeFiscalPositions()

    assert _validator(session, resolver=resolver, fiscal_positions=fiscal_positions).validate(_source(session)) is None
    assert resolver.queries == [] and fiscal_positions.calls == []


def test_account_only_operating_expense_payload_is_unchanged() -> None:
    """Bill-63 (CloudSpark) shape: account-only lines, no product, no RESALE."""

    invoice = _cloudspark_invoice(_cloudspark_lines(), net_total=Decimal("4959.80"))
    bill = VendorBillBuilder().build(
        invoice,
        _cloudspark_partner(),
        _identifier_free_products(invoice),
        _cloudspark_taxes(invoice),
        company_id=COMPANY_ID,
        operating_expense_match=_expense_match(),
    )
    lines = [
        entry[2] for entry in to_odoo_account_move_payload(bill, currency_id=31, product_uom_ids={})["invoice_line_ids"]
    ]
    assert [set(line) for line in lines] == [{"name", "quantity", "price_unit", "account_id", "tax_ids"}] * 3
    assert all(line.resale_account is None for line in bill.invoice_lines)


def test_explicit_account_only_line_payload_is_unchanged() -> None:
    """INTERNAL_USE-style explicit per-line account-only resolution (P0-PROD-08G)."""

    invoice = _cloudspark_invoice(_cloudspark_lines()[:1], net_total=Decimal("1487.94"))
    bill = VendorBillBuilder().build(
        invoice,
        _cloudspark_partner(),
        _identifier_free_products(invoice),
        _cloudspark_taxes(invoice),
        company_id=COMPANY_ID,
        account_only_line_numbers=frozenset({"1"}),
        explicit_account_only_accounts={"1": 4321},
    )
    line = to_odoo_account_move_payload(bill, currency_id=31, product_uom_ids={})["invoice_line_ids"][0][2]
    assert line == {
        "name": "CPU",
        "quantity": "20",
        "price_unit": "74.397000",
        "account_id": 4321,
        "tax_ids": ((6, 0, (34,)),),
    }


# =========================================================================== E: execution ordering / retry


def test_validation_runs_on_every_attempt_so_retry_cannot_bypass_it(session: Session) -> None:
    _accepted(session)
    resolver = _FakeProductAccountResolver({PRODUCT_A: _current(account=ACCOUNT_B)})
    client = _RecordingAccountMoveClient()
    strategy = _strategy(session, _validator(session, resolver=resolver), client)

    first = strategy.execute(_request())
    second = strategy.execute(_request())

    assert first.status is second.status is ExecutionStepStatus.FAILED
    assert len(resolver.queries) == 2
    assert client.odoo_calls == 0


def test_existing_odoo_bill_is_recovered_not_duplicated(session: Session) -> None:
    _accepted(session)
    client = _RecordingAccountMoveClient(existing=[{"id": MOVE_ID, "name": "BILL/1"}])
    result = _strategy(session, _validator(session), client).execute(_request())

    assert result.status is ExecutionStepStatus.EXECUTED
    assert client.created == []
    assert result.produced_artifacts[0].artifact_id == str(MOVE_ID)
    assert result.produced_artifacts[0].created is False


def test_completed_execution_is_never_re_executed_for_validation(session: Session) -> None:
    _accepted(session)

    class _NeverRun:
        def execute(self, command):
            raise AssertionError("a completed execution must not run again")

    class _Runtime:
        def get_snapshot(self, *, execution_id):
            return SimpleNamespace(execution_id=execution_id, state=ExecutionState.COMPLETED, steps=())

    workflow = WorkbenchVendorBillExecutionWorkflow(
        accepted_decision_reader=SqlAlchemyReviewRepository(session),
        source_invoice_reader=SqlAlchemyExecutionSourceInvoiceReader(session),
        execution_use_case=_NeverRun(),  # type: ignore[arg-type]
        runtime_repository=_Runtime(),  # type: ignore[arg-type]
    )
    result = workflow.execute(
        review_id=REVIEW_ID,
        company_id=COMPANY_ID,
        decision_version=DECISION_VERSION,
        mode=ExecutionMode.EXECUTE,
        approval=ExecutionApproval(approved_by="finance.lead"),
    )
    assert result.status is WorkbenchVendorBillExecutionStatus.ALREADY_EXECUTED


def test_composition_always_wires_the_resale_check_into_the_strategy() -> None:
    import inspect

    from app.composition import execution as composition

    parameter = inspect.signature(VendorBillExecutionStrategy).parameters["resale_accounting_check"]
    assert parameter.default is inspect.Parameter.empty
    source = inspect.getsource(composition.build_vendor_bill_execution_use_case)
    assert "resale_accounting_check=build_resale_execution_accounting_validator(" in source
    validator_source = inspect.getsource(composition.build_resale_execution_accounting_validator)
    assert "settings.resale_product_category_ids" in validator_source
    assert "OdooFiscalPositionReader(adapter=OdooReadOnlyAdapter(" in validator_source


# =========================================================================== F: readback


class _Header:
    def read_vendor_bill(self, *, move_id, company_id):
        return VendorBillHeaderVerification(
            move_id=move_id,
            company_id=company_id,
            state="draft",
            move_type="in_invoice",
            partner_id=PARTNER_ID,
            currency="USD",
            amount_untaxed=Decimal("50"),
            amount_tax=Decimal("10"),
            amount_total=Decimal("60"),
        )


class _Lines:
    def __init__(self, *pairs: tuple[int | None, int | None]) -> None:
        self._pairs = pairs

    def read_invoice_lines_for_move(self, *, move_id):
        return tuple(
            VendorBillLineVerification(
                line_id=900 + index,
                move_id=move_id,
                account_id=account_id,
                product_id=product_id,
                quantity=Decimal("1"),
                price_unit=Decimal("50"),
                tax_ids=(TAX_ID,),
                price_subtotal=Decimal("50"),
                price_total=Decimal("60"),
            )
            for index, (product_id, account_id) in enumerate(self._pairs)
        )


class _ReadbackFiscalPosition:
    def __init__(self, *, supported: bool = True, fiscal_position_id: int | None = None) -> None:
        self._supported = supported
        self._fiscal_position_id = fiscal_position_id
        self.calls: list[str] = []

    def vendor_bill_fiscal_position_supported(self):
        self.calls.append("supported")
        return self._supported

    def read_vendor_bill_fiscal_position_id(self, *, move_id, company_id):
        self.calls.append("read")
        return self._fiscal_position_id


class _Review:
    def get_review_item(self, query):
        return None


class _Snapshots:
    def find_latest_snapshot_for_review(self, *, review_id, company_id):
        artifact = ExecutionArtifact(
            artifact_type=ExecutionArtifactType.VENDOR_BILL,
            artifact_id=str(MOVE_ID),
            external_identity="vendor-bill-write:x",
            created=True,
        )
        step = SimpleNamespace(
            step_type=ExecutionStepType.VENDOR_BILL,
            last_result=SimpleNamespace(
                status=ExecutionStepStatus.EXECUTED, dry_run=False, produced_artifacts=(artifact,)
            ),
        )
        return SimpleNamespace(
            state=ExecutionState.COMPLETED,
            execution_id="execution-18f2",
            decision_version=DECISION_VERSION,
            steps=(step,),
        )


def _readback(session: Session, lines: _Lines, fiscal_position: _ReadbackFiscalPosition | None = None):
    return GetVendorBillReadbackUseCase(
        review_reader=_Review(),  # type: ignore[arg-type]
        execution_snapshot_reader=_Snapshots(),  # type: ignore[arg-type]
        header_reader=_Header(),
        line_reader=lines,
        resale_verifier=VendorBillResaleReadbackVerifier(
            pin_reader=SqlAlchemyExecutionSourceInvoiceReader(session),
            purpose_reader=SqlAlchemyReviewPurchasePurposeResolutionRepository(session),
            fiscal_position_reader=fiscal_position or _ReadbackFiscalPosition(),
        ),
    ).execute(review_id=REVIEW_ID, company_id=COMPANY_ID)


def test_readback_matching_resale_product_and_account_verifies(session: Session) -> None:
    _accepted(session)
    readback = _readback(session, _Lines((PRODUCT_A, ACCOUNT_A[0])))

    verification = readback.resale_verification
    assert verification is not None
    assert verification.status is ResaleReadbackStatus.VERIFIED
    assert verification.mismatches == ()
    assert verification.fiscal_position_supported is True and verification.fiscal_position_id is None
    assert verification.lines[0].expected_account_id == ACCOUNT_A[0]


def test_readback_reports_the_bill_fiscal_position(session: Session) -> None:
    _accepted(session)
    readback = _readback(session, _Lines((PRODUCT_A, ACCOUNT_A[0])), _ReadbackFiscalPosition(fiscal_position_id=42))
    assert readback.resale_verification.fiscal_position_id == 42
    assert readback.resale_verification.status is ResaleReadbackStatus.VERIFIED

    unsupported = _ReadbackFiscalPosition(supported=False)
    readback = _readback(session, _Lines((PRODUCT_A, ACCOUNT_A[0])), unsupported)
    assert readback.resale_verification.fiscal_position_supported is False
    assert unsupported.calls == ["supported"]


@pytest.mark.parametrize(
    ("pairs", "fragment"),
    [
        (((PRODUCT_A, ACCOUNT_B[0]),), f"account {ACCOUNT_B[0]} != pinned {ACCOUNT_A[0]}"),
        (((PRODUCT_A, None),), f"account None != pinned {ACCOUNT_A[0]}"),
        (((PRODUCT_B, ACCOUNT_A[0]),), f"product {PRODUCT_B} is not pinned"),
        (((None, ACCOUNT_A[0]),), "product None is not pinned"),
        (((PRODUCT_A, ACCOUNT_A[0]), (PRODUCT_A, ACCOUNT_A[0])), "line count 2 != pinned 1"),
    ],
    ids=["wrong-account", "no-account", "wrong-product", "no-product", "extra-line"],
)
def test_readback_mismatch_is_reported_never_corrected(session: Session, pairs, fragment) -> None:
    _accepted(session)
    readback = _readback(session, _Lines(*pairs))

    assert readback.resale_verification.status is ResaleReadbackStatus.MISMATCH
    assert any(fragment in mismatch for mismatch in readback.resale_verification.mismatches)


def test_readback_of_resale_decision_with_invalid_pin_fails_closed(session: Session) -> None:
    _accepted(session)
    _set_stored_pin(session, None)
    with pytest.raises(VendorBillReadbackIntegrityError):
        _readback(session, _Lines((PRODUCT_A, ACCOUNT_A[0])))


def test_non_resale_readback_is_unchanged(session: Session) -> None:
    _seed_review(session)
    _use_case(session, _FakeProductAccountResolver({})).execute(_decision(_select(), key="decision:plain"))
    fiscal_position = _ReadbackFiscalPosition()
    readback = _readback(session, _Lines((PRODUCT_A, 4242)), fiscal_position)

    assert readback.resale_verification is None
    assert fiscal_position.calls == []


def test_readback_never_edits_odoo() -> None:
    import re

    from app.erp.odoo import fiscal_position_reader

    source = Path(fiscal_position_reader.__file__).read_text(encoding="utf-8")
    adapter_calls = set(re.findall(r"self\._adapter\.(\w+)\(", source))
    assert adapter_calls == {"search_read", "search_read_all", "read_model_field_metadata"}
    readback_source = Path("app/application/workbench/vendor_bill_readback.py").read_text(encoding="utf-8")
    assert "Writer" not in readback_source and "write_" not in readback_source


def _stub_use_case(readback: VendorBillReadback):
    class _Stub:
        def execute(self, *, review_id, company_id):
            return readback

    stub = _Stub()
    return lambda: stub


def test_readback_api_exposes_resale_verification_additively() -> None:
    from app.application.workbench.vendor_bill_readback import (
        ResaleReadbackLineCheck,
        VendorBillResaleReadbackVerification,
    )

    header = _Header().read_vendor_bill(move_id=MOVE_ID, company_id=COMPANY_ID)
    lines = _Lines((PRODUCT_A, ACCOUNT_B[0])).read_invoice_lines_for_move(move_id=MOVE_ID)
    verification = VendorBillResaleReadbackVerification(
        status=ResaleReadbackStatus.MISMATCH,
        pin_review_version=1,
        fiscal_position_supported=True,
        fiscal_position_id=None,
        lines=(
            ResaleReadbackLineCheck(
                line_id=900,
                product_id=PRODUCT_A,
                account_id=ACCOUNT_B[0],
                expected_account_id=ACCOUNT_A[0],
                product_matches=True,
                account_matches=False,
            ),
        ),
        mismatches=("line 900: account mismatch",),
    )
    readbacks = {
        "resale": VendorBillReadback(
            review_id=REVIEW_ID,
            execution_id="e",
            artifact_id=str(MOVE_ID),
            header=header,
            lines=lines,
            resale_verification=verification,
        ),
    }
    readbacks["plain"] = dataclasses.replace(readbacks["resale"], resale_verification=None)

    responses = {}
    for key, readback in readbacks.items():
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[dependencies.get_vendor_bill_readback_use_case] = _stub_use_case(readback)
        app.dependency_overrides[dependencies.get_request_context] = lambda: RequestContext(
            user_id="finance",
            user_name="Finance",
            company_id=COMPANY_ID,
            permissions=tuple(Permission),
            trace_id="p0-prod-18f-2",
            authentication_method=AuthenticationMethod.JWT,
        )
        with TestClient(app) as client:
            response = client.get(f"/api/workbench/reviews/{REVIEW_ID}/vendor-bill-readback")
        assert response.status_code == 200, response.text
        responses[key] = response.json()["data"]

    assert responses["plain"]["resale_verification"] is None
    assert responses["resale"]["resale_verification"] == {
        "status": "mismatch",
        "pin_review_version": 1,
        "fiscal_position_supported": True,
        "fiscal_position_id": None,
        "lines": [
            {
                "line_id": 900,
                "product_id": PRODUCT_A,
                "account_id": ACCOUNT_B[0],
                "expected_account_id": ACCOUNT_A[0],
                "product_matches": True,
                "account_matches": False,
            }
        ],
        "mismatches": ["line 900: account mismatch"],
    }


# =========================================================================== regression / architecture


def test_preview_still_reads_only_the_pin_after_odoo_changes(session: Session) -> None:
    from tests.unit.test_p0_prod_18f_1_resale_account_pinning import _preview

    _accepted(session)
    first = _preview(session)
    # Nothing in preview reads current accounting; a later Odoo change cannot move it.
    second = _preview(session)
    assert first.lines[0].account_id == second.lines[0].account_id == ACCOUNT_A[0]
    assert first.lines[0].resale_accounting == second.lines[0].resale_accounting


def test_no_account_values_are_hard_coded_in_18f_2_code() -> None:
    modules = (
        "app/application/execution/resale_execution_accounting.py",
        "app/application/execution/resale_fiscal_position.py",
        "app/erp/odoo/fiscal_position_reader.py",
        "app/application/workbench/vendor_bill_readback.py",
        "app/billing/dto.py",
        "app/billing/builder.py",
        "app/application/execution/vendor_bill_strategy.py",
    )
    for module in modules:
        tree = ast.parse(Path(module).read_text(encoding="utf-8"))
        literals = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant)}
        assert not literals & FORBIDDEN_LITERALS, module


def test_pin_line_evidence_is_the_only_account_source_in_the_validator() -> None:
    source = Path("app/application/execution/resale_execution_accounting.py").read_text(encoding="utf-8")
    call = source[source.index("issue_validated_resale_line_account(\n") :]
    call = call[: call.index(")")]
    assert "account_id=pinned.pre_fiscal_position_account_id" in call


def test_resale_accounting_pin_row_is_not_modified_by_execution(session: Session) -> None:
    _accepted(session)
    before = session.query(ExecutionSourceInvoiceEvidence).one().resale_accounting_pin
    _strategy(session, _validator(session), _RecordingAccountMoveClient()).execute(_request())
    session.expire_all()
    assert session.query(ExecutionSourceInvoiceEvidence).one().resale_accounting_pin == before
