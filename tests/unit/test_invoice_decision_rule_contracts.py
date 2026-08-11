from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

import app.application as application
from app.application.rules import (
    InvoiceClassificationResult,
    InvoiceDecisionRule,
    InvoiceDecisionRuleAction,
    InvoiceDecisionRuleConflict,
    InvoiceDecisionRuleContractError,
    InvoiceDecisionRuleMatch,
    InvoiceDecisionRulePriority,
    find_invoice_decision_rule_conflicts,
    order_invoice_decision_rules,
)
from app.application.workflow import WorkflowType


def test_invoice_decision_rule_contracts_are_exported_and_immutable() -> None:
    rule = _rule()

    assert application.InvoiceDecisionRule is InvoiceDecisionRule
    assert application.InvoiceDecisionRuleMatch is InvoiceDecisionRuleMatch
    assert application.InvoiceDecisionRuleAction is InvoiceDecisionRuleAction
    assert application.InvoiceDecisionRulePriority is InvoiceDecisionRulePriority
    assert application.InvoiceClassificationResult is InvoiceClassificationResult
    with pytest.raises(FrozenInstanceError):
        rule.enabled = False


def test_enabled_rules_are_ordered_by_specificity_priority_and_identity() -> None:
    generic = _rule(
        rule_code="RULE-GENERIC",
        match=InvoiceDecisionRuleMatch(vendor_tax_id="1234567890"),
        priority=InvoiceDecisionRulePriority(tier=10, rank=10),
    )
    company_exact = _rule(
        rule_code="RULE-COMPANY",
        match=InvoiceDecisionRuleMatch(company_id=7, vendor_tax_id="1234567890"),
        priority=InvoiceDecisionRulePriority(tier=10, rank=10),
    )
    disabled = _rule(
        rule_code="RULE-DISABLED",
        enabled=False,
        match=InvoiceDecisionRuleMatch(company_id=7, vendor_tax_id="1234567890", currency="TRY"),
        priority=InvoiceDecisionRulePriority(tier=0, rank=0),
    )
    higher_priority = _rule(
        rule_code="RULE-PRIORITY",
        match=InvoiceDecisionRuleMatch(company_id=7, vendor_tax_id="1234567890"),
        priority=InvoiceDecisionRulePriority(tier=0, rank=0),
    )

    ordered = order_invoice_decision_rules((generic, company_exact, disabled, higher_priority))

    assert ordered == (higher_priority, company_exact, generic)


def test_exact_vendor_tax_company_currency_and_description_conditions_are_canonical() -> None:
    match = InvoiceDecisionRuleMatch(
        company_id=7,
        vendor_partner_id=501,
        vendor_tax_id=" 1234567890 ",
        currency="try",
        provider_document_type="E_INVOICE",
        purchase_order_present=True,
        description_contains=(" Cloud Service ", "subscription"),
        product_mapping_id=9001,
    )

    assert match.company_id == 7
    assert match.vendor_partner_id == 501
    assert match.vendor_tax_id == "1234567890"
    assert match.currency == "TRY"
    assert match.provider_document_type == "E_INVOICE"
    assert match.purchase_order_present is True
    assert match.description_contains == ("cloud service", "subscription")
    assert match.product_mapping_id == 9001
    assert match.specificity == 8


def test_company_isolation_prevents_same_vendor_tax_rules_from_conflicting() -> None:
    company_7 = _rule(rule_code="RULE-C7", match=InvoiceDecisionRuleMatch(company_id=7, vendor_tax_id="1234567890"))
    company_8 = _rule(rule_code="RULE-C8", match=InvoiceDecisionRuleMatch(company_id=8, vendor_tax_id="1234567890"))

    assert find_invoice_decision_rule_conflicts((company_7, company_8)) == ()


def test_conflicting_enabled_rules_with_same_match_and_priority_fail_closed_contract() -> None:
    first = _rule(rule_code="RULE-A", action=InvoiceDecisionRuleAction(workflow=WorkflowType.VENDOR_BILL))
    second = _rule(rule_code="RULE-B", action=InvoiceDecisionRuleAction(workflow=WorkflowType.MANUAL_REVIEW))

    conflicts = find_invoice_decision_rule_conflicts((first, second))

    assert conflicts == (
        InvoiceDecisionRuleConflict(
            rule_codes=("RULE-A", "RULE-B"),
            match_fingerprint=first.match.fingerprint(),
        ),
    )


