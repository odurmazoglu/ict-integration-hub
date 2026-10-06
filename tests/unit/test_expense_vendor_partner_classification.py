"""ICT partner classification + redesigned ONE_OFF_VENDOR lifecycle.

ONE_OFF_VENDOR no longer creates a supplier-specific partner only to archive it after
the Vendor Bill. It creates (or reuses) the supplier's OWN legal ``res.partner``,
classified ``expense_vendor`` in the configured Studio selection
(``x_studio_musteri_tipi`` in production), keeps it active permanently and lets the
normal deterministic exact-VAT matcher reuse it for later invoices.
CREATE_PERMANENT_SUPPLIER classifies its new partner ``vendor``. An existing partner's
classification is never changed -- only evaluated and surfaced.

Every scenario runs the REAL ``ResolveWorkbenchSupplierUseCase`` and REAL
``OdooSupplierPartnerWriter`` against SQLite Hub persistence and a stateful in-memory
fake standing in for Odoo's JSON-2 HTTP boundary (zero real Odoo/production calls).
"""

from __future__ import annotations

import importlib
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.api import dependencies
from app.api.routers.workbench import _status_code_for_exception, router
from app.api.security import AuthenticationMethod, Permission, RequestContext
from app.application.exceptions.supplier_partner import (
    SupplierPartnerClassificationUnavailableError,
    SupplierPartnerInactiveError,
)
from app.application.partner_classification import (
    PartnerClassificationOutcome,
    SupplierPartnerClassification,
    evaluate_existing_classification,
)
from app.application.workbench.dto import ReviewItem, ReviewStatus
from app.application.workbench.evidence import ReviewSourceInvoiceEvidence
from app.application.workbench.exceptions import OneOffVendorArchiveRetiredError
from app.application.workbench.one_off_vendor_retirement import (
    OneOffVendorRetirement,
    OneOffVendorRetirementStatus,
)
from app.application.workbench.queries import ReviewDetailQuery
from app.application.workbench.reclassification import ReviewReclassificationResult
from app.application.workbench.retirement_recovery import GetOneOffVendorRetirementUseCase
from app.application.workbench.supplier_remediation import (
    ResolveWorkbenchSupplierCommand,
    SupplierPartnerWriteEffectStatus,
    SupplierRemediationEffect,
    SupplierRemediationResult,
    SupplierRemediationStatus,
)
from app.application.workbench.supplier_remediation_use_cases import ResolveWorkbenchSupplierUseCase
from app.application.workbench.supplier_resolution import (
    ResolutionPartnerRecord,
    SupplierResolution,
    SupplierResolutionMode,
)
from app.application.workbench.supplier_resolution_use_cases import ValidateSupplierResolutionUseCase
from app.application.workflow import ManualReviewReason, ManualReviewReasonCode, WorkflowType
from app.billing import VendorBillBuilder
from app.db.base import Base
from app.domain.invoice import Header, InternalInvoice, InvoiceLine, MonetaryTotals, Party, Tax
from app.erp.models import Partner
from app.erp.write.odoo_supplier_partner_writer import (
    OdooPartnerClassificationFieldConfig,
    OdooSupplierPartnerRepository,
    OdooSupplierPartnerWritePolicy,
    OdooSupplierPartnerWriter,
)
from app.matching import PartnerMatchingEngine, PartnerMatchStatus
from app.models.workbench_review_item import WorkbenchReviewItem
from app.models.workbench_review_one_off_vendor_retirement import WorkbenchReviewOneOffVendorRetirement
from app.models.workbench_review_supplier_remediation_effect import WorkbenchReviewSupplierRemediationEffect
from app.models.workbench_review_supplier_resolution import WorkbenchReviewSupplierResolution
from app.persistence import (
    SqlAlchemyReviewOneOffVendorRetirementRepository,
    SqlAlchemyReviewSupplierRemediationEffectRepository,
    SqlAlchemyReviewSupplierResolutionRepository,
    SqlAlchemyUnitOfWork,
)

COMPANY_ID = 1
FIELD = "x_studio_musteri_tipi"
PELIT_VAT = "7280014037"
PELIT_NAME = "PELİT PASTACILIK VE GIDA SANAYİ ANONİM  ŞİRKETİ"
APPLE_VAT = "0710414224"
APPLE_NAME = "APPLE Teknoloji ve Satış Limited Şirketi"
ACTOR = "operator:test"
#: The production selection (verified read-only) after the operator adds expense_vendor.
PRODUCTION_KEYS = ("customer", "prospect", "vendor", "partner", "Karma", "expense_vendor")


# --------------------------------------------------------------------------- stateful fake Odoo


