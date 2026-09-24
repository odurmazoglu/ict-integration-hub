from __future__ import annotations

from collections.abc import Mapping
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Any

from app.billing.dto import (
    CustomerInvoice,
    CustomerInvoiceBillingInstruction,
    CustomerInvoiceLine,
    VendorBill,
    VendorBillLine,
)
from app.billing.exceptions import CustomerInvoiceBuildError, VendorBillBuildError
from app.billing.validation import VendorBillValidationResult, validation_result
from app.domain.invoice import InternalInvoice, InvoiceLine
from app.matching import InvoiceProductMatchResult, PartnerMatchResult, PartnerMatchStatus, ProductMatchStatus
from app.tax_mapping import InvoiceTaxMappingResult, TaxMatchStatus

if TYPE_CHECKING:
    from app.application.expense_mapping import OperatingExpenseMatchResult

# ``app.application.expense_mapping`` sits above ``app.billing`` in the import graph, so the
# operating-expense classification symbols are imported lazily inside the functions that use them
# to avoid an import cycle (app.application.decision -> app.billing -> app.application...).


class VendorBillBuilder:
    def build(
        self,
        invoice: InternalInvoice,
        partner_match: PartnerMatchResult,
        product_match: InvoiceProductMatchResult,
        tax_match: InvoiceTaxMappingResult,
        *,
        company_id: int | None = None,
        operating_expense_match: OperatingExpenseMatchResult | None = None,
        account_only_line_numbers: frozenset[str] = frozenset(),
        account_only_expense_match: OperatingExpenseMatchResult | None = None,
        explicit_account_only_accounts: dict[str, int] | None = None,
    ) -> VendorBill:
        """Build a deterministic Vendor Bill.

        ``account_only_line_numbers``/``account_only_expense_match`` carry an explicit
        human per-line account-only decision (``LineResolution.account_only``) for lines
        that otherwise have no matched Odoo product. They apply per line and are
        independent of the whole-invoice ``operating_expense_match`` path, which is
        unchanged and still takes priority when it applies.

        ``explicit_account_only_accounts`` (P0-PROD-08G) is an optional
        ``{line_number: expense_account_id}`` map of operator-confirmed, per-line
        explicit accounts (``LineResolution.expense_account_id``), pinned once at
        decision-acceptance time -- never re-queried here. Per line, it takes
        precedence over ``account_only_expense_match``, which remains the fallback
        for account-only lines with no explicit account of their own (the legacy
        whole-vendor ``OperatingExpenseMappingRecord`` flow, unchanged). Omitting it
        entirely reproduces the pre-08G behavior exactly.
        """

        validation = validate_vendor_bill_inputs(
            invoice,
            partner_match,
            product_match,
            tax_match,
            company_id=company_id,
            operating_expense_match=operating_expense_match,
            account_only_line_numbers=account_only_line_numbers,
            account_only_expense_match=account_only_expense_match,
            explicit_account_only_accounts=explicit_account_only_accounts,
        )
        if not validation.is_valid:
            raise VendorBillBuildError(validation.errors)

        assert partner_match.partner_id is not None
        tax_ids_by_line = _tax_ids_by_line(tax_match)
        expense_account_id = (
            operating_expense_match.expense_account_id
            if _operating_expense_mode(invoice, operating_expense_match)
            else None
        )
        if expense_account_id is not None:
            invoice_lines = tuple(
                _expense_vendor_bill_line(line, expense_account_id, tax_ids_by_line) for line in invoice.lines
            )
        else:
            product_by_line = _product_results_by_line(product_match)
            resolved_account_only_account_ids = _resolved_account_only_account_ids(
                account_only_line_numbers, account_only_expense_match, explicit_account_only_accounts
            )
            invoice_lines = tuple(
                _expense_vendor_bill_line(line, resolved_account_only_account_ids[line.line_number], tax_ids_by_line)
                if line.line_number in resolved_account_only_account_ids
                else _vendor_bill_line(line, product_by_line[line.line_number], tax_ids_by_line)
                for line in invoice.lines
            )
        return VendorBill(
            supplier_id=partner_match.partner_id,
            invoice_number=invoice.header.invoice_number.strip(),
            invoice_date=invoice.header.issue_date,
            currency=invoice.header.currency_code.strip(),
            external_uuid=invoice.header.invoice_uuid.strip() or invoice.header.ettn,
            reference=invoice.header.invoice_number.strip(),
            company_id=company_id,
            invoice_lines=invoice_lines,
            notes=tuple(note.strip() for note in invoice.header.notes if note and note.strip()),
        )


