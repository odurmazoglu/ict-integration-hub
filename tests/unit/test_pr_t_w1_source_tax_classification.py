"""PR T W1: source-tax classification guards (import, reclassification, evidence recovery,
source-identity correction) and the read side that feeds them.

End-to-end cases run on the source-identity-correction harness: a real SQLite Hub, a real
``LocalDocumentStorage`` and real ``invoice_documents`` rows with real SHA-256 hashes, read
through the production ``StoredSourceTaxFactsReader``. Everything is synthetic.
"""

from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, func, select

import tests.unit.test_review_source_identity_correction as _correction
from app.application.commands import ImportInvoiceCommand
from app.application.dto import DecisionResult, ImportInvoiceResult
from app.application.rules.deterministic import (
    DIRECT_VENDOR_BILL_RULE_ID,
    MANUAL_REVIEW_RULE_ID,
    OPERATING_EXPENSE_VENDOR_BILL_RULE_ID,
)
from app.application.source_tax import (
    SourceDocumentIssue,
    SourceTaxAssessment,
    SourceTaxFactsUnavailableError,
    SourceTaxSafetyBlockedError,
    assess_source_document,
)
from app.application.source_tax.reader import SourceTaxSafetyCheck, StoredSourceTaxFactsReader
from app.application.use_cases import ImportInvoiceUseCase
from app.application.use_cases.effective_decision import (
    EffectiveDecisionResolver,
    _effective_manual_review_reasons,
    _effective_workflow,
)
from app.application.use_cases.reclassify_review import ReclassifyWorkbenchReviewUseCase
from app.application.use_cases.review_classification_outcome import build_review_execution_evidence
from app.application.workbench import ReviewItemCreationService
from app.application.workbench.commands import ReviewDecisionCommand
from app.application.workbench.decision_use_cases import SubmitReviewDecisionUseCase
from app.application.workbench.dto import ReviewDecisionType, TaxResolution
from app.application.workbench.evidence import ReviewSourceInvoiceEvidence
from app.application.workbench.exceptions import (
    ExecutionEvidenceRecoveryMismatchError,
    ReviewNotFoundError,
    ReviewPersistenceError,
    WorkbenchContractError,
)
from app.application.workbench.execution_evidence_recovery import RebuildExecutionEvidenceCommand
from app.application.workbench.execution_evidence_recovery_use_cases import RebuildReviewExecutionEvidenceUseCase
from app.application.workbench.reclassification import ReclassifyReviewCommand, ReviewReclassificationTrigger
from app.application.workflow import (
    SOURCE_TAX_REASON_SOURCE,
    ManualReviewReason,
    ManualReviewReasonCode,
    WorkflowType,
)
from app.composition.source_tax import StoredSourceTaxDocumentReader, build_source_tax_facts_reader
from app.core.config import Settings
from app.domain.invoice.tax_facts import extract_source_tax_facts
from app.erp.models import Company
from app.matching import PartnerMatchStatus
from app.models.invoice_document import InvoiceDocument
from app.models.operating_expense_mapping import OperatingExpenseMappingRecord
from app.models.workbench_review_decision import WorkbenchReviewDecision
from app.models.workbench_review_execution_evidence import WorkbenchReviewExecutionEvidence
from app.models.workbench_review_item import WorkbenchReviewItem
from app.models.workbench_review_reclassification import WorkbenchReviewReclassification
from app.persistence import (
    SqlAlchemyReviewExecutionEvidenceReader,
    SqlAlchemyReviewRepository,
    SqlAlchemyReviewSourceInvoiceEvidenceReader,
    SqlAlchemyUnitOfWork,
)
from app.persistence.workbench_review_source_invoice_correction_repository import (
    SqlAlchemyReviewSourceInvoiceCorrectionRepository,
)
from app.persistence.workbench_review_source_invoice_reader import review_source_invoice_evidence_fingerprint
from app.services.document_storage import DocumentStorageError
from app.services.uyumsoft_canonical_import import ExactCompanyResolver, UyumsoftCanonicalInvoiceImporter
from tests.unit.pr_t_w1_fixtures import BUYER_VKN, SYNTHETIC_UUID, LineSpec, TaxSpec, sha256, synthetic_ubl
from tests.unit.source_tax_support import FixedSourceTaxFactsReader
from tests.unit.test_operating_expense_classification import (
    _command,
    _engine,
    _identifier_free_product_match,
    _invoice,
    _line,
    _matched_expense_result,
    _matched_product_match,
    _partner_match,
    _tax_match,
)
from tests.unit.test_review_source_identity_correction import (
    AY_STYLE,
    COMPANY_ID,
    Env,
    _decision_engine,
    _historical_invoice,
    _seed_historical_review,
    _ubl,
)
from tests.unit.test_uyumsoft_canonical_import import (
    FakeCompanyRepository,
    FakeStorage,
    RecordingImportUseCase,
    _download_result,
    _record,
)
from tests.unit.test_uyumsoft_canonical_import import (
    _invoice as _summary,
)

