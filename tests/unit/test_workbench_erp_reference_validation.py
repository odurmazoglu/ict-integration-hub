from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.application.workbench import (
    AllocationCompleteness,
    AnalyticAccountReference,
    BusinessContextAllocation,
    BusinessContextAllocationSet,
    BusinessContextAllocationType,
    CompanyReference,
    CustomerInvoiceReference,
    OdooWorkbenchDecisionCandidate,
    OpportunityReference,
    PartnerReference,
    PurchaseOrderReference,
    ReviewDecisionType,
    SalesOrderLineReference,
    SalesOrderReference,
    WorkbenchContractError,
    WorkbenchErpReferenceCompanyMismatchError,
    WorkbenchErpReferenceNotFoundError,
    WorkbenchErpReferenceRelationshipError,
    WorkbenchErpReferenceTypeError,
    WorkbenchErpReferenceUnsupportedError,
    WorkbenchErpReferenceValidationError,
    WorkbenchErpReferenceValidator,
)
from app.application.workflow import WorkflowType


def test_validator_requires_candidate_and_positive_company_id() -> None:
    validator = _validator()

    with pytest.raises(WorkbenchContractError):
        validator.validate("not-a-candidate", requested_company_id=7)  # type: ignore[arg-type]
    with pytest.raises(WorkbenchContractError):
        validator.validate(_candidate(), requested_company_id=0)
    with pytest.raises(WorkbenchContractError):
        validator.validate(_candidate(), requested_company_id=True)


def test_valid_candidate_is_returned_unchanged_and_allocation_set_is_not_mutated() -> None:
    allocation_set = _allocation_set(_allocation(customer_id=101, sales_order_id=301))
    candidate = _candidate(business_context_allocations=allocation_set)

    result = _validator(
        partners=[PartnerReference(id=101)],
        sales_orders=[SalesOrderReference(id=301, company_id=7, partner_id=101)],
    ).validate(candidate, requested_company_id=7)

    assert result is candidate
    assert result.business_context_allocations is allocation_set


def test_partner_references_are_validated_by_id_without_display_name_identity() -> None:
    allocation = _allocation(
        allocation_type=BusinessContextAllocationType.CUSTOMER_RECHARGE,
        customer_id=101,
        recharge_partner_id=105,
    )

    _validator(partners=[PartnerReference(id=101), PartnerReference(id=105)]).validate(
        _candidate(business_context_allocations=_allocation_set(allocation)),
        requested_company_id=7,
    )

    with pytest.raises(WorkbenchErpReferenceNotFoundError, match="Customer reference is invalid."):
        _validator(partners=[PartnerReference(id=105)]).validate(
            _candidate(business_context_allocations=_allocation_set(allocation)),
            requested_company_id=7,
        )
    with pytest.raises(WorkbenchErpReferenceNotFoundError, match="Recharge Partner reference is invalid."):
        _validator(partners=[PartnerReference(id=101)]).validate(
            _candidate(business_context_allocations=_allocation_set(allocation)),
            requested_company_id=7,
        )


def test_partner_shared_company_semantics_accept_shared_and_requested_company() -> None:
    allocation = _allocation(
        allocation_type=BusinessContextAllocationType.CUSTOMER_RECHARGE,
        customer_id=101,
        recharge_partner_id=105,
    )

    _validator(partners=[PartnerReference(id=101, company_id=None), PartnerReference(id=105, company_id=7)]).validate(
        _candidate(business_context_allocations=_allocation_set(allocation)),
        requested_company_id=7,
    )

    with pytest.raises(WorkbenchErpReferenceCompanyMismatchError):
        _validator(partners=[PartnerReference(id=101, company_id=8), PartnerReference(id=105)]).validate(
            _candidate(business_context_allocations=_allocation_set(allocation)),
            requested_company_id=7,
        )