class CustomerInvoiceBuilder:
    def build(
        self,
        *,
        company_id: int,
        source_invoice_id: str,
        invoice: InternalInvoice,
        billing_instruction: CustomerInvoiceBillingInstruction,
    ) -> CustomerInvoice:
        validation = validate_customer_invoice_inputs(
            company_id=company_id,
            source_invoice_id=source_invoice_id,
            invoice=invoice,
            billing_instruction=billing_instruction,
        )
        if not validation.is_valid:
            raise CustomerInvoiceBuildError(validation.errors)

        assert invoice.header.issue_date is not None
        invoice_lines = tuple(
            CustomerInvoiceLine(
                product_id=line.product_id,
                quantity=line.quantity,
                unit_price=line.unit_price,
                tax_ids=line.sales_tax_ids,
                description=line.description,
                source_allocation_key=line.allocation_key,
            )
            for line in billing_instruction.lines
        )
        return CustomerInvoice(
            company_id=company_id,
            customer_id=billing_instruction.customer_id,
            invoice_date=invoice.header.issue_date,
            currency=billing_instruction.currency,
            external_uuid=invoice.header.invoice_uuid.strip() or invoice.header.ettn,
            reference=_customer_invoice_reference(
                source_invoice_id=source_invoice_id,
                billing_instruction=billing_instruction,
            ),
            invoice_lines=invoice_lines,
            notes=(f"Source invoice: {source_invoice_id}",),
        )


def validate_customer_invoice_inputs(
    *,
    company_id: object,
    source_invoice_id: object,
    invoice: object,
    billing_instruction: object,
) -> VendorBillValidationResult:
    errors: list[str] = []
    if type(company_id) is not int or company_id <= 0:
        errors.append("company_id must be a positive integer.")
    if not isinstance(source_invoice_id, str) or not source_invoice_id.strip():
        errors.append("source_invoice_id is required.")
    if not isinstance(invoice, InternalInvoice):
        return validation_result(errors + ["InternalInvoice DTO is required."])
    if not isinstance(billing_instruction, CustomerInvoiceBillingInstruction):
        return validation_result(errors + ["Customer Invoice billing instruction is required."])
    if invoice.header.issue_date is None:
        errors.append("Invoice date is required.")
    return validation_result(errors)