#: The source-identity-correction harness fixture: real SQLite Hub + LocalDocumentStorage.
env = _correction.env

WITHHOLDING = ManualReviewReasonCode.WITHHOLDING_TAX_NOT_SUPPORTED
UNVERIFIED = ManualReviewReasonCode.SOURCE_DOCUMENT_UNVERIFIED
UNSUPPORTED = ManualReviewReasonCode.UNSUPPORTED_INVOICE_CONTENT
RETURN = ManualReviewReasonCode.RETURN_INVOICE_NOT_SUPPORTED
SUPPLIER_NOT_FOUND = ManualReviewReasonCode.SUPPLIER_NOT_FOUND
PARTNER_ID = 777
EXPENSE_ACCOUNT_ID = 9101

LINE_WITHHOLDING = b"""
    <cac:WithholdingTaxTotal>
      <cbc:TaxAmount currencyID="TRY">10.00</cbc:TaxAmount>
      <cac:TaxSubtotal><cbc:TaxableAmount currencyID="TRY">20.00</cbc:TaxableAmount>
        <cbc:TaxAmount currencyID="TRY">10.00</cbc:TaxAmount><cbc:Percent>50</cbc:Percent>
        <cac:TaxCategory><cac:TaxScheme><cbc:Name>KDV TEVKIFAT</cbc:Name>
          <cbc:TaxTypeCode>650</cbc:TaxTypeCode></cac:TaxScheme></cac:TaxCategory>
      </cac:TaxSubtotal>
    </cac:WithholdingTaxTotal>"""


def _verified(content: bytes) -> SourceTaxAssessment:
    return SourceTaxAssessment.verified(extract_source_tax_facts(content))


def _withholding_assessment() -> SourceTaxAssessment:
    return _verified(synthetic_ubl(invoice_type="TEVKIFAT"))


def _clean_assessment() -> SourceTaxAssessment:
    return _verified(synthetic_ubl())


def _with_assessment(command: ImportInvoiceCommand, assessment: SourceTaxAssessment | None, *, dry_run=True):
    return replace(command, source_tax_assessment=assessment, dry_run=dry_run)


def _codes(result) -> tuple[ManualReviewReasonCode, ...]:
    assert result.workflow_decision.manual_review is not None
    return tuple(reason.code for reason in result.workflow_decision.manual_review.reasons)


def _withholding_document(content: bytes) -> bytes:
    """The same document with a line-level WithholdingTaxTotal (hash changes accordingly)."""

    marker = b"</cac:TaxSubtotal></cac:TaxTotal>"
    assert content.count(marker) == 1
    return content.replace(marker, marker + LINE_WITHHOLDING)


# ===================================================================== rule engine


def test_clean_assessment_keeps_the_direct_vendor_bill_rule() -> None:
    invoice = _invoice(lines=(_line(seller_item_code="S-1"),))
    engine = _engine(invoice=invoice, product_match=_matched_product_match(invoice))

    result = engine.evaluate(_with_assessment(_command(invoice), _clean_assessment()))

    assert result.workflow is WorkflowType.VENDOR_BILL
    assert result.matched_rule == DIRECT_VENDOR_BILL_RULE_ID


@pytest.mark.parametrize(
    ("assessment", "expected"),
    [
        (_withholding_assessment(), (WITHHOLDING,)),
        (_verified(synthetic_ubl(invoice_withholding=True)), (WITHHOLDING,)),
        (_verified(synthetic_ubl(lines=(LineSpec(withholding=True),))), (WITHHOLDING,)),
        (_verified(synthetic_ubl(invoice_type="YTBTEVKIFAT")), (WITHHOLDING,)),
        (SourceTaxAssessment.unverified(SourceDocumentIssue.HASH_MISMATCH), (UNVERIFIED,)),
        (SourceTaxAssessment.unverified(SourceDocumentIssue.DOCUMENT_MISSING), (UNVERIFIED,)),
    ],
    ids=["TEVKIFAT", "invoice-level", "line-level", "YTBTEVKIFAT", "hash-mismatch", "missing"],
)
def test_fully_matched_invoice_is_hard_blocked(assessment: SourceTaxAssessment, expected) -> None:
    invoice = _invoice(lines=(_line(seller_item_code="S-1"),))
    engine = _engine(invoice=invoice, product_match=_matched_product_match(invoice))

    result = engine.evaluate(_with_assessment(_command(invoice), assessment))

    assert result.workflow is WorkflowType.MANUAL_REVIEW
    assert result.matched_rule == MANUAL_REVIEW_RULE_ID
    assert _codes(result) == expected
    assert all(reason.source == SOURCE_TAX_REASON_SOURCE for reason in result.workflow_decision.manual_review.reasons)
    # Facts are still recorded, never reinterpreted.
    assert result.partner_match.partner_id is not None


