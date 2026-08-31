from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.application.workbench import (
    AllocationCompleteness,
    BusinessContextAllocationType,
    OdooWorkbenchDecisionCandidate,
    ReviewDecisionType,
    WorkbenchCandidateAmbiguityError,
    WorkbenchCandidateDataError,
    WorkbenchCandidateNotFoundError,
    WorkbenchCandidateReadError,
    WorkbenchCandidateUnsupportedDecisionError,
    WorkbenchContractError,
)
from app.application.workflow import WorkflowType
from app.connectors.exceptions import ConnectorError
from app.connectors.odoo.client import OdooJson2Client
from app.erp.exceptions import ErpRepositoryError
from app.erp.odoo.workbench_candidate_reader import (
    OdooWorkbenchAllocationFieldMapping,
    OdooWorkbenchDecisionCandidateReader,
    OdooWorkbenchFieldMapping,
    OdooWorkbenchParentFieldMapping,
)


def test_mapping_configuration_validates_required_names_and_is_immutable() -> None:
    mapping = _mapping(customer_invoice=None)

    assert mapping.allocation.customer_invoice is None
    with pytest.raises(FrozenInstanceError):
        mapping.parent.model = "changed"  # type: ignore[misc]
    with pytest.raises(WorkbenchContractError):
        _mapping(parent_model=" ")
    with pytest.raises(WorkbenchContractError):
        _mapping(allocation_key="")


def test_parent_lookup_uses_exact_review_and_company_domain_and_limit_two() -> None:
    adapter = RecordingAdapter(parent_records=[_parent_record()], allocation_records=_allocation_records())
    reader = OdooWorkbenchDecisionCandidateReader(adapter=adapter, mapping=_mapping())

    reader.get_ready_decision(review_id="review-1", company_id=7)

    assert adapter.calls[0] == {
        "method": "search_read",
        "model": "x_ipp_import_workbench",
        "domain": [["x_review_id", "=", "review-1"], ["x_company_id", "=", 7]],
        "fields": [
            "id",
            "x_review_id",
            "x_company_id",
            "x_version",
            "x_decision",
            "x_selected_workflow",
            "x_decision_ready",
            "x_decided_at",
            "x_decided_by",
            "x_idempotency_key",
            "x_allocations",
            "x_invoice_total",
            "x_currency",
            "x_selected_partner",
            "x_comment",
            "x_line_resolutions",
            "x_tax_resolutions",
            "x_allocation_completeness",
        ],
        "limit": 2,
        "offset": 0,
    }


def test_no_parent_match_returns_canonical_not_found() -> None:
    reader = OdooWorkbenchDecisionCandidateReader(adapter=RecordingAdapter(parent_records=[]), mapping=_mapping())

    with pytest.raises(WorkbenchCandidateNotFoundError):
        reader.get_ready_decision(review_id="review-1", company_id=7)


def test_decision_ready_false_returns_not_found_without_child_read() -> None:
    adapter = RecordingAdapter(parent_records=[_parent_record(x_decision_ready=False)])
    reader = OdooWorkbenchDecisionCandidateReader(adapter=adapter, mapping=_mapping())

    with pytest.raises(WorkbenchCandidateNotFoundError):
        reader.get_ready_decision(review_id="review-1", company_id=7)

    assert len(adapter.calls) == 1


def test_missing_decision_ready_is_rejected_safely() -> None:
    parent = _parent_record()
    parent.pop("x_decision_ready")
    reader = OdooWorkbenchDecisionCandidateReader(
        adapter=RecordingAdapter(parent_records=[parent]),
        mapping=_mapping(),
    )

    with pytest.raises(WorkbenchCandidateDataError):
        reader.get_ready_decision(review_id="review-1", company_id=7)


def test_multiple_parent_matches_are_rejected_safely() -> None:
    reader = OdooWorkbenchDecisionCandidateReader(
        adapter=RecordingAdapter(parent_records=[_parent_record(), _parent_record(id=43)]),
        mapping=_mapping(),
    )

    with pytest.raises(WorkbenchCandidateAmbiguityError):
        reader.get_ready_decision(review_id="review-1", company_id=7)


