from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.application.rules import (
    InvoiceClassificationResult,
    InvoiceClassificationRuleEvidence,
    InvoiceClassificationStatus,
    InvoiceDecisionRule,
    InvoiceDecisionRuleAction,
    InvoiceDecisionRuleMatch,
    InvoiceDecisionRulePriority,
)
from app.application.workbench import (
    ReviewClassificationEvidence,
    ReviewItem,
    ReviewItemCreationService,
    ReviewStatus,
)
from app.application.workbench.exceptions import (
    ReviewDataIntegrityError,
    ReviewIdempotencyConflictError,
    ReviewNotFoundError,
    ReviewPersistenceError,
)
from app.application.workflow import ManualReviewReason, ManualReviewReasonCode, WorkflowType
from app.db.base import Base
from app.models.workbench_review_billing_evidence import WorkbenchReviewBillingEvidence
from app.models.workbench_review_classification_evidence import WorkbenchReviewClassificationEvidence
from app.models.workbench_review_execution_evidence import WorkbenchReviewExecutionEvidence
from app.models.workbench_review_item import WorkbenchReviewItem
from app.persistence import SqlAlchemyReviewClassificationEvidenceReader, SqlAlchemyReviewRepository


def test_matched_classification_evidence_persists_with_review_version(session: Session) -> None:
    repository = SqlAlchemyReviewRepository(session)

    created = repository.create_review_item_with_classification_evidence(
        _review_item(),
        company_id=7,
        idempotency_key="review-key-1",
        classification_evidence=_classification_evidence(),
    )

    record = session.scalar(select(WorkbenchReviewClassificationEvidence))
    assert created.status is ReviewStatus.PENDING_REVIEW
    assert record is not None
    assert record.review_id == "review-1"
    assert record.company_id == 7
    assert record.review_version == 1
    assert record.schema_version == 1
    assert record.status == "MATCHED"
    assert record.matched_rule_id == "rule-cloud"
    assert record.matched_rule_code == "CLOUD_COST_VENDOR_BILL"
    assert record.matched_rule_version == 3
    assert record.matched_rule_name == "Cloud cost vendor bill"
    assert record.workflow == "vendor_bill"
    assert record.classification_code == "CLOUD_COST"
    assert record.require_review is False
    assert record.require_business_context is True
    assert record.conflicting_rules == []


def test_review_required_classification_evidence_persists(session: Session) -> None:
    repository = SqlAlchemyReviewRepository(session)

    repository.create_review_item_with_classification_evidence(
        _review_item(),
        company_id=7,
        idempotency_key="review-key-1",
        classification_evidence=_classification_evidence(
            status=InvoiceClassificationStatus.REVIEW_REQUIRED,
            require_review=True,
            classification_code="EV_CHARGING",
            workflow=WorkflowType.EXPENSE,
        ),
    )

    loaded = repository.get_review_classification_evidence(review_id="review-1", company_id=7, review_version=1)
    assert loaded.status is InvoiceClassificationStatus.REVIEW_REQUIRED
    assert loaded.require_review is True
    assert loaded.workflow is WorkflowType.EXPENSE
    assert loaded.classification_code == "EV_CHARGING"


def test_no_match_classification_evidence_persists_as_explicit_evidence(session: Session) -> None:
    repository = SqlAlchemyReviewRepository(session)

    repository.create_review_item_with_classification_evidence(
        _review_item(),
        company_id=7,
        idempotency_key="review-key-1",
        classification_evidence=_no_match_evidence(),
    )

    record = session.scalar(select(WorkbenchReviewClassificationEvidence))
    assert record is not None
    assert record.status == "NO_MATCH"
    assert record.matched_rule_code is None
    assert record.classification_code is None
    assert (
        repository.get_review_classification_evidence(
            review_id="review-1",
            company_id=7,
            review_version=1,
        ).status
        is InvoiceClassificationStatus.NO_MATCH
    )


