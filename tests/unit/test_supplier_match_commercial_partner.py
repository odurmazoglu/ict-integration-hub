"""Supplier matching decides ambiguity per commercial counterparty, not per res.partner row.

Production evidence (2026-10-06): CloudSpark company partner 439 and its child
contact 440 (commercial_partner_id=439) both carry VAT 1760390647; the matcher
counted two candidates and raised SUPPLIER_AMBIGUOUS.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal

import pytest

from app.application.rules.deterministic import _partner_review_reasons
from app.application.workbench.evidence import ReviewSourceInvoiceEvidence
from app.application.workbench.exceptions import (
    SupplierResolutionPartnerMismatchError,
    SupplierResolutionPartnerNotCommercialError,
)
from app.application.workbench.review_evidence import ReviewEvidenceReader
from app.application.workbench.supplier_resolution import (
    ResolutionPartnerRecord,
    SupplierResolution,
    SupplierResolutionMode,
    SupplierResolutionValidationStatus,
)
from app.application.workbench.supplier_resolution_use_cases import ValidateSupplierResolutionUseCase
from app.application.workflow import ManualReviewReasonCode
from app.domain.invoice import Header, InternalInvoice, MonetaryTotals, Party
from app.erp.models import Partner
from app.erp.odoo.supplier_resolution_partner_reader import OdooSupplierResolutionPartnerReader
from app.matching import PartnerMatchingEngine, PartnerMatchStatus

VAT = "1760390647"
COMPANY_ID = 1
ETTN = "CLOUDSPARK-ETTN-141"
REVIEW_ID = "review:cb0604aa-85c9-5f95-9ae9-b6b35d70b889"


class FakePartnerRepository:
    """In-memory res.partner: exact-VAT lookup mirrors the Odoo domain; find_by_ids hides archived rows."""

    def __init__(self, partners: Sequence[Partner]) -> None:
        self.partners = tuple(partners)
        self.id_calls: list[tuple[int, ...]] = []

    def find_by_tax_number(self, tax_number: str, *, company_id: int | None = None) -> Sequence[Partner]:
        return tuple(
            p
            for p in self.partners
            if p.tax_number == tax_number and (company_id is None or p.company_id in (company_id, None))
        )

    def find_by_ids(self, ids: Sequence[int]) -> Sequence[Partner]:
        self.id_calls.append(tuple(ids))
        return tuple(p for p in self.partners if p.id in ids and p.active)


class FakeProvider:
    def __init__(self, partner_repository: FakePartnerRepository) -> None:
        self.partner_repository = partner_repository


def _company(partner_id: int, *, vat: str | None = VAT, active: bool = True, company_id: int | None = None) -> Partner:
    return Partner(
        id=partner_id,
        name=f"Company {partner_id}",
        tax_number=vat,
        active=active,
        company_id=company_id,
        commercial_partner_id=partner_id,
    )


def _contact(partner_id: int, parent_id: int, *, vat: str | None = VAT, active: bool = True) -> Partner:
    return Partner(
        id=partner_id,
        name=f"Contact {partner_id}",
        tax_number=vat,
        active=active,
        parent_id=parent_id,
        commercial_partner_id=parent_id,
    )


def _cloudspark() -> tuple[Partner, Partner]:
    company = Partner(
        id=439,
        name="CloudSpark Bulut Teknolojileri Sanayi ve Ticaret Anonim Şirketi",
        tax_number=VAT,
        active=True,
        commercial_partner_id=439,
    )
    contact = Partner(
        id=440,
        name="Atakhan Erol",
        tax_number=VAT,
        active=True,
        parent_id=439,
        commercial_partner_id=439,
    )
    return company, contact


def _invoice(tax_number: str | None = VAT) -> InternalInvoice:
    return InternalInvoice(
        header=Header(
            invoice_number="I282026000000141",
            invoice_uuid="00000000-0000-4000-8000-000000000141",
            ettn=ETTN,
            issue_date=date(2026, 9, 30),
            currency_code="TRY",
        ),
        supplier=Party(name="CLOUDSPARK BULUT TEKNOLOJİLERİ", tax_number=tax_number),
        customer=Party(name="ICT", tax_number="1112223334"),
        totals=MonetaryTotals(payable_amount=Decimal("100.00")),
        lines=(),
    )


def _match(partners: Sequence[Partner], *, company_id: int | None = COMPANY_ID):
    repository = FakePartnerRepository(partners)
    result = PartnerMatchingEngine(FakeProvider(repository)).match_invoice(_invoice(), company_id=company_id)
    return result, repository


# --------------------------------------------------------------------------- matcher


def test_company_only_matches_deterministically() -> None:
    result, repository = _match([_company(439)])

    assert result.status is PartnerMatchStatus.MATCHED
    assert result.partner_id == 439
    assert result.matched_by == "tax_number"
    assert result.candidate_count == 1
    assert repository.id_calls == []


def test_company_and_child_sharing_vat_collapse_to_the_company() -> None:
    result, _ = _match([_company(439), _contact(440, 439)])

    assert result.status is PartnerMatchStatus.MATCHED
    assert result.partner_id == 439
    assert result.candidate_count == 1


def test_company_and_many_children_still_collapse_to_one_company() -> None:
    result, _ = _match([_contact(412, 8), _company(8), _contact(9, 8), _contact(423, 8), _contact(10, 8)])

    assert result.status is PartnerMatchStatus.MATCHED
    assert result.partner_id == 8


def test_two_distinct_commercial_companies_remain_ambiguous() -> None:
    result, _ = _match([_company(439), _contact(440, 439), _company(500)])

    assert result.status is PartnerMatchStatus.MULTIPLE_MATCHES
    assert result.partner_id is None
    assert result.candidate_count == 2
    assert result.reason == "Multiple active supplier partner candidates found by tax number."


def test_standalone_individual_supplier_remains_matchable() -> None:
    person = Partner(id=77, name="Ayşe Yılmaz", tax_number=VAT, active=True, commercial_partner_id=77)

    result, _ = _match([person])

    assert result.status is PartnerMatchStatus.MATCHED
    assert result.partner_id == 77


def test_child_contact_never_wins_or_competes_against_its_own_company() -> None:
    # The child row is listed first; the company is still the only canonical candidate.
    result, _ = _match([_contact(440, 439), _company(439)])

    assert result.status is PartnerMatchStatus.MATCHED
    assert result.partner_id == 439


def test_cloudspark_fixture_resolves_to_439_without_supplier_ambiguous() -> None:
    result, _ = _match(_cloudspark())

    assert result.status is PartnerMatchStatus.MATCHED
    assert result.partner_id == 439
    assert _partner_review_reasons(result) == ()


def test_missing_commercial_partner_id_keeps_row_count_semantics() -> None:
    legacy_a = Partner(id=1, name="A", tax_number=VAT, active=True)
    legacy_b = Partner(id=2, name="B", tax_number=VAT, active=True)

    single, single_repo = _match([legacy_a])
    double, _ = _match([legacy_a, legacy_b])

    assert single.status is PartnerMatchStatus.MATCHED
    assert single.partner_id == 1
    assert single_repo.id_calls == []
    assert double.status is PartnerMatchStatus.MULTIPLE_MATCHES
    assert double.candidate_count == 2


def test_archived_canonical_partner_fails_closed() -> None:
    result, repository = _match([_company(439, active=False), _contact(440, 439)])

    assert result.status is PartnerMatchStatus.MULTIPLE_MATCHES
    assert result.partner_id is None
    assert repository.id_calls == [(439,)]
    reasons = _partner_review_reasons(result)
    assert [reason.code for reason in reasons] == [ManualReviewReasonCode.SUPPLIER_AMBIGUOUS]


def test_canonical_partner_with_different_vat_fails_closed() -> None:
    result, repository = _match([_company(439, vat="9999999999"), _contact(440, 439)])

    assert result.status is PartnerMatchStatus.MULTIPLE_MATCHES
    assert result.partner_id is None
    assert repository.id_calls == [(439,)]


def test_canonical_partner_in_another_odoo_company_fails_closed() -> None:
    result, _ = _match([_company(439, company_id=99), _contact(440, 439)])

    assert result.status is PartnerMatchStatus.MULTIPLE_MATCHES
    assert result.partner_id is None


def test_archived_child_with_active_valid_parent_matches_the_parent() -> None:
    result, _ = _match([_company(439), _contact(440, 439, active=False)])

    assert result.status is PartnerMatchStatus.MATCHED
    assert result.partner_id == 439


def test_lone_child_row_resolves_to_its_valid_commercial_partner() -> None:
    # Only the child carries the VAT row the lookup returned; the parent is read by id.
    parent = _company(439)
    repository = FakePartnerRepository([parent, _contact(440, 439)])
    repository.find_by_tax_number = lambda tax_number, *, company_id=None: (_contact(440, 439),)  # type: ignore[method-assign]

    result = PartnerMatchingEngine(FakeProvider(repository)).match_invoice(_invoice(), company_id=COMPANY_ID)

    assert result.status is PartnerMatchStatus.MATCHED
    assert result.partner_id == 439
    assert repository.id_calls == [(439,)]


def test_canonical_partner_lookup_failure_raises_safe_matching_error() -> None:
    from app.matching import PartnerMatchingError

    repository = FakePartnerRepository([_contact(440, 439)])

    def _boom(ids):
        raise RuntimeError("raw HTTP 500 token=secret")

    repository.find_by_ids = _boom  # type: ignore[method-assign]

    with pytest.raises(PartnerMatchingError) as caught:
        PartnerMatchingEngine(FakeProvider(repository)).match_invoice(_invoice(), company_id=COMPANY_ID)
    assert "secret" not in str(caught.value)


# --------------------------------------------------------------------------- MATCH_EXISTING


class _SourceReader:
    def get(self, *, review_id: str, company_id: int) -> ReviewSourceInvoiceEvidence:
        return ReviewSourceInvoiceEvidence(
            review_id=REVIEW_ID,
            company_id=COMPANY_ID,
            review_version=1,
            source_invoice_id=ETTN,
            invoice=_invoice(),
        )


class _PartnerReader:
    def __init__(self, record: ResolutionPartnerRecord) -> None:
        self._record = record

    def find_partner_by_id(self, partner_id: int) -> ResolutionPartnerRecord | None:
        return self._record if self._record.id == partner_id else None


def _validate(record: ResolutionPartnerRecord):
    resolution = SupplierResolution(
        mode=SupplierResolutionMode.MATCH_EXISTING,
        review_id=REVIEW_ID,
        company_id=COMPANY_ID,
        review_version=1,
        source_invoice_id=ETTN,
        resolved_partner_id=record.id,
        approved_by="onur",
    )
    validator = ValidateSupplierResolutionUseCase(
        source_invoice_reader=_SourceReader(), partner_reader=_PartnerReader(record)
    )
    return validator.execute(resolution)


def test_match_existing_rejects_a_child_contact_naming_its_commercial_partner() -> None:
    child = ResolutionPartnerRecord(
        id=440, name="Atakhan Erol", vat=VAT, active=True, company_id=None, commercial_partner_id=439
    )

    with pytest.raises(SupplierResolutionPartnerNotCommercialError) as caught:
        _validate(child)

    # Subclass of the mismatch error, so the existing 409 mapping applies unchanged.
    assert isinstance(caught.value, SupplierResolutionPartnerMismatchError)
    assert caught.value.error_category == "supplier_resolution_partner_not_commercial"
    assert "439" in str(caught.value)


@pytest.mark.parametrize("commercial_partner_id", [439, None])
def test_match_existing_on_the_company_is_unchanged(commercial_partner_id: int | None) -> None:
    company = ResolutionPartnerRecord(
        id=439, name="CloudSpark", vat=VAT, active=True, company_id=None, commercial_partner_id=commercial_partner_id
    )

    validation = _validate(company)

    assert validation.status is SupplierResolutionValidationStatus.VALID
    assert validation.effective_partner_id == 439


def test_resolution_partner_reader_exposes_commercial_partner_id() -> None:
    reader = OdooSupplierResolutionPartnerReader(partner_repository=FakePartnerRepository(_cloudspark()))

    record = reader.find_partner_by_id(440)

    assert record is not None
    assert record.commercial_partner_id == 439


# --------------------------------------------------------------------------- Workbench evidence


def _evidence(partners: FakePartnerRepository):
    class _Execution:
        def get_review_execution_evidence(self, *, review_id: str, company_id: int, review_version: int):
            return None

    return ReviewEvidenceReader(
        source_reader=_SourceReader(), execution_reader=_Execution(), partner_repository=partners
    ).get(review_id=REVIEW_ID, company_id=COMPANY_ID, review_version=1)


def test_evidence_lists_one_canonical_cloudspark_candidate() -> None:
    evidence = _evidence(FakePartnerRepository(_cloudspark()))

    assert evidence is not None
    assert [candidate.partner_id for candidate in evidence.supplier_candidates] == [439]
    assert evidence.supplier_candidates[0].name == "CloudSpark Bulut Teknolojileri Sanayi ve Ticaret Anonim Şirketi"


def test_evidence_keeps_genuinely_distinct_commercial_candidates() -> None:
    evidence = _evidence(FakePartnerRepository([*_cloudspark(), _company(500)]))

    assert evidence is not None
    assert [candidate.partner_id for candidate in evidence.supplier_candidates] == [439, 500]


def test_evidence_shows_raw_rows_when_canonical_partner_is_unreadable() -> None:
    evidence = _evidence(FakePartnerRepository([_company(439, active=False), _contact(440, 439)]))

    assert evidence is not None
    assert [candidate.partner_id for candidate in evidence.supplier_candidates] == [440]