def validate_vendor_bill_inputs(
    invoice: object,
    partner_match: object,
    product_match: object,
    tax_match: object,
    *,
    company_id: int | None = None,
    operating_expense_match: object | None = None,
    account_only_line_numbers: frozenset[str] = frozenset(),
    account_only_expense_match: object | None = None,
    explicit_account_only_accounts: dict[str, int] | None = None,
) -> VendorBillValidationResult:
    """Validate deterministic Vendor Bill inputs.

    ``account_only_line_numbers`` names invoice lines carrying an explicit human
    ``LineResolution.account_only`` decision (see ``app.application.workbench.dto``):
    those specific lines are exempted from the normal per-line product-match
    requirement and instead require a deterministic expense account to be
    resolvable for every one of them -- per line, either an explicit
    ``LineResolution.expense_account_id`` (``explicit_account_only_accounts``,
    P0-PROD-08G, takes precedence) or the legacy whole-vendor
    ``account_only_expense_match`` fallback (via ``_resolved_account_only_account_ids``).
    This is independent of, and does not relax, the existing whole-invoice
    ``operating_expense_match``/``expense_mode`` path below, which still requires every
    line to be free of product identifiers.
    """

    errors: list[str] = []
    if company_id is not None and (type(company_id) is not int or company_id <= 0):
        errors.append("company_id must be a positive integer when provided.")
    if not isinstance(invoice, InternalInvoice):
        return validation_result(["InternalInvoice DTO is required."])
    if not isinstance(partner_match, PartnerMatchResult):
        return validation_result(["PartnerMatchResult DTO is required."])
    if not isinstance(product_match, InvoiceProductMatchResult):
        return validation_result(["InvoiceProductMatchResult DTO is required."])
    if not isinstance(tax_match, InvoiceTaxMappingResult):
        return validation_result(["InvoiceTaxMappingResult DTO is required."])

    expense_mode = _operating_expense_mode(invoice, operating_expense_match)
    account_only_line_numbers = account_only_line_numbers if not expense_mode else frozenset()
    resolved_account_only_account_ids = _resolved_account_only_account_ids(
        account_only_line_numbers, account_only_expense_match, explicit_account_only_accounts
    )
    if account_only_line_numbers - resolved_account_only_account_ids.keys():
        errors.append("Explicit account-only line resolution requires a deterministic expense account mapping.")

    if partner_match.status is not PartnerMatchStatus.MATCHED or partner_match.partner_id is None:
        errors.append("Supplier partner must be matched before building a vendor bill.")
    if not invoice.header.invoice_number.strip():
        errors.append("Invoice number is required.")
    if invoice.header.issue_date is None:
        errors.append("Invoice date is required.")
    if invoice.header.currency_code is None or not invoice.header.currency_code.strip():
        errors.append("Invoice currency is required.")
    if not invoice.lines:
        errors.append("At least one invoice line is required.")

    if expense_mode:
        product_by_line = {}
        errors.extend(_operating_expense_product_shape_errors(invoice, product_match))
    else:
        product_by_line, product_errors = _validated_product_results(
            product_match, skip_line_numbers=account_only_line_numbers
        )
        errors.extend(product_errors)
    tax_by_line, tax_errors = _validated_tax_results(tax_match)
    errors.extend(tax_errors)

    for index, line in enumerate(invoice.lines):
        line_path = f"lines[{index}]"
        if line.line_number is None or not line.line_number.strip():
            errors.append(f"{line_path}.line_number is required.")
            continue
        line_is_account_only = line.line_number in account_only_line_numbers
        if not expense_mode and not line_is_account_only and line.line_number not in product_by_line:
            errors.append(f"{line_path}.product must be matched.")
        if line.quantity is None or line.quantity <= Decimal("0"):
            errors.append(f"{line_path}.quantity must be greater than zero.")
        if line.unit_price is None:
            errors.append(f"{line_path}.unit_price is required.")
        elif line.unit_price < Decimal("0"):
            errors.append(f"{line_path}.unit_price must not be negative.")
        for tax_index, _tax in enumerate(line.taxes):
            if (line.line_number, tax_index) not in tax_by_line:
                errors.append(f"{line_path}.taxes[{tax_index}] must be matched.")
        if line.unit_price is not None and line.quantity is not None:
            errors.extend(_discount_errors(line, line_path))

    if not errors and any(line.discounts for line in invoice.lines):
        errors.extend(_totals_invariant_errors(invoice))

    return validation_result(errors)


def _operating_expense_mode(invoice: InternalInvoice, operating_expense_match: object | None) -> bool:
    """True only for a deterministic operating-expense line invoice.

    Requires an explicit ``MATCHED`` operating-expense result with a positive pinned
    expense account and an invoice whose every line is free of deterministic product
    identifiers. It never bypasses a genuine failed product lookup: any line carrying
    a buyer/seller item code or barcode makes this ``False``.
    """

    from app.application.expense_mapping import OperatingExpenseMatchStatus, invoice_is_product_identifier_free

    if operating_expense_match is None or getattr(operating_expense_match, "status", None) is not (
        OperatingExpenseMatchStatus.MATCHED
    ):
        return False
    account_id = getattr(operating_expense_match, "expense_account_id", None)
    if type(account_id) is not int or account_id <= 0:
        return False
    return invoice_is_product_identifier_free(invoice)


