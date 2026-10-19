"""PR T / T1 VAT normalization safety regressions (synthetic, no Odoo I/O)."""
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.domain.invoice import Header, InternalInvoice, InvoiceLine, MonetaryTotals, Party, Tax
from app.tax_mapping.engine import TaxMappingEngine, normalize_tax_type
from app.tax_mapping.repository import TaxCandidate
from app.tax_mapping.result import TaxMatchStatus, TaxType
from app.erp.odoo.tax_repository import OdooTaxRepository, TAX_FIELDS


def invoice_with(*taxes: Tax) -> InternalInvoice:
    return InternalInvoice(
        header=Header(invoice_number="TEST", invoice_uuid="synthetic"),
        supplier=Party(), customer=Party(), totals=MonetaryTotals(),
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


@pytest.mark.parametrize("name", [
    "KDV", "VAT", "KDV %20", "KDV%20", "KDV % 20",
    "GERÇEK USULDE KATMA DEĞER VERGİSİ",
    "Gercek Usulde Katma Deger Vergisi", "KATMA DEGER VERGISI",
])
def test_ordinary_vat_names(name):
    assert normalize_tax_type(name) is TaxType.VAT
    match, calls = mapped(Tax(tax_type=name, rate=Decimal("20")),
                          [TaxCandidate(tax_id=34, company_id=3, tax_type=TaxType.VAT, rate=Decimal("20"))])
    assert match.status == TaxMatchStatus.MATCHED
    assert match.tax_id == 34
    assert calls == 1


@pytest.mark.parametrize("name", ["ÖTV", "", "KDV %20 EK", "KDV %abc", "tax"])
def test_unknown_names_fail_closed(name):
    match, calls = mapped(Tax(tax_type=name, rate=Decimal("20")))
    assert match.status == TaxMatchStatus.INVALID_INPUT
    assert calls == 0


@pytest.mark.parametrize("tax", [
    Tax(tax_type="KDV %20", rate=Decimal("10")),
    Tax(tax_type="KDV", rate=Decimal("0")),
    Tax(tax_type="KDV", rate=None, tax_amount=Decimal("0")),
    Tax(tax_type="KDV", rate=Decimal("20"), exemption_reason="335"),
    Tax(tax_type="TEVKIFAT", rate=Decimal("20")),
    Tax(tax_type="ISTISNA", rate=Decimal("20")),
])
def test_unsafe_tax_cannot_trigger_lookup(tax):
    match, calls = mapped(tax, [TaxCandidate(tax_id=6, company_id=3, tax_type=TaxType.VAT, rate=Decimal("0"))])
    assert match.status == TaxMatchStatus.INVALID_INPUT
    assert match.tax_id is None
    assert calls == 0


@pytest.mark.parametrize("amount_type,children,amount,valid", [
    ("percent", [], 20, True),
    ("group", [22], 20, False),
    ("fixed", [], 20, False),
    ("percent", [23], 20, False),
    ("percent", [], -1, False),
])
def test_odoo_repository_filters_tax_structure(amount_type, children, amount, valid):
    class Adapter:
        def search_read_all(self, *, model, domain, fields):
            assert model == "account.tax"
            assert "amount_type" in fields and "children_tax_ids" in fields
            return [{"id": 34, "amount": amount, "active": True,
                     "type_tax_use": "purchase", "company_id": [3, "ICT"],
                     "amount_type": amount_type, "children_tax_ids": children}]
    candidates = OdooTaxRepository(adapter=Adapter()).find_candidates(
        company_id=3, rate=Decimal("20"), tax_type=TaxType.WITHHOLDING)
    assert bool(candidates) is valid
    if valid:
        assert candidates[0].tax_type is TaxType.VAT
        assert candidates[0].amount_type == "percent"
        assert not candidates[0].has_children


def test_mixed_rate_invoice_blocks_zero_line_without_affecting_normal_line():
    repo = Repository([TaxCandidate(tax_id=34, company_id=3, tax_type=TaxType.VAT, rate=Decimal("20"))])
    result = TaxMappingEngine(repo).map_invoice(
        InternalInvoice(header=Header(invoice_number="X", invoice_uuid="mixed"),
                        supplier=Party(), customer=Party(), totals=MonetaryTotals(),
                        lines=(InvoiceLine(line_number="1", taxes=(Tax(tax_type="KDV", rate=Decimal("20")),)),
                               InvoiceLine(line_number="2", taxes=(Tax(tax_type="KDV", rate=Decimal("0")),)))),
        company_id=3)
    assert [item.result.status for item in result.line_results] == [
        TaxMatchStatus.MATCHED, TaxMatchStatus.INVALID_INPUT]
    assert repo.calls == 1
