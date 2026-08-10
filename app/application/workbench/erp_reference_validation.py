from __future__ import annotations

from collections.abc import Callable

from app.application.exceptions import ApplicationError
from app.application.workbench.allocations import BusinessContextAllocation, BusinessContextAllocationSet
from app.application.workbench.dto import ReviewDecisionType
from app.application.workbench.erp_references import (
    AnalyticAccountReference,
    AnalyticAccountReferenceRepository,
    CompanyReference,
    CompanyReferenceRepository,
    CustomerInvoiceReference,
    CustomerInvoiceReferenceRepository,
    OpportunityReference,
    OpportunityReferenceRepository,
    PartnerReference,
    PartnerReferenceRepository,
    PurchaseOrderReference,
    PurchaseOrderReferenceRepository,
    SalesOrderLineReference,
    SalesOrderLineReferenceRepository,
    SalesOrderReference,
    SalesOrderReferenceRepository,
)
from app.application.workbench.exceptions import (
    WorkbenchContractError,
    WorkbenchErpReferenceCompanyMismatchError,
    WorkbenchErpReferenceNotFoundError,
    WorkbenchErpReferenceRelationshipError,
    WorkbenchErpReferenceTypeError,
    WorkbenchErpReferenceUnsupportedError,
    WorkbenchErpReferenceValidationError,
)
from app.application.workbench.projection import OdooWorkbenchDecisionCandidate

OUTGOING_CUSTOMER_INVOICE_MOVE_TYPES = frozenset({"out_invoice", "out_refund"})
SAFE_VALIDATION_FAILURE = "ERP reference validation failed."