class StatefulOdoo:
    """In-memory ``res.partner`` with Odoo semantics that matter here: an ``ir.default``
    (``customer``) applies only when the create payload omits the field; archived
    records are visible only to ``active in [True, False]`` lookups."""

    def __init__(self, *, selection_keys: tuple[str, ...] = PRODUCTION_KEYS, ir_default: str = "customer") -> None:
        self.partners: list[dict[str, Any]] = []
        self.selection_keys = selection_keys
        self.ir_default = ir_default
        self.create_calls: list[dict[str, Any]] = []
        self._next_id = 451

    def seed(self, **record: Any) -> dict[str, Any]:
        record.setdefault("active", True)
        record.setdefault("company_id", False)
        record.setdefault(FIELD, False)
        self.partners.append(record)
        return record

    async def create_res_partner(self, payload: dict[str, Any]) -> int:
        self.create_calls.append(dict(payload))
        record = {"id": self._next_id, "active": True, "company_id": False, **payload}
        record.setdefault(FIELD, self.ir_default)
        self._next_id += 1
        self.partners.append(record)
        return record["id"]

    async def search_read(self, *, model: str, domain, fields, limit: int = 20, offset: int = 0):
        if ["is_company", "=", True] in domain:  # create duplicate-guard read: no legacy-VAT companies here
            return []
        assert model == "res.partner"
        vat = next(clause[2] for clause in domain if clause[:2] == ["vat", "="])
        include_archived = ["active", "in", [True, False]] in domain
        rows = [p for p in self.partners if p["vat"] == vat and (include_archived or p["active"])]
        return [{key: row.get(key, False) for key in fields} for row in rows][:limit]

    async def read_model_field_metadata(self, *, model: str, field_name: str):
        return [{"name": FIELD, "ttype": "selection"}] if field_name == FIELD else []

    async def read_field_selection_values(self, *, model: str, field_name: str):
        return self.selection_keys if field_name == FIELD else ()

    # The deterministic matcher's repository view of the same state.
    def find_by_tax_number(self, tax_number: str, *, company_id: int | None = None):
        return tuple(
            Partner(id=p["id"], name=p["name"], tax_number=p["vat"], active=p["active"])
            for p in self.partners
            if p["vat"] == tax_number
        )

    def partner(self, partner_id: int) -> dict[str, Any]:
        return next(p for p in self.partners if p["id"] == partner_id)


# --------------------------------------------------------------------------- harness


def _invoice(*, vat: str, name: str, number: str) -> InternalInvoice:
    return InternalInvoice(
        header=Header(
            invoice_number=number,
            invoice_uuid=f"00000000-0000-4000-8000-{abs(hash(number)) % 10**12:012d}",
            ettn=f"ETTN-{number.removeprefix('INV-')}",
            issue_date=date(2026, 9, 29),
            currency_code="TRY",
        ),
        supplier=Party(name=name, tax_number=vat),
        customer=Party(name="ICT TEKNOLOJİ", tax_number="4651205941"),
        totals=MonetaryTotals(payable_amount=Decimal("1200.00")),
        lines=(
            InvoiceLine(
                line_number="1",
                description="Customer-visit gift",
                quantity=Decimal("1"),
                unit_code="C62",
                unit_price=Decimal("1000.00"),
                taxes=(Tax(tax_type="KDV", rate=Decimal("20")),),
            ),
        ),
    )


def _supplier_not_found() -> ManualReviewReason:
    return ManualReviewReason(
        code=ManualReviewReasonCode.SUPPLIER_NOT_FOUND,
        message="No active supplier partner for VKN.",
        source="partner_matching",
        candidate_count=0,
    )


class _ReviewReader:
    def __init__(self, item: ReviewItem) -> None:
        self.item = item

    def get_review_item(self, query: ReviewDetailQuery) -> ReviewItem:
        return self.item


class _SourceReader:
    def __init__(self, evidence: ReviewSourceInvoiceEvidence) -> None:
        self.evidence = evidence

    def get(self, *, review_id: str, company_id: int) -> ReviewSourceInvoiceEvidence:
        return self.evidence


class _PartnerReader:
    """MATCH_EXISTING/CREATE_PERMANENT re-validation reads the partner from fake Odoo."""

    def __init__(self, odoo: StatefulOdoo) -> None:
        self.odoo = odoo

    def find_partner_by_id(self, partner_id: int) -> ResolutionPartnerRecord | None:
        for p in self.odoo.partners:
            if p["id"] == partner_id:
                return ResolutionPartnerRecord(
                    id=p["id"], name=p["name"], vat=p["vat"], active=p["active"], company_id=None
                )
        return None


