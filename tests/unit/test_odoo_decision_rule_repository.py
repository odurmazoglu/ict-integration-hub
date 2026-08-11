from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.application.rules import OdooDecisionRuleFieldMapping
from app.application.workbench import CurrencyReference
from app.application.workflow import WorkflowType
from app.erp.odoo import OdooDecisionRuleDataError, OdooDecisionRuleRepository


def test_exact_company_specific_rule_read() -> None:
    adapter = FakeReadOnlyAdapter([_record(company=[7, "Wrong Display Company"])])
    repository = _repository(adapter=adapter)

    rules = repository.list_invoice_decision_rules(company_id=7)

    assert len(rules) == 1
    assert rules[0].match.company_id == 7
    assert adapter.calls == [
        {
            "model": "x_ipp_decision_rule",
            "domain": [
                "&",
                ["active", "=", True],
                "|",
                ["company_id", "=", 7],
                ["company_id", "=", False],
            ],
            "fields": ("id", *OdooDecisionRuleFieldMapping().studio_fields()),
        }
    ]


def test_shared_global_rule_is_included_when_company_empty() -> None:
    repository = _repository(records=[_record(company=False)])

    rules = repository.list_invoice_decision_rules(company_id=7)

    assert len(rules) == 1
    assert rules[0].match.company_id is None


def test_other_company_rule_is_excluded() -> None:
    repository = _repository(records=[_record(company=[8, "Other Company"])])

    assert repository.list_invoice_decision_rules(company_id=7) == ()


def test_inactive_rule_is_excluded() -> None:
    repository = _repository(records=[_record(active=False)])

    assert repository.list_invoice_decision_rules(company_id=7) == ()


def test_many2one_ids_are_parsed_by_id_only() -> None:
    repository = _repository(
        records=[
            _record(
                company=[7, "Display Company Must Be Ignored"],
                vendor=[51, "Display Vendor Must Be Ignored"],
                product_mapping=[9001, "Display Product Mapping Must Be Ignored"],
            )
        ]
    )

    rule = repository.list_invoice_decision_rules(company_id=7)[0]

    assert rule.match.company_id == 7
    assert rule.match.vendor_partner_id == 51
    assert rule.match.product_mapping_id == 9001


def test_currency_id_parsed_by_id_and_display_label_ignored() -> None:
    currency_repository = FakeCurrencyRepository({31: CurrencyReference(id=31, code="TRY", active=True)})
    repository = _repository(
        records=[_record(currency=[31, "Display Label Is Not Canonical"])],
        currency_repository=currency_repository,
    )

    rule = repository.list_invoice_decision_rules(company_id=7)[0]

    assert currency_repository.calls == [(31,)]
    assert rule.match.currency == "TRY"


def test_unknown_or_inactive_currency_fails_closed() -> None:
    with pytest.raises(OdooDecisionRuleDataError):
        _repository(records=[_record(currency=[999, "Missing"])]).list_invoice_decision_rules(company_id=7)

    inactive_currency_repository = FakeCurrencyRepository({31: CurrencyReference(id=31, code="TRY", active=False)})
    with pytest.raises(OdooDecisionRuleDataError):
        _repository(currency_repository=inactive_currency_repository).list_invoice_decision_rules(company_id=7)


def test_workflow_mapped_from_canonical_stored_value_and_invalid_workflow_rejected() -> None:
    assert (
        _repository(records=[_record(workflow="vendor_bill")])
        .list_invoice_decision_rules(company_id=7)[0]
        .action.workflow
        is WorkflowType.VENDOR_BILL
    )

    with pytest.raises(OdooDecisionRuleDataError):
        _repository(records=[_record(workflow="Purchase Order")]).list_invoice_decision_rules(company_id=7)


def test_classification_code_preserved_without_enum_lookup() -> None:
    rule = _repository(records=[_record(classification_code=" cloud_cost ")]).list_invoice_decision_rules(company_id=7)[
        0
    ]

    assert rule.action.classification_code == "CLOUD_COST"