def test_non_vat_tax_code_is_unsupported_content_from_the_source_document() -> None:

    invoice = _invoice(lines=(_line(seller_item_code="S-1"),))
    engine = _engine(invoice=invoice, product_match=_matched_product_match(invoice))
    assessment = _verified(synthetic_ubl(lines=(LineSpec(taxes=(TaxSpec(), TaxSpec(code="4080"))),)))

    result = engine.evaluate(_with_assessment(_command(invoice), assessment))

    assert _codes(result) == (UNSUPPORTED,)
    assert result.workflow_decision.manual_review.reasons[0].source == SOURCE_TAX_REASON_SOURCE


def test_withholding_blocks_the_operating_expense_branch() -> None:
    invoice = _invoice()
    engine = _engine(
        invoice=invoice,
        product_match=_identifier_free_product_match(invoice),
        operating_expense_match=_matched_expense_result(),
    )
    assert engine.evaluate(_command(invoice)).matched_rule == OPERATING_EXPENSE_VENDOR_BILL_RULE_ID

    result = engine.evaluate(_with_assessment(_command(invoice), _withholding_assessment()))

    assert result.workflow is WorkflowType.MANUAL_REVIEW
    assert _codes(result) == (WITHHOLDING,)


def test_withholding_reason_comes_first_and_other_reasons_are_kept() -> None:
    invoice = _invoice(lines=(_line(seller_item_code="S-1"),))
    engine = _engine(
        invoice=invoice,
        partner_match=_partner_match(PartnerMatchStatus.NOT_FOUND),
        product_match=_matched_product_match(invoice),
    )

    result = engine.evaluate(_with_assessment(_command(invoice), _withholding_assessment()))

    assert _codes(result) == (WITHHOLDING, SUPPLIER_NOT_FOUND)


@pytest.mark.parametrize("invoice_type", ["IADE", "TEVKIFATIADE", "YTBTEVKIFATIADE"])
def test_return_invoice_precedence_is_preserved(invoice_type: str) -> None:
    invoice = _invoice(lines=(_line(seller_item_code="S-1"),))
    invoice = replace(invoice, header=replace(invoice.header, invoice_type=invoice_type))
    engine = _engine(invoice=invoice, product_match=_matched_product_match(invoice))
    assessment = _verified(synthetic_ubl(invoice_type=invoice_type, invoice_withholding=True))

    result = engine.evaluate(_with_assessment(_command(invoice), assessment))

    assert _codes(result) == (RETURN,)


def test_real_write_without_assessment_fails_closed() -> None:
    invoice = _invoice(lines=(_line(seller_item_code="S-1"),))
    engine = _engine(invoice=invoice, product_match=_matched_product_match(invoice))

    result = engine.evaluate(_with_assessment(_command(invoice), None, dry_run=False))

    assert _codes(result) == (UNVERIFIED,)
    assert result.workflow_decision.manual_review.reasons[0].details == (
        ("issue", SourceDocumentIssue.FACTS_NOT_SUPPLIED.value),
    )


def test_dry_run_without_assessment_is_unchanged_for_legacy_callers() -> None:
    invoice = _invoice(lines=(_line(seller_item_code="S-1"),))
    engine = _engine(invoice=invoice, product_match=_matched_product_match(invoice))

    assert engine.evaluate(_command(invoice)).matched_rule == DIRECT_VENDOR_BILL_RULE_ID


# ===================================================================== Stage-1 evidence + effective state


def _decision(invoice, reasons: tuple[ManualReviewReason, ...]) -> DecisionResult:

    return DecisionResult(
        success=False,
        invoice_id=invoice.header.ettn,
        workflow=WorkflowType.MANUAL_REVIEW,
        strategy=WorkflowType.MANUAL_REVIEW.value,
        status="review_required",
        review_required=True,
        review_reasons=reasons,
        partner_match=_partner_match(),
        product_match=_matched_product_match(invoice),
        tax_match=_tax_match(invoice),
    )


@pytest.mark.parametrize(
    "reason",
    [
        ManualReviewReason(code=WITHHOLDING, message="w", source=SOURCE_TAX_REASON_SOURCE),
        ManualReviewReason(code=UNVERIFIED, message="u", source=SOURCE_TAX_REASON_SOURCE),
        ManualReviewReason(code=UNSUPPORTED, message="t", source=SOURCE_TAX_REASON_SOURCE),
    ],
    ids=["withholding", "unverified", "unsupported-from-source"],
)
def test_source_tax_reasons_never_get_stage_one_evidence(reason: ManualReviewReason) -> None:
    invoice = _invoice(lines=(_line(seller_item_code="S-1"),))
    kwargs = dict(review_id="review-w1", company_id=COMPANY_ID, review_version=1, invoice=invoice)
    assert build_review_execution_evidence(decision_result=_decision(invoice, ()), **kwargs) is not None

    assert build_review_execution_evidence(decision_result=_decision(invoice, (reason,)), **kwargs) is None


