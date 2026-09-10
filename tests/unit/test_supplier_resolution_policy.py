"""Explicit supplier-resolution policy for Workbench reviews (P0-3D2C).

Models the three business resolutions for a ``SUPPLIER_NOT_FOUND`` review
(MATCH_EXISTING / USE_ONE_OFF_SUPPLIER / CREATE_PERMANENT_SUPPLIER), persists the
chosen decision as immutable evidence, and validates MATCH_EXISTING against the
immutable source-invoice supplier identity. It creates no Odoo partner, wires no
Vendor Bill execution, and never fakes a ``PartnerMatchResult``.
"""

from __future__ import annotations

import ast
import dataclasses
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.application.workbench.evidence import ReviewSourceInvoiceEvidence
from app.application.workbench.exceptions import (
    SupplierResolutionConflictError,
    SupplierResolutionContractError,
    SupplierResolutionNotFoundError,
    SupplierResolutionPartnerInactiveError,
    SupplierResolutionPartnerMismatchError,
    SupplierResolutionPartnerNotFoundError,
)
from app.application.workbench.supplier_resolution import (
    ResolutionPartnerRecord,
    SupplierResolution,
    SupplierResolutionMode,
    SupplierResolutionValidation,
    SupplierResolutionValidationStatus,
    normalize_supplier_vat,
)
from app.application.workbench.supplier_resolution_use_cases import ValidateSupplierResolutionUseCase
from app.core.config import Settings
from app.db.base import Base
from app.domain.invoice import Header, InternalInvoice, InvoiceLine, MonetaryTotals, Party, Tax
from app.models.workbench_review_classification_evidence import WorkbenchReviewClassificationEvidence
from app.models.workbench_review_item import WorkbenchReviewItem
from app.models.workbench_review_supplier_resolution import WorkbenchReviewSupplierResolution
from app.persistence import SqlAlchemyReviewSupplierResolutionRepository

COMPANY_ID = 7
REVIEW_ID = "review:supplier-resolution-1"
ETTN = "AKYASAM-ETTN-RESOLVE-1"
VKN = "0430367181"


# --------------------------------------------------------------------------- builders


def _source_invoice(*, supplier_vat: str | None = VKN) -> InternalInvoice:
    return InternalInvoice(
        header=Header(
            invoice_number="AKY-1",
            invoice_uuid="00000000-0000-4000-8000-00000000c001",
            ettn=ETTN,
            issue_date=date(2026, 8, 20),
            currency_code="TRY",
        ),
        supplier=Party(name="AKYASAM", tax_number=supplier_vat),
        customer=Party(name="ICT TEKNOLOJI", tax_number="1112223334"),
        totals=MonetaryTotals(payable_amount=Decimal("100.00")),
        lines=(
            InvoiceLine(
                line_number="1",
                description="Yillik aidat",
                quantity=Decimal("1"),
                unit_code="C62",
                unit_price=Decimal("83.33"),
                taxes=(Tax(tax_type="KDV", rate=Decimal("20")),),
            ),
        ),
    )


def _source_evidence(*, supplier_vat: str | None = VKN) -> ReviewSourceInvoiceEvidence:
    return ReviewSourceInvoiceEvidence(
        review_id=REVIEW_ID,
        company_id=COMPANY_ID,
        review_version=1,
        source_invoice_id=ETTN,
        invoice=_source_invoice(supplier_vat=supplier_vat),
    )


class _FakeSourceReader:
    def __init__(self, evidence: ReviewSourceInvoiceEvidence) -> None:
        self._evidence = evidence
        self.calls: list[tuple[str, int]] = []

    def get(self, *, review_id: str, company_id: int) -> ReviewSourceInvoiceEvidence:
        self.calls.append((review_id, company_id))
        return self._evidence


class _FakePartnerReader:
    def __init__(self, partner: ResolutionPartnerRecord | None) -> None:
        self._partner = partner
        self.calls: list[int] = []

    def find_partner_by_id(self, partner_id: int) -> ResolutionPartnerRecord | None:
        self.calls.append(partner_id)
        return self._partner


