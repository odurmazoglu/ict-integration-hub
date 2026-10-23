"""V1 "Muhasebeleştirme Dışı Bırak": dismissal at any open step, without supplier/product matching.

Covers the Hub side (candidate reader, decision use case on the real SQLite repository,
V1 operator "Karar" handler, operator-request permissions) and the prepared Studio
artifacts in docs/odoo_studio/v1_dismiss (server action through a fake Odoo record, view
applied on top of the production form + the V1 buttons view 4221).
"""

from __future__ import annotations

import ast
import datetime as dt
import importlib.util
import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.application.workbench import ReviewDecisionCommand
from app.application.workbench.decision_ingestion import decision_idempotency_key
from app.application.workbench.decision_use_cases import SubmitReviewDecisionUseCase
from app.application.workbench.dto import ReviewDecisionType, ReviewStatus
from app.application.workbench.exceptions import (
    DISMISS_COMMENT_MIN_LENGTH,
    DismissCommentRequiredError,
    ReviewDecisionIdempotencyConflictError,
    ReviewStateConflictError,
    ReviewVersionConflictError,
)
from app.application.workbench.operator_guidance import NOT_STATUTORY_REJECTION, OperatorNextAction
from app.application.workbench.operator_request_handlers import DISMISSED_REQUEST_MESSAGE, DecisionRequestHandler
from app.application.workbench.operator_request_ingestion import (
    ACTION_PERMISSIONS,
    OperatorActor,
    OperatorRequestAction,
    OperatorRequestOutcome,
)
from app.application.workflow import ManualReviewReason, ManualReviewReasonCode
from app.db.base import Base
from app.erp.odoo.workbench_candidate_reader import (
    ODOO_DECISION_TO_CANONICAL,
    OdooWorkbenchDecisionCandidateReader,
)
from app.erp.odoo.workbench_operator_request_reader import ACTION_BY_ODOO_VALUE
from app.erp.odoo.workbench_projection_publisher import ODOO_REVIEW_STATUS_BY_CANONICAL
from app.models.operating_expense_mapping import OperatingExpenseMappingRecord
from app.models.workbench_review_accounting_resolution import WorkbenchReviewAccountingResolution
from app.models.workbench_review_decision import WorkbenchReviewDecision
from app.models.workbench_review_item import WorkbenchReviewItem
from app.models.workbench_review_purchase_purpose_resolution import WorkbenchReviewPurchasePurposeResolution
from app.models.workflow_execution import WorkflowExecution
from app.persistence import SqlAlchemyReviewRepository
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork
from tests.unit.test_adr_0013_operator_request_ingestion import (
    FakeAcknowledger,
    FakeReader,
    ScriptedHandler,
    _decision_request,
    _workflow,
)
from tests.unit.test_odoo_workbench_candidate_reader import (
    RecordingAdapter,
    _allocation_records,
    _mapping,
    _parent_record,
)
from tests.unit.test_workbench_review_persistence import _review_item

COMPANY_ID = 1
REASON = "Şirkete kesilmiş kişisel alım; şahsi kartla ödendi"
STUDIO = Path(__file__).resolve().parents[2] / "docs" / "odoo_studio"


def _reason(code: ManualReviewReasonCode) -> ManualReviewReason:
    return ManualReviewReason(code=code, message="blocked", line_number="1", source="test")


SUPPLIER_NOT_FOUND = (
    _reason(ManualReviewReasonCode.SUPPLIER_NOT_FOUND),
    _reason(ManualReviewReasonCode.PRODUCT_NOT_FOUND),
)
SUPPLIER_AMBIGUOUS = (
    _reason(ManualReviewReasonCode.SUPPLIER_AMBIGUOUS),
    _reason(ManualReviewReasonCode.PRODUCT_NOT_FOUND),
)
PRODUCT_NOT_FOUND = (_reason(ManualReviewReasonCode.PRODUCT_NOT_FOUND),)
PURPOSE_STEP = (_reason(ManualReviewReasonCode.OPERATING_EXPENSE_MAPPING_REQUIRED),)