class _MatcherBackedReclassifier:
    """Reclassifies using the REAL deterministic PartnerMatchingEngine against fake Odoo."""

    def __init__(self, odoo: StatefulOdoo, invoice: InternalInvoice) -> None:
        self.engine = PartnerMatchingEngine(SimpleNamespace(partner_repository=odoo))
        self.invoice = invoice

    async def execute(self, command):
        match = self.engine.match_invoice(self.invoice, company_id=command.company_id)
        reasons = () if match.status is PartnerMatchStatus.MATCHED else (_supplier_not_found(),)
        return ReviewReclassificationResult(
            review_id=command.review_id,
            company_id=command.company_id,
            from_version=command.expected_version,
            to_version=command.expected_version + 1,
            changed=True,
            previous_workflow=WorkflowType.MANUAL_REVIEW,
            new_workflow=WorkflowType.MANUAL_REVIEW,
            previous_review_reasons=(_supplier_not_found(),),
            new_review_reasons=reasons,
            trigger=command.trigger,
            executable=False,
        )


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            WorkbenchReviewItem.__table__,
            WorkbenchReviewSupplierResolution.__table__,
            WorkbenchReviewSupplierRemediationEffect.__table__,
            WorkbenchReviewOneOffVendorRetirement.__table__,
        ],
    )
    with sessionmaker(bind=engine)() as db_session:
        yield db_session


def _writer(odoo: StatefulOdoo, *, field_name: str | None = FIELD) -> OdooSupplierPartnerWriter:
    return OdooSupplierPartnerWriter(
        repository=OdooSupplierPartnerRepository(client=odoo),
        policy=OdooSupplierPartnerWritePolicy(
            supplier_remediation_write_enabled=True, app_env="staging", odoo_host="test-ictteknoloji.odoo.com"
        ),
        classification_config=OdooPartnerClassificationFieldConfig(field_name=field_name),
    )


def _use_case(
    session: Session,
    odoo: StatefulOdoo,
    *,
    review_id: str,
    vat: str = PELIT_VAT,
    name: str = PELIT_NAME,
    version: int = 1,
    field_name: str | None = FIELD,
) -> ResolveWorkbenchSupplierUseCase:
    invoice = _invoice(vat=vat, name=name, number=f"INV-{review_id}")
    source = ReviewSourceInvoiceEvidence(
        review_id=review_id,
        company_id=COMPANY_ID,
        review_version=version,
        source_invoice_id=f"ETTN-{review_id}",
        invoice=invoice,
    )
    review = ReviewItem(
        review_id=review_id,
        invoice_id=f"ETTN-{review_id}",
        invoice_number=f"INV-{review_id}",
        supplier_tax_number=vat,
        supplier_name=name,
        invoice_date=date(2026, 9, 29),
        currency="TRY",
        total_amount=Decimal("1200.00"),
        workflow=WorkflowType.MANUAL_REVIEW,
        status=ReviewStatus.PENDING_REVIEW,
        review_reasons=(_supplier_not_found(),),
        version=version,
    )
    return ResolveWorkbenchSupplierUseCase(
        review_reader=_ReviewReader(review),
        source_invoice_reader=_SourceReader(source),
        resolution_validator=ValidateSupplierResolutionUseCase(
            source_invoice_reader=_SourceReader(source), partner_reader=_PartnerReader(odoo)
        ),
        resolution_writer=SqlAlchemyReviewSupplierResolutionRepository(session),
        remediation_effect_writer=SqlAlchemyReviewSupplierRemediationEffectRepository(session),
        supplier_partner_writer=_writer(odoo, field_name=field_name),
        reclassifier=_MatcherBackedReclassifier(odoo, invoice),
        unit_of_work=SqlAlchemyUnitOfWork(session),
        retirement_writer=SqlAlchemyReviewOneOffVendorRetirementRepository(session),
    )


def _command(review_id: str, mode: SupplierResolutionMode, *, version: int = 1) -> ResolveWorkbenchSupplierCommand:
    return ResolveWorkbenchSupplierCommand(
        review_id=review_id,
        company_id=COMPANY_ID,
        expected_version=version,
        mode=mode,
        approved_by=ACTOR,
    )


def _seed_effect(session: Session, *, review_id: str, partner_id: int, mode=SupplierResolutionMode.ONE_OFF_VENDOR):
    SqlAlchemyReviewSupplierRemediationEffectRepository(session).create_remediation_effect(
        SupplierRemediationEffect(
            review_id=review_id,
            company_id=COMPANY_ID,
            review_version=2,
            source_invoice_id=f"ETTN-{review_id}",
            mode=mode,
            resolved_partner_id=partner_id,
            partner_write_status=SupplierPartnerWriteEffectStatus.CREATED,
            source_supplier_tax_number=PELIT_VAT,
            approved_by=ACTOR,
        )
    )
    session.commit()