def _partner(
    *,
    partner_id: int = 4010,
    vat: str | None = VKN,
    active: bool = True,
    company_id: int | None = None,
) -> ResolutionPartnerRecord:
    return ResolutionPartnerRecord(id=partner_id, name="AKYASAM", vat=vat, active=active, company_id=company_id)


def _resolution(**overrides) -> SupplierResolution:
    kwargs = {
        "mode": SupplierResolutionMode.MATCH_EXISTING,
        "review_id": REVIEW_ID,
        "company_id": COMPANY_ID,
        "review_version": 1,
        "source_invoice_id": ETTN,
        "resolved_partner_id": 4010,
        "approved_by": "finance.operator",
    }
    kwargs.update(overrides)
    return SupplierResolution(**kwargs)


_UNSET = object()


def _validator(
    *,
    supplier_vat: str | None = VKN,
    partner: ResolutionPartnerRecord | None | object = _UNSET,
) -> tuple[ValidateSupplierResolutionUseCase, _FakePartnerReader]:
    resolved = _partner(vat=supplier_vat) if partner is _UNSET else partner
    partner_reader = _FakePartnerReader(resolved)  # type: ignore[arg-type]
    use_case = ValidateSupplierResolutionUseCase(
        source_invoice_reader=_FakeSourceReader(_source_evidence(supplier_vat=supplier_vat)),
        partner_reader=partner_reader,
    )
    return use_case, partner_reader


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            WorkbenchReviewItem.__table__,
            WorkbenchReviewClassificationEvidence.__table__,
            WorkbenchReviewSupplierResolution.__table__,
        ],
    )
    factory = sessionmaker(bind=engine)
    with factory() as db_session:
        yield db_session


def _seed_supplier_not_found_review(session: Session) -> None:
    session.add(
        WorkbenchReviewItem(
            review_id=REVIEW_ID,
            company_id=COMPANY_ID,
            invoice_id=ETTN,
            invoice_number="AKY-1",
            supplier_tax_number=VKN,
            supplier_name="AKYASAM",
            invoice_date=date(2026, 8, 20),
            currency="TRY",
            total_amount=Decimal("100.00"),
            workflow="manual_review",
            status="pending_review",
            review_reasons=[{"code": "supplier_not_found", "message": "No supplier partner."}],
            warnings=[],
            version=1,
            idempotency_key="uyumsoft:7:AKYASAM-ETTN-RESOLVE-1",
        )
    )
    session.flush()


# --------------------------------------------------------- Phase 19: mode contract


def test_supplier_resolution_mode_has_exactly_three_business_options() -> None:
    assert {mode.value for mode in SupplierResolutionMode} == {
        "match_existing",
        "use_one_off_supplier",
        "create_permanent_supplier",
    }


def test_supplier_resolution_requires_an_explicit_mode_no_default() -> None:
    mode_field = next(f for f in dataclasses.fields(SupplierResolution) if f.name == "mode")
    assert mode_field.default is dataclasses.MISSING
    assert mode_field.default_factory is dataclasses.MISSING


# --------------------------------------------------------- Phase 20: source trust boundary


def test_supplier_resolution_never_carries_legal_identity_from_the_caller() -> None:
    resolution_fields = {f.name for f in dataclasses.fields(SupplierResolution)}
    validation_fields = {f.name for f in dataclasses.fields(SupplierResolutionValidation)}
    for forbidden in ("supplier_name", "supplier_vat", "supplier_tax_number", "invoice", "internal_invoice"):
        assert forbidden not in resolution_fields
        assert forbidden not in validation_fields


def test_validation_rejects_source_invoice_id_that_disagrees_with_stored_evidence() -> None:
    use_case, _ = _validator()
    with pytest.raises(SupplierResolutionContractError):
        use_case.execute(_resolution(source_invoice_id="SOME-OTHER-ETTN"))


def test_validation_reads_supplier_identity_from_immutable_source_evidence() -> None:
    use_case, _ = _validator(supplier_vat=VKN)
    result = use_case.execute(_resolution())
    assert result.source_supplier_tax_number == VKN


# --------------------------------------------------------- Phase 21: MATCH_EXISTING exact VAT


