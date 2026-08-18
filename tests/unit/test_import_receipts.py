from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.application.commands import ImportInvoiceCommand
from app.application.dto import DecisionResult
from app.application.use_cases import ImportInvoiceUseCase
from app.application.workbench import ReviewItemCreationService
from app.application.workflow import ManualReviewReason, ManualReviewReasonCode, WorkflowType
from app.db.base import Base
from app.domain.invoice import Header, InternalInvoice, InvoiceLine, MonetaryTotals, Party
from app.models.import_receipt import ImportReceipt
from app.models.workbench_review_item import WorkbenchReviewItem
from app.persistence import SqlAlchemyImportHistory, SqlAlchemyReviewRepository


@pytest.mark.asyncio
async def test_non_review_dry_run_success_persists_receipt_and_replay_is_idempotent(session: Session) -> None:
    engine = RecordingDecisionEngine(_dry_run_decision())
    use_case = ImportInvoiceUseCase(
        import_history=SqlAlchemyImportHistory(session),
        decision_engine=engine,
    )
    command = _command(company_id=7, idempotency_key="uyumsoft:company:7:inbox:ettn:INV-ETTN")

    first = await use_case.execute(command)
    second = await use_case.execute(command)

    receipts = session.scalars(select(ImportReceipt)).all()
    assert first.status == "dry_run"
    assert second.status == "already_imported"
    assert second.invoice_id == "INV-ETTN"
    assert engine.calls == [command]
    assert len(receipts) == 1
    assert receipts[0].company_id == 7
    assert receipts[0].idempotency_key == "uyumsoft:company:7:inbox:ettn:INV-ETTN"
    assert receipts[0].status == "dry_run"


@pytest.mark.asyncio
async def test_import_receipt_idempotency_is_company_scoped(session: Session) -> None:
    use_case = ImportInvoiceUseCase(
        import_history=SqlAlchemyImportHistory(session),
        decision_engine=RecordingDecisionEngine(_dry_run_decision()),
    )

    await use_case.execute(_command(company_id=7, idempotency_key="uyumsoft:company:7:inbox:ettn:INV-ETTN"))
    await use_case.execute(_command(company_id=8, idempotency_key="uyumsoft:company:8:inbox:ettn:INV-ETTN"))

    receipts = session.scalars(select(ImportReceipt).order_by(ImportReceipt.company_id)).all()
    assert [(receipt.company_id, receipt.idempotency_key) for receipt in receipts] == [
        (7, "uyumsoft:company:7:inbox:ettn:INV-ETTN"),
        (8, "uyumsoft:company:8:inbox:ettn:INV-ETTN"),
    ]


def test_duplicate_receipt_race_returns_concurrent_import_without_unrelated_500() -> None:
    history = SqlAlchemyImportHistory(RacingReceiptSession())

    existing = history.record_import_result(
        company_id=7,
        idempotency_key="uyumsoft:company:7:inbox:ettn:INV-ETTN",
        result=_dry_run_import_result(),
    )

    assert existing.invoice_id == "INV-ETTN"
    assert existing.status == "already_imported"


@pytest.mark.asyncio
async def test_review_created_path_replays_from_workbench_without_duplicate_review_or_receipt(
    session: Session,
) -> None:
    engine = RecordingDecisionEngine(_review_required_decision())
    use_case = ImportInvoiceUseCase(
        import_history=SqlAlchemyImportHistory(session),
        decision_engine=engine,
        review_item_creation_service=ReviewItemCreationService(SqlAlchemyReviewRepository(session)),
    )
    command = _command(company_id=7, idempotency_key="uyumsoft:company:7:inbox:ettn:REVIEW-ETTN")

    first = await use_case.execute(command)
    second = await use_case.execute(command)

    reviews = session.scalars(select(WorkbenchReviewItem)).all()
    receipts = session.scalars(select(ImportReceipt)).all()
    assert first.status == "review_required"
    assert first.review_id is not None
    assert second.status == "already_imported"
    assert second.invoice_id == "INV-ETTN"
    assert len(reviews) == 1
    assert receipts == []
    assert len(engine.calls) == 1


class RecordingDecisionEngine:
    def __init__(self, result: DecisionResult) -> None:
        self.result = result
        self.calls: list[ImportInvoiceCommand] = []

    async def decide(self, command: ImportInvoiceCommand) -> DecisionResult:
        self.calls.append(command)
        return self.result


def _command(*, company_id: int, idempotency_key: str) -> ImportInvoiceCommand:
    return ImportInvoiceCommand(
        invoice=_invoice(),
        idempotency_key=idempotency_key,
        company_id=company_id,
        dry_run=True,
    )


def _invoice() -> InternalInvoice:
    return InternalInvoice(
        header=Header(
            invoice_number="INV-1",
            invoice_uuid="INV-UUID",
            ettn="INV-ETTN",
            issue_date=date(2026, 8, 18),
            currency_code="TRY",
            invoice_type="E_INVOICE",
        ),
        supplier=Party(name="Supplier", tax_number="1111111111"),
        customer=Party(name="Customer", tax_number="2222222222"),
        totals=MonetaryTotals(payable_amount=Decimal("120")),
        lines=(InvoiceLine(line_number="1", description="Managed service"),),
    )


def _dry_run_decision() -> DecisionResult:
    return DecisionResult(
        success=True,
        invoice_id="INV-ETTN",
        workflow=WorkflowType.VENDOR_BILL,
        strategy=WorkflowType.VENDOR_BILL.value,
        status="dry_run",
    )


def _dry_run_import_result():
    from app.application.dto import ImportInvoiceResult

    return ImportInvoiceResult(success=True, invoice_id="INV-ETTN", status="dry_run")


def _review_required_decision() -> DecisionResult:
    return DecisionResult(
        success=False,
        invoice_id="INV-ETTN",
        workflow=WorkflowType.MANUAL_REVIEW,
        strategy=WorkflowType.MANUAL_REVIEW.value,
        status="review_required",
        review_required=True,
        review_reasons=(
            ManualReviewReason(
                code=ManualReviewReasonCode.SUPPLIER_NOT_FOUND,
                message="Supplier was not matched deterministically.",
                source="partner_matching",
            ),
        ),
    )


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    db = factory()
    try:
        yield db
    finally:
        db.close()


class RacingReceiptSession:
    def __init__(self) -> None:
        self.scalar_calls = 0
        self.receipt = ImportReceipt(
            company_id=7,
            idempotency_key="uyumsoft:company:7:inbox:ettn:INV-ETTN",
            invoice_id="INV-ETTN",
            status="dry_run",
            vendor_bill_id=None,
            review_id=None,
        )

    def scalar(self, statement):
        self.scalar_calls += 1
        return None if self.scalar_calls == 1 else self.receipt

    def begin_nested(self):
        return self

    def add(self, record) -> None:
        pass

    def flush(self) -> None:
        raise IntegrityError("insert", {}, Exception("duplicate"))

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        return False