def _audit_snapshot(session: Session) -> list[tuple]:
    rows: list[tuple] = []
    for model in (
        WorkbenchReviewSupplierResolution,
        WorkbenchReviewSupplierRemediationEffect,
        WorkbenchReviewOneOffVendorRetirement,
    ):
        for row in session.scalars(select(model).order_by(model.id)).all():
            rows.append((model.__tablename__, *(getattr(row, column.key) for column in model.__table__.columns)))
    return rows


# --------------------------------------------------------------------------- 1, 2: classified on create


async def test_1_create_permanent_supplier_creates_partner_classified_vendor(session: Session) -> None:
    odoo = StatefulOdoo()
    result = await _use_case(session, odoo, review_id="r-apple", vat=APPLE_VAT, name=APPLE_NAME).execute(
        _command("r-apple", SupplierResolutionMode.CREATE_PERMANENT_SUPPLIER)
    )

    assert odoo.create_calls == [{"name": APPLE_NAME, "vat": APPLE_VAT, FIELD: "vendor"}]
    assert odoo.partner(result.effective_partner_id)[FIELD] == "vendor"
    assert result.status is SupplierRemediationStatus.RESOLVED
    assert result.partner_classification_outcome is PartnerClassificationOutcome.CLASSIFIED_ON_CREATE
    assert result.partner_classification_value == "vendor"
    assert result.one_off_vendor_hub_owned is None


async def test_2_one_off_vendor_creates_partner_classified_expense_vendor(session: Session) -> None:
    odoo = StatefulOdoo()
    result = await _use_case(session, odoo, review_id="r-pelit").execute(
        _command("r-pelit", SupplierResolutionMode.ONE_OFF_VENDOR)
    )

    assert odoo.create_calls == [{"name": PELIT_NAME.strip(), "vat": PELIT_VAT, FIELD: "expense_vendor"}]
    created = odoo.partner(result.effective_partner_id)
    assert created[FIELD] == "expense_vendor"
    assert result.status is SupplierRemediationStatus.RESOLVED
    assert result.partner_write_status is SupplierPartnerWriteEffectStatus.CREATED
    assert result.partner_classification_outcome is PartnerClassificationOutcome.CLASSIFIED_ON_CREATE
    assert result.one_off_vendor_hub_owned is True


# --------------------------------------------------------------------------- 3, 6: active forever, no lifecycle


async def test_3_6_one_off_vendor_partner_stays_active_and_no_retirement_is_created(session: Session) -> None:
    odoo = StatefulOdoo()
    result = await _use_case(session, odoo, review_id="r-pelit").execute(
        _command("r-pelit", SupplierResolutionMode.ONE_OFF_VENDOR)
    )

    assert odoo.partner(result.effective_partner_id)["active"] is True
    assert result.one_off_vendor_retirement_status is None
    assert session.query(WorkbenchReviewOneOffVendorRetirement).count() == 0
    # No archive executor exists any more: nothing can archive it after a Vendor Bill.
    # (The full real-API Vendor Bill run proving the partner is left untouched lives in
    # test_vendor_bill_production_wiring.py::test_runtime_is_committed_before_projection_
    # and_never_retires_the_partner.)
    for retired in (
        "app.application.workbench.one_off_vendor_use_cases",
        "app.erp.write.odoo_one_off_vendor_retirement_writer",
        "app.application.ports.one_off_vendor_retirement_writer",
    ):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(retired)


