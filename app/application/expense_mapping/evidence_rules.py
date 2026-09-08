from __future__ import annotations

from decimal import Decimal

from app.application.expense_mapping.matching import OperatingExpenseMatchResult, OperatingExpenseMatchStatus
from app.application.expense_mapping.predicates import invoice_is_product_identifier_free
from app.domain.invoice import InternalInvoice
from app.matching import InvoiceProductMatchResult, PartnerMatchResult, ProductMatchStatus

EXACT_OPERATING_EXPENSE_CONFIDENCE = Decimal("1.00")


def operating_expense_evidence_errors(
    *,
    operating_expense_match: object | None,
    company_id: int,
    invoice: InternalInvoice,
    partner_match: PartnerMatchResult,
    product_match: InvoiceProductMatchResult,
) -> list[str]:
    """Fail-closed contradiction checks for immutable operating-expense execution evidence.

    Returns ``[]`` for product-mode evidence (``operating_expense_match is None``) and for a
    fully consistent operating-expense snapshot. Otherwise it returns the contradictions so the
    calling evidence DTO can reject the state with its own error type. Never queries anything.
    """

    if operating_expense_match is None:
        return []
    if not isinstance(operating_expense_match, OperatingExpenseMatchResult):
        return ["operating_expense_match must be an OperatingExpenseMatchResult."]

    match = operating_expense_match
    errors: list[str] = []
    if match.status is not OperatingExpenseMatchStatus.MATCHED:
        errors.append("operating_expense_match evidence must be MATCHED.")
    for name, value in (
        ("mapping_id", match.mapping_id),
        ("company_id", match.company_id),
        ("vendor_partner_id", match.vendor_partner_id),
        ("expense_account_id", match.expense_account_id),
    ):
        if type(value) is not int or value <= 0:
            errors.append(f"operating_expense_match.{name} must be a positive integer.")
    if not isinstance(match.expense_category, str) or not match.expense_category.strip():
        errors.append("operating_expense_match.expense_category is required.")
    if not isinstance(match.matched_by, str) or not match.matched_by.strip():
        errors.append("operating_expense_match.matched_by is required.")
    if match.confidence != EXACT_OPERATING_EXPENSE_CONFIDENCE:
        errors.append("operating_expense_match.confidence must be exactly 1.00.")
    if isinstance(company_id, int) and match.company_id != company_id:
        errors.append("operating_expense_match.company_id must match evidence company_id.")
    if partner_match.partner_id is not None and match.vendor_partner_id != partner_match.partner_id:
        errors.append("operating_expense_match.vendor_partner_id must match the matched supplier partner.")
    if not invoice_is_product_identifier_free(invoice):
        errors.append("operating-expense evidence requires an invoice free of deterministic product identifiers.")
    errors.extend(_product_shape_errors(invoice, product_match))
    return errors


def _product_shape_errors(invoice: InternalInvoice, product_match: InvoiceProductMatchResult) -> list[str]:
    if product_match.errors:
        return ["operating-expense product evidence carries evaluation errors."]
    invoice_line_numbers = tuple(line.line_number for line in invoice.lines)
    result_line_numbers = tuple(line_result.line_number for line_result in product_match.line_results)
    if result_line_numbers != invoice_line_numbers:
        return ["operating-expense product evidence must cover every invoice line exactly once."]
    for line_result in product_match.line_results:
        if line_result.result.status is not ProductMatchStatus.INVALID_INPUT:
            return ["operating-expense product evidence must be INVALID_INPUT for every line."]
    return []
