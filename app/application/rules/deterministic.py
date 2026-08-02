from __future__ import annotations

from typing import Protocol

from app.application.commands import ImportInvoiceCommand
from app.application.dto import RuleEvaluationResult
from app.application.exceptions import ApplicationError
from app.application.workflow import (
    ManualReviewDecision,
    ManualReviewReason,
    ManualReviewReasonCode,
    WorkflowDecision,
    WorkflowType,
)
from app.domain.invoice import InternalInvoice
from app.matching import (
    InvoiceProductMatchResult,
    PartnerMatchResult,
    PartnerMatchStatus,
    ProductMatchStatus,
)
from app.tax_mapping import InvoiceTaxMappingResult, TaxMatchStatus

DIRECT_VENDOR_BILL_RULE_ID = "RULE-DIRECT-VENDOR-BILL-001"
MANUAL_REVIEW_RULE_ID = "RULE-MANUAL-REVIEW-001"
DIRECT_VENDOR_BILL_EXPLANATION = (
    "Supplier, products and taxes matched deterministically; Vendor Bill workflow selected."
)
MANUAL_REVIEW_EXPLANATION = "Deterministic business mismatch requires Manual Review."


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
        product_match = _evaluate_products(self._product_matcher, invoice, command.company_id)
        tax_match = _evaluate_taxes(self._tax_mapper, invoice, command.company_id)

        warnings = product_match.warnings + tax_match.warnings
        review_reasons = (
            _partner_review_reasons(partner_match)
            + _product_review_reasons(invoice, product_match)
            + _tax_review_reasons(invoice, tax_match)
        )
        if review_reasons:
            return RuleEvaluationResult(
                workflow_decision=_manual_review_decision(review_reasons),
                partner_match=partner_match,
                product_match=product_match,
                tax_match=tax_match,
                warnings=warnings,
            )

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


def _partner_review_reasons(result: PartnerMatchResult) -> tuple[ManualReviewReason, ...]:
    if result.status is PartnerMatchStatus.MATCHED and result.partner_id is not None:
        return ()
    if result.status is PartnerMatchStatus.NOT_FOUND:
        return (
            _reason(
                code=ManualReviewReasonCode.SUPPLIER_NOT_FOUND,
                message="Supplier was not matched deterministically.",
                source="partner_matching",
                candidate_count=result.candidate_count,
                details=(("matched_by", result.matched_by or ""), ("reason", result.reason)),
            ),
        )
    if result.status is PartnerMatchStatus.MULTIPLE_MATCHES:
        return (
            _reason(
                code=ManualReviewReasonCode.SUPPLIER_AMBIGUOUS,
                message="Supplier match is ambiguous.",
                source="partner_matching",
                candidate_count=result.candidate_count,
                details=(("reason", result.reason),),
            ),
        )
    if "tax number" in result.reason.lower():
        code = ManualReviewReasonCode.SUPPLIER_TAX_NUMBER_MISSING
        message = "Supplier tax number is missing or invalid."
    else:
        code = ManualReviewReasonCode.UNSUPPORTED_INVOICE_CONTENT
        message = "Supplier content is not supported by deterministic review rules."
    return (
        _reason(
            code=code,
            message=message,
            source="partner_matching",
            candidate_count=result.candidate_count,
            details=(("reason", result.reason),),
        ),
    )


def _product_review_reasons(
    invoice: InternalInvoice,
    result: InvoiceProductMatchResult,
) -> tuple[ManualReviewReason, ...]:
    reasons: list[ManualReviewReason] = []
    if result.errors:
        reasons.append(
            _reason(
                code=ManualReviewReasonCode.UNSUPPORTED_INVOICE_CONTENT,
                message="Product matching could not evaluate invoice content deterministically.",
                source="product_matching",
                details=(("errors", _join(result.errors)),),
            )
        )

    invoice_line_numbers = tuple(line.line_number for line in invoice.lines)
    result_line_numbers = tuple(line.line_number for line in result.line_results)
    if result_line_numbers != invoice_line_numbers:
        reasons.append(
            _reason(
                code=ManualReviewReasonCode.PRODUCT_MAPPING_INCOMPLETE,
                message="Product matching result is incomplete for invoice lines.",
                source="product_matching",
                details=(
                    ("expected_line_numbers", _join_values(invoice_line_numbers)),
                    ("result_line_numbers", _join_values(result_line_numbers)),
                ),
            )
        )

    for line_result in result.line_results:
        match = line_result.result
        if match.status is ProductMatchStatus.MATCHED and match.product_id is not None:
            continue
        if match.status is ProductMatchStatus.NOT_FOUND:
            reasons.append(
                _reason(
                    code=ManualReviewReasonCode.PRODUCT_NOT_FOUND,
                    message="Product was not matched deterministically.",
                    line_number=line_result.line_number,
                    source="product_matching",
                    candidate_count=match.candidate_count,
                    details=(("reason", match.reason),),
                )
            )
            continue
        if match.status is ProductMatchStatus.MULTIPLE_MATCHES:
            reasons.append(
                _reason(
                    code=ManualReviewReasonCode.PRODUCT_AMBIGUOUS,
                    message="Product match is ambiguous.",
                    line_number=line_result.line_number,
                    source="product_matching",
                    candidate_count=match.candidate_count,
                    details=(("reason", match.reason),),
                )
            )
            continue
        code = (
            ManualReviewReasonCode.PRODUCT_IDENTIFIER_MISSING
            if "identifier" in match.reason.lower()
            else ManualReviewReasonCode.UNSUPPORTED_INVOICE_CONTENT
        )
        reasons.append(
            _reason(
                code=code,
                message="Product identifier is missing or invoice line content is unsupported.",
                line_number=line_result.line_number,
                source="product_matching",
                candidate_count=match.candidate_count,
                details=(("reason", match.reason),),
            )
        )
    return tuple(reasons)