def test_no_operator_resolution_strips_a_source_tax_reason() -> None:
    reasons = (
        ManualReviewReason(code=WITHHOLDING, message="w", source=SOURCE_TAX_REASON_SOURCE),
        ManualReviewReason(code=UNSUPPORTED, message="t", source=SOURCE_TAX_REASON_SOURCE),
        ManualReviewReason(code=ManualReviewReasonCode.OPERATING_EXPENSE_MAPPING_REQUIRED, message="o"),
    )
    review_scoped = replace(_matched_expense_result(), matched_by="review_accounting_resolution")

    effective = _effective_manual_review_reasons(reasons, None, review_scoped, fixed_asset_resolved=True)

    assert tuple(reason.code for reason in effective) == (WITHHOLDING, UNSUPPORTED)
    assert _effective_workflow(WorkflowType.MANUAL_REVIEW, effective) is WorkflowType.MANUAL_REVIEW


# ===================================================================== reader


class _Document(SimpleNamespace):
    pass


class _Locator:
    def __init__(self, documents=(), error: Exception | None = None) -> None:
        self.documents = tuple(documents)
        self.error = error

    def find_source_documents(self, *, review_id: str, company_id: int):
        if self.error is not None:
            raise self.error
        return self.documents


class _Contents:
    def __init__(self, content: bytes | None = None, error: Exception | None = None) -> None:
        self.content = content
        self.error = error
        self.reads: list[str] = []

    def read(self, storage_key: str) -> bytes:
        self.reads.append(storage_key)
        if self.error is not None:
            raise self.error
        assert self.content is not None
        return self.content


def _reader(documents=(), *, content: bytes | None = None, locator_error=None, read_error=None):
    return StoredSourceTaxFactsReader(
        document_locator=_Locator(documents, locator_error),
        document_reader=_Contents(content, read_error),
    )


def test_reader_verifies_the_single_stored_document() -> None:
    content = synthetic_ubl(invoice_type="TEVKIFAT")
    reader = _reader((_Document(storage_key="k", content_sha256=sha256(content)),), content=content)

    assessment = reader.assess(review_id="r", company_id=1, invoice_uuid=SYNTHETIC_UUID)

    assert assessment.is_verified
    assert assessment.facts.has_withholding


def test_reader_missing_document_is_deterministically_unverified() -> None:
    assert _reader(()).assess(review_id="r", company_id=1, invoice_uuid=SYNTHETIC_UUID).issue is (
        SourceDocumentIssue.DOCUMENT_MISSING
    )


def test_reader_ambiguous_documents_are_unverified_without_reading_any() -> None:
    documents = (_Document(storage_key="a", content_sha256="x"), _Document(storage_key="b", content_sha256="y"))
    reader = _reader(documents, content=b"never")

    assert reader.assess(review_id="r", company_id=1, invoice_uuid=SYNTHETIC_UUID).issue is (
        SourceDocumentIssue.DOCUMENT_AMBIGUOUS
    )


@pytest.mark.parametrize(
    ("stored_hash", "uuid", "issue"),
    [
        ("0" * 64, SYNTHETIC_UUID, SourceDocumentIssue.HASH_MISMATCH),
        (None, "7e570000-0000-4000-8000-0000000000ff", SourceDocumentIssue.UUID_MISMATCH),
    ],
)
def test_reader_hash_and_uuid_mismatch_are_unverified(stored_hash, uuid, issue) -> None:
    content = synthetic_ubl()
    reader = _reader((_Document(storage_key="k", content_sha256=stored_hash or sha256(content)),), content=content)

    assert reader.assess(review_id="r", company_id=1, invoice_uuid=uuid).issue is issue


def test_reader_storage_failure_is_retryable_not_a_verdict() -> None:
    reader = _reader(
        (_Document(storage_key="k", content_sha256="x"),),
        read_error=SourceTaxFactsUnavailableError("offline"),
    )

    with pytest.raises(SourceTaxFactsUnavailableError):
        reader.assess(review_id="r", company_id=1, invoice_uuid=SYNTHETIC_UUID)


def test_reader_database_failure_is_retryable() -> None:
    reader = _reader(locator_error=ReviewPersistenceError("db down"))

    with pytest.raises(SourceTaxFactsUnavailableError):
        reader.assess(review_id="r", company_id=1, invoice_uuid=SYNTHETIC_UUID)


def test_reader_unknown_review_is_not_found_not_unverified() -> None:
    with pytest.raises(ReviewNotFoundError):
        _reader(locator_error=ReviewNotFoundError("no review")).assess(review_id="r", company_id=1, invoice_uuid="u")


