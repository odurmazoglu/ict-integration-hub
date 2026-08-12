from __future__ import annotations

from pathlib import Path

import pytest

import app.application as application
from app.application.rules import (
    InvoiceClassificationContext,
    InvoiceClassificationStatus,
    InvoiceDecisionRule,
    InvoiceDecisionRuleAction,
    InvoiceDecisionRuleContractError,
    InvoiceDecisionRuleEngine,
    InvoiceDecisionRuleMatch,
    InvoiceDecisionRulePriority,
)
from app.application.workflow import WorkflowType


def test_exact_company_match_and_mismatch() -> None:
    result = _engine().classify(
        context=_context(company_id=7),
        rules=(_rule(match=InvoiceDecisionRuleMatch(company_id=7)),),
    )

    assert result.status is InvoiceClassificationStatus.MATCHED
    assert result.selected_rule is not None
    assert result.selected_rule.rule_code == "RULE-CLOUD"
    assert _engine().classify(context=_context(company_id=8), rules=(result.selected_rule,)).status is (
        InvoiceClassificationStatus.NO_MATCH
    )


def test_exact_vendor_id_match_and_mismatch() -> None:
    rule = _rule(match=InvoiceDecisionRuleMatch(vendor_partner_id=501))

    assert _engine().classify(context=_context(vendor_partner_id=501), rules=(rule,)).status is (
        InvoiceClassificationStatus.MATCHED
    )
    assert _engine().classify(context=_context(vendor_partner_id=502), rules=(rule,)).status is (
        InvoiceClassificationStatus.NO_MATCH
    )


def test_exact_vendor_tax_currency_and_provider_document_type_match() -> None:
    rule = _rule(
        match=InvoiceDecisionRuleMatch(
            vendor_tax_id="1234567890",
            currency="TRY",
            provider_document_type="E_INVOICE",
        )
    )

    result = _engine().classify(
        context=_context(vendor_tax_id="1234567890", currency="try", provider_document_type="e_invoice"),
        rules=(rule,),
    )

    assert result.status is InvoiceClassificationStatus.MATCHED
    assert _engine().classify(context=_context(vendor_tax_id="0000000000", currency="TRY"), rules=(rule,)).status is (
        InvoiceClassificationStatus.NO_MATCH
    )


@pytest.mark.parametrize("purchase_order_present", [True, False])
def test_purchase_order_present_exact_boolean_match(purchase_order_present: bool) -> None:
    rule = _rule(match=InvoiceDecisionRuleMatch(purchase_order_present=purchase_order_present))

    assert (
        _engine().classify(context=_context(purchase_order_present=purchase_order_present), rules=(rule,)).status
        is InvoiceClassificationStatus.MATCHED
    )
    assert (
        _engine().classify(context=_context(purchase_order_present=not purchase_order_present), rules=(rule,)).status
        is InvoiceClassificationStatus.NO_MATCH
    )


def test_purchase_order_none_rule_condition_is_ignored() -> None:
    rule = _rule(match=InvoiceDecisionRuleMatch(company_id=7, purchase_order_present=None))

    assert _engine().classify(context=_context(company_id=7, purchase_order_present=False), rules=(rule,)).status is (
        InvoiceClassificationStatus.MATCHED
    )


def test_product_mapping_id_presence_and_absence() -> None:
    rule = _rule(match=InvoiceDecisionRuleMatch(product_mapping_id=9001))

    assert _engine().classify(context=_context(product_mapping_ids=(9001, 9002)), rules=(rule,)).status is (
        InvoiceClassificationStatus.MATCHED
    )
    assert _engine().classify(context=_context(product_mapping_ids=(9002,)), rules=(rule,)).status is (
        InvoiceClassificationStatus.NO_MATCH
    )


def test_description_contains_single_and_multiple_required_terms() -> None:
    single = _rule(match=InvoiceDecisionRuleMatch(description_contains=("azure",)))
    multiple = _rule(match=InvoiceDecisionRuleMatch(description_contains=("azure", "consumption")))
    context = InvoiceClassificationContext.from_line_descriptions(
        company_id=7,
        line_descriptions=("Microsoft Azure", "Consumption Services"),
    )

    assert _engine().classify(context=context, rules=(single,)).status is InvoiceClassificationStatus.MATCHED
    assert _engine().classify(context=context, rules=(multiple,)).status is InvoiceClassificationStatus.MATCHED


