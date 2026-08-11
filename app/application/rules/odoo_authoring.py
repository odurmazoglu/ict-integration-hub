from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from app.application.dto import ApplicationDTO
from app.application.exceptions import ApplicationError
from app.application.rules.contracts import (
    InvoiceDecisionRule,
    InvoiceDecisionRuleAction,
    InvoiceDecisionRuleMatch,
    InvoiceDecisionRulePriority,
)
from app.application.workflow import WorkflowType

ODOO_DECISION_RULE_MODEL: Final[str] = "x_ipp_decision_rule"


class OdooDecisionRuleAuthoringContractError(ApplicationError):
    error_category = "odoo_decision_rule_authoring_contract_error"


@dataclass(frozen=True, slots=True)
class OdooDecisionRuleFieldMapping(ApplicationDTO):
    """Centralized Odoo Studio field names for authored decision rules."""

    model_name: str = ODOO_DECISION_RULE_MODEL
    name: str = "x_studio_name"
    rule_code: str = "x_studio_rule_code"
    active: str = "x_studio_active"
    priority: str = "x_studio_priority"
    company: str = "x_studio_company_id"
    vendor: str = "x_studio_vendor_id"
    vendor_tax_id: str = "x_studio_vendor_tax_id"
    currency: str = "x_studio_currency_id"
    description_contains: str = "x_studio_description_contains"
    workflow: str = "x_studio_workflow"
    classification_code: str = "x_studio_classification_code"
    require_review: str = "x_studio_require_review"
    require_business_context: str = "x_studio_require_business_context"
    rule_version: str = "x_studio_rule_version"
    notes: str = "x_studio_notes"

    def __post_init__(self) -> None:
        model_name = _required_model_name(self.model_name)
        object.__setattr__(self, "model_name", model_name)
        seen: set[str] = set()
        for field_name in self.studio_fields():
            if field_name in seen:
                raise OdooDecisionRuleAuthoringContractError("Odoo decision rule field names must be unique.")
            seen.add(field_name)

    def studio_fields(self) -> tuple[str, ...]:
        fields = (
            self.name,
            self.rule_code,
            self.active,
            self.priority,
            self.company,
            self.vendor,
            self.vendor_tax_id,
            self.currency,
            self.description_contains,
            self.workflow,
            self.classification_code,
            self.require_review,
            self.require_business_context,
            self.rule_version,
            self.notes,
        )
        return tuple(_required_studio_field_name(field_name) for field_name in fields)


@dataclass(frozen=True, slots=True)
class OdooDecisionRuleAuthoringRecord(ApplicationDTO):
    """Canonical immutable representation of one Odoo-authored rule row."""

    odoo_record_id: int
    name: str
    rule_code: str
    active: bool
    priority: int
    rule_version: int
    workflow: str | WorkflowType
    classification_code: str | None = None
    company_id: int | None = None
    vendor_partner_id: int | None = None
    vendor_tax_id: str | None = None
    currency_id: int | None = None
    currency_code: str | None = None
    description_contains: tuple[str, ...] = ()
    require_review: bool = False
    require_business_context: bool = False
    notes: str | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.odoo_record_id, "odoo_record_id must be a positive integer.")
        _require_non_negative_int(self.priority, "priority must be a non-negative integer.")
        _require_positive_int(self.rule_version, "rule_version must be a positive integer.")
        _require_bool(self.active, "active must be boolean.")
        _require_bool(self.require_review, "require_review must be boolean.")
        _require_bool(self.require_business_context, "require_business_context must be boolean.")
        _require_optional_positive_int(self.company_id, "company_id must be a positive integer when supplied.")
        _require_optional_positive_int(
            self.vendor_partner_id,
            "vendor_partner_id must be a positive integer when supplied.",
        )
        _require_optional_positive_int(self.currency_id, "currency_id must be a positive integer when supplied.")
        if (self.currency_id is None) != (self.currency_code is None):
            raise OdooDecisionRuleAuthoringContractError(
                "currency_id and canonical currency_code must be supplied together."
            )
        object.__setattr__(self, "name", _required_text(self.name, "name"))
        object.__setattr__(self, "rule_code", _required_text(self.rule_code, "rule_code").upper())
        object.__setattr__(self, "workflow", odoo_workflow_to_workflow_type(self.workflow))
        if self.notes is not None:
            object.__setattr__(self, "notes", _required_text(self.notes, "notes"))
        if not isinstance(self.description_contains, tuple):
            raise OdooDecisionRuleAuthoringContractError("description_contains must be an immutable tuple.")
        for term in self.description_contains:
            _required_text(term, "description_contains")

    def to_invoice_decision_rule(self) -> InvoiceDecisionRule:
        return InvoiceDecisionRule(
            rule_id=f"odoo:{self.odoo_record_id}",
            rule_code=self.rule_code,
            rule_version=self.rule_version,
            name=self.name,
            enabled=self.active,
            priority=InvoiceDecisionRulePriority(tier=self.priority),
            match=InvoiceDecisionRuleMatch(
                company_id=self.company_id,
                vendor_partner_id=self.vendor_partner_id,
                vendor_tax_id=self.vendor_tax_id,
                currency=self.currency_code,
                description_contains=self.description_contains,
            ),
            action=InvoiceDecisionRuleAction(
                workflow=self.workflow,
                classification_code=self.classification_code,
                require_review=self.require_review,
                require_business_context=self.require_business_context,
            ),
        )


