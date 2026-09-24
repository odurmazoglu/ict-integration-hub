"""P0-PROD-15AB: review-decision success-without-commit transaction defect.

Regression coverage for the real production symptom discovered in P0-PROD-15AA:
``POST .../decision`` returned HTTP 200 ``accepted=true`` with the review
projected to advance from version 3 to version 4, but a fresh read immediately
afterward showed the review still at version 3 -- and a SECOND submission using a
different idempotency key but the SAME stale ``expected_version=3`` also returned
a fresh-looking ``accepted=true``/version=4 response, rather than the expected
optimistic-concurrency conflict.

Root cause: ``SubmitReviewDecisionUseCase`` had no ``UnitOfWork`` at all, and none
of ``ReviewDecisionWriter``'s three write methods ever call ``session.commit()`` --
each only stages its record set inside one internal nested unit of work (a
SAVEPOINT), which makes pending changes visible to further calls sharing the same
session, but never durably persists them. The request-scoped session is simply
closed (never committed) once the request completes, silently discarding every
pending write -- while the acknowledgement returned to the caller was already
built from the in-memory (staged-but-uncommitted) result, so the API reported
success for a decision that was never durably persisted.

Two tests here:

* ``test_repository_write_alone_is_never_durable_without_an_explicit_commit``
  reproduces the defect at the exact layer it originates in -- calling
  ``ReviewDecisionWriter.submit_review_decision_with_execution_evidence`` directly,
  with no ``UnitOfWork`` involved, proves the repository layer's own contract:
  it stages but never commits. This stays true after the fix (by design -- the
  fix adds the missing commit in the *use case* layer, per the existing
  UnitOfWork/request-transaction architecture, not inside the repository).
* The CloudSpark-shaped acceptance tests prove the FIXED ``SubmitReviewDecisionUseCase``
  (wired with a real ``SqlAlchemyUnitOfWork``) durably commits on success, that a
  completely fresh session observes the committed result, and that a stale-version
  replay now correctly hits the existing optimistic-concurrency contract instead of
  silently "succeeding" again.
"""

from __future__ import annotations

import os
import tempfile
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from app.application.execution.contracts import ExecutionSourceInvoice
from app.application.expense_mapping.matching import OperatingExpenseMatchResult, OperatingExpenseMatchStatus
from app.application.workbench.commands import ReviewDecisionCommand
from app.application.workbench.decision_use_cases import SubmitReviewDecisionUseCase
from app.application.workbench.dto import ReviewDecisionType, ReviewStatus
from app.application.workbench.evidence import ReviewExecutionEvidence
from app.application.workbench.exceptions import ReviewStateConflictError, ReviewVersionConflictError
from app.application.workflow import WorkflowType
from app.db.base import Base
from app.domain.invoice import Header, InternalInvoice, InvoiceLine, MonetaryTotals, Party, Tax
from app.matching import (
    InvoiceProductLineResult,
    InvoiceProductMatchResult,
    PartnerMatchResult,
    PartnerMatchStatus,
    ProductMatchResult,
    ProductMatchStatus,
)
from app.models.execution_source_invoice_evidence import ExecutionSourceInvoiceEvidence
from app.models.workbench_review_classification_evidence import WorkbenchReviewClassificationEvidence
from app.models.workbench_review_decision import WorkbenchReviewDecision
from app.models.workbench_review_execution_evidence import WorkbenchReviewExecutionEvidence
from app.models.workbench_review_item import WorkbenchReviewItem
from app.models.workbench_review_reclassification import WorkbenchReviewReclassification
from app.models.workbench_review_source_invoice_evidence import WorkbenchReviewSourceInvoiceEvidence
from app.persistence import SqlAlchemyReviewExecutionEvidenceReader, SqlAlchemyReviewRepository, SqlAlchemyUnitOfWork

COMPANY_ID = 1
REVIEW_ID = "review:p0-prod-15ab-cloudspark-shape"
CANONICAL_PARTNER_ID = 439
EXPENSE_ACCOUNT = 247


