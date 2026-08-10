from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.application.commands import VendorBillWriteCommand
from app.application.dto import VendorBillWriteResult
from app.application.execution import (
    ExecutionApproval,
    ExecutionMode,
    ExecutionSourceInvoice,
    ExecutionSourceInvoiceIntegrityError,
    ExecutionSourceInvoiceNotFoundError,
    ExecutionStep,
    ExecutionStepRequest,
    ExecutionStepStatus,
    ExecutionStepType,
    VendorBillExecutionStrategy,
)
from app.application.workbench import ReviewDecisionType, ReviewStatus
from app.application.workflow import WorkflowType
from app.billing import VendorBillBuilder
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
from app.models.workbench_review_decision import WorkbenchReviewDecision
from app.models.workbench_review_item import WorkbenchReviewItem
from app.persistence.execution_source_invoice_reader import (
    SqlAlchemyExecutionSourceInvoiceReader,
    serialize_execution_source_invoice,
)
from app.tax_mapping import InvoiceTaxLineResult, InvoiceTaxMappingResult, TaxMatchResult, TaxMatchStatus, TaxType


def test_exact_review_company_version_returns_source_evidence(session: Session) -> None:
    _seed_decision_and_evidence(session)

    source = SqlAlchemyExecutionSourceInvoiceReader(session).get_source_invoice(
        review_id="review-1",
        company_id=7,
        decision_version=4,
    )

    assert source.review_id == "review-1"
    assert source.company_id == 7
    assert source.decision_version == 4
    assert source.source_invoice_id == "ETTN-4"
    assert source.invoice.header.invoice_uuid == "ETTN-4"


def test_wrong_company_is_rejected_without_cross_company_details(session: Session) -> None:
    _seed_decision_and_evidence(session, company_id=8)

    with pytest.raises(ExecutionSourceInvoiceNotFoundError) as exc_info:
        SqlAlchemyExecutionSourceInvoiceReader(session).get_source_invoice(
            review_id="review-1",
            company_id=7,
            decision_version=4,
        )

    assert "company 8" not in str(exc_info.value).lower()
    assert "ETTN-4" not in str(exc_info.value)


def test_wrong_version_is_rejected(session: Session) -> None:
    _seed_decision_and_evidence(session, decision_version=4)

    with pytest.raises(ExecutionSourceInvoiceNotFoundError):
        SqlAlchemyExecutionSourceInvoiceReader(session).get_source_invoice(
            review_id="review-1",
            company_id=7,
            decision_version=3,
        )


def test_later_review_version_does_not_affect_earlier_source(session: Session) -> None:
    _seed_decision_and_evidence(session, decision_version=4, source_invoice_id="ETTN-4")
    _seed_decision_and_evidence(session, decision_version=5, decision_id="decision-5", source_invoice_id="ETTN-5")

    source = SqlAlchemyExecutionSourceInvoiceReader(session).get_source_invoice(
        review_id="review-1",
        company_id=7,
        decision_version=4,
    )

    assert source.source_invoice_id == "ETTN-4"
    assert source.invoice.header.invoice_number == "INV-4"


def test_internal_invoice_reconstructed_immutably(session: Session) -> None:
    _seed_decision_and_evidence(session)

    invoice = (
        SqlAlchemyExecutionSourceInvoiceReader(session)
        .get_source_invoice(
            review_id="review-1",
            company_id=7,
            decision_version=4,
        )
        .invoice
    )

    assert isinstance(invoice, InternalInvoice)
    assert invoice.header.invoice_number == "INV-4"
    assert invoice.lines[0].quantity == Decimal("2.00")
    assert invoice.lines[0].taxes[0].rate == Decimal("20")
    assert not hasattr(invoice, "__dict__")
    with pytest.raises(FrozenInstanceError):
        invoice.header = Header(invoice_number="other", invoice_uuid="other")  # type: ignore[misc]


def test_supplier_match_evidence_reconstructed(session: Session) -> None:
    _seed_decision_and_evidence(session)

    partner_match = _load_source(session).partner_match

    assert isinstance(partner_match, PartnerMatchResult)
    assert partner_match.status is PartnerMatchStatus.MATCHED
    assert partner_match.partner_id == 501
    assert partner_match.matched_by == "tax_number"
    assert partner_match.confidence == Decimal("1.00")


def test_product_match_evidence_reconstructed(session: Session) -> None:
    _seed_decision_and_evidence(session)

    product_match = _load_source(session).product_match

    assert isinstance(product_match, InvoiceProductMatchResult)
    assert product_match.line_results[0].line_number == "1"
    assert product_match.line_results[0].result.status is ProductMatchStatus.MATCHED
    assert product_match.line_results[0].result.product_id == 701
    assert product_match.line_results[0].result.default_code == "P-001"