def _tax_review_reasons(invoice: InternalInvoice, result: InvoiceTaxMappingResult) -> tuple[ManualReviewReason, ...]:
    reasons: list[ManualReviewReason] = []
    if result.errors:
        reasons.append(
            _reason(
                code=ManualReviewReasonCode.TAX_MAPPING_INCOMPLETE,
                message="Tax mapping result is incomplete for invoice taxes.",
                source="tax_mapping",
                details=(("errors", _join(result.errors)),),
            )
        )

    expected_keys = tuple(
        (line.line_number, tax_index) for line in invoice.lines for tax_index, _tax in enumerate(line.taxes)
    )
    result_keys = tuple((line.line_number, line.tax_index) for line in result.line_results)
    if not result.errors and result_keys != expected_keys:
        reasons.append(
            _reason(
                code=ManualReviewReasonCode.TAX_MAPPING_INCOMPLETE,
                message="Tax mapping result is incomplete for invoice taxes.",
                source="tax_mapping",
                details=(
                    ("expected_tax_keys", _join_keys(expected_keys)),
                    ("result_tax_keys", _join_keys(result_keys)),
                ),
            )
        )

    for line_result in result.line_results:
        match = line_result.result
        if match.status is TaxMatchStatus.MATCHED and match.tax_id is not None:
            continue
        if match.status is TaxMatchStatus.NOT_FOUND:
            reasons.append(
                _reason(
                    code=ManualReviewReasonCode.TAX_NOT_FOUND,
                    message="Tax was not mapped deterministically.",
                    line_number=line_result.line_number,
                    tax_index=line_result.tax_index,
                    source="tax_mapping",
                    candidate_count=match.candidate_count,
                    details=(("reason", match.reason),),
                )
            )
            continue
        if match.status is TaxMatchStatus.MULTIPLE_MATCHES:
            reasons.append(
                _reason(
                    code=ManualReviewReasonCode.TAX_AMBIGUOUS,
                    message="Tax mapping is ambiguous.",
                    line_number=line_result.line_number,
                    tax_index=line_result.tax_index,
                    source="tax_mapping",
                    candidate_count=match.candidate_count,
                    details=(("reason", match.reason),),
                )
            )
            continue
        reasons.append(
            _reason(
                code=ManualReviewReasonCode.UNSUPPORTED_INVOICE_CONTENT,
                message="Tax content is not supported by deterministic review rules.",
                line_number=line_result.line_number,
                tax_index=line_result.tax_index,
                source="tax_mapping",
                candidate_count=match.candidate_count,
                details=(("reason", match.reason),),
            )
        )
    return tuple(reasons)


def _manual_review_decision(reasons: tuple[ManualReviewReason, ...]) -> WorkflowDecision:
    return WorkflowDecision(
        workflow=WorkflowType.MANUAL_REVIEW,
        matched_rule=MANUAL_REVIEW_RULE_ID,
        explanation=MANUAL_REVIEW_EXPLANATION,
        manual_review=ManualReviewDecision(
            reasons=reasons,
            summary=f"{len(reasons)} deterministic review reason(s) require manual review.",
        ),
    )


def _reason(
    *,
    code: ManualReviewReasonCode,
    message: str,
    line_number: str | None = None,
    tax_index: int | None = None,
    candidate_count: int | None = None,
    source: str,
    details: tuple[tuple[str, str], ...] = (),
) -> ManualReviewReason:
    return ManualReviewReason(
        code=code,
        message=message,
        line_number=line_number,
        tax_index=tax_index,
        candidate_count=candidate_count,
        source=source,
        details=tuple((key, value) for key, value in details if value),
    )


def _safe_message(exc: Exception, fallback_message: str) -> str:
    safe_message = getattr(exc, "safe_message", None)
    return safe_message if isinstance(safe_message, str) and safe_message.strip() else fallback_message


def _join(messages: tuple[str, ...]) -> str:
    return "; ".join(message for message in messages if message.strip())


def _join_values(values: tuple[str | None, ...]) -> str:
    return ",".join(value or "" for value in values)


def _join_keys(values: tuple[tuple[str | None, int], ...]) -> str:
    return ",".join(f"{line_number or ''}:{tax_index}" for line_number, tax_index in values)
