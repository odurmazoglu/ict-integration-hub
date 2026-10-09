"""PR C: existing-product mapping requested on one Workbench child product line row.

The child row (``x_ipp_wb_product_line``) carries a small typed request (product, ready,
snapshotted version, requester, time). The Hub derives the line identity only from the
Hub-owned child projection cross-checked against the deterministic line key and the
parent Workbench row, then runs the *same* #208 handler and ``MapExistingProductUseCase``.

The end-to-end tests drive the REAL reader/acknowledger (over an in-memory Odoo with both
Workbench models), the REAL ingestion workflow, the REAL handler and the REAL use case
(writing through the real supplierinfo writer into the #208 in-memory Odoo).
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any

import pytest

from app.application.workbench.exceptions import WorkbenchCandidateReadError, WorkbenchContractError
from app.application.workbench.operator_request_handlers import ProductMappingRequestHandler
from app.application.workbench.operator_request_ingestion import (
    ACTION_PERMISSIONS,
    OperatorActor,
    OperatorActorDirectory,
    OperatorRequest,
    OperatorRequestAction,
    OperatorRequestIngestionWorkflow,
    OperatorRequestOutcome,
    OperatorRequestReadFailure,
    operator_request_key,
)
from app.application.workbench.product_line_projection import product_line_key
from app.core.config import Settings
from app.core.runtime_checks import runtime_configuration_errors
from app.erp.exceptions import ErpRepositoryError
from app.erp.odoo.workbench_operator_request_reader import (
    OdooOperatorRequestFieldMapping,
    OdooOperatorRequestReader,
)
from app.erp.odoo.workbench_product_line_publisher import OdooWorkbenchProductLineFieldMapping
from app.erp.odoo.workbench_product_line_request_reader import (
    DUPLICATE_LINE_MESSAGE,
    IDENTITY_MISMATCH_MESSAGE,
    NOT_CURRENT_MESSAGE,
    PARENT_MISMATCH_MESSAGE,
    OdooProductLineRequestAcknowledger,
    OdooProductLineRequestFieldMapping,
    OdooProductLineRequestReader,
)
from tests.unit.test_adr_0013_operator_request_ingestion import DECIDER, FakeIssuer, FakeLedger
from tests.unit.test_product_not_found_operator_flow import (
    COMPANY,
    ICT_BULUT,
    REVIEW,
    Harness,
    _ict_8699,
    _lines_with_reason,
)

CHILD = "x_ipp_wb_product_line"
PARENT = "x_ipp_import_workbench"
PARENT_ROW = 24
LINE_ROW = 21
OPERATOR_UID = 7
OPERATOR = OperatorActor(actor="operator", permissions=frozenset({"workbench_review_decide", "workbench_execute"}))
SUBMITTED = "2026-10-09 10:00:00"
SUBMITTED_AT = datetime(2026, 10, 9, 10, 0, tzinfo=UTC)
TGDR_PRODUCT = 502  # "SQL Server Standard 2 Core" in the #208 in-memory Odoo


# --------------------------------------------------------------------------- in-memory Workbench


class WorkbenchOdoo:
    """Both Workbench Studio models behind the JSON-2 adapter shape (search_read/write)."""

    def __init__(self, *, children: list[dict[str, Any]], parents: list[dict[str, Any]]) -> None:
        self.rows: dict[str, dict[int, dict[str, Any]]] = {
            CHILD: {row["id"]: row for row in children},
            PARENT: {row["id"]: row for row in parents},
        }
        self.reads: list[tuple[str, list[Any], list[str]]] = []
        self.writes: list[tuple[str, int, dict[str, Any]]] = []
        self.fail_reads_of: set[str] = set()
        self.fail_writes = 0

    def search_read(self, *, model: str, domain: list[Any], fields: list[str], limit: int, offset: int = 0):
        self.reads.append((model, domain, fields))
        if model in self.fail_reads_of:
            raise ErpRepositoryError("Odoo request timed out.")
        rows = list(self.rows[model].values())
        for name, op, value in domain:
            if op == "=":
                rows = [row for row in rows if _plain(row.get(name)) == value]
            elif op == "in":
                rows = [row for row in rows if row.get(name) in value]
        return tuple({name: copy.deepcopy(row.get(name, False)) for name in fields} for row in rows[:limit])

    def write(self, *, model: str, record_id: int, values: dict[str, Any]) -> None:
        if self.fail_writes:
            self.fail_writes -= 1
            raise ErpRepositoryError("Odoo request timed out.")
        self.writes.append((model, record_id, dict(values)))
        self.rows[model][record_id].update(values)

    def child(self, record_id: int = LINE_ROW) -> dict[str, Any]:
        return self.rows[CHILD][record_id]


def _plain(value: Any) -> Any:
    return value[0] if isinstance(value, list) and value else value


def _child(line: str = "2", **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": LINE_ROW,
        "x_studio_ipp_workbench_id": [PARENT_ROW, REVIEW],
        "x_studio_ipp_line_key": product_line_key(company_id=COMPANY, review_id=REVIEW, line_number=line),
        "x_studio_ipp_is_current": True,
        "x_studio_ipp_review_id": REVIEW,
        "x_studio_ipp_company_id": COMPANY,
        "x_studio_ipp_review_version": 2,
        "x_studio_ipp_line_number": line,
        "x_studio_ipp_seller_code": "TGDR",
        "x_studio_ipp_description": "MS SQL Server Standart Edition 2 Core Lisans",
        "x_studio_ipp_match_state": "product_not_found",
        "x_studio_ipp_product_id": False,
        "x_studio_ipp_req_product": [TGDR_PRODUCT, "SQL Server Standard 2 Core"],
        "x_studio_ipp_req_ready": True,
        "x_studio_ipp_req_version": 2,
        "x_studio_ipp_req_requested_by": [OPERATOR_UID, "Operator"],
        "x_studio_ipp_req_requested_at": SUBMITTED,
        "x_studio_ipp_req_result": False,
        "x_studio_ipp_req_message": False,
        "x_studio_ipp_req_processed_at": False,
    }
    row.update(overrides)
    return row


def _parent(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {"id": PARENT_ROW, "x_studio_review_id": REVIEW, "x_studio_company": [COMPANY, "ICT"]}
    row.update(overrides)
    return row


def _parent_mapping() -> OdooOperatorRequestFieldMapping:
    return OdooOperatorRequestFieldMapping(
        model=PARENT,
        review_id="x_studio_review_id",
        company_id="x_studio_company",
        ready="x_studio_ipp_req_ready",
        action="x_studio_ipp_req_action",
        expected_version="x_studio_ipp_req_version",
        requested_by="x_studio_ipp_req_requested_by",
        requested_at="x_studio_ipp_req_requested_at",
        result="x_studio_ipp_req_result",
        message="x_studio_ipp_req_message",
        processed_at="x_studio_ipp_req_processed_at",
        line="x_studio_ipp_req_line",
        product="x_studio_ipp_req_product",
    )


def _mapping() -> OdooProductLineRequestFieldMapping:
    return OdooProductLineRequestFieldMapping(
        lines=OdooWorkbenchProductLineFieldMapping(),
        parent_model=PARENT,
        parent_review_id="x_studio_review_id",
        parent_company_id="x_studio_company",
    )


def _read(odoo: WorkbenchOdoo) -> tuple[OperatorRequest | OperatorRequestReadFailure, ...]:
    return OdooProductLineRequestReader(adapter=odoo, mapping=_mapping()).list_pending(company_id=COMPANY, limit=25)


def _only_failure(odoo: WorkbenchOdoo) -> str:
    (item,) = _read(odoo)
    assert isinstance(item, OperatorRequestReadFailure), item
    assert item.odoo_record_id == LINE_ROW
    return item.message


# --------------------------------------------------------------------------- Studio contract


def test_request_field_defaults_are_the_runbook_contract_and_disjoint_from_projection_fields() -> None:
    mapping = _mapping()

    assert mapping.request_fields() == (
        "x_studio_ipp_req_product",
        "x_studio_ipp_req_ready",
        "x_studio_ipp_req_version",
        "x_studio_ipp_req_requested_by",
        "x_studio_ipp_req_requested_at",
        "x_studio_ipp_req_result",
        "x_studio_ipp_req_message",
        "x_studio_ipp_req_processed_at",
    )
    projection = {getattr(mapping.lines, name) for name in mapping.lines.__dataclass_fields__ if name != "model"}
    assert not set(mapping.request_fields()) & projection


def test_a_request_field_mapped_onto_a_hub_owned_projection_field_is_a_contract_error() -> None:
    with pytest.raises(WorkbenchContractError, match="overlap"):
        OdooProductLineRequestFieldMapping(
            lines=OdooWorkbenchProductLineFieldMapping(),
            parent_model=PARENT,
            parent_review_id="x_studio_review_id",
            parent_company_id="x_studio_company",
            product="x_studio_ipp_product_id",
        )
    with pytest.raises(WorkbenchContractError, match="distinct"):
        OdooProductLineRequestFieldMapping(
            lines=OdooWorkbenchProductLineFieldMapping(),
            parent_model=PARENT,
            parent_review_id="x_studio_review_id",
            parent_company_id="x_studio_company",
            result="x_studio_ipp_req_message",
        )


def test_environment_overrides_request_fields_and_rejects_blank_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ODOO_WORKBENCH_PRODUCT_LINE_REQ_PRODUCT_FIELD", "x_studio_other_product")
    mapping = OdooProductLineRequestFieldMapping.from_environment(parent=_parent_mapping())
    assert mapping.product == "x_studio_other_product"
    assert (mapping.parent_model, mapping.parent_review_id, mapping.parent_company_id) == (
        PARENT,
        "x_studio_review_id",
        "x_studio_company",
    )

    monkeypatch.setenv("ODOO_WORKBENCH_PRODUCT_LINE_REQ_READY_FIELD", " ")
    with pytest.raises(WorkbenchContractError, match="READY_FIELD"):
        OdooProductLineRequestFieldMapping.from_environment(parent=_parent_mapping())


def test_reader_never_reads_operator_editable_display_fields() -> None:
    read = set(_mapping().read_fields())

    for display in (
        "x_studio_ipp_seller_code",
        "x_studio_ipp_description",
        "x_studio_ipp_supplier_id",
        "x_studio_ipp_match_state",
        "x_studio_ipp_product_id",
        "x_studio_ipp_review_version",
    ):
        assert display not in read


# --------------------------------------------------------------------------- reader: positive


def test_ready_child_row_becomes_a_line_mapping_request_with_hub_owned_identity() -> None:
    odoo = WorkbenchOdoo(
        children=[_child(x_studio_ipp_seller_code="TAMPERED", x_studio_ipp_description="Bambaşka ürün")],
        parents=[_parent()],
    )

    (request,) = _read(odoo)

    assert request == OperatorRequest(
        odoo_record_id=LINE_ROW,
        review_id=REVIEW,
        company_id=COMPANY,
        action=OperatorRequestAction.PRODUCT_LINE_MAPPING,
        expected_version=2,
        requested_by_odoo_user_id=OPERATOR_UID,
        requested_at=SUBMITTED_AT,
        line_number="2",
        product_id=TGDR_PRODUCT,
    )
    model, domain, _fields = odoo.reads[0]
    assert model == CHILD
    assert domain == [["x_studio_ipp_company_id", "=", COMPANY], ["x_studio_ipp_req_ready", "=", True]]
    assert odoo.reads[1][:2] == (PARENT, [["id", "in", [PARENT_ROW]]])
    assert odoo.writes == []


def test_no_ready_rows_reads_nothing_else() -> None:
    odoo = WorkbenchOdoo(children=[_child(x_studio_ipp_req_ready=False)], parents=[_parent()])

    assert _read(odoo) == ()
    assert [model for model, _domain, _fields in odoo.reads] == [CHILD]


# --------------------------------------------------------------------------- reader: fail closed


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"x_studio_ipp_is_current": False}, NOT_CURRENT_MESSAGE),
        # identity fields that disagree with the Hub key (or each other)
        ({"x_studio_ipp_line_number": "1"}, IDENTITY_MISMATCH_MESSAGE),
        ({"x_studio_ipp_line_key": "ipp-pl:v1:1:review:other:2"}, IDENTITY_MISMATCH_MESSAGE),
        ({"x_studio_ipp_review_id": "review:other"}, IDENTITY_MISMATCH_MESSAGE),
        ({"x_studio_ipp_line_key": False}, IDENTITY_MISMATCH_MESSAGE),
        ({"x_studio_ipp_line_number": False}, IDENTITY_MISMATCH_MESSAGE),
        # mismatched or missing parent
        ({"x_studio_ipp_workbench_id": [99, "x"]}, PARENT_MISMATCH_MESSAGE),
        ({"x_studio_ipp_workbench_id": False}, PARENT_MISMATCH_MESSAGE),
        # malformed request inputs
        ({"x_studio_ipp_req_product": False}, "Eşleştirilecek Odoo ürünü seçilmelidir."),
        ({"x_studio_ipp_req_version": 0}, "İnceleme sürümü geçersiz."),
        ({"x_studio_ipp_req_requested_by": False}, "İsteyen kullanıcı eksik."),
    ],
)
def test_inconsistent_or_incomplete_rows_fail_closed(overrides: dict[str, Any], message: str) -> None:
    odoo = WorkbenchOdoo(children=[_child(**overrides)], parents=[_parent()])

    assert _only_failure(odoo) == message


def test_row_without_a_submit_snapshot_is_refused() -> None:
    odoo = WorkbenchOdoo(children=[_child(x_studio_ipp_req_requested_at=False)], parents=[_parent()])

    assert "Eşleştir" in _only_failure(odoo)


def test_parent_row_of_another_review_or_company_fails_closed() -> None:
    assert (
        _only_failure(WorkbenchOdoo(children=[_child()], parents=[_parent(x_studio_review_id="review:other")]))
        == PARENT_MISMATCH_MESSAGE
    )
    assert (
        _only_failure(WorkbenchOdoo(children=[_child()], parents=[_parent(x_studio_company=[2, "Other"])]))
        == PARENT_MISMATCH_MESSAGE
    )


def test_cross_company_child_identity_fails_closed_even_if_listed() -> None:
    # A row listed for company 1 whose Hub key says company 2 is never processed as company 1.
    key = product_line_key(company_id=2, review_id=REVIEW, line_number="2")
    odoo = WorkbenchOdoo(children=[_child(x_studio_ipp_line_key=key)], parents=[_parent()])

    assert _only_failure(odoo) == IDENTITY_MISMATCH_MESSAGE


def test_two_ready_rows_claiming_the_same_line_are_both_refused() -> None:
    odoo = WorkbenchOdoo(children=[_child(), _child(id=22)], parents=[_parent()])

    first, second = _read(odoo)

    assert isinstance(first, OperatorRequestReadFailure) and isinstance(second, OperatorRequestReadFailure)
    assert first.message == second.message == DUPLICATE_LINE_MESSAGE


def test_unreadable_odoo_is_a_transient_read_error() -> None:
    for model in (CHILD, PARENT):
        odoo = WorkbenchOdoo(children=[_child()], parents=[_parent()])
        odoo.fail_reads_of.add(model)
        with pytest.raises(WorkbenchCandidateReadError):
            _read(odoo)


def test_the_parent_reader_never_accepts_the_child_line_action() -> None:
    row = {
        "id": PARENT_ROW,
        "x_studio_review_id": REVIEW,
        "x_studio_company": [COMPANY, "ICT"],
        "x_studio_ipp_req_ready": True,
        "x_studio_ipp_req_action": "product_line_mapping",
        "x_studio_ipp_req_version": 2,
        "x_studio_ipp_req_requested_by": [OPERATOR_UID, "Operator"],
        "x_studio_ipp_req_requested_at": SUBMITTED,
        "x_studio_ipp_req_line": "2",
        "x_studio_ipp_req_product": [TGDR_PRODUCT, "x"],
    }
    odoo = WorkbenchOdoo(children=[], parents=[row])

    (failure,) = OdooOperatorRequestReader(adapter=odoo, mapping=_parent_mapping()).list_pending(
        company_id=COMPANY, limit=10
    )

    assert isinstance(failure, OperatorRequestReadFailure) and "desteklenmiyor" in failure.message


# --------------------------------------------------------------------------- acknowledger


def _ack(odoo: WorkbenchOdoo, outcome: OperatorRequestOutcome, *, clear: bool, requested_at=SUBMITTED_AT) -> bool:
    return OdooProductLineRequestAcknowledger(adapter=odoo, mapping=_mapping()).acknowledge(
        odoo_record_id=LINE_ROW,
        requested_at=requested_at,
        outcome=outcome,
        message="Ürün eşleştirildi.",
        processed_at=datetime(2026, 10, 9, 10, 1, tzinfo=UTC),
        clear_request_inputs=clear,
    )


def test_completed_acknowledgement_writes_only_request_fields_and_clears_the_product() -> None:
    odoo = WorkbenchOdoo(children=[_child()], parents=[_parent()])

    assert _ack(odoo, OperatorRequestOutcome.COMPLETED, clear=True) is True

    assert odoo.writes == [
        (
            CHILD,
            LINE_ROW,
            {
                "x_studio_ipp_req_result": "Tamamlandı",
                "x_studio_ipp_req_message": "Ürün eşleştirildi.",
                "x_studio_ipp_req_processed_at": "2026-10-09 10:01:00",
                "x_studio_ipp_req_ready": False,
                "x_studio_ipp_req_product": False,
            },
        )
    ]
    assert set(odoo.writes[0][2]) <= set(_mapping().request_fields())


def test_rejected_acknowledgement_keeps_the_product_for_correction() -> None:
    odoo = WorkbenchOdoo(children=[_child()], parents=[_parent()])

    _ack(odoo, OperatorRequestOutcome.REJECTED, clear=False)

    assert odoo.child()["x_studio_ipp_req_product"] == [TGDR_PRODUCT, "SQL Server Standard 2 Core"]
    assert odoo.child()["x_studio_ipp_req_result"] == "Reddedildi"
    assert odoo.child()["x_studio_ipp_req_ready"] is False


def test_a_newer_submission_is_never_overwritten() -> None:
    odoo = WorkbenchOdoo(children=[_child(x_studio_ipp_req_requested_at="2026-10-09 10:05:00")], parents=[_parent()])

    assert _ack(odoo, OperatorRequestOutcome.COMPLETED, clear=True) is False
    assert odoo.writes == []


# --------------------------------------------------------------------------- end to end (real use case)


class ProjectionRefresh:
    """Stand-in for the canonical synchronizer: re-projects the child rows from Hub state as the Hub user."""

    def __init__(self, harness: Harness, odoo: WorkbenchOdoo) -> None:
        self.harness = harness
        self.odoo = odoo
        self.calls: list[tuple[str, int]] = []

    def sync(self, *, review_id: str, company_id: int) -> None:
        self.calls.append((review_id, company_id))
        unmatched = set(_lines_with_reason(self.harness.review))
        for row in self.odoo.rows[CHILD].values():
            if row["x_studio_ipp_review_id"] != review_id:
                continue
            row["x_studio_ipp_review_version"] = self.harness.review.version
            row["x_studio_ipp_match_state"] = (
                "product_not_found" if row["x_studio_ipp_line_number"] in unmatched else "matched"
            )


def _pipeline(
    harness: Harness,
    odoo: WorkbenchOdoo,
    *,
    ledger: FakeLedger | None = None,
    issuer: FakeIssuer | None = None,
    actors: dict[int, OperatorActor] | None = None,
) -> tuple[OperatorRequestIngestionWorkflow, ProjectionRefresh]:
    refresher = ProjectionRefresh(harness, odoo)
    workflow = OperatorRequestIngestionWorkflow(
        reader=OdooProductLineRequestReader(adapter=odoo, mapping=_mapping()),
        acknowledger=OdooProductLineRequestAcknowledger(adapter=odoo, mapping=_mapping()),
        ledger=ledger or FakeLedger(),
        actors=OperatorActorDirectory({OPERATOR_UID: OPERATOR} if actors is None else actors),
        handlers={OperatorRequestAction.PRODUCT_LINE_MAPPING: ProductMappingRequestHandler(use_case=harness.use_case)},
        authorization_issuer=issuer or FakeIssuer(),
        projection_refresher=refresher,
        clock=lambda: datetime(2026, 10, 9, 10, 1, tzinfo=UTC),
        transient_errors=(ErpRepositoryError,),
    )
    return workflow, refresher


def _world() -> tuple[Harness, WorkbenchOdoo]:
    harness = Harness(_ict_8699())
    assert _lines_with_reason(harness.review) == ["1", "2"]
    return harness, WorkbenchOdoo(children=[_child()], parents=[_parent()])


def test_child_line_request_maps_the_product_refreshes_the_line_to_matched_and_acknowledges() -> None:
    harness, odoo = _world()
    issuer = FakeIssuer()
    workflow, refresher = _pipeline(harness, odoo, issuer=issuer)

    (result,) = workflow.run(company_id=COMPANY).results

    assert result.outcome is OperatorRequestOutcome.COMPLETED and result.acknowledged
    assert result.message.startswith("Ürün eşleştirildi: Satır 2 (satıcı kodu TGDR)")
    # exactly one supplier-specific mapping, under the Hub-derived supplier and seller code
    (row,) = harness.new_rows()
    assert (row["partner_id"], row["product_code"], row["product_id"]) == (ICT_BULUT, "TGDR", TGDR_PRODUCT)
    # one narrow authorization for the version the operator saw, consumed by the use case
    assert [(call["operation_type"], call["target_version"]) for call in issuer.calls] == [("MAP_EXISTING_PRODUCT", 2)]
    assert len(harness.consumed) == 1
    # reclassified: line 2 resolved, line 1 still open; projection refreshed; request closed
    assert _lines_with_reason(harness.review) == ["1"] and harness.review.version == 3
    assert refresher.calls == [(REVIEW, COMPANY)]
    child = odoo.child()
    assert child["x_studio_ipp_match_state"] == "matched" and child["x_studio_ipp_review_version"] == 3
    assert child["x_studio_ipp_req_ready"] is False and child["x_studio_ipp_req_product"] is False
    assert child["x_studio_ipp_req_result"] == "Tamamlandı"


def test_crash_before_acknowledgement_only_reacknowledges_and_never_writes_a_second_mapping() -> None:
    harness, odoo = _world()
    ledger = FakeLedger()
    workflow, _ = _pipeline(harness, odoo, ledger=ledger)
    odoo.fail_writes = 1  # the Odoo acknowledgement fails after the Hub committed

    (first,) = workflow.run(company_id=COMPANY).results
    assert first.outcome is OperatorRequestOutcome.COMPLETED and not first.acknowledged
    assert odoo.child()["x_studio_ipp_req_ready"] is True

    (second,) = workflow.run(company_id=COMPANY).results

    assert second.outcome is OperatorRequestOutcome.COMPLETED and second.acknowledged
    assert len(harness.new_rows()) == 1 and len(harness.reclassify_calls) == 1
    assert odoo.child()["x_studio_ipp_req_ready"] is False and odoo.child()["x_studio_ipp_req_product"] is False


def test_crash_after_supplierinfo_write_resumes_by_reusing_the_identical_mapping() -> None:
    harness, odoo = _world()
    ledger = FakeLedger()
    issuer = FakeIssuer()
    workflow, _ = _pipeline(harness, odoo, ledger=ledger, issuer=issuer)
    harness.reclassify_error = ErpRepositoryError("Odoo request timed out.")  # transient after the write

    (first,) = workflow.run(company_id=COMPANY).results
    assert first.outcome is OperatorRequestOutcome.RETRY_LATER
    assert len(harness.new_rows()) == 1 and harness.review.version == 2

    (second,) = workflow.run(company_id=COMPANY).results

    assert second.outcome is OperatorRequestOutcome.COMPLETED
    assert len(harness.new_rows()) == 1  # reused, not duplicated
    assert len(issuer.calls) == 1  # the recorded authorization is reused on resume


def test_resubmitting_a_completed_line_never_writes_again() -> None:
    harness, odoo = _world()
    workflow, _ = _pipeline(harness, odoo)
    workflow.run(company_id=COMPANY)

    # Operator presses the button again on the now-matched row, once with the old version
    # snapshot and once with the refreshed one.
    odoo.child().update(
        x_studio_ipp_req_product=[TGDR_PRODUCT, "x"],
        x_studio_ipp_req_ready=True,
        x_studio_ipp_req_requested_at="2026-10-09 10:10:00",
    )
    (stale,) = workflow.run(company_id=COMPANY).results
    odoo.child().update(
        x_studio_ipp_req_product=[TGDR_PRODUCT, "x"],
        x_studio_ipp_req_ready=True,
        x_studio_ipp_req_version=3,
        x_studio_ipp_req_requested_at="2026-10-09 10:11:00",
    )
    (not_needed,) = workflow.run(company_id=COMPANY).results

    assert stale.outcome is OperatorRequestOutcome.STALE
    assert not_needed.outcome is OperatorRequestOutcome.REJECTED and "gerekmiyor" in not_needed.message
    assert len(harness.new_rows()) == 1 and len(harness.reclassify_calls) == 1


def test_stale_snapshot_version_is_never_reinterpreted() -> None:
    harness, odoo = _world()
    odoo.child()["x_studio_ipp_req_version"] = 1
    workflow, _ = _pipeline(harness, odoo)

    (result,) = workflow.run(company_id=COMPANY).results

    assert result.outcome is OperatorRequestOutcome.STALE
    assert harness.new_rows() == [] and harness.consumed == []
    assert odoo.child()["x_studio_ipp_req_result"] == "Güncel Değil"
    assert odoo.child()["x_studio_ipp_req_product"] == [TGDR_PRODUCT, "SQL Server Standard 2 Core"]


def test_unknown_odoo_user_is_unauthorized_and_nothing_runs() -> None:
    harness, odoo = _world()
    workflow, refresher = _pipeline(harness, odoo, actors={})

    (result,) = workflow.run(company_id=COMPANY).results

    assert result.outcome is OperatorRequestOutcome.UNAUTHORIZED
    assert harness.new_rows() == [] and refresher.calls == []
    assert odoo.child()["x_studio_ipp_req_result"] == "Yetkisiz"


def test_operator_without_execute_permission_cannot_write_a_mapping() -> None:
    harness, odoo = _world()
    issuer = FakeIssuer()
    workflow, _ = _pipeline(harness, odoo, issuer=issuer, actors={OPERATOR_UID: DECIDER})

    (result,) = workflow.run(company_id=COMPANY).results

    assert result.outcome is OperatorRequestOutcome.REJECTED and "workbench_execute" in result.message
    assert issuer.calls == [] and harness.new_rows() == []


def test_existing_mapping_of_the_code_to_another_product_fails_closed() -> None:
    harness, odoo = _world()
    harness.odoo.supplierinfo.append(
        {
            "id": 9,
            "partner_id": ICT_BULUT,
            "product_tmpl_id": 301,
            "product_id": 501,
            "product_code": "TGDR",
            "company_id": 1,
        }
    )
    workflow, _ = _pipeline(harness, odoo)

    (result,) = workflow.run(company_id=COMPANY).results

    assert result.outcome is OperatorRequestOutcome.REJECTED and "başka bir ürüne" in result.message
    assert harness.odoo.create_calls == [] and harness.reclassify_calls == []


def test_inconsistent_row_is_rejected_and_acknowledged_without_any_use_case() -> None:
    harness, odoo = _world()
    odoo.child()["x_studio_ipp_line_number"] = "1"  # tampered towards another line
    workflow, refresher = _pipeline(harness, odoo)

    (result,) = workflow.run(company_id=COMPANY).results

    assert result.outcome is OperatorRequestOutcome.REJECTED and IDENTITY_MISMATCH_MESSAGE in result.message
    assert harness.new_rows() == [] and harness.consumed == [] and refresher.calls == []
    assert odoo.child()["x_studio_ipp_req_ready"] is False


def test_parent_and_child_requests_for_the_same_line_never_both_apply() -> None:
    """Same line, same snapshotted version, one tick: the first wins; the second is stale."""

    from app.application.workbench.operator_request_handlers import ProductMappingRequestHandler as Handler
    from app.composition.operator_requests import SequentialOperatorRequestWorkflows
    from tests.unit.test_adr_0013_operator_request_ingestion import FakeAcknowledger, FakeReader

    harness, odoo = _world()
    ledger = FakeLedger()
    issuer = FakeIssuer()
    lines, _ = _pipeline(harness, odoo, ledger=ledger, issuer=issuer)
    parent_request = OperatorRequest(
        odoo_record_id=PARENT_ROW,
        review_id=REVIEW,
        company_id=COMPANY,
        action=OperatorRequestAction.PRODUCT_MAPPING,
        expected_version=2,
        requested_by_odoo_user_id=OPERATOR_UID,
        requested_at=SUBMITTED_AT,
        line_number="2",
        product_id=TGDR_PRODUCT,
    )
    parent = OperatorRequestIngestionWorkflow(
        reader=FakeReader([parent_request]),
        acknowledger=FakeAcknowledger(),
        ledger=ledger,
        actors=OperatorActorDirectory({OPERATOR_UID: OPERATOR}),
        handlers={OperatorRequestAction.PRODUCT_MAPPING: Handler(use_case=harness.use_case)},
        authorization_issuer=issuer,
        projection_refresher=ProjectionRefresh(harness, odoo),
        transient_errors=(ErpRepositoryError,),
    )

    child_result, parent_result = SequentialOperatorRequestWorkflows((lines, parent)).run(company_id=COMPANY).results

    assert child_result.outcome is OperatorRequestOutcome.COMPLETED
    assert parent_result.outcome is OperatorRequestOutcome.STALE
    assert len(harness.new_rows()) == 1 and len(harness.reclassify_calls) == 1
    assert operator_request_key(parent_request) != child_result.request_key


# --------------------------------------------------------------------------- request shape + configuration


def test_line_action_needs_line_and_product_and_the_same_permission_as_the_parent_action() -> None:
    with pytest.raises(WorkbenchContractError, match="fatura satırı"):
        OperatorRequest(
            odoo_record_id=LINE_ROW,
            review_id=REVIEW,
            company_id=COMPANY,
            action=OperatorRequestAction.PRODUCT_LINE_MAPPING,
            expected_version=2,
            requested_by_odoo_user_id=OPERATOR_UID,
            requested_at=SUBMITTED_AT,
            product_id=TGDR_PRODUCT,
        )
    assert (
        ACTION_PERMISSIONS[OperatorRequestAction.PRODUCT_LINE_MAPPING]
        == ACTION_PERMISSIONS[OperatorRequestAction.PRODUCT_MAPPING]
    )


def test_child_and_parent_requests_with_the_same_ids_have_different_keys() -> None:
    values = dict(
        odoo_record_id=24,
        review_id=REVIEW,
        company_id=COMPANY,
        expected_version=2,
        requested_by_odoo_user_id=OPERATOR_UID,
        requested_at=SUBMITTED_AT,
        line_number="2",
        product_id=TGDR_PRODUCT,
    )
    parent = OperatorRequest(action=OperatorRequestAction.PRODUCT_MAPPING, **values)
    child = OperatorRequest(action=OperatorRequestAction.PRODUCT_LINE_MAPPING, **values)

    assert operator_request_key(parent) != operator_request_key(child)


def test_line_requests_need_the_request_tick_and_the_live_child_projection() -> None:
    errors = runtime_configuration_errors(Settings(odoo_workbench_product_line_requests_enabled=True))

    assert any("ODOO_WORKBENCH_OPERATOR_REQUESTS_ENABLED=true" in error for error in errors)
    assert any("ODOO_WORKBENCH_PRODUCT_LINE_PROJECTION_ENABLED=true" in error for error in errors)
    assert not any(
        "PRODUCT_LINE_REQUESTS" in error
        for error in runtime_configuration_errors(
            Settings(
                odoo_workbench_product_line_requests_enabled=True,
                odoo_workbench_operator_requests_enabled=True,
                odoo_workbench_operator_requests_company_id=1,
                odoo_workbench_projection_publish_enabled=True,
                odoo_workbench_product_line_projection_enabled=True,
            )
        )
    )


def test_tick_runs_child_lines_before_parent_rows_only_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import MagicMock

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.composition import operator_requests
    from app.composition.operator_requests import SequentialOperatorRequestWorkflows, _tick_workflow
    from tests.unit.test_odoo_supplier_partner_writer import FakeJson2Client

    # The parent decision reader mapping comes from env in production; irrelevant here.
    monkeypatch.setattr(operator_requests, "OdooWorkbenchFieldMapping", MagicMock())
    monkeypatch.setattr(operator_requests, "decision_mapping_for_requests", lambda base, request: MagicMock())
    session = Session(bind=create_engine("sqlite://"))
    common = dict(
        business_session=session,
        ledger_session=session,
        settings=Settings(),
        odoo_client=FakeJson2Client(),
        request_mapping=_parent_mapping(),
    )

    disabled = _tick_workflow(line_request_mapping=None, **common)
    enabled = _tick_workflow(line_request_mapping=_mapping(), **common)

    assert isinstance(disabled, OperatorRequestIngestionWorkflow)
    assert OperatorRequestAction.PRODUCT_LINE_MAPPING not in disabled._handlers
    assert isinstance(enabled, SequentialOperatorRequestWorkflows)
    lines, parent = enabled._workflows
    assert set(lines._handlers) == {OperatorRequestAction.PRODUCT_LINE_MAPPING}
    assert type(lines._handlers[OperatorRequestAction.PRODUCT_LINE_MAPPING]).__name__ == "ProductMappingRequestHandler"
    assert OperatorRequestAction.PRODUCT_MAPPING in parent._handlers  # the parent path is unchanged


def test_projection_publisher_never_writes_request_fields() -> None:
    from app.erp.odoo.workbench_product_line_publisher import OdooWorkbenchProductLinePublisher

    publisher_fields = set(OdooWorkbenchProductLineFieldMapping().read_fields())
    assert not publisher_fields & set(_mapping().request_fields())
    assert OdooWorkbenchProductLinePublisher  # imported: the publisher's own mapping is the only field source
