"""PR B: read-only per-invoice-line Workbench child projection (``x_ipp_wb_product_line``).

Three layers:

* the pure line builder (identity, inclusion rule, evidence -> state mapping);
* the child publisher against an Odoo Studio fake that reads values back in Odoo's
  shapes (Many2one ``[id, name]``, archived rows only with an explicit active domain);
* the real composition + reconcile CLI over SQLite-persisted reviews (pending
  PRODUCT_NOT_FOUND -> accepted human product selection), proving idempotency, version
  changes and the disabled gate end to end.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.application.workbench import LineResolution
from app.application.workbench.dto import ReviewReasonsRole, ReviewStatus
from app.application.workbench.exceptions import WorkbenchContractError
from app.application.workbench.product_line_projection import (
    PRODUCT_LINE_STATE_LABELS,
    ProductLineFacts,
    ProductLineMatchState,
    build_product_line_projections,
    product_line_key,
)
from app.application.workbench.projection import WorkbenchProjection
from app.application.workbench.projection_sync_contracts import (
    PRODUCT_LINE_SYNC_WARNING,
    ProductLineSyncOutcome,
    ProjectionSyncOutcome,
    ProjectionSyncResult,
    projection_sync_warnings,
)
from app.application.workflow import ManualReviewReason, ManualReviewReasonCode, WorkflowType
from app.cli.reconcile_workbench_projection import run_reconcile
from app.composition.imports import build_workbench_projection_synchronizer
from app.core.config import Settings
from app.db.base import Base
from app.erp.odoo.workbench_product_line_publisher import (
    OdooWorkbenchProductLineFieldMapping,
    OdooWorkbenchProductLinePublisher,
)
from app.erp.odoo.workbench_projection_publisher import OdooWorkbenchProjectionPublisher
from app.matching import PartnerMatchStatus, ProductMatchStatus
from tests.unit.test_ops_ui_01a_workbench_projection_sync import _mapping as _parent_mapping
from tests.unit.test_p0_prod_19f_review_lifecycle_effective_state import (
    PRODUCT_NOT_FOUND_REASON,
    REVIEW_ID,
    VITEL_PRODUCT_ID,
    VITEL_SKU,
    _accept,
    _command,
    _seed,
)

COMPANY_ID = 1
PARENT_MODEL = "x_ipp_import_workbench"
LINE = OdooWorkbenchProductLineFieldMapping()
LINE_MODEL = LINE.model
ALL_STATES = tuple(state.value for state in ProductLineMatchState)
DFCCD66E = "review:dfccd66e-43e7-546b-b559-49323e79ab9b"
ICT_BULUT_PARTNER_ID = 24


# --------------------------------------------------------------------------- builders


@dataclass(frozen=True)
class _Line:
    line_number: str | None
    seller_item_code: str | None = None
    description: str | None = None
    quantity: Decimal | None = Decimal("1")
    unit_code: str | None = "C62"


@dataclass(frozen=True)
class _Match:
    status: ProductMatchStatus
    product_id: int | None = None
    matched_by: str | None = None


@dataclass(frozen=True)
class _Partner:
    status: PartnerMatchStatus
    partner_id: int | None


@dataclass(frozen=True)
class _Resolution:
    kind: str
    product_id: int | None = None
    product_source: str | None = None
    matched_by: str | None = None


def _reason(code: ManualReviewReasonCode, line: str | None = None) -> ManualReviewReason:
    return ManualReviewReason(code=code, message=code.value, line_number=line)


def _dfccd66e_facts(**overrides: Any) -> ProductLineFacts:
    """Production ICT Bulut review dfccd66e at v3 (read-only survey, 2026-10-08)."""

    values: dict[str, Any] = {
        "review_id": DFCCD66E,
        "company_id": COMPANY_ID,
        "review_version": 3,
        "invoice_number": "ICF2026000008700",
        "reasons": (_reason(ManualReviewReasonCode.PRODUCT_NOT_FOUND, "2"),),
        "reasons_role": ReviewReasonsRole.CURRENT_BLOCKERS,
        "source_lines": (
            _Line("1", "100020", "Microsoft 365 Business Basic", Decimal("5"), "C62"),
            _Line("2", "100021", "Microsoft 365 Business Standard", Decimal("2"), "C62"),
        ),
        "supplier_match": _Partner(PartnerMatchStatus.MATCHED, ICT_BULUT_PARTNER_ID),
        "evidence_lines": {
            "1": _Match(ProductMatchStatus.MATCHED, 393, "supplier_product_code"),
            "2": _Match(ProductMatchStatus.NOT_FOUND),
        },
        "evidence_product_mode": False,
    }
    values.update(overrides)
    return ProductLineFacts(**values)


# --------------------------------------------------------------------------- 1. identity


def test_line_key_is_deterministic_and_bound_to_company_review_and_source_line() -> None:
    key = product_line_key(company_id=1, review_id=DFCCD66E, line_number=" 2 ")

    assert key == f"ipp-pl:v1:1:{DFCCD66E}:2"
    assert key == product_line_key(company_id=1, review_id=DFCCD66E, line_number="2")
    assert key != product_line_key(company_id=2, review_id=DFCCD66E, line_number="2")
    assert key != product_line_key(company_id=1, review_id="review:other", line_number="2")
    with pytest.raises(WorkbenchContractError):
        product_line_key(company_id=1, review_id=DFCCD66E, line_number="  ")


def test_line_key_ignores_version_description_and_seller_code() -> None:
    v2 = build_product_line_projections(_dfccd66e_facts(review_version=2))
    v3 = build_product_line_projections(
        _dfccd66e_facts(
            source_lines=(_Line("1", "OTHER", "Renamed"), _Line("2", "X", "Y")),
        )
    )

    assert [line.line_key for line in v2] == [line.line_key for line in v3]


def test_blank_or_repeated_source_line_numbers_fail_closed() -> None:
    for lines in ((_Line("1"), _Line("1")), (_Line("1"), _Line(None))):
        with pytest.raises(WorkbenchContractError, match="blank or repeated"):
            build_product_line_projections(_dfccd66e_facts(source_lines=lines))


# --------------------------------------------------------------------------- 2. evidence -> state


def test_ict_bulut_dfccd66e_projects_matched_and_product_not_found_lines() -> None:
    line_1, line_2 = build_product_line_projections(_dfccd66e_facts())

    assert (line_1.line_number, line_1.seller_item_code, line_1.description) == (
        "1",
        "100020",
        "Microsoft 365 Business Basic",
    )
    assert line_1.match_state is ProductLineMatchState.MATCHED
    assert (line_1.product_id, line_1.matched_by) == (393, "supplier_product_code")
    assert line_1.supplier_partner_id == ICT_BULUT_PARTNER_ID
    assert (line_1.quantity, line_1.unit_code, line_1.review_version) == (Decimal("5"), "C62", 3)

    assert (line_2.seller_item_code, line_2.description) == ("100021", "Microsoft 365 Business Standard")
    assert line_2.match_state is ProductLineMatchState.PRODUCT_NOT_FOUND
    assert (line_2.product_id, line_2.matched_by) == (None, None)
    assert line_2.supplier_partner_id == ICT_BULUT_PARTNER_ID
    assert line_2.message == "Bu tedarikçi ve satıcı ürün kodu için ürün bulunamadı."


def test_a_current_product_reason_wins_over_a_matched_evidence_result() -> None:
    facts = _dfccd66e_facts(reasons=(_reason(ManualReviewReasonCode.PRODUCT_NOT_FOUND, "1"),))

    line_1 = build_product_line_projections(facts)[0]

    assert line_1.match_state is ProductLineMatchState.PRODUCT_NOT_FOUND
    assert line_1.product_id is None


def test_evidence_ambiguous_and_invalid_input_map_to_their_states() -> None:
    facts = _dfccd66e_facts(
        reasons=(),
        evidence_lines={
            "1": _Match(ProductMatchStatus.MULTIPLE_MATCHES),
            "2": _Match(ProductMatchStatus.INVALID_INPUT),
        },
        evidence_product_mode=True,
    )
    # Neither line matched and no reason: not a product review by itself...
    assert build_product_line_projections(facts) == ()
    # ...but with the reasons the rule engine always emits for them, both are projected.
    facts = replace(
        facts,
        reasons=(
            _reason(ManualReviewReasonCode.PRODUCT_AMBIGUOUS, "1"),
            _reason(ManualReviewReasonCode.PRODUCT_IDENTIFIER_MISSING, "2"),
        ),
    )
    states = [line.match_state for line in build_product_line_projections(facts)]

    assert states == [ProductLineMatchState.PRODUCT_AMBIGUOUS, ProductLineMatchState.IDENTIFIER_MISSING]


def test_no_evidence_with_supplier_blocker_projects_supplier_unresolved_without_a_supplier() -> None:
    facts = _dfccd66e_facts(
        reasons=(
            _reason(ManualReviewReasonCode.SUPPLIER_NOT_FOUND),
            _reason(ManualReviewReasonCode.PRODUCT_NOT_FOUND, "1"),
            _reason(ManualReviewReasonCode.PRODUCT_IDENTIFIER_MISSING, "2"),
        ),
        supplier_match=None,
        evidence_lines=None,
    )

    line_1, line_2 = build_product_line_projections(facts)

    assert line_1.match_state is ProductLineMatchState.SUPPLIER_UNRESOLVED
    assert line_1.supplier_partner_id is None
    assert line_1.message == "Tedarikçi kesinleşmeden ürün eşleştirilemez."
    # A missing identifier is a property of the line, independent of the supplier.
    assert line_2.match_state is ProductLineMatchState.IDENTIFIER_MISSING


def test_no_evidence_without_supplier_blocker_keeps_product_not_found_and_never_invents_a_match() -> None:
    facts = _dfccd66e_facts(supplier_match=None, evidence_lines=None)

    line_1, line_2 = build_product_line_projections(facts)

    # Line 1 has no reason but no evidence either: no product id can be shown.
    assert line_1.match_state is ProductLineMatchState.NO_EVIDENCE
    assert line_1.product_id is None
    assert line_2.match_state is ProductLineMatchState.PRODUCT_NOT_FOUND
    assert line_2.supplier_partner_id is None


def test_unmatched_supplier_evidence_never_projects_a_supplier() -> None:
    facts = _dfccd66e_facts(supplier_match=_Partner(PartnerMatchStatus.MULTIPLE_MATCHES, None))

    assert {line.supplier_partner_id for line in build_product_line_projections(facts)} == {None}


def test_seller_code_missing_is_visible_on_a_product_not_found_line() -> None:
    facts = _dfccd66e_facts(source_lines=(_Line("1", "100020"), _Line("2", None, "Hizmet")))

    line_2 = build_product_line_projections(facts)[1]

    assert line_2.seller_item_code is None
    assert line_2.message == "Ürün bulunamadı; satırda satıcı ürün kodu yok."


# --------------------------------------------------------------------------- 3. inclusion rule


def test_fully_matched_product_mode_review_is_included_so_completed_lines_stay_visible() -> None:
    facts = _dfccd66e_facts(
        reasons=(),
        evidence_lines={
            "1": _Match(ProductMatchStatus.MATCHED, 393, "seller_item_code"),
            "2": _Match(ProductMatchStatus.MATCHED, 394, "seller_item_code"),
        },
        evidence_product_mode=True,
    )

    lines = build_product_line_projections(facts)

    assert [(line.match_state, line.product_id) for line in lines] == [
        (ProductLineMatchState.MATCHED, 393),
        (ProductLineMatchState.MATCHED, 394),
    ]


def test_operating_expense_review_without_product_reasons_is_not_a_product_review() -> None:
    pending_opex = _dfccd66e_facts(
        reasons=(_reason(ManualReviewReasonCode.OPERATING_EXPENSE_MAPPING_REQUIRED),),
        evidence_lines=None,
        supplier_match=None,
    )
    opex_evidence = _dfccd66e_facts(
        reasons=(),
        evidence_lines={"1": _Match(ProductMatchStatus.MATCHED, 393), "2": _Match(ProductMatchStatus.INVALID_INPUT)},
        evidence_product_mode=False,
    )

    assert build_product_line_projections(pending_opex) == ()
    assert build_product_line_projections(opex_evidence) == ()


def test_decided_review_uses_the_accepted_resolution_per_line() -> None:
    facts = _dfccd66e_facts(
        reasons_role=ReviewReasonsRole.DECISION_BASIS,
        evidence_lines=None,
        decision_resolutions={
            "1": _Resolution("product", 393, "automatic", "supplier_product_code"),
            "2": _Resolution("account_only"),
        },
    )

    line_1, line_2 = build_product_line_projections(facts)

    assert (line_1.match_state, line_1.product_id, line_1.matched_by) == (
        ProductLineMatchState.MATCHED,
        393,
        "supplier_product_code",
    )
    assert line_1.message == "Kabul edilen kararla ürün belirlendi."
    assert line_2.match_state is ProductLineMatchState.RESOLVED_WITHOUT_PRODUCT
    assert line_2.product_id is None


def test_decided_unresolved_or_missing_resolution_is_no_evidence_never_resolved() -> None:
    facts = _dfccd66e_facts(
        reasons_role=ReviewReasonsRole.DECISION_BASIS,
        decision_resolutions={"1": _Resolution("unresolved")},
    )

    states = [line.match_state for line in build_product_line_projections(facts)]

    assert states == [ProductLineMatchState.NO_EVIDENCE, ProductLineMatchState.NO_EVIDENCE]


def test_decided_whole_invoice_accounting_review_without_product_reasons_is_excluded() -> None:
    facts = _dfccd66e_facts(
        reasons=(),
        reasons_role=ReviewReasonsRole.DECISION_BASIS,
        decision_resolutions={"1": _Resolution("fixed_asset"), "2": _Resolution("fixed_asset")},
    )

    assert build_product_line_projections(facts) == ()


def test_every_state_has_a_studio_selection_label() -> None:
    assert set(PRODUCT_LINE_STATE_LABELS) == set(ProductLineMatchState)
    assert len(set(PRODUCT_LINE_STATE_LABELS.values())) == len(ProductLineMatchState)


# --------------------------------------------------------------------------- Odoo Studio fake


class TwoModelStudio:
    """Parent + child Studio models, read back in Odoo's shapes."""

    MANY2ONE = {
        PARENT_MODEL: frozenset({"x_studio_company", "x_studio_currency_id", "x_studio_vendor_bill"}),
        LINE_MODEL: frozenset({LINE.parent, LINE.supplier, LINE.product}),
    }

    def __init__(self, *, line_states: tuple[str, ...] = ALL_STATES) -> None:
        self.rows: dict[str, dict[int, dict[str, Any]]] = {PARENT_MODEL: {}, LINE_MODEL: {}}
        self.creates: list[tuple[str, dict[str, Any]]] = []
        self.writes: list[tuple[str, int, dict[str, Any]]] = []
        self.calls: list[tuple[str, str]] = []
        self.line_states = line_states
        self._next_id = {PARENT_MODEL: 1, LINE_MODEL: 1000}

    def search_read(self, *, model: str, domain: list[Any], fields: list[str], limit: int, offset: int = 0):
        self.calls.append(("search_read", model))
        if model == "res.currency":
            return ({"id": 31, "name": "TRY"},)
        rows = self.rows[model]
        explicit_active = any(clause[0] == LINE.active for clause in domain)
        matches = [
            (record_id, row)
            for record_id, row in rows.items()
            if all(_matches(row.get(name), op, value) for name, op, value in domain)
            # Odoo's active_test: archived rows are invisible unless the domain names the field.
            and (model != LINE_MODEL or explicit_active or row.get(LINE.active) is not False)
        ]
        return tuple(self._read_shape(model, record_id, row, fields) for record_id, row in matches)[:limit]

    def create(self, *, model: str, values: dict[str, Any]) -> int:
        self.calls.append(("create", model))
        record_id = self._next_id[model]
        self._next_id[model] += 1
        self.creates.append((model, dict(values)))
        self.rows[model][record_id] = dict(values)
        return record_id

    def write(self, *, model: str, record_id: int, values: dict[str, Any]) -> None:
        self.calls.append(("write", model))
        self.writes.append((model, record_id, dict(values)))
        self.rows[model][record_id].update(values)

    def read_selection_values(self, *, model: str, field_name: str) -> tuple[str, ...]:
        self.calls.append(("selection", model))
        if model == LINE_MODEL:
            return self.line_states if field_name == LINE.match_state else ()
        return {
            "x_studio_review_status": ("Pending Review", "Decision Submitted", "Resolved", "Dismissed"),
            "x_studio_workflow": ("Vendor Bill", "RFQ", "Expense", "Asset", "Subscription", "Manual Review"),
            "x_studio_execution_status": ("Executed", "Already Executed"),
        }.get(field_name, ())

    def line_rows(self) -> list[dict[str, Any]]:
        return list(self.rows[LINE_MODEL].values())

    def child_calls(self) -> list[tuple[str, str]]:
        return [call for call in self.calls if call[1] == LINE_MODEL]

    def _read_shape(self, model: str, record_id: int, row: dict[str, Any], fields: list[str]) -> dict[str, Any]:
        shaped: dict[str, Any] = {}
        for name in fields:
            if name == "id":
                shaped["id"] = record_id
                continue
            value = row.get(name)
            if name in self.MANY2ONE[model]:
                shaped[name] = [value, f"Record {value}"] if value else False
            elif value is None:
                shaped[name] = 0 if name in {"x_studio_review_version", LINE.review_version} else False
            else:
                shaped[name] = value
        return shaped