def test_provider_failure_is_translated_safely() -> None:
    sensitive = ErpRepositoryError("raw Odoo payload token=secret")
    reader = OdooWorkbenchDecisionCandidateReader(
        adapter=RecordingAdapter(parent_error=sensitive),
        mapping=_mapping(),
    )

    with pytest.raises(WorkbenchCandidateReadError) as error:
        reader.get_ready_decision(review_id="review-1", company_id=7)

    assert str(error.value) == "Odoo Workbench decision candidate read failed."
    assert "secret" not in str(error.value)
    assert error.value.__cause__ is sensitive


def test_parent_fields_and_many2one_ids_are_parsed() -> None:
    reader = OdooWorkbenchDecisionCandidateReader(
        adapter=RecordingAdapter(parent_records=[_parent_record()], allocation_records=_allocation_records()),
        mapping=_mapping(),
    )

    candidate = reader.get_ready_decision(review_id="review-1", company_id=7)

    assert isinstance(candidate, OdooWorkbenchDecisionCandidate)
    assert candidate.review_id == "review-1"
    assert candidate.company_id == 7
    assert candidate.expected_version == 4
    assert candidate.decision is ReviewDecisionType.SELECT_WORKFLOW
    assert candidate.selected_workflow is WorkflowType.VENDOR_BILL
    assert candidate.selected_partner_id == 700
    assert candidate.decided_by_odoo_user_id == 11
    assert candidate.decided_at == datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
    assert candidate.idempotency_key == "odoo-key-1"
    assert candidate.line_resolutions[0].selected_product_id == 800
    assert candidate.tax_resolutions[0].selected_tax_id == 900
    with pytest.raises(FrozenInstanceError):
        candidate.review_id = "changed"  # type: ignore[misc]


def test_existing_purchase_order_selected_workflow_requires_explicit_allocation_intent() -> None:
    reader = OdooWorkbenchDecisionCandidateReader(
        adapter=RecordingAdapter(
            parent_records=[_parent_record(x_selected_workflow="Existing Purchase Order")],
            allocation_records=_allocation_records(),
        ),
        mapping=_mapping(),
    )

    with pytest.raises(WorkbenchCandidateDataError):
        reader.get_ready_decision(review_id="review-1", company_id=7)


def test_existing_purchase_order_intent_is_preserved_by_canonical_allocation_type() -> None:
    reader = OdooWorkbenchDecisionCandidateReader(
        adapter=RecordingAdapter(
            parent_records=[_parent_record(x_selected_workflow="Existing Purchase Order")],
            allocation_records=_existing_purchase_order_allocation_records(),
        ),
        mapping=_mapping(),
    )

    candidate = reader.get_ready_decision(review_id="review-1", company_id=7)

    assert candidate.selected_workflow is WorkflowType.VENDOR_BILL
    assert candidate.business_context_allocations is not None
    assert candidate.business_context_allocations.allocations[0].allocation_type is (
        BusinessContextAllocationType.EXISTING_PURCHASE_ORDER
    )
    assert candidate.business_context_allocations.allocations[0].purchase_order_id == 501


def test_direct_vendor_bill_rejects_purchase_order_intent_collision() -> None:
    reader = OdooWorkbenchDecisionCandidateReader(
        adapter=RecordingAdapter(
            parent_records=[_parent_record(x_selected_workflow="Direct Vendor Bill")],
            allocation_records=_existing_purchase_order_allocation_records(),
        ),
        mapping=_mapping(),
    )

    with pytest.raises(WorkbenchCandidateDataError):
        reader.get_ready_decision(review_id="review-1", company_id=7)