# =========================================================================== Hub: real repository


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def _seed(db: Session, review_id: str, reasons: tuple[ManualReviewReason, ...]) -> None:
    SqlAlchemyReviewRepository(db).create_review_item(
        _review_item(review_id, review_reasons=reasons), company_id=COMPANY_ID, idempotency_key=f"import:{review_id}"
    )
    db.commit()


def _use_case(db: Session) -> SubmitReviewDecisionUseCase:
    return SubmitReviewDecisionUseCase(
        review_decision_writer=SqlAlchemyReviewRepository(db), unit_of_work=SqlAlchemyUnitOfWork(db)
    )


def _dismiss(review_id: str, comment: str | None = REASON, *, version: int = 1, key: str = "k-1"):
    return ReviewDecisionCommand(
        review_id=review_id,
        company_id=COMPANY_ID,
        expected_version=version,
        decision=ReviewDecisionType.DISMISS,
        decided_by="odoo:2",
        idempotency_key=key,
        comment=comment,
    )


def _row(db: Session, review_id: str) -> WorkbenchReviewItem:
    return db.scalar(select(WorkbenchReviewItem).where(WorkbenchReviewItem.review_id == review_id))


def _decisions(db: Session) -> list[WorkbenchReviewDecision]:
    return list(db.scalars(select(WorkbenchReviewDecision)))


@pytest.mark.parametrize(
    "reasons",
    [SUPPLIER_NOT_FOUND, SUPPLIER_AMBIGUOUS, PRODUCT_NOT_FOUND],
    ids=["supplier-not-found", "supplier-ambiguous", "product-not-found"],
)
def test_blocked_review_is_dismissed_without_supplier_or_product_matching(session: Session, reasons) -> None:
    _seed(session, "review:p", reasons)

    ack = _use_case(session).execute(_dismiss("review:p", f"  {REASON}  "))

    assert ack.status is ReviewStatus.DISMISSED
    row = _row(session, "review:p")
    assert (row.status, row.version) == (ReviewStatus.DISMISSED.value, 2)
    (decision,) = _decisions(session)
    # Complete audit evidence: who, when, from which version, why (trimmed).
    assert decision.decision_type == ReviewDecisionType.DISMISS.value
    assert decision.decided_by == "odoo:2" and decision.submitted_at is not None
    assert (decision.review_version_before, decision.review_version_after) == (1, 2)
    assert decision.comment == REASON
    assert decision.selected_workflow is None
    # The review's own blockers stay recorded; nothing is resolved on its behalf.
    assert {reason["code"] for reason in row.review_reasons} == {reason.code.value for reason in reasons}
    # No Vendor Bill / execution and no supplier-wide mapping is created.
    assert session.query(WorkflowExecution).count() == 0
    assert session.query(OperatingExpenseMappingRecord).count() == 0


def test_purpose_and_accounting_stage_dismissal_keeps_recorded_resolutions(session: Session) -> None:
    _seed(session, "review:a", PURPOSE_STEP)
    session.add(
        WorkbenchReviewPurchasePurposeResolution(
            review_id="review:a",
            company_id=COMPANY_ID,
            review_version=1,
            source_invoice_id="src-a",
            purchase_purpose="internal_use",
        )
    )
    session.add(
        WorkbenchReviewAccountingResolution(
            review_id="review:a",
            company_id=COMPANY_ID,
            review_version=1,
            treatment_type="expense_account",
            expense_account_id=247,
            expense_category="CLOUD_SERVICES_INTERNAL",
        )
    )
    session.commit()

    _use_case(session).execute(_dismiss("review:a"))

    assert _row(session, "review:a").status == ReviewStatus.DISMISSED.value
    purpose = session.scalar(select(WorkbenchReviewPurchasePurposeResolution))
    accounting = session.scalar(select(WorkbenchReviewAccountingResolution))
    assert (purpose.purchase_purpose, purpose.review_version) == ("internal_use", 1)
    assert (accounting.expense_account_id, accounting.expense_category, accounting.review_version) == (
        247,
        "CLOUD_SERVICES_INTERNAL",
        1,
    )
    assert session.query(WorkbenchReviewPurchasePurposeResolution).count() == 1
    assert session.query(WorkbenchReviewAccountingResolution).count() == 1