def _resolved_account_only_account_ids(
    account_only_line_numbers: frozenset[str],
    account_only_expense_match: object | None,
    explicit_account_only_accounts: dict[str, int] | None,
) -> dict[str, int]:
    """The deterministic expense account for each explicit account-only line, if any.

    Precedence is per line: an explicit, decision-time-pinned
    ``LineResolution.expense_account_id`` (P0-PROD-08G) always wins; the legacy
    whole-vendor ``account_only_expense_match`` is the fallback for any account-only
    line that carries no explicit account of its own. A line resolved by neither is
    simply absent from the returned mapping -- callers must fail closed on that
    absence rather than guess an account.
    """

    if not account_only_line_numbers:
        return {}

    fallback_account_id = _fallback_account_only_account_id(account_only_expense_match)
    explicit_accounts = explicit_account_only_accounts or {}

    resolved: dict[str, int] = {}
    for line_number in account_only_line_numbers:
        explicit_account_id = explicit_accounts.get(line_number)
        if type(explicit_account_id) is int and not isinstance(explicit_account_id, bool) and explicit_account_id > 0:
            resolved[line_number] = explicit_account_id
        elif fallback_account_id is not None:
            resolved[line_number] = fallback_account_id
    return resolved


def _fallback_account_only_account_id(account_only_expense_match: object | None) -> int | None:
    """The legacy whole-vendor expense account, if the pinned mapping is a clean match.

    Returns ``None`` unless the resolution is a clean ``MATCHED`` result with a
    positive account id.
    """

    from app.application.expense_mapping import OperatingExpenseMatchStatus

    if account_only_expense_match is None or getattr(account_only_expense_match, "status", None) is not (
        OperatingExpenseMatchStatus.MATCHED
    ):
        return None
    account_id = getattr(account_only_expense_match, "expense_account_id", None)
    if type(account_id) is not int or account_id <= 0:
        return None
    return account_id


def _operating_expense_product_shape_errors(
    invoice: InternalInvoice,
    product_match: InvoiceProductMatchResult,
) -> tuple[str, ...]:
    """Product results for an identifier-free invoice must be one INVALID_INPUT per line.

    Malformed or incomplete product result collections still fail closed even when an
    operating-expense mapping is present.
    """

    if product_match.errors:
        return ("Operating-expense product result carries evaluation errors.",)
    invoice_line_numbers = tuple(line.line_number for line in invoice.lines)
    result_line_numbers = tuple(line_result.line_number for line_result in product_match.line_results)
    if result_line_numbers != invoice_line_numbers:
        return ("Operating-expense product result does not cover every invoice line exactly once.",)
    for line_result in product_match.line_results:
        if line_result.result.status is not ProductMatchStatus.INVALID_INPUT:
            return ("Operating-expense product result must be INVALID_INPUT for an identifier-free invoice.",)
    return ()


def to_odoo_account_move_payload(
    vendor_bill: VendorBill, *, currency_id: int, product_uom_ids: Mapping[int, int]
) -> dict[str, Any]:
    if type(currency_id) is not int or currency_id <= 0:
        raise ValueError("currency_id must be a positive Odoo id.")
    if not isinstance(product_uom_ids, Mapping):
        raise ValueError("product_uom_ids must be a mapping of Odoo product id to Odoo uom id.")
    payload: dict[str, Any] = {
        "move_type": "in_invoice",
        "partner_id": vendor_bill.supplier_id,
        "invoice_date": vendor_bill.invoice_date.isoformat(),
        "ref": vendor_bill.reference,
        "currency_id": currency_id,
        "invoice_line_ids": tuple((0, 0, _line_payload(line, product_uom_ids)) for line in vendor_bill.invoice_lines),
    }
    if vendor_bill.company_id is not None:
        payload["company_id"] = vendor_bill.company_id
    if vendor_bill.notes:
        payload["narration"] = "\n".join(vendor_bill.notes)
    return {key: value for key, value in payload.items() if value is not None}


