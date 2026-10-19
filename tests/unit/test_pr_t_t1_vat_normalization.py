"""PR T / T1 VAT normalization safety regressions (synthetic, no Odoo I/O)."""

import dataclasses
import unicodedata
from decimal import Decimal

import pytest

from app.application.workflow import ManualReviewReasonCode
from app.domain.invoice import Header, InternalInvoice, InvoiceLine, MonetaryTotals, Party, Tax
from app.erp.odoo.tax_repository import TAX_FIELDS, OdooTaxRepository
from app.tax_mapping.engine import TaxMappingEngine, normalize_tax_type
from app.tax_mapping.repository import TaxCandidate
from app.tax_mapping.result import TaxMatchStatus, TaxType
from tests.unit.test_deterministic_rule_engine import _command, _engine, _invoice, _line

# Synthetic stand-ins shaped like the production Odoo purchase taxes; no Odoo call is made.
ORDINARY_VAT_20 = TaxCandidate(
    tax_id=34,
    company_id=3,
    tax_type=TaxType.VAT,
    rate=Decimal("20"),
    usage_type="purchase",
    amount_type="percent",
    has_children=False,
)
ZERO_PERCENT_TAX = dataclasses.replace(ORDINARY_VAT_20, tax_id=6, rate=Decimal("0"))
_MISSING = object()


def invoice_with(*taxes: Tax) -> InternalInvoice:
    return InternalInvoice(
        header=Header(invoice_number="TEST", invoice_uuid="synthetic"),
        supplier=Party(),
        customer=Party(),
        totals=MonetaryTotals(),
        lines=(InvoiceLine(line_number="1", taxes=tuple(taxes)),),
    )


class Repository:
    def __init__(self, candidates):
        self.candidates = candidates
        self.calls = 0

    def find_candidates(self, *, company_id, rate, tax_type):
        self.calls += 1
        return self.candidates


def mapped(tax, candidates=()):
    repository = Repository(candidates)
    result = TaxMappingEngine(repository).map_invoice(invoice_with(tax), company_id=3)
    return result.line_results[0].result, repository.calls


@pytest.mark.parametrize(
    "name",
    [
        "KDV",
        "VAT",
        "kdv",
        "KDV %20",
        "KDV%20",
        "KDV % 20",
        "KDV %20,0",
        "GERÇEK USULDE KATMA DEĞER VERGİSİ",
        "gerçek usulde katma değer vergisi",
        unicodedata.normalize("NFD", "GERÇEK USULDE KATMA DEĞER VERGİSİ"),
        "Gercek Usulde Katma Deger Vergisi",
        "KATMA DEGER VERGISI",
        "Katma Değer Vergisi",
        "VALUE ADDED TAX",
        "VALUE_ADDED_TAX",
        "Value-Added Tax",
        "  KDV  ",
    ],
)
def test_ordinary_vat_names(name):
    assert normalize_tax_type(name) is TaxType.VAT
    match, calls = mapped(Tax(tax_type=name, rate=Decimal("20")), [ORDINARY_VAT_20])
    assert match.status == TaxMatchStatus.MATCHED
    assert match.tax_id == 34
    assert calls == 1


@pytest.mark.parametrize(
    "name",
    ["ÖTV", "", "   ", "KDV %20 EK", "KDV %abc", "KDV %٢٠", "%20 KDV", "KDVX", "KATMA DEGER", "TEVKIFATLI KDV", "tax"],
)
def test_unknown_names_fail_closed(name):
    match, calls = mapped(Tax(tax_type=name, rate=Decimal("20")), [ORDINARY_VAT_20])
    assert match.status == TaxMatchStatus.INVALID_INPUT
    assert match.tax_id is None
    assert calls == 0


@pytest.mark.parametrize(
    "name,rate,matched",
    [
        ("KDV %20", "20", True),
        ("KDV %20", "20.00", True),
        ("KDV %20,00", "20", True),
        ("KDV %20", "20.0001", False),
        ("KDV %20", "19.99", False),
        ("KDV %1", "10", False),
        ("KDV %10", "1", False),
        ("KDV %20", "10", False),
    ],
)
def test_rate_bearing_name_requires_exact_percent_agreement(name, rate, matched):
    candidate = dataclasses.replace(ORDINARY_VAT_20, rate=Decimal(rate))
    match, calls = mapped(Tax(tax_type=name, rate=Decimal(rate)), [candidate])
    if matched:
        assert match.status == TaxMatchStatus.MATCHED
        assert calls == 1
    else:
        assert match.status == TaxMatchStatus.INVALID_INPUT
        assert match.reason == "VAT rate in tax name does not match Percent."
        assert calls == 0