class WorkbenchErpReferenceValidator:
    """Read-only semantic validator for Workbench allocation ERP references."""

    def __init__(
        self,
        *,
        partner_repository: PartnerReferenceRepository,
        company_repository: CompanyReferenceRepository,
        sales_order_repository: SalesOrderReferenceRepository,
        purchase_order_repository: PurchaseOrderReferenceRepository,
        customer_invoice_repository: CustomerInvoiceReferenceRepository,
        opportunity_repository: OpportunityReferenceRepository,
        analytic_account_repository: AnalyticAccountReferenceRepository,
        sales_order_line_repository: SalesOrderLineReferenceRepository | None = None,
    ) -> None:
        self._partner_repository = partner_repository
        self._company_repository = company_repository
        self._sales_order_repository = sales_order_repository
        self._sales_order_line_repository = sales_order_line_repository
        self._purchase_order_repository = purchase_order_repository
        self._customer_invoice_repository = customer_invoice_repository
        self._opportunity_repository = opportunity_repository
        self._analytic_account_repository = analytic_account_repository

    def validate(
        self,
        candidate: OdooWorkbenchDecisionCandidate,
        *,
        requested_company_id: int,
    ) -> OdooWorkbenchDecisionCandidate:
        if not isinstance(candidate, OdooWorkbenchDecisionCandidate):
            raise WorkbenchContractError("OdooWorkbenchDecisionCandidate is required.")
        _require_positive_int(requested_company_id, "requested company_id must be positive.")
        if candidate.decision is ReviewDecisionType.DISMISS and candidate.business_context_allocations is None:
            return candidate
        allocations = candidate.business_context_allocations
        if allocations is None:
            return candidate

        references = _translate_validation_failure(lambda: self._read_references(allocations))
        for allocation in allocations.allocations:
            self._validate_allocation(allocation, requested_company_id=requested_company_id, references=references)
        return candidate

    def _read_references(self, allocations: BusinessContextAllocationSet) -> _ReferenceMaps:
        partner_ids = _unique_ids(allocations, "customer_id", "recharge_partner_id")
        company_ids = _unique_ids(allocations, "target_company_id")
        sales_order_ids = _unique_ids(allocations, "sales_order_id")
        sales_order_line_ids = _unique_ids(allocations, "sales_order_line_id")
        purchase_order_ids = _unique_ids(allocations, "purchase_order_id")
        customer_invoice_ids = _unique_ids(allocations, "customer_invoice_id")
        opportunity_ids = _unique_ids(allocations, "opportunity_id")
        analytic_account_ids = _unique_ids(allocations, "analytic_account_id")

        return _ReferenceMaps(
            partners=_by_id(self._partner_repository.find_partners_by_ids(partner_ids)),
            companies=_by_id(self._company_repository.find_companies_by_ids(company_ids)),
            sales_orders=_by_id(self._sales_order_repository.find_sales_orders_by_ids(sales_order_ids)),
            sales_order_lines=_by_id(
                self._sales_order_line_repository.find_sales_order_lines_by_ids(sales_order_line_ids)
                if self._sales_order_line_repository is not None
                else ()
            ),
            sales_order_lines_supported=self._sales_order_line_repository is not None,
            purchase_orders=_by_id(self._purchase_order_repository.find_purchase_orders_by_ids(purchase_order_ids)),
            customer_invoices=_by_id(
                self._customer_invoice_repository.find_customer_invoices_by_ids(customer_invoice_ids)
            ),
            opportunities=_by_id(self._opportunity_repository.find_opportunities_by_ids(opportunity_ids)),
            analytic_accounts=_by_id(
                self._analytic_account_repository.find_analytic_accounts_by_ids(analytic_account_ids)
            ),
        )

    def _validate_allocation(
        self,
        allocation: BusinessContextAllocation,
        *,
        requested_company_id: int,
        references: _ReferenceMaps,
    ) -> None:
        customer = _optional_reference(
            allocation.customer_id,
            references.partners,
            "Customer reference is invalid.",
        )
        recharge_partner = _optional_reference(
            allocation.recharge_partner_id,
            references.partners,
            "Recharge Partner reference is invalid.",
        )
        _validate_partner_company(customer, requested_company_id=requested_company_id)
        _validate_partner_company(recharge_partner, requested_company_id=requested_company_id)

        _optional_reference(allocation.target_company_id, references.companies, "Target Company reference is invalid.")

        opportunity = _optional_reference(
            allocation.opportunity_id,
            references.opportunities,
            "Opportunity reference is invalid.",
        )
        if opportunity is not None:
            _validate_optional_company(
                opportunity.company_id,
                requested_company_id=requested_company_id,
                message="ERP reference company scope mismatch.",
            )
            if customer is not None and opportunity.partner_id is not None and opportunity.partner_id != customer.id:
                raise WorkbenchErpReferenceRelationshipError("Opportunity customer does not match allocation customer.")

        sales_order = _optional_reference(
            allocation.sales_order_id,
            references.sales_orders,
            "Sales Order reference is invalid.",
        )
        if sales_order is not None:
            _validate_required_company(sales_order.company_id, requested_company_id=requested_company_id)
            if customer is not None and sales_order.partner_id is not None and sales_order.partner_id != customer.id:
                raise WorkbenchErpReferenceRelationshipError("Sales Order customer does not match allocation customer.")

        sales_order_line = (
            _optional_reference(
                allocation.sales_order_line_id,
                references.sales_order_lines,
                "Sales Order Line reference is invalid.",
            )
            if references.sales_order_lines_supported
            else None
        )
        if allocation.sales_order_line_id is not None and not references.sales_order_lines_supported:
            raise WorkbenchErpReferenceUnsupportedError("Sales Order Line reference validation is not supported.")
        if sales_order_line is not None and sales_order is not None and sales_order_line.order_id != sales_order.id:
            raise WorkbenchErpReferenceRelationshipError("Sales Order Line does not match Sales Order.")

        purchase_order = _optional_reference(
            allocation.purchase_order_id,
            references.purchase_orders,
            "Purchase Order reference is invalid.",
        )
        if purchase_order is not None:
            _validate_required_company(purchase_order.company_id, requested_company_id=requested_company_id)

        customer_invoice = _optional_reference(
            allocation.customer_invoice_id,
            references.customer_invoices,
            "Customer Invoice reference is invalid.",
        )
        if customer_invoice is not None:
            if customer_invoice.move_type not in OUTGOING_CUSTOMER_INVOICE_MOVE_TYPES:
                raise WorkbenchErpReferenceTypeError("Customer Invoice reference is not an outgoing invoice.")
            _validate_required_company(customer_invoice.company_id, requested_company_id=requested_company_id)
            expected_partner_id = allocation.recharge_partner_id or allocation.customer_id
            if (
                expected_partner_id is not None
                and customer_invoice.partner_id is not None
                and customer_invoice.partner_id != expected_partner_id
            ):
                raise WorkbenchErpReferenceRelationshipError(
                    "Customer Invoice partner does not match allocation partner."
                )

        if allocation.project_id is not None:
            raise WorkbenchErpReferenceUnsupportedError("Project reference validation is not supported.")

        analytic_account = _optional_reference(
            allocation.analytic_account_id,
            references.analytic_accounts,
            "Analytic Account reference is invalid.",
        )
        if analytic_account is not None:
            _validate_optional_company(
                analytic_account.company_id,
                requested_company_id=requested_company_id,
                message="ERP reference company scope mismatch.",
            )

        if allocation.subscription_id is not None:
            raise WorkbenchErpReferenceUnsupportedError("Subscription reference validation is not supported.")