@pytest.mark.parametrize("comment", [None, "", "   ", "kişisel", "  kısa not "])
def test_missing_or_too_short_reason_is_refused_without_mutation(session: Session, comment) -> None:
    _seed(session, "review:p", SUPPLIER_NOT_FOUND)

    with pytest.raises(DismissCommentRequiredError) as error:
        _use_case(session).execute(_dismiss("review:p", comment))

    assert "gerekçe" in error.value.safe_message
    assert (_row(session, "review:p").status, _row(session, "review:p").version) == ("pending_review", 1)
    assert _decisions(session) == []


def test_reason_of_exactly_the_minimum_length_is_accepted(session: Session) -> None:
    _seed(session, "review:p", SUPPLIER_NOT_FOUND)
    reason = "x" * DISMISS_COMMENT_MIN_LENGTH

    _use_case(session).execute(_dismiss("review:p", f" {reason} "))

    assert _decisions(session)[0].comment == reason


def test_stale_version_is_rejected(session: Session) -> None:
    _seed(session, "review:p", SUPPLIER_NOT_FOUND)

    with pytest.raises(ReviewVersionConflictError):
        _use_case(session).execute(_dismiss("review:p", version=2))

    assert _row(session, "review:p").status == "pending_review" and _decisions(session) == []


def test_repeated_submission_is_one_decision(session: Session) -> None:
    _seed(session, "review:p", SUPPLIER_NOT_FOUND)
    first = _use_case(session).execute(_dismiss("review:p"))

    replay = _use_case(session).execute(_dismiss("review:p"))  # same request -> same acknowledgement
    assert replay == first
    with pytest.raises(ReviewDecisionIdempotencyConflictError):  # same key, different content
        _use_case(session).execute(_dismiss("review:p", "Başka bir gerekçe metni"))
    with pytest.raises(ReviewStateConflictError):  # a second press after the dismissal (new key)
        _use_case(session).execute(_dismiss("review:p", version=2, key="k-2"))

    assert len(_decisions(session)) == 1


@pytest.mark.parametrize(
    "status",
    [ReviewStatus.DECISION_SUBMITTED, ReviewStatus.RESOLVED, ReviewStatus.DISMISSED],
)
def test_accepted_executed_or_closed_review_cannot_be_dismissed(session: Session, status: ReviewStatus) -> None:
    _seed(session, "review:p", PRODUCT_NOT_FOUND)
    session.execute(
        update(WorkbenchReviewItem)
        .where(WorkbenchReviewItem.review_id == "review:p")
        .values(status=status.value, version=2)
    )
    session.commit()

    with pytest.raises(ReviewStateConflictError):
        _use_case(session).execute(_dismiss("review:p", version=2))

    assert _row(session, "review:p").status == status.value
    assert _decisions(session) == []


def test_review_with_an_executed_vendor_bill_cannot_be_dismissed(session: Session) -> None:
    _seed(session, "review:p", PRODUCT_NOT_FOUND)
    session.execute(
        update(WorkbenchReviewItem)
        .where(WorkbenchReviewItem.review_id == "review:p")
        .values(status=ReviewStatus.DECISION_SUBMITTED.value, version=2)
    )
    session.add(
        WorkflowExecution(
            execution_id="e-1",
            review_id="review:p",
            decision_version=2,
            company_id=COMPANY_ID,
            state="completed",
            mode="execute",
            idempotency_key="ek-1",
            plan_signature="sig",
            plan={},
            checkpoint={},
            retry_policy={},
        )
    )
    session.commit()

    for version in (1, 2):
        with pytest.raises((ReviewStateConflictError, ReviewVersionConflictError)):
            _use_case(session).execute(_dismiss("review:p", version=version, key=f"k-{version}"))

    assert _row(session, "review:p").status == ReviewStatus.DECISION_SUBMITTED.value
    assert _decisions(session) == []
    assert session.query(WorkflowExecution).count() == 1


