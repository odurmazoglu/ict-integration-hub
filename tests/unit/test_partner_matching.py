from __future__ import annotations

from collections.abc import Sequence
from dataclasses import FrozenInstanceError
from decimal import Decimal
from pathlib import Path

import pytest

from app.domain.invoice import Header, InternalInvoice, MonetaryTotals, Party
from app.erp.models import Partner
from app.matching import PartnerMatchingEngine, PartnerMatchStatus


class FakePartnerRepository:
    def __init__(self, *, records: dict[str, Sequence[Partner]] | None = None, fail: bool = False) -> None:
        self.records = records or {}
        self.fail = fail
        self.calls: list[tuple[str, int | None]] = []

    def find_by_tax_number(self, tax_number: str, *, company_id: int | None = None) -> Sequence[Partner]:
        self.calls.append((tax_number, company_id))
        if self.fail:
            raise RuntimeError("repository failed")
        return tuple(self.records.get(tax_number, ()))

    def find_by_ids(self, ids: Sequence[int]) -> Sequence[Partner]:
        del ids
        return ()


class FakeProvider:
    def __init__(self, partner_repository: FakePartnerRepository) -> None:
        self.partner_repository = partner_repository


def test_exact_tax_number_supplier_match() -> None:
    repository = FakePartnerRepository(records={"1234567890": [_partner(10, tax_number="1234567890")]})

    result = _match(_invoice(tax_number="1234567890", supplier_name="Supplier"), repository, company_id=7)

    assert result.status is PartnerMatchStatus.MATCHED
    assert result.partner_id == 10
    assert result.matched_by == "tax_number"
    assert result.confidence == Decimal("1.00")
    assert result.candidate_count == 1
    assert repository.calls == [("1234567890", 7)]


def test_missing_supplier_tax_number_is_invalid_without_name_fallback() -> None:
    repository = FakePartnerRepository(records={"Supplier": [_partner(10, tax_number=None)]})

    result = _match(_invoice(tax_number=None, supplier_name="Supplier"), repository)

    assert result.status is PartnerMatchStatus.INVALID_INPUT
    assert result.partner_id is None
    assert result.reason == "Supplier tax number is required for deterministic matching."
    assert repository.calls == []


def test_ambiguous_supplier_match_stops_without_guessing() -> None:
    repository = FakePartnerRepository(
        records={
            "1234567890": [
                _partner(10, tax_number="1234567890"),
                _partner(11, tax_number="1234567890"),
            ]
        }
    )

    result = _match(_invoice(tax_number="1234567890"), repository)

    assert result.status is PartnerMatchStatus.MULTIPLE_MATCHES
    assert result.partner_id is None
    assert result.candidate_count == 2
    assert repository.calls == [("1234567890", None)]


def test_supplier_not_found_is_reviewable_result() -> None:
    repository = FakePartnerRepository()

    result = _match(_invoice(tax_number="404"), repository)

    assert result.status is PartnerMatchStatus.NOT_FOUND
    assert result.reason == "No active deterministic supplier partner candidate found."
    assert repository.calls == [("404", None)]


def test_repository_failure_is_sanitized() -> None:
    repository = FakePartnerRepository(fail=True)

    result = _match(_invoice(tax_number="1234567890"), repository)

    assert result.status is PartnerMatchStatus.INVALID_INPUT
    assert result.reason == "Partner repository lookup failed."
    assert "repository failed" not in result.reason


def test_partner_match_result_is_immutable() -> None:
    repository = FakePartnerRepository(records={"1234567890": [_partner(10, tax_number="1234567890")]})

    result = _match(_invoice(tax_number="1234567890"), repository)

    with pytest.raises(FrozenInstanceError):
        result.partner_id = 99


def test_partner_matching_package_has_no_fuzzy_or_provider_write_dependency() -> None:
    source = (Path("app/matching/partner.py")).read_text()

    assert "OdooJson2Client" not in source
    assert "search_read" not in source
    assert "sqlalchemy" not in source.lower()
    assert "app.db" not in source
    assert "create" not in source
    assert "write" not in source
    assert "unlink" not in source
    assert "action_post" not in source
    assert "fuzzy" not in source.lower()
    assert "levenshtein" not in source.lower()
    assert "embedding" not in source.lower()
    assert "similarity" not in source.lower()


def _match(
    invoice: InternalInvoice,
    repository: FakePartnerRepository,
    *,
    company_id: int | None = None,
):
    return PartnerMatchingEngine(FakeProvider(repository)).match_invoice(invoice, company_id=company_id)


def _invoice(*, tax_number: str | None, supplier_name: str | None = "Supplier") -> InternalInvoice:
    return InternalInvoice(
        header=Header(invoice_number="INV-1", invoice_uuid="uuid-1", currency_code="TRY"),
        supplier=Party(name=supplier_name, tax_number=tax_number),
        customer=Party(name="Customer"),
        totals=MonetaryTotals(payable_amount=Decimal("1.00")),
        lines=(),
    )


def _partner(partner_id: int, *, tax_number: str | None, active: bool = True) -> Partner:
    return Partner(id=partner_id, name="Supplier", tax_number=tax_number, active=active)