def test_conflict_classification_evidence_persists_all_winning_rules(session: Session) -> None:
    repository = SqlAlchemyReviewRepository(session)

    repository.create_review_item_with_classification_evidence(
        _review_item(),
        company_id=7,
        idempotency_key="review-key-1",
        classification_evidence=_conflict_evidence(),
    )

    loaded = repository.get_review_classification_evidence(review_id="review-1", company_id=7, review_version=1)
    assert loaded.status is InvoiceClassificationStatus.CONFLICT
    assert tuple(rule.rule_code for rule in loaded.conflicting_rules) == (
        "CLOUD_COST_VENDOR_BILL",
        "SOFTWARE_LICENSE_EXPENSE",
    )
    assert loaded.conflicting_rules[0].workflow is WorkflowType.VENDOR_BILL
    assert loaded.conflicting_rules[0].classification_code == "CLOUD_COST"
    assert loaded.conflicting_rules[1].workflow is WorkflowType.EXPENSE
    assert loaded.conflicting_rules[1].classification_code == "SOFTWARE_LICENSE_COST"


def test_classification_evidence_dto_is_immutable() -> None:
    evidence = _classification_evidence()

    assert not hasattr(evidence, "__dict__")
    with pytest.raises(FrozenInstanceError):
        evidence.review_id = "other"  # type: ignore[misc]


def test_classification_evidence_from_result_pins_rule_result() -> None:
    rule = _rule()
    result = InvoiceClassificationResult(
        status=InvoiceClassificationStatus.MATCHED,
        matched_rules=(rule,),
        selected_rule=rule,
        matched_rule_evidence=(_rule_evidence(),),
    )

    evidence = ReviewClassificationEvidence.from_result(
        review_id="review-1",
        company_id=7,
        review_version=1,
        result=result,
    )

    assert evidence.matched_rule_code == "CLOUD_COST_VENDOR_BILL"
    assert evidence.workflow is WorkflowType.VENDOR_BILL
    assert evidence.classification_code == "CLOUD_COST"


def test_reader_uses_exact_review_company_version_lookup(session: Session) -> None:
    _create_classification_evidence(session)

    loaded = SqlAlchemyReviewClassificationEvidenceReader(session).get_classification_evidence(
        review_id="review-1",
        company_id=7,
        review_version=1,
    )

    assert loaded == _classification_evidence()


def test_reader_rejects_other_company_lookup(session: Session) -> None:
    _create_classification_evidence(session)

    with pytest.raises(ReviewNotFoundError):
        SqlAlchemyReviewClassificationEvidenceReader(session).get_classification_evidence(
            review_id="review-1",
            company_id=8,
            review_version=1,
        )


def test_reader_rejects_other_review_version_lookup(session: Session) -> None:
    _create_classification_evidence(session)

    with pytest.raises(ReviewNotFoundError):
        SqlAlchemyReviewClassificationEvidenceReader(session).get_classification_evidence(
            review_id="review-1",
            company_id=7,
            review_version=2,
        )


def test_missing_classification_evidence_fails_safely(session: Session) -> None:
    with pytest.raises(ReviewNotFoundError):
        SqlAlchemyReviewClassificationEvidenceReader(session).get_classification_evidence(
            review_id="review-1",
            company_id=7,
            review_version=1,
        )


def test_malformed_classification_evidence_fails_safely_without_raw_leak(session: Session) -> None:
    _seed_malformed_classification_evidence(session, matched_rule_name="<secret-token>")

    with pytest.raises(ReviewDataIntegrityError) as exc_info:
        SqlAlchemyReviewClassificationEvidenceReader(session).get_classification_evidence(
            review_id="review-1",
            company_id=7,
            review_version=1,
        )

    assert "secret-token" not in str(exc_info.value)


def test_unsupported_schema_version_fails_safely(session: Session) -> None:
    _create_classification_evidence(session)
    record = session.scalar(select(WorkbenchReviewClassificationEvidence))
    assert record is not None
    record.schema_version = 999
    session.flush()

    with pytest.raises(ReviewDataIntegrityError):
        SqlAlchemyReviewClassificationEvidenceReader(session).get_classification_evidence(
            review_id="review-1",
            company_id=7,
            review_version=1,
        )