def test_workflow_and_business_classification_are_independent() -> None:
    ev_charging_expense = InvoiceDecisionRuleAction(
        workflow=WorkflowType.EXPENSE,
        classification_code="EV_CHARGING",
    )
    cloud_cost_vendor_bill = InvoiceDecisionRuleAction(
        workflow=WorkflowType.VENDOR_BILL,
        classification_code="CLOUD_COST",
    )

    assert ev_charging_expense.workflow is WorkflowType.EXPENSE
    assert ev_charging_expense.classification_code == "EV_CHARGING"
    assert cloud_cost_vendor_bill.workflow is WorkflowType.VENDOR_BILL
    assert cloud_cost_vendor_bill.classification_code == "CLOUD_COST"


def test_new_valid_business_classification_requires_no_enum_change() -> None:
    action = InvoiceDecisionRuleAction(
        workflow=WorkflowType.EXPENSE,
        classification_code="OFFICE_RENT",
    )

    assert action.classification_code == "OFFICE_RENT"


def test_classification_code_canonicalizes_case_and_outer_whitespace() -> None:
    action = InvoiceDecisionRuleAction(
        workflow=WorkflowType.VENDOR_BILL,
        classification_code=" cloud_cost ",
    )

    assert action.classification_code == "CLOUD_COST"


@pytest.mark.parametrize(
    "classification_code",
    [
        "",
        " ",
        "1CLOUD_COST",
        "_CLOUD_COST",
        "CLOUD-COST",
        "CLOUD COST",
        "CLOUD.COST",
        "A" * 65,
    ],
)
def test_malformed_classification_codes_are_rejected(classification_code: str) -> None:
    with pytest.raises(InvoiceDecisionRuleContractError):
        InvoiceDecisionRuleAction(workflow=WorkflowType.VENDOR_BILL, classification_code=classification_code)


def test_classification_participates_in_deterministic_fingerprints() -> None:
    cloud = InvoiceDecisionRuleAction(workflow=WorkflowType.VENDOR_BILL, classification_code="CLOUD_COST")
    utility = InvoiceDecisionRuleAction(workflow=WorkflowType.VENDOR_BILL, classification_code="OFFICE_UTILITY")

    assert cloud.fingerprint() != utility.fingerprint()


def test_same_workflow_with_different_classification_is_not_equivalent() -> None:
    first = _rule(
        rule_id="rule-1",
        rule_code="RULE-A",
        action=InvoiceDecisionRuleAction(workflow=WorkflowType.EXPENSE, classification_code="EV_CHARGING"),
    )
    second = _rule(
        rule_id="rule-2",
        rule_code="RULE-B",
        action=InvoiceDecisionRuleAction(workflow=WorkflowType.EXPENSE, classification_code="OFFICE_UTILITY"),
    )

    assert first.is_behaviorally_equivalent_to(second) is False


def test_same_classification_with_different_workflow_is_not_equivalent() -> None:
    first = _rule(
        rule_id="rule-1",
        rule_code="RULE-A",
        action=InvoiceDecisionRuleAction(workflow=WorkflowType.EXPENSE, classification_code="CLOUD_COST"),
    )
    second = _rule(
        rule_id="rule-2",
        rule_code="RULE-B",
        action=InvoiceDecisionRuleAction(workflow=WorkflowType.VENDOR_BILL, classification_code="CLOUD_COST"),
    )

    assert first.is_behaviorally_equivalent_to(second) is False


def test_workflow_type_remains_closed() -> None:
    with pytest.raises(InvoiceDecisionRuleContractError):
        InvoiceDecisionRuleAction(workflow="EXPENSE", classification_code="EV_CHARGING")  # type: ignore[arg-type]


def test_equivalent_rule_behaviour_is_stable_and_not_a_conflict() -> None:
    first = _rule(rule_id="rule-1", rule_code="RULE-A")
    second = _rule(rule_id="rule-2", rule_code="RULE-B")

    assert first.is_behaviorally_equivalent_to(second) is True
    assert first.behavior_fingerprint() == second.behavior_fingerprint()
    assert find_invoice_decision_rule_conflicts((first, second)) == ()