def to_odoo_customer_invoice_payload(
    customer_invoice: CustomerInvoice,
    *,
    currency_id: int,
) -> dict[str, Any]:
    if type(currency_id) is not int or currency_id <= 0:
        raise ValueError("currency_id must be a positive Odoo id.")
    payload: dict[str, Any] = {
        "move_type": "out_invoice",
        "company_id": customer_invoice.company_id,
        "partner_id": customer_invoice.customer_id,
        "invoice_date": customer_invoice.invoice_date.isoformat(),
        "ref": customer_invoice.reference,
        "currency_id": currency_id,
        "invoice_line_ids": tuple((0, 0, _customer_line_payload(line)) for line in customer_invoice.invoice_lines),
    }
    if customer_invoice.notes:
        payload["narration"] = "\n".join(customer_invoice.notes)
    return {key: value for key, value in payload.items() if value is not None}


def _line_payload(line: VendorBillLine, product_uom_ids: Mapping[int, int]) -> dict[str, Any]:
    if line.account_id is not None:
        return _operating_expense_line_payload(line)
    # P0-PROD-10E: the source invoice's raw UN/CEFACT unit code (e.g. "C62") must
    # never reach this payload -- Odoo's product_uom_id is a many2one integer id,
    # never an external unit-code string. product_uom_ids is resolved read-only
    # from the product itself (see AccountMoveRepository._resolve_vendor_bill_product_uoms
    # / OdooVendorBillPreviewProductUomReader), never derived from the invoice here.
    assert line.product_id is not None
    uom_id = product_uom_ids.get(line.product_id)
    if type(uom_id) is not int or uom_id <= 0:
        raise ValueError(f"No resolved Odoo product_uom_id for product {line.product_id}.")
    payload: dict[str, Any] = {
        "product_id": line.product_id,
        "quantity": _decimal_text(line.quantity),
        "price_unit": _decimal_text(line.unit_price),
        "tax_ids": ((6, 0, line.tax_ids),),
        "name": line.description,
        "product_uom_id": uom_id,
    }
    return {key: value for key, value in payload.items() if value is not None}


def _operating_expense_line_payload(line: VendorBillLine) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": line.description,
        "quantity": _decimal_text(line.quantity),
        "price_unit": _decimal_text(line.unit_price),
        "account_id": line.account_id,
        "tax_ids": ((6, 0, line.tax_ids),),
    }
    return {key: value for key, value in payload.items() if value is not None}


def _customer_line_payload(line: CustomerInvoiceLine) -> dict[str, Any]:
    return {
        key: value
        for key, value in {
            "product_id": line.product_id,
            "quantity": _decimal_text(line.quantity),
            "price_unit": _decimal_text(line.unit_price),
            "tax_ids": ((6, 0, line.tax_ids),),
            "name": line.description,
        }.items()
        if value is not None
    }


def _vendor_bill_line(
    line: InvoiceLine,
    product_result: Any,
    tax_ids_by_line: dict[tuple[str | None, int], int],
) -> VendorBillLine:
    assert product_result.product_id is not None
    assert line.quantity is not None
    assert line.unit_price is not None
    tax_ids = tuple(tax_ids_by_line[(line.line_number, tax_index)] for tax_index, _tax in enumerate(line.taxes))
    return VendorBillLine(
        product_id=product_result.product_id,
        quantity=line.quantity,
        unit_price=_net_unit_price(line),
        tax_ids=tax_ids,
        description=line.description,
    )