def _matches(stored: Any, op: str, value: Any) -> bool:
    if op == "in":
        return (stored if stored is not None else True) in value
    return stored == value


def _line_publisher(studio: TwoModelStudio) -> OdooWorkbenchProjectionPublisher:
    return OdooWorkbenchProjectionPublisher(
        adapter=studio,
        mapping=_parent_mapping(),
        product_line_publisher=OdooWorkbenchProductLinePublisher(adapter=studio, mapping=LINE),
    )


def _projection(**overrides: Any) -> WorkbenchProjection:
    values: dict[str, Any] = {
        "review_id": DFCCD66E,
        "company_id": COMPANY_ID,
        "invoice_id": "ICF2026000008700",
        "version": 3,
        "status": ReviewStatus.PENDING_REVIEW,
        "invoice_number": "ICF2026000008700",
        "supplier_name": "ICT BULUT BİLİŞİM A.Ş.",
        "supplier_tax_number": "4650459971",
        "invoice_date": None,
        "currency": "TRY",
        "total_amount": Decimal("100.00"),
        "workflow": WorkflowType.MANUAL_REVIEW,
        "review_reasons_role": ReviewReasonsRole.CURRENT_BLOCKERS,
        "product_lines": build_product_line_projections(_dfccd66e_facts()),
    }
    values.update(overrides)
    return WorkbenchProjection(**values)