def test_storage_adapter_never_hides_a_non_storage_bug() -> None:
    class _Broken:
        def read(self, storage_key: str) -> bytes:
            raise TypeError("bug")

    with pytest.raises(TypeError):
        StoredSourceTaxDocumentReader(_Broken()).read("k")  # type: ignore[arg-type]


def test_storage_adapter_reads_document_storage_errors_only() -> None:
    class _Failing:
        def read(self, storage_key: str) -> bytes:
            raise DocumentStorageError("read failed")

    with pytest.raises(SourceTaxFactsUnavailableError):
        StoredSourceTaxDocumentReader(_Failing()).read("k")  # type: ignore[arg-type]


# ===================================================================== canonical import


def _canonical_importer(content: bytes, *, stored_hash: str | None = None):
    use_case = RecordingImportUseCase(
        ImportInvoiceResult(success=False, invoice_id="i", status="review_required", review_required=True)
    )

    class _Downloads:
        def download_documents(self, *, invoice_ids, document_type):
            result = _download_result(invoice_ids[0], "key")
            item = replace(result.items[0], content_hash_sha256=stored_hash or sha256(content))
            return replace(result, items=[item])

    importer = UyumsoftCanonicalInvoiceImporter(
        document_service=_Downloads(),  # type: ignore[arg-type]
        storage=FakeStorage(content),
        company_resolver=ExactCompanyResolver(FakeCompanyRepository((Company(id=7, name="B", tax_number=BUYER_VKN),))),
        import_use_case_factory=lambda: use_case,  # type: ignore[arg-type]
    )
    return importer, use_case


@pytest.mark.parametrize(
    ("content", "expected_withholding"),
    [(synthetic_ubl(), False), (synthetic_ubl(invoice_type="TEVKIFAT"), True)],
    ids=["clean", "withholding"],
)
def test_canonical_import_attaches_the_verified_assessment(content: bytes, expected_withholding: bool) -> None:
    importer, use_case = _canonical_importer(content)

    importer.import_invoice(_summary(), persisted_record=_record())

    (command,) = use_case.commands
    assessment = command.source_tax_assessment
    assert assessment is not None and assessment.is_verified
    assert assessment.facts.has_withholding is expected_withholding
    assert command.invoice.header.invoice_uuid == SYNTHETIC_UUID


def test_canonical_import_hash_mismatch_is_unverified() -> None:
    importer, use_case = _canonical_importer(synthetic_ubl(), stored_hash="0" * 64)

    importer.import_invoice(_summary(), persisted_record=_record())

    assert use_case.commands[0].source_tax_assessment.issue is SourceDocumentIssue.HASH_MISMATCH


# ===================================================================== end-to-end (real storage)


def _real_reader(env: Env) -> StoredSourceTaxFactsReader:
    return build_source_tax_facts_reader(session=env.session, settings=Settings(), storage=env.storage)


def _import_use_case(env: Env) -> ImportInvoiceUseCase:
    return ImportInvoiceUseCase(
        import_history=SimpleNamespace(find_imported_invoice=lambda key: None, record_import_result=lambda **_: None),
        decision_engine=_decision_engine(env.session, env.partners),
        review_item_creation_service=ReviewItemCreationService(SqlAlchemyReviewRepository(env.session)),
        unit_of_work=SqlAlchemyUnitOfWork(env.session),
    )


def _executable_supplier(env: Env) -> None:
    """Supplier matched by VKN + a supplier-wide expense mapping: an executable OPEX invoice."""

    env.partners.add(partner_id=PARTNER_ID, vat=AY_STYLE.historical_value)
    env.partners.add(partner_id=PARTNER_ID, vat=AY_STYLE.vkn)
    env.session.add(
        OperatingExpenseMappingRecord(
            company_id=COMPANY_ID,
            vendor_partner_id=PARTNER_ID,
            expense_account_id=EXPENSE_ACCOUNT_ID,
            expense_category="SUPPLIER_WIDE",
            enabled=True,
        )
    )
    env.session.commit()


def _review(env: Env, review_id: str) -> WorkbenchReviewItem:
    env.session.expire_all()
    return env.session.scalar(select(WorkbenchReviewItem).where(WorkbenchReviewItem.review_id == review_id))


def _stage_one_count(env: Env, review_id: str) -> int:
    return env.session.scalar(
        select(func.count())
        .select_from(WorkbenchReviewExecutionEvidence)
        .where(WorkbenchReviewExecutionEvidence.review_id == review_id)
    )


def _reclassifier(env: Env, reader) -> ReclassifyWorkbenchReviewUseCase:
    return ReclassifyWorkbenchReviewUseCase(
        decision_engine=_decision_engine(env.session, env.partners),
        source_invoice_reader=SqlAlchemyReviewSourceInvoiceEvidenceReader(env.session),
        reclassification_writer=SqlAlchemyReviewRepository(env.session),
        source_tax_facts_reader=reader,
    )