def test_new_rfq_purchase_selected_workflow_requires_matching_allocation_intent() -> None:
    reader = OdooWorkbenchDecisionCandidateReader(
        adapter=RecordingAdapter(
            parent_records=[_parent_record(x_selected_workflow="New RFQ + Purchase Order")],
            allocation_records=_new_rfq_purchase_allocation_records(),
        ),
        mapping=_mapping(),
    )

    candidate = reader.get_ready_decision(review_id="review-1", company_id=7)

    assert candidate.selected_workflow is WorkflowType.RFQ
    assert candidate.business_context_allocations is not None
    assert candidate.business_context_allocations.allocations[0].allocation_type is (
        BusinessContextAllocationType.NEW_RFQ_PURCHASE
    )


def test_malformed_parent_values_are_rejected_safely() -> None:
    cases = [
        {"x_decision": "not-real"},
        {"x_selected_workflow": "not-real"},
        {"x_selected_partner": True},
        {"x_version": True},
        {"x_decided_at": "2026-08-04T12:00:00"},
        {"x_company_id": [8, "Other"]},
    ]
    for override in cases:
        reader = OdooWorkbenchDecisionCandidateReader(
            adapter=RecordingAdapter(
                parent_records=[_parent_record(**override)],
                allocation_records=_allocation_records(),
            ),
            mapping=_mapping(),
        )

        with pytest.raises(WorkbenchCandidateDataError):
            reader.get_ready_decision(review_id="review-1", company_id=7)


def test_request_investigation_is_rejected_without_canonical_enum_expansion() -> None:
    reader = OdooWorkbenchDecisionCandidateReader(
        adapter=RecordingAdapter(
            parent_records=[_parent_record(x_decision="Request Investigation")],
            allocation_records=_allocation_records(),
        ),
        mapping=_mapping(),
    )

    with pytest.raises(WorkbenchCandidateUnsupportedDecisionError):
        reader.get_ready_decision(review_id="review-1", company_id=7)

    assert not any(member.value == "request_investigation" for member in ReviewDecisionType)


def test_child_allocations_are_read_by_exact_parent_id_and_ordered_deterministically() -> None:
    adapter = RecordingAdapter(
        parent_records=[_parent_record()],
        allocation_records=list(reversed(_allocation_records())),
    )
    reader = OdooWorkbenchDecisionCandidateReader(adapter=adapter, mapping=_mapping())

    candidate = reader.get_ready_decision(review_id="review-1", company_id=7)

    assert adapter.calls[1]["method"] == "search_read_all"
    assert adapter.calls[1]["model"] == "x_ipp_review_allocatio"
    assert adapter.calls[1]["domain"] == [["x_import_review_id", "=", 42]]
    assert candidate.business_context_allocations is not None
    assert [allocation.allocation_key for allocation in candidate.business_context_allocations.allocations] == [
        "ALLOC-001",
        "ALLOC-002",
    ]
    assert candidate.business_context_allocations.allocations[0].source_line_number == "1"
    assert candidate.business_context_allocations.allocations[1].source_line_number == "1"


def test_child_provider_failure_is_translated_safely() -> None:
    reader = OdooWorkbenchDecisionCandidateReader(
        adapter=RecordingAdapter(parent_records=[_parent_record()], child_error=ErpRepositoryError("url secret")),
        mapping=_mapping(),
    )

    with pytest.raises(WorkbenchCandidateReadError) as error:
        reader.get_ready_decision(review_id="review-1", company_id=7)

    assert "secret" not in str(error.value)


def test_allocation_fields_are_parsed_without_using_display_names_as_identity() -> None:
    reader = OdooWorkbenchDecisionCandidateReader(
        adapter=RecordingAdapter(parent_records=[_parent_record()], allocation_records=_allocation_records()),
        mapping=_mapping(),
    )

    allocation_set = reader.get_ready_decision(review_id="review-1", company_id=7).business_context_allocations

    assert allocation_set is not None
    first = allocation_set.allocations[0]
    assert first.allocation_type is BusinessContextAllocationType.SALES_ORDER_COST
    assert first.amount == Decimal("40000.000000")
    assert first.percentage == Decimal("40")
    assert isinstance(first.amount, Decimal)
    assert first.customer_id == 101
    assert first.recharge_partner_id == 105
    assert first.customer_invoice_id == 9001
    assert first.sales_order_id == 301
    assert first.internal_note == "note"
    with pytest.raises(FrozenInstanceError):
        allocation_set.allocations = ()  # type: ignore[misc]