# --------------------------------------------------------------------------- 4. publisher upsert


def test_first_apply_creates_parent_and_one_child_per_line_under_that_parent() -> None:
    studio = TwoModelStudio()

    result = _line_publisher(studio).sync_projection(_projection(), apply=True)

    assert result.outcome is ProjectionSyncOutcome.CREATED
    assert [line.outcome for line in result.line_results] == [ProductLineSyncOutcome.CREATED] * 2
    rows = studio.line_rows()
    assert {row[LINE.parent] for row in rows} == {result.odoo_record_id}
    assert [(row[LINE.line_number], row[LINE.match_state], row[LINE.product]) for row in rows] == [
        ("1", "matched", 393),
        ("2", "product_not_found", None),
    ]
    assert rows[0][LINE.active] is True
    assert rows[0][LINE.name] == "ICF2026000008700 · Satır 1"
    assert rows[0][LINE.line_key] == f"ipp-pl:v1:1:{DFCCD66E}:1"
    assert rows[0][LINE.supplier] == ICT_BULUT_PARTNER_ID
    assert rows[0][LINE.quantity] == 5.0


def test_second_apply_is_a_no_op_with_zero_writes() -> None:
    studio = TwoModelStudio()
    publisher = _line_publisher(studio)
    publisher.sync_projection(_projection(), apply=True)
    writes = (len(studio.creates), len(studio.writes))

    result = publisher.sync_projection(_projection(), apply=True)

    assert result.outcome is ProjectionSyncOutcome.NO_CHANGE
    assert [line.outcome for line in result.line_results] == [ProductLineSyncOutcome.NO_CHANGE] * 2
    assert (len(studio.creates), len(studio.writes)) == writes
    assert len(studio.line_rows()) == 2


