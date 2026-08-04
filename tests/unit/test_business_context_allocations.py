from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import Decimal
from pathlib import Path

import pytest

from app.application.workbench import (
    AllocationCompleteness,
    BusinessContextAllocation,
    BusinessContextAllocationSet,
    BusinessContextAllocationType,
    BusinessContextDecision,
    ReviewDecisionCommand,
    ReviewDecisionType,
    WorkbenchContractError,
)
from app.application.workflow import WorkflowType


def test_business_context_allocation_is_immutable() -> None:
    allocation = _allocation()

    with pytest.raises(FrozenInstanceError):
        allocation.allocation_key = "changed"  # type: ignore[misc]


def test_business_context_allocation_type_vocabulary_is_canonical() -> None:
    assert {allocation_type.value for allocation_type in BusinessContextAllocationType} == {
        "sales_order_cost",
        "customer_recharge",
        "existing_purchase_order",
        "new_rfq_purchase",
        "project_cost",
        "operating_expense",
        "fixed_asset",
        "subscription_service",
        "internal_cost",
    }
    assert "manual_review" not in {allocation_type.value for allocation_type in BusinessContextAllocationType}


def test_business_context_allocation_requires_allocation_key() -> None:
    with pytest.raises(WorkbenchContractError, match="allocation_key is required"):
        _allocation(allocation_key=" ")


def test_business_context_allocation_set_requires_unique_allocation_keys() -> None:
    with pytest.raises(WorkbenchContractError, match="allocation_key values must be unique"):
        BusinessContextAllocationSet(
            allocations=(
                _allocation(allocation_key="line-1", amount=Decimal("40.00")),
                _allocation(allocation_key="line-1", amount=Decimal("60.00")),
            ),
            invoice_total=Decimal("100.00"),
        )


def test_business_context_allocation_accepts_valid_decimal_amount_without_float_conversion() -> None:
    amount = Decimal("123456789012345678.123456")
    allocation = _allocation(amount=amount)

    assert allocation.amount is amount
    assert "float(" not in Path("app/application/workbench/allocations.py").read_text(encoding="utf-8")


def test_business_context_allocation_rejects_float_amount() -> None:
    with pytest.raises(WorkbenchContractError, match="amount must be a finite Decimal"):
        _allocation(amount=10.5)  # type: ignore[arg-type]


@pytest.mark.parametrize("amount", [Decimal("0"), Decimal("-1.00")])
def test_business_context_allocation_requires_amount_greater_than_zero(amount: Decimal) -> None:
    with pytest.raises(WorkbenchContractError, match="amount must be greater than zero"):
        _allocation(amount=amount)


def test_business_context_allocation_rejects_unsupported_amount_scale() -> None:
    with pytest.raises(WorkbenchContractError, match="at most 6 fractional digits"):
        _allocation(amount=Decimal("1.1234567"))


def test_business_context_allocation_rejects_unsupported_amount_precision() -> None:
    with pytest.raises(WorkbenchContractError, match="at most 24 total digits"):
        _allocation(amount=Decimal("1234567890123456789.123456"))


def test_business_context_allocation_accepts_valid_percentage() -> None:
    allocation = _allocation(amount=None, percentage=Decimal("25.500000"))

    assert allocation.percentage == Decimal("25.500000")


@pytest.mark.parametrize("percentage", [Decimal("0"), Decimal("-0.1")])
def test_business_context_allocation_requires_percentage_greater_than_zero(percentage: Decimal) -> None:
    with pytest.raises(WorkbenchContractError, match="percentage must be greater than zero"):
        _allocation(amount=None, percentage=percentage)


def test_business_context_allocation_requires_percentage_at_most_100() -> None:
    with pytest.raises(WorkbenchContractError, match="percentage must be at most 100"):
        _allocation(amount=None, percentage=Decimal("100.01"))


def test_business_context_allocation_requires_amount_or_percentage() -> None:
    with pytest.raises(WorkbenchContractError, match="amount or percentage is required"):
        _allocation(amount=None, percentage=None)


@pytest.mark.parametrize(
    "field_name",
    [
        "customer_id",
        "recharge_partner_id",
        "customer_invoice_id",
        "target_company_id",
        "opportunity_id",
        "sales_order_id",
        "sales_order_line_id",
        "proposal_scenario_id",
        "purchase_order_id",
        "project_id",
        "analytic_account_id",
        "subscription_id",
    ],
)
def test_business_context_allocation_requires_positive_erp_ids(field_name: str) -> None:
    with pytest.raises(WorkbenchContractError, match=f"{field_name} must be a positive ERP id"):
        _allocation(**{field_name: 0})


def test_business_context_allocation_accepts_customer_invoice_id() -> None:
    allocation = _allocation(customer_invoice_id=9001)

    assert allocation.customer_invoice_id == 9001


