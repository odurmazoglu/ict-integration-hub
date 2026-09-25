from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

# P0-PROD-18F-2: the one sanctioned way a product-backed line may also carry an
# explicit account. Only ``issue_validated_resale_line_account`` can produce a
# ``ValidatedResaleLineAccount`` (it holds the module-private seal), and only the
# RESALE execution validator
# (``app.application.execution.resale_execution_accounting``) calls it -- after the
# immutable RESALE pin passed execution-time drift and fiscal-position checks. An
# architecture test pins that single call site.
_RESALE_LINE_ACCOUNT_SEAL = object()


@dataclass(frozen=True, slots=True)
class ValidatedResaleLineAccount:
    """Proof that one RESALE line's explicit ``account_id`` is its immutable pinned account.

    ``account_id`` is copied verbatim from the decision's RESALE accounting pin
    (P0-PROD-18F-1); it is never taken from a request, an operator, or a fresh Odoo
    read. A directly constructed (or ``dataclasses.replace``-d) instance is unsealed and
    ``VendorBillLine`` rejects it -- see ``issue_validated_resale_line_account``.
    """

    line_number: str
    product_id: int
    account_id: int
    _seal: object = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        _require_text(self.line_number, "line_number is required.")
        _require_positive_int(self.product_id, "product_id must be a positive ERP id.")
        _require_positive_int(self.account_id, "account_id must be a positive ERP account id.")

    @property
    def sealed(self) -> bool:
        return self._seal is _RESALE_LINE_ACCOUNT_SEAL


def issue_validated_resale_line_account(
    *, line_number: str, product_id: int, account_id: int
) -> ValidatedResaleLineAccount:
    """Internal to the RESALE execution validator -- see ``_RESALE_LINE_ACCOUNT_SEAL``."""

    issued = ValidatedResaleLineAccount(line_number=line_number, product_id=product_id, account_id=account_id)
    object.__setattr__(issued, "_seal", _RESALE_LINE_ACCOUNT_SEAL)
    return issued


@dataclass(frozen=True, slots=True)
class VendorBillLine:
    """One draft Vendor Bill line.

    Exactly one of three shapes:

    * product line -- a matched ``product_id``; Odoo derives the expense account;
    * account-only line -- a pinned ``account_id`` for a deterministic operating
      expense line with no product;
    * RESALE pinned product line (P0-PROD-18F-2) -- ``product_id`` *and* ``account_id``,
      allowed only together with a ``resale_account`` proving the account is the
      decision's immutable, execution-time-validated RESALE pin for this product.

    ``product_id`` and ``account_id`` without ``resale_account`` stays rejected, so no
    caller can attach an arbitrary account to a product-backed line.

    Carries no UoM of any kind (P0-PROD-10E): the source invoice's raw UN/CEFACT
    unit code is immutable source evidence, never an Odoo id, and must never be
    stored here or written to Odoo. The real Odoo ``product_uom_id`` is resolved
    read-only from ``product_id`` itself at payload-construction time (see
    ``to_odoo_account_move_payload``'s ``product_uom_ids`` parameter) -- never
    carried on this DTO, exactly like ``currency_id`` is resolved and passed in
    separately rather than stored per line.
    """

    product_id: int | None
    quantity: Decimal
    unit_price: Decimal
    tax_ids: tuple[int, ...] = field(default_factory=tuple)
    description: str | None = None
    account_id: int | None = None
    resale_account: ValidatedResaleLineAccount | None = None

    def __post_init__(self) -> None:
        if self.resale_account is not None:
            if not isinstance(self.resale_account, ValidatedResaleLineAccount) or not self.resale_account.sealed:
                raise ValueError("resale_account must be issued by RESALE execution validation.")
            _require_positive_int(self.product_id, "product_id must be a positive ERP id.")
            _require_positive_int(self.account_id, "account_id must be a positive ERP account id.")
            if (self.product_id, self.account_id) != (
                self.resale_account.product_id,
                self.resale_account.account_id,
            ):
                raise ValueError("A RESALE line's product and account must be exactly its validated pin.")
            return
        product_set = self.product_id is not None
        account_set = self.account_id is not None
        if product_set == account_set:
            raise ValueError("VendorBillLine requires exactly one of product_id or account_id.")
        if product_set:
            _require_positive_int(self.product_id, "product_id must be a positive ERP id.")
        else:
            _require_positive_int(self.account_id, "account_id must be a positive ERP account id.")


