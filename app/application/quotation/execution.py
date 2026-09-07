from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from app.application.dto import ApplicationDTO
from app.application.quotation.contracts import QuotationScenarioSnapshot
from app.application.quotation.identity import customer_quotation_execution_key
from app.application.workbench.exceptions import WorkbenchContractError


@dataclass(frozen=True, slots=True)
class CustomerQuotationLine(ApplicationDTO):
    """Immutable ERP-independent line for one draft customer Sales Quotation."""

    line_id: str
    product_variant_id: int
    quantity: Decimal
    sales_unit_price: Decimal
    description: str | None = None
    uom_id: int | None = None

    def __post_init__(self) -> None:
        _require_text(self.line_id, "line_id is required.")
        _require_positive_int(self.product_variant_id, "product_variant_id must be positive.")
        _require_positive_decimal(self.quantity, "quantity must be greater than zero.")
        _require_nonnegative_decimal(self.sales_unit_price, "sales_unit_price must not be negative.")
        if self.description is not None and not isinstance(self.description, str):
            raise WorkbenchContractError("description must be a string when supplied.")
        if self.uom_id is not None:
            _require_positive_int(self.uom_id, "uom_id must be positive when supplied.")


@dataclass(frozen=True, slots=True)
class CustomerQuotationDraft(ApplicationDTO):
    """Everything required to create exactly one draft ``sale.order`` from one scenario.

    Built only from an immutable, persisted :class:`QuotationScenarioSnapshot`;
    it never carries mutable Odoo Proposal Scenario authoring state.
    """

    company_id: int
    customer_id: int
    currency: str
    scenario_id: str
    scenario_name: str
    review_id: str
    decision_id: str
    decision_version: int
    lines: tuple[CustomerQuotationLine, ...]
    opportunity_id: int | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.company_id, "company_id must be positive.")
        _require_positive_int(self.customer_id, "customer_id must be positive.")
        _require_currency(self.currency)
        _require_text(self.scenario_id, "scenario_id is required.")
        _require_text(self.scenario_name, "scenario_name is required.")
        _require_text(self.review_id, "review_id is required.")
        _require_text(self.decision_id, "decision_id is required.")
        _require_positive_int(self.decision_version, "decision_version must be positive.")
        if self.opportunity_id is not None:
            _require_positive_int(self.opportunity_id, "opportunity_id must be positive when supplied.")
        lines = tuple(self.lines)
        if not lines:
            raise WorkbenchContractError("customer quotation requires at least one line.")
        if any(not isinstance(line, CustomerQuotationLine) for line in lines):
            raise WorkbenchContractError("customer quotation lines must be canonical.")
        if len({line.line_id for line in lines}) != len(lines):
            raise WorkbenchContractError("customer quotation line_id values must be unique.")
        object.__setattr__(self, "lines", lines)
        object.__setattr__(self, "currency", self.currency.strip().upper())

    @classmethod
    def from_snapshot(cls, snapshot: QuotationScenarioSnapshot) -> CustomerQuotationDraft:
        if not isinstance(snapshot, QuotationScenarioSnapshot):
            raise WorkbenchContractError("a canonical QuotationScenarioSnapshot is required.")
        return cls(
            company_id=snapshot.company_id,
            customer_id=snapshot.customer_id,
            currency=snapshot.currency,
            scenario_id=snapshot.scenario_id,
            scenario_name=snapshot.scenario_name,
            review_id=snapshot.review_id,
            decision_id=snapshot.decision_id,
            decision_version=snapshot.decision_version,
            opportunity_id=snapshot.opportunity_id,
            lines=tuple(
                CustomerQuotationLine(
                    line_id=line.line_id,
                    product_variant_id=line.product_variant_id,
                    quantity=line.quantity,
                    sales_unit_price=line.sales_unit_price,
                    description=line.description,
                    uom_id=line.uom_id,
                )
                for line in snapshot.lines
            ),
        )

    @property
    def execution_key(self) -> str:
        return customer_quotation_execution_key(
            company_id=self.company_id,
            review_id=self.review_id,
            decision_id=self.decision_id,
            decision_version=self.decision_version,
            scenario_id=self.scenario_id,
        )


@dataclass(frozen=True, slots=True)
class CreateCustomerQuotationCommand(ApplicationDTO):
    """Command to create one idempotent draft customer Sales Quotation."""

    draft: CustomerQuotationDraft
    approved_by: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.draft, CustomerQuotationDraft):
            raise WorkbenchContractError("draft must be a canonical CustomerQuotationDraft.")
        if self.approved_by is not None and not (isinstance(self.approved_by, str) and self.approved_by.strip()):
            raise WorkbenchContractError("approved_by must be a non-empty string when supplied.")

    @property
    def execution_key(self) -> str:
        return self.draft.execution_key


@dataclass(frozen=True, slots=True)
class CustomerQuotationCreationResult(ApplicationDTO):
    """Immutable ERP-independent outcome of one customer quotation creation."""

    external_quotation_id: int
    execution_key: str
    created: bool
    external_reference: str | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.external_quotation_id, "external_quotation_id must be positive.")
        _require_text(self.execution_key, "execution_key is required.")
        if type(self.created) is not bool:
            raise WorkbenchContractError("created must be a boolean.")
        if self.external_reference is not None and not isinstance(self.external_reference, str):
            raise WorkbenchContractError("external_reference must be a string when supplied.")


class CustomerQuotationWriter(Protocol):
    """Application-facing port for idempotent draft customer Sales Quotation creation.

    Implementations must resolve idempotency by ``(company_id, execution_key)``:
    zero matches create exactly one draft, one match returns the existing
    quotation unchanged, more than one match fails closed. Replay never mutates
    an existing quotation's commercial contents.
    """

    async def create_quotation(
        self,
        command: CreateCustomerQuotationCommand,
    ) -> CustomerQuotationCreationResult:
        pass


class CustomerQuotationPricelistResolver(Protocol):
    """Read-only port resolving one existing Odoo pricelist for company + currency.

    Fails closed when there is no matching pricelist or more than one. It never
    creates a pricelist and never mutates currency configuration.
    """

    async def resolve_pricelist_id(self, *, company_id: int, currency: str) -> int:
        pass


def _require_text(value: object, message: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise WorkbenchContractError(message)


def _require_positive_int(value: object, message: str) -> None:
    if type(value) is not int or value <= 0:
        raise WorkbenchContractError(message)


def _require_currency(value: object) -> None:
    if not isinstance(value, str) or len(value.strip()) != 3 or not value.strip().isalpha():
        raise WorkbenchContractError("currency must be a three-letter code.")


def _require_decimal(value: object, message: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise WorkbenchContractError(message)
    return value


def _require_positive_decimal(value: object, message: str) -> None:
    if _require_decimal(value, message) <= Decimal("0"):
        raise WorkbenchContractError(message)


def _require_nonnegative_decimal(value: object, message: str) -> None:
    if _require_decimal(value, message) < Decimal("0"):
        raise WorkbenchContractError(message)