def test_disabled_rules_do_not_participate_in_conflict_detection() -> None:
    first = _rule(rule_code="RULE-A", action=InvoiceDecisionRuleAction(workflow=WorkflowType.VENDOR_BILL))
    disabled_conflict = _rule(
        rule_code="RULE-B",
        enabled=False,
        action=InvoiceDecisionRuleAction(workflow=WorkflowType.MANUAL_REVIEW),
    )

    assert find_invoice_decision_rule_conflicts((first, disabled_conflict)) == ()


def test_invoice_classification_result_contract_does_not_classify_by_itself() -> None:
    rule = _rule()
    result = InvoiceClassificationResult(matched_rules=(rule,), selected_rule=rule)

    assert result.matched_rules == (rule,)
    assert result.selected_rule == rule
    assert result.classification_code == "CLOUD_COST"
    assert result.conflicts == ()
    with pytest.raises(InvoiceDecisionRuleContractError):
        InvoiceClassificationResult(matched_rules=(), selected_rule=rule)
    with pytest.raises(InvoiceDecisionRuleContractError):
        InvoiceClassificationResult(
            matched_rules=(rule,),
            selected_rule=rule,
            conflicts=(InvoiceDecisionRuleConflict(rule_codes=("RULE-A", "RULE-B"), match_fingerprint=()),),
        )


@pytest.mark.parametrize(
    "builder",
    [
        lambda: InvoiceDecisionRulePriority(tier=1.5),  # type: ignore[arg-type]
        lambda: InvoiceDecisionRulePriority(tier=True),  # type: ignore[arg-type]
        lambda: InvoiceDecisionRuleMatch(company_id=0),
        lambda: InvoiceDecisionRuleMatch(vendor_partner_id=1.5),  # type: ignore[arg-type]
        lambda: InvoiceDecisionRuleMatch(currency="TRY1"),
        lambda: InvoiceDecisionRuleMatch(purchase_order_present="yes"),  # type: ignore[arg-type]
        lambda: InvoiceDecisionRuleMatch(description_contains=["cloud"]),  # type: ignore[arg-type]
        lambda: InvoiceDecisionRuleMatch(description_contains=("cloud", "Cloud")),
        lambda: InvoiceDecisionRuleAction(default_department_id=1.5),  # type: ignore[arg-type]
        lambda: InvoiceDecisionRuleAction(require_review=1),  # type: ignore[arg-type]
        lambda: InvoiceDecisionRuleAction(classification_code=1),  # type: ignore[arg-type]
        lambda: InvoiceDecisionRuleAction(),
        lambda: _rule(rule_version=0),
        lambda: _rule(enabled="yes"),  # type: ignore[arg-type]
        lambda: _rule(rule_code=""),
    ],
)
def test_malformed_rule_values_are_rejected(builder: object) -> None:
    with pytest.raises(InvoiceDecisionRuleContractError):
        builder()


def test_rule_contract_source_has_no_floats_ai_fuzzy_or_infrastructure_imports() -> None:
    source = Path("app/application/rules/contracts.py").read_text().lower()

    assert "float" not in source
    assert "openai" not in source
    assert "fuzzy" not in source
    assert "similarity" not in source
    assert "app.connectors" not in source
    assert "app.erp" not in source
    assert "app.models" not in source
    assert "sqlalchemy" not in source
    assert "odoo" not in source


def _rule(
    *,
    rule_id: str = "rule-1",
    rule_code: str = "RULE-VENDOR-TAX-001",
    rule_version: int = 1,
    name: str = "Vendor tax direct bill",
    enabled: bool = True,
    priority: InvoiceDecisionRulePriority | None = None,
    match: InvoiceDecisionRuleMatch | None = None,
    action: InvoiceDecisionRuleAction | None = None,
) -> InvoiceDecisionRule:
    return InvoiceDecisionRule(
        rule_id=rule_id,
        rule_code=rule_code,
        rule_version=rule_version,
        name=name,
        enabled=enabled,
        priority=priority or InvoiceDecisionRulePriority(tier=10, rank=100),
        match=match or InvoiceDecisionRuleMatch(company_id=7, vendor_tax_id="1234567890", currency="TRY"),
        action=action
        or InvoiceDecisionRuleAction(
            workflow=WorkflowType.VENDOR_BILL,
            classification_code="CLOUD_COST",
            require_business_context=True,
        ),
    )