async def _reclassify(env: Env, review_id: str, *, version: int, reader=None):
    outcome = await _reclassifier(env, reader or _real_reader(env)).execute(
        ReclassifyReviewCommand(
            review_id=review_id,
            company_id=COMPANY_ID,
            expected_version=version,
            trigger=ReviewReclassificationTrigger.MASTER_DATA_CHANGED,
        )
    )
    env.session.commit()
    return outcome


async def test_import_with_withholding_document_creates_a_blocked_review_without_stage_one(env: Env) -> None:
    _executable_supplier(env)
    content = _withholding_document(_ubl(AY_STYLE))
    review_id = await _seed_historical_review(env, AY_STYLE, document=content)
    # Seeding imports like the pre-W1 image (no assessment): executable, Stage-1 pinned.
    assert _review(env, review_id).workflow == WorkflowType.VENDOR_BILL.value
    assert _stage_one_count(env, review_id) == 1

    other = replace(AY_STYLE, invoice_number="SYN2026000000099", invoice_uuid="aaaaaaaa-0000-4000-8000-000000000099")
    other_content = _withholding_document(_ubl(other))
    invoice = _historical_invoice(other_content)
    result = await _import_use_case(env).execute(
        ImportInvoiceCommand(
            invoice=invoice,
            idempotency_key=f"uyumsoft:company:{COMPANY_ID}:inbox:ettn:{other.invoice_number}",
            company_id=COMPANY_ID,
            source_tax_assessment=assess_source_document(
                other_content, expected_sha256=sha256(other_content), expected_invoice_uuid=other.invoice_uuid
            ),
        )
    )

    item = _review(env, result.review_id)
    assert item.workflow == WorkflowType.MANUAL_REVIEW.value
    assert [reason["code"] for reason in item.review_reasons] == [WITHHOLDING.value]
    assert _stage_one_count(env, result.review_id) == 0


async def test_import_source_evidence_and_replay_are_unchanged_by_the_assessment(env: Env) -> None:
    other = replace(AY_STYLE, invoice_number="SYN2026000000098", invoice_uuid="aaaaaaaa-0000-4000-8000-000000000098")
    content = _ubl(other)
    invoice = _historical_invoice(content)
    command = ImportInvoiceCommand(
        invoice=invoice,
        idempotency_key=f"uyumsoft:company:{COMPANY_ID}:inbox:ettn:{other.invoice_number}",
        company_id=COMPANY_ID,
        source_tax_assessment=assess_source_document(
            content, expected_sha256=sha256(content), expected_invoice_uuid=other.invoice_uuid
        ),
    )

    first = await _import_use_case(env).execute(command)
    replay = await _import_use_case(env).execute(command)
    legacy_replay = await _import_use_case(env).execute(replace(command, source_tax_assessment=None))

    assert first.review_id == replay.review_id == legacy_replay.review_id
    stored = SqlAlchemyReviewSourceInvoiceEvidenceReader(env.session).get_original(
        review_id=first.review_id, company_id=COMPANY_ID
    )
    expected = ReviewSourceInvoiceEvidence(
        review_id=first.review_id,
        company_id=COMPANY_ID,
        review_version=1,
        source_invoice_id=stored.source_invoice_id,
        invoice=invoice,
    )
    assert review_source_invoice_evidence_fingerprint(stored) == review_source_invoice_evidence_fingerprint(expected)
    assert env.session.scalar(select(func.count()).select_from(WorkbenchReviewItem)) == 1


async def test_reclassification_blocks_a_pre_w1_executable_withholding_review(env: Env) -> None:
    _executable_supplier(env)
    review_id = await _seed_historical_review(env, AY_STYLE, document=_withholding_document(_ubl(AY_STYLE)))
    assert _review(env, review_id).workflow == WorkflowType.VENDOR_BILL.value

    outcome = await _reclassify(env, review_id, version=1)

    assert outcome.changed is True
    item = _review(env, review_id)
    assert item.version == 2
    assert item.workflow == WorkflowType.MANUAL_REVIEW.value
    assert [reason["code"] for reason in item.review_reasons] == [WITHHOLDING.value]
    stage_one_versions = env.session.scalars(
        select(WorkbenchReviewExecutionEvidence.review_version).where(
            WorkbenchReviewExecutionEvidence.review_id == review_id
        )
    ).all()
    assert stage_one_versions == [1]  # no Stage-1 evidence for the blocked version


async def test_reclassification_of_a_clean_document_is_a_no_op(env: Env) -> None:
    _executable_supplier(env)
    review_id = await _seed_historical_review(env, AY_STYLE)

    outcome = await _reclassify(env, review_id, version=1)

    assert outcome.changed is False
    assert _review(env, review_id).workflow == WorkflowType.VENDOR_BILL.value