def test_dismissal_path_never_reaches_a_provider_or_vendor_bill_writer() -> None:
    """No legal e-Fatura response: the decision path has no Uyumsoft, Vendor Bill or posting code."""

    root = Path(__file__).resolve().parents[2] / "app"
    for module in (
        "application/workbench/decision_use_cases.py",
        "application/workbench/operator_request_handlers.py",
        "erp/odoo/workbench_candidate_reader.py",
    ):
        tree = ast.parse((root / module).read_text(encoding="utf-8"))
        imported = {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)} | {
            alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
        }
        assert not any("uyumsoft" in name for name in imported), module
        source = (root / module).read_text(encoding="utf-8")
        assert "action_post" not in source and "account.move" not in source, module
    assert "yasal e-Fatura reddi veya iptali değildir" in NOT_STATUTORY_REJECTION


# =========================================================================== Hub: candidate reader


@pytest.mark.parametrize(
    "leftovers",
    [
        {"x_selected_workflow": "Operating Expense"},  # would be a workflow error for a decision
        {"x_selected_workflow": "Bogus", "x_line_resolutions": "not json", "x_tax_resolutions": "broken"},
        {"x_selected_partner": [700, "Half-chosen supplier"]},
        {"x_allocation_completeness": False},
    ],
)
def test_dismiss_candidate_ignores_and_never_reads_workflow_inputs(leftovers) -> None:
    adapter = RecordingAdapter(
        parent_records=[_parent_record(x_decision="Dismiss", x_comment=REASON, **leftovers)],
        allocation_records=_allocation_records(),
    )
    candidate = OdooWorkbenchDecisionCandidateReader(adapter=adapter, mapping=_mapping()).get_ready_decision(
        review_id="review-1", company_id=7
    )

    assert candidate.decision is ReviewDecisionType.DISMISS
    assert candidate.comment == REASON
    assert (candidate.expected_version, candidate.decided_by_odoo_user_id) == (4, 11)
    assert candidate.selected_workflow is None and candidate.selected_partner_id is None
    assert candidate.line_resolutions == () and candidate.tax_resolutions == ()
    assert candidate.business_context_allocations is None
    assert len(adapter.calls) == 1  # allocation child rows are not read


def test_submit_decision_candidate_still_parses_and_validates_every_input() -> None:
    adapter = RecordingAdapter(
        parent_records=[_parent_record(x_selected_workflow="Bogus")], allocation_records=_allocation_records()
    )
    from app.application.workbench.exceptions import WorkbenchCandidateDataError

    with pytest.raises(WorkbenchCandidateDataError):
        OdooWorkbenchDecisionCandidateReader(adapter=adapter, mapping=_mapping()).get_ready_decision(
            review_id="review-1", company_id=7
        )


# =========================================================================== Hub: V1 "Karar" request


class _Reader:
    def __init__(self, candidate) -> None:
        self.candidate = candidate

    def get_ready_decision(self, *, review_id: str, company_id: int):
        return self.candidate


class _Validator:
    def validate(self, candidate, *, requested_company_id: int):
        return candidate


def _candidate_for(review_id: str, *, comment: str | None = REASON, version: int = 1, odoo_id: int = 42):
    adapter = RecordingAdapter(
        parent_records=[
            _parent_record(
                id=odoo_id,
                x_review_id=review_id,
                x_company_id=[COMPANY_ID, "ICT"],
                x_version=version,
                x_decision="Dismiss",
                x_comment=comment,
                x_selected_workflow="Operating Expense",  # stale input from an unfinished step
                x_decided_by=[2, "Operator"],
            )
        ]
    )
    return OdooWorkbenchDecisionCandidateReader(adapter=adapter, mapping=_mapping()).get_ready_decision(
        review_id=review_id, company_id=COMPANY_ID
    )


def _request(review_id: str, *, version: int = 1, odoo_id: int = 42):
    return _decision_request().__class__(
        odoo_record_id=odoo_id,
        review_id=review_id,
        company_id=COMPANY_ID,
        action=OperatorRequestAction.DECISION,
        expected_version=version,
        requested_by_odoo_user_id=2,
        requested_at=datetime(2026, 10, 23, 12, 0, tzinfo=UTC),
    )


def _handler(db: Session, candidate) -> DecisionRequestHandler:
    return DecisionRequestHandler(
        candidate_reader=_Reader(candidate),
        erp_reference_validator=_Validator(),
        decision_submitter=_use_case(db),
        unit_of_work=SqlAlchemyUnitOfWork(db),
    )


