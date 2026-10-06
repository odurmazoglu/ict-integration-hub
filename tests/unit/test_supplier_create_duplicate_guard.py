"""Create-time guard: never create a supplier that may duplicate an Odoo company stored
under a missing/invalid VAT (production: ICT Bulut partner 24 held legacy code 983551
while its e-invoices carry VKN 4650459971).

VAT stays the only deterministic identity -- the guard never matches, links or reuses.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.api.routers.workbench import _status_code_for_exception
from app.application.exceptions.supplier_partner import (
    SupplierPartnerDataIntegrityError,
    SupplierPartnerProbableDuplicateError,
    SupplierPartnerWriteError,
)
from app.application.partner_classification import SupplierPartnerClassification
from app.application.workbench.operator_request_ingestion import _classified_application_error
from app.application.workbench.supplier_remediation import SupplierRemediationStatus
from app.application.workbench.supplier_remediation_use_cases import ResolveWorkbenchSupplierUseCase
from app.application.workbench.supplier_resolution import SupplierResolutionMode
from app.application.workbench.supplier_resolution_use_cases import ValidateSupplierResolutionUseCase
from app.composition.supplier_remediation import build_resolve_workbench_supplier_use_case
from app.connectors.exceptions import ConnectorTimeoutError
from app.core.config import Settings
from app.domain.company_identity import ExistingCompanyRecord, canonical_company_name, probable_existing_companies
from app.domain.invoice.party_tax_identity import is_valid_party_tax_identifier
from app.erp.write import odoo_supplier_partner_writer as writer_module
from app.models.workbench_review_supplier_resolution import WorkbenchReviewSupplierResolution
from app.persistence import SqlAlchemyUnitOfWork
from tests.unit.test_odoo_supplier_partner_writer import FakeJson2Client, _command, _writer
from tests.unit.test_supplier_remediation_orchestration import _Harness, _partner, _source_evidence
from tests.unit.test_supplier_remediation_orchestration import session as orchestration_session  # noqa: F401

ICT_INVOICE_NAME = "ICT BULUT BİLİŞİM A.Ş."
ICT_INVOICE_VKN = "4650459971"
ICT_ODOO_NAME = "ICT Bulut Bilişim Anonim Şirketi"
ICT_LEGACY_VAT = "983551"
GUARD_TEXT = "Odoo'da aynı şirket olabilecek bir kayıt bulundu ancak VKN bilgisi eksik veya geçersiz"


@pytest.fixture()
def session(orchestration_session: Session) -> Session:  # noqa: F811 - reuses the orchestration in-memory DB
    return orchestration_session


def _company(partner_id: int, name: str, vat: Any, **overrides: Any) -> dict[str, Any]:
    record = {"id": partner_id, "name": name, "vat": vat, "active": True, "is_company": True, "parent_id": False}
    record.update(overrides)
    return record


def _record(partner_id: int, name: str, vat: str | None, **overrides: Any) -> ExistingCompanyRecord:
    values = {"partner_id": partner_id, "name": name, "vat": vat, "active": True, "is_company": True, "parent_id": None}
    values.update(overrides)
    return ExistingCompanyRecord(**values)


# --------------------------------------------------------------------------- pure rule


@pytest.mark.parametrize(
    ("invoice_name", "odoo_name"),
    [
        (ICT_INVOICE_NAME, ICT_ODOO_NAME),  # A.Ş. vs Anonim Şirketi, Turkish casing
        ("DALGAKIRAN MAK.SAN.VE TİC.AŞ.", "Dalgakıran Makina Sanayi ve Ticaret Anonim Şirketi"),  # glued abbreviations
        ("DALGAKIRAN MAKİNA SAN. VE TİC.  A.Ş.", "Dalgakıran Makina Sanayi ve Ticaret Anonim Şirketi"),
        ("Denge Bilgisayar San. ve Tic. Ltd.Şti.", "Denge Bilgisayar Sanayi ve Ticaret Limited Şirketi"),
        ("ICT Bulut Bilişim A. S.", "ICT BULUT BILISIM ANONIM SIRKETI"),  # spaced A. S., ASCII-only spelling
    ],
)
def test_supported_legal_name_variants_share_one_canonical_core(invoice_name: str, odoo_name: str) -> None:
    assert canonical_company_name(invoice_name) is not None
    assert canonical_company_name(invoice_name) == canonical_company_name(odoo_name)


@pytest.mark.parametrize("weak", ["Melike", "ICT A.Ş.", "A.Ş.", "", None])
def test_names_too_weak_to_compare_never_produce_a_core(weak: str | None) -> None:
    assert canonical_company_name(weak) is None


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        (ICT_INVOICE_VKN, True),
        ("2680149578", True),
        ("10000000146", True),  # TCKN
        (ICT_LEGACY_VAT, False),  # legacy 6-digit code
        ("1601004383", False),  # 10 digits, bad check digit (production partner 57)
        ("12345678901", False),
        ("０１２３４５６７８９", False),  # full-width digits
        (" 4650459971", False),
        (None, False),
    ],
)
def test_turkish_tax_identifier_validity_includes_check_digits(value: str | None, valid: bool) -> None:
    assert is_valid_party_tax_identifier(value) is valid


def test_rule_flags_same_name_company_with_invalid_or_missing_vat() -> None:
    blocking = probable_existing_companies(
        supplier_name=ICT_INVOICE_NAME,
        supplier_tax_number=ICT_INVOICE_VKN,
        candidates=[_record(24, ICT_ODOO_NAME, ICT_LEGACY_VAT), _record(30, ICT_ODOO_NAME, None)],
    )
    assert [c.partner_id for c in blocking] == [24, 30]


def test_rule_ignores_valid_different_vat_children_archived_persons_and_other_names() -> None:
    blocking = probable_existing_companies(
        supplier_name="DALGAKIRAN MAK.SAN.VE TİC.AŞ.",
        supplier_tax_number="2680149578",
        candidates=[
            _record(1, "Dalgakıran Makina Sanayi ve Ticaret A.Ş.", ICT_INVOICE_VKN),  # different VALID VKN (case C)
            _record(2, "Dalgakıran Makina Sanayi ve Ticaret A.Ş.", "272183", parent_id=75),  # child contact
            _record(3, "Dalgakıran Makina Sanayi ve Ticaret A.Ş.", "272183", active=False),  # archived
            _record(4, "Dalgakıran Makina Sanayi ve Ticaret A.Ş.", "272183", is_company=False),  # person
            _record(5, "IHI Dalgakıran Makina Sanayi ve Ticaret A.Ş.", "28728"),  # similar, not equivalent
            _record(6, "Dalgakıran Makina Sanayi ve Ticaret A.Ş.", "2680149578"),  # exact VAT: not this guard's job
        ],
    )
    assert blocking == ()


# --------------------------------------------------------------------------- writer (real repository, fake Odoo)


def _ict_command(**overrides: Any):
    return _command(supplier_name=ICT_INVOICE_NAME, supplier_tax_number=ICT_INVOICE_VKN, **overrides)


@pytest.mark.parametrize(
    "classification", [SupplierPartnerClassification.VENDOR, SupplierPartnerClassification.EXPENSE_VENDOR]
)
async def test_ict_bulut_legacy_vat_blocks_create_without_any_partner_write(
    classification: SupplierPartnerClassification,
) -> None:
    client = FakeJson2Client(search_results=[], company_results=[_company(24, ICT_ODOO_NAME, ICT_LEGACY_VAT)])

    with pytest.raises(SupplierPartnerProbableDuplicateError) as caught:
        await _writer(client).create_supplier(_ict_command(classification=classification))

    assert client.create_calls == []
    assert caught.value.candidate_partner_ids == (24,)
    assert caught.value.error_category == "supplier_partner_probable_duplicate"
    message = caught.value.safe_message
    assert message.startswith(GUARD_TEXT)
    assert "#24" in message and ICT_LEGACY_VAT in message
    assert "Odoo şirket kartındaki VKN'yi kontrol edin" in message
    assert "Error" not in message


async def test_same_name_company_with_missing_vat_blocks_create() -> None:
    client = FakeJson2Client(search_results=[], company_results=[_company(24, ICT_ODOO_NAME, False)])

    with pytest.raises(SupplierPartnerProbableDuplicateError) as caught:
        await _writer(client).create_supplier(_ict_command())

    assert client.create_calls == []
    assert "VKN: yok" in caught.value.safe_message


async def test_several_legacy_candidates_fail_closed_and_are_all_exposed() -> None:
    client = FakeJson2Client(
        search_results=[],
        company_results=[
            _company(31, "ICT Bulut Bilişim Ltd. Şti.", "11"),
            _company(24, ICT_ODOO_NAME, ICT_LEGACY_VAT),
        ],
    )

    with pytest.raises(SupplierPartnerProbableDuplicateError) as caught:
        await _writer(client).create_supplier(_ict_command())

    assert client.create_calls == []
    assert caught.value.candidate_partner_ids == (24, 31)
    assert "#24" in caught.value.safe_message and "#31" in caught.value.safe_message


@pytest.mark.parametrize(
    "company",
    [
        _company(77, "Bulutistan Veri Merkezi Anonim Şirketi", "983551"),  # unrelated invalid-VAT company
        _company(78, "ICT Bulut Bilişim Hizmetleri Anonim Şirketi", "983551"),  # similar, not equivalent
        _company(79, ICT_ODOO_NAME, "2680149578"),  # same name, different VALID VKN (case C)
    ],
)
async def test_non_blocking_companies_keep_existing_create_behavior(company: dict[str, Any]) -> None:
    client = FakeJson2Client(
        search_sequence=[
            [],
            [
                {
                    "id": 501,
                    "name": ICT_INVOICE_NAME,
                    "vat": ICT_INVOICE_VKN,
                    "active": True,
                    "company_id": False,
                    "x_studio_musteri_tipi": "vendor",
                }
            ],
        ],
        company_results=[company],
    )

    result = await _writer(client).create_supplier(_ict_command())

    assert result.partner_id == 501
    assert len(client.create_calls) == 1


async def test_exact_vat_partner_is_reused_without_consulting_the_guard() -> None:
    existing = {
        "id": 24,
        "name": ICT_ODOO_NAME,
        "vat": ICT_INVOICE_VKN,
        "active": True,
        "company_id": False,
        "x_studio_musteri_tipi": "vendor",
    }
    client = FakeJson2Client(search_results=[existing], company_results=[_company(9, ICT_ODOO_NAME, "1")])

    result = await _writer(client).create_supplier(_ict_command())

    assert result.partner_id == 24
    assert client.create_calls == []
    assert client.company_calls == []


async def test_guard_reads_only_active_commercial_companies_of_the_company() -> None:
    client = FakeJson2Client(search_results=[], company_results=[_company(24, ICT_ODOO_NAME, ICT_LEGACY_VAT)])

    with pytest.raises(SupplierPartnerProbableDuplicateError):
        await _writer(client).create_supplier(_ict_command())

    domain = client.company_calls[0]["domain"]
    assert ["is_company", "=", True] in domain
    assert ["parent_id", "=", False] in domain  # child contacts never become candidates
    assert ["active", "=", True] in domain  # archived companies are out of scope
    assert ["company_id", "in", [1, False]] in domain


async def test_guard_read_failure_fails_closed_without_create() -> None:
    class _Failing(FakeJson2Client):
        async def search_read(self, *, model, domain, fields, limit=20, offset=0):
            if ["is_company", "=", True] in domain:
                raise ConnectorTimeoutError("Odoo request timed out.")
            return await super().search_read(model=model, domain=domain, fields=fields, limit=limit, offset=offset)

    client = _Failing(search_results=[])

    with pytest.raises(SupplierPartnerWriteError):  # connector error translated; never a silent create
        await _writer(client).create_supplier(_ict_command())
    assert client.create_calls == []


async def test_guard_pages_through_companies_and_fails_closed_past_its_bound(monkeypatch) -> None:
    class _Paged(FakeJson2Client):
        async def search_read(self, *, model, domain, fields, limit=20, offset=0):
            if ["is_company", "=", True] in domain:
                self.company_calls.append({"offset": offset, "limit": limit})
                return self.company_results[offset : offset + limit]
            return await super().search_read(model=model, domain=domain, fields=fields, limit=limit, offset=offset)

    monkeypatch.setattr(writer_module, "_COMPANY_CANDIDATE_PAGE_SIZE", 2)
    rows = [_company(100 + i, f"Unrelated Company Number {i}", "1") for i in range(4)]
    paged = _Paged(search_results=[], company_results=[*rows, _company(24, ICT_ODOO_NAME, ICT_LEGACY_VAT)])

    with pytest.raises(SupplierPartnerProbableDuplicateError):  # the blocking row is on the last page
        await _writer(paged).create_supplier(_ict_command())
    assert [call["offset"] for call in paged.company_calls] == [0, 2, 4]

    monkeypatch.setattr(writer_module, "_COMPANY_CANDIDATE_MAX_RECORDS", 3)
    bounded = _Paged(search_results=[], company_results=rows)
    with pytest.raises(SupplierPartnerDataIntegrityError):
        await _writer(bounded).create_supplier(_ict_command())
    assert bounded.create_calls == []


# --------------------------------------------------------------------------- orchestration (before reservation)


class _Guard:
    def __init__(self, *, block: bool) -> None:
        self.block = block
        self.calls: list[dict[str, Any]] = []

    async def ensure_no_probable_existing_company(self, *, company_id, supplier_name, supplier_tax_number) -> None:
        self.calls.append({"company_id": company_id, "name": supplier_name, "vat": supplier_tax_number})
        if self.block:
            raise SupplierPartnerProbableDuplicateError(
                f"{GUARD_TEXT}: {ICT_ODOO_NAME} (#24, VKN: {ICT_LEGACY_VAT}).", candidate_partner_ids=(24,)
            )


def _guarded(session: Session, guard: _Guard, *, ict_source: bool = True) -> _Harness:
    # The created/selected partner is re-validated read-only against the source VKN.
    h = (
        _Harness(
            session,
            source=_source_evidence(supplier_name=ICT_INVOICE_NAME, supplier_vat=ICT_INVOICE_VKN),
            partner=_partner(id=6001, vat=ICT_INVOICE_VKN),
        )
        if ict_source
        else _Harness(session)
    )
    h.use_case = ResolveWorkbenchSupplierUseCase(
        review_reader=h.reader,
        source_invoice_reader=h.source_reader,
        resolution_validator=ValidateSupplierResolutionUseCase(
            source_invoice_reader=h.source_reader, partner_reader=h.partner_reader
        ),
        resolution_writer=h.resolution_repo,
        remediation_effect_writer=h.effect_repo,
        supplier_partner_writer=h.writer,
        reclassifier=h.reclassifier,
        unit_of_work=SqlAlchemyUnitOfWork(session),
        retirement_writer=h.retirement_repo,
        create_duplicate_guard=guard,
    )
    return h


@pytest.mark.parametrize(
    "mode", [SupplierResolutionMode.CREATE_PERMANENT_SUPPLIER, SupplierResolutionMode.ONE_OFF_VENDOR]
)
async def test_partner_creating_modes_are_refused_before_any_reservation_or_write(
    session: Session, mode: SupplierResolutionMode
) -> None:
    guard = _Guard(block=True)
    h = _guarded(session, guard)

    with pytest.raises(SupplierPartnerProbableDuplicateError):
        await h.use_case.execute(h.command(mode=mode, resolved_partner_id=None))

    assert guard.calls == [{"company_id": 7, "name": ICT_INVOICE_NAME, "vat": ICT_INVOICE_VKN}]
    assert session.query(WorkbenchReviewSupplierResolution).count() == 0  # MATCH_EXISTING stays possible later
    assert h.writer.calls == []
    assert h.reclassifier.calls == []


@pytest.mark.parametrize(
    ("mode", "partner_id"),
    [(SupplierResolutionMode.MATCH_EXISTING, 4010), (SupplierResolutionMode.USE_ONE_OFF_SUPPLIER, None)],
)
async def test_non_creating_modes_never_consult_the_guard(
    session: Session, mode: SupplierResolutionMode, partner_id: int | None
) -> None:
    guard = _Guard(block=True)
    h = _guarded(session, guard, ict_source=False)

    await h.use_case.execute(h.command(mode=mode, resolved_partner_id=partner_id))

    assert guard.calls == []


async def test_passing_guard_keeps_create_and_exact_replay_unchanged(session: Session) -> None:
    guard = _Guard(block=False)
    h = _guarded(session, guard)
    command = h.command(mode=SupplierResolutionMode.CREATE_PERMANENT_SUPPLIER, resolved_partner_id=None)

    first = await h.use_case.execute(command)
    second = await h.use_case.execute(command)

    assert first.status is SupplierRemediationStatus.RESOLVED
    assert second.already_applied is True
    assert len(h.writer.calls) == 1
    assert len(guard.calls) == 1  # the replay resumes the recorded intent; it is not a new create attempt


# --------------------------------------------------------------------------- operator / HTTP surfaces


def test_operator_request_shows_the_human_readable_turkish_reason() -> None:
    error = SupplierPartnerProbableDuplicateError(
        f"{GUARD_TEXT}: {ICT_ODOO_NAME} (#24, VKN: {ICT_LEGACY_VAT}).", candidate_partner_ids=(24,)
    )

    outcome = _classified_application_error(error)

    assert outcome.message.startswith(f"İşlem reddedildi: {GUARD_TEXT}")
    assert "SupplierPartnerProbableDuplicateError" not in outcome.message


def test_api_maps_the_guard_to_conflict() -> None:
    error = SupplierPartnerProbableDuplicateError("x", candidate_partner_ids=(24,))

    assert _status_code_for_exception(error) == 409


def test_production_composition_wires_the_writer_as_the_pre_reservation_guard(session: Session) -> None:
    use_case = build_resolve_workbench_supplier_use_case(
        session=session,
        settings=Settings(),
        odoo_client=FakeJson2Client(),  # type: ignore[arg-type]
    )

    assert use_case._create_duplicate_guard is use_case._supplier_partner_writer