@dataclass(frozen=True, slots=True)
class VendorBill:
    supplier_id: int
    invoice_number: str
    invoice_date: date
    currency: str
    external_uuid: str | None
    reference: str | None
    company_id: int | None = None
    invoice_lines: tuple[VendorBillLine, ...] = field(default_factory=tuple)
    notes: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class CustomerInvoiceLine:
    product_id: int
    quantity: Decimal
    unit_price: Decimal
    tax_ids: tuple[int, ...] = field(default_factory=tuple)
    description: str | None = None
    source_allocation_key: str | None = None


@dataclass(frozen=True, slots=True)
class CustomerInvoiceBillingLine:
    allocation_key: str
    product_id: int
    description: str
    quantity: Decimal
    unit_price: Decimal
    sales_tax_ids: tuple[int, ...]

    def __post_init__(self) -> None:
        _require_text(self.allocation_key, "allocation_key is required.")
        _require_positive_int(self.product_id, "product_id must be a positive ERP id.")
        _require_text(self.description, "description is required.")
        _require_positive_decimal(self.quantity, "quantity must be a positive Decimal value.")
        _require_positive_decimal(self.unit_price, "unit_price must be a positive Decimal value.")
        sales_tax_ids = tuple(self.sales_tax_ids)
        if not sales_tax_ids:
            raise ValueError("sales_tax_ids are required.")
        if len(set(sales_tax_ids)) != len(sales_tax_ids):
            raise ValueError("sales_tax_ids must be unique per billing line.")
        for sales_tax_id in sales_tax_ids:
            _require_positive_int(sales_tax_id, "sales_tax_ids must contain positive ERP ids.")
        object.__setattr__(self, "sales_tax_ids", sales_tax_ids)


@dataclass(frozen=True, slots=True)
class CustomerInvoiceBillingInstruction:
    billing_key: str
    customer_id: int
    currency: str
    lines: tuple[CustomerInvoiceBillingLine, ...]

    def __post_init__(self) -> None:
        _require_text(self.billing_key, "billing_key is required.")
        _require_positive_int(self.customer_id, "customer_id must be a positive ERP id.")
        _require_text(self.currency, "currency is required.")
        currency = self.currency.strip().upper()
        if len(currency) != 3 or not currency.isalpha():
            raise ValueError("currency must be a stable ISO-4217 code.")
        lines = tuple(self.lines)
        if not lines:
            raise ValueError("Customer Invoice billing instruction requires at least one line.")
        for line in lines:
            if not isinstance(line, CustomerInvoiceBillingLine):
                raise ValueError("lines must contain canonical CustomerInvoiceBillingLine values.")
        allocation_keys = tuple(line.allocation_key for line in lines)
        if len(set(allocation_keys)) != len(allocation_keys):
            raise ValueError("billing instruction allocation keys must be unique.")
        object.__setattr__(self, "currency", currency)
        object.__setattr__(self, "lines", lines)


@dataclass(frozen=True, slots=True)
class CustomerInvoice:
    company_id: int
    customer_id: int
    invoice_date: date
    currency: str
    external_uuid: str | None
    reference: str
    invoice_lines: tuple[CustomerInvoiceLine, ...] = field(default_factory=tuple)
    notes: tuple[str, ...] = field(default_factory=tuple)


def _require_text(value: str | None, message: str) -> None:
    if value is None or not isinstance(value, str) or not value.strip():
        raise ValueError(message)


def _require_positive_int(value: int, message: str) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(message)


def _require_positive_decimal(value: Decimal, message: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= Decimal("0"):
        raise ValueError(message)
