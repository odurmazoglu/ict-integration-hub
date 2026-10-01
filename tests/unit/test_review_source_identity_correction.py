"""Append-only historical source-identity correction (supplier.tax_number, PR #201).

Before PR #201 the UBL parser stored a party's *first* ``PartyIdentification/cbc:ID``
as its tax number. Suppliers that list MERSISNO (or another identifier) before
their VKN were therefore persisted with the wrong supplier tax number inside the
immutable source-invoice evidence. These tests reproduce that historical state
through the real import path with synthetic documents (no production identifiers)
and prove the audited correction:

* never rewrites the original evidence row;
* is visible to every consumer through the normal effective source reader;
* advances the review exactly once with SOURCE_IDENTITY_CORRECTED and freshly
  recalculated classification;
* refuses on any unsafe precondition, and is idempotent;
* projects to the Workbench only after the Hub commit, never undoing it.
"""

from __future__ import annotations

import hashlib
import io
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.application.commands import ImportInvoiceCommand
from app.application.commands.supplier_partner import CreateSupplierPartnerCommand
from app.application.decision import (
    DecisionEngine,
    ManualReviewStrategy,
    VendorBillReviewRecommendationStrategy,
    WorkflowStrategyResolver,
)
from app.application.dto.supplier_partner import SupplierPartnerWriteResult, SupplierPartnerWriteStatus
from app.application.expense_mapping import OperatingExpenseMatchingEngine
from app.application.rules import InvoiceDecisionRuleEngine
from app.application.rules.deterministic import DeterministicRuleEngine
from app.application.use_cases import ImportInvoiceUseCase
from app.application.use_cases.effective_decision import EffectiveDecisionResolver
from app.application.use_cases.reclassify_review import ReclassifyWorkbenchReviewUseCase
from app.application.workbench import ReviewItemCreationService
from app.application.workbench.exceptions import (
    ReviewDataIntegrityError,
    ReviewNotFoundError,
    SupplierResolutionPartnerMismatchError,
    WorkbenchContractError,
)
from app.application.workbench.projection_sync_contracts import ProjectionSyncOutcome, ProjectionSyncResult
from app.application.workbench.reclassification import ReclassifyReviewCommand, ReviewReclassificationTrigger
from app.application.workbench.source_identity_correction import (
    CorrectionCheckStatus,
    CorrectReviewSourceIdentityCommand,
    ReviewSourceInvoiceCorrection,
    SourceIdentityCorrectionOutcome,
    SourceInvoiceCorrectionField,
    SourceInvoiceCorrectionReason,
    apply_source_invoice_corrections,
    diff_invoice_payloads,
)
from app.application.workbench.source_identity_correction_use_cases import CorrectReviewSourceIdentityUseCase
from app.application.workbench.supplier_remediation import ResolveWorkbenchSupplierCommand
from app.application.workbench.supplier_remediation_use_cases import ResolveWorkbenchSupplierUseCase
from app.application.workbench.supplier_resolution import (
    ResolutionPartnerRecord,
    SupplierResolution,
    SupplierResolutionMode,
)
from app.application.workbench.supplier_resolution_use_cases import ValidateSupplierResolutionUseCase
from app.application.workflow import ManualReviewReasonCode, WorkflowType
from app.cli import correct_review_source_identity as cli
from app.db.base import Base
from app.domain.invoice import InternalInvoice
from app.domain.invoice.parser import legacy_supplier_tax_identifier, parse_ubl_invoice
from app.domain.invoice.party_tax_identity import is_party_tax_identifier
from app.erp.models import Partner
from app.matching import (
    InvoiceProductLineResult,
    InvoiceProductMatchResult,
    PartnerMatchingEngine,
    ProductMatchResult,
    ProductMatchStatus,
)
from app.models.invoice_document import InvoiceDocument
from app.models.uyumsoft_invoice import UyumsoftInvoiceMetadata
from app.models.workbench_review_classification_evidence import WorkbenchReviewClassificationEvidence
from app.models.workbench_review_decision import WorkbenchReviewDecision
from app.models.workbench_review_item import WorkbenchReviewItem
from app.models.workbench_review_reclassification import WorkbenchReviewReclassification
from app.models.workbench_review_source_invoice_correction import WorkbenchReviewSourceInvoiceCorrection
from app.models.workbench_review_source_invoice_evidence import WorkbenchReviewSourceInvoiceEvidence
from app.models.workbench_review_supplier_remediation_effect import WorkbenchReviewSupplierRemediationEffect
from app.models.workbench_review_write_authorization import WorkbenchReviewWriteAuthorization
from app.models.workflow_execution import WorkflowExecution
from app.persistence import (
    SqlAlchemyOperatingExpenseMappingRepository,
    SqlAlchemyReviewAccountingResolutionRepository,
    SqlAlchemyReviewRepository,
    SqlAlchemyReviewSourceInvoiceEvidenceReader,
    SqlAlchemyReviewSupplierRemediationEffectRepository,
    SqlAlchemyReviewSupplierResolutionRepository,
    SqlAlchemyUnitOfWork,
)
from app.persistence.workbench_review_source_invoice_correction_repository import (
    SqlAlchemyReviewSourceInvoiceCorrectionRepository,
)
from app.services.document_storage import LocalDocumentStorage
from app.tax_mapping import InvoiceTaxLineResult, InvoiceTaxMappingResult, TaxMatchResult, TaxMatchStatus, TaxType

COMPANY_ID = 1
BUYER_VKN = "1111111111"
ACTOR = "source-correction-test-operator"
TAX_ID = 8801
NO_PARTNER_REASONS = {
    ManualReviewReasonCode.SUPPLIER_NOT_FOUND.value,
    ManualReviewReasonCode.OPERATING_EXPENSE_MAPPING_REQUIRED.value,
}


@dataclass(frozen=True)
class Scenario:
    """A synthetic historical document shape (no production identifiers)."""

    invoice_number: str
    invoice_uuid: str
    supplier_ids: tuple[tuple[str, str], ...]
    vkn: str
    historical_value: str
    company_id_registration: str | None = None


#: AY-style: MERSISNO listed before the VKN.
AY_STYLE = Scenario(
    invoice_number="SYN2026000000001",
    invoice_uuid="aaaaaaaa-0000-4000-8000-000000000001",
    supplier_ids=(("MERSISNO", "0123456789000017"), ("VKN", "1234567890")),
    vkn="1234567890",
    historical_value="0123456789000017",
)
#: I10-style: leading-zero VKN after MERSISNO and a trade-registry id, with a
#: PartyTaxScheme/CompanyID registration that the old rule never reached.
I10_STYLE = Scenario(
    invoice_number="SYN2026000000002",
    invoice_uuid="bbbbbbbb-0000-4000-8000-000000000002",
    supplier_ids=(("MERSISNO", "0098765432100038"), ("TICARETSICILNO", "123456"), ("VKN", "0987654321")),
    vkn="0987654321",
    historical_value="0098765432100038",
    company_id_registration="0987654321",
)


