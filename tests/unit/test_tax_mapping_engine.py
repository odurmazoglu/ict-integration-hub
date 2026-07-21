from collections.abc import Sequence
from dataclasses import FrozenInstanceError
from decimal import Decimal
from pathlib import Path

import pytest

from app.domain.invoice import Header, InternalInvoice, InvoiceLine, MonetaryTotals, Party, Tax
from app.tax_mapping import (
    InvoiceTaxMappingResult,
    TaxCandidate,
    TaxMappingEngine,
    TaxMatchResult,
    TaxMatchStatus,
    TaxType,
)
from app.tax_mapping.exceptions import TaxMappingError


def test_exact_vat_match() -> None:
    result = _map(
        _invoice([_line("1", [Tax(tax_type="VAT", rate=Decimal("20"))])]),
        [TaxCandidate(tax_id=10, company_id=3, tax_type=TaxType.VAT, rate=Decimal("20.00"))],
        company_id=3,
    )

    match = result.line_results[0].result
    assert match.status == TaxMatchStatus.MATCHED
    assert match.tax_id == 10
    assert match.company_id == 3
    assert match.tax_type == TaxType.VAT
    assert match.tax_rate == Decimal("2E+1")
    assert match.matched_by == "company_type_rate"
    assert match.confidence == Decimal("1.00")
    assert match.candidate_count == 1


def test_exact_withholding_match() -> None:
    result = _map(
        _invoice([_line("1", [Tax(tax_type="WITHHOLDING", rate=Decimal("5"))])]),
        [TaxCandidate(tax_id=20, company_id=3, tax_type=TaxType.WITHHOLDING, rate=Decimal("5"))],
        company_id=3,
    )

    assert result.line_results[0].result.status == TaxMatchStatus.MATCHED
    assert result.line_results[0].result.tax_id == 20


def test_exact_exemption_match() -> None:
    result = _map(
        _invoice([_line("1", [Tax(tax_type="EXEMPTION", rate=Decimal("0"))])]),
        [TaxCandidate(tax_id=30, company_id=3, tax_type=TaxType.EXEMPTION, rate=Decimal("0"))],
        company_id=3,
    )

    assert result.line_results[0].result.status == TaxMatchStatus.MATCHED
    assert result.line_results[0].result.tax_id == 30


def test_zero_rate_vat_remains_distinct_from_exemption() -> None:
    result = _map(
        _invoice([_line("1", [Tax(tax_type="VAT", rate=Decimal("0"))])]),
        [
            TaxCandidate(tax_id=30, company_id=3, tax_type=TaxType.EXEMPTION, rate=Decimal("0")),
            TaxCandidate(tax_id=40, company_id=3, tax_type=TaxType.VAT, rate=Decimal("0.00")),
        ],
        company_id=3,
    )

    assert result.line_results[0].result.status == TaxMatchStatus.MATCHED
    assert result.line_results[0].result.tax_id == 40


@pytest.mark.parametrize("tax_type", [None, "", "LOCAL_TAX", "UNKNOWN"])
def test_unknown_or_unsupported_tax_type_is_invalid(tax_type: object) -> None:
    result = _map(
        _invoice([_line("1", [Tax(tax_type=tax_type, rate=Decimal("20"))])]),  # type: ignore[arg-type]
        [],
        company_id=3,
    )

    assert result.line_results[0].result.status == TaxMatchStatus.INVALID_INPUT
    assert result.line_results[0].result.tax_id is None


def test_negative_rate_is_invalid() -> None:
    result = _map(
        _invoice([_line("1", [Tax(tax_type="VAT", rate=Decimal("-1"))])]),
        [],
        company_id=3,
    )

    assert result.line_results[0].result.status == TaxMatchStatus.INVALID_INPUT
    assert result.line_results[0].result.reason == "Tax rate must not be negative."


def test_malformed_rate_is_invalid() -> None:
    result = _map(
        _invoice([_line("1", [Tax(tax_type="VAT", rate="not-a-decimal")])]),  # type: ignore[arg-type]
        [],
        company_id=3,
    )

    assert result.line_results[0].result.status == TaxMatchStatus.INVALID_INPUT
    assert result.line_results[0].result.reason == "Tax rate is required or malformed."


def test_not_found() -> None:
    result = _map(_invoice([_line("1", [Tax(tax_type="VAT", rate=Decimal("20"))])]), [], company_id=3)

    assert result.line_results[0].result.status == TaxMatchStatus.NOT_FOUND
    assert result.line_results[0].result.candidate_count == 0