def test_allocation_type_mapping_is_explicit_and_exact() -> None:
    records = _allocation_records()
    records[0]["x_allocation_type"] = "Sales Order Cost "
    reader = OdooWorkbenchDecisionCandidateReader(
        adapter=RecordingAdapter(parent_records=[_parent_record()], allocation_records=records),
        mapping=_mapping(),
    )

    with pytest.raises(WorkbenchCandidateDataError):
        reader.get_ready_decision(review_id="review-1", company_id=7)


def test_optional_false_values_are_empty_but_true_numeric_values_are_rejected() -> None:
    records = _allocation_records()
    records[0]["x_customer_invoice_id"] = False
    reader = OdooWorkbenchDecisionCandidateReader(
        adapter=RecordingAdapter(parent_records=[_parent_record()], allocation_records=records),
        mapping=_mapping(),
    )

    assert (
        reader.get_ready_decision(review_id="review-1", company_id=7)
        .business_context_allocations.allocations[0]
        .customer_invoice_id
        is None
    )

    records = _allocation_records()
    records[0]["x_amount"] = True
    reader = OdooWorkbenchDecisionCandidateReader(
        adapter=RecordingAdapter(parent_records=[_parent_record()], allocation_records=records),
        mapping=_mapping(),
    )
    with pytest.raises(WorkbenchCandidateDataError):
        reader.get_ready_decision(review_id="review-1", company_id=7)


def test_malformed_allocation_values_are_rejected_safely() -> None:
    cases = [
        {"x_allocation_type": "not-real"},
        {"x_amount": "not-decimal"},
        {"x_percentage": float("inf")},
        {"x_sales_order_id": True},
    ]
    for override in cases:
        records = _allocation_records()
        records[0].update(override)
        reader = OdooWorkbenchDecisionCandidateReader(
            adapter=RecordingAdapter(parent_records=[_parent_record()], allocation_records=records),
            mapping=_mapping(),
        )
        with pytest.raises(WorkbenchCandidateDataError):
            reader.get_ready_decision(review_id="review-1", company_id=7)


def test_customer_invoice_is_none_when_mapping_is_absent_and_department_is_ignored() -> None:
    records = _allocation_records()
    records[0]["x_studio_department_id"] = [99, "Department"]
    reader = OdooWorkbenchDecisionCandidateReader(
        adapter=RecordingAdapter(parent_records=[_parent_record()], allocation_records=records),
        mapping=_mapping(customer_invoice=None),
    )

    allocation = reader.get_ready_decision(review_id="review-1", company_id=7).business_context_allocations.allocations[
        0
    ]

    assert allocation.customer_invoice_id is None


def test_allocation_set_completeness_policies() -> None:
    assert (
        _candidate(
            _parent_record(x_allocation_completeness="complete"), _allocation_records()
        ).business_context_allocations.completeness
        is AllocationCompleteness.COMPLETE
    )
    assert (
        _candidate(
            _parent_record(x_allocation_completeness="partial", x_invoice_total="200000.000000"),
            _allocation_records(),
        ).business_context_allocations.completeness
        is AllocationCompleteness.PARTIAL
    )
    assert (
        _candidate(
            _parent_record(),
            _allocation_records(),
            mapping=_mapping(allocation_completeness=None, fixed=AllocationCompleteness.COMPLETE),
        ).business_context_allocations.completeness
        is AllocationCompleteness.COMPLETE
    )