def test_operator_dismiss_request_at_the_supplier_step_completes_with_the_turkish_message(session: Session) -> None:
    _seed(session, "review:p", SUPPLIER_NOT_FOUND)
    candidate = _candidate_for("review:p")

    outcome = _handler(session, candidate).handle(_request("review:p"), None)  # type: ignore[arg-type]

    assert outcome.outcome is OperatorRequestOutcome.COMPLETED
    assert outcome.message == DISMISSED_REQUEST_MESSAGE
    assert "yasal e-Fatura reddi veya iptali değildir" in outcome.message
    assert "reddedildi" not in outcome.message.lower()
    assert _row(session, "review:p").status == ReviewStatus.DISMISSED.value
    (decision,) = _decisions(session)
    assert decision.decided_by == "odoo:2" and decision.selected_workflow is None
    assert decision.idempotency_key == decision_idempotency_key(candidate)

    replay = _handler(session, candidate).handle(_request("review:p"), None)  # type: ignore[arg-type]
    assert replay.outcome is OperatorRequestOutcome.ALREADY_COMPLETED
    assert len(_decisions(session)) == 1


def test_operator_dismiss_request_for_another_version_is_stale(session: Session) -> None:
    _seed(session, "review:p", SUPPLIER_NOT_FOUND)

    with pytest.raises(ReviewVersionConflictError):  # request snapshot != row
        _handler(session, _candidate_for("review:p", version=2)).handle(_request("review:p"), None)  # type: ignore[arg-type]
    with pytest.raises(ReviewVersionConflictError):  # both say v2, the Hub is at v1
        _handler(session, _candidate_for("review:p", version=2)).handle(_request("review:p", version=2), None)  # type: ignore[arg-type]

    assert _decisions(session) == []


@pytest.mark.parametrize(
    "actor",
    [
        None,
        OperatorActor(actor="viewer", permissions=frozenset()),
        OperatorActor(actor="exec", permissions=frozenset({"workbench_execute"})),
    ],
    ids=["unknown-user", "no-permission", "execute-only"],
)
def test_dismiss_request_without_the_decide_permission_is_unauthorized(actor) -> None:
    assert ACTION_PERMISSIONS[OperatorRequestAction.DECISION] == frozenset({"workbench_review_decide"})
    handler = ScriptedHandler([])
    ack = FakeAcknowledger()
    result = _workflow(
        FakeReader([_decision_request()]),
        handler,
        acknowledger=ack,
        actors={} if actor is None else {2: actor},
        action=OperatorRequestAction.DECISION,
    ).run(company_id=COMPANY_ID)

    assert result.results[0].outcome is OperatorRequestOutcome.UNAUTHORIZED
    assert handler.calls == []


# =========================================================================== Studio artifacts


def _module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


BUTTONS = _module(STUDIO / "v1_operator_buttons" / "provision.py", "v1_buttons_provision")
SNAPSHOT = STUDIO / "v1_operator_buttons" / "production_form_combined_20261021.xml"
DISMISS_VIEW = (STUDIO / "v1_dismiss" / "view.xml").read_text(encoding="utf-8")
DISMISS_CODE = (STUDIO / "v1_dismiss" / "actions" / "dismiss.py").read_text(encoding="utf-8")
DISMISS_ID = 990001
OPEN_STEPS = (
    "Tedarikçi Doğrulanmalı",
    "Ürün Eşleştirmesi Yapılmalı",
    "Satın Alma Amacı Seçilmeli",
    "Muhasebe İşlemi Seçilmeli",
    "Teknik Destek Gerekli",
    "Karar Verilmeli",
)


def _form():
    """Production form + view 4221 (priority 260) + this view (priority 270)."""

    from lxml import etree

    artifacts = BUTTONS.load_artifacts()
    ids = {placeholder: 900000 + index for index, placeholder in enumerate(BUTTONS.ACTIONS)}
    with_buttons = BUTTONS.apply_extension(SNAPSHOT.read_text(encoding="utf-8"), BUTTONS.view_arch(artifacts, ids))
    arch = etree.tostring(with_buttons, encoding="unicode")
    return BUTTONS.apply_extension(arch, DISMISS_VIEW.replace("__SA_DISMISS__", str(DISMISS_ID)))