def test_match_existing_with_exact_vat_is_valid() -> None:
    use_case, partner_reader = _validator(partner=_partner(partner_id=4010, vat=" 0430367181 "))
    result = use_case.execute(_resolution(resolved_partner_id=4010))
    assert result.status is SupplierResolutionValidationStatus.VALID
    assert result.effective_partner_id == 4010
    assert partner_reader.calls == [4010]


def test_match_existing_with_different_vat_fails_closed() -> None:
    use_case, _ = _validator(partner=_partner(vat="9999999999"))
    with pytest.raises(SupplierResolutionPartnerMismatchError):
        use_case.execute(_resolution())


def test_match_existing_with_blank_partner_vat_fails_closed() -> None:
    use_case, _ = _validator(partner=_partner(vat="   "))
    with pytest.raises(SupplierResolutionPartnerMismatchError):
        use_case.execute(_resolution())


def test_match_existing_with_missing_partner_fails_closed() -> None:
    use_case, _ = _validator(partner=None)
    with pytest.raises(SupplierResolutionPartnerNotFoundError):
        use_case.execute(_resolution())


def test_match_existing_requires_a_partner_id_in_the_dto() -> None:
    with pytest.raises(SupplierResolutionContractError):
        _resolution(resolved_partner_id=None)


def test_match_existing_with_inactive_partner_fails_closed() -> None:
    use_case, _ = _validator(partner=_partner(active=False))
    with pytest.raises(SupplierResolutionPartnerInactiveError):
        use_case.execute(_resolution())


def test_match_existing_with_wrong_company_partner_fails_closed() -> None:
    use_case, _ = _validator(partner=_partner(company_id=999))
    with pytest.raises(SupplierResolutionContractError):
        use_case.execute(_resolution())


def test_match_existing_accepts_a_shared_partner() -> None:
    use_case, _ = _validator(partner=_partner(company_id=None))
    assert use_case.execute(_resolution()).status is SupplierResolutionValidationStatus.VALID


def test_match_existing_fails_closed_when_source_invoice_has_no_tax_number() -> None:
    use_case, _ = _validator(supplier_vat=None, partner=_partner(vat=VKN))
    with pytest.raises(SupplierResolutionPartnerMismatchError):
        use_case.execute(_resolution())


# --------------------------------------------------------- Phase 23: CREATE_PERMANENT does not write


def test_create_permanent_supplier_returns_pending_and_performs_no_write() -> None:
    use_case, partner_reader = _validator()
    result = use_case.execute(
        _resolution(mode=SupplierResolutionMode.CREATE_PERMANENT_SUPPLIER, resolved_partner_id=None)
    )
    assert result.status is SupplierResolutionValidationStatus.PENDING_PERMANENT_SUPPLIER
    assert result.effective_partner_id is None
    assert partner_reader.calls == []  # no partner read, and certainly no write


def test_supplier_resolution_modules_do_not_reference_the_controlled_partner_writer() -> None:
    for module_path in (
        "app/application/workbench/supplier_resolution.py",
        "app/application/workbench/supplier_resolution_use_cases.py",
        "app/persistence/workbench_review_supplier_resolution_reader.py",
        "app/erp/odoo/supplier_resolution_partner_reader.py",
    ):
        source = Path(module_path).read_text(encoding="utf-8")
        for token in (
            "SupplierPartnerWriter",
            "OdooSupplierPartnerWriter",
            "create_res_partner",
            "res.partner/create",
            "controlled-supplier-partner-writer",
        ):
            assert token not in source, (module_path, token)


# --------------------------------------------------------- Phase 24: one-off is deferred, no config


def test_no_one_off_supplier_partner_config_is_introduced() -> None:
    assert not hasattr(Settings(), "odoo_one_off_supplier_partner_id")
    assert not hasattr(Settings(), "one_off_supplier_partner_id")


def test_use_one_off_supplier_is_recorded_but_execution_is_not_supported() -> None:
    use_case, _ = _validator()
    result = use_case.execute(_resolution(mode=SupplierResolutionMode.USE_ONE_OFF_SUPPLIER, resolved_partner_id=None))
    assert result.status is SupplierResolutionValidationStatus.ONE_OFF_EXECUTION_NOT_SUPPORTED
    assert result.effective_partner_id is None
    assert result.source_supplier_tax_number == VKN


