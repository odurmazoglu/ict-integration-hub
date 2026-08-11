from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.application.dto import ApplicationDTO
from app.application.exceptions import ApplicationError
from app.application.workflow import WorkflowType

MAX_RULE_CODE_LENGTH = 120
MAX_RULE_NAME_LENGTH = 200
MAX_RULE_TEXT_LENGTH = 500
CLASSIFICATION_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


class InvoiceDecisionRuleContractError(ApplicationError):
    error_category = "invoice_decision_rule_contract_error"


@dataclass(frozen=True, slots=True)
class InvoiceDecisionRulePriority(ApplicationDTO):
    """Deterministic rule priority; lower tier/rank sorts earlier."""

    tier: int
    rank: int = 1000

    def __post_init__(self) -> None:
        _require_non_negative_int(self.tier, "priority tier must be a non-negative integer.")
        _require_non_negative_int(self.rank, "priority rank must be a non-negative integer.")

    def sort_key(self) -> tuple[int, int]:
        return (self.tier, self.rank)


@dataclass(frozen=True, slots=True)
class InvoiceDecisionRuleMatch(ApplicationDTO):
    """Deterministic match conditions for an invoice decision rule."""

    company_id: int | None = None
    vendor_partner_id: int | None = None
    vendor_tax_id: str | None = None
    currency: str | None = None
    provider_document_type: str | None = None
    purchase_order_present: bool | None = None
    description_contains: tuple[str, ...] = field(default_factory=tuple)
    product_mapping_id: int | None = None

    def __post_init__(self) -> None:
        _require_optional_positive_int(self.company_id, "company_id must be a positive integer when supplied.")
        _require_optional_positive_int(
            self.vendor_partner_id,
            "vendor_partner_id must be a positive integer when supplied.",
        )
        _require_optional_positive_int(
            self.product_mapping_id,
            "product_mapping_id must be a positive integer when supplied.",
        )
        vendor_tax_id = _optional_text(self.vendor_tax_id, "vendor_tax_id")
        currency = _optional_currency(self.currency)
        provider_document_type = _optional_text(self.provider_document_type, "provider_document_type")
        if self.purchase_order_present is not None and type(self.purchase_order_present) is not bool:
            raise InvoiceDecisionRuleContractError("purchase_order_present must be boolean when supplied.")
        description_contains = _canonical_text_tuple(
            self.description_contains,
            field_name="description_contains",
            lower=True,
        )
        object.__setattr__(self, "vendor_tax_id", vendor_tax_id)
        object.__setattr__(self, "currency", currency)
        object.__setattr__(self, "provider_document_type", provider_document_type)
        object.__setattr__(self, "description_contains", description_contains)

    @property
    def specificity(self) -> int:
        return sum(
            (
                self.company_id is not None,
                self.vendor_partner_id is not None,
                self.vendor_tax_id is not None,
                self.currency is not None,
                self.provider_document_type is not None,
                self.purchase_order_present is not None,
                bool(self.description_contains),
                self.product_mapping_id is not None,
            )
        )

    def fingerprint(self) -> tuple[Any, ...]:
        return (
            self.company_id,
            self.vendor_partner_id,
            self.vendor_tax_id,
            self.currency,
            self.provider_document_type,
            self.purchase_order_present,
            self.description_contains,
            self.product_mapping_id,
        )


@dataclass(frozen=True, slots=True)
class InvoiceDecisionRuleAction(ApplicationDTO):
    """ERP-neutral action requested by a deterministic invoice decision rule."""

    workflow: WorkflowType | None = None
    classification_code: str | None = None
    default_department_id: int | None = None
    default_analytic_account_id: int | None = None
    require_review: bool = False
    require_business_context: bool = False

    def __post_init__(self) -> None:
        if self.workflow is not None and not isinstance(self.workflow, WorkflowType):
            raise InvoiceDecisionRuleContractError("workflow must be a canonical WorkflowType when supplied.")
        classification_code = _optional_classification_code(self.classification_code)
        _require_optional_positive_int(
            self.default_department_id,
            "default_department_id must be a positive integer when supplied.",
        )
        _require_optional_positive_int(
            self.default_analytic_account_id,
            "default_analytic_account_id must be a positive integer when supplied.",
        )
        _require_bool(self.require_review, "require_review must be boolean.")
        _require_bool(self.require_business_context, "require_business_context must be boolean.")
        if (
            self.workflow is None
            and classification_code is None
            and self.default_department_id is None
            and self.default_analytic_account_id is None
            and self.require_review is False
            and self.require_business_context is False
        ):
            raise InvoiceDecisionRuleContractError("at least one rule action value is required.")
        object.__setattr__(self, "classification_code", classification_code)

    def fingerprint(self) -> tuple[Any, ...]:
        return (
            self.workflow,
            self.classification_code,
            self.default_department_id,
            self.default_analytic_account_id,
            self.require_review,
            self.require_business_context,
        )