def _ubl(scenario: Scenario, *, supplier_name: str = "Synthetic Supplier A.S.") -> bytes:
    ids = "".join(
        f'<cac:PartyIdentification><cbc:ID schemeID="{scheme}">{value}</cbc:ID></cac:PartyIdentification>'
        for scheme, value in scenario.supplier_ids
    )
    registration = (
        f"<cbc:CompanyID>{scenario.company_id_registration}</cbc:CompanyID>" if scenario.company_id_registration else ""
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
         xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
         xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
  <cbc:ID>{scenario.invoice_number}</cbc:ID>
  <cbc:UUID>{scenario.invoice_uuid}</cbc:UUID>
  <cbc:IssueDate>2026-09-29</cbc:IssueDate>
  <cbc:DocumentCurrencyCode>TRY</cbc:DocumentCurrencyCode>
  <cac:AdditionalDocumentReference>
    <cbc:ID>{scenario.invoice_uuid}</cbc:ID>
    <cac:Attachment><cbc:EmbeddedDocumentBinaryObject mimeCode="application/xml" encodingCode="Base64"
      filename="{scenario.invoice_number}.xslt">PHhzbDpzdHlsZXNoZWV0Lz4=</cbc:EmbeddedDocumentBinaryObject></cac:Attachment>
  </cac:AdditionalDocumentReference>
  <cac:AccountingSupplierParty><cac:Party>{ids}
    <cac:PartyName><cbc:Name>{supplier_name}</cbc:Name></cac:PartyName>
    <cac:PartyTaxScheme>{registration}
      <cac:TaxScheme><cbc:Name>Synthetic VD</cbc:Name></cac:TaxScheme>
    </cac:PartyTaxScheme>
  </cac:Party></cac:AccountingSupplierParty>
  <cac:AccountingCustomerParty><cac:Party>
    <cac:PartyIdentification><cbc:ID schemeID="VKN">{BUYER_VKN}</cbc:ID></cac:PartyIdentification>
    <cac:PartyName><cbc:Name>Synthetic Buyer A.S.</cbc:Name></cac:PartyName>
  </cac:Party></cac:AccountingCustomerParty>
  <cac:LegalMonetaryTotal>
    <cbc:LineExtensionAmount currencyID="TRY">100.00</cbc:LineExtensionAmount>
    <cbc:TaxExclusiveAmount currencyID="TRY">100.00</cbc:TaxExclusiveAmount>
    <cbc:TaxInclusiveAmount currencyID="TRY">120.00</cbc:TaxInclusiveAmount>
    <cbc:PayableAmount currencyID="TRY">120.00</cbc:PayableAmount>
  </cac:LegalMonetaryTotal>
  <cac:InvoiceLine>
    <cbc:ID>1</cbc:ID>
    <cbc:InvoicedQuantity unitCode="C62">1</cbc:InvoicedQuantity>
    <cbc:LineExtensionAmount currencyID="TRY">100.00</cbc:LineExtensionAmount>
    <cac:TaxTotal><cac:TaxSubtotal>
      <cbc:TaxableAmount currencyID="TRY">100.00</cbc:TaxableAmount>
      <cbc:TaxAmount currencyID="TRY">20.00</cbc:TaxAmount>
      <cbc:Percent>20</cbc:Percent>
      <cac:TaxCategory><cac:TaxScheme><cbc:Name>KDV</cbc:Name><cbc:TaxTypeCode>0015</cbc:TaxTypeCode></cac:TaxScheme></cac:TaxCategory>
    </cac:TaxSubtotal></cac:TaxTotal>
    <cac:Item><cbc:Name>Synthetic service</cbc:Name></cac:Item>
    <cac:Price><cbc:PriceAmount currencyID="TRY">100.00</cbc:PriceAmount></cac:Price>
  </cac:InvoiceLine>
</Invoice>
""".encode()


def _historical_invoice(content: bytes) -> InternalInvoice:
    """What the pre-PR #201 parser persisted: the current parse with the legacy supplier tax number."""

    parsed = parse_ubl_invoice(content)
    return replace(parsed, supplier=replace(parsed.supplier, tax_number=legacy_supplier_tax_identifier(content)))


# --------------------------------------------------------------------------- deterministic matching (Odoo faked)


class _Partners:
    """Fake read-only Odoo partner repository: exact-VAT lookups only."""

    def __init__(self) -> None:
        self.by_vat: dict[str, list[Partner]] = {}
        self.lookups: list[str] = []

    def add(self, *, partner_id: int, vat: str) -> None:
        self.by_vat.setdefault(vat, []).append(Partner(id=partner_id, name="Synthetic", tax_number=vat, active=True))

    def find_by_tax_number(self, tax_number: str, *, company_id: int | None = None) -> tuple[Partner, ...]:
        self.lookups.append(tax_number)
        return tuple(self.by_vat.get(tax_number, ()))


class _Facts:
    def __init__(self, factory) -> None:
        self._factory = factory

    def match_invoice(self, invoice: InternalInvoice, *, company_id: int, partner_match: object = None) -> object:
        return self._factory(invoice)

    def map_invoice(self, invoice: InternalInvoice, *, company_id: int) -> object:
        return self._factory(invoice)


class _NoRules:
    def list_invoice_decision_rules(self, *, company_id: int) -> tuple:
        return ()


def _identifier_free_products(invoice: InternalInvoice) -> InvoiceProductMatchResult:
    return InvoiceProductMatchResult(
        line_results=tuple(
            InvoiceProductLineResult(
                line_number=line.line_number,
                result=ProductMatchResult(
                    status=ProductMatchStatus.INVALID_INPUT,
                    line_number=line.line_number,
                    product_id=None,
                    default_code=None,
                    barcode=None,
                    seller_item_code=line.seller_item_code,
                    matched_by=None,
                    reason="No deterministic product identifier present on this line.",
                    candidate_count=0,
                    confidence=None,
                ),
            )
            for line in invoice.lines
        )
    )


def _matched_taxes(invoice: InternalInvoice) -> InvoiceTaxMappingResult:
    return InvoiceTaxMappingResult(
        line_results=tuple(
            InvoiceTaxLineResult(
                line_number=line.line_number,
                tax_index=index,
                result=TaxMatchResult(
                    status=TaxMatchStatus.MATCHED,
                    tax_id=TAX_ID,
                    company_id=COMPANY_ID,
                    tax_type=TaxType.VAT,
                    tax_rate=Decimal("20"),
                    matched_by="company_type_rate",
                    confidence=Decimal("1.00"),
                    reason="matched",
                    candidate_count=1,
                ),
            )
            for line in invoice.lines
            for index, _tax in enumerate(line.taxes)
        )
    )


def _decision_engine(session: Session, partners: _Partners) -> DecisionEngine:
    return DecisionEngine(
        rule_engine=DeterministicRuleEngine(
            partner_matcher=PartnerMatchingEngine(SimpleNamespace(partner_repository=partners)),
            product_matcher=_Facts(_identifier_free_products),
            tax_mapper=_Facts(_matched_taxes),
            operating_expense_matcher=OperatingExpenseMatchingEngine(
                SqlAlchemyOperatingExpenseMappingRepository(session)
            ),
        ),
        strategy_resolver=WorkflowStrategyResolver([VendorBillReviewRecommendationStrategy(), ManualReviewStrategy()]),
        decision_rule_repository=_NoRules(),
        invoice_decision_rule_engine=InvoiceDecisionRuleEngine(),
    )


def _resolver(session: Session, partners: _Partners, reader) -> EffectiveDecisionResolver:
    return EffectiveDecisionResolver(
        decision_engine=_decision_engine(session, partners),
        source_invoice_reader=reader,
        supplier_remediation_effect_reader=SqlAlchemyReviewSupplierRemediationEffectRepository(session),
        operating_expense_matcher=OperatingExpenseMatchingEngine(SqlAlchemyOperatingExpenseMappingRepository(session)),
        review_accounting_resolution_reader=SqlAlchemyReviewAccountingResolutionRepository(session),
    )


# --------------------------------------------------------------------------- environment


class _Synchronizer:
    """Records post-commit projection calls and what a *separate* connection sees."""

    def __init__(self, engine, *, fail: bool = False) -> None:
        self._engine = engine
        self._fail = fail
        self.calls: list[dict[str, Any]] = []

    def sync(self, *, review_id: str, company_id: int) -> ProjectionSyncResult:
        with self._engine.connect() as connection:
            committed = connection.execute(
                text("SELECT version, supplier_tax_number FROM workbench_review_items WHERE review_id = :r"),
                {"r": review_id},
            ).one()
        self.calls.append({"review_id": review_id, "version": committed[0], "supplier_tax_number": committed[1]})
        if self._fail:
            raise RuntimeError("odoo unreachable")
        return ProjectionSyncResult(
            review_id=review_id,
            outcome=ProjectionSyncOutcome.UPDATED,
            applied=True,
            odoo_record_id=18,
            review_version=committed[0],
        )

    def plan(self, *, review_id: str, company_id: int) -> ProjectionSyncResult:
        raise AssertionError("the use case never plans")


@dataclass
class Env:
    engine: Any
    session: Session
    storage: LocalDocumentStorage
    partners: _Partners
    storage_root: Path

    def use_case(self, *, synchronizer=None, **overrides) -> CorrectReviewSourceIdentityUseCase:
        repository = SqlAlchemyReviewSourceInvoiceCorrectionRepository(self.session)
        wiring: dict[str, Any] = {
            "review_reader": SqlAlchemyReviewRepository(self.session),
            "source_reader": SqlAlchemyReviewSourceInvoiceEvidenceReader(self.session),
            "state_reader": repository,
            "document_reader": self.storage,
            "resolver_factory": lambda reader: _resolver(self.session, self.partners, reader),
            "writer": repository,
            "unit_of_work": SqlAlchemyUnitOfWork(self.session),
            "projection_synchronizer": synchronizer,
        }
        wiring.update(overrides)
        return CorrectReviewSourceIdentityUseCase(**wiring)

    async def correct(self, review_id: str, *, apply: bool, expected_version: int = 1, **kwargs):
        synchronizer = kwargs.pop("synchronizer", None)
        return await self.use_case(synchronizer=synchronizer, **kwargs).execute(
            CorrectReviewSourceIdentityCommand(
                review_id=review_id,
                company_id=COMPANY_ID,
                expected_version=expected_version,
                approved_by=ACTOR,
                apply=apply,
            )
        )


@pytest.fixture()
def env(tmp_path: Path) -> Env:
    # check_same_thread=False: the CLI tests drive the use case from a worker thread.
    engine = create_engine(f"sqlite:///{tmp_path / 'hub.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    root = tmp_path / "documents"
    root.mkdir()
    with sessionmaker(bind=engine)() as session:
        yield Env(engine, session, LocalDocumentStorage(root), _Partners(), root)
    engine.dispose()


async def _seed_historical_review(env: Env, scenario: Scenario, *, document: bytes | None = None) -> str:
    """Reproduce the historical state through the real import path, plus the stored UBL."""

    content = document or _ubl(scenario)
    idempotency_key = f"uyumsoft:company:{COMPANY_ID}:inbox:ettn:{scenario.invoice_number}"
    use_case = ImportInvoiceUseCase(
        import_history=SimpleNamespace(
            find_imported_invoice=lambda key: None,
            record_import_result=lambda **kwargs: None,
        ),
        decision_engine=_decision_engine(env.session, env.partners),
        review_item_creation_service=ReviewItemCreationService(SqlAlchemyReviewRepository(env.session)),
        unit_of_work=SqlAlchemyUnitOfWork(env.session),
    )
    result = await use_case.execute(
        ImportInvoiceCommand(
            invoice=_historical_invoice(content), idempotency_key=idempotency_key, company_id=COMPANY_ID
        )
    )
    now = datetime(2026, 10, 1, 8, 31, tzinfo=UTC)
    metadata = UyumsoftInvoiceMetadata(
        provider="uyumsoft",
        direction="Inbox",
        provider_invoice_id=scenario.invoice_number,
        ettn=scenario.invoice_number,
        identity_key=f"ettn:{scenario.invoice_number}",
        identity_strategy="ettn",
        invoice_number=scenario.invoice_uuid,
        sender_tax_number=scenario.vkn,
        raw_metadata={},
        first_seen_at=now,
        last_seen_at=now,
    )
    env.session.add(metadata)
    env.session.flush()
    digest = hashlib.sha256(content).hexdigest()
    storage_key = f"uyumsoft/inbox/{metadata.id}/ubl_xml/{digest}.xml"
    env.storage.write(storage_key, content)
    env.session.add(
        InvoiceDocument(
            invoice_id=metadata.id,
            provider="uyumsoft",
            direction="Inbox",
            document_type="UBL_XML",
            storage_backend="local_filesystem",
            storage_key=storage_key,
            content_hash_sha256=digest,
            mime_type="application/xml",
            content_size_bytes=len(content),
            downloaded_at=now,
        )
    )
    env.session.commit()
    assert result.review_id is not None
    return result.review_id


def _review(env: Env, review_id: str) -> WorkbenchReviewItem:
    env.session.expire_all()
    return env.session.scalar(select(WorkbenchReviewItem).where(WorkbenchReviewItem.review_id == review_id))


def _snapshot(engine) -> dict[str, list[tuple]]:
    """Every row of every table, from a separate connection."""

    with engine.connect() as connection:
        return {
            table.name: sorted(
                (tuple(str(value) for value in row) for row in connection.execute(table.select())),
            )
            for table in Base.metadata.sorted_tables
        }


def _original_evidence_bytes(env: Env, review_id: str) -> str:
    env.session.expire_all()
    record = env.session.scalar(
        select(WorkbenchReviewSourceInvoiceEvidence).where(WorkbenchReviewSourceInvoiceEvidence.review_id == review_id)
    )
    return json.dumps(
        {
            "id": record.id,
            "review_version": record.review_version,
            "source_invoice_id": record.source_invoice_id,
            "schema_version": record.schema_version,
            "invoice": record.invoice,
            "created_at": str(record.created_at),
        },
        sort_keys=True,
    )


def _count(env: Env, model) -> int:
    env.session.expire_all()
    return env.session.scalar(select(func.count()).select_from(model))


def _check(report, name: str) -> CorrectionCheckStatus:
    return next(check.status for check in report.checks if check.name == name)


# --------------------------------------------------------------------------- domain helpers


@pytest.mark.parametrize("scenario", [AY_STYLE, I10_STYLE], ids=["ay-style", "i10-style"])
def test_legacy_rule_reproduces_the_historical_value_and_current_rule_the_vkn(scenario: Scenario) -> None:
    content = _ubl(scenario)

    assert legacy_supplier_tax_identifier(content) == scenario.historical_value
    assert parse_ubl_invoice(content).supplier.tax_number == scenario.vkn


def test_synthetic_documents_carry_an_embedded_attachment() -> None:
    """Real e-Fatura UBLs embed an XSLT; the evidence comparison must handle it."""

    attachments = parse_ubl_invoice(_ubl(AY_STYLE)).attachments
    assert len(attachments) == 1
    assert attachments[0].sha256 is not None and attachments[0].size


def test_tax_identifier_shape_validation() -> None:
    assert is_party_tax_identifier("1234567890")
    assert is_party_tax_identifier("12345678901")
    for value in (None, "", "123456789", "0123456789000017", "12345678AB", " 1234567890"):
        assert not is_party_tax_identifier(value)


def _correction(**overrides) -> ReviewSourceInvoiceCorrection:
    values = {
        "review_id": "review:x",
        "company_id": 1,
        "from_version": 1,
        "to_version": 2,
        "source_invoice_id": "inv",
        "field_path": SourceInvoiceCorrectionField.SUPPLIER_TAX_NUMBER,
        "old_value": AY_STYLE.historical_value,
        "new_value": AY_STYLE.vkn,
        "source_document_id": 7,
        "source_document_sha256": "a" * 64,
        "reason": SourceInvoiceCorrectionReason.UBL_PARTY_TAX_IDENTIFIER_PR201,
        "approved_by": ACTOR,
    }
    values.update(overrides)
    return ReviewSourceInvoiceCorrection(**values)


@pytest.mark.parametrize(
    "overrides",
    [
        {"to_version": 3},
        {"new_value": "12345"},
        {"new_value": AY_STYLE.historical_value},
        {"old_value": AY_STYLE.vkn},
        {"source_document_sha256": "XYZ"},
        {"approved_by": " "},
        {"field_path": "supplier.name"},
    ],
)
def test_correction_contract_rejects_invalid_records(overrides: dict) -> None:
    with pytest.raises(WorkbenchContractError):
        _correction(**overrides)


def test_overlay_applies_in_version_order_and_rejects_a_broken_chain() -> None:
    invoice = _historical_invoice(_ubl(AY_STYLE))
    corrected = apply_source_invoice_corrections(invoice, [_correction()])

    assert corrected.supplier.tax_number == AY_STYLE.vkn
    assert invoice.supplier.tax_number == AY_STYLE.historical_value  # original object untouched
    assert diff_invoice_payloads({"a": {"b": 1, "c": [1, 2]}}, {"a": {"b": 2, "c": [1, 3]}}) == ("a.b", "a.c[1]")
    with pytest.raises(ReviewDataIntegrityError):
        apply_source_invoice_corrections(invoice, [_correction(old_value="0000000000000000")])


def test_generic_reclassification_cannot_use_the_reserved_trigger() -> None:
    with pytest.raises(WorkbenchContractError):
        ReclassifyReviewCommand(
            review_id="review:x",
            company_id=1,
            expected_version=1,
            trigger=ReviewReclassificationTrigger.SOURCE_IDENTITY_CORRECTED,
        )


# --------------------------------------------------------------------------- 1, 2, 4-10: successful apply


@pytest.mark.parametrize("scenario", [AY_STYLE, I10_STYLE], ids=["ay-style", "i10-style"])
async def test_historical_review_is_corrected_append_only(env: Env, scenario: Scenario) -> None:
    review_id = await _seed_historical_review(env, scenario)
    before = _review(env, review_id)
    assert before.supplier_tax_number == scenario.historical_value
    assert before.version == 1
    assert {reason["code"] for reason in before.review_reasons} == NO_PARTNER_REASONS
    original_bytes = _original_evidence_bytes(env, review_id)
    synchronizer = _Synchronizer(env.engine)

    report = await env.correct(review_id, apply=True, synchronizer=synchronizer)

    assert report.outcome is SourceIdentityCorrectionOutcome.APPLIED
    assert report.applied is True
    assert all(check.status is CorrectionCheckStatus.PASSED for check in report.checks)
    assert (report.old_value, report.new_value) == (scenario.historical_value, scenario.vkn)
    assert (report.from_version, report.to_version) == (1, 2)

    # 5. the immutable original evidence row is byte-identical
    assert _original_evidence_bytes(env, review_id) == original_bytes
    reader = SqlAlchemyReviewSourceInvoiceEvidenceReader(env.session)
    assert reader.get_original(review_id=review_id, company_id=COMPANY_ID).invoice.supplier.tax_number == (
        scenario.historical_value
    )
    # 6. the normal (effective) reader returns the corrected VKN
    assert reader.get(review_id=review_id, company_id=COMPANY_ID).invoice.supplier.tax_number == scenario.vkn
    assert reader.get_invoice(review_id=review_id, company_id=COMPANY_ID).supplier.tax_number == scenario.vkn

    # 7. version advanced exactly once; 10. status/workflow/reasons unchanged
    after = _review(env, review_id)
    assert after.version == 2
    assert after.supplier_tax_number == scenario.vkn
    assert after.status == "pending_review"
    assert after.workflow == WorkflowType.MANUAL_REVIEW.value
    assert {reason["code"] for reason in after.review_reasons} == NO_PARTNER_REASONS
    assert report.previous_reason_codes == report.new_reason_codes
    assert report.classification_status == "NO_MATCH"

    corrections = env.session.scalars(select(WorkbenchReviewSourceInvoiceCorrection)).all()
    assert len(corrections) == 1
    correction = corrections[0]
    assert (correction.review_id, correction.from_version, correction.to_version) == (review_id, 1, 2)
    assert (correction.field_path, correction.old_value, correction.new_value) == (
        "supplier.tax_number",
        scenario.historical_value,
        scenario.vkn,
    )
    assert correction.reason == "UBL_PARTY_TAX_IDENTIFIER_PR201"
    assert correction.approved_by == ACTOR
    assert correction.source_document_sha256 == hashlib.sha256(_ubl(scenario)).hexdigest()
    assert correction.source_document_id > 0

    # 8. classification evidence appended for v2 (v1 kept)
    classifications = env.session.scalars(
        select(WorkbenchReviewClassificationEvidence).order_by(WorkbenchReviewClassificationEvidence.review_version)
    ).all()
    assert [(row.review_version, row.status) for row in classifications] == [(1, "NO_MATCH"), (2, "NO_MATCH")]

    # 9. SOURCE_IDENTITY_CORRECTED reclassification appended
    events = env.session.scalars(select(WorkbenchReviewReclassification)).all()
    assert len(events) == 1
    event = events[0]
    assert (event.trigger, event.from_version, event.to_version) == ("source_identity_corrected", 1, 2)
    assert event.previous_workflow == event.new_workflow == WorkflowType.MANUAL_REVIEW.value
    assert event.previous_review_reasons == event.new_review_reasons

    # nothing else was written
    assert _count(env, WorkbenchReviewDecision) == 0
    assert _count(env, WorkbenchReviewWriteAuthorization) == 0
    assert _count(env, WorkflowExecution) == 0
    assert _count(env, WorkbenchReviewSupplierRemediationEffect) == 0

    # the matcher actually ran against the corrected VKN
    assert env.partners.lookups[-1] == scenario.vkn
    assert report.projection_outcome == "updated"


async def test_dry_run_reports_everything_and_writes_nothing(env: Env) -> None:
    review_id = await _seed_historical_review(env, AY_STYLE)
    before = _snapshot(env.engine)
    synchronizer = _Synchronizer(env.engine)

    report = await env.correct(review_id, apply=False, synchronizer=synchronizer)

    assert report.outcome is SourceIdentityCorrectionOutcome.WOULD_APPLY
    assert report.applied is False
    assert all(check.status is CorrectionCheckStatus.PASSED for check in report.checks)
    assert (report.old_value, report.new_value, report.from_version, report.to_version) == (
        AY_STYLE.historical_value,
        AY_STYLE.vkn,
        1,
        2,
    )
    hub = {(change.target, change.field) for change in report.hub_changes}
    assert {
        ("workbench_review_source_invoice_corrections", "INSERT"),
        ("workbench_review_items", "supplier_tax_number"),
        ("workbench_review_items", "version"),
        ("workbench_review_classification_evidence", "INSERT"),
        ("workbench_review_reclassifications", "INSERT"),
    } == hub
    projection = {change.field: (change.before, change.after) for change in report.projection_changes}
    assert projection["supplier_tax_number"] == (AY_STYLE.historical_value, AY_STYLE.vkn)
    assert projection["review_version"] == (1, 2)
    assert "workflow" not in projection and "review_reasons" not in projection
    env.session.rollback()
    assert _snapshot(env.engine) == before
    assert synchronizer.calls == []


# --------------------------------------------------------------------------- 11-13: downstream consumers


class _PartnerReader:
    def __init__(self, *records: ResolutionPartnerRecord) -> None:
        self._by_id = {record.id: record for record in records}

    def find_partner_by_id(self, partner_id: int) -> ResolutionPartnerRecord | None:
        return self._by_id.get(partner_id)


class _RecordingSupplierWriter:
    def __init__(self, partner_id: int) -> None:
        self.partner_id = partner_id
        self.calls: list[CreateSupplierPartnerCommand] = []

    async def create_supplier(self, command: CreateSupplierPartnerCommand) -> SupplierPartnerWriteResult:
        self.calls.append(command)
        return SupplierPartnerWriteResult(
            status=SupplierPartnerWriteStatus.CREATED,
            partner_id=self.partner_id,
            company_id=command.company_id,
            supplier_name=command.supplier_name,
            supplier_tax_number=command.supplier_tax_number,
            idempotency_key=command.idempotency_key,
        )


def _supplier_remediation(env: Env, *, partner: ResolutionPartnerRecord, writer) -> ResolveWorkbenchSupplierUseCase:
    session = env.session
    return ResolveWorkbenchSupplierUseCase(
        review_reader=SqlAlchemyReviewRepository(session),
        source_invoice_reader=SqlAlchemyReviewSourceInvoiceEvidenceReader(session),
        resolution_validator=ValidateSupplierResolutionUseCase(
            source_invoice_reader=SqlAlchemyReviewSourceInvoiceEvidenceReader(session),
            partner_reader=_PartnerReader(partner),
        ),
        resolution_writer=SqlAlchemyReviewSupplierResolutionRepository(session),
        remediation_effect_writer=SqlAlchemyReviewSupplierRemediationEffectRepository(session),
        supplier_partner_writer=writer,
        reclassifier=ReclassifyWorkbenchReviewUseCase(
            decision_engine=_decision_engine(session, env.partners),
            source_invoice_reader=SqlAlchemyReviewSourceInvoiceEvidenceReader(session),
            reclassification_writer=SqlAlchemyReviewRepository(session),
            supplier_remediation_effect_reader=SqlAlchemyReviewSupplierRemediationEffectRepository(session),
        ),
        unit_of_work=SqlAlchemyUnitOfWork(session),
    )


async def test_create_permanent_supplier_uses_the_corrected_vkn(env: Env) -> None:
    review_id = await _seed_historical_review(env, AY_STYLE)
    await env.correct(review_id, apply=True)
    writer = _RecordingSupplierWriter(partner_id=6001)
    partner = ResolutionPartnerRecord(id=6001, name="Synthetic", vat=AY_STYLE.vkn, active=True, company_id=None)

    await _supplier_remediation(env, partner=partner, writer=writer).execute(
        ResolveWorkbenchSupplierCommand(
            review_id=review_id,
            company_id=COMPANY_ID,
            expected_version=2,
            mode=SupplierResolutionMode.CREATE_PERMANENT_SUPPLIER,
            approved_by=ACTOR,
        )
    )

    assert [call.supplier_tax_number for call in writer.calls] == [AY_STYLE.vkn]
    effect = env.session.scalar(select(WorkbenchReviewSupplierRemediationEffect))
    assert effect.source_supplier_tax_number == AY_STYLE.vkn


async def test_uncorrected_review_would_have_sent_the_historical_identifier(env: Env) -> None:
    """Control: without the correction, CREATE_PERMANENT_SUPPLIER writes the MERSIS number."""

    review_id = await _seed_historical_review(env, AY_STYLE)
    writer = _RecordingSupplierWriter(partner_id=6001)
    partner = ResolutionPartnerRecord(
        id=6001, name="Synthetic", vat=AY_STYLE.historical_value, active=True, company_id=None
    )

    await _supplier_remediation(env, partner=partner, writer=writer).execute(
        ResolveWorkbenchSupplierCommand(
            review_id=review_id,
            company_id=COMPANY_ID,
            expected_version=1,
            mode=SupplierResolutionMode.CREATE_PERMANENT_SUPPLIER,
            approved_by=ACTOR,
        )
    )

    assert [call.supplier_tax_number for call in writer.calls] == [AY_STYLE.historical_value]


async def test_match_existing_validation_uses_the_corrected_vkn(env: Env) -> None:
    review_id = await _seed_historical_review(env, AY_STYLE)
    vkn_partner = ResolutionPartnerRecord(id=501, name="Synthetic", vat=AY_STYLE.vkn, active=True, company_id=None)
    mersis_partner = ResolutionPartnerRecord(
        id=502, name="Synthetic", vat=AY_STYLE.historical_value, active=True, company_id=None
    )

    reader = SqlAlchemyReviewSourceInvoiceEvidenceReader(env.session)
    source_invoice_id = reader.get_original(review_id=review_id, company_id=COMPANY_ID).source_invoice_id

    def validate(partner: ResolutionPartnerRecord, version: int):
        return ValidateSupplierResolutionUseCase(
            source_invoice_reader=SqlAlchemyReviewSourceInvoiceEvidenceReader(env.session),
            partner_reader=_PartnerReader(partner),
        ).execute(
            SupplierResolution(
                mode=SupplierResolutionMode.MATCH_EXISTING,
                review_id=review_id,
                company_id=COMPANY_ID,
                review_version=version,
                source_invoice_id=source_invoice_id,
                resolved_partner_id=partner.id,
            )
        )

    with pytest.raises(SupplierResolutionPartnerMismatchError):
        validate(vkn_partner, 1)  # before: the historical identifier blocks the real supplier

    await env.correct(review_id, apply=True)

    assert validate(vkn_partner, 2).source_supplier_tax_number == AY_STYLE.vkn
    with pytest.raises(SupplierResolutionPartnerMismatchError):
        validate(mersis_partner, 2)


async def test_correction_recalculates_classification_through_the_normal_engine(env: Env) -> None:
    """When the real supplier exists under its VKN, the corrected review now matches it."""

    review_id = await _seed_historical_review(env, AY_STYLE)
    env.partners.add(partner_id=777, vat=AY_STYLE.vkn)

    report = await env.correct(review_id, apply=True)

    assert report.outcome is SourceIdentityCorrectionOutcome.APPLIED
    assert ManualReviewReasonCode.SUPPLIER_NOT_FOUND.value in report.previous_reason_codes
    assert ManualReviewReasonCode.SUPPLIER_NOT_FOUND.value not in report.new_reason_codes
    after = _review(env, review_id)
    assert after.version == 2
    assert ManualReviewReasonCode.SUPPLIER_NOT_FOUND.value not in {reason["code"] for reason in after.review_reasons}
    projection = {change.field for change in report.projection_changes}
    assert "review_reasons" in projection


async def test_later_generic_reclassification_reads_the_corrected_source(env: Env) -> None:
    review_id = await _seed_historical_review(env, AY_STYLE)
    await env.correct(review_id, apply=True)
    env.partners.add(partner_id=777, vat=AY_STYLE.vkn)  # supplier created later

    outcome = await ReclassifyWorkbenchReviewUseCase(
        decision_engine=_decision_engine(env.session, env.partners),
        source_invoice_reader=SqlAlchemyReviewSourceInvoiceEvidenceReader(env.session),
        reclassification_writer=SqlAlchemyReviewRepository(env.session),
    ).execute(
        ReclassifyReviewCommand(
            review_id=review_id,
            company_id=COMPANY_ID,
            expected_version=2,
            trigger=ReviewReclassificationTrigger.MASTER_DATA_CHANGED,
        )
    )
    env.session.commit()

    assert outcome.changed is True
    assert ManualReviewReasonCode.SUPPLIER_NOT_FOUND not in {reason.code for reason in outcome.new_review_reasons}


# --------------------------------------------------------------------------- 14: idempotency


async def test_second_apply_is_already_applied_and_writes_nothing(env: Env) -> None:
    review_id = await _seed_historical_review(env, AY_STYLE)
    await env.correct(review_id, apply=True)
    before = _snapshot(env.engine)
    synchronizer = _Synchronizer(env.engine)

    again = await env.correct(review_id, apply=True, synchronizer=synchronizer)

    assert again.outcome is SourceIdentityCorrectionOutcome.ALREADY_APPLIED
    assert again.applied is False
    assert (again.old_value, again.new_value, again.from_version, again.to_version) == (
        AY_STYLE.historical_value,
        AY_STYLE.vkn,
        1,
        2,
    )
    assert _snapshot(env.engine) == before
    assert synchronizer.calls == []


async def test_correction_at_the_new_version_reports_no_change(env: Env) -> None:
    review_id = await _seed_historical_review(env, AY_STYLE)
    await env.correct(review_id, apply=True)
    before = _snapshot(env.engine)

    again = await env.correct(review_id, apply=True, expected_version=2)

    assert again.outcome is SourceIdentityCorrectionOutcome.NO_CHANGE
    assert _snapshot(env.engine) == before


# --------------------------------------------------------------------------- 15-21: refusals


async def _assert_refused(env: Env, review_id: str, check: str, **kwargs) -> None:
    before = _snapshot(env.engine)
    report = await env.correct(review_id, apply=True, **kwargs)
    assert report.outcome is SourceIdentityCorrectionOutcome.REFUSED, report.checks
    assert _check(report, check) is CorrectionCheckStatus.FAILED
    assert report.applied is False
    env.session.rollback()
    assert _snapshot(env.engine) == before


async def test_stale_review_version_refuses(env: Env) -> None:
    review_id = await _seed_historical_review(env, AY_STYLE)
    await _assert_refused(env, review_id, "version_matches", expected_version=2)


async def test_unknown_review_refuses(env: Env) -> None:
    await _assert_refused(env, "review:missing", "review_exists")


async def test_decided_review_refuses(env: Env) -> None:
    review_id = await _seed_historical_review(env, AY_STYLE)
    env.session.add(
        WorkbenchReviewDecision(
            decision_id="decision-1",
            review_id=review_id,
            company_id=COMPANY_ID,
            review_version_before=1,
            review_version_after=2,
            decision_type="accept",
            decided_by=ACTOR,
            idempotency_key="decision-key",
        )
    )
    env.session.commit()
    await _assert_refused(env, review_id, "no_decision")


async def test_authorized_review_refuses(env: Env) -> None:
    review_id = await _seed_historical_review(env, AY_STYLE)
    env.session.add(
        WorkbenchReviewWriteAuthorization(
            authorization_id="00000000-0000-4000-8000-000000000001",
            company_id=COMPANY_ID,
            review_id=review_id,
            operation_type="CREATE_PERMANENT_SUPPLIER",
            target_version=1,
            status="pending",
            authorized_by=ACTOR,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    env.session.commit()
    await _assert_refused(env, review_id, "no_write_authorization")


async def test_executed_review_refuses(env: Env) -> None:
    review_id = await _seed_historical_review(env, AY_STYLE)
    env.session.add(
        WorkflowExecution(
            execution_id="execution-1",
            review_id=review_id,
            decision_version=2,
            company_id=COMPANY_ID,
            state="completed",
            mode="vendor_bill",
            idempotency_key="execution-key",
            plan_signature="signature",
            plan={},
            checkpoint={},
            retry_policy={},
        )
    )
    env.session.commit()
    await _assert_refused(env, review_id, "no_execution")


async def test_supplier_remediation_state_refuses(env: Env) -> None:
    review_id = await _seed_historical_review(env, AY_STYLE)
    env.session.add(
        WorkbenchReviewSupplierRemediationEffect(
            review_id=review_id,
            company_id=COMPANY_ID,
            review_version=1,
            source_invoice_id=AY_STYLE.invoice_uuid,
            mode="match_existing",
            resolved_partner_id=9,
            partner_write_status="selected",
        )
    )
    env.session.commit()
    await _assert_refused(env, review_id, "no_downstream_remediation")


async def test_ubl_hash_mismatch_refuses(env: Env) -> None:
    review_id = await _seed_historical_review(env, AY_STYLE)
    document = env.session.scalar(select(InvoiceDocument))
    (env.storage_root / document.storage_key).write_bytes(_ubl(AY_STYLE) + b"<!-- tampered -->")
    await _assert_refused(env, review_id, "source_hash_matches")


async def test_missing_stored_document_refuses(env: Env) -> None:
    review_id = await _seed_historical_review(env, AY_STYLE)
    document = env.session.scalar(select(InvoiceDocument))
    (env.storage_root / document.storage_key).unlink()
    await _assert_refused(env, review_id, "source_document_present")


async def test_reparsed_source_differing_in_another_field_refuses(env: Env) -> None:
    """The stored document says something else besides the tax number: not a pure identity fix."""

    review_id = await _seed_historical_review(env, AY_STYLE)
    renamed = _ubl(AY_STYLE, supplier_name="Another Legal Name A.S.")
    document = env.session.scalar(select(InvoiceDocument))
    (env.storage_root / document.storage_key).write_bytes(renamed)
    document.content_hash_sha256 = hashlib.sha256(renamed).hexdigest()
    env.session.commit()
    await _assert_refused(env, review_id, "only_target_field_differs")


async def test_malformed_new_tax_identifier_refuses(env: Env) -> None:
    review_id = await _seed_historical_review(env, AY_STYLE)

    def parse_with_malformed_vkn(content: bytes) -> InternalInvoice:
        parsed = parse_ubl_invoice(content)
        return replace(parsed, supplier=replace(parsed.supplier, tax_number="12345X"))

    await _assert_refused(env, review_id, "new_value_valid", parse_document=parse_with_malformed_vkn)


async def test_value_not_produced_by_the_defect_refuses(env: Env) -> None:
    """The persisted value must be exactly what the pre-PR #201 rule produced."""

    review_id = await _seed_historical_review(env, AY_STYLE)
    await _assert_refused(
        env,
        review_id,
        "historical_identifier_matches_reason",
        legacy_supplier_identifier=lambda content: "9999999999999999",
    )


async def test_unparseable_stored_document_refuses(env: Env) -> None:
    review_id = await _seed_historical_review(env, AY_STYLE)

    def reject(content: bytes) -> InternalInvoice:
        raise ValueError("unsupported document")

    await _assert_refused(env, review_id, "reparse_succeeds", parse_document=reject)


async def test_missing_source_evidence_refuses(env: Env) -> None:
    review_id = await _seed_historical_review(env, AY_STYLE)

    class _NoEvidence(SqlAlchemyReviewSourceInvoiceEvidenceReader):
        def get(self, *, review_id: str, company_id: int):
            raise ReviewNotFoundError("Review source invoice evidence was not found.")

    await _assert_refused(env, review_id, "source_evidence_present", source_reader=_NoEvidence(env.session))


async def test_nothing_to_correct_with_a_failed_check_is_refused_not_no_change(env: Env) -> None:
    review_id = await _seed_historical_review(env, AY_STYLE)
    await env.correct(review_id, apply=True)

    report = await env.correct(review_id, apply=True, expected_version=3)

    assert report.outcome is SourceIdentityCorrectionOutcome.REFUSED
    assert _check(report, "version_matches") is CorrectionCheckStatus.FAILED


async def test_review_not_pending_refuses(env: Env) -> None:
    review_id = await _seed_historical_review(env, AY_STYLE)
    item = _review(env, review_id)
    item.status = "decision_submitted"
    env.session.commit()
    await _assert_refused(env, review_id, "review_pending")


async def test_concurrent_version_change_conflicts_without_partial_writes(env: Env) -> None:
    """A review advanced between precondition checks and the write never gets a half correction."""

    review_id = await _seed_historical_review(env, AY_STYLE)

    class _RacingWriter(SqlAlchemyReviewSourceInvoiceCorrectionRepository):
        def apply_source_invoice_correction(self, correction, proposal) -> None:
            with env.engine.begin() as connection:
                connection.execute(
                    text("UPDATE workbench_review_items SET version = 5 WHERE review_id = :r"), {"r": review_id}
                )
            super().apply_source_invoice_correction(correction, proposal)

    from app.application.workbench.exceptions import ReviewVersionConflictError

    with pytest.raises(ReviewVersionConflictError):
        await env.correct(review_id, apply=True, writer=_RacingWriter(env.session))
    assert _count(env, WorkbenchReviewSourceInvoiceCorrection) == 0
    assert _count(env, WorkbenchReviewReclassification) == 0


# --------------------------------------------------------------------------- 22-23: projection


async def test_projection_runs_only_after_the_hub_commit(env: Env) -> None:
    review_id = await _seed_historical_review(env, AY_STYLE)
    synchronizer = _Synchronizer(env.engine)

    report = await env.correct(review_id, apply=True, synchronizer=synchronizer)

    # The synchronizer reads through its own connection: it can only see committed state.
    assert synchronizer.calls == [{"review_id": review_id, "version": 2, "supplier_tax_number": AY_STYLE.vkn}]
    assert report.projection_outcome == "updated"
    assert report.projection_odoo_record_id == 18


async def test_projection_failure_never_undoes_the_hub_correction(env: Env) -> None:
    review_id = await _seed_historical_review(env, AY_STYLE)
    synchronizer = _Synchronizer(env.engine, fail=True)

    report = await env.correct(review_id, apply=True, synchronizer=synchronizer)

    assert report.outcome is SourceIdentityCorrectionOutcome.APPLIED
    assert report.projection_outcome == "ERROR"
    assert report.projection_error == "RuntimeError"
    with env.engine.connect() as connection:
        version, vat = connection.execute(
            text("SELECT version, supplier_tax_number FROM workbench_review_items WHERE review_id = :r"),
            {"r": review_id},
        ).one()
    assert (version, vat) == (2, AY_STYLE.vkn)
    assert _count(env, WorkbenchReviewSourceInvoiceCorrection) == 1


async def test_disabled_projection_is_reported(env: Env) -> None:
    review_id = await _seed_historical_review(env, AY_STYLE)
    report = await env.correct(review_id, apply=True)
    assert report.projection_outcome == "DISABLED"


# --------------------------------------------------------------------------- CLI


def test_review_target_requires_an_explicit_expected_version() -> None:
    assert cli.parse_review_target("review:abc@1") == cli.ReviewTarget("review:abc", 1)
    for value in ("review:abc", "review:abc@", "review:abc@0", "@1", "review:abc@x"):
        with pytest.raises(Exception):  # noqa: B017,PT011 - argparse.ArgumentTypeError
            cli.parse_review_target(value)


def test_cli_requires_reviews_and_operator() -> None:
    with pytest.raises(SystemExit):
        cli.main(["--company", "1", "--approved-by", ACTOR])
    with pytest.raises(SystemExit):
        cli.main(["--company", "1", "--review", "review:a@1"])
    with pytest.raises(SystemExit):
        cli.main(["--company", "1", "--approved-by", ACTOR, "--review", "review:a@1", "--review", "review:a@1"])


def _cli_scope(env: Env, *, broken: frozenset[str] = frozenset()):
    """A CLI runner whose per-review scope yields the test-wired use case (or a broken one)."""

    from contextlib import nullcontext

    class _Broken:
        async def execute(self, command: CorrectReviewSourceIdentityCommand):
            raise RuntimeError("storage offline")

    def run(argv: list[str], out: io.StringIO) -> int:
        pending = [argv[index + 1].rpartition("@")[0] for index, arg in enumerate(argv) if arg == "--review"]

        def scope(apply: bool):
            review_id = pending.pop(0)
            return nullcontext(_Broken() if review_id in broken else env.use_case())

        return cli.main(argv, out=out, scope=scope)

    return run


async def test_cli_dry_run_then_apply_then_reapply(env: Env) -> None:
    first = await _seed_historical_review(env, AY_STYLE)
    second = await _seed_historical_review(env, I10_STYLE)
    run = _cli_scope(env)
    argv = ["--company", "1", "--approved-by", ACTOR, "--review", f"{first}@1", "--review", f"{second}@1"]
    before = _snapshot(env.engine)

    dry = io.StringIO()
    assert await _in_thread(run, argv, dry) == 0
    env.session.rollback()
    assert _snapshot(env.engine) == before
    output = dry.getvalue()
    assert "DRY-RUN" in output
    assert output.count("WOULD_APPLY") >= 2
    assert f"'{AY_STYLE.historical_value}' -> '{AY_STYLE.vkn}'" in output
    assert f"'{I10_STYLE.historical_value}' -> '{I10_STYLE.vkn}'" in output
    assert "version: v1 -> v2" in output
    assert "[PASS] source_hash_matches" in output
    assert "INSERT workbench_review_source_invoice_corrections" in output
    assert "Odoo Workbench projection changes (after commit):" in output
    assert "Summary: WOULD_APPLY=2" in output

    applied = io.StringIO()
    assert await _in_thread(run, [*argv, "--apply"], applied) == 0
    assert "Summary: WOULD_APPLY=0 APPLIED=2" in applied.getvalue()
    assert _count(env, WorkbenchReviewSourceInvoiceCorrection) == 2

    again = io.StringIO()
    assert await _in_thread(run, [*argv, "--apply"], again) == 0
    assert "ALREADY_APPLIED=2" in again.getvalue()
    assert _count(env, WorkbenchReviewSourceInvoiceCorrection) == 2


async def test_cli_fails_each_review_independently(env: Env) -> None:
    good = await _seed_historical_review(env, AY_STYLE)
    bad = await _seed_historical_review(env, I10_STYLE)
    run = _cli_scope(env, broken=frozenset({bad}))
    out = io.StringIO()

    code = await _in_thread(
        run,
        ["--company", "1", "--approved-by", ACTOR, "--review", f"{bad}@1", "--review", f"{good}@1", "--apply"],
        out,
    )

    assert code == cli.EXIT_REVIEW_FAILURES
    assert "ERROR         RuntimeError: storage offline" in out.getvalue()
    assert "APPLIED=1" in out.getvalue() and "ERROR=1" in out.getvalue()
    assert _review(env, good).version == 2
    assert _review(env, bad).version == 1


async def test_cli_refusal_sets_failure_exit_code(env: Env) -> None:
    review_id = await _seed_historical_review(env, AY_STYLE)
    out = io.StringIO()

    code = await _in_thread(
        _cli_scope(env), ["--company", "1", "--approved-by", ACTOR, "--review", f"{review_id}@3", "--apply"], out
    )

    assert code == cli.EXIT_REVIEW_FAILURES
    assert "REFUSED" in out.getvalue()
    assert "[FAIL] version_matches" in out.getvalue()


async def _in_thread(run, argv, out) -> int:
    """The CLI drives its own event loop (asyncio.run); keep it off the test's loop."""

    import asyncio

    return await asyncio.to_thread(run, argv, out)