def _expense_vendor_bill_line(
    line: InvoiceLine,
    expense_account_id: int,
    tax_ids_by_line: dict[tuple[str | None, int], int],
) -> VendorBillLine:
    assert line.quantity is not None
    assert line.unit_price is not None
    tax_ids = tuple(tax_ids_by_line[(line.line_number, tax_index)] for tax_index, _tax in enumerate(line.taxes))
    return VendorBillLine(
        product_id=None,
        account_id=expense_account_id,
        quantity=line.quantity,
        unit_price=_net_unit_price(line),
        tax_ids=tax_ids,
        description=line.description,
    )


# P0-PROD-08L: preserve source invoice discounts/allowances when building a Vendor
# Bill line. Odoo's own account.move.line.discount is a percentage field; deriving a
# percentage from a source *amount* is an unnecessary, lossy round-trip (an amount is
# what the immutable evidence actually says). Instead the discount is netted directly
# into price_unit -- quantity * net_unit_price reproduces the source's post-discount
# line economics exactly, with no reliance on Odoo's own rounding behavior at all.
#
# P0-PROD-15AD: ``line.unit_price`` is a rounded *display* figure transmitted by the
# source invoice (typically 2 decimal places) -- for an undiscounted line, it does
# not, in general, reproduce the line's authoritative net amount when multiplied by
# quantity (proven against real production data: CloudSpark's CPU/HDD/RAM lines
# have zero discounts, yet quantity * unit_price differs from the transmitted net
# by a few kuruş on every line). For that specific, narrow case -- no discount at
# all -- the line's own ``line_extension_amount`` (UBL: cbc:LineExtensionAmount) is
# trusted directly as the authoritative net. This is deliberately NOT extended to
# the discounted case: at least one real historical production invoice in this
# system's own fixtures carries a ``line_extension_amount`` that is the line's
# *pre-discount* figure, not net-of-discount as strict UBL would imply -- Uyumsoft's
# actual transmitted semantics for a discounted line are not uniformly verified
# here, so the existing, already-correct P0-PROD-08L reconstruction (gross computed
# from quantity * unit_price, minus the discount amount) remains untouched and
# authoritative whenever any discount is present. ``unit_price`` remains required,
# validated, and untouched everywhere it is presented as source evidence (see
# ``ReviewSourceInvoiceEvidence``/the review-detail API) -- this only changes what
# *Odoo posting* price_unit is derived from, never the immutable source record.
#
# Only a source amount is ever trusted. A rate-only allowance (no cbc:Amount) has no
# safely-inferable base without inventing accounting logic the immutable evidence does
# not itself provide -- validate_vendor_bill_inputs fails the whole build closed for
# that line rather than guess (see _discount_errors). Line-level *charges*
# (ChargeIndicator=true) are not modeled anywhere in InvoiceLine at all -- the parser
# never preserves them -- so a genuine charge is invisible to this function; the
# per-invoice totals invariant below is what catches that (and any other unmodeled
# economic difference, including a header-only allowance -- see MonetaryTotals) rather
# than this function silently mismatching.
_DISCOUNT_UNIT_PRICE_PRECISION = Decimal("0.000001")  # matches the source's own unit_price precision
TOTALS_INVARIANT_TOLERANCE = Decimal("0.01")  # one minor currency unit (kuruş/cent)


def _line_gross_total(line: InvoiceLine) -> Decimal:
    assert line.unit_price is not None
    assert line.quantity is not None
    return line.unit_price * line.quantity


def _line_total_discount(line: InvoiceLine) -> Decimal:
    return sum((discount.amount for discount in line.discounts if discount.amount is not None), Decimal("0"))


def _line_net_total(line: InvoiceLine) -> Decimal:
    """The line's authoritative net (tax-exclusive, post-allowance) amount.

    For an undiscounted line, prefers the source's own transmitted
    ``line_extension_amount`` over quantity * unit_price, which silently drifts
    whenever the source's rounded display unit_price does not reproduce
    line_extension_amount exactly (see P0-PROD-15AD). Discounted lines are
    unaffected -- see that module comment for why ``line_extension_amount`` is not
    trusted as net-of-discount here -- and keep the pre-15AD
    quantity/unit_price/discount reconstruction exactly.
    """

    if not line.discounts and line.line_extension_amount is not None:
        return line.line_extension_amount
    return _line_gross_total(line) - _line_total_discount(line)