@dataclass(frozen=True, slots=True)
class InvoiceDecisionRule(ApplicationDTO):
    """Immutable deterministic invoice decision rule definition."""

    rule_id: str
    rule_code: str
    rule_version: int
    name: str
    enabled: bool
    priority: InvoiceDecisionRulePriority
    match: InvoiceDecisionRuleMatch
    action: InvoiceDecisionRuleAction

    def __post_init__(self) -> None:
        object.__setattr__(self, "rule_id", _required_text(self.rule_id, "rule_id", max_length=MAX_RULE_CODE_LENGTH))
        object.__setattr__(
            self,
            "rule_code",
            _required_text(self.rule_code, "rule_code", max_length=MAX_RULE_CODE_LENGTH).upper(),
        )
        _require_positive_int(self.rule_version, "rule_version must be a positive integer.")
        object.__setattr__(self, "name", _required_text(self.name, "name", max_length=MAX_RULE_NAME_LENGTH))
        _require_bool(self.enabled, "enabled must be boolean.")
        if not isinstance(self.priority, InvoiceDecisionRulePriority):
            raise InvoiceDecisionRuleContractError("priority must be an InvoiceDecisionRulePriority.")
        if not isinstance(self.match, InvoiceDecisionRuleMatch):
            raise InvoiceDecisionRuleContractError("match must be an InvoiceDecisionRuleMatch.")
        if not isinstance(self.action, InvoiceDecisionRuleAction):
            raise InvoiceDecisionRuleContractError("action must be an InvoiceDecisionRuleAction.")

    def precedence_key(self) -> tuple[int, int, int, str, int]:
        return (
            -self.match.specificity,
            self.priority.tier,
            self.priority.rank,
            self.rule_code,
            self.rule_version,
        )

    def behavior_fingerprint(self) -> tuple[Any, ...]:
        return (self.match.fingerprint(), self.action.fingerprint())

    def is_behaviorally_equivalent_to(self, other: InvoiceDecisionRule) -> bool:
        if not isinstance(other, InvoiceDecisionRule):
            raise InvoiceDecisionRuleContractError("other must be an InvoiceDecisionRule.")
        return self.behavior_fingerprint() == other.behavior_fingerprint()


@dataclass(frozen=True, slots=True)
class InvoiceDecisionRuleConflict(ApplicationDTO):
    rule_codes: tuple[str, ...]
    match_fingerprint: tuple[Any, ...]

    def __post_init__(self) -> None:
        rule_codes = _canonical_text_tuple(self.rule_codes, field_name="rule_codes", lower=False)
        if len(rule_codes) < 2:
            raise InvoiceDecisionRuleContractError("conflict requires at least two rule codes.")
        object.__setattr__(self, "rule_codes", rule_codes)
        if not isinstance(self.match_fingerprint, tuple):
            raise InvoiceDecisionRuleContractError("match_fingerprint must be canonical tuple data.")