def test_priority_and_version_are_parsed_exactly() -> None:
    rule = _repository(records=[_record(priority="7", rule_version="3")]).list_invoice_decision_rules(company_id=7)[0]

    assert rule.priority.tier == 7
    assert rule.rule_version == 3


@pytest.mark.parametrize("priority", [1.5, True, "1.5", "-1", "abc"])
def test_malformed_priority_rejected(priority: object) -> None:
    with pytest.raises(OdooDecisionRuleDataError):
        _repository(records=[_record(priority=priority)]).list_invoice_decision_rules(company_id=7)


@pytest.mark.parametrize("rule_version", [0, 1.5, True, "1.5", "abc"])
def test_malformed_version_rejected(rule_version: object) -> None:
    with pytest.raises(OdooDecisionRuleDataError):
        _repository(records=[_record(rule_version=rule_version)]).list_invoice_decision_rules(company_id=7)


@pytest.mark.parametrize(
    ("stored_value", "expected"),
    [
        ("any", None),
        ("required", True),
        ("must_not_exist", False),
        (False, None),
    ],
)
def test_purchase_order_presence_tri_state_mapping(stored_value: object, expected: bool | None) -> None:
    rule = _repository(records=[_record(purchase_order_presence=stored_value)]).list_invoice_decision_rules(
        company_id=7
    )[0]

    assert rule.match.purchase_order_present is expected


def test_unknown_purchase_order_presence_rejected() -> None:
    with pytest.raises(OdooDecisionRuleDataError):
        _repository(records=[_record(purchase_order_presence="Required")]).list_invoice_decision_rules(company_id=7)


def test_description_terms_parse_from_newline_format_only() -> None:
    rule = _repository(records=[_record(description="Azure\n\nConsumption  ")]).list_invoice_decision_rules(
        company_id=7
    )[0]

    assert rule.match.description_contains == ("azure", "consumption")


def test_no_fuzzy_ai_or_comma_tokenization_for_description_terms() -> None:
    rule = _repository(records=[_record(description="Azure, Consumption")]).list_invoice_decision_rules(company_id=7)[0]

    assert rule.match.description_contains == ("azure, consumption",)


def test_provider_document_type_preserved_canonically() -> None:
    rule = _repository(records=[_record(provider_document_type=" e_archive ")]).list_invoice_decision_rules(
        company_id=7
    )[0]

    assert rule.match.provider_document_type == "E_ARCHIVE"


def test_duplicate_rule_code_and_version_rejected() -> None:
    records = [
        _record(odoo_id=1, rule_code="RULE-A", rule_version=2),
        _record(odoo_id=2, rule_code=" rule-a ", rule_version=2),
    ]

    with pytest.raises(OdooDecisionRuleDataError):
        _repository(records=records).list_invoice_decision_rules(company_id=7)


def test_deterministic_ordering_does_not_depend_on_odoo_row_order() -> None:
    generic = _record(odoo_id=1, rule_code="RULE-GENERIC", company=False, priority=10)
    specific = _record(odoo_id=2, rule_code="RULE-SPECIFIC", company=[7, "Company"], priority=10)
    high_priority = _record(odoo_id=3, rule_code="RULE-HIGH", company=[7, "Company"], priority=1)

    rules = _repository(records=[generic, specific, high_priority]).list_invoice_decision_rules(company_id=7)

    assert tuple(rule.rule_code for rule in rules) == ("RULE-HIGH", "RULE-SPECIFIC", "RULE-GENERIC")


def test_raw_odoo_dictionaries_do_not_escape_adapter() -> None:
    rules = _repository().list_invoice_decision_rules(company_id=7)

    assert isinstance(rules, tuple)
    assert not isinstance(rules[0], dict)


def test_no_odoo_write_methods_invoked() -> None:
    adapter = FakeReadOnlyAdapter([_record()])
    _repository(adapter=adapter).list_invoice_decision_rules(company_id=7)

    assert adapter.write_calls == []


