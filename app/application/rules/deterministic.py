from __future__ import annotations

from typing import Protocol

from app.application.commands import ImportInvoiceCommand
from app.application.dto import RuleEvaluationResult
from app.application.exceptions import ApplicationError
from app.application.workflow import WorkflowDecision, WorkflowType
from app.domain.invoice import InternalInvoice
from app.matching import (
    InvoiceProductMatchResult,
    PartnerMatchResult,
    PartnerMatchStatus,
    ProductMatchStatus,
)
from app.tax_mapping import InvoiceTaxMappingResult, TaxMatchStatus

DIRECT_VENDOR_BILL_RULE_ID = "RULE-DIRECT-VENDOR-BILL-001"
DIRECT_VENDOR_BILL_EXPLANATION = (
    "Supplier, products and taxes matched deterministically; Vendor Bill workflow selected."
)


class RuleEvaluationError(ApplicationError):
    error_category = "rule_evaluation_error"


class PartnerRuleEvaluationError(RuleEvaluationError):
    error_category = "partner_rule_evaluation_error"


class ProductRuleEvaluationError(RuleEvaluationError):
    error_category = "product_rule_evaluation_error"


class TaxRuleEvaluationError(RuleEvaluationError):
    error_category = "tax_rule_evaluation_error"


class PartnerMatcher(Protocol):
    def match_invoice(self, invoice: InternalInvoice, *, company_id: int | None = None) -> PartnerMatchResult:
        pass


class ProductMatcher(Protocol):
    def match_invoice(self, invoice: InternalInvoice, *, company_id: int | None = None) -> InvoiceProductMatchResult:
        pass


class TaxMapper(Protocol):
    def map_invoice(self, invoice: InternalInvoice, *, company_id: int | None = None) -> InvoiceTaxMappingResult:
        pass


class DeterministicRuleEngine:
    """Evaluate deterministic invoice facts for the direct Vendor Bill rule."""

    def __init__(
        self,
        *,
        partner_matcher: PartnerMatcher,
        product_matcher: ProductMatcher,
        tax_mapper: TaxMapper,
    ) -> None:
        self._partner_matcher = partner_matcher
        self._product_matcher = product_matcher
        self._tax_mapper = tax_mapper

    def evaluate(self, command: ImportInvoiceCommand) -> RuleEvaluationResult:
        invoice = _invoice(command)
        partner_match = _evaluate_partner(self._partner_matcher, invoice, command.company_id)
        _require_partner_match(partner_match)

        product_match = _evaluate_products(self._product_matcher, invoice, command.company_id)
        _require_product_matches(invoice, product_match)

        tax_match = _evaluate_taxes(self._tax_mapper, invoice, command.company_id)
        _require_tax_matches(invoice, tax_match)

        warnings = product_match.warnings + tax_match.warnings
        workflow_decision = WorkflowDecision(
            workflow=WorkflowType.VENDOR_BILL,
            matched_rule=DIRECT_VENDOR_BILL_RULE_ID,
            explanation=DIRECT_VENDOR_BILL_EXPLANATION,
        )
        return RuleEvaluationResult(
            workflow_decision=workflow_decision,
            partner_match=partner_match,
            product_match=product_match,
            tax_match=tax_match,
            warnings=warnings,
        )


def _invoice(command: ImportInvoiceCommand) -> InternalInvoice:
    if not isinstance(command.invoice, InternalInvoice):
        raise RuleEvaluationError("InternalInvoice DTO is required for rule evaluation.")
    return command.invoice


def _evaluate_partner(
    matcher: PartnerMatcher,
    invoice: InternalInvoice,
    company_id: int | None,
) -> PartnerMatchResult:
    try:
        return matcher.match_invoice(invoice, company_id=company_id)
    except ApplicationError:
        raise
    except Exception as exc:
        raise PartnerRuleEvaluationError(_safe_message(exc, "Supplier matching failed.")) from exc