def test_tax_mapping_evidence_reconstructed(session: Session) -> None:
    _seed_decision_and_evidence(session)

    tax_match = _load_source(session).tax_match

    assert isinstance(tax_match, InvoiceTaxMappingResult)
    assert tax_match.line_results[0].line_number == "1"
    assert tax_match.line_results[0].tax_index == 0
    assert tax_match.line_results[0].result.status is TaxMatchStatus.MATCHED
    assert tax_match.line_results[0].result.tax_id == 801
    assert tax_match.line_results[0].result.tax_rate == Decimal("20")


def test_adapter_has_no_external_provider_call() -> None:
    source = Path("app/persistence/execution_source_invoice_reader.py").read_text(encoding="utf-8")

    assert "connectors" not in source
    assert "Uyumsoft" not in source


def test_adapter_has_no_odoo_call() -> None:
    source = Path("app/persistence/execution_source_invoice_reader.py").read_text(encoding="utf-8")

    assert "Odoo" not in source
    assert "search_read" not in source


def test_adapter_does_not_rerun_matching_or_mapping() -> None:
    source = Path("app/persistence/execution_source_invoice_reader.py").read_text(encoding="utf-8")

    assert ".match_invoice(" not in source
    assert ".map_invoice(" not in source


def test_adapter_has_no_fuzzy_matching() -> None:
    source = Path("app/persistence/execution_source_invoice_reader.py").read_text(encoding="utf-8")

    assert "fuzzy" not in source.lower()
    assert "levenshtein" not in source.lower()


def test_missing_invoice_evidence_fails_closed(session: Session) -> None:
    _seed_decision(session)

    with pytest.raises(ExecutionSourceInvoiceNotFoundError):
        _load_source(session)


def test_missing_supplier_evidence_fails_closed(session: Session) -> None:
    _seed_decision_and_evidence(session, overrides={"partner_match": {}})

    with pytest.raises(ExecutionSourceInvoiceIntegrityError):
        _load_source(session)


def test_missing_product_evidence_fails_closed(session: Session) -> None:
    _seed_decision_and_evidence(session, overrides={"product_match": {}})

    with pytest.raises(ExecutionSourceInvoiceIntegrityError):
        _load_source(session)


def test_missing_tax_evidence_fails_closed(session: Session) -> None:
    _seed_decision_and_evidence(session, overrides={"tax_match": {}})

    with pytest.raises(ExecutionSourceInvoiceIntegrityError):
        _load_source(session)


def test_malformed_persisted_invoice_data_fails_safely(session: Session) -> None:
    _seed_decision_and_evidence(session, overrides={"invoice": "<Invoice><Secret>raw</Secret></Invoice>"})

    with pytest.raises(ExecutionSourceInvoiceIntegrityError) as exc_info:
        _load_source(session)

    assert "raw" not in str(exc_info.value).lower()
    assert "xml" not in str(exc_info.value).lower()
    assert "secret" not in str(exc_info.value).lower()


def test_malformed_match_evidence_fails_safely(session: Session) -> None:
    _seed_decision_and_evidence(session, overrides={"product_match": {"raw": {"token": "secret-token"}}})

    with pytest.raises(ExecutionSourceInvoiceIntegrityError) as exc_info:
        _load_source(session)

    assert "secret-token" not in str(exc_info.value)
    assert "raw" not in str(exc_info.value).lower()


def test_duplicate_decision_evidence_is_prevented_by_unique_constraint(session: Session) -> None:
    _seed_decision_and_evidence(session)

    with pytest.raises(IntegrityError):
        _seed_evidence(session)


def test_unsupported_schema_version_is_rejected_safely(session: Session) -> None:
    _seed_decision_and_evidence(session)
    evidence = session.scalar(select(ExecutionSourceInvoiceEvidence))
    assert evidence is not None
    evidence.schema_version = 999
    session.flush()

    with pytest.raises(ExecutionSourceInvoiceIntegrityError) as exc_info:
        _load_source(session)

    assert str(exc_info.value) == "Execution source invoice evidence is invalid."


def test_company_isolation_is_enforced_in_evidence_query(session: Session) -> None:
    _seed_decision(session)
    _seed_evidence(session, company_id=8)

    with pytest.raises(ExecutionSourceInvoiceNotFoundError):
        _load_source(session)


def test_review_item_source_invoice_linkage_is_enforced(session: Session) -> None:
    _seed_decision_and_evidence(session)
    review_item = session.scalar(select(WorkbenchReviewItem).where(WorkbenchReviewItem.review_id == "review-1"))
    assert review_item is not None
    review_item.invoice_id = "OTHER-ETTN"
    session.flush()

    with pytest.raises(ExecutionSourceInvoiceIntegrityError):
        _load_source(session)