def test_sales_order_existence_company_and_customer_relationship_are_validated() -> None:
    allocation = _allocation(customer_id=101, sales_order_id=301)

    _validator(
        partners=[PartnerReference(id=101)],
        sales_orders=[SalesOrderReference(id=301, company_id=7, partner_id=101)],
    ).validate(_candidate(business_context_allocations=_allocation_set(allocation)), requested_company_id=7)

    with pytest.raises(WorkbenchErpReferenceNotFoundError, match="Sales Order reference is invalid."):
        _validator(partners=[PartnerReference(id=101)]).validate(
            _candidate(business_context_allocations=_allocation_set(allocation)),
            requested_company_id=7,
        )
    with pytest.raises(WorkbenchErpReferenceCompanyMismatchError):
        _validator(
            partners=[PartnerReference(id=101)],
            sales_orders=[SalesOrderReference(id=301, company_id=8, partner_id=101)],
        ).validate(_candidate(business_context_allocations=_allocation_set(allocation)), requested_company_id=7)
    with pytest.raises(WorkbenchErpReferenceRelationshipError):
        _validator(
            partners=[PartnerReference(id=101)],
            sales_orders=[SalesOrderReference(id=301, company_id=7, partner_id=102)],
        ).validate(_candidate(business_context_allocations=_allocation_set(allocation)), requested_company_id=7)


def test_sales_order_customer_relationship_is_skipped_when_customer_missing() -> None:
    _validator(sales_orders=[SalesOrderReference(id=301, company_id=7, partner_id=999)]).validate(
        _candidate(business_context_allocations=_allocation_set(_allocation(sales_order_id=301))),
        requested_company_id=7,
    )


def test_sales_order_line_supported_validation_and_relationship() -> None:
    allocation = _allocation(sales_order_id=301, sales_order_line_id=401)

    _validator(
        sales_orders=[SalesOrderReference(id=301, company_id=7, partner_id=101)],
        sales_order_lines=[SalesOrderLineReference(id=401, order_id=301)],
    ).validate(_candidate(business_context_allocations=_allocation_set(allocation)), requested_company_id=7)

    with pytest.raises(WorkbenchErpReferenceRelationshipError):
        _validator(
            sales_orders=[SalesOrderReference(id=301, company_id=7, partner_id=101)],
            sales_order_lines=[SalesOrderLineReference(id=401, order_id=302)],
        ).validate(_candidate(business_context_allocations=_allocation_set(allocation)), requested_company_id=7)


def test_unsupported_sales_order_line_reference_rejects_safely() -> None:
    with pytest.raises(WorkbenchErpReferenceUnsupportedError):
        _validator(
            sales_orders=[SalesOrderReference(id=301, company_id=7)],
            sales_order_lines=None,
        ).validate(
            _candidate(
                business_context_allocations=_allocation_set(_allocation(sales_order_id=301, sales_order_line_id=401))
            ),
            requested_company_id=7,
        )


def test_purchase_order_existence_and_company_are_validated() -> None:
    allocation = _allocation(
        allocation_type=BusinessContextAllocationType.EXISTING_PURCHASE_ORDER,
        purchase_order_id=501,
    )

    _validator(purchase_orders=[PurchaseOrderReference(id=501, company_id=7, partner_id=201)]).validate(
        _candidate(business_context_allocations=_allocation_set(allocation)),
        requested_company_id=7,
    )
    with pytest.raises(WorkbenchErpReferenceNotFoundError, match="Purchase Order reference is invalid."):
        _validator().validate(
            _candidate(business_context_allocations=_allocation_set(allocation)),
            requested_company_id=7,
        )
    with pytest.raises(WorkbenchErpReferenceCompanyMismatchError):
        _validator(purchase_orders=[PurchaseOrderReference(id=501, company_id=8, partner_id=201)]).validate(
            _candidate(business_context_allocations=_allocation_set(allocation)),
            requested_company_id=7,
        )


@pytest.mark.parametrize("move_type", ["out_invoice", "out_refund"])
def test_customer_invoice_outgoing_types_are_accepted(move_type: str) -> None:
    allocation = _allocation(
        allocation_type=BusinessContextAllocationType.CUSTOMER_RECHARGE,
        recharge_partner_id=105,
        customer_invoice_id=9001,
    )

    _validator(
        partners=[PartnerReference(id=105)],
        customer_invoices=[CustomerInvoiceReference(id=9001, company_id=7, partner_id=105, move_type=move_type)],
    ).validate(_candidate(business_context_allocations=_allocation_set(allocation)), requested_company_id=7)


