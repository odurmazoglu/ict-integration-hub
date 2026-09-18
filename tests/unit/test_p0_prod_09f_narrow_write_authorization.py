"""P0-PROD-09F: narrow runtime write authorization for CREATE_PERMANENT_SUPPLIER,
ONE_OFF_VENDOR supplier creation/reuse, and ONE_OFF_VENDOR archive/recovery.

Reuses the exact P0-PROD-09D1 WriteAuthorization state machine (TTL, single-use,
audit, row-locking, replay semantics) unchanged -- extended only with three new
``WriteAuthorizationOperationType`` members and operation-aware target-version
validation (current review version for supplier ops; the retirement row's own,
generally older, version for ONE_OFF_VENDOR_ARCHIVE).

Three layers of coverage:
  A. Application-layer: real ResolveWorkbenchSupplierUseCase / ArchiveOneOffVendorUseCase
     / CreateWriteAuthorizationUseCase / SqlAlchemyWriteAuthorizationRepository, real
     OdooSupplierPartnerWriter, fake Odoo JSON-2 boundary and fake review/source
     readers -- proves the full issue -> claim -> consume -> scope-check chain for
     every new operation type, every failure mode, and that the master kill switch
     stays absolute.
  B. Repository-level replay/concurrency: claim_and_consume's own row-locked
     single-use/idempotent-resume semantics, exercised directly against the new
     operation types (the same shared code path EXECUTE_VENDOR_BILL already proves;
     this confirms it is not accidentally bypassed for the new types).
  C. HTTP-level wiring smoke test: the new authorization_id field reaches the
     command on both endpoints, using a fake use-case dependency override (no DB).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.api import dependencies
from app.api.routers.workbench import router
from app.api.security import AuthenticationMethod, Permission, RequestContext
from app.application.exceptions.supplier_partner import (
    SupplierPartnerInactiveError,
    SupplierPartnerWriteSafetyGateError,
)
from app.application.workbench.dto import ReviewItem, ReviewStatus
from app.application.workbench.evidence import ReviewSourceInvoiceEvidence
from app.application.workbench.exceptions import SupplierResolutionContractError
from app.application.workbench.one_off_vendor_retirement import (
    ArchiveOneOffVendorCommand,
    ArchiveOneOffVendorStatus,
    OneOffVendorRetirement,
    OneOffVendorRetirementStatus,
)
from app.application.workbench.one_off_vendor_use_cases import ArchiveOneOffVendorUseCase, OneOffVendorRetirementError
from app.application.workbench.queries import ReviewDetailQuery
from app.application.workbench.reclassification import ReviewReclassificationResult
from app.application.workbench.supplier_remediation import (
    ResolveWorkbenchSupplierCommand,
    SupplierPartnerWriteEffectStatus,
    SupplierRemediationEffect,
    SupplierRemediationStatus,
)
from app.application.workbench.supplier_remediation_use_cases import ResolveWorkbenchSupplierUseCase
from app.application.workbench.supplier_resolution import ResolutionPartnerRecord, SupplierResolutionMode
from app.application.workbench.supplier_resolution_use_cases import ValidateSupplierResolutionUseCase
from app.application.workbench.write_authorization import (
    WriteAuthorizationAlreadyConsumedError,
    WriteAuthorizationExpiredError,
    WriteAuthorizationOperationType,
    WriteAuthorizationRecord,
    WriteAuthorizationRevokedError,
    WriteAuthorizationScopeMismatchError,
    WriteAuthorizationStatus,
    one_off_vendor_archive_authorization_consumer_id,
    supplier_resolution_authorization_consumer_id,
)
from app.application.workbench.write_authorization_use_cases import (
    RevokeWriteAuthorizationUseCase,
)
from app.application.workflow import ManualReviewReason, ManualReviewReasonCode, WorkflowType
from app.db.base import Base
from app.domain.invoice import Header, InternalInvoice, InvoiceLine, MonetaryTotals, Party, Tax
from app.erp.write.odoo_supplier_partner_writer import (
    OdooSupplierPartnerRepository,
    OdooSupplierPartnerWritePolicy,
    OdooSupplierPartnerWriter,
)
from app.models.workbench_review_item import WorkbenchReviewItem
from app.models.workbench_review_one_off_vendor_retirement import WorkbenchReviewOneOffVendorRetirement
from app.models.workbench_review_supplier_remediation_effect import WorkbenchReviewSupplierRemediationEffect
from app.models.workbench_review_supplier_resolution import WorkbenchReviewSupplierResolution
from app.models.workbench_review_write_authorization import WorkbenchReviewWriteAuthorization
from app.persistence import (
    SqlAlchemyReviewOneOffVendorRetirementRepository,
    SqlAlchemyReviewSupplierRemediationEffectRepository,
    SqlAlchemyReviewSupplierResolutionRepository,
    SqlAlchemyUnitOfWork,
    SqlAlchemyWriteAuthorizationRepository,
)

COMPANY_ID = 1
OTHER_COMPANY_ID = 2
NEW_VAT = "9998887770"
ARCHIVED_VAT = "2650179910"
SUPPLIER_REVIEW_ID = "review:supplier-new"
ARCHIVE_REVIEW_ID = "review:archive-recover"
ARCHIVED_PARTNER_ID = 448
ACTOR = "finance.operator"


# --------------------------------------------------------------------------- shared fakes


class _FakeOdooJson2Client:
    """When ``search_results`` is empty, models a genuinely new VAT: the pre-create
    exact-VAT search returns nothing, ``create_res_partner`` "creates" the record,
    and every subsequent search (the writer's own post-create re-query) returns
    that created record -- exactly what a real read-before-write, read-after-write
    writer needs to see to succeed. When ``search_results`` is non-empty, every
    search always returns it (the existing-partner/reuse path never re-queries)."""

    def __init__(self, *, search_results: list[dict[str, Any]] | None = None, create_result: int = 9001) -> None:
        self._search_results = search_results if search_results is not None else []
        self._create_result = create_result
        self._created_payload: dict[str, Any] | None = None
        self.create_calls: list[dict[str, Any]] = []
        self.search_calls: list[dict[str, Any]] = []
        self.archive_calls: list[int] = []

    async def create_res_partner(self, payload: dict[str, Any]) -> int:
        self.create_calls.append(payload)
        self._created_payload = payload
        return self._create_result

    async def search_read(self, *, model: str, domain, fields, limit: int = 20, offset: int = 0):
        self.search_calls.append({"model": model, "domain": domain})
        if model != "res.partner":
            return []
        if self._search_results:
            return self._search_results
        if self._created_payload is not None:
            return [
                {
                    "id": self._create_result,
                    "name": self._created_payload["name"],
                    "vat": self._created_payload["vat"],
                    "active": True,
                    "company_id": False,
                }
            ]
        return []

    async def archive_res_partner(self, *, partner_id: int) -> bool:
        self.archive_calls.append(partner_id)
        for record in self._search_results:
            if record.get("id") == partner_id:
                record["active"] = False
        return True


def _writer(client: _FakeOdooJson2Client, *, policy: OdooSupplierPartnerWritePolicy | None = None):
    return OdooSupplierPartnerWriter(
        repository=OdooSupplierPartnerRepository(client=client),
        policy=policy
        or OdooSupplierPartnerWritePolicy(
            supplier_remediation_write_enabled=False,  # narrow authorization must carry the write alone
            production_operations_enabled=True,
            production_approval_ack="APPROVED_FOR_PRODUCTION",
            app_env="production",
        ),
    )


def _source_invoice(*, vat: str, ettn: str) -> InternalInvoice:
    return InternalInvoice(
        header=Header(
            invoice_number="INV-1",
            invoice_uuid=f"00000000-0000-4000-8000-{abs(hash(ettn)) % 10**12:012d}",
            ettn=ettn,
            issue_date=date(2026, 9, 20),
            currency_code="TRY",
        ),
        supplier=Party(name="Some Vendor A.S.", tax_number=vat),
        customer=Party(name="ICT", tax_number="1112223334"),
        totals=MonetaryTotals(payable_amount=Decimal("100.00")),
        lines=(
            InvoiceLine(
                line_number="1",
                description="Item",
                quantity=Decimal("1"),
                unit_code="C62",
                unit_price=Decimal("83.33"),
                taxes=(Tax(tax_type="KDV", rate=Decimal("20")),),
            ),
        ),
    )


class _FakeReviewReader:
    def __init__(self, item: ReviewItem) -> None:
        self.item = item

    def get_review_item(self, query: ReviewDetailQuery) -> ReviewItem:
        return self.item


class _FakeSourceReader:
    def __init__(self, evidence: ReviewSourceInvoiceEvidence) -> None:
        self.evidence = evidence

    def get(self, *, review_id: str, company_id: int) -> ReviewSourceInvoiceEvidence:
        return self.evidence


class _FakePartnerReader:
    """CREATE_PERMANENT_SUPPLIER re-validates the created/existing partner (belt and
    suspenders) via this reader -- by default it knows about the fake Odoo client's
    default created-partner id/vat so that re-validation succeeds."""

    def __init__(self, record: ResolutionPartnerRecord | None = None) -> None:
        self._record = record or ResolutionPartnerRecord(
            id=9001, name="Some Vendor A.S.", vat=NEW_VAT, active=True, company_id=None
        )

    def find_partner_by_id(self, partner_id: int) -> ResolutionPartnerRecord | None:
        return self._record if self._record.id == partner_id else None


class _FakeReclassifier:
    async def execute(self, command):
        return ReviewReclassificationResult(
            review_id=command.review_id,
            company_id=command.company_id,
            from_version=command.expected_version,
            to_version=command.expected_version + 1,
            changed=True,
            previous_workflow=WorkflowType.MANUAL_REVIEW,
            new_workflow=WorkflowType.MANUAL_REVIEW,
            previous_review_reasons=(),
            new_review_reasons=(),
            trigger=command.trigger,
            executable=False,
        )


class _FakeVendorBillEvidenceReader:
    def has_successful_vendor_bill(self, *, review_id: str, company_id: int) -> bool:
        return True


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
            WorkbenchReviewWriteAuthorization.__table__,
        ],
    )
    factory = sessionmaker(bind=engine)
    with factory() as db_session:
        db_session.add(
            WorkbenchReviewItem(
                review_id=SUPPLIER_REVIEW_ID,
                company_id=COMPANY_ID,
                invoice_id="supplier-ettn",
                invoice_number="INV-1",
                supplier_tax_number=NEW_VAT,
                supplier_name="Some Vendor A.S.",
                invoice_date=date(2026, 9, 20),
                currency="TRY",
                total_amount=Decimal("100.00"),
                workflow="manual_review",
                status="pending_review",
                review_reasons=[{"code": "supplier_not_found", "message": "x"}],
                warnings=[],
                version=1,
                idempotency_key="uyumsoft:1:supplier-ettn",
            )
        )
        db_session.add(
            WorkbenchReviewItem(
                review_id=ARCHIVE_REVIEW_ID,
                company_id=COMPANY_ID,
                invoice_id="archive-ettn",
                invoice_number="HD1",
                supplier_tax_number=ARCHIVED_VAT,
                supplier_name="D-Market",
                invoice_date=date(2026, 9, 10),
                currency="TRY",
                total_amount=Decimal("676.21"),
                workflow="vendor_bill",
                status="decision_submitted",
                review_reasons=[],
                warnings=[],
                # The review has advanced well past the retirement row's own version --
                # exactly the real-world shape this feature must handle.
                version=5,
                idempotency_key="uyumsoft:1:archive-ettn",
            )
        )
        db_session.flush()
        SqlAlchemyReviewOneOffVendorRetirementRepository(db_session).create_retirement(
            OneOffVendorRetirement(
                review_id=ARCHIVE_REVIEW_ID,
                company_id=COMPANY_ID,
                review_version=1,
                resolved_partner_id=ARCHIVED_PARTNER_ID,
                status=OneOffVendorRetirementStatus.PENDING_VENDOR_BILL,
            )
        )
        db_session.commit()
        yield db_session


def _auth_repo(session: Session) -> SqlAlchemyWriteAuthorizationRepository:
    return SqlAlchemyWriteAuthorizationRepository(session)


def _issue(
    session: Session,
    *,
    operation_type: WriteAuthorizationOperationType,
    target_version: int,
    review_id: str = SUPPLIER_REVIEW_ID,
    expires_at: datetime | None = None,
) -> WriteAuthorizationRecord:
    from uuid import uuid4

    record = _auth_repo(session).create(
        authorization_id=str(uuid4()),
        company_id=COMPANY_ID,
        review_id=review_id,
        operation_type=operation_type,
        target_version=target_version,
        authorized_by=ACTOR,
        expires_at=expires_at or (datetime.now(UTC) + timedelta(minutes=15)),
    )
    session.commit()
    return record


def _resolve_use_case(
    session: Session,
    *,
    writer: OdooSupplierPartnerWriter,
    review: ReviewItem | None = None,
    source: InternalInvoice | None = None,
    vat: str = NEW_VAT,
) -> ResolveWorkbenchSupplierUseCase:
    review = review or ReviewItem(
        review_id=SUPPLIER_REVIEW_ID,
        invoice_id="supplier-ettn",
        invoice_number="INV-1",
        supplier_tax_number=vat,
        supplier_name="Some Vendor A.S.",
        invoice_date=date(2026, 9, 20),
        currency="TRY",
        total_amount=Decimal("100.00"),
        workflow=WorkflowType.MANUAL_REVIEW,
        status=ReviewStatus.PENDING_REVIEW,
        review_reasons=(
            ManualReviewReason(
                code=ManualReviewReasonCode.SUPPLIER_NOT_FOUND,
                message="x",
                source="partner_matching",
                candidate_count=0,
            ),
        ),
        version=1,
    )
    source_evidence = ReviewSourceInvoiceEvidence(
        review_id=review.review_id,
        company_id=COMPANY_ID,
        review_version=review.version,
        source_invoice_id="supplier-ettn",
        invoice=source or _source_invoice(vat=vat, ettn="supplier-ettn"),
    )
    return ResolveWorkbenchSupplierUseCase(
        review_reader=_FakeReviewReader(review),
        source_invoice_reader=_FakeSourceReader(source_evidence),
        resolution_validator=ValidateSupplierResolutionUseCase(
            source_invoice_reader=_FakeSourceReader(source_evidence),
            partner_reader=_FakePartnerReader(),
        ),
        resolution_writer=SqlAlchemyReviewSupplierResolutionRepository(session),
        remediation_effect_writer=SqlAlchemyReviewSupplierRemediationEffectRepository(session),
        supplier_partner_writer=writer,
        reclassifier=_FakeReclassifier(),
        unit_of_work=SqlAlchemyUnitOfWork(session),
        workbench_republisher=None,
        retirement_writer=SqlAlchemyReviewOneOffVendorRetirementRepository(session),
        write_authorization_repository=_auth_repo(session),
    )


def _supplier_command(
    *, mode: SupplierResolutionMode, authorization_id: str | None, review_id: str = SUPPLIER_REVIEW_ID
) -> ResolveWorkbenchSupplierCommand:
    return ResolveWorkbenchSupplierCommand(
        review_id=review_id,
        company_id=COMPANY_ID,
        expected_version=1,
        mode=mode,
        approved_by=ACTOR,
        resolved_partner_id=None,
        authorization_id=authorization_id,
    )


def _active_archived_partner_client(**kwargs: Any) -> _FakeOdooJson2Client:
    """The archive writer always reads the partner back FIRST (active=True here, so
    it proceeds to actually attempt the archive write) before ever checking the
    write gate -- every archive-scenario test needs this, not an empty client."""

    return _FakeOdooJson2Client(
        search_results=[{"id": ARCHIVED_PARTNER_ID, "name": "D-Market", "vat": ARCHIVED_VAT, "active": True}],
        **kwargs,
    )


def _archive_use_case(
    session: Session,
    *,
    client: _FakeOdooJson2Client,
    authorization_id: str | None,
    policy: OdooSupplierPartnerWritePolicy | None = None,
) -> ArchiveOneOffVendorUseCase:
    from app.erp.write.odoo_one_off_vendor_retirement_writer import OdooOneOffVendorRetirementWriter

    return ArchiveOneOffVendorUseCase(
        retirement_writer=SqlAlchemyReviewOneOffVendorRetirementRepository(session),
        vendor_bill_evidence_reader=_FakeVendorBillEvidenceReader(),
        retirement_port=OdooOneOffVendorRetirementWriter(
            repository=OdooSupplierPartnerRepository(client=client),
            client=client,
            policy=policy
            or OdooSupplierPartnerWritePolicy(
                supplier_remediation_write_enabled=False,
                production_operations_enabled=True,
                production_approval_ack="APPROVED_FOR_PRODUCTION",
                app_env="production",
            ),
        ),
        unit_of_work=SqlAlchemyUnitOfWork(session),
        approved_by=ACTOR,
        write_authorization_repository=_auth_repo(session),
        authorization_id=authorization_id,
    )


# =================================================================== A1: CREATE_PERMANENT_SUPPLIER


async def test_permanent_supplier_write_authorized_without_global_gate(session: Session) -> None:
    authorization = _issue(
        session, operation_type=WriteAuthorizationOperationType.CREATE_PERMANENT_SUPPLIER, target_version=1
    )
    client = _FakeOdooJson2Client(search_results=[])
    use_case = _resolve_use_case(session, writer=_writer(client))

    result = await use_case.execute(
        _supplier_command(
            mode=SupplierResolutionMode.CREATE_PERMANENT_SUPPLIER, authorization_id=authorization.authorization_id
        )
    )

    assert result.status is SupplierRemediationStatus.RESOLVED
    assert result.partner_write_status is SupplierPartnerWriteEffectStatus.CREATED
    assert len(client.create_calls) == 1

    stored = _auth_repo(session).get_by_id(authorization_id=authorization.authorization_id, company_id=COMPANY_ID)
    assert stored.status is WriteAuthorizationStatus.CONSUMED
    assert stored.use_count == 1


async def test_permanent_supplier_without_authorization_still_needs_global_gate(session: Session) -> None:
    client = _FakeOdooJson2Client(search_results=[])
    use_case = _resolve_use_case(session, writer=_writer(client))

    with pytest.raises(SupplierPartnerWriteSafetyGateError):
        await use_case.execute(
            _supplier_command(mode=SupplierResolutionMode.CREATE_PERMANENT_SUPPLIER, authorization_id=None)
        )
    assert client.create_calls == []


# =================================================================== A2: ONE_OFF_VENDOR_SUPPLIER


async def test_one_off_vendor_supplier_create_is_authorized_without_global_gate(session: Session) -> None:
    authorization = _issue(
        session, operation_type=WriteAuthorizationOperationType.ONE_OFF_VENDOR_SUPPLIER, target_version=1
    )
    client = _FakeOdooJson2Client(search_results=[])
    use_case = _resolve_use_case(session, writer=_writer(client))

    result = await use_case.execute(
        _supplier_command(mode=SupplierResolutionMode.ONE_OFF_VENDOR, authorization_id=authorization.authorization_id)
    )

    assert result.status is SupplierRemediationStatus.RESOLVED
    assert result.one_off_vendor_hub_owned is True
    assert result.one_off_vendor_retirement_status is OneOffVendorRetirementStatus.PENDING_VENDOR_BILL
    assert len(client.create_calls) == 1


async def test_one_off_vendor_reuse_of_archived_hub_owned_partner_still_works_with_authorization(
    session: Session,
) -> None:
    """#159 regression, now also authorized narrowly: reusing an archived Hub-owned
    partner for a NEW review is unaffected by adding narrow authorization support."""

    SqlAlchemyReviewSupplierRemediationEffectRepository(session).create_remediation_effect(
        SupplierRemediationEffect(
            review_id="review:prior-one-off",
            company_id=COMPANY_ID,
            review_version=1,
            source_invoice_id="prior-ettn",
            mode=SupplierResolutionMode.ONE_OFF_VENDOR,
            resolved_partner_id=ARCHIVED_PARTNER_ID,
            partner_write_status=SupplierPartnerWriteEffectStatus.CREATED,
            source_supplier_tax_number=ARCHIVED_VAT,
            approved_by=ACTOR,
        )
    )
    session.commit()

    authorization = _issue(
        session, operation_type=WriteAuthorizationOperationType.ONE_OFF_VENDOR_SUPPLIER, target_version=1
    )
    client = _FakeOdooJson2Client(
        search_results=[{"id": ARCHIVED_PARTNER_ID, "name": "D-Market", "vat": ARCHIVED_VAT, "active": False}]
    )
    use_case = _resolve_use_case(session, writer=_writer(client), vat=ARCHIVED_VAT)

    result = await use_case.execute(
        _supplier_command(mode=SupplierResolutionMode.ONE_OFF_VENDOR, authorization_id=authorization.authorization_id)
    )

    assert result.effective_partner_id == ARCHIVED_PARTNER_ID
    assert result.partner_write_status is SupplierPartnerWriteEffectStatus.ALREADY_EXISTS
    assert client.create_calls == []  # no duplicate partner


async def test_inactive_non_hub_owned_partner_still_fails_closed_even_with_authorization(session: Session) -> None:
    """Case B is unaffected: narrow authorization only bypasses the *gate*, never the
    Hub-ownership proof required to reuse an archived partner."""

    authorization = _issue(
        session, operation_type=WriteAuthorizationOperationType.ONE_OFF_VENDOR_SUPPLIER, target_version=1
    )
    client = _FakeOdooJson2Client(
        search_results=[{"id": 9999, "name": "Not Hub Owned", "vat": NEW_VAT, "active": False}]
    )
    use_case = _resolve_use_case(session, writer=_writer(client))

    with pytest.raises(SupplierPartnerInactiveError):
        await use_case.execute(
            _supplier_command(
                mode=SupplierResolutionMode.ONE_OFF_VENDOR, authorization_id=authorization.authorization_id
            )
        )
    assert client.create_calls == []


# =================================================================== A3: ONE_OFF_VENDOR_ARCHIVE


async def test_archive_recovery_authorized_without_global_gate(session: Session) -> None:
    authorization = _issue(
        session,
        operation_type=WriteAuthorizationOperationType.ONE_OFF_VENDOR_ARCHIVE,
        target_version=1,
        review_id=ARCHIVE_REVIEW_ID,
    )
    client = _active_archived_partner_client()
    use_case = _archive_use_case(session, client=client, authorization_id=authorization.authorization_id)

    result = await use_case.execute(
        ArchiveOneOffVendorCommand(review_id=ARCHIVE_REVIEW_ID, company_id=COMPANY_ID, review_version=1)
    )

    assert result.status is ArchiveOneOffVendorStatus.ARCHIVED
    assert client.archive_calls == [ARCHIVED_PARTNER_ID]
    stored = _auth_repo(session).get_by_id(authorization_id=authorization.authorization_id, company_id=COMPANY_ID)
    assert stored.status is WriteAuthorizationStatus.CONSUMED


async def test_archive_recovery_target_version_is_retirement_version_not_review_current_version(
    session: Session,
) -> None:
    """The review is at version 5; the retirement row (and the authorization) target
    version 1 -- proving target_version validation is retirement-scoped, not
    review-current-version-scoped, for this operation type specifically."""

    authorization = _issue(
        session,
        operation_type=WriteAuthorizationOperationType.ONE_OFF_VENDOR_ARCHIVE,
        target_version=1,
        review_id=ARCHIVE_REVIEW_ID,
    )
    use_case = _archive_use_case(
        session, client=_active_archived_partner_client(), authorization_id=authorization.authorization_id
    )

    result = await use_case.execute(
        ArchiveOneOffVendorCommand(review_id=ARCHIVE_REVIEW_ID, company_id=COMPANY_ID, review_version=1)
    )
    assert result.status is ArchiveOneOffVendorStatus.ARCHIVED


async def test_archive_recovery_without_authorization_still_needs_global_gate(session: Session) -> None:
    client = _active_archived_partner_client()
    use_case = _archive_use_case(session, client=client, authorization_id=None)

    with pytest.raises(SupplierPartnerWriteSafetyGateError):
        await use_case.execute(
            ArchiveOneOffVendorCommand(review_id=ARCHIVE_REVIEW_ID, company_id=COMPANY_ID, review_version=1)
        )
    assert client.archive_calls == []


async def test_archive_recovery_already_archived_is_a_no_op_and_never_touches_authorization(
    session: Session,
) -> None:
    """No write attempted -> no authorization claim attempted -- issuing one is
    wasteful but harmless, and stays PENDING (never silently burned) since the
    use case returns before ever reaching the claim."""

    retirement_repo = SqlAlchemyReviewOneOffVendorRetirementRepository(session)
    row = retirement_repo.find(review_id=ARCHIVE_REVIEW_ID, company_id=COMPANY_ID, review_version=1)
    retirement_repo.advance(
        row,
        expected_status=OneOffVendorRetirementStatus.PENDING_VENDOR_BILL,
        new_status=OneOffVendorRetirementStatus.ARCHIVE_ATTEMPTED,
    )
    row = retirement_repo.find(review_id=ARCHIVE_REVIEW_ID, company_id=COMPANY_ID, review_version=1)
    retirement_repo.advance(
        row,
        expected_status=OneOffVendorRetirementStatus.ARCHIVE_ATTEMPTED,
        new_status=OneOffVendorRetirementStatus.ARCHIVED,
    )
    session.commit()

    authorization = _issue(
        session,
        operation_type=WriteAuthorizationOperationType.ONE_OFF_VENDOR_ARCHIVE,
        target_version=1,
        review_id=ARCHIVE_REVIEW_ID,
    )
    client = _FakeOdooJson2Client()
    use_case = _archive_use_case(session, client=client, authorization_id=authorization.authorization_id)

    result = await use_case.execute(
        ArchiveOneOffVendorCommand(review_id=ARCHIVE_REVIEW_ID, company_id=COMPANY_ID, review_version=1)
    )
    assert result.already_applied is True
    assert client.archive_calls == []
    stored = _auth_repo(session).get_by_id(authorization_id=authorization.authorization_id, company_id=COMPANY_ID)
    assert stored.status is WriteAuthorizationStatus.PENDING


# =================================================================== wrong company/review/version/operation


@pytest.mark.parametrize(
    "override",
    [
        # company_id is deliberately NOT included here -- it is part of the primary
        # lookup filter itself, so a mismatch raises WriteAuthorizationNotFoundError
        # instead (see test_claim_rejects_wrong_company_even_when_authorization_id_is_a_guess).
        {"review_id": "review:other"},
        {"target_version": 2},
        {"operation_type": WriteAuthorizationOperationType.ONE_OFF_VENDOR_SUPPLIER},
    ],
)
def test_claim_rejects_any_scope_mismatch(session: Session, override: dict) -> None:
    authorization = _issue(
        session, operation_type=WriteAuthorizationOperationType.CREATE_PERMANENT_SUPPLIER, target_version=1
    )
    kwargs = {
        "company_id": COMPANY_ID,
        "review_id": SUPPLIER_REVIEW_ID,
        "operation_type": WriteAuthorizationOperationType.CREATE_PERMANENT_SUPPLIER,
        "target_version": 1,
        "authorization_id": authorization.authorization_id,
        "execution_id": "consumer-x",
    }
    kwargs.update(override)
    with pytest.raises(WriteAuthorizationScopeMismatchError):
        _auth_repo(session).claim_and_consume(**kwargs)


def test_claim_rejects_wrong_company_even_when_authorization_id_is_a_guess(session: Session) -> None:
    """company_id is part of the primary lookup filter itself (not just a post-check)
    -- a caller from a different tenant cannot even find the row to claim."""

    authorization = _issue(
        session, operation_type=WriteAuthorizationOperationType.CREATE_PERMANENT_SUPPLIER, target_version=1
    )
    from app.application.workbench.write_authorization import WriteAuthorizationNotFoundError

    with pytest.raises(WriteAuthorizationNotFoundError):
        _auth_repo(session).claim_and_consume(
            company_id=OTHER_COMPANY_ID,
            review_id=SUPPLIER_REVIEW_ID,
            operation_type=WriteAuthorizationOperationType.CREATE_PERMANENT_SUPPLIER,
            target_version=1,
            authorization_id=authorization.authorization_id,
            execution_id="consumer-x",
        )


def test_archive_claim_rejects_stale_retirement_version(session: Session) -> None:
    """A target_version for which no retirement row exists at all fails closed --
    the ONE_OFF_VENDOR_ARCHIVE-specific validity check, not the generic
    current-review-version one."""

    authorization = _issue(
        session,
        operation_type=WriteAuthorizationOperationType.ONE_OFF_VENDOR_ARCHIVE,
        target_version=1,
        review_id=ARCHIVE_REVIEW_ID,
    )
    with pytest.raises(WriteAuthorizationScopeMismatchError):
        _auth_repo(session).claim_and_consume(
            company_id=COMPANY_ID,
            review_id=ARCHIVE_REVIEW_ID,
            operation_type=WriteAuthorizationOperationType.ONE_OFF_VENDOR_ARCHIVE,
            target_version=99,  # authorization itself targets 1; this simulates a forged/mismatched claim
            authorization_id=authorization.authorization_id,
            execution_id="consumer-x",
        )


# =================================================================== expired/revoked/consumed


def test_claim_rejects_expired_authorization(session: Session) -> None:
    # WriteAuthorizationRecord.__post_init__ itself rejects expires_at <= created_at,
    # so an already-expired record cannot be *issued* through the normal create()
    # path (which re-hydrates and validates the domain object on every read). To
    # model "issued fine, later expired", mutate the persisted row directly --
    # claim_and_consume reads the raw SQLAlchemy model's expires_at, not a
    # re-validated domain object, so this exercises the real expiry check exactly.
    authorization = _issue(
        session, operation_type=WriteAuthorizationOperationType.CREATE_PERMANENT_SUPPLIER, target_version=1
    )
    row = (
        session.query(WorkbenchReviewWriteAuthorization)
        .filter_by(authorization_id=authorization.authorization_id)
        .one()
    )
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    session.commit()
    with pytest.raises(WriteAuthorizationExpiredError):
        _auth_repo(session).claim_and_consume(
            company_id=COMPANY_ID,
            review_id=SUPPLIER_REVIEW_ID,
            operation_type=WriteAuthorizationOperationType.CREATE_PERMANENT_SUPPLIER,
            target_version=1,
            authorization_id=authorization.authorization_id,
            execution_id="consumer-x",
        )


def test_claim_rejects_revoked_authorization(session: Session) -> None:
    authorization = _issue(
        session, operation_type=WriteAuthorizationOperationType.CREATE_PERMANENT_SUPPLIER, target_version=1
    )
    revoke_use_case = RevokeWriteAuthorizationUseCase(
        repository=_auth_repo(session), unit_of_work=SqlAlchemyUnitOfWork(session)
    )
    revoke_use_case.execute(
        company_id=COMPANY_ID,
        review_id=SUPPLIER_REVIEW_ID,
        authorization_id=authorization.authorization_id,
        revoked_by=ACTOR,
    )
    with pytest.raises(WriteAuthorizationRevokedError):
        _auth_repo(session).claim_and_consume(
            company_id=COMPANY_ID,
            review_id=SUPPLIER_REVIEW_ID,
            operation_type=WriteAuthorizationOperationType.CREATE_PERMANENT_SUPPLIER,
            target_version=1,
            authorization_id=authorization.authorization_id,
            execution_id="consumer-x",
        )


def test_claim_rejects_already_consumed_by_a_different_attempt(session: Session) -> None:
    authorization = _issue(
        session, operation_type=WriteAuthorizationOperationType.CREATE_PERMANENT_SUPPLIER, target_version=1
    )
    _auth_repo(session).claim_and_consume(
        company_id=COMPANY_ID,
        review_id=SUPPLIER_REVIEW_ID,
        operation_type=WriteAuthorizationOperationType.CREATE_PERMANENT_SUPPLIER,
        target_version=1,
        authorization_id=authorization.authorization_id,
        execution_id="consumer-A",
    )
    session.commit()
    with pytest.raises(WriteAuthorizationAlreadyConsumedError):
        _auth_repo(session).claim_and_consume(
            company_id=COMPANY_ID,
            review_id=SUPPLIER_REVIEW_ID,
            operation_type=WriteAuthorizationOperationType.CREATE_PERMANENT_SUPPLIER,
            target_version=1,
            authorization_id=authorization.authorization_id,
            execution_id="consumer-B",
        )


# =================================================================== B: replay / concurrency


def test_replay_with_same_consumer_id_is_idempotent(session: Session) -> None:
    """A legitimate crash-then-retry of the same underlying write attempt (same
    deterministic consumer id) re-claims its own already-consumed authorization
    successfully -- proven directly for the new operation types."""

    authorization = _issue(
        session, operation_type=WriteAuthorizationOperationType.ONE_OFF_VENDOR_SUPPLIER, target_version=1
    )
    consumer_id = supplier_resolution_authorization_consumer_id(
        company_id=COMPANY_ID, review_id=SUPPLIER_REVIEW_ID, expected_version=1, mode="one_off_vendor"
    )
    first = _auth_repo(session).claim_and_consume(
        company_id=COMPANY_ID,
        review_id=SUPPLIER_REVIEW_ID,
        operation_type=WriteAuthorizationOperationType.ONE_OFF_VENDOR_SUPPLIER,
        target_version=1,
        authorization_id=authorization.authorization_id,
        execution_id=consumer_id,
    )
    session.commit()
    second = _auth_repo(session).claim_and_consume(
        company_id=COMPANY_ID,
        review_id=SUPPLIER_REVIEW_ID,
        operation_type=WriteAuthorizationOperationType.ONE_OFF_VENDOR_SUPPLIER,
        target_version=1,
        authorization_id=authorization.authorization_id,
        execution_id=consumer_id,
    )
    assert first.status is second.status is WriteAuthorizationStatus.CONSUMED
    assert first.use_count == 1  # the conditional UPDATE's WHERE clause makes the replay a true no-op re-read


def test_deterministic_consumer_id_helpers_are_stable_and_distinct() -> None:
    a = supplier_resolution_authorization_consumer_id(
        company_id=1, review_id="r1", expected_version=1, mode="create_permanent_supplier"
    )
    b = supplier_resolution_authorization_consumer_id(
        company_id=1, review_id="r1", expected_version=1, mode="create_permanent_supplier"
    )
    c = supplier_resolution_authorization_consumer_id(
        company_id=1, review_id="r1", expected_version=1, mode="one_off_vendor"
    )
    assert a == b
    assert a != c
    d = one_off_vendor_archive_authorization_consumer_id(company_id=1, review_id="r1", review_version=1)
    e = one_off_vendor_archive_authorization_consumer_id(company_id=1, review_id="r1", review_version=1)
    f = one_off_vendor_archive_authorization_consumer_id(company_id=1, review_id="r1", review_version=2)
    assert d == e
    assert d != f
    assert a != d  # different operation families never collide


# =================================================================== master kill switch


async def test_master_kill_switch_blocks_supplier_write_even_with_valid_authorization(session: Session) -> None:
    authorization = _issue(
        session, operation_type=WriteAuthorizationOperationType.CREATE_PERMANENT_SUPPLIER, target_version=1
    )
    client = _FakeOdooJson2Client(search_results=[])
    policy = OdooSupplierPartnerWritePolicy(
        supplier_remediation_write_enabled=False,
        production_operations_enabled=False,  # the absolute master switch, OFF
        production_approval_ack="APPROVED_FOR_PRODUCTION",
        app_env="production",
    )
    use_case = _resolve_use_case(session, writer=_writer(client, policy=policy))

    with pytest.raises(SupplierPartnerWriteSafetyGateError):
        await use_case.execute(
            _supplier_command(
                mode=SupplierResolutionMode.CREATE_PERMANENT_SUPPLIER, authorization_id=authorization.authorization_id
            )
        )
    assert client.create_calls == []
    # The authorization was rolled back, not burned -- still usable once the real
    # misconfiguration (master switch) is fixed.
    stored = _auth_repo(session).get_by_id(authorization_id=authorization.authorization_id, company_id=COMPANY_ID)
    assert stored.status is WriteAuthorizationStatus.PENDING


async def test_master_kill_switch_blocks_archive_write_even_with_valid_authorization(session: Session) -> None:
    authorization = _issue(
        session,
        operation_type=WriteAuthorizationOperationType.ONE_OFF_VENDOR_ARCHIVE,
        target_version=1,
        review_id=ARCHIVE_REVIEW_ID,
    )
    client = _active_archived_partner_client()
    policy = OdooSupplierPartnerWritePolicy(
        supplier_remediation_write_enabled=False,
        production_operations_enabled=False,
        production_approval_ack="APPROVED_FOR_PRODUCTION",
        app_env="production",
    )
    use_case = _archive_use_case(session, client=client, authorization_id=authorization.authorization_id, policy=policy)

    with pytest.raises(SupplierPartnerWriteSafetyGateError):
        await use_case.execute(
            ArchiveOneOffVendorCommand(review_id=ARCHIVE_REVIEW_ID, company_id=COMPANY_ID, review_version=1)
        )
    assert client.archive_calls == []
    stored = _auth_repo(session).get_by_id(authorization_id=authorization.authorization_id, company_id=COMPANY_ID)
    assert stored.status is WriteAuthorizationStatus.PENDING
    retirement = SqlAlchemyReviewOneOffVendorRetirementRepository(session).find(
        review_id=ARCHIVE_REVIEW_ID, company_id=COMPANY_ID, review_version=1
    )
    # A certain-no-write failure (the existing, unmodified _CERTAIN_NO_WRITE_EXCEPTIONS
    # handler) reverts ARCHIVE_ATTEMPTED back to PENDING_VENDOR_BILL -- never stuck,
    # never silently treated as archived.
    assert retirement.status is OneOffVendorRetirementStatus.PENDING_VENDOR_BILL


async def test_missing_approval_ack_blocks_write_even_with_valid_authorization(session: Session) -> None:
    authorization = _issue(
        session, operation_type=WriteAuthorizationOperationType.CREATE_PERMANENT_SUPPLIER, target_version=1
    )
    client = _FakeOdooJson2Client(search_results=[])
    policy = OdooSupplierPartnerWritePolicy(
        supplier_remediation_write_enabled=False,
        production_operations_enabled=True,
        production_approval_ack="",  # missing
        app_env="production",
    )
    use_case = _resolve_use_case(session, writer=_writer(client, policy=policy))

    with pytest.raises(SupplierPartnerWriteSafetyGateError):
        await use_case.execute(
            _supplier_command(
                mode=SupplierResolutionMode.CREATE_PERMANENT_SUPPLIER, authorization_id=authorization.authorization_id
            )
        )
    assert client.create_calls == []


def test_missing_named_approver_blocks_write_even_with_valid_authorization(session: Session) -> None:
    """ResolveWorkbenchSupplierCommand itself already requires a non-blank
    approved_by unconditionally (the real actor always comes from the authenticated
    RequestContext, never user-suppliable) -- so this attack surface can only be
    exercised at the policy the writer actually calls. Proves the bypass branch
    still runs _ensure_named_approver exactly like the non-bypass branch always has."""

    authorization = _issue(
        session, operation_type=WriteAuthorizationOperationType.CREATE_PERMANENT_SUPPLIER, target_version=1
    )
    policy = OdooSupplierPartnerWritePolicy(
        supplier_remediation_write_enabled=False,
        production_operations_enabled=True,
        production_approval_ack="APPROVED_FOR_PRODUCTION",
        app_env="production",
    )
    with pytest.raises(SupplierPartnerWriteSafetyGateError):
        policy.ensure_real_write_allowed(approved_by=None, write_authorization=authorization)
    with pytest.raises(SupplierPartnerWriteSafetyGateError):
        policy.ensure_real_write_allowed(approved_by="   ", write_authorization=authorization)


# =================================================================== operation type not supported here


async def test_no_write_authorization_repository_configured_fails_closed(session: Session) -> None:
    client = _FakeOdooJson2Client(search_results=[])
    use_case = ResolveWorkbenchSupplierUseCase(
        review_reader=_FakeReviewReader(
            ReviewItem(
                review_id=SUPPLIER_REVIEW_ID,
                invoice_id="supplier-ettn",
                invoice_number="INV-1",
                supplier_tax_number=NEW_VAT,
                supplier_name="Some Vendor A.S.",
                invoice_date=date(2026, 9, 20),
                currency="TRY",
                total_amount=Decimal("100.00"),
                workflow=WorkflowType.MANUAL_REVIEW,
                status=ReviewStatus.PENDING_REVIEW,
                review_reasons=(
                    ManualReviewReason(
                        code=ManualReviewReasonCode.SUPPLIER_NOT_FOUND,
                        message="x",
                        source="partner_matching",
                        candidate_count=0,
                    ),
                ),
                version=1,
            )
        ),
        source_invoice_reader=_FakeSourceReader(
            ReviewSourceInvoiceEvidence(
                review_id=SUPPLIER_REVIEW_ID,
                company_id=COMPANY_ID,
                review_version=1,
                source_invoice_id="supplier-ettn",
                invoice=_source_invoice(vat=NEW_VAT, ettn="supplier-ettn"),
            )
        ),
        resolution_validator=ValidateSupplierResolutionUseCase(
            source_invoice_reader=_FakeSourceReader(
                ReviewSourceInvoiceEvidence(
                    review_id=SUPPLIER_REVIEW_ID,
                    company_id=COMPANY_ID,
                    review_version=1,
                    source_invoice_id="supplier-ettn",
                    invoice=_source_invoice(vat=NEW_VAT, ettn="supplier-ettn"),
                )
            ),
            partner_reader=_FakePartnerReader(),
        ),
        resolution_writer=SqlAlchemyReviewSupplierResolutionRepository(session),
        remediation_effect_writer=SqlAlchemyReviewSupplierRemediationEffectRepository(session),
        supplier_partner_writer=_writer(client),
        reclassifier=_FakeReclassifier(),
        unit_of_work=SqlAlchemyUnitOfWork(session),
        retirement_writer=SqlAlchemyReviewOneOffVendorRetirementRepository(session),
        write_authorization_repository=None,  # not configured
    )
    with pytest.raises(SupplierResolutionContractError):
        await use_case.execute(
            _supplier_command(mode=SupplierResolutionMode.CREATE_PERMANENT_SUPPLIER, authorization_id="fake-id")
        )


async def test_archive_use_case_without_write_authorization_repository_fails_closed(session: Session) -> None:
    from app.erp.write.odoo_one_off_vendor_retirement_writer import OdooOneOffVendorRetirementWriter

    client = _FakeOdooJson2Client()
    use_case = ArchiveOneOffVendorUseCase(
        retirement_writer=SqlAlchemyReviewOneOffVendorRetirementRepository(session),
        vendor_bill_evidence_reader=_FakeVendorBillEvidenceReader(),
        retirement_port=OdooOneOffVendorRetirementWriter(
            repository=OdooSupplierPartnerRepository(client=client),
            client=client,
            policy=OdooSupplierPartnerWritePolicy(
                supplier_remediation_write_enabled=False,
                production_operations_enabled=True,
                production_approval_ack="APPROVED_FOR_PRODUCTION",
                app_env="production",
            ),
        ),
        unit_of_work=SqlAlchemyUnitOfWork(session),
        approved_by=ACTOR,
        write_authorization_repository=None,
        authorization_id="fake-id",
    )
    with pytest.raises(OneOffVendorRetirementError):
        await use_case.execute(
            ArchiveOneOffVendorCommand(review_id=ARCHIVE_REVIEW_ID, company_id=COMPANY_ID, review_version=1)
        )


# =================================================================== C: HTTP-level wiring smoke test


class _FakeResolveUseCase:
    def __init__(self) -> None:
        self.calls: list[ResolveWorkbenchSupplierCommand] = []

    async def execute(self, command: ResolveWorkbenchSupplierCommand):
        self.calls.append(command)
        return SupplierRemediationEffectStub()


class SupplierRemediationEffectStub:
    review_id = SUPPLIER_REVIEW_ID
    company_id = COMPANY_ID
    mode = SupplierResolutionMode.CREATE_PERMANENT_SUPPLIER
    status = SupplierRemediationStatus.RESOLVED
    previous_version = 1
    current_version = 2
    current_workflow = WorkflowType.VENDOR_BILL
    current_review_reasons = ()
    effective_partner_id = 9001
    partner_write_status = SupplierPartnerWriteEffectStatus.CREATED
    reclassified = True
    already_applied = False
    workbench_republished = False
    safe_message = "ok"
    one_off_vendor_hub_owned = None
    one_off_vendor_retirement_status = None


def test_supplier_resolution_authorization_id_reaches_the_command() -> None:
    fake_use_case = _FakeResolveUseCase()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[dependencies.get_resolve_workbench_supplier_use_case] = lambda: fake_use_case
    app.dependency_overrides[dependencies.get_request_context] = lambda: RequestContext(
        user_id="finance",
        user_name="Finance",
        company_id=COMPANY_ID,
        permissions=(Permission.WORKBENCH_REVIEW_DECIDE,),
        trace_id="wiring-test",
        authentication_method=AuthenticationMethod.JWT,
    )
    with TestClient(app) as client:
        response = client.post(
            f"/api/workbench/reviews/{SUPPLIER_REVIEW_ID}/supplier-resolution",
            json={
                "mode": "create_permanent_supplier",
                "expected_version": 1,
                "authorization_id": "11111111-1111-1111-1111-111111111111",
            },
        )
    assert response.status_code == 200, response.text
    assert len(fake_use_case.calls) == 1
    assert fake_use_case.calls[0].authorization_id == "11111111-1111-1111-1111-111111111111"