def test_exact_classification_replay_is_idempotent(session: Session) -> None:
    repository = SqlAlchemyReviewRepository(session)
    item = _review_item()
    evidence = _classification_evidence()

    first = repository.create_review_item_with_classification_evidence(
        item,
        company_id=7,
        idempotency_key="review-key-1",
        classification_evidence=evidence,
    )
    second = repository.create_review_item_with_classification_evidence(
        item,
        company_id=7,
        idempotency_key="review-key-1",
        classification_evidence=evidence,
    )

    assert second == first
    assert session.query(WorkbenchReviewItem).count() == 1
    assert session.query(WorkbenchReviewClassificationEvidence).count() == 1


def test_changed_classification_replay_fails_closed(session: Session) -> None:
    repository = SqlAlchemyReviewRepository(session)
    item = _review_item()
    repository.create_review_item_with_classification_evidence(
        item,
        company_id=7,
        idempotency_key="review-key-1",
        classification_evidence=_classification_evidence(classification_code="CLOUD_COST"),
    )

    with pytest.raises(ReviewIdempotencyConflictError):
        repository.create_review_item_with_classification_evidence(
            item,
            company_id=7,
            idempotency_key="review-key-1",
            classification_evidence=_classification_evidence(classification_code="SOFTWARE_LICENSE_COST"),
        )

    assert session.query(WorkbenchReviewClassificationEvidence).count() == 1


def test_removed_conflict_rule_replay_fails_closed(session: Session) -> None:
    repository = SqlAlchemyReviewRepository(session)
    item = _review_item()
    three_rules = (
        _rule_evidence(),
        _rule_evidence(rule_code="SOFTWARE_LICENSE_EXPENSE", classification_code="SOFTWARE_LICENSE_COST"),
        _rule_evidence(rule_code="OFFICE_UTILITY_EXPENSE", classification_code="OFFICE_UTILITY"),
    )
    repository.create_review_item_with_classification_evidence(
        item,
        company_id=7,
        idempotency_key="review-key-1",
        classification_evidence=_conflict_evidence(conflicting_rules=three_rules),
    )

    with pytest.raises(ReviewIdempotencyConflictError):
        repository.create_review_item_with_classification_evidence(
            item,
            company_id=7,
            idempotency_key="review-key-1",
            classification_evidence=_conflict_evidence(conflicting_rules=three_rules[:2]),
        )

    assert session.query(WorkbenchReviewClassificationEvidence).count() == 1


def test_added_conflict_rule_replay_fails_closed(session: Session) -> None:
    repository = SqlAlchemyReviewRepository(session)
    item = _review_item()
    two_rules = (_rule_evidence(), _rule_evidence(rule_code="SOFTWARE_LICENSE_EXPENSE"))
    repository.create_review_item_with_classification_evidence(
        item,
        company_id=7,
        idempotency_key="review-key-1",
        classification_evidence=_conflict_evidence(conflicting_rules=two_rules),
    )

    with pytest.raises(ReviewIdempotencyConflictError):
        repository.create_review_item_with_classification_evidence(
            item,
            company_id=7,
            idempotency_key="review-key-1",
            classification_evidence=_conflict_evidence(
                conflicting_rules=(
                    *two_rules,
                    _rule_evidence(rule_code="OFFICE_UTILITY_EXPENSE", classification_code="OFFICE_UTILITY"),
                )
            ),
        )

    assert session.query(WorkbenchReviewClassificationEvidence).count() == 1