def test_description_mismatch_and_case_normalization() -> None:
    rule = _rule(match=InvoiceDecisionRuleMatch(description_contains=("AZURE", "COMPUTE")))
    context = InvoiceClassificationContext.from_line_descriptions(
        company_id=7,
        line_descriptions=("microsoft azure", "storage"),
    )

    assert _engine().classify(context=context, rules=(rule,)).status is InvoiceClassificationStatus.NO_MATCH
    assert (
        _engine()
        .classify(
            context=InvoiceClassificationContext.from_line_descriptions(
                company_id=7,
                line_descriptions=("MICROSOFT AZURE", "COMPUTE SERVICES"),
            ),
            rules=(rule,),
        )
        .status
        is InvoiceClassificationStatus.MATCHED
    )


def test_disabled_rule_is_ignored_and_no_match_is_deterministic() -> None:
    disabled = _rule(enabled=False, match=InvoiceDecisionRuleMatch(company_id=7))

    result = _engine().classify(context=_context(company_id=7), rules=(disabled,))

    assert result.status is InvoiceClassificationStatus.NO_MATCH
    assert result.matched_rules == ()
    assert result.classification_code is None


def test_one_winner_preserves_workflow_classification_identity_and_flags() -> None:
    rule = _rule(
        rule_id="odoo:42",
        rule_code="MICROSOFT_AZURE",
        rule_version=3,
        name="Microsoft Azure",
        action=InvoiceDecisionRuleAction(
            workflow=WorkflowType.VENDOR_BILL,
            classification_code="CLOUD_COST",
            require_business_context=True,
        ),
    )

    result = _engine().classify(context=_context(), rules=(rule,))

    assert result.status is InvoiceClassificationStatus.MATCHED
    assert result.workflow is WorkflowType.VENDOR_BILL
    assert result.classification_code == "CLOUD_COST"
    assert result.require_business_context is True
    assert result.matched_rule_evidence[0].rule_id == "odoo:42"
    assert result.matched_rule_evidence[0].rule_code == "MICROSOFT_AZURE"
    assert result.matched_rule_evidence[0].rule_version == 3
    assert result.matched_rule_evidence[0].rule_name == "Microsoft Azure"


def test_equivalent_same_precedence_rules_return_deterministic_equivalent_result() -> None:
    first = _rule(rule_id="odoo:1", rule_code="RULE-A")
    second = _rule(rule_id="odoo:2", rule_code="RULE-B")

    result = _engine().classify(context=_context(), rules=(second, first))

    assert result.status is InvoiceClassificationStatus.MATCHED
    assert result.selected_rule == first
    assert tuple(evidence.rule_code for evidence in result.matched_rule_evidence) == ("RULE-A", "RULE-B")


def test_conflicting_same_precedence_rules_return_conflict_evidence() -> None:
    first = _rule(rule_id="odoo:1", rule_code="RULE-A")
    second = _rule(
        rule_id="odoo:2",
        rule_code="RULE-B",
        action=InvoiceDecisionRuleAction(workflow=WorkflowType.EXPENSE, classification_code="EV_CHARGING"),
    )

    result = _engine().classify(context=_context(), rules=(first, second))

    assert result.status is InvoiceClassificationStatus.CONFLICT
    assert result.selected_rule is None
    assert tuple(evidence.rule_code for evidence in result.conflict_rule_evidence) == ("RULE-A", "RULE-B")
    assert {evidence.workflow for evidence in result.conflict_rule_evidence} == {
        WorkflowType.VENDOR_BILL,
        WorkflowType.EXPENSE,
    }


def test_more_specific_rule_outranks_generic_rule() -> None:
    generic = _rule(rule_code="RULE-GENERIC", match=InvoiceDecisionRuleMatch(vendor_tax_id="1234567890"))
    specific = _rule(
        rule_code="RULE-SPECIFIC",
        match=InvoiceDecisionRuleMatch(company_id=7, vendor_tax_id="1234567890"),
        action=InvoiceDecisionRuleAction(workflow=WorkflowType.EXPENSE, classification_code="EV_CHARGING"),
    )

    result = _engine().classify(context=_context(company_id=7, vendor_tax_id="1234567890"), rules=(generic, specific))

    assert result.status is InvoiceClassificationStatus.MATCHED
    assert result.selected_rule == specific