OPEN_ROW_CONDITION = (
    "x_studio_review_status != 'Pending Review' or x_studio_vendor_bill or "
    "x_studio_ipp_next_action in (False, 'Tamamlandı', 'Fatura Oluşturulmalı')"
)


def test_view_adds_one_confirmed_operator_button_for_every_open_pending_row() -> None:
    form = _form()
    (button,) = form.xpath(f"//header/button[@name='{DISMISS_ID}']")
    assert button.get("string") == "Muhasebeleştirme Dışı Bırak"
    assert button.get("type") == "action"
    assert button.get("groups") == "studio_customization.ipp_workbench_operator_group"
    assert button.get("invisible") == f"x_studio_ipp_req_ready or {OPEN_ROW_CONDITION}"
    confirm = button.get("confirm")
    for fragment in (
        "GERİ ALINAMAZ",
        "yasal e-Fatura reddi veya iptali DEĞİLDİR",
        "alış faturası oluşturmaz",
        "Karar Notu",
    ):
        assert fragment in confirm
    # Every V1 step button of view 4221 is still there, unchanged.
    assert len(form.xpath("//header/button")) == 1 + 7 + 1  # 1026 (hidden) + 7 V1 buttons + dismiss


def test_reason_field_is_the_existing_comment_shown_at_every_open_step() -> None:
    form = _form()
    (field,) = form.xpath("//field[@name='x_studio_decision_comment']")
    assert field.get("string") == "Karar Notu / Dışı Bırakma Gerekçesi"
    assert field.get("invisible") == OPEN_ROW_CONDITION
    assert field.get("readonly") == "x_studio_ipp_req_ready"  # unchanged
    assert field.get("required") == "x_studio_ipp_next_action == 'Karar Verilmeli' and x_studio_decision == 'Dismiss'"
    (hint,) = form.xpath("//div[@name='ipp_v1_dismiss_hint']")
    assert hint.get("invisible") == OPEN_ROW_CONDITION
    assert "yasal e-Fatura reddi veya iptali değildir" in hint.text
    assert "muhasebe ile ayrıca belirlenir" in hint.text


def test_view_only_adds_and_never_anchors_on_string() -> None:
    assert "@string" not in DISMISS_VIEW
    positions = re.findall(r'position="([a-z]+)"', DISMISS_VIEW)
    assert positions == ["inside", "attributes", "after"]
    assert "replace" not in positions


def test_compared_values_exist_in_hub_mappings() -> None:
    stages = set(re.findall(r"'([^']+)'", OPEN_ROW_CONDITION)) - {"Pending Review"}
    assert stages <= {action.value for action in OperatorNextAction}
    assert set(OPEN_STEPS) <= {action.value for action in OperatorNextAction}
    assert "Pending Review" in ODOO_REVIEW_STATUS_BY_CANONICAL.values()
    assert ODOO_DECISION_TO_CANONICAL["Dismiss"] is ReviewDecisionType.DISMISS
    assert ACTION_BY_ODOO_VALUE["Karar"] is OperatorRequestAction.DECISION


class UserError(Exception):
    pass


class _Record(SimpleNamespace):
    def __init__(self, **values: Any) -> None:
        defaults: dict[str, Any] = {
            "x_studio_ipp_req_ready": False,
            "x_studio_ipp_next_action": "Tedarikçi Doğrulanmalı",
            "x_studio_review_version": 1,
            "x_studio_review_status": "Pending Review",
            "x_studio_vendor_bill": False,
            "x_studio_decision": False,
            "x_studio_selected_workflow": "Operating Expense",
            "x_studio_decision_comment": f"  {REASON} ",
        }
        defaults.update(values)
        super().__init__(**defaults)
        self.writes: list[dict[str, Any]] = []

    def __len__(self) -> int:
        return 1

    def write(self, values: dict[str, Any]) -> bool:
        self.writes.append(values)
        return True