def test_reordered_equivalent_conflict_evidence_is_idempotent(session: Session) -> None:
    repository = SqlAlchemyReviewRepository(session)
    item = _review_item()
    evidence = _conflict_evidence()
    repository.create_review_item_with_classification_evidence(
        item,
        company_id=7,
        idempotency_key="review-key-1",
        classification_evidence=evidence,
    )

    replayed = repository.create_review_item_with_classification_evidence(
        item,
        company_id=7,
        idempotency_key="review-key-1",
        classification_evidence=_conflict_evidence(conflicting_rules=tuple(reversed(evidence.conflicting_rules))),
    )

    assert replayed.review_id == item.review_id
    assert session.query(WorkbenchReviewClassificationEvidence).count() == 1


def test_review_creation_and_classification_evidence_persist_atomically(session: Session) -> None:
    repository = SqlAlchemyReviewRepository(session)

    repository.create_review_item_with_classification_evidence(
        _review_item(),
        company_id=7,
        idempotency_key="review-key-1",
        classification_evidence=_classification_evidence(),
    )

    assert session.query(WorkbenchReviewItem).count() == 1
    assert session.query(WorkbenchReviewClassificationEvidence).count() == 1


def test_classification_evidence_insert_failure_rolls_back_review_creation(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SqlAlchemyReviewRepository(session)
    original_add = session.add

    def fail_on_classification(record: object) -> None:
        if isinstance(record, WorkbenchReviewClassificationEvidence):
            raise SQLAlchemyError("database detail with secret-token")
        original_add(record)

    monkeypatch.setattr(session, "add", fail_on_classification)

    with pytest.raises(ReviewPersistenceError) as exc_info:
        repository.create_review_item_with_classification_evidence(
            _review_item(),
            company_id=7,
            idempotency_key="review-key-1",
            classification_evidence=_classification_evidence(),
        )

    assert "secret-token" not in str(exc_info.value)
    assert session.query(WorkbenchReviewItem).count() == 0
    assert session.query(WorkbenchReviewClassificationEvidence).count() == 0


def test_review_insert_failure_creates_no_classification_evidence(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SqlAlchemyReviewRepository(session)
    original_add = session.add

    def fail_on_review(record: object) -> None:
        if isinstance(record, WorkbenchReviewItem):
            raise SQLAlchemyError("database detail")
        original_add(record)

    monkeypatch.setattr(session, "add", fail_on_review)

    with pytest.raises(ReviewPersistenceError):
        repository.create_review_item_with_classification_evidence(
            _review_item(),
            company_id=7,
            idempotency_key="review-key-1",
            classification_evidence=_classification_evidence(),
        )

    assert session.query(WorkbenchReviewItem).count() == 0
    assert session.query(WorkbenchReviewClassificationEvidence).count() == 0


def test_historical_classification_does_not_change_after_rule_configuration_changes(session: Session) -> None:
    _create_classification_evidence(
        session,
        evidence=_classification_evidence(
            workflow=WorkflowType.VENDOR_BILL,
            classification_code="CLOUD_COST",
        ),
    )
    changed_current_rule_result = _classification_evidence(
        workflow=WorkflowType.EXPENSE,
        classification_code="SOFTWARE_LICENSE_COST",
    )

    loaded = SqlAlchemyReviewClassificationEvidenceReader(session).get_classification_evidence(
        review_id="review-1",
        company_id=7,
        review_version=1,
    )

    assert changed_current_rule_result.classification_code == "SOFTWARE_LICENSE_COST"
    assert loaded.classification_code == "CLOUD_COST"
    assert loaded.workflow is WorkflowType.VENDOR_BILL


def test_service_delegates_classification_evidence_creation_to_writer() -> None:
    item = _review_item()
    evidence = _classification_evidence()
    writer = RecordingClassificationEvidenceWriter()

    created = ReviewItemCreationService(writer).create_pending_review_item_with_classification_evidence(
        item,
        company_id=7,
        idempotency_key="review-key-1",
        classification_evidence=evidence,
    )

    assert created is item
    assert writer.classification_calls == ((item, 7, "review-key-1", evidence),)


def test_classification_reader_application_port_has_no_sqlalchemy_leak() -> None:
    source = Path("app/application/workbench/ports.py").read_text(encoding="utf-8").lower()

    assert "sqlalchemy" not in source
    assert "app.models" not in source
    assert "session" not in source
    assert "reviewclassificationevidencereader" in source


def test_classification_persistence_has_no_provider_classifier_or_writer_dependencies() -> None:
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            Path("app/persistence/workbench_review_repository.py"),
            Path("app/persistence/review_classification_evidence_reader.py"),
            Path("app/application/workbench/evidence.py"),
            Path("app/models/workbench_review_classification_evidence.py"),
        )
    ).lower()

    forbidden = (
        "app.connectors",
        "app.erp",
        "uyumsoft",
        "httpx",
        "zeep",
        ".classify(",
        "decisionrulerepository",
        "invoicedecisionruleengine",
        "vendorbillwriter",
        "customerinvoicewriter",
        "executionruntime",
        "runtimecoordinator",
        "fuzzy",
        "levenshtein",
        "openai",
        "anthropic",
        "embedding",
        "search_read",
    )
    for token in forbidden:
        assert token not in source