def validate_unique_odoo_decision_rule_identities(rules: tuple[InvoiceDecisionRule, ...]) -> None:
    if not isinstance(rules, tuple):
        raise OdooDecisionRuleAuthoringContractError("rules must be supplied as an immutable tuple.")
    seen: set[tuple[str, int]] = set()
    for rule in rules:
        if not isinstance(rule, InvoiceDecisionRule):
            raise OdooDecisionRuleAuthoringContractError("rules must contain InvoiceDecisionRule values.")
        identity = (rule.rule_code, rule.rule_version)
        if identity in seen:
            raise OdooDecisionRuleAuthoringContractError("duplicate Rule Code + Rule Version is not allowed.")
        seen.add(identity)


def odoo_workflow_to_workflow_type(value: str | WorkflowType) -> WorkflowType:
    if isinstance(value, WorkflowType):
        return value
    if not isinstance(value, str):
        raise OdooDecisionRuleAuthoringContractError("workflow must be mapped from canonical text.")
    cleaned = value.strip().upper()
    for workflow in WorkflowType:
        if cleaned in {workflow.name, workflow.value.upper()}:
            return workflow
    raise OdooDecisionRuleAuthoringContractError("workflow must map exactly to a supported WorkflowType.")


def _required_model_name(value: str) -> str:
    cleaned = _required_text(value, "model_name")
    if not cleaned.startswith("x_"):
        raise OdooDecisionRuleAuthoringContractError("Odoo decision rule model must be a Studio/custom model.")
    return cleaned


def _required_studio_field_name(value: str) -> str:
    cleaned = _required_text(value, "field_name")
    if not cleaned.startswith("x_studio_"):
        raise OdooDecisionRuleAuthoringContractError("Odoo decision rule fields must be centralized Studio fields.")
    return cleaned


def _required_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OdooDecisionRuleAuthoringContractError(f"{field_name} is required.")
    return value.strip()


def _require_positive_int(value: int, message: str) -> None:
    if type(value) is not int or value <= 0:
        raise OdooDecisionRuleAuthoringContractError(message)


def _require_non_negative_int(value: int, message: str) -> None:
    if type(value) is not int or value < 0:
        raise OdooDecisionRuleAuthoringContractError(message)


def _require_optional_positive_int(value: int | None, message: str) -> None:
    if value is not None:
        _require_positive_int(value, message)


def _require_bool(value: bool, message: str) -> None:
    if type(value) is not bool:
        raise OdooDecisionRuleAuthoringContractError(message)