@pytest.mark.parametrize("move_type", ["in_invoice", "in_refund", "entry"])
def test_customer_invoice_rejects_non_outgoing_types(move_type: str) -> None:
    allocation = _allocation(
        allocation_type=BusinessContextAllocationType.CUSTOMER_RECHARGE,
        recharge_partner_id=105,
        customer_invoice_id=9001,
    )

    with pytest.raises(WorkbenchErpReferenceTypeError):
        _validator(
            partners=[PartnerReference(id=105)],
            customer_invoices=[CustomerInvoiceReference(id=9001, company_id=7, partner_id=105, move_type=move_type)],
        ).validate(_candidate(business_context_allocations=_allocation_set(allocation)), requested_company_id=7)


def test_customer_invoice_company_and_partner_relationship_are_validated() -> None:
    allocation = _allocation(
        allocation_type=BusinessContextAllocationType.CUSTOMER_RECHARGE,
        customer_id=101,
        recharge_partner_id=105,
        customer_invoice_id=9001,
    )

    with pytest.raises(WorkbenchErpReferenceCompanyMismatchError):
        _validator(
            partners=[PartnerReference(id=101), PartnerReference(id=105)],
            customer_invoices=[
                CustomerInvoiceReference(id=9001, company_id=8, partner_id=105, move_type="out_invoice")
            ],
        ).validate(
            _candidate(business_context_allocations=_allocation_set(allocation)),
            requested_company_id=7,
        )
    with pytest.raises(WorkbenchErpReferenceRelationshipError):
        _validator(
            partners=[PartnerReference(id=101), PartnerReference(id=105)],
            customer_invoices=[
                CustomerInvoiceReference(id=9001, company_id=7, partner_id=101, move_type="out_invoice")
            ],
        ).validate(
            _candidate(business_context_allocations=_allocation_set(allocation)),
            requested_company_id=7,
        )

    fallback = _allocation(customer_id=101, customer_invoice_id=9001)
    _validator(
        partners=[PartnerReference(id=101)],
        customer_invoices=[CustomerInvoiceReference(id=9001, company_id=7, partner_id=101, move_type="out_invoice")],
    ).validate(
        _candidate(business_context_allocations=_allocation_set(fallback)),
        requested_company_id=7,
    )
    with pytest.raises(WorkbenchErpReferenceRelationshipError):
        _validator(
            partners=[PartnerReference(id=101)],
            customer_invoices=[
                CustomerInvoiceReference(id=9001, company_id=7, partner_id=102, move_type="out_invoice")
            ],
        ).validate(
            _candidate(business_context_allocations=_allocation_set(fallback)),
            requested_company_id=7,
        )


def test_opportunity_existence_company_and_customer_relationship_are_validated() -> None:
    allocation = _allocation(customer_id=101, opportunity_id=601)

    _validator(
        partners=[PartnerReference(id=101)],
        opportunities=[OpportunityReference(id=601, company_id=7, partner_id=101)],
    ).validate(
        _candidate(business_context_allocations=_allocation_set(allocation)),
        requested_company_id=7,
    )
    _validator(
        partners=[PartnerReference(id=101)],
        opportunities=[OpportunityReference(id=601, company_id=7, partner_id=None)],
    ).validate(
        _candidate(business_context_allocations=_allocation_set(allocation)),
        requested_company_id=7,
    )

    with pytest.raises(WorkbenchErpReferenceNotFoundError, match="Opportunity reference is invalid."):
        _validator(partners=[PartnerReference(id=101)]).validate(
            _candidate(business_context_allocations=_allocation_set(allocation)),
            requested_company_id=7,
        )
    with pytest.raises(WorkbenchErpReferenceCompanyMismatchError):
        _validator(
            partners=[PartnerReference(id=101)],
            opportunities=[OpportunityReference(id=601, company_id=8, partner_id=101)],
        ).validate(
            _candidate(business_context_allocations=_allocation_set(allocation)),
            requested_company_id=7,
        )
    with pytest.raises(WorkbenchErpReferenceRelationshipError):
        _validator(
            partners=[PartnerReference(id=101)],
            opportunities=[OpportunityReference(id=601, company_id=7, partner_id=102)],
        ).validate(
            _candidate(business_context_allocations=_allocation_set(allocation)),
            requested_company_id=7,
        )