@dataclass(frozen=True, slots=True)
class InvoiceClassificationResult(ApplicationDTO):
    """Future rule-classification output contract; no evaluator is implemented here."""

    matched_rules: tuple[InvoiceDecisionRule, ...] = field(default_factory=tuple)
    selected_rule: InvoiceDecisionRule | None = None
    conflicts: tuple[InvoiceDecisionRuleConflict, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        matched_rules = tuple(self.matched_rules)
        for rule in matched_rules:
            if not isinstance(rule, InvoiceDecisionRule):
                raise InvoiceDecisionRuleContractError("matched_rules must contain InvoiceDecisionRule values.")
        conflicts = tuple(self.conflicts)
        for conflict in conflicts:
            if not isinstance(conflict, InvoiceDecisionRuleConflict):
                raise InvoiceDecisionRuleContractError("conflicts must contain InvoiceDecisionRuleConflict values.")
        if self.selected_rule is not None and not isinstance(self.selected_rule, InvoiceDecisionRule):
            raise InvoiceDecisionRuleContractError("selected_rule must be an InvoiceDecisionRule when supplied.")
        if self.selected_rule is not None and self.selected_rule not in matched_rules:
            raise InvoiceDecisionRuleContractError("selected_rule must be included in matched_rules.")
        if conflicts and self.selected_rule is not None:
            raise InvoiceDecisionRuleContractError("conflicting classification results must not select a rule.")
        object.__setattr__(self, "matched_rules", matched_rules)
        object.__setattr__(self, "conflicts", conflicts)

    @property
    def classification_code(self) -> str | None:
        if self.selected_rule is None:
            return None
        return self.selected_rule.action.classification_code


def order_invoice_decision_rules(rules: tuple[InvoiceDecisionRule, ...]) -> tuple[InvoiceDecisionRule, ...]:
    """Order enabled rules deterministically without evaluating invoice facts."""

    _validate_rule_tuple(rules)
    return tuple(sorted((rule for rule in rules if rule.enabled), key=lambda rule: rule.precedence_key()))


def find_invoice_decision_rule_conflicts(
    rules: tuple[InvoiceDecisionRule, ...],
) -> tuple[InvoiceDecisionRuleConflict, ...]:
    """Find enabled rules with identical match/priority and different actions."""

    _validate_rule_tuple(rules)
    buckets: dict[tuple[Any, ...], list[InvoiceDecisionRule]] = {}
    for rule in rules:
        if not rule.enabled:
            continue
        key = (rule.priority.sort_key(), rule.match.fingerprint())
        buckets.setdefault(key, []).append(rule)
    conflicts: list[InvoiceDecisionRuleConflict] = []
    for (_priority, match_fingerprint), bucket in buckets.items():
        action_fingerprints = {rule.action.fingerprint() for rule in bucket}
        if len(action_fingerprints) <= 1:
            continue
        conflicts.append(
            InvoiceDecisionRuleConflict(
                rule_codes=tuple(rule.rule_code for rule in sorted(bucket, key=lambda item: item.rule_code)),
                match_fingerprint=match_fingerprint,
            )
        )
    return tuple(sorted(conflicts, key=lambda conflict: conflict.rule_codes))


def _validate_rule_tuple(rules: tuple[InvoiceDecisionRule, ...]) -> None:
    if not isinstance(rules, tuple):
        raise InvoiceDecisionRuleContractError("rules must be supplied as an immutable tuple.")
    for rule in rules:
        if not isinstance(rule, InvoiceDecisionRule):
            raise InvoiceDecisionRuleContractError("rules must contain InvoiceDecisionRule values.")


def _require_positive_int(value: int, message: str) -> None:
    if type(value) is not int or value <= 0:
        raise InvoiceDecisionRuleContractError(message)


def _require_non_negative_int(value: int, message: str) -> None:
    if type(value) is not int or value < 0:
        raise InvoiceDecisionRuleContractError(message)


def _require_optional_positive_int(value: int | None, message: str) -> None:
    if value is not None:
        _require_positive_int(value, message)


def _require_bool(value: bool, message: str) -> None:
    if type(value) is not bool:
        raise InvoiceDecisionRuleContractError(message)


def _required_text(value: str, field_name: str, *, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvoiceDecisionRuleContractError(f"{field_name} is required.")
    cleaned = value.strip()
    if len(cleaned) > max_length:
        raise InvoiceDecisionRuleContractError(f"{field_name} exceeds maximum length.")
    return cleaned


def _optional_text(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field_name, max_length=MAX_RULE_TEXT_LENGTH)


def _optional_currency(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = _required_text(value, "currency", max_length=3).upper()
    if len(cleaned) != 3 or not cleaned.isalpha():
        raise InvoiceDecisionRuleContractError("currency must be a canonical ISO-4217 code.")
    return cleaned


def _optional_classification_code(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = _required_text(value, "classification_code", max_length=64).upper()
    if CLASSIFICATION_CODE_PATTERN.fullmatch(cleaned) is None:
        raise InvoiceDecisionRuleContractError("classification_code must match [A-Z][A-Z0-9_]{0,63} when supplied.")
    return cleaned


def _canonical_text_tuple(values: tuple[str, ...], *, field_name: str, lower: bool) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise InvoiceDecisionRuleContractError(f"{field_name} must be an immutable tuple.")
    cleaned_values: list[str] = []
    for value in values:
        cleaned = _required_text(value, field_name, max_length=MAX_RULE_TEXT_LENGTH)
        cleaned_values.append(cleaned.lower() if lower else cleaned.upper())
    if len(set(cleaned_values)) != len(cleaned_values):
        raise InvoiceDecisionRuleContractError(f"{field_name} values must be unique.")
    return tuple(sorted(cleaned_values))