@pytest.mark.parametrize(
    "tax",
    [
        Tax(tax_type="KDV %20", rate=Decimal("10")),
        Tax(tax_type="KDV", rate=Decimal("0")),
        Tax(tax_type="KDV", rate=None, tax_amount=Decimal("0")),
        Tax(tax_type="KDV", rate=Decimal("20"), exemption_reason="335"),
        Tax(tax_type="TEVKIFAT", rate=Decimal("20")),
        Tax(tax_type="ISTISNA", rate=Decimal("20")),
    ],
)
def test_unsafe_tax_cannot_trigger_lookup(tax):
    match, calls = mapped(tax, [ZERO_PERCENT_TAX])
    assert match.status == TaxMatchStatus.INVALID_INPUT
    assert match.tax_id is None
    assert calls == 0


@pytest.mark.parametrize(
    "tax",
    [
        # 209: 17/4-e exemption at 0%.
        Tax(tax_type="KDV", rate=Decimal("0"), tax_amount=Decimal("0"), exemption_reason="17/4-e istisna"),
        Tax(tax_type="KDV", rate=Decimal("0.00"), tax_amount=Decimal("0"), exemption_reason="209"),
        # 335: ISTISNA invoice with no Percent at all; 0% must never be inferred.
        Tax(tax_type="KDV", rate=None, tax_amount=Decimal("0"), exemption_reason="335"),
        Tax(tax_type="GERÇEK USULDE KATMA DEĞER VERGİSİ", rate=None, tax_amount=Decimal("0")),
        # 351: zero-rated line of a mixed-rate invoice.
        Tax(tax_type="KDV", rate=Decimal("0"), tax_amount=Decimal("0"), exemption_reason="351"),
        Tax(tax_type="KDV %0", rate=Decimal("0"), tax_amount=Decimal("0")),
    ],
)
def test_exemptions_never_map_even_when_tax_6_is_the_only_candidate(tax):
    match, calls = mapped(tax, [ZERO_PERCENT_TAX])
    assert match.status == TaxMatchStatus.INVALID_INPUT
    assert match.tax_id is None
    assert match.candidate_count == 0
    assert calls == 0


@pytest.mark.parametrize(
    "structure",
    [
        None,
        {"usage_type": "sale"},
        {"amount_type": "group"},
        {"amount_type": "fixed"},
        {"has_children": True},
        {"has_children": None},
        {"active": False},
    ],
)
def test_engine_rejects_candidates_without_explicit_standalone_purchase_percent_structure(structure):
    if structure is None:
        candidate = TaxCandidate(tax_id=34, company_id=3, tax_type=TaxType.VAT, rate=Decimal("20"))
    else:
        candidate = dataclasses.replace(ORDINARY_VAT_20, **structure)
    match, calls = mapped(Tax(tax_type="KDV", rate=Decimal("20")), [candidate])
    assert match.status == TaxMatchStatus.NOT_FOUND
    assert match.tax_id is None
    assert calls == 1


def _odoo_record(**overrides):
    record = {
        "id": 34,
        "amount": 20.0,
        "active": True,
        "type_tax_use": "purchase",
        "company_id": [3, "ICT"],
        "amount_type": "percent",
        "children_tax_ids": [],
    }
    record.update(overrides)
    return {key: value for key, value in record.items() if value is not _MISSING}


class _Adapter:
    def __init__(self, records):
        self.records = records
        self.calls = []

    def search_read_all(self, *, model, domain, fields):
        self.calls.append((model, domain, fields))
        return self.records


@pytest.mark.parametrize(
    "overrides,valid",
    [
        ({}, True),
        ({"amount_type": "group", "children_tax_ids": [22]}, False),
        ({"amount_type": "fixed"}, False),
        ({"amount_type": "division"}, False),
        ({"children_tax_ids": [23]}, False),
        ({"amount": -1}, False),
        ({"type_tax_use": "sale"}, False),
        ({"type_tax_use": "none"}, False),
        ({"amount_type": _MISSING}, False),
        ({"children_tax_ids": _MISSING}, False),
        ({"children_tax_ids": False}, False),
    ],
)
def test_odoo_repository_filters_tax_structure(overrides, valid):
    adapter = _Adapter([_odoo_record(**overrides)])
    candidates = OdooTaxRepository(adapter=adapter).find_candidates(
        company_id=3, rate=Decimal("20"), tax_type=TaxType.WITHHOLDING
    )
    assert bool(candidates) is valid
    if valid:
        assert candidates[0].tax_type is TaxType.VAT
        assert candidates[0].usage_type == "purchase"
        assert candidates[0].amount_type == "percent"
        assert candidates[0].has_children is False


def test_odoo_repository_query_contract_is_unchanged_apart_from_structure_fields():
    adapter = _Adapter([])
    OdooTaxRepository(adapter=adapter).find_candidates(company_id=3, rate=Decimal("20"), tax_type=TaxType.VAT)
    [(model, domain, fields)] = adapter.calls
    assert model == "account.tax"
    assert domain == [["amount", "=", 20.0], ["type_tax_use", "=", "purchase"], ["company_id", "in", [3, False]]]
    assert fields == TAX_FIELDS
    assert {"amount_type", "children_tax_ids"} <= set(TAX_FIELDS)