def test_sqlalchemy_models_do_not_leak_into_application_dtos(session: Session) -> None:
    _seed_decision_and_evidence(session)

    source = _load_source(session)

    assert isinstance(source, ExecutionSourceInvoice)
    assert not isinstance(source, ExecutionSourceInvoiceEvidence)
    assert not isinstance(source.invoice, ExecutionSourceInvoiceEvidence)
    assert not hasattr(source, "__dict__")


def test_raw_json_not_leaked_in_public_errors(session: Session) -> None:
    _seed_decision_and_evidence(session, overrides={"partner_match": {"password": "super-secret"}})

    with pytest.raises(ExecutionSourceInvoiceIntegrityError) as exc_info:
        _load_source(session)

    assert "password" not in str(exc_info.value).lower()
    assert "super-secret" not in str(exc_info.value)


def test_vendor_bill_execution_strategy_can_consume_returned_source(session: Session) -> None:
    _seed_decision_and_evidence(session)
    writer = RecordingVendorBillWriter()
    strategy = VendorBillExecutionStrategy(
        source_invoice_reader=SqlAlchemyExecutionSourceInvoiceReader(session),
        vendor_bill_builder=VendorBillBuilder(),
        vendor_bill_writer=writer,
    )

    result = strategy.execute(_step_request())

    assert result.status is ExecutionStepStatus.EXECUTED
    assert writer.calls == 1
    assert writer.commands[0].vendor_bill.supplier_id == 501
    assert writer.commands[0].vendor_bill.invoice_lines[0].product_id == 701


def _load_source(session: Session) -> ExecutionSourceInvoice:
    return SqlAlchemyExecutionSourceInvoiceReader(session).get_source_invoice(
        review_id="review-1",
        company_id=7,
        decision_version=4,
    )


def _seed_decision_and_evidence(
    session: Session,
    *,
    company_id: int = 7,
    decision_version: int = 4,
    decision_id: str = "decision-4",
    source_invoice_id: str = "ETTN-4",
    overrides: dict[str, object] | None = None,
) -> None:
    _seed_decision(
        session,
        company_id=company_id,
        decision_version=decision_version,
        decision_id=decision_id,
        source_invoice_id=source_invoice_id,
    )
    _seed_evidence(
        session,
        company_id=company_id,
        decision_version=decision_version,
        decision_id=decision_id,
        source_invoice_id=source_invoice_id,
        overrides=overrides,
    )


def _seed_decision(
    session: Session,
    *,
    company_id: int = 7,
    decision_version: int = 4,
    decision_id: str = "decision-4",
    source_invoice_id: str = "ETTN-4",
) -> None:
    existing_item = session.scalar(select(WorkbenchReviewItem).where(WorkbenchReviewItem.review_id == "review-1"))
    if existing_item is None:
        session.add(
            WorkbenchReviewItem(
                review_id="review-1",
                company_id=company_id,
                invoice_id=source_invoice_id,
                invoice_number=f"INV-{decision_version}",
                supplier_tax_number="1234567890",
                supplier_name="Supplier",
                invoice_date=date(2026, 8, 1),
                currency="TRY",
                total_amount=Decimal("240.00"),
                workflow=WorkflowType.VENDOR_BILL.value,
                status=ReviewStatus.DECISION_SUBMITTED.value,
                review_reasons=[],
                warnings=[],
                version=decision_version,
                idempotency_key=f"review:{company_id}:{decision_version}",
            )
        )
    else:
        existing_item.version = max(existing_item.version, decision_version)
    session.add(
        WorkbenchReviewDecision(
            decision_id=decision_id,
            review_id="review-1",
            company_id=company_id,
            review_version_before=decision_version - 1,
            review_version_after=decision_version,
            decision_type=ReviewDecisionType.SELECT_WORKFLOW.value,
            selected_workflow=WorkflowType.VENDOR_BILL.value,
            selected_partner_id=501,
            line_resolutions=[{"line_number": "1", "selected_product_id": 701}],
            tax_resolutions=[{"line_number": "1", "tax_index": 0, "selected_tax_id": 801}],
            business_context=None,
            business_context_allocations=None,
            comment=None,
            decided_by="finance.user",
            idempotency_key=f"decision:{company_id}:{decision_version}",
        )
    )
    session.flush()