def test_priority_ordering_is_respected() -> None:
    lower_priority = _rule(rule_code="RULE-LOW", priority=InvoiceDecisionRulePriority(tier=10, rank=0))
    higher_priority = _rule(
        rule_code="RULE-HIGH",
        priority=InvoiceDecisionRulePriority(tier=0, rank=0),
        action=InvoiceDecisionRuleAction(workflow=WorkflowType.EXPENSE, classification_code="EV_CHARGING"),
    )

    result = _engine().classify(context=_context(), rules=(lower_priority, higher_priority))

    assert result.selected_rule == higher_priority


def test_require_review_returns_review_required_without_discarding_match_evidence() -> None:
    rule = _rule(
        rule_code="MICROSOFT_AZURE",
        action=InvoiceDecisionRuleAction(
            workflow=WorkflowType.VENDOR_BILL,
            classification_code="CLOUD_COST",
            require_review=True,
            require_business_context=True,
        ),
    )

    result = _engine().classify(context=_context(), rules=(rule,))

    assert result.status is InvoiceClassificationStatus.REVIEW_REQUIRED
    assert result.classification_code == "CLOUD_COST"
    assert result.workflow is WorkflowType.VENDOR_BILL
    assert result.require_review is True
    assert result.require_business_context is True
    assert result.matched_rule_evidence[0].rule_code == "MICROSOFT_AZURE"


def test_context_rejects_malformed_values() -> None:
    with pytest.raises(InvoiceDecisionRuleContractError):
        _context(company_id=0)
    with pytest.raises(InvoiceDecisionRuleContractError):
        _context(currency="TRY1")
    with pytest.raises(InvoiceDecisionRuleContractError):
        _context(product_mapping_ids=(0,))
    with pytest.raises(InvoiceDecisionRuleContractError):
        _context(purchase_order_present="yes")  # type: ignore[arg-type]


def test_engine_contracts_are_exported() -> None:
    assert application.InvoiceClassificationContext is InvoiceClassificationContext
    assert application.InvoiceDecisionRuleEngine is InvoiceDecisionRuleEngine
    assert application.InvoiceClassificationStatus is InvoiceClassificationStatus


def test_engine_has_no_infrastructure_runtime_ai_or_historical_dependencies() -> None:
    source = Path("app/application/rules/classification.py").read_text().lower()

    assert "app.erp.odoo" not in source
    assert "app.connectors" not in source
    assert "app.persistence" not in source
    assert "sqlalchemy" not in source
    assert "runtime" not in source
    assert "execution" not in source
    assert "openai" not in source
    assert "fuzzy" not in source
    assert "similarity" not in source
    assert "history" not in source
    assert "create(" not in source
    assert "write(" not in source
    assert "unlink(" not in source


def _engine() -> InvoiceDecisionRuleEngine:
    return InvoiceDecisionRuleEngine()


def _context(
    *,
    company_id: int = 7,
    vendor_partner_id: int | None = 501,
    vendor_tax_id: str | None = "1234567890",
    currency: str | None = "TRY",
    provider_document_type: str | None = "E_INVOICE",
    purchase_order_present: bool | None = True,
    description_corpus: str = "microsoft azure cloud consumption",
    product_mapping_ids: tuple[int, ...] = (9001,),
) -> InvoiceClassificationContext:
    return InvoiceClassificationContext(
        company_id=company_id,
        vendor_partner_id=vendor_partner_id,
        vendor_tax_id=vendor_tax_id,
        currency=currency,
        provider_document_type=provider_document_type,
        purchase_order_present=purchase_order_present,
        description_corpus=description_corpus,
        product_mapping_ids=product_mapping_ids,
    )


def _rule(
    *,
    rule_id: str = "odoo:1",
    rule_code: str = "RULE-CLOUD",
    rule_version: int = 1,
    name: str = "Cloud Cost",
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
        match=match or InvoiceDecisionRuleMatch(vendor_tax_id="1234567890", currency="TRY"),
        action=action
        or InvoiceDecisionRuleAction(
            workflow=WorkflowType.VENDOR_BILL,
            classification_code="CLOUD_COST",
            require_business_context=True,
        ),
    )