def test_review_version_change_updates_the_same_child_rows_without_duplicates() -> None:
    studio = TwoModelStudio()
    publisher = _line_publisher(studio)
    publisher.sync_projection(_projection(), apply=True)
    ids_before = sorted(studio.rows[LINE_MODEL])
    # v4: line 2 got mapped (e.g. #208) -> MATCHED 394.
    facts = _dfccd66e_facts(
        review_version=4,
        reasons=(),
        evidence_lines={
            "1": _Match(ProductMatchStatus.MATCHED, 393, "supplier_product_code"),
            "2": _Match(ProductMatchStatus.MATCHED, 394, "supplier_product_code"),
        },
        evidence_product_mode=True,
    )

    result = publisher.sync_projection(
        _projection(version=4, product_lines=build_product_line_projections(facts)), apply=True
    )

    assert sorted(studio.rows[LINE_MODEL]) == ids_before
    outcomes = {line.line_number: line for line in result.line_results}
    assert outcomes["1"].outcome is ProductLineSyncOutcome.UPDATED
    assert {change.field for change in outcomes["1"].changes} == {LINE.review_version}
    assert {change.field for change in outcomes["2"].changes} == {
        LINE.review_version,
        LINE.match_state,
        LINE.product,
        LINE.matched_by,
        LINE.message,
    }
    line_2 = studio.rows[LINE_MODEL][ids_before[1]]
    assert (line_2[LINE.match_state], line_2[LINE.product]) == ("matched", 394)