def _seed_evidence(
    session: Session,
    *,
    company_id: int = 7,
    decision_version: int = 4,
    decision_id: str = "decision-4",
    source_invoice_id: str = "ETTN-4",
    overrides: dict[str, object] | None = None,
) -> None:
    values = serialize_execution_source_invoice(
        _source(company_id=company_id, decision_version=decision_version, source_invoice_id=source_invoice_id),
        decision_id=decision_id,
    )
    values.update(overrides or {})
    session.add(ExecutionSourceInvoiceEvidence(**values))
    session.flush()


def _source(
    *,
    company_id: int = 7,
    decision_version: int = 4,
    source_invoice_id: str = "ETTN-4",
) -> ExecutionSourceInvoice:
    invoice = InternalInvoice(
        header=Header(
            invoice_number=f"INV-{decision_version}",
            invoice_uuid=source_invoice_id,
            ettn=source_invoice_id,
            issue_date=date(2026, 8, 1),
            currency_code="TRY",
            notes=("safe note",),
        ),
        supplier=Party(name="Supplier", tax_number="1234567890"),
        customer=Party(name="Customer", tax_number="0987654321"),
        totals=MonetaryTotals(
            line_extension_amount=Decimal("200.00"),
            tax_exclusive_amount=Decimal("200.00"),
            tax_inclusive_amount=Decimal("240.00"),
            payable_amount=Decimal("240.00"),
        ),
        lines=(
            InvoiceLine(
                line_number="1",
                description="Service",
                seller_item_code="SELL-1",
                buyer_item_code="P-001",
                barcode="BAR-1",
                quantity=Decimal("2.00"),
                unit_code="EA",
                unit_price=Decimal("100.00"),
                line_extension_amount=Decimal("200.00"),
                taxes=(
                    Tax(
                        tax_type="VAT",
                        rate=Decimal("20"),
                        base_amount=Decimal("200.00"),
                        tax_amount=Decimal("40.00"),
                    ),
                ),
            ),
        ),
    )
    return ExecutionSourceInvoice(
        review_id="review-1",
        company_id=company_id,
        decision_version=decision_version,
        source_invoice_id=source_invoice_id,
        invoice=invoice,
        partner_match=PartnerMatchResult(
            status=PartnerMatchStatus.MATCHED,
            partner_id=501,
            matched_by="tax_number",
            reason="Matched by supplier tax number.",
            candidate_count=1,
            confidence=Decimal("1.00"),
        ),
        product_match=InvoiceProductMatchResult(
            line_results=(
                InvoiceProductLineResult(
                    line_number="1",
                    result=ProductMatchResult(
                        status=ProductMatchStatus.MATCHED,
                        line_number="1",
                        product_id=701,
                        default_code="P-001",
                        barcode="BAR-1",
                        seller_item_code="SELL-1",
                        matched_by="default_code",
                        reason="Matched by default_code.",
                        candidate_count=1,
                        confidence=Decimal("1.00"),
                    ),
                ),
            )
        ),
        tax_match=InvoiceTaxMappingResult(
            line_results=(
                InvoiceTaxLineResult(
                    line_number="1",
                    tax_index=0,
                    result=TaxMatchResult(
                        status=TaxMatchStatus.MATCHED,
                        tax_id=801,
                        company_id=company_id,
                        tax_type=TaxType.VAT,
                        tax_rate=Decimal("20"),
                        matched_by="rate",
                        confidence=Decimal("1.00"),
                        reason="Matched by VAT rate.",
                        candidate_count=1,
                    ),
                ),
            )
        ),
    )


def _step_request() -> ExecutionStepRequest:
    return ExecutionStepRequest(
        execution_id="execution-1",
        review_id="review-1",
        company_id=7,
        decision_version=4,
        mode=ExecutionMode.EXECUTE,
        step=ExecutionStep(
            step_key="review-1:4:vendor_bill:workflow",
            step_type=ExecutionStepType.VENDOR_BILL,
            allocation_keys=(),
            sequence=1,
            execute_supported=True,
        ),
        approval=ExecutionApproval(approved_by="finance.lead"),
    )


class RecordingVendorBillWriter:
    def __init__(self) -> None:
        self.calls = 0
        self.commands: tuple[VendorBillWriteCommand, ...] = ()

    async def write_vendor_bill(self, command: VendorBillWriteCommand) -> VendorBillWriteResult:
        self.calls += 1
        self.commands = (*self.commands, command)
        return VendorBillWriteResult(status="created", idempotency_key=command.idempotency_key, external_id=9001)


def _engine():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            WorkbenchReviewItem.__table__,
            WorkbenchReviewDecision.__table__,
            ExecutionSourceInvoiceEvidence.__table__,
        ],
    )
    return engine


@pytest.fixture()
def session() -> Session:
    factory = sessionmaker(bind=_engine())
    with factory() as db_session:
        yield db_session
