"""ONE_OFF_VENDOR archive-last crash/retry contract (P0-PROD-08H).

Scenarios I-R from the P0-PROD-08H specification:
  I. Hub-owned partner is archive-eligible
  J. non-Hub-owned partner is not archive-eligible (proven in
     test_supplier_remediation_orchestration.py::test_h_... -- ownership is decided
     at *resolution* time, never at archive time; there is structurally no way to
     reach this use case for a partner without a retirement row)
  K. no Vendor Bill evidence -> no archive
  L. durable Vendor Bill evidence -> archive allowed
  M. already archived -> idempotent success
  N. archive success lost -> retry/read-back succeeds
  O. archive write with a certain no-write outcome -> reverts, safe to retry
  P/Q. crash recovery resumes correctly from every persisted state
  R. full completed replay -> zero new remote writes
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.application.commands.one_off_vendor_retirement import ArchiveOneOffVendorPartnerCommand
from app.application.dto.one_off_vendor_retirement import OneOffVendorArchiveWriteResult, OneOffVendorArchiveWriteStatus
from app.application.exceptions.supplier_partner import (
    SupplierPartnerWriteSafetyGateError,
    SupplierPartnerWriteTransportError,
)
from app.application.workbench.exceptions import OneOffVendorRetirementDataIntegrityError
from app.application.workbench.one_off_vendor_retirement import (
    ArchiveOneOffVendorCommand,
    ArchiveOneOffVendorStatus,
    OneOffVendorRetirement,
    OneOffVendorRetirementStatus,
)
from app.application.workbench.one_off_vendor_use_cases import ArchiveOneOffVendorUseCase
from app.db.base import Base
from app.models.workbench_review_item import WorkbenchReviewItem
from app.models.workbench_review_one_off_vendor_retirement import WorkbenchReviewOneOffVendorRetirement
from app.persistence import SqlAlchemyReviewOneOffVendorRetirementRepository, SqlAlchemyUnitOfWork

COMPANY_ID = 7
REVIEW_ID = "review:one-off-archive-1"
PARTNER_ID = 6001


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[WorkbenchReviewItem.__table__, WorkbenchReviewOneOffVendorRetirement.__table__],
    )
    factory = sessionmaker(bind=engine)
    with factory() as db_session:
        from datetime import date
        from decimal import Decimal

        db_session.add(
            WorkbenchReviewItem(
                review_id=REVIEW_ID,
                company_id=COMPANY_ID,
                invoice_id="dmarket-ettn",
                invoice_number="HD1",
                supplier_tax_number="2650179910",
                supplier_name="D-Market",
                invoice_date=date(2026, 9, 10),
                currency="TRY",
                total_amount=Decimal("676.21"),
                workflow="vendor_bill",
                status="decision_submitted",
                review_reasons=[],
                warnings=[],
                version=2,
                idempotency_key="uyumsoft:7:dmarket-ettn",
            )
        )
        db_session.flush()
        yield db_session


class _FakeVendorBillEvidenceReader:
    def __init__(self, *, has_evidence: bool) -> None:
        self.has_evidence = has_evidence
        self.calls = 0

    def has_successful_vendor_bill(self, *, review_id: str, company_id: int) -> bool:
        self.calls += 1
        return self.has_evidence


class _FakeRetirementPort:
    def __init__(self) -> None:
        self.calls: list[ArchiveOneOffVendorPartnerCommand] = []
        self.raise_error: Exception | None = None
        self.result_status = OneOffVendorArchiveWriteStatus.ARCHIVED

    async def archive_partner(self, command: ArchiveOneOffVendorPartnerCommand) -> OneOffVendorArchiveWriteResult:
        self.calls.append(command)
        if self.raise_error is not None:
            raise self.raise_error
        return OneOffVendorArchiveWriteResult(
            status=self.result_status, partner_id=command.partner_id, safe_message="ok"
        )


def _use_case(session: Session, *, evidence_reader, port) -> ArchiveOneOffVendorUseCase:
    return ArchiveOneOffVendorUseCase(
        retirement_writer=SqlAlchemyReviewOneOffVendorRetirementRepository(session),
        vendor_bill_evidence_reader=evidence_reader,
        retirement_port=port,
        unit_of_work=SqlAlchemyUnitOfWork(session),
        approved_by="finance.operator",
    )


def _seed_retirement(session: Session, *, status: OneOffVendorRetirementStatus) -> None:
    SqlAlchemyReviewOneOffVendorRetirementRepository(session).create_retirement(
        OneOffVendorRetirement(
            review_id=REVIEW_ID,
            company_id=COMPANY_ID,
            review_version=2,
            resolved_partner_id=PARTNER_ID,
            status=OneOffVendorRetirementStatus.PENDING_VENDOR_BILL,
        )
    )
    if status is not OneOffVendorRetirementStatus.PENDING_VENDOR_BILL:
        repo = SqlAlchemyReviewOneOffVendorRetirementRepository(session)
        row = repo.find(review_id=REVIEW_ID, company_id=COMPANY_ID, review_version=2)
        repo.advance(row, expected_status=OneOffVendorRetirementStatus.PENDING_VENDOR_BILL, new_status=status)
    session.commit()


def _command() -> ArchiveOneOffVendorCommand:
    return ArchiveOneOffVendorCommand(review_id=REVIEW_ID, company_id=COMPANY_ID, review_version=2)


# --------------------------------------------------------------------------- K/L


async def test_k_no_vendor_bill_evidence_no_archive(session: Session) -> None:
    _seed_retirement(session, status=OneOffVendorRetirementStatus.PENDING_VENDOR_BILL)
    evidence_reader = _FakeVendorBillEvidenceReader(has_evidence=False)
    port = _FakeRetirementPort()
    result = await _use_case(session, evidence_reader=evidence_reader, port=port).execute(_command())

    assert result.status is ArchiveOneOffVendorStatus.AWAITING_VENDOR_BILL
    assert port.calls == []
    row = SqlAlchemyReviewOneOffVendorRetirementRepository(session).find(
        review_id=REVIEW_ID, company_id=COMPANY_ID, review_version=2
    )
    assert row.status is OneOffVendorRetirementStatus.PENDING_VENDOR_BILL


async def test_l_durable_vendor_bill_evidence_allows_archive(session: Session) -> None:
    _seed_retirement(session, status=OneOffVendorRetirementStatus.PENDING_VENDOR_BILL)
    evidence_reader = _FakeVendorBillEvidenceReader(has_evidence=True)
    port = _FakeRetirementPort()
    result = await _use_case(session, evidence_reader=evidence_reader, port=port).execute(_command())

    assert result.status is ArchiveOneOffVendorStatus.ARCHIVED
    assert result.resolved_partner_id == PARTNER_ID
    assert len(port.calls) == 1
    assert port.calls[0].partner_id == PARTNER_ID
    row = SqlAlchemyReviewOneOffVendorRetirementRepository(session).find(
        review_id=REVIEW_ID, company_id=COMPANY_ID, review_version=2
    )
    assert row.status is OneOffVendorRetirementStatus.ARCHIVED


# --------------------------------------------------------------------------- I/M


async def test_i_hub_owned_partner_is_archive_eligible(session: Session) -> None:
    """The retirement row's mere existence, keyed by (review, company, version),
    IS the durable proof of Hub ownership this use case relies on -- see
    test_h_existing_permanent_partner_is_never_adopted_as_one_off for the
    resolution-time check that ensures a row only ever exists for a Hub-created
    or Hub-owned-and-reused partner."""

    _seed_retirement(session, status=OneOffVendorRetirementStatus.PENDING_VENDOR_BILL)
    result = await _use_case(
        session,
        evidence_reader=_FakeVendorBillEvidenceReader(has_evidence=True),
        port=_FakeRetirementPort(),
    ).execute(_command())
    assert result.status is ArchiveOneOffVendorStatus.ARCHIVED


async def test_m_already_archived_is_idempotent_success(session: Session) -> None:
    _seed_retirement(session, status=OneOffVendorRetirementStatus.ARCHIVED)
    port = _FakeRetirementPort()
    result = await _use_case(
        session, evidence_reader=_FakeVendorBillEvidenceReader(has_evidence=True), port=port
    ).execute(_command())

    assert result.status is ArchiveOneOffVendorStatus.ARCHIVED
    assert result.already_applied is True
    assert port.calls == []  # no remote call at all -- pure DB read


# --------------------------------------------------------------------------- N/O


async def test_n_archive_success_lost_resume_readback_succeeds(session: Session) -> None:
    """ARCHIVE_ATTEMPTED (crash before the outcome was persisted) resumes by calling
    the port again; the port's own read-before-write makes this safe even though the
    prior remote write may have already succeeded (ALREADY_ARCHIVED)."""

    _seed_retirement(session, status=OneOffVendorRetirementStatus.ARCHIVE_ATTEMPTED)
    port = _FakeRetirementPort()
    port.result_status = OneOffVendorArchiveWriteStatus.ALREADY_ARCHIVED
    result = await _use_case(
        session, evidence_reader=_FakeVendorBillEvidenceReader(has_evidence=True), port=port
    ).execute(_command())

    assert result.status is ArchiveOneOffVendorStatus.ARCHIVED
    assert len(port.calls) == 1
    row = SqlAlchemyReviewOneOffVendorRetirementRepository(session).find(
        review_id=REVIEW_ID, company_id=COMPANY_ID, review_version=2
    )
    assert row.status is OneOffVendorRetirementStatus.ARCHIVED


async def test_o_certain_no_write_failure_reverts_to_pending(session: Session) -> None:
    """A gate-not-enabled failure is CERTAIN to mean no Odoo call happened at all --
    safe to revert PENDING_VENDOR_BILL rather than leave ARCHIVE_ATTEMPTED."""

    _seed_retirement(session, status=OneOffVendorRetirementStatus.PENDING_VENDOR_BILL)
    port = _FakeRetirementPort()
    port.raise_error = SupplierPartnerWriteSafetyGateError("gate disabled")
    with pytest.raises(SupplierPartnerWriteSafetyGateError):
        await _use_case(session, evidence_reader=_FakeVendorBillEvidenceReader(has_evidence=True), port=port).execute(
            _command()
        )

    row = SqlAlchemyReviewOneOffVendorRetirementRepository(session).find(
        review_id=REVIEW_ID, company_id=COMPANY_ID, review_version=2
    )
    assert row.status is OneOffVendorRetirementStatus.PENDING_VENDOR_BILL


async def test_uncertain_failure_leaves_archive_attempted_for_resume(session: Session) -> None:
    _seed_retirement(session, status=OneOffVendorRetirementStatus.PENDING_VENDOR_BILL)
    port = _FakeRetirementPort()
    port.raise_error = SupplierPartnerWriteTransportError("timeout")
    with pytest.raises(SupplierPartnerWriteTransportError):
        await _use_case(session, evidence_reader=_FakeVendorBillEvidenceReader(has_evidence=True), port=port).execute(
            _command()
        )

    row = SqlAlchemyReviewOneOffVendorRetirementRepository(session).find(
        review_id=REVIEW_ID, company_id=COMPANY_ID, review_version=2
    )
    assert row.status is OneOffVendorRetirementStatus.NEEDS_RECONCILIATION


# --------------------------------------------------------------------------- P/Q


async def test_p_resume_from_archive_attempted_completes(session: Session) -> None:
    _seed_retirement(session, status=OneOffVendorRetirementStatus.ARCHIVE_ATTEMPTED)
    result = await _use_case(
        session,
        evidence_reader=_FakeVendorBillEvidenceReader(has_evidence=True),
        port=_FakeRetirementPort(),
    ).execute(_command())
    assert result.status is ArchiveOneOffVendorStatus.ARCHIVED


async def test_q_resume_from_needs_reconciliation_completes(session: Session) -> None:
    _seed_retirement(session, status=OneOffVendorRetirementStatus.NEEDS_RECONCILIATION)
    result = await _use_case(
        session,
        evidence_reader=_FakeVendorBillEvidenceReader(has_evidence=True),
        port=_FakeRetirementPort(),
    ).execute(_command())
    assert result.status is ArchiveOneOffVendorStatus.ARCHIVED


async def test_no_retirement_row_fails_closed(session: Session) -> None:
    with pytest.raises(OneOffVendorRetirementDataIntegrityError):
        await _use_case(
            session,
            evidence_reader=_FakeVendorBillEvidenceReader(has_evidence=True),
            port=_FakeRetirementPort(),
        ).execute(_command())


# --------------------------------------------------------------------------- R


async def test_r_full_replay_after_completion_makes_zero_new_remote_calls(session: Session) -> None:
    _seed_retirement(session, status=OneOffVendorRetirementStatus.PENDING_VENDOR_BILL)
    evidence_reader = _FakeVendorBillEvidenceReader(has_evidence=True)
    port = _FakeRetirementPort()
    use_case = _use_case(session, evidence_reader=evidence_reader, port=port)

    first = await use_case.execute(_command())
    second = await use_case.execute(_command())
    third = await use_case.execute(_command())

    assert first.status is second.status is third.status is ArchiveOneOffVendorStatus.ARCHIVED
    assert len(port.calls) == 1  # only the first call ever reached the port


# --------------------------------------------------------------------------- S/T/U/V/W


def test_t_no_operating_expense_mapping_write_anywhere_in_one_off_vendor_code() -> None:
    from pathlib import Path

    sources = "".join(
        Path(p).read_text(encoding="utf-8")
        for p in (
            "app/application/workbench/one_off_vendor_retirement.py",
            "app/application/workbench/one_off_vendor_use_cases.py",
            "app/erp/write/odoo_one_off_vendor_retirement_writer.py",
        )
    )
    assert "OperatingExpenseMappingRecord" not in sources
    assert "OperatingExpenseMatchingEngine" not in sources


def test_u_v_no_product_or_supplierinfo_write_anywhere_in_one_off_vendor_code() -> None:
    from pathlib import Path

    sources = "".join(
        Path(p).read_text(encoding="utf-8")
        for p in (
            "app/application/workbench/one_off_vendor_retirement.py",
            "app/application/workbench/one_off_vendor_use_cases.py",
            "app/erp/write/odoo_one_off_vendor_retirement_writer.py",
        )
    )
    assert "product.template" not in sources
    assert "product.supplierinfo" not in sources
    assert "OdooProductWriter" not in sources
    assert "OdooSupplierInfoWriter" not in sources


def test_s_expense_account_builder_and_execution_untouched_by_this_pr() -> None:
    """P0-PROD-08G's per-line explicit expense account machinery -- builder.py and
    vendor_bill_strategy.py -- carries no ONE_OFF_VENDOR-specific code; the two
    features compose without either needing to know about the other."""

    from pathlib import Path

    builder_source = Path("app/billing/builder.py").read_text(encoding="utf-8")
    strategy_source = Path("app/application/execution/vendor_bill_strategy.py").read_text(encoding="utf-8")
    for source in (builder_source, strategy_source):
        assert "one_off_vendor" not in source.lower()
        assert "OneOffVendor" not in source
    # The 08G contract itself is still present and unchanged in shape.
    assert "explicit_account_only_accounts" in builder_source
    assert "explicit_account_only_accounts" in strategy_source