def test_dry_run_plans_creates_with_zero_writes() -> None:
    studio = TwoModelStudio()

    result = _line_publisher(studio).sync_projection(_projection(), apply=False)

    assert result.outcome is ProjectionSyncOutcome.CREATED
    assert not result.applied
    assert [line.outcome for line in result.line_results] == [ProductLineSyncOutcome.CREATED] * 2
    assert studio.creates == [] and studio.writes == []


def test_unprojected_child_is_archived_not_deleted_and_reactivated_when_projected_again() -> None:
    studio = TwoModelStudio()
    publisher = _line_publisher(studio)
    publisher.sync_projection(_projection(), apply=True)

    gone = publisher.sync_projection(_projection(product_lines=()), apply=True)

    assert [line.outcome for line in gone.line_results] == [ProductLineSyncOutcome.DEACTIVATED] * 2
    assert len(studio.line_rows()) == 2
    assert {row[LINE.active] for row in studio.line_rows()} == {False}
    # Archived rows are not re-archived.
    assert publisher.sync_projection(_projection(product_lines=()), apply=True).line_results == ()

    back = publisher.sync_projection(_projection(), apply=True)

    assert [line.outcome for line in back.line_results] == [ProductLineSyncOutcome.UPDATED] * 2
    assert {change.field for change in back.line_results[0].changes} == {LINE.active}
    assert {row[LINE.active] for row in studio.line_rows()} == {True}
    assert len(studio.line_rows()) == 2