def _run(record: Any) -> None:
    scope = {
        "records": record,
        "env": SimpleNamespace(user=SimpleNamespace(id=2)),
        "datetime": dt,
        "UserError": UserError,
    }
    exec(compile(DISMISS_CODE, "dismiss.py", "exec"), scope)  # noqa: S102 - test harness for Odoo code


@pytest.mark.parametrize("stage", OPEN_STEPS)
def test_server_action_queues_one_dismiss_decision_at_any_open_step(stage: str) -> None:
    record = _Record(x_studio_ipp_next_action=stage)
    _run(record)

    (values,) = record.writes
    assert values == {
        "x_studio_decision": "Dismiss",
        "x_studio_decision_comment": REASON,
        "x_studio_ipp_req_action": "Karar",
        "x_studio_ipp_req_version": 1,
        "x_studio_ipp_req_requested_by": 2,
        "x_studio_ipp_req_requested_at": values["x_studio_ipp_req_requested_at"],
        "x_studio_ipp_req_result": False,
        "x_studio_ipp_req_message": False,
        "x_studio_ipp_req_ready": True,
    }
    assert isinstance(values["x_studio_ipp_req_requested_at"], dt.datetime)
    # The operator's other inputs (workflow, supplier, purpose, accounting) are never touched.
    assert "x_studio_selected_workflow" not in values


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"x_studio_ipp_req_ready": True}, "gönderilmiş bir istek"),
        ({"x_studio_review_status": "Decision Submitted"}, "dışı bırakılamaz"),
        ({"x_studio_review_status": "Dismissed"}, "dışı bırakılamaz"),
        ({"x_studio_review_status": "Resolved"}, "dışı bırakılamaz"),
        ({"x_studio_vendor_bill": SimpleNamespace(id=78)}, "dışı bırakılamaz"),
        ({"x_studio_ipp_next_action": "Tamamlandı"}, "dışı bırakılamaz"),
        ({"x_studio_ipp_next_action": "Fatura Oluşturulmalı"}, "dışı bırakılamaz"),
        ({"x_studio_ipp_next_action": False}, "dışı bırakılamaz"),
        ({"x_studio_review_version": False}, "eşitlenmedi"),
        ({"x_studio_decision_comment": False}, "Gerekçe zorunludur"),
        ({"x_studio_decision_comment": "   "}, "Gerekçe zorunludur"),
        ({"x_studio_decision_comment": " kişisel "}, "Gerekçe zorunludur"),
    ],
)
def test_server_action_refuses_without_writing(overrides: dict[str, Any], fragment: str) -> None:
    record = _Record(**overrides)
    with pytest.raises(UserError, match=fragment):
        _run(record)
    assert record.writes == []


def test_server_action_minimum_matches_the_hub_rule() -> None:
    assert "len(reason) < 10" in DISMISS_CODE and DISMISS_COMMENT_MIN_LENGTH == 10
    record = _Record(x_studio_decision_comment="x" * DISMISS_COMMENT_MIN_LENGTH)
    _run(record)
    assert len(record.writes) == 1


def test_server_action_is_safe_eval_code_without_imports_posting_or_bill_writes() -> None:
    tree = ast.parse(DISMISS_CODE)
    assert not [node for node in ast.walk(tree) if isinstance(node, ast.Import | ast.ImportFrom)]
    for forbidden in ("account.move", "action_post", 'x_studio_vendor_bill":', "unlink", "env["):
        assert forbidden not in DISMISS_CODE
    written = set(re.findall(r'"(x_studio_[a-z0-9_]+)":', DISMISS_CODE))
    assert written == {
        "x_studio_decision",
        "x_studio_decision_comment",
        "x_studio_ipp_req_action",
        "x_studio_ipp_req_version",
        "x_studio_ipp_req_requested_by",
        "x_studio_ipp_req_requested_at",
        "x_studio_ipp_req_result",
        "x_studio_ipp_req_message",
        "x_studio_ipp_req_ready",
    }


def test_existing_v1_decision_action_1032_is_unchanged() -> None:
    import hashlib

    code = (STUDIO / "v1_operator_buttons" / "actions" / "decision.py").read_bytes()
    assert hashlib.sha256(code).hexdigest() == "ca4b360d3f976b8df1cdb2baf204cef4d8262aa703390f23848a3d614cb377e9"