def line_gross_total(line: InvoiceLine) -> Decimal:
    """Public reuse point for ``_line_gross_total`` (P0-PROD-09B: Vendor Bill preview).

    Preview needs the exact same per-line gross/discount/net economics
    ``VendorBillBuilder.build`` already computes and validates (P0-PROD-08L/15AD) --
    this and its two siblings below exist so preview never reimplements that
    arithmetic, only reads it.
    """

    return _line_gross_total(line)


def line_total_discount(line: InvoiceLine) -> Decimal:
    """Public reuse point for ``_line_total_discount`` -- see ``line_gross_total``."""

    return _line_total_discount(line)


def line_net_total(line: InvoiceLine) -> Decimal:
    """Public reuse point for ``_line_net_total`` -- see ``line_gross_total``."""

    return _line_net_total(line)


def _net_unit_price(line: InvoiceLine) -> Decimal:
    """Odoo's posting ``price_unit`` -- reconciled to the line's authoritative net
    amount (``_line_net_total``: ``line_extension_amount`` when the source
    transmitted one, else the quantity/unit_price/discount reconstruction), never
    assumed equal to the source's rounded display ``unit_price`` (P0-PROD-15AD).
    ``validate_vendor_bill_inputs`` has already proven quantity is positive and
    every discount carries a usable amount not exceeding the line's gross total
    before this is ever reached.

    ``line.unit_price`` unchanged -- byte-identical to pre-15AD/pre-08L behavior,
    including its exact decimal precision -- whenever it already reproduces the
    authoritative net exactly (quantity * unit_price == net_total): the common
    case for a well-formed, undiscounted, exact-precision line. Only recomputed
    (and only then quantized to a higher, explicit precision) when it does not.
    """

    assert line.unit_price is not None
    assert line.quantity is not None
    net_total = _line_net_total(line)
    if line.unit_price * line.quantity == net_total:
        return line.unit_price
    return (net_total / line.quantity).quantize(_DISCOUNT_UNIT_PRICE_PRECISION, rounding=ROUND_HALF_UP)


def _discount_errors(line: InvoiceLine, line_path: str) -> list[str]:
    if not line.discounts:
        return []
    if any(discount.amount is None for discount in line.discounts):
        return [
            f"{line_path}.discounts contains an allowance with no amount; "
            "percentage-only/rate-only allowances are not supported."
        ]
    total_discount = _line_total_discount(line)
    if total_discount < Decimal("0"):
        return [f"{line_path}.discounts total must not be negative."]
    if total_discount > _line_gross_total(line) + TOTALS_INVARIANT_TOLERANCE:
        return [f"{line_path}.discounts total must not exceed the line's gross amount."]
    return []


def _totals_invariant_errors(invoice: InternalInvoice) -> list[str]:
    """P0-PROD-08L: the exact safety net for the defect found in P0-PROD-08K -- never
    build a Vendor Bill whose net line economics silently diverge from the immutable
    source invoice's own authoritative tax-exclusive total. This also fails closed on
    an invoice-level-only allowance (MonetaryTotals.allowance_total not fully explained
    by any line's own discounts) and on a line-level charge (never parsed into
    InvoiceLine at all) -- neither is modeled by this PR, so either would otherwise
    silently produce a Vendor Bill with different economics than the source invoice.
    Only evaluated when at least one line actually carries a discount; a no-discount
    invoice never reaches this check, preserving pre-08L behavior exactly.
    """

    target = invoice.totals.tax_exclusive_amount
    if target is None:
        return ["totals.tax_exclusive_amount is required to validate discounted line economics."]
    computed = sum(
        (_line_net_total(line) for line in invoice.lines if line.unit_price is not None and line.quantity is not None),
        Decimal("0"),
    )
    if abs(computed - target) > TOTALS_INVARIANT_TOLERANCE:
        return [
            f"Computed net line total ({computed}) does not match the source invoice's tax-exclusive "
            f"amount ({target}); refusing to build a Vendor Bill with different economics than the "
            "source invoice."
        ]
    return []