def test_duplicate_child_rows_fail_the_lines_closed_without_touching_the_parent_result() -> None:
    studio = TwoModelStudio()
    publisher = _line_publisher(studio)
    publisher.sync_projection(_projection(), apply=True)
    first = next(iter(studio.rows[LINE_MODEL].values()))
    studio.rows[LINE_MODEL][9999] = dict(first)
    writes_before = len(studio.writes)

    result = publisher.sync_projection(_projection(version=4), apply=True)

    assert result.outcome is ProjectionSyncOutcome.UPDATED  # the parent still converged
    assert result.line_error is not None and "Duplicate" in result.line_error
    assert result.line_results == ()
    assert [model for model, *_ in studio.writes[writes_before:]] == [PARENT_MODEL]
    assert projection_sync_warnings(result) == (PRODUCT_LINE_SYNC_WARNING,)


def test_missing_selection_keys_fail_the_contract_before_any_child_write() -> None:
    studio = TwoModelStudio(line_states=ALL_STATES[:-1])

    result = _line_publisher(studio).sync_projection(_projection(), apply=True)

    assert result.outcome is ProjectionSyncOutcome.CREATED
    assert "no_evidence" in (result.line_error or "")
    assert studio.line_rows() == []


def test_stale_parent_skips_its_children() -> None:
    studio = TwoModelStudio()
    publisher = _line_publisher(studio)
    publisher.sync_projection(_projection(version=5), apply=True)
    calls_before = len(studio.child_calls())

    result = publisher.sync_projection(_projection(version=3), apply=True)

    assert result.outcome is ProjectionSyncOutcome.SKIPPED_STALE
    assert result.line_results == () and result.line_error is None
    assert len(studio.child_calls()) == calls_before


def test_uncomposed_lines_or_no_line_publisher_never_call_the_child_model() -> None:
    studio = TwoModelStudio()
    _line_publisher(studio).sync_projection(_projection(product_lines=None), apply=True)
    OdooWorkbenchProjectionPublisher(adapter=studio, mapping=_parent_mapping()).sync_projection(
        _projection(), apply=True
    )

    assert studio.child_calls() == []


def test_line_derivation_error_is_reported_without_child_calls() -> None:
    studio = TwoModelStudio()

    result = _line_publisher(studio).sync_projection(
        _projection(product_lines=None, product_line_error="Source lines repeated."), apply=True
    )

    assert result.outcome is ProjectionSyncOutcome.CREATED
    assert result.line_error == "Source lines repeated."
    assert studio.child_calls() == []


def test_parent_failure_warning_still_wins_over_line_warning() -> None:
    result = ProjectionSyncResult(
        review_id="r", outcome=ProjectionSyncOutcome.ERROR, applied=False, error="x", line_error="y"
    )

    assert projection_sync_warnings(result) != (PRODUCT_LINE_SYNC_WARNING,)