async def test_reclassification_with_tampered_document_is_unverified(env: Env) -> None:
    _executable_supplier(env)
    review_id = await _seed_historical_review(env, AY_STYLE)
    document = env.session.scalar(select(InvoiceDocument))
    (env.storage_root / document.storage_key).write_bytes(_withholding_document(_ubl(AY_STYLE)))

    await _reclassify(env, review_id, version=1)

    item = _review(env, review_id)
    assert [reason["code"] for reason in item.review_reasons] == [UNVERIFIED.value]
    assert ("issue", SourceDocumentIssue.HASH_MISMATCH.value) in [
        tuple(pair) for pair in item.review_reasons[0]["details"]
    ]


async def test_reclassification_missing_file_under_a_verified_root_is_unverified(env: Env) -> None:
    """R1: the row exists, the populated root is mounted, the file is gone -> deterministic."""

    _executable_supplier(env)
    review_id = await _seed_historical_review(env, AY_STYLE, document=_withholding_document(_ubl(AY_STYLE)))
    (env.storage_root / "unrelated-marker").write_text("keeps the root populated")
    document = env.session.scalar(select(InvoiceDocument))
    (env.storage_root / document.storage_key).unlink()

    await _reclassify(env, review_id, version=1)

    item = _review(env, review_id)
    assert [reason["code"] for reason in item.review_reasons] == [UNVERIFIED.value]
    assert ["issue", SourceDocumentIssue.DOCUMENT_FILE_MISSING.value] in item.review_reasons[0]["details"]


async def test_reclassification_storage_outage_writes_nothing(env: Env) -> None:
    """R1: an unmounted / missing storage root is an outage: 503, no reason persisted."""

    _executable_supplier(env)
    review_id = await _seed_historical_review(env, AY_STYLE, document=_withholding_document(_ubl(AY_STYLE)))
    env.storage_root.rename(env.storage_root.with_name("documents-unmounted"))

    with pytest.raises(SourceTaxFactsUnavailableError):
        await _reclassify(env, review_id, version=1)
    env.session.rollback()

    item = _review(env, review_id)
    assert (item.version, item.workflow, item.review_reasons) == (1, WorkflowType.VENDOR_BILL.value, [])
    assert env.session.scalar(select(func.count()).select_from(WorkbenchReviewReclassification)) == 0


async def test_reclassification_without_any_document_row_is_unverified(env: Env) -> None:
    _executable_supplier(env)
    review_id = await _seed_historical_review(env, AY_STYLE)
    env.session.execute(delete(InvoiceDocument))
    env.session.commit()

    await _reclassify(env, review_id, version=1)

    assert [reason["code"] for reason in _review(env, review_id).review_reasons] == [UNVERIFIED.value]


def _recovery(env: Env, reader) -> RebuildReviewExecutionEvidenceUseCase:
    repository = SqlAlchemyReviewRepository(env.session)
    return RebuildReviewExecutionEvidenceUseCase(
        review_reader=repository,
        resolver=EffectiveDecisionResolver(
            decision_engine=_decision_engine(env.session, env.partners),
            source_invoice_reader=SqlAlchemyReviewSourceInvoiceEvidenceReader(env.session),
            source_tax_facts_reader=reader,
        ),
        execution_evidence_writer=repository,
        unit_of_work=SqlAlchemyUnitOfWork(env.session),
    )


async def _drop_stage_one(env: Env, review_id: str) -> None:
    env.session.execute(
        delete(WorkbenchReviewExecutionEvidence).where(WorkbenchReviewExecutionEvidence.review_id == review_id)
    )
    env.session.commit()


async def test_evidence_recovery_refuses_a_withholding_review(env: Env) -> None:
    _executable_supplier(env)
    review_id = await _seed_historical_review(env, AY_STYLE, document=_withholding_document(_ubl(AY_STYLE)))
    await _drop_stage_one(env, review_id)

    with pytest.raises(ExecutionEvidenceRecoveryMismatchError):
        await _recovery(env, _real_reader(env)).execute(
            RebuildExecutionEvidenceCommand(review_id=review_id, company_id=COMPANY_ID, expected_version=1)
        )
    env.session.rollback()

    assert _stage_one_count(env, review_id) == 0


async def test_evidence_recovery_still_works_for_a_clean_document(env: Env) -> None:
    _executable_supplier(env)
    review_id = await _seed_historical_review(env, AY_STYLE)
    await _drop_stage_one(env, review_id)

    result = await _recovery(env, _real_reader(env)).execute(
        RebuildExecutionEvidenceCommand(review_id=review_id, company_id=COMPANY_ID, expected_version=1)
    )

    assert result.already_applied is False
    assert _stage_one_count(env, review_id) == 1