def test_no_rule_evaluation_or_runtime_dependencies() -> None:
    source = Path("app/erp/odoo/decision_rule_repository.py").read_text().lower()

    assert "evaluate(" not in source
    assert "decisionengine" not in source
    assert "internalinvoice" not in source
    assert "runtime" not in source
    assert "execution" not in source
    assert "workbench" not in source.replace("app.application.workbench import currencyreferencerepository", "")
    assert "create(" not in source
    assert "write(" not in source
    assert "unlink(" not in source
    assert "openai" not in source
    assert "fuzzy" not in source
    assert "similarity" not in source


class FakeReadOnlyAdapter:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self.records = records
        self.calls: list[dict[str, Any]] = []
        self.write_calls: list[str] = []

    def search_read_all(
        self,
        *,
        model: str,
        domain: list[Any],
        fields: list[str],
    ) -> tuple[dict[str, Any], ...]:
        self.calls.append({"model": model, "domain": domain, "fields": tuple(fields)})
        return tuple(self.records)

    def create(self, *_args: object, **_kwargs: object) -> None:
        self.write_calls.append("create")
        raise AssertionError("write method must not be called")

    def write(self, *_args: object, **_kwargs: object) -> None:
        self.write_calls.append("write")
        raise AssertionError("write method must not be called")

    def unlink(self, *_args: object, **_kwargs: object) -> None:
        self.write_calls.append("unlink")
        raise AssertionError("write method must not be called")


class FakeCurrencyRepository:
    def __init__(self, currencies: dict[int, CurrencyReference] | None = None) -> None:
        self.currencies = currencies or {31: CurrencyReference(id=31, code="TRY", active=True)}
        self.calls: list[tuple[int, ...]] = []

    def find_currencies_by_ids(self, ids: tuple[int, ...]) -> tuple[CurrencyReference, ...]:
        self.calls.append(ids)
        return tuple(self.currencies[currency_id] for currency_id in ids if currency_id in self.currencies)

    def find_currencies_by_codes(self, codes: tuple[str, ...]) -> tuple[CurrencyReference, ...]:
        raise AssertionError("currency lookup must use exact IDs")


def _repository(
    *,
    records: list[dict[str, Any]] | None = None,
    adapter: FakeReadOnlyAdapter | None = None,
    currency_repository: FakeCurrencyRepository | None = None,
) -> OdooDecisionRuleRepository:
    return OdooDecisionRuleRepository(
        adapter=adapter or FakeReadOnlyAdapter(records or [_record()]),  # type: ignore[arg-type]
        mapping=OdooDecisionRuleFieldMapping(),
        currency_repository=currency_repository or FakeCurrencyRepository(),
    )


def _record(
    *,
    odoo_id: object = 42,
    name: object = "Cloud rule",
    rule_code: object = "CLOUD-001",
    active: object = True,
    priority: object = 7,
    company: object = [7, "Company Display"],
    vendor: object = [51, "Vendor Display"],
    vendor_tax_id: object = "1234567890",
    currency: object = [31, "Bad Display Currency"],
    provider_document_type: object = "e_invoice",
    purchase_order_presence: object = "required",
    description: object = "Cloud\nAzure",
    product_mapping: object = [9001, "Product Mapping Display"],
    workflow: object = "vendor_bill",
    classification_code: object = "cloud_cost",
    require_review: object = False,
    require_business_context: object = True,
    rule_version: object = 3,
    notes: object = "Odoo-authored.",
) -> dict[str, Any]:
    mapping = OdooDecisionRuleFieldMapping()
    return {
        "id": odoo_id,
        mapping.name: name,
        mapping.rule_code: rule_code,
        mapping.active: active,
        mapping.priority: priority,
        mapping.company: company,
        mapping.vendor: vendor,
        mapping.vendor_tax_id: vendor_tax_id,
        mapping.currency: currency,
        mapping.provider_document_type: provider_document_type,
        mapping.purchase_order_present: purchase_order_presence,
        mapping.description_contains: description,
        mapping.product_mapping: product_mapping,
        mapping.workflow: workflow,
        mapping.classification_code: classification_code,
        mapping.require_review: require_review,
        mapping.require_business_context: require_business_context,
        mapping.rule_version: rule_version,
        mapping.notes: notes,
    }