def test_missing_explicit_completeness_is_rejected_without_fixed_policy() -> None:
    reader = OdooWorkbenchDecisionCandidateReader(
        adapter=RecordingAdapter(parent_records=[_parent_record()], allocation_records=_allocation_records()),
        mapping=_mapping(allocation_completeness=None),
    )

    with pytest.raises(WorkbenchCandidateDataError):
        reader.get_ready_decision(review_id="review-1", company_id=7)


def test_allocation_set_contract_errors_are_translated_safely() -> None:
    records = _allocation_records()
    records[1]["x_amount"] = "1"
    reader = OdooWorkbenchDecisionCandidateReader(
        adapter=RecordingAdapter(parent_records=[_parent_record()], allocation_records=records),
        mapping=_mapping(),
    )

    with pytest.raises(WorkbenchCandidateDataError):
        reader.get_ready_decision(review_id="review-1", company_id=7)


def test_dismiss_candidate_cannot_carry_allocations() -> None:
    reader = OdooWorkbenchDecisionCandidateReader(
        adapter=RecordingAdapter(
            parent_records=[_parent_record(x_decision="dismiss", x_selected_workflow=False)],
            allocation_records=_allocation_records(),
        ),
        mapping=_mapping(),
    )

    with pytest.raises(WorkbenchCandidateDataError):
        reader.get_ready_decision(review_id="review-1", company_id=7)


def test_list_ready_decisions_uses_company_and_ready_domain() -> None:
    adapter = RecordingAdapter(parent_records=[_parent_record()], allocation_records=_allocation_records())
    reader = OdooWorkbenchDecisionCandidateReader(adapter=adapter, mapping=_mapping())

    candidates = reader.list_ready_decisions(company_id=7, limit=5)

    assert len(candidates) == 1
    assert adapter.calls[0]["domain"] == [["x_company_id", "=", 7], ["x_decision_ready", "=", True]]
    assert adapter.calls[0]["limit"] == 5


def test_list_ready_decisions_orders_candidates_by_odoo_record_id() -> None:
    adapter = RecordingAdapter(
        parent_records=[
            _parent_record(id=43, x_review_id="review-2"),
            _parent_record(id=42, x_review_id="review-1"),
        ],
        allocation_records=_allocation_records(),
    )
    reader = OdooWorkbenchDecisionCandidateReader(adapter=adapter, mapping=_mapping())

    candidates = reader.list_ready_decisions(company_id=7, limit=5)

    assert [candidate.odoo_record_id for candidate in candidates] == [42, 43]


def test_hub_does_not_require_or_trust_odoo_idempotency_field_for_candidate_identity() -> None:
    reader = OdooWorkbenchDecisionCandidateReader(
        adapter=RecordingAdapter(
            parent_records=[_parent_record(x_idempotency_key=False)],
            allocation_records=_allocation_records(),
        ),
        mapping=_mapping(),
    )

    candidate = reader.get_ready_decision(review_id="review-1", company_id=7)

    assert candidate.idempotency_key is None


async def test_odoo_json2_client_allows_studio_custom_models_for_read_only_search_read() -> None:
    client = OdooJson2Client(
        base_url="https://example.odoo.com",
        database="example",
        api_key="secret",
        timeout_seconds=10,
        http_client=FakeHttpClient(),
    )

    assert await client.search_read(model="x_ipp_import_workbench", domain=[], fields=["id"]) == []
    assert await client.search_read(model="x_studio_review_allocation", domain=[], fields=["id"]) == []
    with pytest.raises(ConnectorError):
        await client.search_read(model="account.payment", domain=[], fields=["id"])


def test_reader_implements_existing_port_shape() -> None:
    assert "list_ready_decisions" in OdooWorkbenchDecisionCandidateReader.__dict__
    assert "get_ready_decision" in OdooWorkbenchDecisionCandidateReader.__dict__
    assert "submit_review_decision" not in OdooWorkbenchDecisionCandidateReader.__dict__