@pytest.mark.parametrize("customer_invoice_id", [0, -1, True])
def test_business_context_allocation_rejects_invalid_customer_invoice_id(customer_invoice_id: object) -> None:
    with pytest.raises(WorkbenchContractError):
        _allocation(customer_invoice_id=customer_invoice_id)


def test_business_context_allocation_normalizes_and_validates_currency() -> None:
    allocation = _allocation(currency=" try ")

    assert allocation.currency == "TRY"
    with pytest.raises(WorkbenchContractError, match="currency must be a three-letter code"):
        _allocation(currency="TL")


def test_sales_order_cost_requires_sales_order_id() -> None:
    with pytest.raises(WorkbenchContractError, match="sales_order_id is required"):
        _allocation(allocation_type=BusinessContextAllocationType.SALES_ORDER_COST, sales_order_id=None)


def test_customer_recharge_requires_recharge_partner_id() -> None:
    with pytest.raises(WorkbenchContractError, match="recharge_partner_id is required"):
        _allocation(allocation_type=BusinessContextAllocationType.CUSTOMER_RECHARGE)

    allocation = _allocation(
        allocation_type=BusinessContextAllocationType.CUSTOMER_RECHARGE,
        customer_id=10,
        recharge_partner_id=20,
    )
    assert allocation.customer_id == 10
    assert allocation.recharge_partner_id == 20


def test_existing_purchase_order_requires_purchase_order_id() -> None:
    with pytest.raises(WorkbenchContractError, match="purchase_order_id is required"):
        _allocation(allocation_type=BusinessContextAllocationType.EXISTING_PURCHASE_ORDER)


def test_project_cost_requires_project_or_analytic_account() -> None:
    with pytest.raises(WorkbenchContractError, match="project_id or analytic_account_id is required"):
        _allocation(allocation_type=BusinessContextAllocationType.PROJECT_COST)

    assert _allocation(allocation_type=BusinessContextAllocationType.PROJECT_COST, project_id=7).project_id == 7
    analytic_allocation = _allocation(
        allocation_type=BusinessContextAllocationType.PROJECT_COST,
        analytic_account_id=8,
    )
    assert analytic_allocation.analytic_account_id == 8


def test_internal_cost_does_not_require_customer() -> None:
    allocation = _allocation(allocation_type=BusinessContextAllocationType.INTERNAL_COST)

    assert allocation.customer_id is None


def test_same_source_line_may_appear_in_multiple_allocations() -> None:
    allocation_set = BusinessContextAllocationSet(
        allocations=(
            _allocation(allocation_key="a", source_line_number="1", amount=Decimal("40.00")),
            _allocation(allocation_key="b", source_line_number="1", amount=Decimal("60.00")),
        ),
        invoice_total=Decimal("100.00"),
    )

    assert [allocation.source_line_number for allocation in allocation_set.allocations] == ["1", "1"]


def test_complete_amount_set_reconciles_exactly() -> None:
    allocation_set = BusinessContextAllocationSet(
        allocations=(
            _allocation(allocation_key="a", amount=Decimal("40.00")),
            _allocation(allocation_key="b", amount=Decimal("60.00")),
        ),
        invoice_total=Decimal("100.00"),
    )

    assert allocation_set.allocated_amount_total == Decimal("100.00")


def test_complete_amount_set_mismatch_is_rejected() -> None:
    with pytest.raises(WorkbenchContractError, match="must equal invoice_total"):
        BusinessContextAllocationSet(
            allocations=(
                _allocation(allocation_key="a", amount=Decimal("40.00")),
                _allocation(allocation_key="b", amount=Decimal("50.00")),
            ),
            invoice_total=Decimal("100.00"),
        )


def test_complete_percentage_set_totals_100() -> None:
    allocation_set = BusinessContextAllocationSet(
        allocations=(
            _allocation(allocation_key="a", amount=None, percentage=Decimal("40")),
            _allocation(allocation_key="b", amount=None, percentage=Decimal("60")),
        )
    )

    assert allocation_set.allocated_percentage_total == Decimal("100")


def test_complete_percentage_mismatch_is_rejected() -> None:
    with pytest.raises(WorkbenchContractError, match="must total 100"):
        BusinessContextAllocationSet(
            allocations=(
                _allocation(allocation_key="a", amount=None, percentage=Decimal("40")),
                _allocation(allocation_key="b", amount=None, percentage=Decimal("50")),
            )
        )


def test_partial_amount_set_below_total_is_accepted() -> None:
    allocation_set = BusinessContextAllocationSet(
        allocations=(_allocation(amount=Decimal("40.00")),),
        completeness=AllocationCompleteness.PARTIAL,
        invoice_total=Decimal("100.00"),
    )

    assert allocation_set.allocated_amount_total == Decimal("40.00")


def test_partial_amount_set_above_invoice_total_is_rejected() -> None:
    with pytest.raises(WorkbenchContractError, match="must not exceed invoice_total"):
        BusinessContextAllocationSet(
            allocations=(_allocation(amount=Decimal("140.00")),),
            completeness=AllocationCompleteness.PARTIAL,
            invoice_total=Decimal("100.00"),
        )


