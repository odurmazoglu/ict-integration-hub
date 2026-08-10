from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.application.dto import ApplicationDTO
from app.application.workbench.exceptions import WorkbenchContractError


@dataclass(frozen=True, slots=True)
class PartnerReference(ApplicationDTO):
    id: int
    company_id: int | None = None
    commercial_partner_id: int | None = None
    active: bool = True

    def __post_init__(self) -> None:
        _require_positive_int(self.id, "partner id must be positive.")
        _require_optional_positive_int(self.company_id, "partner company_id must be positive when supplied.")
        _require_optional_positive_int(
            self.commercial_partner_id,
            "partner commercial_partner_id must be positive when supplied.",
        )
        _require_bool(self.active, "partner active must be boolean.")


@dataclass(frozen=True, slots=True)
class CompanyReference(ApplicationDTO):
    id: int

    def __post_init__(self) -> None:
        _require_positive_int(self.id, "company id must be positive.")


@dataclass(frozen=True, slots=True)
class SalesOrderReference(ApplicationDTO):
    id: int
    company_id: int
    partner_id: int | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.id, "sales order id must be positive.")
        _require_positive_int(self.company_id, "sales order company_id must be positive.")
        _require_optional_positive_int(self.partner_id, "sales order partner_id must be positive when supplied.")


@dataclass(frozen=True, slots=True)
class SalesOrderLineReference(ApplicationDTO):
    id: int
    order_id: int

    def __post_init__(self) -> None:
        _require_positive_int(self.id, "sales order line id must be positive.")
        _require_positive_int(self.order_id, "sales order line order_id must be positive.")


@dataclass(frozen=True, slots=True)
class PurchaseOrderReference(ApplicationDTO):
    id: int
    company_id: int
    partner_id: int | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.id, "purchase order id must be positive.")
        _require_positive_int(self.company_id, "purchase order company_id must be positive.")
        _require_optional_positive_int(self.partner_id, "purchase order partner_id must be positive when supplied.")


@dataclass(frozen=True, slots=True)
class CustomerInvoiceReference(ApplicationDTO):
    id: int
    company_id: int
    partner_id: int | None
    move_type: str

    def __post_init__(self) -> None:
        _require_positive_int(self.id, "customer invoice id must be positive.")
        _require_positive_int(self.company_id, "customer invoice company_id must be positive.")
        _require_optional_positive_int(self.partner_id, "customer invoice partner_id must be positive when supplied.")
        if not isinstance(self.move_type, str) or not self.move_type.strip():
            raise WorkbenchContractError("customer invoice move_type is required.")


@dataclass(frozen=True, slots=True)
class OpportunityReference(ApplicationDTO):
    id: int
    company_id: int | None = None
    partner_id: int | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.id, "opportunity id must be positive.")
        _require_optional_positive_int(self.company_id, "opportunity company_id must be positive when supplied.")
        _require_optional_positive_int(self.partner_id, "opportunity partner_id must be positive when supplied.")


@dataclass(frozen=True, slots=True)
class AnalyticAccountReference(ApplicationDTO):
    id: int
    company_id: int | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.id, "analytic account id must be positive.")
        _require_optional_positive_int(self.company_id, "analytic account company_id must be positive when supplied.")


class PartnerReferenceRepository(Protocol):
    def find_partners_by_ids(self, ids: tuple[int, ...]) -> tuple[PartnerReference, ...]:
        pass


class CompanyReferenceRepository(Protocol):
    def find_companies_by_ids(self, ids: tuple[int, ...]) -> tuple[CompanyReference, ...]:
        pass


class SalesOrderReferenceRepository(Protocol):
    def find_sales_orders_by_ids(self, ids: tuple[int, ...]) -> tuple[SalesOrderReference, ...]:
        pass


class SalesOrderLineReferenceRepository(Protocol):
    def find_sales_order_lines_by_ids(self, ids: tuple[int, ...]) -> tuple[SalesOrderLineReference, ...]:
        pass


class PurchaseOrderReferenceRepository(Protocol):
    def find_purchase_orders_by_ids(self, ids: tuple[int, ...]) -> tuple[PurchaseOrderReference, ...]:
        pass


class CustomerInvoiceReferenceRepository(Protocol):
    def find_customer_invoices_by_ids(self, ids: tuple[int, ...]) -> tuple[CustomerInvoiceReference, ...]:
        pass


class OpportunityReferenceRepository(Protocol):
    def find_opportunities_by_ids(self, ids: tuple[int, ...]) -> tuple[OpportunityReference, ...]:
        pass


class AnalyticAccountReferenceRepository(Protocol):
    def find_analytic_accounts_by_ids(self, ids: tuple[int, ...]) -> tuple[AnalyticAccountReference, ...]:
        pass


def _require_positive_int(value: int, message: str) -> None:
    if type(value) is not int or value <= 0:
        raise WorkbenchContractError(message)


def _require_optional_positive_int(value: int | None, message: str) -> None:
    if value is not None:
        _require_positive_int(value, message)


def _require_bool(value: bool, message: str) -> None:
    if type(value) is not bool:
        raise WorkbenchContractError(message)