def test_multiple_matches() -> None:
    result = _map(
        _invoice([_line("1", [Tax(tax_type="VAT", rate=Decimal("20"))])]),
        [
            TaxCandidate(tax_id=10, company_id=3, tax_type=TaxType.VAT, rate=Decimal("20")),
            TaxCandidate(tax_id=11, company_id=3, tax_type=TaxType.VAT, rate=Decimal("20.0")),
        ],
        company_id=3,
    )

    match = result.line_results[0].result
    assert match.status == TaxMatchStatus.MULTIPLE_MATCHES
    assert match.tax_id is None
    assert match.candidate_count == 2


def test_same_rate_and_type_in_different_companies_with_explicit_company_selects_only_correct_company() -> None:
    result = _map(
        _invoice([_line("1", [Tax(tax_type="VAT", rate=Decimal("20"))])]),
        [
            TaxCandidate(tax_id=10, company_id=2, tax_type=TaxType.VAT, rate=Decimal("20")),
            TaxCandidate(tax_id=11, company_id=3, tax_type=TaxType.VAT, rate=Decimal("20")),
        ],
        company_id=3,
    )

    assert result.line_results[0].result.status == TaxMatchStatus.MATCHED
    assert result.line_results[0].result.tax_id == 11


def test_company_id_none_with_cross_company_duplicates_returns_multiple_matches() -> None:
    result = _map(
        _invoice([_line("1", [Tax(tax_type="VAT", rate=Decimal("20"))])]),
        [
            TaxCandidate(tax_id=10, company_id=2, tax_type=TaxType.VAT, rate=Decimal("20")),
            TaxCandidate(tax_id=11, company_id=3, tax_type=TaxType.VAT, rate=Decimal("20")),
        ],
        company_id=None,
    )

    assert result.line_results[0].result.status == TaxMatchStatus.MULTIPLE_MATCHES
    assert result.line_results[0].result.candidate_count == 2


def test_inactive_candidates_are_not_valid_matches() -> None:
    result = _map(
        _invoice([_line("1", [Tax(tax_type="VAT", rate=Decimal("20"))])]),
        [TaxCandidate(tax_id=10, company_id=3, tax_type=TaxType.VAT, rate=Decimal("20"), active=False)],
        company_id=3,
    )

    assert result.line_results[0].result.status == TaxMatchStatus.NOT_FOUND
    assert result.line_results[0].result.candidate_count == 0


def test_multiple_invoice_lines_and_multiple_taxes_on_one_line() -> None:
    result = _map(
        _invoice(
            [
                _line("1", [Tax(tax_type="VAT", rate=Decimal("20")), Tax(tax_type="WITHHOLDING", rate=Decimal("5"))]),
                _line("2", [Tax(tax_type="VAT", rate=Decimal("10"))]),
            ]
        ),
        [
            TaxCandidate(tax_id=10, company_id=3, tax_type=TaxType.VAT, rate=Decimal("20")),
            TaxCandidate(tax_id=20, company_id=3, tax_type=TaxType.WITHHOLDING, rate=Decimal("5")),
            TaxCandidate(tax_id=30, company_id=3, tax_type=TaxType.VAT, rate=Decimal("10")),
        ],
        company_id=3,
    )

    assert [(item.line_number, item.tax_index, item.result.tax_id) for item in result.line_results] == [
        ("1", 0, 10),
        ("1", 1, 20),
        ("2", 0, 30),
    ]


def test_duplicate_tax_occurrences_each_receive_result_and_lookup_is_cached() -> None:
    repository = FakeTaxRepository([TaxCandidate(tax_id=10, company_id=3, tax_type=TaxType.VAT, rate=Decimal("20"))])
    invoice = _invoice(
        [
            _line("1", [Tax(tax_type="VAT", rate=Decimal("20"))]),
            _line("2", [Tax(tax_type="VAT", rate=Decimal("20.00"))]),
        ]
    )

    result = TaxMappingEngine(repository).map_invoice(invoice, company_id=3)

    assert [item.result.tax_id for item in result.line_results] == [10, 10]
    assert repository.calls == [(3, Decimal("2E+1"), TaxType.VAT)]


def test_decimal_normalization_treats_equivalent_representations_as_same_rate() -> None:
    result = _map(
        _invoice([_line("1", [Tax(tax_type="VAT", rate=Decimal("20.0"))])]),
        [TaxCandidate(tax_id=10, company_id=3, tax_type=TaxType.VAT, rate=Decimal("20.00"))],
        company_id=3,
    )

    assert result.line_results[0].result.status == TaxMatchStatus.MATCHED