@pytest.fixture()
def engine():
    """A file-backed SQLite database, each Session getting its own real
    connection from the pool -- the only faithful way to prove that a write
    performed through one session is (or isn't) durably visible through a
    genuinely different one, mirroring separate HTTP request-scoped sessions in
    production. Uses SQLAlchemy's documented pysqlite recipe (disable pysqlite's
    own implicit transaction handling; let SQLAlchemy issue BEGIN/COMMIT/ROLLBACK
    explicitly) -- without it, pysqlite's default autocommit-outside-an-explicit-
    BEGIN behavior makes a Session.begin_nested() SAVEPOINT release durable even
    across a later session.rollback(), which is specific to that default pysqlite
    quirk and not how any real client/server database (including production's
    PostgreSQL) behaves.
    """

    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    os.remove(path)
    sqlalchemy_engine = create_engine(f"sqlite:///{path}")

    @event.listens_for(sqlalchemy_engine, "connect")
    def _do_connect(dbapi_connection, connection_record):  # noqa: ARG001
        dbapi_connection.isolation_level = None

    @event.listens_for(sqlalchemy_engine, "begin")
    def _do_begin(conn):
        conn.exec_driver_sql("BEGIN")

    _create_tables(sqlalchemy_engine)
    try:
        yield sqlalchemy_engine
    finally:
        sqlalchemy_engine.dispose()
        if os.path.exists(path):
            os.remove(path)


def _create_tables(engine) -> None:
    Base.metadata.create_all(
        engine,
        tables=[
            WorkbenchReviewItem.__table__,
            WorkbenchReviewExecutionEvidence.__table__,
            WorkbenchReviewClassificationEvidence.__table__,
            WorkbenchReviewSourceInvoiceEvidence.__table__,
            WorkbenchReviewReclassification.__table__,
            WorkbenchReviewDecision.__table__,
            ExecutionSourceInvoiceEvidence.__table__,
        ],
    )
    return engine


def _new_session(engine) -> Session:
    return sessionmaker(bind=engine)()


def _invoice() -> InternalInvoice:
    """Identifier-free lines, shaped like the real CloudSpark production invoice.
    A fixture shape only -- no CloudSpark-specific branching exists anywhere in
    application code; this is exactly the same InternalInvoice DTO any invoice
    uses.
    """

    return InternalInvoice(
        header=Header(
            invoice_number="I082026000000009",
            invoice_uuid="P0-PROD-15AB-ETTN",
            ettn="P0-PROD-15AB-ETTN",
            issue_date=date(2026, 9, 8),
            currency_code="TRY",
        ),
        supplier=Party(name="CLOUDSPARK BULUT TEKNOLOJILERI SAN. TIC. A.S.", tax_number="1760390647"),
        customer=Party(name="ICT Teknoloji", tax_number="4651205941"),
        totals=MonetaryTotals(
            line_extension_amount=Decimal("4959.80"),
            tax_exclusive_amount=Decimal("4959.80"),
            tax_inclusive_amount=Decimal("5951.76"),
            payable_amount=Decimal("5951.76"),
        ),
        lines=(
            InvoiceLine(
                line_number="1",
                description="CPU",
                quantity=Decimal("20"),
                unit_code="C62",
                unit_price=Decimal("74.40"),
                line_extension_amount=Decimal("1487.94"),
                taxes=(Tax(tax_type="KDV", rate=Decimal("20")),),
            ),
        ),
    )


def _partner_matched() -> PartnerMatchResult:
    return PartnerMatchResult(
        status=PartnerMatchStatus.MATCHED,
        partner_id=CANONICAL_PARTNER_ID,
        matched_by="supplier_remediation_effect",
        reason="Resolved via an accepted supplier remediation effect for this review.",
        candidate_count=1,
        confidence=Decimal("1.00"),
    )


def _product_invalid_input(invoice: InternalInvoice) -> InvoiceProductMatchResult:
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