class RecordingClassificationEvidenceWriter:
    def __init__(self) -> None:
        self.classification_calls: tuple[tuple[ReviewItem, int, str, ReviewClassificationEvidence], ...] = ()

    def create_review_item(self, item: ReviewItem, *, company_id: int, idempotency_key: str) -> ReviewItem:
        raise AssertionError("plain review creation must not be used")

    def create_review_item_with_classification_evidence(
        self,
        item: ReviewItem,
        *,
        company_id: int,
        idempotency_key: str,
        classification_evidence: ReviewClassificationEvidence,
    ) -> ReviewItem:
        self.classification_calls = (
            *self.classification_calls,
            (item, company_id, idempotency_key, classification_evidence),
        )
        return item


def _create_classification_evidence(
    session: Session,
    *,
    evidence: ReviewClassificationEvidence | None = None,
) -> None:
    SqlAlchemyReviewRepository(session).create_review_item_with_classification_evidence(
        _review_item(),
        company_id=7,
        idempotency_key="review-key-1",
        classification_evidence=evidence or _classification_evidence(),
    )


def _classification_evidence(
    *,
    status: InvoiceClassificationStatus = InvoiceClassificationStatus.MATCHED,
    review_id: str = "review-1",
    company_id: int = 7,
    review_version: int = 1,
    workflow: WorkflowType = WorkflowType.VENDOR_BILL,
    classification_code: str = "CLOUD_COST",
    require_review: bool = False,
    require_business_context: bool = True,
) -> ReviewClassificationEvidence:
    return ReviewClassificationEvidence(
        review_id=review_id,
        company_id=company_id,
        review_version=review_version,
        status=status,
        matched_rule_id="rule-cloud",
        matched_rule_code="CLOUD_COST_VENDOR_BILL",
        matched_rule_version=3,
        matched_rule_name="Cloud cost vendor bill",
        workflow=workflow,
        classification_code=classification_code,
        require_review=require_review,
        require_business_context=require_business_context,
    )


def _no_match_evidence() -> ReviewClassificationEvidence:
    return ReviewClassificationEvidence(
        review_id="review-1",
        company_id=7,
        review_version=1,
        status=InvoiceClassificationStatus.NO_MATCH,
    )


def _conflict_evidence(
    *,
    conflicting_rules: tuple[InvoiceClassificationRuleEvidence, ...] | None = None,
) -> ReviewClassificationEvidence:
    return ReviewClassificationEvidence(
        review_id="review-1",
        company_id=7,
        review_version=1,
        status=InvoiceClassificationStatus.CONFLICT,
        conflicting_rules=conflicting_rules
        or (
            _rule_evidence(),
            _rule_evidence(
                rule_id="rule-software",
                rule_code="SOFTWARE_LICENSE_EXPENSE",
                rule_name="Software license expense",
                workflow=WorkflowType.EXPENSE,
                classification_code="SOFTWARE_LICENSE_COST",
                require_review=True,
                require_business_context=False,
            ),
        ),
    )