# --------------------------------------------------------------------------- 5. mapping contract


def test_mapping_defaults_are_the_runbook_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [key for key in __import__("os").environ if key.startswith("ODOO_WORKBENCH_PRODUCT_LINE_")]:
        monkeypatch.delenv(name)

    mapping = OdooWorkbenchProductLineFieldMapping.from_environment()

    assert mapping == OdooWorkbenchProductLineFieldMapping()
    assert mapping.model == "x_ipp_wb_product_line"
    assert mapping.parent == "x_studio_ipp_workbench_id"
    assert mapping.match_state == "x_studio_ipp_match_state"


def test_mapping_env_override_and_invalid_contracts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ODOO_WORKBENCH_PRODUCT_LINE_PRODUCT_FIELD", "x_studio_other_product")
    assert OdooWorkbenchProductLineFieldMapping.from_environment().product == "x_studio_other_product"

    monkeypatch.setenv("ODOO_WORKBENCH_PRODUCT_LINE_PRODUCT_FIELD", " ")
    with pytest.raises(WorkbenchContractError, match="must be non-empty"):
        OdooWorkbenchProductLineFieldMapping.from_environment()
    monkeypatch.delenv("ODOO_WORKBENCH_PRODUCT_LINE_PRODUCT_FIELD")

    monkeypatch.setenv("ODOO_WORKBENCH_PRODUCT_LINE_MODEL", "res.partner")
    with pytest.raises(WorkbenchContractError, match="Studio"):
        OdooWorkbenchProductLineFieldMapping.from_environment()
    with pytest.raises(WorkbenchContractError, match="distinct"):
        OdooWorkbenchProductLineFieldMapping(product="x_studio_ipp_supplier_id")


# --------------------------------------------------------------------------- 6. composition + reconcile (SQLite)


@pytest.fixture()
def engine(tmp_path):
    db_engine = create_engine(f"sqlite:///{tmp_path / 'hub.db'}")
    Base.metadata.create_all(db_engine)
    yield db_engine
    db_engine.dispose()


def _settings(*, lines: bool) -> Settings:
    return Settings(odoo_workbench_product_line_projection_enabled=lines)


def _synchronizer(engine, studio: TwoModelStudio, *, lines: bool):
    return build_workbench_projection_synchronizer(
        engine=engine,
        settings=_settings(lines=lines),
        projection_adapter=studio,
        mapping=_parent_mapping(),
        product_mapping_enabled=False,
    )


def _seed_pending(engine) -> None:
    with sessionmaker(bind=engine)() as db:
        _seed(db, product_id=None, seller_item_code=VITEL_SKU, reasons=(PRODUCT_NOT_FOUND_REASON,))
        db.commit()


def _reconcile(synchronizer, *, apply: bool) -> tuple[str, Any]:
    out = io.StringIO()
    report = run_reconcile(synchronizer, review_ids=(REVIEW_ID,), company_id=COMPANY_ID, apply=apply, out=out)
    return out.getvalue(), report


def test_end_to_end_pending_review_projects_lines_through_real_composition(engine) -> None:
    _seed_pending(engine)
    studio = TwoModelStudio()
    synchronizer = _synchronizer(engine, studio, lines=True)

    dry_text, dry = _reconcile(synchronizer, apply=False)

    assert "line CREATE     1 ipp-pl:v1:1:review:vitel:1" in dry_text
    assert "Lines: CREATE=1 UPDATE=0 NO_CHANGE=0 DEACTIVATE=0 ERROR=0 | applied=False" in dry_text
    assert studio.creates == [] and studio.writes == []
    assert not dry.has_errors

    _, applied = _reconcile(synchronizer, apply=True)

    assert applied.results[0].outcome is ProjectionSyncOutcome.CREATED
    (row,) = studio.line_rows()
    assert row[LINE.match_state] == "product_not_found"
    assert row[LINE.seller_code] == VITEL_SKU
    assert row[LINE.supplier] == 101  # the evidence partner the product matching ran under
    assert row[LINE.product] is None
    assert row[LINE.review_version] == 1

    again_text, again = _reconcile(synchronizer, apply=True)

    assert "Totals: CREATE=0 UPDATE=0 NO_CHANGE=1" in again_text
    assert "Lines: CREATE=0 UPDATE=0 NO_CHANGE=1 DEACTIVATE=0 ERROR=0" in again_text
    assert "line " not in again_text.replace("Lines:", "")
    assert len(studio.line_rows()) == 1
    assert not again.has_errors