def test_target_company_exists_but_does_not_override_requested_company_scope() -> None:
    allocation = _allocation(target_company_id=8, sales_order_id=301)

    _validator(
        companies=[CompanyReference(id=8)],
        sales_orders=[SalesOrderReference(id=301, company_id=7)],
    ).validate(
        _candidate(business_context_allocations=_allocation_set(allocation)),
        requested_company_id=7,
    )

    with pytest.raises(WorkbenchErpReferenceNotFoundError, match="Target Company reference is invalid."):
        _validator(sales_orders=[SalesOrderReference(id=301, company_id=7)]).validate(
            _candidate(business_context_allocations=_allocation_set(allocation)),
            requested_company_id=7,
        )


def test_analytic_account_accepts_requested_or_shared_company_and_rejects_wrong_company_or_missing() -> None:
    allocation = _allocation(
        allocation_type=BusinessContextAllocationType.OPERATING_EXPENSE,
        analytic_account_id=701,
    )

    _validator(analytic_accounts=[AnalyticAccountReference(id=701, company_id=7)]).validate(
        _candidate(business_context_allocations=_allocation_set(allocation)),
        requested_company_id=7,
    )
    _validator(analytic_accounts=[AnalyticAccountReference(id=701, company_id=None)]).validate(
        _candidate(business_context_allocations=_allocation_set(allocation)),
        requested_company_id=7,
    )
    with pytest.raises(WorkbenchErpReferenceCompanyMismatchError):
        _validator(analytic_accounts=[AnalyticAccountReference(id=701, company_id=8)]).validate(
            _candidate(business_context_allocations=_allocation_set(allocation)),
            requested_company_id=7,
        )
    with pytest.raises(WorkbenchErpReferenceNotFoundError, match="Analytic Account reference is invalid."):
        _validator().validate(
            _candidate(business_context_allocations=_allocation_set(allocation)),
            requested_company_id=7,
        )


def test_project_and_subscription_references_are_unsupported_when_non_null_but_null_is_accepted() -> None:
    _validator().validate(
        _candidate(business_context_allocations=_allocation_set(_allocation())),
        requested_company_id=7,
    )
    with pytest.raises(WorkbenchErpReferenceUnsupportedError, match="Project reference validation is not supported."):
        _validator().validate(
            _candidate(
                business_context_allocations=_allocation_set(
                    _allocation(allocation_type=BusinessContextAllocationType.PROJECT_COST, project_id=801)
                )
            ),
            requested_company_id=7,
        )
    with pytest.raises(
        WorkbenchErpReferenceUnsupportedError,
        match="Subscription reference validation is not supported.",
    ):
        _validator().validate(
            _candidate(business_context_allocations=_allocation_set(_allocation(subscription_id=901))),
            requested_company_id=7,
        )


def test_allocation_type_semantic_validation_success_cases() -> None:
    allocations = (
        _allocation(
            allocation_type=BusinessContextAllocationType.SALES_ORDER_COST,
            customer_id=101,
            sales_order_id=301,
        ),
        _allocation(
            allocation_key="recharge",
            allocation_type=BusinessContextAllocationType.CUSTOMER_RECHARGE,
            recharge_partner_id=105,
        ),
        _allocation(
            allocation_key="po",
            allocation_type=BusinessContextAllocationType.EXISTING_PURCHASE_ORDER,
            purchase_order_id=501,
        ),
        _allocation(allocation_key="internal", allocation_type=BusinessContextAllocationType.INTERNAL_COST),
        _allocation(
            allocation_key="expense",
            allocation_type=BusinessContextAllocationType.OPERATING_EXPENSE,
            analytic_account_id=701,
        ),
    )

    _validator(
        partners=[PartnerReference(id=101), PartnerReference(id=105)],
        sales_orders=[SalesOrderReference(id=301, company_id=7, partner_id=101)],
        purchase_orders=[PurchaseOrderReference(id=501, company_id=7)],
        analytic_accounts=[AnalyticAccountReference(id=701, company_id=7)],
    ).validate(_candidate(business_context_allocations=_allocation_set(*allocations)), requested_company_id=7)