def test_3_no_res_partner_archive_or_update_path_exists_in_app_code() -> None:
    offenders = [
        str(path)
        for path in Path("app").rglob("*.py")
        if "res.partner/write" in path.read_text(encoding="utf-8")
        or "archive_res_partner" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


# --------------------------------------------------------------------------- 4, 5: deterministic reuse


async def test_4_5_later_invoice_same_vat_deterministically_reuses_the_same_partner(session: Session) -> None:
    odoo = StatefulOdoo()
    first = await _use_case(session, odoo, review_id="r-pelit-1").execute(
        _command("r-pelit-1", SupplierResolutionMode.ONE_OFF_VENDOR)
    )

    # A later invoice from the same VAT: the normal import-time deterministic matcher
    # finds the (still active) supplier partner -- SUPPLIER_NOT_FOUND never arises.
    later = _invoice(vat=PELIT_VAT, name=PELIT_NAME, number="INV-LATER")
    match = PartnerMatchingEngine(SimpleNamespace(partner_repository=odoo)).match_invoice(later, company_id=COMPANY_ID)
    assert match.status is PartnerMatchStatus.MATCHED
    assert match.partner_id == first.effective_partner_id

    # Even an explicit ONE_OFF_VENDOR on a second review reuses it (Hub-owned).
    second = await _use_case(session, odoo, review_id="r-pelit-2").execute(
        _command("r-pelit-2", SupplierResolutionMode.ONE_OFF_VENDOR)
    )
    assert second.effective_partner_id == first.effective_partner_id
    assert second.partner_write_status is SupplierPartnerWriteEffectStatus.ALREADY_EXISTS
    assert second.partner_classification_outcome is PartnerClassificationOutcome.ALREADY_CLASSIFIED
    assert len(odoo.create_calls) == 1
    assert len([p for p in odoo.partners if p["vat"] == PELIT_VAT]) == 1
    assert session.query(WorkbenchReviewOneOffVendorRetirement).count() == 0


# --------------------------------------------------------------------------- 7, 15: history


async def test_7_historical_retirement_records_remain_readable(session: Session) -> None:
    """Pelit-shaped historical state: a pre-redesign pending retirement for partner 452
    stays readable both through the GET use case and on an already-applied replay."""

    session.add(
        WorkbenchReviewItem(
            review_id="r-hist",
            company_id=COMPANY_ID,
            invoice_id="ETTN-r-hist",
            invoice_number="INV-r-hist",
            supplier_tax_number=PELIT_VAT,
            supplier_name=PELIT_NAME,
            invoice_date=date(2026, 9, 29),
            currency="TRY",
            total_amount=Decimal("1200.00"),
            workflow="manual_review",
            status="pending_review",
            review_reasons=[],
            warnings=[],
            version=3,
            idempotency_key="uyumsoft:1:r-hist",
        )
    )
    SqlAlchemyReviewSupplierResolutionRepository(session).reserve_supplier_resolution(
        SupplierResolution(
            mode=SupplierResolutionMode.ONE_OFF_VENDOR,
            review_id="r-hist",
            company_id=COMPANY_ID,
            review_version=2,
            source_invoice_id="ETTN-r-hist",
            approved_by=ACTOR,
        )
    )
    _seed_effect(session, review_id="r-hist", partner_id=452)
    SqlAlchemyReviewOneOffVendorRetirementRepository(session).create_retirement(
        OneOffVendorRetirement(
            review_id="r-hist",
            company_id=COMPANY_ID,
            review_version=2,
            resolved_partner_id=452,
            status=OneOffVendorRetirementStatus.PENDING_VENDOR_BILL,
        )
    )
    session.commit()

    retirement = GetOneOffVendorRetirementUseCase(
        review_reader=_ReviewReader(SimpleNamespace()),
        retirement_reader=SqlAlchemyReviewOneOffVendorRetirementRepository(session),
    ).execute(review_id="r-hist", company_id=COMPANY_ID)
    assert (retirement.resolved_partner_id, retirement.status) == (
        452,
        OneOffVendorRetirementStatus.PENDING_VENDOR_BILL,
    )

    # Replay of the historical, already-applied resolution (review advanced to v3):
    odoo = StatefulOdoo()
    use_case = _use_case(session, odoo, review_id="r-hist", version=3)
    replay = await use_case.execute(_command("r-hist", SupplierResolutionMode.ONE_OFF_VENDOR, version=2))
    assert replay.already_applied is True
    assert replay.effective_partner_id == 452
    assert replay.one_off_vendor_retirement_status is OneOffVendorRetirementStatus.PENDING_VENDOR_BILL
    assert replay.partner_classification_outcome is None  # no Odoo write on a replay
    assert odoo.create_calls == []


async def test_15_existing_historical_audit_records_remain_unchanged(session: Session) -> None:
    _seed_effect(session, review_id="r-hist-a", partner_id=452)
    SqlAlchemyReviewOneOffVendorRetirementRepository(session).create_retirement(
        OneOffVendorRetirement(
            review_id="r-hist-a",
            company_id=COMPANY_ID,
            review_version=2,
            resolved_partner_id=452,
            status=OneOffVendorRetirementStatus.PENDING_VENDOR_BILL,
        )
    )
    session.commit()
    before = _audit_snapshot(session)

    odoo = StatefulOdoo()
    await _use_case(session, odoo, review_id="r-new", vat=APPLE_VAT, name=APPLE_NAME).execute(
        _command("r-new", SupplierResolutionMode.ONE_OFF_VENDOR)
    )

    after = _audit_snapshot(session)
    # Every pre-existing audit row is byte-identical afterwards; only new rows were added.
    assert set(before) <= set(after)
    assert session.query(WorkbenchReviewOneOffVendorRetirement).count() == 1  # only the historical row


# --------------------------------------------------------------------------- 8, 9: existing classifications


@pytest.mark.parametrize("existing", ["customer", "vendor", "partner", "Karma"])
async def test_8_existing_meaningful_classification_is_never_overwritten(session: Session, existing: str) -> None:
    """A Hub-owned partner (prior ONE_OFF_VENDOR effect) whose classification an operator
    (or Odoo's ir.default, e.g. Pelit 452 = customer) set to something else is reused
    but never reclassified -- the condition is surfaced instead."""

    odoo = StatefulOdoo()
    odoo.seed(id=452, name=PELIT_NAME, vat=PELIT_VAT, **{FIELD: existing})
    _seed_effect(session, review_id="r-prior", partner_id=452)

    result = await _use_case(session, odoo, review_id="r-pelit").execute(
        _command("r-pelit", SupplierResolutionMode.ONE_OFF_VENDOR)
    )

    assert result.effective_partner_id == 452
    assert odoo.partner(452)[FIELD] == existing  # untouched
    assert odoo.create_calls == []
    assert result.partner_classification_outcome is PartnerClassificationOutcome.DIFFERENT_CLASSIFICATION_PRESERVED
    assert result.partner_classification_value == existing


@pytest.mark.parametrize(
    ("existing", "outcome"),
    [
        ("expense_vendor", PartnerClassificationOutcome.ALREADY_CLASSIFIED),
        (False, PartnerClassificationOutcome.UNCLASSIFIED_PRESERVED),
        ("customer", PartnerClassificationOutcome.DIFFERENT_CLASSIFICATION_PRESERVED),
    ],
)
async def test_9_hub_owned_expense_vendor_classification_is_deterministic(
    session: Session, existing: Any, outcome: PartnerClassificationOutcome
) -> None:
    odoo = StatefulOdoo()
    odoo.seed(id=452, name=PELIT_NAME, vat=PELIT_VAT, **{FIELD: existing})
    _seed_effect(session, review_id="r-prior", partner_id=452)

    results = [
        await _use_case(session, odoo, review_id=f"r-{n}").execute(
            _command(f"r-{n}", SupplierResolutionMode.ONE_OFF_VENDOR)
        )
        for n in (1, 2)
    ]

    assert [r.partner_classification_outcome for r in results] == [outcome, outcome]
    assert odoo.partner(452)[FIELD] == existing
    assert odoo.create_calls == []


def test_9_classification_policy_table() -> None:
    target = SupplierPartnerClassification.EXPENSE_VENDOR
    assert evaluate_existing_classification("expense_vendor", target=target) is (
        PartnerClassificationOutcome.ALREADY_CLASSIFIED
    )
    for empty in (None, False, "", "  "):
        assert evaluate_existing_classification(empty, target=target) is (
            PartnerClassificationOutcome.UNCLASSIFIED_PRESERVED
        )
    for other in ("customer", "vendor", "partner", "Karma", "prospect", "karma", "EXPENSE_VENDOR"):
        assert evaluate_existing_classification(other, target=target) is (
            PartnerClassificationOutcome.DIFFERENT_CLASSIFICATION_PRESERVED
        )
    with pytest.raises(ValueError):
        evaluate_existing_classification("vendor", target="vendor")  # type: ignore[arg-type]


async def test_existing_non_hub_owned_partner_is_still_never_adopted_by_one_off_vendor(session: Session) -> None:
    odoo = StatefulOdoo()
    odoo.seed(id=167, name="Pelit Çikolata ve Gıda Sanayi Anonim Şirketi", vat=PELIT_VAT)
    from app.application.workbench.exceptions import SupplierResolutionOneOffVendorNotHubOwnedError

    with pytest.raises(SupplierResolutionOneOffVendorNotHubOwnedError):
        await _use_case(session, odoo, review_id="r-pelit").execute(
            _command("r-pelit", SupplierResolutionMode.ONE_OFF_VENDOR)
        )
    assert odoo.partner(167)[FIELD] is False
    assert odoo.create_calls == []


async def test_archived_historical_one_off_partner_fails_closed_never_reactivated(session: Session) -> None:
    odoo = StatefulOdoo()
    odoo.seed(id=448, name="D-MARKET", vat=PELIT_VAT, active=False, **{FIELD: "customer"})
    _seed_effect(session, review_id="r-prior", partner_id=448)

    with pytest.raises(SupplierPartnerInactiveError):
        await _use_case(session, odoo, review_id="r-next").execute(
            _command("r-next", SupplierResolutionMode.ONE_OFF_VENDOR)
        )
    assert odoo.partner(448)["active"] is False
    assert odoo.create_calls == []
    assert session.query(WorkbenchReviewOneOffVendorRetirement).count() == 0


# --------------------------------------------------------------------------- 10, 11


@pytest.mark.parametrize(
    ("field_name", "keys"),
    [
        (None, PRODUCTION_KEYS),
        ("x_studio_does_not_exist", PRODUCTION_KEYS),
        (FIELD, ("customer", "prospect", "vendor", "partner", "Karma")),  # expense_vendor not added yet
    ],
)
async def test_10_missing_or_misconfigured_classification_fails_safely(
    session: Session, field_name: str | None, keys: tuple[str, ...]
) -> None:
    odoo = StatefulOdoo(selection_keys=keys)
    with pytest.raises(SupplierPartnerClassificationUnavailableError):
        await _use_case(session, odoo, review_id="r-pelit", field_name=field_name).execute(
            _command("r-pelit", SupplierResolutionMode.ONE_OFF_VENDOR)
        )
    assert odoo.create_calls == []
    assert odoo.partners == []
    assert session.query(WorkbenchReviewSupplierRemediationEffect).count() == 0
    assert _status_code_for_exception(SupplierPartnerClassificationUnavailableError("x")) == 503


@pytest.mark.parametrize(
    "mode", [SupplierResolutionMode.CREATE_PERMANENT_SUPPLIER, SupplierResolutionMode.ONE_OFF_VENDOR]
)
async def test_11_odoo_default_customer_cannot_override_the_explicit_classification(
    session: Session, mode: SupplierResolutionMode
) -> None:
    odoo = StatefulOdoo(ir_default="customer")
    result = await _use_case(session, odoo, review_id="r-x").execute(_command("r-x", mode))

    expected = "vendor" if mode is SupplierResolutionMode.CREATE_PERMANENT_SUPPLIER else "expense_vendor"
    assert odoo.partner(result.effective_partner_id)[FIELD] == expected
    assert all(FIELD in call for call in odoo.create_calls)


# --------------------------------------------------------------------------- 12: idempotency


@pytest.mark.parametrize(
    "mode", [SupplierResolutionMode.CREATE_PERMANENT_SUPPLIER, SupplierResolutionMode.ONE_OFF_VENDOR]
)
async def test_12_resolution_remains_idempotent(session: Session, mode: SupplierResolutionMode) -> None:
    odoo = StatefulOdoo()
    first = await _use_case(session, odoo, review_id="r-x").execute(_command("r-x", mode))
    replay = await _use_case(session, odoo, review_id="r-x", version=2).execute(_command("r-x", mode))

    assert replay.already_applied is True
    assert replay.effective_partner_id == first.effective_partner_id
    assert len(odoo.create_calls) == 1
    assert session.query(WorkbenchReviewSupplierRemediationEffect).count() == 1


async def test_12_crash_after_partner_create_resume_never_creates_a_second_partner(session: Session) -> None:
    """The intent is reserved before the Odoo write; if the post-create read-back fails
    after the partner already exists, a retry finds it by exact VAT (with the Hub's own
    explicit classification) instead of creating another."""

    odoo = StatefulOdoo()
    real_search = odoo.search_read
    calls = {"n": 0}

    async def flaky_search(**kwargs):
        if ["is_company", "=", True] in kwargs["domain"]:  # create duplicate-guard read: not a VAT lookup
            return await real_search(**kwargs)
        calls["n"] += 1
        if calls["n"] == 2:  # the writer's post-create read-back on the first attempt
            from app.connectors.exceptions import ConnectorTimeoutError

            raise ConnectorTimeoutError("Odoo request timed out.")
        return await real_search(**kwargs)

    odoo.search_read = flaky_search
    use_case = _use_case(session, odoo, review_id="r-x", vat=APPLE_VAT, name=APPLE_NAME)
    with pytest.raises(Exception):  # noqa: B017 - translated safe transport error
        await use_case.execute(_command("r-x", SupplierResolutionMode.CREATE_PERMANENT_SUPPLIER))
    assert session.query(WorkbenchReviewSupplierRemediationEffect).count() == 0

    result = await use_case.execute(_command("r-x", SupplierResolutionMode.CREATE_PERMANENT_SUPPLIER))

    assert result.status is SupplierRemediationStatus.RESOLVED
    assert len(odoo.create_calls) == 1
    assert result.partner_write_status is SupplierPartnerWriteEffectStatus.ALREADY_EXISTS
    assert result.partner_classification_outcome is PartnerClassificationOutcome.ALREADY_CLASSIFIED
    assert odoo.partner(result.effective_partner_id)[FIELD] == "vendor"


# --------------------------------------------------------------------------- 13: no customer creation


def test_13_no_customer_partner_auto_creation_path_exists() -> None:
    assert {member.value for member in SupplierPartnerClassification} == {"vendor", "expense_vendor"}
    create_callers = sorted(
        str(path) for path in Path("app").rglob("*.py") if "create_res_partner(" in path.read_text(encoding="utf-8")
    )
    assert create_callers == ["app/connectors/odoo/client.py", "app/erp/write/odoo_supplier_partner_writer.py"]
    writer_users = sorted(
        str(path)
        for path in Path("app").rglob("*.py")
        if "OdooSupplierPartnerWriter(" in path.read_text(encoding="utf-8")
    )
    # The class definition itself plus its single composition root.
    assert writer_users == ["app/composition/supplier_remediation.py", "app/erp/write/odoo_supplier_partner_writer.py"]
    for module in (
        "app/erp/write/odoo_customer_invoice_writer.py",
        "app/erp/write/odoo_customer_quotation_writer.py",
        "app/application/execution/customer_quotation_strategy.py",
    ):
        source = Path(module).read_text(encoding="utf-8")
        assert "res.partner/create" not in source
        assert "create_res_partner" not in source


# --------------------------------------------------------------------------- 14: Vendor Bill partner


async def test_14_vendor_bill_partner_is_the_actual_supplier_specific_partner(session: Session) -> None:
    odoo = StatefulOdoo()
    resolved = await _use_case(session, odoo, review_id="r-pelit").execute(
        _command("r-pelit", SupplierResolutionMode.ONE_OFF_VENDOR)
    )
    invoice = _invoice(vat=PELIT_VAT, name=PELIT_NAME, number="INV-r-pelit")
    match = PartnerMatchingEngine(SimpleNamespace(partner_repository=odoo)).match_invoice(invoice, company_id=1)

    assert match.partner_id == resolved.effective_partner_id
    assert odoo.partner(match.partner_id)["vat"] == PELIT_VAT  # never a shared/generic partner
    builder_source = Path("app/billing/builder.py").read_text(encoding="utf-8")
    assert "supplier_id=partner_match.partner_id" in builder_source
    assert VendorBillBuilder is not None


# --------------------------------------------------------------------------- API surface


def _api(result: SupplierRemediationResult) -> TestClient:
    class _UseCase:
        async def execute(self, command):
            return result

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[dependencies.get_resolve_workbench_supplier_use_case] = lambda: _UseCase()
    app.dependency_overrides[dependencies.get_request_context] = lambda: RequestContext(
        user_id="finance",
        user_name="Finance",
        company_id=COMPANY_ID,
        permissions=(Permission.WORKBENCH_REVIEW_DECIDE,),
        trace_id="t",
        authentication_method=AuthenticationMethod.JWT,
    )
    return TestClient(app)


def _result(outcome: PartnerClassificationOutcome | None, value: str | None) -> SupplierRemediationResult:
    return SupplierRemediationResult(
        review_id="r-pelit",
        company_id=COMPANY_ID,
        mode=SupplierResolutionMode.ONE_OFF_VENDOR,
        status=SupplierRemediationStatus.RESOLVED,
        previous_version=1,
        current_version=2,
        current_workflow=WorkflowType.MANUAL_REVIEW,
        effective_partner_id=452,
        partner_write_status=SupplierPartnerWriteEffectStatus.ALREADY_EXISTS,
        reclassified=True,
        one_off_vendor_hub_owned=True,
        partner_classification_outcome=outcome,
        partner_classification_value=value,
    )


@pytest.mark.parametrize(
    ("outcome", "value", "warned"),
    [
        (PartnerClassificationOutcome.CLASSIFIED_ON_CREATE, "expense_vendor", False),
        (PartnerClassificationOutcome.ALREADY_CLASSIFIED, "expense_vendor", False),
        (PartnerClassificationOutcome.DIFFERENT_CLASSIFICATION_PRESERVED, "customer", True),
        (PartnerClassificationOutcome.UNCLASSIFIED_PRESERVED, None, True),
        (None, None, False),
    ],
)
def test_api_surfaces_classification_and_warns_on_attention(outcome, value, warned: bool) -> None:
    with _api(_result(outcome, value)) as client:
        response = client.post(
            "/api/workbench/reviews/r-pelit/supplier-resolution",
            json={"mode": "one_off_vendor", "expected_version": 1},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["data"]["partner_classification_outcome"] == (outcome.value if outcome else None)
    assert body["data"]["partner_classification"] == value
    assert bool(body["warnings"]) is warned


def test_archive_retired_error_maps_to_gone() -> None:
    assert _status_code_for_exception(OneOffVendorArchiveRetiredError("x")) == 410
