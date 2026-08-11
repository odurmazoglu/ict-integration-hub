from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Protocol

import pytest

import app.application as application
from app.application.ports import DecisionRuleRepository
from app.application.rules import (
    ODOO_DECISION_RULE_MODEL,
    InvoiceDecisionRule,
    InvoiceDecisionRuleContractError,
    OdooDecisionRuleAuthoringContractError,
    OdooDecisionRuleAuthoringRecord,
    OdooDecisionRuleFieldMapping,
    odoo_workflow_to_workflow_type,
    validate_unique_odoo_decision_rule_identities,
)
from app.application.workflow import WorkflowType


def test_odoo_decision_rule_field_mapping_is_immutable_and_exported() -> None:
    mapping = OdooDecisionRuleFieldMapping()

    assert application.OdooDecisionRuleFieldMapping is OdooDecisionRuleFieldMapping
    assert application.ODOO_DECISION_RULE_MODEL == ODOO_DECISION_RULE_MODEL
    assert mapping.model_name == "x_ipp_decision_rule"
    assert mapping.workflow == "x_studio_workflow"
    assert mapping.classification_code == "x_studio_classification_code"
    assert mapping.studio_fields() == (
        "x_studio_name",
        "x_studio_rule_code",
        "x_studio_active",
        "x_studio_priority",
        "x_studio_company_id",
        "x_studio_vendor_id",
        "x_studio_vendor_tax_id",
        "x_studio_currency_id",
        "x_studio_description_contains",
        "x_studio_workflow",
        "x_studio_classification_code",
        "x_studio_require_review",
        "x_studio_require_business_context",
        "x_studio_rule_version",
        "x_studio_notes",
    )
    with pytest.raises(FrozenInstanceError):
        mapping.workflow = "x_studio_other"  # type: ignore[misc]


def test_field_mapping_rejects_non_studio_model_fields_and_duplicates() -> None:
    with pytest.raises(OdooDecisionRuleAuthoringContractError):
        OdooDecisionRuleFieldMapping(model_name="account.move")
    with pytest.raises(OdooDecisionRuleAuthoringContractError):
        OdooDecisionRuleFieldMapping(rule_code="rule_code")
    with pytest.raises(OdooDecisionRuleAuthoringContractError):
        OdooDecisionRuleFieldMapping(rule_code="x_studio_name")


def test_odoo_authoring_record_maps_to_canonical_invoice_decision_rule() -> None:
    record = _authoring_record()

    rule = record.to_invoice_decision_rule()

    assert isinstance(rule, InvoiceDecisionRule)
    assert rule.rule_id == "odoo:42"
    assert rule.rule_code == "CLOUD-COST-001"
    assert rule.enabled is True
    assert rule.rule_version == 3
    assert rule.priority.tier == 7
    assert rule.match.company_id == 1
    assert rule.match.vendor_partner_id == 51
    assert rule.match.vendor_tax_id == "1234567890"
    assert rule.match.currency == "TRY"
    assert rule.match.description_contains == ("azure", "cloud")
    assert rule.action.workflow is WorkflowType.VENDOR_BILL
    assert rule.action.classification_code == "CLOUD_COST"
    assert rule.action.require_review is True
    assert rule.action.require_business_context is True


def test_workflow_mapping_is_exact_and_closed() -> None:
    assert odoo_workflow_to_workflow_type("vendor_bill") is WorkflowType.VENDOR_BILL
    assert odoo_workflow_to_workflow_type("VENDOR_BILL") is WorkflowType.VENDOR_BILL
    assert odoo_workflow_to_workflow_type(WorkflowType.EXPENSE) is WorkflowType.EXPENSE

    with pytest.raises(OdooDecisionRuleAuthoringContractError):
        odoo_workflow_to_workflow_type("purchase_order")
    with pytest.raises(OdooDecisionRuleAuthoringContractError):
        odoo_workflow_to_workflow_type("made_up_workflow")