def _tax_matched(invoice: InternalInvoice):
    from app.tax_mapping import InvoiceTaxLineResult, InvoiceTaxMappingResult, TaxMatchResult, TaxMatchStatus, TaxType

    return InvoiceTaxMappingResult(
        line_results=tuple(
            InvoiceTaxLineResult(
                line_number=line.line_number,
                tax_index=idx,
                result=TaxMatchResult(
                    status=TaxMatchStatus.MATCHED,
                    tax_id=8801,
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
            for idx, _t in enumerate(line.taxes)
        )
    )


def _cloudspark_shaped_execution_evidence(*, review_version: int) -> ReviewExecutionEvidence:
    invoice = _invoice()
    operating_expense_match = OperatingExpenseMatchResult(
        status=OperatingExpenseMatchStatus.MATCHED,
        reason="Resolved via an accepted review-scoped accounting resolution for this review.",
        candidate_count=1,
        mapping_id=1,
        company_id=COMPANY_ID,
        vendor_partner_id=CANONICAL_PARTNER_ID,
        expense_account_id=EXPENSE_ACCOUNT,
        expense_category="IT_HARDWARE_INTERNAL",
        matched_by="review_accounting_resolution",
        confidence=Decimal("1.00"),
    )
    return ReviewExecutionEvidence(
        review_id=REVIEW_ID,
        company_id=COMPANY_ID,
        review_version=review_version,
        source_invoice_id=invoice.header.ettn,
        invoice=invoice,
        partner_match=_partner_matched(),
        product_match=_product_invalid_input(invoice),
        tax_match=_tax_matched(invoice),
        operating_expense_match=operating_expense_match,
    )


def _seed_cloudspark_shaped_review_at_v3(session: Session) -> None:
    """Directly seeds the exact durable review shape P0-PROD-15AA found in
    production: version 3, pending_review, vendor_bill, reasons=[], with
    matching Stage-1 execution evidence already persisted for version 3 (as if
    P0-PROD-15Z's rebuild had already run) -- so this test targets only the
    decision-submission transaction boundary, not upstream reclassification.
    """

    session.add(
        WorkbenchReviewItem(
            review_id=REVIEW_ID,
            company_id=COMPANY_ID,
            invoice_id="P0-PROD-15AB-ETTN",
            invoice_number="I082026000000009",
            supplier_tax_number="1760390647",
            supplier_name="CloudSpark",
            invoice_date=date(2026, 9, 8),
            currency="TRY",
            total_amount=Decimal("5951.76"),
            workflow=WorkflowType.VENDOR_BILL.value,
            status=ReviewStatus.PENDING_REVIEW.value,
            review_reasons=[],
            warnings=[],
            version=3,
            idempotency_key=f"uyumsoft:{COMPANY_ID}:{REVIEW_ID}",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )
    session.flush()
    SqlAlchemyReviewRepository(session).create_execution_evidence_for_current_version(
        review_id=REVIEW_ID,
        company_id=COMPANY_ID,
        expected_version=3,
        evidence=_cloudspark_shaped_execution_evidence(review_version=3),
    )
    session.commit()


def _decision_command(*, expected_version: int, idempotency_key: str) -> ReviewDecisionCommand:
    return ReviewDecisionCommand(
        review_id=REVIEW_ID,
        company_id=COMPANY_ID,
        expected_version=expected_version,
        decision=ReviewDecisionType.SELECT_WORKFLOW,
        selected_workflow=WorkflowType.VENDOR_BILL,
        decided_by="p0-prod-15ab-test-operator",
        idempotency_key=idempotency_key,
    )


def _fresh_review_item(session: Session) -> WorkbenchReviewItem:
    item = session.scalar(select(WorkbenchReviewItem).where(WorkbenchReviewItem.review_id == REVIEW_ID))
    assert item is not None
    return item


# ======================================================== reproduction: repository layer alone


def test_repository_write_alone_is_never_durable_without_an_explicit_commit(engine) -> None:
    """Reproduces the exact production symptom at its root: the repository's
    ``ReviewDecisionWriter`` write methods stage but never commit. Calling one
    directly (bypassing the use case and any UnitOfWork entirely) proves that a
    genuinely fresh session sees no trace of the write, even though the call
    itself reports a plausible-looking successful acknowledgement.
    """

    setup_session = _new_session(engine)
    _seed_cloudspark_shaped_review_at_v3(setup_session)
    setup_session.close()

    request_session = _new_session(engine)
    repository = SqlAlchemyReviewRepository(request_session)
    evidence_reader = SqlAlchemyReviewExecutionEvidenceReader(request_session)
    evidence: ExecutionSourceInvoice = evidence_reader.get_evidence(
        review_id=REVIEW_ID, company_id=COMPANY_ID, expected_version=3
    )
    command = _decision_command(expected_version=3, idempotency_key=str(uuid4()))

    acknowledgement = repository.submit_review_decision_with_execution_evidence(command, evidence)
    assert acknowledgement.accepted is True
    assert acknowledgement.version == 4  # the in-memory, staged-but-uncommitted result

    # No commit call anywhere. app.api.dependencies.get_db_session's finally block
    # only ever calls session.close() -- never commit() -- for exactly this reason:
    # the *use case* layer must own the transaction boundary (this is the fix, see
    # SubmitReviewDecisionUseCase._write_and_commit).
    request_session.close()

    fresh_session = _new_session(engine)
    item = fresh_session.scalar(select(WorkbenchReviewItem).where(WorkbenchReviewItem.review_id == REVIEW_ID))
    assert item is not None
    assert item.version == 3  # unchanged -- the staged write was never durably committed
    assert item.status == ReviewStatus.PENDING_REVIEW.value
    assert fresh_session.query(WorkbenchReviewDecision).count() == 0  # the decision row never persisted either


# ======================================================== CloudSpark-shaped acceptance (post-fix)


def test_first_decision_durably_commits_and_a_fresh_session_observes_it(engine) -> None:
    setup_session = _new_session(engine)
    _seed_cloudspark_shaped_review_at_v3(setup_session)
    setup_session.close()

    request_session = _new_session(engine)
    use_case = SubmitReviewDecisionUseCase(
        review_decision_writer=SqlAlchemyReviewRepository(request_session),
        unit_of_work=SqlAlchemyUnitOfWork(request_session),
        execution_evidence_reader=SqlAlchemyReviewExecutionEvidenceReader(request_session),
    )
    command = _decision_command(expected_version=3, idempotency_key="p0-prod-15ab-decision-1")

    acknowledgement = use_case.execute(command)
    assert acknowledgement.accepted is True
    assert acknowledgement.version == 4
    assert acknowledgement.status is ReviewStatus.DECISION_SUBMITTED

    # Simulate the request ending -- no further action on this session.
    request_session.close()

    fresh_session = _new_session(engine)
    item = _fresh_review_item(fresh_session)
    assert item.version == 4
    assert item.status == ReviewStatus.DECISION_SUBMITTED.value
    assert fresh_session.query(WorkbenchReviewDecision).count() == 1


def test_stale_expected_version_second_request_hits_the_real_conflict_contract(engine) -> None:
    setup_session = _new_session(engine)
    _seed_cloudspark_shaped_review_at_v3(setup_session)
    setup_session.close()

    first_session = _new_session(engine)
    SubmitReviewDecisionUseCase(
        review_decision_writer=SqlAlchemyReviewRepository(first_session),
        unit_of_work=SqlAlchemyUnitOfWork(first_session),
        execution_evidence_reader=SqlAlchemyReviewExecutionEvidenceReader(first_session),
    ).execute(_decision_command(expected_version=3, idempotency_key="p0-prod-15ab-decision-1"))
    first_session.close()

    # A second request, a DIFFERENT idempotency key, but the SAME now-stale
    # expected_version=3 -- must fail closed with the existing optimistic-
    # concurrency contract, never silently "succeed" identically again. The first
    # decision also advanced status away from pending_review, so the writer's own
    # conflict diagnosis correctly reports ReviewStateConflictError (checked before
    # ReviewVersionConflictError in _raise_submission_conflict) -- either way, the
    # request must fail closed, never appear to succeed a second time.
    second_session = _new_session(engine)
    second_use_case = SubmitReviewDecisionUseCase(
        review_decision_writer=SqlAlchemyReviewRepository(second_session),
        unit_of_work=SqlAlchemyUnitOfWork(second_session),
        execution_evidence_reader=SqlAlchemyReviewExecutionEvidenceReader(second_session),
    )
    with pytest.raises(ReviewStateConflictError):
        second_use_case.execute(_decision_command(expected_version=3, idempotency_key="p0-prod-15ab-decision-2"))
    second_session.close()

    fresh_session = _new_session(engine)
    item = _fresh_review_item(fresh_session)
    assert item.version == 4  # unchanged by the rejected stale request
    assert item.status == ReviewStatus.DECISION_SUBMITTED.value
    # Exactly one decision row -- the stale, rejected request created no duplicate.
    assert fresh_session.query(WorkbenchReviewDecision).count() == 1
    decisions = fresh_session.query(WorkbenchReviewDecision).all()
    assert {d.idempotency_key for d in decisions} == {"p0-prod-15ab-decision-1"}


def test_pure_stale_version_while_still_pending_review_conflicts(engine) -> None:
    """A concurrent, decision-unrelated advance (e.g. another reclassification)
    bumps the review's version while it is still pending_review -- exercises the
    other branch of the writer's own conflict diagnosis (ReviewVersionConflictError,
    not ReviewStateConflictError)."""

    setup_session = _new_session(engine)
    _seed_cloudspark_shaped_review_at_v3(setup_session)
    item = _fresh_review_item(setup_session)
    item.version = 5
    setup_session.commit()

    request_session = _new_session(engine)
    use_case = SubmitReviewDecisionUseCase(
        review_decision_writer=SqlAlchemyReviewRepository(request_session),
        unit_of_work=SqlAlchemyUnitOfWork(request_session),
        execution_evidence_reader=SqlAlchemyReviewExecutionEvidenceReader(request_session),
    )
    with pytest.raises(ReviewVersionConflictError):
        use_case.execute(_decision_command(expected_version=3, idempotency_key="p0-prod-15ab-decision-stale"))
    request_session.close()

    fresh_session = _new_session(engine)
    item = _fresh_review_item(fresh_session)
    assert item.version == 5  # unchanged by the rejected stale request
    assert item.status == ReviewStatus.PENDING_REVIEW.value
    assert fresh_session.query(WorkbenchReviewDecision).count() == 0


def test_identical_replay_with_matching_expected_version_is_idempotent_no_duplicate(engine) -> None:
    setup_session = _new_session(engine)
    _seed_cloudspark_shaped_review_at_v3(setup_session)
    setup_session.close()

    idempotency_key = "p0-prod-15ab-decision-replay"
    first_session = _new_session(engine)
    first = SubmitReviewDecisionUseCase(
        review_decision_writer=SqlAlchemyReviewRepository(first_session),
        unit_of_work=SqlAlchemyUnitOfWork(first_session),
        execution_evidence_reader=SqlAlchemyReviewExecutionEvidenceReader(first_session),
    ).execute(_decision_command(expected_version=3, idempotency_key=idempotency_key))
    first_session.close()

    # Genuine replay: identical idempotency key AND identical (now-stale)
    # expected_version, from a fresh session/request -- must return the exact
    # same durable result, never raise, never duplicate.
    second_session = _new_session(engine)
    second = SubmitReviewDecisionUseCase(
        review_decision_writer=SqlAlchemyReviewRepository(second_session),
        unit_of_work=SqlAlchemyUnitOfWork(second_session),
        execution_evidence_reader=SqlAlchemyReviewExecutionEvidenceReader(second_session),
    ).execute(_decision_command(expected_version=3, idempotency_key=idempotency_key))
    second_session.close()

    assert second.version == first.version == 4
    assert second.status == first.status

    fresh_session = _new_session(engine)
    assert fresh_session.query(WorkbenchReviewDecision).count() == 1
    item = _fresh_review_item(fresh_session)
    assert item.version == 4


def test_execution_evidence_relationship_remains_valid_after_commit(engine) -> None:
    """The rebuilt-execution-evidence relationship (partner/account/category) this
    decision consumed must still read back correctly after a fresh session,
    proving the fix didn't disturb P0-PROD-15Z's evidence contract."""

    setup_session = _new_session(engine)
    _seed_cloudspark_shaped_review_at_v3(setup_session)
    setup_session.close()

    request_session = _new_session(engine)
    SubmitReviewDecisionUseCase(
        review_decision_writer=SqlAlchemyReviewRepository(request_session),
        unit_of_work=SqlAlchemyUnitOfWork(request_session),
        execution_evidence_reader=SqlAlchemyReviewExecutionEvidenceReader(request_session),
    ).execute(_decision_command(expected_version=3, idempotency_key="p0-prod-15ab-decision-evidence"))
    request_session.close()

    fresh_session = _new_session(engine)
    evidence = SqlAlchemyReviewExecutionEvidenceReader(fresh_session).get_evidence(
        review_id=REVIEW_ID, company_id=COMPANY_ID, expected_version=3
    )
    assert evidence.partner_match.partner_id == CANONICAL_PARTNER_ID
    assert evidence.operating_expense_match.expense_account_id == EXPENSE_ACCOUNT
    assert evidence.operating_expense_match.expense_category == "IT_HARDWARE_INTERNAL"

    decision_evidence = fresh_session.scalar(select(ExecutionSourceInvoiceEvidence))
    assert decision_evidence is not None
    assert decision_evidence.partner_match["partner_id"] == CANONICAL_PARTNER_ID
    assert decision_evidence.operating_expense_match["expense_account_id"] == EXPENSE_ACCOUNT