def test_partial_percentage_above_100_is_rejected() -> None:
    with pytest.raises(WorkbenchContractError, match="must not exceed 100"):
        BusinessContextAllocationSet(
            allocations=(
                _allocation(allocation_key="a", amount=None, percentage=Decimal("60")),
                _allocation(allocation_key="b", amount=None, percentage=Decimal("50")),
            ),
            completeness=AllocationCompleteness.PARTIAL,
        )


def test_mixed_amount_percentage_complete_set_is_rejected_until_reconciliation_exists() -> None:
    with pytest.raises(WorkbenchContractError, match="COMPLETE allocations require"):
        BusinessContextAllocationSet(
            allocations=(
                _allocation(allocation_key="amount-only", amount=Decimal("40.00"), percentage=None),
                _allocation(allocation_key="percent-only", amount=None, percentage=Decimal("60")),
            ),
            invoice_total=Decimal("100.00"),
        )


def test_mixed_amount_percentage_partial_set_is_accepted_when_below_limits() -> None:
    allocation_set = BusinessContextAllocationSet(
        allocations=(
            _allocation(allocation_key="amount-only", amount=Decimal("40.00"), percentage=None),
            _allocation(allocation_key="percent-only", amount=None, percentage=Decimal("50")),
        ),
        completeness=AllocationCompleteness.PARTIAL,
        invoice_total=Decimal("100.00"),
    )

    assert allocation_set.allocated_amount_total == Decimal("40.00")
    assert allocation_set.allocated_percentage_total == Decimal("50")


def test_decimal_precision_is_retained_without_float_conversion() -> None:
    allocation = _allocation(amount=Decimal("259.2000"), percentage=Decimal("25.0000"))

    assert allocation.amount == Decimal("259.2000")
    assert allocation.percentage == Decimal("25.0000")
    assert "float(" not in Path("app/application/workbench/allocations.py").read_text(encoding="utf-8")


def test_allocation_contracts_import_no_odoo_or_sqlalchemy() -> None:
    source = Path("app/application/workbench/allocations.py").read_text(encoding="utf-8").lower()

    for forbidden in ("odoo", "sqlalchemy", "app.models", "app.db", "app.connectors", "httpx", "fastapi"):
        assert forbidden not in source


def test_allocation_contract_is_now_active_submission_evidence() -> None:
    schema_source = Path("app/schemas/workbench.py").read_text(encoding="utf-8")
    command_source = Path("app/application/workbench/commands.py").read_text(encoding="utf-8")
    persistence_source = Path("app/persistence/workbench_review_repository.py").read_text(encoding="utf-8")

    assert "business_context_allocations" in schema_source
    assert "business_context_allocations" in command_source
    assert "BusinessContextAllocation" in persistence_source


def test_allocation_contracts_do_not_execute_workflows() -> None:
    source = Path("app/application/workbench/allocations.py").read_text(encoding="utf-8")

    for forbidden in ("WorkflowStrategy", "DecisionEngine", "VendorBillStrategy", "execute(", "VendorBillWriter"):
        assert forbidden not in source


def test_legacy_business_context_decision_remains_exported_but_not_active_command_input() -> None:
    context = BusinessContextDecision(sales_order_id=10, project_id=20)

    assert context.sales_order_id == 10
    with pytest.raises(TypeError):
        ReviewDecisionCommand(
            review_id="review-1",
            company_id=7,
            expected_version=1,
            decision=ReviewDecisionType.SELECT_WORKFLOW,
            decided_by="user-1",
            idempotency_key="decision-key-1",
            selected_workflow=WorkflowType.VENDOR_BILL,
            business_context=context,
        )


def test_review_decision_command_accepts_allocation_set() -> None:
    allocation_set = BusinessContextAllocationSet(
        allocations=(_allocation(),),
        invoice_total=Decimal("100.00"),
        currency="TRY",
    )
    command = ReviewDecisionCommand(
        review_id="review-1",
        company_id=7,
        expected_version=1,
        decision=ReviewDecisionType.SELECT_WORKFLOW,
        decided_by="user-1",
        idempotency_key="decision-key-1",
        selected_workflow=WorkflowType.VENDOR_BILL,
        business_context_allocations=allocation_set,
    )

    assert command.business_context_allocations is allocation_set
    assert not hasattr(command, "business_context")


def _allocation(**overrides) -> BusinessContextAllocation:
    values = {
        "allocation_key": "allocation-1",
        "allocation_type": BusinessContextAllocationType.SALES_ORDER_COST,
        "source_line_number": "1",
        "description": "Customer project cost",
        "amount": Decimal("100.00"),
        "percentage": None,
        "currency": "TRY",
        "sales_order_id": 10,
    }
    values.update(overrides)
    return BusinessContextAllocation(**values)