def test_classification_code_validation_comes_from_canonical_rule_contract() -> None:
    rule = _authoring_record(classification_code=" ev_charging ", workflow="expense").to_invoice_decision_rule()

    assert rule.action.workflow is WorkflowType.EXPENSE
    assert rule.action.classification_code == "EV_CHARGING"
    with pytest.raises(InvoiceDecisionRuleContractError):
        _authoring_record(classification_code="EV CHARGING").to_invoice_decision_rule()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"odoo_record_id": 0},
        {"priority": -1},
        {"priority": True},
        {"rule_version": 0},
        {"active": "yes"},
        {"require_review": 1},
        {"company_id": 0},
        {"vendor_partner_id": 1.5},
        {"currency_id": 0},
        {"currency_id": 31, "currency_code": None},
        {"currency_id": None, "currency_code": "TRY"},
        {"description_contains": ["cloud"]},
        {"name": ""},
        {"rule_code": " "},
    ],
)
def test_malformed_odoo_authoring_values_fail_closed(kwargs: dict[str, object]) -> None:
    with pytest.raises(OdooDecisionRuleAuthoringContractError):
        _authoring_record(**kwargs)


def test_duplicate_rule_code_and_version_rejected() -> None:
    first = _authoring_record(odoo_record_id=42, rule_code="RULE-A", rule_version=2).to_invoice_decision_rule()
    duplicate = _authoring_record(odoo_record_id=43, rule_code=" rule-a ", rule_version=2).to_invoice_decision_rule()
    next_version = _authoring_record(odoo_record_id=44, rule_code="RULE-A", rule_version=3).to_invoice_decision_rule()

    with pytest.raises(OdooDecisionRuleAuthoringContractError):
        validate_unique_odoo_decision_rule_identities((first, duplicate))
    validate_unique_odoo_decision_rule_identities((first, next_version))


def test_decision_rule_repository_port_returns_canonical_rules_only() -> None:
    class FakeDecisionRuleRepository:
        def list_invoice_decision_rules(self, *, company_id: int) -> tuple[InvoiceDecisionRule, ...]:
            assert company_id == 1
            return (_authoring_record(company_id=1).to_invoice_decision_rule(),)

    repository: DecisionRuleRepository = FakeDecisionRuleRepository()
    rules = repository.list_invoice_decision_rules(company_id=1)

    assert application.DecisionRuleRepository is DecisionRuleRepository
    assert isinstance(rules, tuple)
    assert isinstance(rules[0], InvoiceDecisionRule)
    assert not isinstance(rules[0], dict)


def test_repository_contract_exposes_no_write_cache_or_raw_odoo_methods() -> None:
    methods = set(DecisionRuleRepository.__dict__)

    assert "list_invoice_decision_rules" in methods
    assert "save" not in methods
    assert "append" not in methods
    assert "cache" not in methods
    assert "search_read" not in methods
    assert "read" not in methods


def test_decision_rule_repository_is_an_application_protocol() -> None:
    assert issubclass(DecisionRuleRepository, Protocol)


def test_authoring_contract_sources_have_no_infrastructure_ai_or_fuzzy_dependencies() -> None:
    sources = "\n".join(
        Path(path).read_text().lower()
        for path in (
            "app/application/rules/odoo_authoring.py",
            "app/application/ports/decision_rule_repository.py",
        )
    )

    assert "sqlalchemy" not in sources
    assert "app.models" not in sources
    assert "app.connectors" not in sources
    assert "jsonrpc" not in sources
    assert "xmlrpc" not in sources
    assert "search_read" not in sources
    assert "http" not in sources
    assert "openai" not in sources
    assert "fuzzy" not in sources
    assert "similarity" not in sources


def _authoring_record(**overrides: object) -> OdooDecisionRuleAuthoringRecord:
    values: dict[str, object] = {
        "odoo_record_id": 42,
        "name": "Cloud cost rule",
        "rule_code": " cloud-cost-001 ",
        "active": True,
        "priority": 7,
        "rule_version": 3,
        "workflow": "vendor_bill",
        "classification_code": " cloud_cost ",
        "company_id": 1,
        "vendor_partner_id": 51,
        "vendor_tax_id": " 1234567890 ",
        "currency_id": 31,
        "currency_code": "try",
        "description_contains": (" Cloud ", "Azure"),
        "require_review": True,
        "require_business_context": True,
        "notes": "Configured in Odoo.",
    }
    values.update(overrides)
    return OdooDecisionRuleAuthoringRecord(**values)  # type: ignore[arg-type]