async def test_evidence_recovery_storage_outage_writes_nothing(env: Env) -> None:
    _executable_supplier(env)
    review_id = await _seed_historical_review(env, AY_STYLE)
    await _drop_stage_one(env, review_id)
    reader = FixedSourceTaxFactsReader(error=SourceTaxFactsUnavailableError("offline"))

    with pytest.raises(SourceTaxFactsUnavailableError):
        await _recovery(env, reader).execute(
            RebuildExecutionEvidenceCommand(review_id=review_id, company_id=COMPANY_ID, expected_version=1)
        )
    env.session.rollback()

    assert _stage_one_count(env, review_id) == 0


async def test_source_identity_correction_classifies_with_the_source_tax_facts(env: Env) -> None:
    review_id = await _seed_historical_review(env, AY_STYLE, document=_withholding_document(_ubl(AY_STYLE)))
    real_reader = _real_reader(env)

    def resolver_factory(reader) -> EffectiveDecisionResolver:
        return EffectiveDecisionResolver(
            decision_engine=_decision_engine(env.session, env.partners),
            source_invoice_reader=reader,
            source_tax_facts_reader=real_reader,
        )

    report = await env.correct(review_id, apply=True, resolver_factory=resolver_factory)

    assert WITHHOLDING.value in report.new_reason_codes
    item = _review(env, review_id)
    assert item.version == 2
    assert item.review_reasons[0]["code"] == WITHHOLDING.value
    assert item.supplier_tax_number == AY_STYLE.vkn


# ===================================================================== wiring is required


def test_resolver_and_reclassifier_require_a_facts_reader() -> None:
    with pytest.raises(TypeError):
        EffectiveDecisionResolver(decision_engine=object(), source_invoice_reader=object())  # type: ignore[call-arg]
    with pytest.raises(WorkbenchContractError):
        EffectiveDecisionResolver(
            decision_engine=object(),  # type: ignore[arg-type]
            source_invoice_reader=object(),  # type: ignore[arg-type]
            source_tax_facts_reader=None,  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError):
        ReclassifyWorkbenchReviewUseCase(  # type: ignore[call-arg]
            decision_engine=object(), source_invoice_reader=object(), reclassification_writer=object()
        )


def test_every_production_classifier_is_wired_with_the_stored_document_reader() -> None:
    """Every resolver / reclassifier composed in ``app/`` reads the real stored document."""

    constructors = {"EffectiveDecisionResolver", "ReclassifyWorkbenchReviewUseCase"}
    found = 0
    for path in Path("app").rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) in constructors):
                continue
            keywords = {keyword.arg: keyword.value for keyword in node.keywords}
            value = keywords.get("source_tax_facts_reader")
            assert value is not None, f"{path}:{node.lineno} has no source_tax_facts_reader"
            if path.parts[1] == "composition":
                assert isinstance(value, ast.Call) and getattr(value.func, "id", None) == (
                    "build_source_tax_facts_reader"
                ), f"{path}:{node.lineno}"
                found += 1
    assert found == 6


def test_correction_locator_is_the_pr_r_locator() -> None:
    reader = build_source_tax_facts_reader(session=object(), settings=Settings())  # type: ignore[arg-type]

    assert isinstance(reader._document_locator, SqlAlchemyReviewSourceInvoiceCorrectionRepository)
    assert isinstance(reader._document_reader, StoredSourceTaxDocumentReader)


# ===================================================================== pre-W1 Stage-1 evidence


def _decide_vendor_bill(env: Env, review_id: str, check):

    return SubmitReviewDecisionUseCase(
        review_decision_writer=SqlAlchemyReviewRepository(env.session),
        unit_of_work=SqlAlchemyUnitOfWork(env.session),
        execution_evidence_reader=SqlAlchemyReviewExecutionEvidenceReader(env.session),
        source_tax_safety_check=check,
    ).execute(
        ReviewDecisionCommand(
            review_id=review_id,
            company_id=COMPANY_ID,
            expected_version=1,
            decision=ReviewDecisionType.SELECT_WORKFLOW,
            selected_workflow=WorkflowType.VENDOR_BILL,
            decided_by="w1-test-operator",
            idempotency_key=f"w1:{review_id}",
            # An explicit operator tax choice does not route around the source document.
            tax_resolutions=(TaxResolution(line_number="1", tax_index=0, selected_tax_id=8801),),
        )
    )


async def test_pinned_pre_w1_stage_one_evidence_cannot_be_accepted_for_a_withholding_invoice(env: Env) -> None:

    _executable_supplier(env)
    review_id = await _seed_historical_review(env, AY_STYLE, document=_withholding_document(_ubl(AY_STYLE)))
    assert _stage_one_count(env, review_id) == 1  # pinned by the pre-W1 import

    with pytest.raises(SourceTaxSafetyBlockedError) as raised:
        _decide_vendor_bill(env, review_id, SourceTaxSafetyCheck(facts_reader=_real_reader(env)))
    env.session.rollback()

    assert raised.value.reason_codes == (WITHHOLDING,)
    assert env.session.scalar(select(func.count()).select_from(WorkbenchReviewDecision)) == 0
    assert _review(env, review_id).version == 1