def test_reader_architecture_is_read_only_and_has_no_submission_or_execution() -> None:
    source = Path("app/erp/odoo/workbench_candidate_reader.py").read_text(encoding="utf-8").lower()

    for forbidden in (
        "create_account_move",
        ".create(",
        ".write(",
        "unlink",
        "action_post",
        "submit_review_decision",
        "/api/workbench",
        "vendorbill",
        "workflowstrategy",
        "decisionengine",
        "customer invoice creation",
        "fuzzy",
        "levenshtein",
        "embedding",
    ):
        assert forbidden not in source


def test_application_contracts_do_not_import_infrastructure() -> None:
    source = "\n".join(path.read_text(encoding="utf-8") for path in Path("app/application/workbench").rglob("*.py"))

    assert "app.erp.odoo" not in source
    assert "sqlalchemy" not in source.lower()


class RecordingAdapter:
    def __init__(
        self,
        *,
        parent_records: list[dict[str, Any]] | None = None,
        allocation_records: list[dict[str, Any]] | None = None,
        parent_error: Exception | None = None,
        child_error: Exception | None = None,
    ) -> None:
        self.parent_records = parent_records or []
        self.allocation_records = allocation_records or []
        self.parent_error = parent_error
        self.child_error = child_error
        self.calls: list[dict[str, Any]] = []

    def search_read(
        self,
        *,
        model: str,
        domain: list[Any],
        fields: list[str],
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[dict[str, Any], ...]:
        self.calls.append(
            {
                "method": "search_read",
                "model": model,
                "domain": domain,
                "fields": fields,
                "limit": limit,
                "offset": offset,
            }
        )
        if self.parent_error is not None:
            raise self.parent_error
        return tuple(self.parent_records[:limit])

    def search_read_all(
        self,
        *,
        model: str,
        domain: list[Any],
        fields: list[str],
        page_size: int | None = None,
        max_records: int | None = None,
    ) -> tuple[dict[str, Any], ...]:
        self.calls.append(
            {
                "method": "search_read_all",
                "model": model,
                "domain": domain,
                "fields": fields,
                "page_size": page_size,
                "max_records": max_records,
            }
        )
        if self.child_error is not None:
            raise self.child_error
        return tuple(self.allocation_records)


class FakeHttpClient:
    async def post(self, path: str, *, json: dict[str, Any], headers: dict[str, str]):
        del json, headers
        assert path in {
            "/json/2/x_ipp_import_workbench/search_read",
            "/json/2/x_studio_review_allocation/search_read",
        }
        return FakeResponse()


class FakeResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> list[dict[str, Any]]:
        return []


def _candidate(
    parent: dict[str, Any],
    allocations: list[dict[str, Any]],
    *,
    mapping: OdooWorkbenchFieldMapping | None = None,
) -> OdooWorkbenchDecisionCandidate:
    return OdooWorkbenchDecisionCandidateReader(
        adapter=RecordingAdapter(parent_records=[parent], allocation_records=allocations),
        mapping=mapping or _mapping(),
    ).get_ready_decision(review_id="review-1", company_id=7)


def _parent_record(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "id": 42,
        "x_review_id": "review-1",
        "x_company_id": [7, "ICT"],
        "x_version": 4,
        "x_decision": "Submit Decision",
        "x_selected_workflow": "Direct Vendor Bill",
        "x_selected_partner": [700, "Supplier"],
        "x_comment": "Reviewed in Odoo.",
        "x_decision_ready": True,
        "x_decided_at": "2026-08-04T12:00:00+00:00",
        "x_decided_by": [11, "Finance User"],
        "x_idempotency_key": "odoo-key-1",
        "x_allocations": [100, 101],
        "x_invoice_total": "100000.000000",
        "x_currency": "TRY",
        "x_allocation_completeness": "complete",
        "x_line_resolutions": '[{"line_number": "1", "selected_product_id": 800}]',
        "x_tax_resolutions": [{"line_number": "1", "tax_index": 0, "selected_tax_id": 900}],
    }
    values.update(overrides)
    return values


def _allocation_records() -> list[dict[str, Any]]:
    return [
        {
            "id": 100,
            "x_import_review_id": [42, "review-1"],
            "x_allocation_key": "ALLOC-001",
            "x_allocation_type": "Sales Order Cost",
            "x_source_line_number": "1",
            "x_description": "Customer A share",
            "x_amount": 40000.0,
            "x_percentage": "40",
            "x_currency": "TRY",
            "x_customer_id": [101, "Customer A"],
            "x_recharge_partner_id": [105, "Recharge A"],
            "x_customer_invoice_id": [9001, "INV/2026/001"],
            "x_sales_order_id": [301, "SO301"],
            "x_internal_note": "note",
        },
        {
            "id": 101,
            "x_import_review_id": [42, "review-1"],
            "x_allocation_key": "ALLOC-002",
            "x_allocation_type": "Internal Cost",
            "x_source_line_number": "1",
            "x_amount": "60000.000000",
            "x_percentage": "60",
            "x_currency": "TRY",
        },
    ]


def _existing_purchase_order_allocation_records() -> list[dict[str, Any]]:
    return [
        {
            "id": 100,
            "x_import_review_id": [42, "review-1"],
            "x_allocation_key": "ALLOC-PO",
            "x_allocation_type": "Existing Purchase Order",
            "x_source_line_number": "1",
            "x_amount": "100000.000000",
            "x_percentage": "100",
            "x_currency": "TRY",
            "x_purchase_order_id": [501, "PO501"],
        }
    ]


def _new_rfq_purchase_allocation_records() -> list[dict[str, Any]]:
    return [
        {
            "id": 100,
            "x_import_review_id": [42, "review-1"],
            "x_allocation_key": "ALLOC-RFQ",
            "x_allocation_type": "New RFQ + Purchase",
            "x_source_line_number": "1",
            "x_amount": "100000.000000",
            "x_percentage": "100",
            "x_currency": "TRY",
        }
    ]


def _mapping(
    *,
    parent_model: str = "x_ipp_import_workbench",
    allocation_model: str = "x_ipp_review_allocatio",
    allocation_key: str = "x_allocation_key",
    customer_invoice: str | None = "x_customer_invoice_id",
    allocation_completeness: str | None = "x_allocation_completeness",
    fixed: AllocationCompleteness | None = None,
) -> OdooWorkbenchFieldMapping:
    return OdooWorkbenchFieldMapping(
        parent=OdooWorkbenchParentFieldMapping(
            model=parent_model,
            review_id="x_review_id",
            company_id="x_company_id",
            expected_version="x_version",
            decision="x_decision",
            selected_workflow="x_selected_workflow",
            decision_ready="x_decision_ready",
            decided_at="x_decided_at",
            decided_by="x_decided_by",
            idempotency_key="x_idempotency_key",
            allocation_one2many_field="x_allocations",
            invoice_total="x_invoice_total",
            currency="x_currency",
            selected_partner="x_selected_partner",
            decision_comment="x_comment",
            line_resolutions="x_line_resolutions",
            tax_resolutions="x_tax_resolutions",
            allocation_completeness=allocation_completeness,
            fixed_allocation_completeness=fixed,
        ),
        allocation=OdooWorkbenchAllocationFieldMapping(
            model=allocation_model,
            parent_many2one_field="x_import_review_id",
            allocation_key=allocation_key,
            allocation_type="x_allocation_type",
            source_line_number="x_source_line_number",
            description="x_description",
            amount="x_amount",
            percentage="x_percentage",
            currency="x_currency",
            customer="x_customer_id",
            recharge_partner="x_recharge_partner_id",
            customer_invoice=customer_invoice,
            target_company="x_target_company_id",
            opportunity="x_opportunity_id",
            sales_order="x_sales_order_id",
            sales_order_line="x_sales_order_line_id",
            proposal_scenario="x_proposal_scenario_id",
            purchase_order="x_purchase_order_id",
            project="x_project_id",
            analytic_account="x_analytic_account_id",
            subscription="x_subscription_id",
            internal_note="x_internal_note",
        ),
    )