def _customer_invoice_reference(
    *,
    source_invoice_id: str,
    billing_instruction: CustomerInvoiceBillingInstruction,
) -> str:
    return f"Recharge {source_invoice_id}:{billing_instruction.billing_key}"


def _validated_product_results(
    product_match: InvoiceProductMatchResult,
    *,
    skip_line_numbers: frozenset[str] = frozenset(),
) -> tuple[dict[str | None, Any], tuple[str, ...]]:
    """Matched product results by line, skipping ``skip_line_numbers`` entirely.

    A skipped line is neither required to be matched nor reported as unmatched -- it is
    reserved for an explicit, separately-validated resolution instead (see
    ``account_only_line_numbers`` in ``validate_vendor_bill_inputs``). The default is
    empty, so existing callers see unchanged behavior.
    """

    errors = list(product_match.errors)
    product_by_line: dict[str | None, Any] = {}
    for line_result in product_match.line_results:
        line_number = line_result.line_number
        if line_number in skip_line_numbers:
            continue
        if line_number in product_by_line:
            errors.append(f"Duplicate product mapping for line {line_number}.")
            continue
        result = line_result.result
        if result.status is not ProductMatchStatus.MATCHED or result.product_id is None:
            errors.append(f"Product mapping for line {line_number} is not matched.")
            continue
        product_by_line[line_number] = result
    return product_by_line, tuple(errors)


def _validated_tax_results(
    tax_match: InvoiceTaxMappingResult,
) -> tuple[dict[tuple[str | None, int], Any], tuple[str, ...]]:
    errors = list(tax_match.errors)
    tax_by_line: dict[tuple[str | None, int], Any] = {}
    for line_result in tax_match.line_results:
        key = (line_result.line_number, line_result.tax_index)
        if key in tax_by_line:
            errors.append(f"Duplicate tax mapping for line {line_result.line_number} tax {line_result.tax_index}.")
            continue
        result = line_result.result
        if result.status is not TaxMatchStatus.MATCHED or result.tax_id is None:
            errors.append(f"Tax mapping for line {line_result.line_number} tax {line_result.tax_index} is not matched.")
            continue
        tax_by_line[key] = result
    return tax_by_line, tuple(errors)


def tax_lines_fully_matched(invoice: InternalInvoice, tax_match: InvoiceTaxMappingResult) -> bool:
    """True only when every tax on every invoice line resolves to a matched Odoo tax id.

    Used to decide whether Stage-1 execution evidence may be pinned for a review whose
    product lines are not (yet) fully resolved -- e.g. pending an explicit human
    account-only decision (``LineResolution.account_only``) -- without weakening the
    existing product-mode or whole-invoice operating-expense validation paths, which
    are unchanged and still evaluated first.
    """

    if not isinstance(invoice, InternalInvoice) or not isinstance(tax_match, InvoiceTaxMappingResult):
        return False
    tax_by_line, tax_errors = _validated_tax_results(tax_match)
    if tax_errors:
        return False
    for line in invoice.lines:
        for tax_index, _tax in enumerate(line.taxes):
            if (line.line_number, tax_index) not in tax_by_line:
                return False
    return True


def _product_results_by_line(product_match: InvoiceProductMatchResult) -> dict[str | None, Any]:
    return {line_result.line_number: line_result.result for line_result in product_match.line_results}


def _tax_ids_by_line(tax_match: InvoiceTaxMappingResult) -> dict[tuple[str | None, int], int]:
    return {
        (line_result.line_number, line_result.tax_index): line_result.result.tax_id
        for line_result in tax_match.line_results
        if line_result.result.tax_id is not None
    }


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")