def test_odoo_shared_company_tax_is_not_matched_for_specific_company():
    adapter = _Adapter([_odoo_record(company_id=False)])
    engine = TaxMappingEngine(OdooTaxRepository(adapter=adapter))
    result = engine.map_invoice(invoice_with(Tax(tax_type="KDV", rate=Decimal("20"))), company_id=3)
    assert result.line_results[0].result.status == TaxMatchStatus.NOT_FOUND


def test_odoo_group_taxes_at_zero_percent_are_dropped_and_zero_rate_is_never_looked_up():
    # Pre-T1, 0% returned tax 6 plus seven group taxes (TAX_AMBIGUOUS). Post-T1 the
    # repository drops the groups and the engine refuses 0% before any lookup.
    records = [_odoo_record(id=6, amount=0.0)] + [
        _odoo_record(id=tax_id, amount=0.0, amount_type="group", children_tax_ids=[tax_id + 100])
        for tax_id in (22, 23, 24, 25, 26, 29, 30)
    ]
    adapter = _Adapter(records)
    repository = OdooTaxRepository(adapter=adapter)
    direct = repository.find_candidates(company_id=3, rate=Decimal("0"), tax_type=TaxType.VAT)
    assert [candidate.tax_id for candidate in direct] == [6]

    result = TaxMappingEngine(repository).map_invoice(
        invoice_with(Tax(tax_type="KDV", rate=Decimal("0"), exemption_reason="209")), company_id=3
    )
    assert result.line_results[0].result.status == TaxMatchStatus.INVALID_INPUT
    assert len(adapter.calls) == 1


def test_mixed_rate_invoice_blocks_zero_line_without_affecting_normal_lines():
    repo = Repository([ORDINARY_VAT_20])
    result = TaxMappingEngine(repo).map_invoice(
        InternalInvoice(
            header=Header(invoice_number="X", invoice_uuid="mixed"),
            supplier=Party(),
            customer=Party(),
            totals=MonetaryTotals(),
            lines=tuple(
                InvoiceLine(
                    line_number=str(number),
                    taxes=(
                        Tax(tax_type="KDV", rate=Decimal("0"), exemption_reason="351")
                        if number == 6
                        else Tax(tax_type="KDV", rate=Decimal("20")),
                    ),
                )
                for number in range(1, 9)
            ),
        ),
        company_id=3,
    )
    statuses = [(item.line_number, item.result.status) for item in result.line_results]
    assert statuses == [
        (str(number), TaxMatchStatus.INVALID_INPUT if number == 6 else TaxMatchStatus.MATCHED) for number in range(1, 9)
    ]
    assert {item.result.tax_id for item in result.line_results} == {34, None}
    assert repo.calls == 1


def _rule_result(tax: Tax, candidates):
    line = dataclasses.replace(_line("1", buyer_item_code="SKU-1"), taxes=(tax,))
    company_candidates = [dataclasses.replace(candidate, company_id=7) for candidate in candidates]
    engine = _engine(tax_mapper=TaxMappingEngine(Repository(company_candidates)))
    return engine.evaluate(_command(invoice=_invoice(lines=(line,))))


def _tax_reason_codes(result):
    manual_review = result.workflow_decision.manual_review
    if manual_review is None:
        return ()
    return tuple(reason.code for reason in manual_review.reasons if reason.source == "tax_mapping")


@pytest.mark.parametrize(
    "tax",
    [
        Tax(tax_type="KDV", rate=Decimal("0"), exemption_reason="209"),
        Tax(tax_type="KDV", rate=None, tax_amount=Decimal("0"), exemption_reason="335"),
        Tax(tax_type="KDV", rate=Decimal("0"), exemption_reason="351"),
    ],
)
def test_rules_keep_exemptions_blocked_as_unsupported_content(tax):
    result = _rule_result(tax, [ZERO_PERCENT_TAX])
    assert _tax_reason_codes(result) == (ManualReviewReasonCode.UNSUPPORTED_INVOICE_CONTENT,)


@pytest.mark.parametrize("name", ["GERÇEK USULDE KATMA DEĞER VERGİSİ", "KDV %20", "KDV %1"])
def test_rules_accept_rc1_vat_names(name):
    rate = Decimal("1") if name == "KDV %1" else Decimal("20")
    candidate = dataclasses.replace(ORDINARY_VAT_20, tax_id=36 if rate == 1 else 34, rate=rate)
    result = _rule_result(Tax(tax_type=name, rate=rate), [candidate])
    assert _tax_reason_codes(result) == ()


def test_tax_evidence_shape_is_unchanged():
    assert [field.name for field in dataclasses.fields(Tax)] == [
        "tax_type",
        "rate",
        "base_amount",
        "tax_amount",
        "exemption_reason",
    ]
