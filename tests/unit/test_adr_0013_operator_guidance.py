"""ADR-0013: operator guidance ("Yapılması Gerekenler") and its Workbench projection.

Acceptance scenarios use read-only, hand-written snapshots shaped like the production
CloudSpark and Apple reviews; no production record is read or written.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from app.application.workbench.accounting_resolution import AccountingTreatmentType, ReviewAccountingResolution
from app.application.workbench.dto import ReviewDecisionType, ReviewItem, ReviewStatus
from app.application.workbench.exceptions import ReviewNotFoundError
from app.application.workbench.operator_guidance import (
    GuidanceInput,
    OperatorGuidanceFacts,
    OperatorNextAction,
    build_operator_guidance,
)
from app.application.workbench.projection import WorkbenchProjection
from app.application.workbench.projection_sync import WorkbenchProjectionSources, WorkbenchProjectionSynchronizer
from app.application.workbench.purchase_purpose import PurchasePurpose
from app.application.workflow import ManualReviewReason, ManualReviewReasonCode, WorkflowType
from app.erp.odoo.workbench_projection_publisher import (
    OdooWorkbenchProjectionFieldMapping,
    OdooWorkbenchProjectionPublisher,
)

R = ManualReviewReasonCode


def _input(
    *,
    status: ReviewStatus = ReviewStatus.PENDING_REVIEW,
    codes: tuple[ManualReviewReasonCode, ...] = (),
    supplier: str | None = "CLOUDSPARK BILISIM",
    decision_type: ReviewDecisionType | None = None,
    workflow: WorkflowType | None = None,
    decision_version: int | None = None,
    execution_state: str | None = None,
    vendor_bill_id: int | None = None,
) -> GuidanceInput:
    return GuidanceInput(
        status=status,
        reason_codes=codes,
        supplier_name=supplier,
        decision_type=decision_type,
        decision_workflow=workflow,
        decision_version=decision_version,
        execution_state=execution_state,
        vendor_bill_id=vendor_bill_id,
    )


def _fixed_asset_resolution() -> ReviewAccountingResolution:
    return ReviewAccountingResolution(
        review_id="review:apple",
        company_id=1,
        review_version=4,
        treatment_type=AccountingTreatmentType.CAPITALIZE_FIXED_ASSET,
        asset_account_id=91,
        depreciation_model_id=12,
        approved_by="operator",
    )


# ---------------------------------------------------------------------- acceptance scenarios


def test_cloudspark_supplier_ambiguity_comes_first_with_human_readable_findings() -> None:
    guidance = build_operator_guidance(
        _input(codes=(R.SUPPLIER_AMBIGUOUS, R.OPERATING_EXPENSE_MAPPING_REQUIRED)), OperatorGuidanceFacts()
    )
    assert guidance.next_action is OperatorNextAction.SUPPLIER
    assert "Tedarikçi eşleşmesi doğrulanmalı" in guidance.todo_html
    assert "Muhasebe işlemi seçilmeli" in guidance.todo_html
    assert "SUPPLIER_AMBIGUOUS" not in guidance.todo_html  # codes stay in the technical view
    assert "Tedarikçi:" not in guidance.completed_html  # supplier is not done yet


def test_cloudspark_after_supplier_resolution_asks_purpose_then_accounting() -> None:
    pending = _input(codes=(R.OPERATING_EXPENSE_MAPPING_REQUIRED,))
    purpose = build_operator_guidance(pending, OperatorGuidanceFacts())
    accounting = build_operator_guidance(
        pending,
        OperatorGuidanceFacts(
            current_purchase_purpose=PurchasePurpose.OTHER_OPERATING_EXPENSE,
            latest_purchase_purpose=PurchasePurpose.OTHER_OPERATING_EXPENSE,
        ),
    )
    assert purpose.next_action is OperatorNextAction.PURPOSE
    assert accounting.next_action is OperatorNextAction.ACCOUNTING
    assert "✓ Tedarikçi: CLOUDSPARK BILISIM" in accounting.completed_html
    assert "Diğer İşletme Gideri" in accounting.completed_html


def test_purpose_recorded_for_an_older_version_does_not_skip_the_purpose_step() -> None:
    guidance = build_operator_guidance(
        _input(codes=(R.OPERATING_EXPENSE_MAPPING_REQUIRED,)),
        OperatorGuidanceFacts(current_purchase_purpose=None, latest_purchase_purpose=PurchasePurpose.INTERNAL_USE),
    )
    assert guidance.next_action is OperatorNextAction.PURPOSE


def test_ordinary_expense_invoice_without_blockers_asks_for_the_decision() -> None:
    guidance = build_operator_guidance(_input(codes=()), OperatorGuidanceFacts())
    assert guidance.next_action is OperatorNextAction.DECISION


def test_apple_completed_fixed_asset_history_is_done_and_summarised() -> None:
    guidance = build_operator_guidance(
        _input(
            status=ReviewStatus.DECISION_SUBMITTED,
            codes=(R.OPERATING_EXPENSE_MAPPING_REQUIRED,),  # decision basis, not a blocker
            supplier="APPLE Teknoloji ve Satış Limited Şirketi",
            decision_type=ReviewDecisionType.SELECT_WORKFLOW,
            workflow=WorkflowType.VENDOR_BILL,
            decision_version=5,
            execution_state="completed",
            vendor_bill_id=69,
        ),
        OperatorGuidanceFacts(
            latest_purchase_purpose=PurchasePurpose.INTERNAL_USE,
            latest_accounting_resolution=_fixed_asset_resolution(),
            eligible_asset_account_ids=(91,),
        ),
    )
    assert guidance.next_action is OperatorNextAction.DONE
    html = guidance.completed_html
    for expected in (
        "✓ Tedarikçi: APPLE",
        "Şirket İçi Kullanım",
        "Sabit kıymet — hesap #91, amortisman modeli #12",
        "Tedarikçi Faturası (v5)",
        "Taslak fatura: Odoo kayıt #69",
    ):
        assert expected in html
    assert "onaylayın" in guidance.todo_html  # posting stays a human Odoo action


def test_accepted_vendor_bill_without_execution_asks_to_create_the_bill() -> None:
    guidance = build_operator_guidance(
        _input(
            status=ReviewStatus.DECISION_SUBMITTED,
            decision_type=ReviewDecisionType.SELECT_WORKFLOW,
            workflow=WorkflowType.VENDOR_BILL,
            decision_version=5,
            execution_state="waiting_retry",
        ),
        OperatorGuidanceFacts(),
    )
    assert guidance.next_action is OperatorNextAction.EXECUTE


@pytest.mark.parametrize("purpose", [PurchasePurpose.RESALE, PurchasePurpose.CUSTOMER_PROJECT])
def test_resale_and_customer_project_are_not_offered_an_unsupported_accounting_step(purpose) -> None:
    guidance = build_operator_guidance(
        _input(codes=(R.OPERATING_EXPENSE_MAPPING_REQUIRED,)),
        OperatorGuidanceFacts(current_purchase_purpose=purpose, latest_purchase_purpose=purpose),
    )
    assert guidance.next_action is OperatorNextAction.TECHNICAL


def test_resale_product_blocker_needs_technical_support_in_this_slice() -> None:
    guidance = build_operator_guidance(_input(codes=(R.PRODUCT_NOT_FOUND,)), OperatorGuidanceFacts())
    assert guidance.next_action is OperatorNextAction.TECHNICAL
    assert "Ürün Odoo'da bulunamadı" in guidance.todo_html


def test_customer_project_decision_after_blockers_asks_for_the_decision() -> None:
    # Business-context allocations are part of the existing decision inputs (İş Bağlamı tab).
    guidance = build_operator_guidance(
        _input(codes=()), OperatorGuidanceFacts(latest_purchase_purpose=PurchasePurpose.CUSTOMER_PROJECT)
    )
    assert guidance.next_action is OperatorNextAction.DECISION


def test_dismissed_and_resolved_reviews_are_done() -> None:
    dismissed = _input(
        status=ReviewStatus.DECISION_SUBMITTED, decision_type=ReviewDecisionType.DISMISS, decision_version=2
    )
    assert build_operator_guidance(dismissed, OperatorGuidanceFacts()).next_action is OperatorNextAction.DONE
    assert (
        build_operator_guidance(_input(status=ReviewStatus.RESOLVED), OperatorGuidanceFacts()).next_action
        is OperatorNextAction.DONE
    )


def test_guidance_escapes_supplier_text() -> None:
    guidance = build_operator_guidance(_input(codes=(), supplier="<script>x</script>"), OperatorGuidanceFacts())
    assert "<script>" not in guidance.completed_html


# ---------------------------------------------------------------------- publisher

MODEL = "x_ipp_import_workbench"
SELECTIONS = {
    "x_studio_review_status": ("Pending Review", "Decision Submitted", "Resolved", "Dismissed"),
    "x_studio_workflow": ("Vendor Bill", "Manual Review"),
    "x_studio_ipp_next_action": tuple(action.value for action in OperatorNextAction),
}


class Studio:
    def __init__(self, row: dict[str, Any] | None = None) -> None:
        self.row = row
        self.writes: list[dict[str, Any]] = []
        self.creates: list[dict[str, Any]] = []

    def search_read(self, *, model: str, domain: list[Any], fields: list[str], limit: int, offset: int = 0):
        if self.row is None:
            return ()
        return ({name: self.row.get(name, False) for name in fields} | {"id": 7},)

    def create(self, *, model: str, values: dict[str, Any]) -> int:
        self.creates.append(values)
        return 7

    def write(self, *, model: str, record_id: int, values: dict[str, Any]) -> None:
        self.writes.append(values)

    def read_selection_values(self, *, model: str, field_name: str) -> tuple[str, ...]:
        return SELECTIONS.get(field_name, ())


def _mapping(*, guidance: bool) -> OdooWorkbenchProjectionFieldMapping:
    extra = (
        {
            "next_action": "x_studio_ipp_next_action",
            "todo": "x_studio_ipp_todo",
            "completed": "x_studio_ipp_completed",
            "eligible_asset_accounts": "x_studio_ipp_eligible_asset_account_ids",
        }
        if guidance
        else {}
    )
    return OdooWorkbenchProjectionFieldMapping(
        model=MODEL,
        name="x_name",
        review_id="x_studio_review_id",
        company_id="x_studio_company",
        invoice_number="x_studio_invoice_number",
        supplier="x_studio_supplier",
        supplier_tax_number="x_studio_supplier_tax_number",
        invoice_date="x_studio_invoice_date",
        currency="x_studio_currency",
        invoice_total="x_studio_invoice_total",
        review_status="x_studio_review_status",
        workflow="x_studio_workflow",
        review_version="x_studio_review_version",
        last_sync_at="x_studio_last_sync_at",
        **extra,
    )


def _projection(**overrides: Any) -> WorkbenchProjection:
    from app.application.workbench.dto import ReviewReasonsRole

    values: dict[str, Any] = {
        "review_id": "review:cloudspark",
        "company_id": 1,
        "invoice_id": "inv-1",
        "version": 3,
        "status": ReviewStatus.PENDING_REVIEW,
        "invoice_number": "CSP2026000001",
        "supplier_name": "CLOUDSPARK BILISIM",
        "supplier_tax_number": "1234567890",
        "invoice_date": date(2026, 9, 1),
        "currency": "TRY",
        "total_amount": Decimal("1200.00"),
        "workflow": WorkflowType.MANUAL_REVIEW,
        "review_reasons_role": ReviewReasonsRole.CURRENT_BLOCKERS,
    }
    values.update(overrides)
    return WorkbenchProjection(**values)


def _guided(projection: WorkbenchProjection, eligible: tuple[int, ...] = (74, 91)) -> WorkbenchProjection:
    import dataclasses

    guidance = build_operator_guidance(
        _input(codes=(R.SUPPLIER_AMBIGUOUS,)), OperatorGuidanceFacts(eligible_asset_account_ids=eligible)
    )
    return dataclasses.replace(projection, operator_guidance=guidance)


def test_publisher_projects_guidance_fields_and_writes_many2many_as_replace_command() -> None:
    studio = Studio()
    publisher = OdooWorkbenchProjectionPublisher(adapter=studio, mapping=_mapping(guidance=True))
    publisher.sync_projection(_guided(_projection()), apply=True)

    created = studio.creates[0]
    assert created["x_studio_ipp_next_action"] == "Tedarikçi Doğrulanmalı"
    assert "Tedarikçi eşleşmesi doğrulanmalı" in created["x_studio_ipp_todo"]
    assert created["x_studio_ipp_eligible_asset_account_ids"] == [[6, 0, [74, 91]]]


def test_publisher_compares_many2many_as_id_sets() -> None:
    projection = _guided(_projection())
    publisher = OdooWorkbenchProjectionPublisher(adapter=Studio(), mapping=_mapping(guidance=True))
    desired = publisher._desired_values(projection)  # noqa: SLF001 - the exact payload Odoo would hold
    row = dict(desired) | {
        "x_studio_review_id": "review:cloudspark",
        "x_studio_company": 1,
        "x_studio_ipp_eligible_asset_account_ids": [91, 74],  # Odoo read order is irrelevant
    }
    studio = Studio(row)
    result = OdooWorkbenchProjectionPublisher(adapter=studio, mapping=_mapping(guidance=True)).sync_projection(
        projection, apply=True
    )
    assert result.outcome.value == "no_change" and studio.writes == []


def test_unmapped_guidance_keeps_the_pre_adr_0013_payload() -> None:
    publisher = OdooWorkbenchProjectionPublisher(adapter=Studio(), mapping=_mapping(guidance=False))
    assert publisher._desired_values(_guided(_projection())) == publisher._desired_values(_projection())  # noqa: SLF001


def test_unrepresentable_next_action_fails_the_review_explicitly() -> None:
    SELECTIONS["x_studio_ipp_next_action"] = ("Tamamlandı",)
    try:
        publisher = OdooWorkbenchProjectionPublisher(adapter=Studio(), mapping=_mapping(guidance=True))
        from app.application.workbench.exceptions import WorkbenchProjectionPublishError

        with pytest.raises(WorkbenchProjectionPublishError, match="selection"):
            publisher.sync_projection(_guided(_projection()), apply=False)
    finally:
        SELECTIONS["x_studio_ipp_next_action"] = tuple(action.value for action in OperatorNextAction)


# ---------------------------------------------------------------------- synchronizer integration


class _Reviews:
    def __init__(self, review: ReviewItem) -> None:
        self.review = review

    def get_review_item(self, query):
        return self.review

    def get_accepted_decision(self, **kwargs):
        raise ReviewNotFoundError("none")


class _Snapshots:
    def find_latest_snapshot_for_review(self, **kwargs):
        return None


def _review() -> ReviewItem:
    return ReviewItem(
        review_id="review:cloudspark",
        invoice_id="inv-1",
        invoice_number="CSP2026000001",
        supplier_tax_number="1234567890",
        supplier_name="CLOUDSPARK BILISIM",
        invoice_date=date(2026, 9, 1),
        currency="TRY",
        total_amount=Decimal("1200.00"),
        workflow=WorkflowType.MANUAL_REVIEW,
        status=ReviewStatus.PENDING_REVIEW,
        review_reasons=(ManualReviewReason(code=R.OPERATING_EXPENSE_MAPPING_REQUIRED, message="mapping required"),),
        updated_at=datetime(2026, 10, 6, tzinfo=UTC),
        version=3,
    )


def _synchronizer(facts_reader) -> WorkbenchProjectionSynchronizer:
    reviews = _Reviews(_review())

    @contextmanager
    def scope():
        yield WorkbenchProjectionSources(
            review_reader=reviews,
            accepted_decision_reader=reviews,
            accepted_source_reader=reviews,
            execution_snapshot_reader=_Snapshots(),
            publisher=None,
            guidance_facts_reader=facts_reader,
        )

    return WorkbenchProjectionSynchronizer(read_scope=scope)


def test_synchronizer_attaches_guidance_from_committed_facts_with_the_company_scope() -> None:
    seen: list[tuple[str, int]] = []

    def facts(review: ReviewItem, company_id: int) -> OperatorGuidanceFacts:
        seen.append((review.review_id, company_id))
        return OperatorGuidanceFacts(current_purchase_purpose=PurchasePurpose.INTERNAL_USE)

    projection = _synchronizer(facts).build_projection(review_id="review:cloudspark", company_id=1)
    assert seen == [("review:cloudspark", 1)]
    assert projection.operator_guidance is not None
    assert projection.operator_guidance.next_action is OperatorNextAction.ACCOUNTING


def test_synchronizer_without_guidance_reader_is_unchanged() -> None:
    projection = _synchronizer(None).build_projection(review_id="review:cloudspark", company_id=1)
    assert projection.operator_guidance is None


# ---------------------------------------------------------------------- provisioning doc stays in sync


def test_provisioning_doc_lists_every_env_key_and_studio_label_the_code_depends_on() -> None:
    import inspect
    import re
    from pathlib import Path

    from app.erp.odoo import workbench_operator_request_reader as reader_module
    from app.erp.odoo.workbench_operator_request_reader import (
        ACTION_BY_ODOO_VALUE,
        ODOO_RESULT_BY_OUTCOME,
        PURPOSE_BY_ODOO_VALUE,
        SUPPLIER_MODE_BY_ODOO_VALUE,
        TREATMENT_BY_ODOO_VALUE,
    )

    doc = (Path(__file__).resolve().parents[2] / "docs" / "ODOO_WORKBENCH_OPERATOR_UI.md").read_text(encoding="utf-8")
    request_keys = re.findall(r'(?:required|optional)\("([A-Z_]+)"\)', inspect.getsource(reader_module))
    assert request_keys
    for key in request_keys:
        assert key in doc, key
    for key in ("NEXT_ACTION_FIELD", "TODO_FIELD", "COMPLETED_FIELD", "ELIGIBLE_ASSET_ACCOUNTS_FIELD"):
        assert key in doc, key
    for table in (ACTION_BY_ODOO_VALUE, SUPPLIER_MODE_BY_ODOO_VALUE, PURPOSE_BY_ODOO_VALUE, TREATMENT_BY_ODOO_VALUE):
        for label in (value for value in table if not value.islower()):
            assert label in doc, label
    for label in set(ODOO_RESULT_BY_OUTCOME.values()) | {action.value for action in OperatorNextAction}:
        assert label in doc, label