class _ReferenceMaps:
    def __init__(
        self,
        *,
        partners: dict[int, PartnerReference],
        companies: dict[int, CompanyReference],
        sales_orders: dict[int, SalesOrderReference],
        sales_order_lines: dict[int, SalesOrderLineReference],
        sales_order_lines_supported: bool,
        purchase_orders: dict[int, PurchaseOrderReference],
        customer_invoices: dict[int, CustomerInvoiceReference],
        opportunities: dict[int, OpportunityReference],
        analytic_accounts: dict[int, AnalyticAccountReference],
    ) -> None:
        self.partners = partners
        self.companies = companies
        self.sales_orders = sales_orders
        self.sales_order_lines = sales_order_lines
        self.sales_order_lines_supported = sales_order_lines_supported
        self.purchase_orders = purchase_orders
        self.customer_invoices = customer_invoices
        self.opportunities = opportunities
        self.analytic_accounts = analytic_accounts


def _unique_ids(allocations: BusinessContextAllocationSet, *field_names: str) -> tuple[int, ...]:
    ids: set[int] = set()
    for allocation in allocations.allocations:
        for field_name in field_names:
            value = getattr(allocation, field_name)
            if value is not None:
                ids.add(value)
    return tuple(sorted(ids))


def _by_id[ReferenceT](references: tuple[ReferenceT, ...]) -> dict[int, ReferenceT]:
    return {reference.id: reference for reference in references}


def _optional_reference[ReferenceT](
    reference_id: int | None,
    references: dict[int, ReferenceT],
    message: str,
) -> ReferenceT | None:
    if reference_id is None:
        return None
    reference = references.get(reference_id)
    if reference is None:
        raise WorkbenchErpReferenceNotFoundError(message)
    return reference


def _validate_partner_company(reference: PartnerReference | None, *, requested_company_id: int) -> None:
    if reference is None:
        return
    _validate_optional_company(
        reference.company_id,
        requested_company_id=requested_company_id,
        message="ERP reference company scope mismatch.",
    )


def _validate_optional_company(company_id: int | None, *, requested_company_id: int, message: str) -> None:
    if company_id is None:
        return
    _validate_required_company(company_id, requested_company_id=requested_company_id, message=message)


def _validate_required_company(
    company_id: int,
    *,
    requested_company_id: int,
    message: str = "ERP reference company scope mismatch.",
) -> None:
    if company_id != requested_company_id:
        raise WorkbenchErpReferenceCompanyMismatchError(message)


def _translate_validation_failure[ResultT](operation: Callable[[], ResultT]) -> ResultT:
    try:
        return operation()
    except ApplicationError:
        raise
    except Exception as exc:
        raise WorkbenchErpReferenceValidationError(SAFE_VALIDATION_FAILURE) from exc


def _require_positive_int(value: int, message: str) -> None:
    if type(value) is not int or value <= 0:
        raise WorkbenchContractError(message)