# --------------------------------------------------------- Phase 22: original evidence preserved


def test_persisting_a_resolution_does_not_touch_the_review_or_classification_evidence(session: Session) -> None:
    _seed_supplier_not_found_review(session)
    before = session.scalar(select(WorkbenchReviewItem))
    snapshot = (before.workflow, before.status, before.version, tuple(before.review_reasons))

    repo = SqlAlchemyReviewSupplierResolutionRepository(session)
    repo.create_supplier_resolution(_resolution())

    after = session.scalar(select(WorkbenchReviewItem))
    assert (after.workflow, after.status, after.version, tuple(after.review_reasons)) == snapshot
    assert session.query(WorkbenchReviewClassificationEvidence).count() == 0
    rows = session.query(WorkbenchReviewSupplierResolution).all()
    assert len(rows) == 1
    assert rows[0].mode == "match_existing"
    assert rows[0].resolved_partner_id == 4010


# --------------------------------------------------------- repository: append-only idempotency


def test_resolution_repository_round_trips(session: Session) -> None:
    _seed_supplier_not_found_review(session)
    repo = SqlAlchemyReviewSupplierResolutionRepository(session)
    created = repo.create_supplier_resolution(
        _resolution(mode=SupplierResolutionMode.USE_ONE_OFF_SUPPLIER, resolved_partner_id=None)
    )
    fetched = repo.get_supplier_resolution(review_id=REVIEW_ID, company_id=COMPANY_ID, review_version=1)
    assert created == fetched
    assert fetched.mode is SupplierResolutionMode.USE_ONE_OFF_SUPPLIER


def test_identical_resolution_replay_returns_the_existing_row(session: Session) -> None:
    _seed_supplier_not_found_review(session)
    repo = SqlAlchemyReviewSupplierResolutionRepository(session)
    first = repo.create_supplier_resolution(_resolution())
    second = repo.create_supplier_resolution(_resolution())
    assert first == second
    assert session.query(WorkbenchReviewSupplierResolution).count() == 1


def test_conflicting_resolution_for_same_review_version_fails_closed(session: Session) -> None:
    _seed_supplier_not_found_review(session)
    repo = SqlAlchemyReviewSupplierResolutionRepository(session)
    repo.create_supplier_resolution(_resolution(mode=SupplierResolutionMode.MATCH_EXISTING, resolved_partner_id=4010))
    with pytest.raises(SupplierResolutionConflictError):
        repo.create_supplier_resolution(
            _resolution(mode=SupplierResolutionMode.CREATE_PERMANENT_SUPPLIER, resolved_partner_id=None)
        )
    assert session.query(WorkbenchReviewSupplierResolution).count() == 1


def test_get_missing_resolution_raises_not_found(session: Session) -> None:
    _seed_supplier_not_found_review(session)
    repo = SqlAlchemyReviewSupplierResolutionRepository(session)
    with pytest.raises(SupplierResolutionNotFoundError):
        repo.get_supplier_resolution(review_id=REVIEW_ID, company_id=COMPANY_ID, review_version=1)


# --------------------------------------------------------- normalization parity


def test_normalization_matches_partner_matching_engine_strip_only() -> None:
    from app.matching.partner import _clean

    for value in ("  0430367181 ", "TR0430367181", "0430367181\n", "  "):
        assert normalize_supplier_vat(value) == (_clean(value))


# --------------------------------------------------------- Phase 26/27/28: regression isolation


@pytest.mark.parametrize(
    "module_path",
    [
        "app/billing/builder.py",
        "app/billing/dto.py",
        "app/application/workbench/evidence.py",
        "app/application/use_cases/import_invoice.py",
        "app/application/use_cases/reclassify_review.py",
        "app/application/rules/deterministic.py",
        "app/erp/write/odoo_vendor_bill_writer.py",
    ],
)
def test_vendor_bill_and_classification_paths_do_not_import_supplier_resolution(module_path: str) -> None:
    tree = ast.parse(Path(module_path).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    for name in imported:
        assert "supplier_resolution" not in name, (module_path, name)