def _evaluate_products(
    matcher: ProductMatcher,
    invoice: InternalInvoice,
    company_id: int | None,
) -> InvoiceProductMatchResult:
    try:
        return matcher.match_invoice(invoice, company_id=company_id)
    except ApplicationError:
        raise
    except Exception as exc:
        raise ProductRuleEvaluationError(_safe_message(exc, "Product matching failed.")) from exc


def _evaluate_taxes(
    mapper: TaxMapper,
    invoice: InternalInvoice,
    company_id: int | None,
) -> InvoiceTaxMappingResult:
    try:
        return mapper.map_invoice(invoice, company_id=company_id)
    except ApplicationError:
        raise
    except Exception as exc:
        raise TaxRuleEvaluationError(_safe_message(exc, "Tax mapping failed.")) from exc


def _require_partner_match(result: PartnerMatchResult) -> None:
    if result.status is PartnerMatchStatus.MATCHED and result.partner_id is not None:
        return
    if result.status is PartnerMatchStatus.NOT_FOUND:
        raise PartnerRuleEvaluationError(f"Supplier was not matched deterministically: {result.reason}")
    if result.status is PartnerMatchStatus.MULTIPLE_MATCHES:
        raise PartnerRuleEvaluationError(f"Supplier match is ambiguous: {result.reason}")
    raise PartnerRuleEvaluationError(f"Supplier matching result is invalid: {result.reason}")


def _require_product_matches(invoice: InternalInvoice, result: InvoiceProductMatchResult) -> None:
    if result.errors:
        raise ProductRuleEvaluationError(f"Product matching failed: {_join(result.errors)}")

    invoice_line_numbers = tuple(line.line_number for line in invoice.lines)
    result_line_numbers = tuple(line.line_number for line in result.line_results)
    if result_line_numbers != invoice_line_numbers:
        raise ProductRuleEvaluationError("Product matching result is incomplete for invoice lines.")

    for line_result in result.line_results:
        match = line_result.result
        if match.status is ProductMatchStatus.MATCHED and match.product_id is not None:
            continue
        if match.status is ProductMatchStatus.NOT_FOUND:
            raise ProductRuleEvaluationError(
                f"Product was not matched deterministically for line {line_result.line_number}: {match.reason}"
            )
        if match.status is ProductMatchStatus.MULTIPLE_MATCHES:
            raise ProductRuleEvaluationError(
                f"Product match is ambiguous for line {line_result.line_number}: {match.reason}"
            )
        raise ProductRuleEvaluationError(
            f"Product matching result is invalid for line {line_result.line_number}: {match.reason}"
        )


def _require_tax_matches(invoice: InternalInvoice, result: InvoiceTaxMappingResult) -> None:
    if result.errors:
        raise TaxRuleEvaluationError(f"Tax mapping failed: {_join(result.errors)}")

    expected_keys = tuple(
        (line.line_number, tax_index) for line in invoice.lines for tax_index, _tax in enumerate(line.taxes)
    )
    result_keys = tuple((line.line_number, line.tax_index) for line in result.line_results)
    if result_keys != expected_keys:
        raise TaxRuleEvaluationError("Tax mapping result is incomplete for invoice taxes.")

    for line_result in result.line_results:
        match = line_result.result
        if match.status is TaxMatchStatus.MATCHED and match.tax_id is not None:
            continue
        if match.status is TaxMatchStatus.NOT_FOUND:
            raise TaxRuleEvaluationError(
                f"Tax was not mapped deterministically for line {line_result.line_number}: {match.reason}"
            )
        if match.status is TaxMatchStatus.MULTIPLE_MATCHES:
            raise TaxRuleEvaluationError(f"Tax mapping is ambiguous for line {line_result.line_number}: {match.reason}")
        raise TaxRuleEvaluationError(
            f"Tax mapping result is invalid for line {line_result.line_number}: {match.reason}"
        )


def _safe_message(exc: Exception, fallback_message: str) -> str:
    safe_message = getattr(exc, "safe_message", None)
    return safe_message if isinstance(safe_message, str) and safe_message.strip() else fallback_message


def _join(messages: tuple[str, ...]) -> str:
    return "; ".join(message for message in messages if message.strip())