def _rule_evidence(
    *,
    rule_id: str = "rule-cloud",
    rule_code: str = "CLOUD_COST_VENDOR_BILL",
    rule_version: int = 3,
    rule_name: str = "Cloud cost vendor bill",
    workflow: WorkflowType = WorkflowType.VENDOR_BILL,
    classification_code: str = "CLOUD_COST",
    require_review: bool = False,
    require_business_context: bool = True,
) -> InvoiceClassificationRuleEvidence:
    return InvoiceClassificationRuleEvidence(
        rule_id=rule_id,
        rule_code=rule_code,
        rule_version=rule_version,
        rule_name=rule_name,
        workflow=workflow,
        classification_code=classification_code,
        require_review=require_review,
        require_business_context=require_business_context,
    )


def _rule() -> InvoiceDecisionRule:
    return InvoiceDecisionRule(
        rule_id="rule-cloud",
        rule_code="CLOUD_COST_VENDOR_BILL",
        rule_version=3,
        name="Cloud cost vendor bill",
        enabled=True,
        priority=InvoiceDecisionRulePriority(tier=0, rank=10),
        match=InvoiceDecisionRuleMatch(company_id=7, vendor_tax_id="1234567890"),
        action=InvoiceDecisionRuleAction(
            workflow=WorkflowType.VENDOR_BILL,
            classification_code="CLOUD_COST",
            require_business_context=True,
        ),
    )


def _seed_malformed_classification_evidence(session: Session, **overrides: object) -> None:
    session.add(
        WorkbenchReviewItem(
            review_id="review-1",
            company_id=7,
            invoice_id="ETTN-1",
            invoice_number="INV-1",
            supplier_tax_number="1234567890",
            supplier_name="Supplier Display",
            invoice_date=date(2026, 8, 1),
            currency="TRY",
            total_amount=Decimal("240.00"),
            workflow=WorkflowType.MANUAL_REVIEW.value,
            status=ReviewStatus.PENDING_REVIEW.value,
            review_reasons=[],
            warnings=[],
            version=1,
            idempotency_key="review-key-1",
        )
    )
    values = {
        "review_id": "review-1",
        "company_id": 7,
        "review_version": 1,
        "schema_version": 1,
        "status": "MATCHED",
        "matched_rule_id": "rule-cloud",
        "matched_rule_code": "CLOUD_COST_VENDOR_BILL",
        "matched_rule_version": None,
        "matched_rule_name": "Cloud cost vendor bill",
        "workflow": "vendor_bill",
        "classification_code": "CLOUD_COST",
        "require_review": False,
        "require_business_context": True,
        "conflicting_rules": [],
    }
    values.update(overrides)
    session.add(WorkbenchReviewClassificationEvidence(**values))
    session.flush()


def _review_item() -> ReviewItem:
    return ReviewItem(
        review_id="review-1",
        invoice_id="ETTN-1",
        invoice_number="INV-1",
        supplier_tax_number="1234567890",
        supplier_name="Supplier Display",
        invoice_date=date(2026, 8, 1),
        currency="TRY",
        total_amount=Decimal("240.00"),
        workflow=WorkflowType.MANUAL_REVIEW,
        status=ReviewStatus.PENDING_REVIEW,
        review_reasons=(
            ManualReviewReason(
                code=ManualReviewReasonCode.PRODUCT_NOT_FOUND,
                message="Product was not matched deterministically.",
                line_number="1",
                source="product_matching",
            ),
        ),
        warnings=("safe warning",),
        version=1,
    )


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            WorkbenchReviewItem.__table__,
            WorkbenchReviewExecutionEvidence.__table__,
            WorkbenchReviewBillingEvidence.__table__,
            WorkbenchReviewClassificationEvidence.__table__,
        ],
    )
    factory = sessionmaker(bind=engine)
    with factory() as db_session:
        yield db_session