def test_repositories_are_called_once_with_deduplicated_ids() -> None:
    repositories = _repositories(
        partners=[PartnerReference(id=101)],
        sales_orders=[SalesOrderReference(id=301, company_id=7, partner_id=101)],
    )
    validator = WorkbenchErpReferenceValidator(**repositories)
    allocation_set = _allocation_set(
        _allocation(allocation_key="a", customer_id=101, sales_order_id=301),
        _allocation(allocation_key="b", customer_id=101, sales_order_id=301),
    )

    validator.validate(_candidate(business_context_allocations=allocation_set), requested_company_id=7)

    assert repositories["partner_repository"].calls == ((101,),)
    assert repositories["sales_order_repository"].calls == ((301,),)


def test_dismiss_without_allocations_skips_reference_reads() -> None:
    repositories = _repositories()
    candidate = _candidate(
        decision=ReviewDecisionType.DISMISS,
        selected_workflow=None,
        selected_partner_id=None,
        business_context_allocations=None,
    )

    WorkbenchErpReferenceValidator(**repositories).validate(candidate, requested_company_id=7)

    assert repositories["partner_repository"].calls == ()
    assert repositories["sales_order_repository"].calls == ()


def test_provider_failure_is_translated_safely_with_chaining() -> None:
    sensitive = RuntimeError("raw Odoo url token=secret")
    validator = _validator(partner_error=sensitive)

    with pytest.raises(WorkbenchErpReferenceValidationError) as raised:
        validator.validate(
            _candidate(business_context_allocations=_allocation_set(_allocation(customer_id=101))),
            requested_company_id=7,
        )

    assert str(raised.value) == "ERP reference validation failed."
    assert "secret" not in str(raised.value)
    assert raised.value.__cause__ is sensitive


def test_application_validator_imports_no_infrastructure_persistence_workflows_ai_or_fuzzy_matching() -> None:
    source = Path("app/application/workbench/erp_reference_validation.py").read_text(encoding="utf-8").lower()
    forbidden = (
        "app.erp",
        "app.connectors",
        "app.models",
        "app.persistence",
        "sqlalchemy",
        "fastapi",
        "httpx",
        ".create(",
        ".write(",
        ".unlink(",
        "submit_review_decision",
        "decisionengine",
        "workflowstrategy",
        "vendorbillwriter",
        "action_post",
        "fuzzy",
        "embedding",
        "ai_advisor",
        "ollama",
    )

    for token in forbidden:
        assert token not in source


def _validator(
    *,
    partners: list[PartnerReference] | None = None,
    companies: list[CompanyReference] | None = None,
    sales_orders: list[SalesOrderReference] | None = None,
    sales_order_lines: list[SalesOrderLineReference] | None = None,
    purchase_orders: list[PurchaseOrderReference] | None = None,
    customer_invoices: list[CustomerInvoiceReference] | None = None,
    opportunities: list[OpportunityReference] | None = None,
    analytic_accounts: list[AnalyticAccountReference] | None = None,
    partner_error: Exception | None = None,
) -> WorkbenchErpReferenceValidator:
    repositories = _repositories(
        partners=partners,
        companies=companies,
        sales_orders=sales_orders,
        sales_order_lines=sales_order_lines,
        purchase_orders=purchase_orders,
        customer_invoices=customer_invoices,
        opportunities=opportunities,
        analytic_accounts=analytic_accounts,
        partner_error=partner_error,
    )
    return WorkbenchErpReferenceValidator(**repositories)


def _repositories(
    *,
    partners: list[PartnerReference] | None = None,
    companies: list[CompanyReference] | None = None,
    sales_orders: list[SalesOrderReference] | None = None,
    sales_order_lines: list[SalesOrderLineReference] | None = None,
    purchase_orders: list[PurchaseOrderReference] | None = None,
    customer_invoices: list[CustomerInvoiceReference] | None = None,
    opportunities: list[OpportunityReference] | None = None,
    analytic_accounts: list[AnalyticAccountReference] | None = None,
    partner_error: Exception | None = None,
) -> dict[str, object]:
    return {
        "partner_repository": PartnerRepo(partners or [], error=partner_error),
        "company_repository": CompanyRepo(companies or []),
        "sales_order_repository": SalesOrderRepo(sales_orders or []),
        "sales_order_line_repository": None if sales_order_lines is None else SalesOrderLineRepo(sales_order_lines),
        "purchase_order_repository": PurchaseOrderRepo(purchase_orders or []),
        "customer_invoice_repository": CustomerInvoiceRepo(customer_invoices or []),
        "opportunity_repository": OpportunityRepo(opportunities or []),
        "analytic_account_repository": AnalyticAccountRepo(analytic_accounts or []),
    }