def test_end_to_end_accepted_human_selection_updates_the_same_child_row(engine) -> None:
    _seed_pending(engine)
    studio = TwoModelStudio()
    synchronizer = _synchronizer(engine, studio, lines=True)
    _reconcile(synchronizer, apply=True)
    (record_id,) = studio.rows[LINE_MODEL]
    with sessionmaker(bind=engine)() as db:
        _accept(db, _command((LineResolution(line_number="1", selected_product_id=VITEL_PRODUCT_ID),)))

    text, report = _reconcile(synchronizer, apply=True)

    assert report.results[0].outcome is ProjectionSyncOutcome.UPDATED
    assert "line UPDATE     1" in text
    assert list(studio.rows[LINE_MODEL]) == [record_id]
    row = studio.rows[LINE_MODEL][record_id]
    assert (row[LINE.match_state], row[LINE.product], row[LINE.review_version]) == ("matched", VITEL_PRODUCT_ID, 2)
    assert row[LINE.matched_by] == "human_selected"
    assert row[LINE.supplier] == 101
    assert _reconcile(synchronizer, apply=True)[1].results[0].line_results[0].outcome is (
        ProductLineSyncOutcome.NO_CHANGE
    )


def test_disabled_gate_keeps_parent_projection_identical_and_never_calls_the_child_model(engine) -> None:
    _seed_pending(engine)
    disabled, enabled = TwoModelStudio(), TwoModelStudio()

    disabled_text, _ = _reconcile(_synchronizer(engine, disabled, lines=False), apply=True)
    _reconcile(_synchronizer(engine, enabled, lines=True), apply=True)

    assert disabled.child_calls() == []
    assert "Lines:" not in disabled_text and "    line " not in disabled_text
    strip = {"x_studio_last_sync_at"}
    assert [{k: v for k, v in values.items() if k not in strip} for model, values in disabled.creates] == [
        {k: v for k, v in values.items() if k not in strip}
        for model, values in enabled.creates
        if model == PARENT_MODEL
    ]


def test_enabled_with_an_invalid_child_contract_fails_at_composition(engine, monkeypatch) -> None:
    monkeypatch.setenv("ODOO_WORKBENCH_PRODUCT_LINE_MODEL", "product.product")

    with pytest.raises(WorkbenchContractError):
        _synchronizer(engine, TwoModelStudio(), lines=True)
    # Disabled, the same invalid child env is never even read.
    _synchronizer(engine, TwoModelStudio(), lines=False)


def test_reconcile_exit_reports_line_errors(engine) -> None:
    _seed_pending(engine)
    studio = TwoModelStudio(line_states=())

    text, report = _reconcile(_synchronizer(engine, studio, lines=True), apply=True)

    assert report.results[0].outcome is ProjectionSyncOutcome.CREATED
    assert "line ERROR:" in text
    assert "ERROR=1 | applied=True" in text.splitlines()[-1]
    assert report.has_errors
    assert studio.line_rows() == []


def test_unexpected_line_publisher_failure_never_turns_a_written_parent_into_error() -> None:
    class Exploding:
        def sync_lines(self, projection, *, parent_record_id, apply):
            raise RuntimeError("boom with internal detail")

    studio = TwoModelStudio()
    publisher = OdooWorkbenchProjectionPublisher(
        adapter=studio, mapping=_parent_mapping(), product_line_publisher=Exploding()
    )

    result = publisher.sync_projection(_projection(), apply=True)

    assert result.outcome is ProjectionSyncOutcome.CREATED and result.applied
    assert result.line_error == "Odoo Workbench product line sync failed."
    assert "internal detail" not in result.line_error


def test_unexpected_line_fact_failure_is_isolated_from_the_parent(engine, monkeypatch) -> None:
    _seed_pending(engine)
    studio = TwoModelStudio()
    calls: list[str] = []

    def exploding(*args: Any, **kwargs: Any) -> Any:
        calls.append("called")
        raise KeyError("corrupt")

    monkeypatch.setattr("app.composition.imports._product_line_read_facts", exploding)

    result = _synchronizer(engine, studio, lines=True).sync(review_id=REVIEW_ID, company_id=COMPANY_ID)

    assert calls == ["called"]
    assert result.outcome is ProjectionSyncOutcome.CREATED
    assert result.line_error == "Workbench product lines could not be derived from committed Hub evidence."
    assert studio.child_calls() == []