def test_empty_invoice_lines_returns_sanitized_error() -> None:
    result = TaxMappingEngine(FakeTaxRepository([])).map_invoice(_invoice([]), company_id=3)

    assert result == InvoiceTaxMappingResult(errors=("Invoice has no lines to map.",))


def test_missing_line_identifier_returns_invalid_result() -> None:
    result = _map(_invoice([_line(None, [Tax(tax_type="VAT", rate=Decimal("20"))])]), [], company_id=3)

    assert result.line_results[0].result.status == TaxMatchStatus.INVALID_INPUT
    assert result.line_results[0].result.reason == "Line identifier is required for tax mapping."


def test_repository_failure_is_sanitized() -> None:
    repository = FailingTaxRepository()

    result = TaxMappingEngine(repository).map_invoice(
        _invoice([_line("1", [Tax(tax_type="VAT", rate=Decimal("20"))])]),
        company_id=3,
    )

    assert result.line_results[0].result.status == TaxMatchStatus.INVALID_INPUT
    assert result.line_results[0].result.reason == "Tax repository lookup failed."
    assert "secret" not in str(result).lower()


def test_unexpected_repository_programming_error_is_not_swallowed() -> None:
    repository = ProgrammingErrorRepository()

    with pytest.raises(RuntimeError, match="programming error"):
        TaxMappingEngine(repository).map_invoice(
            _invoice([_line("1", [Tax(tax_type="VAT", rate=Decimal("20"))])]),
            company_id=3,
        )


def test_candidate_and_result_dtos_are_immutable() -> None:
    candidate = TaxCandidate(tax_id=10, company_id=3, tax_type=TaxType.VAT, rate=Decimal("20"))
    result = TaxMatchResult(
        status=TaxMatchStatus.MATCHED,
        tax_id=10,
        company_id=3,
        tax_type=TaxType.VAT,
        tax_rate=Decimal("20"),
        matched_by="company_type_rate",
        confidence=Decimal("1.00"),
        reason="Exact company, type, and rate match.",
        candidate_count=1,
    )

    with pytest.raises(FrozenInstanceError):
        candidate.tax_id = 11  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.tax_id = 11  # type: ignore[misc]


def test_tax_mapping_package_has_no_provider_persistence_http_or_db_dependency() -> None:
    package_root = Path(__file__).resolve().parents[2] / "app" / "tax_mapping"
    source = "\n".join(path.read_text(encoding="utf-8") for path in package_root.glob("*.py"))

    forbidden = (
        "odoo",
        "uyumsoft",
        "soap",
        "zeep",
        "sqlalchemy",
        "database",
        "session",
        "fastapi",
        "requests",
        "httpx",
    )
    lowered = source.lower()
    for token in forbidden:
        assert token not in lowered


class FakeTaxRepository:
    def __init__(self, candidates: Sequence[TaxCandidate]) -> None:
        self.candidates = tuple(candidates)
        self.calls: list[tuple[int | None, Decimal, TaxType]] = []

    def find_candidates(
        self,
        *,
        company_id: int | None,
        rate: Decimal,
        tax_type: TaxType,
    ) -> Sequence[TaxCandidate]:
        self.calls.append((company_id, rate, tax_type))
        return self.candidates


class FailingTaxRepository:
    def find_candidates(
        self,
        *,
        company_id: int | None,
        rate: Decimal,
        tax_type: TaxType,
    ) -> Sequence[TaxCandidate]:
        raise TaxMappingError("secret repository detail")


class ProgrammingErrorRepository:
    def find_candidates(
        self,
        *,
        company_id: int | None,
        rate: Decimal,
        tax_type: TaxType,
    ) -> Sequence[TaxCandidate]:
        raise RuntimeError("programming error")


def _map(
    invoice: InternalInvoice,
    candidates: Sequence[TaxCandidate],
    *,
    company_id: int | None,
) -> InvoiceTaxMappingResult:
    return TaxMappingEngine(FakeTaxRepository(candidates)).map_invoice(invoice, company_id=company_id)


def _invoice(lines: list[InvoiceLine]) -> InternalInvoice:
    return InternalInvoice(
        header=Header(invoice_number="INV-1", invoice_uuid="uuid-1", currency_code="TRY"),
        supplier=Party(name="Supplier"),
        customer=Party(name="Customer"),
        totals=MonetaryTotals(payable_amount=Decimal("1.00")),
        lines=tuple(lines),
    )


def _line(line_number: str | None, taxes: list[Tax]) -> InvoiceLine:
    return InvoiceLine(line_number=line_number, taxes=tuple(taxes))