class _Repo:
    def __init__(self, records: list[object], *, error: Exception | None = None) -> None:
        self.records = records
        self.error = error
        self.calls: tuple[tuple[int, ...], ...] = ()

    def _find(self, ids: tuple[int, ...]) -> tuple[object, ...]:
        self.calls = (*self.calls, ids)
        if self.error is not None:
            raise self.error
        requested = set(ids)
        return tuple(record for record in self.records if record.id in requested)


class PartnerRepo(_Repo):
    def find_partners_by_ids(self, ids: tuple[int, ...]) -> tuple[PartnerReference, ...]:
        return self._find(ids)  # type: ignore[return-value]


class CompanyRepo(_Repo):
    def find_companies_by_ids(self, ids: tuple[int, ...]) -> tuple[CompanyReference, ...]:
        return self._find(ids)  # type: ignore[return-value]


class SalesOrderRepo(_Repo):
    def find_sales_orders_by_ids(self, ids: tuple[int, ...]) -> tuple[SalesOrderReference, ...]:
        return self._find(ids)  # type: ignore[return-value]


class SalesOrderLineRepo(_Repo):
    def find_sales_order_lines_by_ids(self, ids: tuple[int, ...]) -> tuple[SalesOrderLineReference, ...]:
        return self._find(ids)  # type: ignore[return-value]


class PurchaseOrderRepo(_Repo):
    def find_purchase_orders_by_ids(self, ids: tuple[int, ...]) -> tuple[PurchaseOrderReference, ...]:
        return self._find(ids)  # type: ignore[return-value]


class CustomerInvoiceRepo(_Repo):
    def find_customer_invoices_by_ids(self, ids: tuple[int, ...]) -> tuple[CustomerInvoiceReference, ...]:
        return self._find(ids)  # type: ignore[return-value]


class OpportunityRepo(_Repo):
    def find_opportunities_by_ids(self, ids: tuple[int, ...]) -> tuple[OpportunityReference, ...]:
        return self._find(ids)  # type: ignore[return-value]


class AnalyticAccountRepo(_Repo):
    def find_analytic_accounts_by_ids(self, ids: tuple[int, ...]) -> tuple[AnalyticAccountReference, ...]:
        return self._find(ids)  # type: ignore[return-value]


def _candidate(**overrides) -> OdooWorkbenchDecisionCandidate:
    values = {
        "odoo_record_id": 42,
        "review_id": "review-1",
        "company_id": 7,
        "expected_version": 4,
        "decision": ReviewDecisionType.SELECT_WORKFLOW,
        "idempotency_key": "odoo-key-1",
        "decided_by_odoo_user_id": 11,
        "decided_at": datetime(2026, 8, 4, 12, 0, tzinfo=UTC),
        "decision_ready": True,
        "selected_workflow": WorkflowType.VENDOR_BILL,
        "selected_partner_id": 700,
        "business_context_allocations": _allocation_set(_allocation()),
    }
    values.update(overrides)
    return OdooWorkbenchDecisionCandidate(**values)


def _allocation_set(*allocations: BusinessContextAllocation) -> BusinessContextAllocationSet:
    return BusinessContextAllocationSet(
        allocations=allocations or (_allocation(),),
        completeness=AllocationCompleteness.PARTIAL,
        invoice_total=Decimal("100.00"),
        currency="TRY",
    )


def _allocation(**overrides) -> BusinessContextAllocation:
    values = {
        "allocation_key": "allocation-1",
        "allocation_type": BusinessContextAllocationType.INTERNAL_COST,
        "amount": Decimal("10.00"),
        "currency": "TRY",
    }
    values.update(overrides)
    return BusinessContextAllocation(**values)
