from __future__ import annotations

from dataclasses import dataclass, field

from app.application.dto import ApplicationDTO
from app.application.rules.contracts import (
    InvoiceClassificationResult,
    InvoiceClassificationRuleEvidence,
    InvoiceClassificationStatus,
    InvoiceDecisionRule,
    InvoiceDecisionRuleContractError,
    InvoiceDecisionRuleMatch,
    order_invoice_decision_rules,
)


@dataclass(frozen=True, slots=True)
class InvoiceClassificationContext(ApplicationDTO):
    """Canonical ERP-neutral evidence for deterministic invoice rule matching."""

    company_id: int
    vendor_partner_id: int | None = None
    vendor_tax_id: str | None = None
    currency: str | None = None
    provider_document_type: str | None = None
    purchase_order_present: bool | None = None
    description_corpus: str = ""
    product_mapping_ids: tuple[int, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _require_positive_int(self.company_id, "company_id must be a positive integer.")
        _require_optional_positive_int(
            self.vendor_partner_id,
            "vendor_partner_id must be a positive integer when supplied.",
        )
        vendor_tax_id = _optional_text(self.vendor_tax_id)
        currency = _optional_currency(self.currency)
        provider_document_type = _optional_text(self.provider_document_type)
        if self.purchase_order_present is not None and type(self.purchase_order_present) is not bool:
            raise InvoiceDecisionRuleContractError("purchase_order_present must be boolean when supplied.")
        if not isinstance(self.description_corpus, str):
            raise InvoiceDecisionRuleContractError("description_corpus must be canonical text.")
        product_mapping_ids = _positive_int_tuple(self.product_mapping_ids, field_name="product_mapping_ids")
        object.__setattr__(self, "vendor_tax_id", vendor_tax_id)
        object.__setattr__(self, "currency", currency)
        object.__setattr__(self, "provider_document_type", provider_document_type)
        object.__setattr__(self, "description_corpus", self.description_corpus.strip().lower())
        object.__setattr__(self, "product_mapping_ids", product_mapping_ids)

    @classmethod
    def from_line_descriptions(
        cls,
        *,
        company_id: int,
        line_descriptions: tuple[str, ...],
        vendor_partner_id: int | None = None,
        vendor_tax_id: str | None = None,
        currency: str | None = None,
        provider_document_type: str | None = None,
        purchase_order_present: bool | None = None,
        product_mapping_ids: tuple[int, ...] = (),
    ) -> InvoiceClassificationContext:
        if not isinstance(line_descriptions, tuple):
            raise InvoiceDecisionRuleContractError("line_descriptions must be an immutable tuple.")
        corpus = "\n".join(description.strip() for description in line_descriptions if description.strip())
        return cls(
            company_id=company_id,
            vendor_partner_id=vendor_partner_id,
            vendor_tax_id=vendor_tax_id,
            currency=currency,
            provider_document_type=provider_document_type,
            purchase_order_present=purchase_order_present,
            description_corpus=corpus,
            product_mapping_ids=product_mapping_ids,
        )


class InvoiceDecisionRuleEngine:
    """Classify inbound invoices by deterministic canonical rule matching."""

    def classify(
        self,
        *,
        context: InvoiceClassificationContext,
        rules: tuple[InvoiceDecisionRule, ...],
    ) -> InvoiceClassificationResult:
        if not isinstance(context, InvoiceClassificationContext):
            raise InvoiceDecisionRuleContractError("context must be an InvoiceClassificationContext.")
        ordered_rules = order_invoice_decision_rules(rules)
        matched_rules = tuple(rule for rule in ordered_rules if _matches(rule.match, context))
        if not matched_rules:
            return InvoiceClassificationResult(status=InvoiceClassificationStatus.NO_MATCH)

        winning_key = _effective_precedence_key(matched_rules[0])
        winning_rules = tuple(rule for rule in matched_rules if _effective_precedence_key(rule) == winning_key)
        action_fingerprints = {rule.action.fingerprint() for rule in winning_rules}
        if len(action_fingerprints) > 1:
            return InvoiceClassificationResult(
                status=InvoiceClassificationStatus.CONFLICT,
                matched_rules=matched_rules,
                conflict_rule_evidence=tuple(
                    InvoiceClassificationRuleEvidence.from_rule(rule) for rule in winning_rules
                ),
            )

        selected_rule = winning_rules[0]
        status = (
            InvoiceClassificationStatus.REVIEW_REQUIRED
            if selected_rule.action.require_review
            else InvoiceClassificationStatus.MATCHED
        )
        return InvoiceClassificationResult(
            status=status,
            matched_rules=matched_rules,
            selected_rule=selected_rule,
            matched_rule_evidence=tuple(InvoiceClassificationRuleEvidence.from_rule(rule) for rule in winning_rules),
        )


def _matches(match: InvoiceDecisionRuleMatch, context: InvoiceClassificationContext) -> bool:
    if match.company_id is not None and match.company_id != context.company_id:
        return False
    if match.vendor_partner_id is not None and match.vendor_partner_id != context.vendor_partner_id:
        return False
    if match.vendor_tax_id is not None and match.vendor_tax_id != context.vendor_tax_id:
        return False
    if match.currency is not None and match.currency != context.currency:
        return False
    if match.provider_document_type is not None and match.provider_document_type != context.provider_document_type:
        return False
    if match.purchase_order_present is not None and match.purchase_order_present is not context.purchase_order_present:
        return False
    if match.product_mapping_id is not None and match.product_mapping_id not in context.product_mapping_ids:
        return False
    return all(term in context.description_corpus for term in match.description_contains)


def _effective_precedence_key(rule: InvoiceDecisionRule) -> tuple[int, int, int]:
    return (-rule.match.specificity, rule.priority.tier, rule.priority.rank)


def _require_positive_int(value: int, message: str) -> None:
    if type(value) is not int or value <= 0:
        raise InvoiceDecisionRuleContractError(message)


def _require_optional_positive_int(value: int | None, message: str) -> None:
    if value is not None:
        _require_positive_int(value, message)


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise InvoiceDecisionRuleContractError("canonical text values must be non-empty when supplied.")
    return value.strip().upper()


def _optional_currency(value: str | None) -> str | None:
    cleaned = _optional_text(value)
    if cleaned is None:
        return None
    if len(cleaned) != 3 or not cleaned.isalpha():
        raise InvoiceDecisionRuleContractError("currency must be a canonical ISO-4217 code.")
    return cleaned


def _positive_int_tuple(values: tuple[int, ...], *, field_name: str) -> tuple[int, ...]:
    if not isinstance(values, tuple):
        raise InvoiceDecisionRuleContractError(f"{field_name} must be an immutable tuple.")
    normalized: set[int] = set()
    for value in values:
        _require_positive_int(value, f"{field_name} must contain positive integers.")
        normalized.add(value)
    return tuple(sorted(normalized))
