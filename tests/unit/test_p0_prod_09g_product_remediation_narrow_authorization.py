"""P0-PROD-09G: narrow runtime write authorization for CREATE_NEW_PRODUCT.

Reuses the exact P0-PROD-09D1/09F WriteAuthorization state machine (TTL,
single-use, audit, row-locking, replay semantics) unchanged -- extended only
with one new ``WriteAuthorizationOperationType`` member. CREATE_NEW_PRODUCT
covers both underlying Odoo writes a review line's remediation makes
(``product.template`` create, then ``product.supplierinfo`` create/link):
``CreateNewProductUseCase`` claims the same authorization again, idempotently,
immediately before each one, so one authorization covers the whole chain and a
legitimate crash-then-retry of either step resumes against its own
already-consumed authorization.

Three layers of coverage:
  A. Application-layer: real ``CreateNewProductUseCase`` /
     ``SqlAlchemyWriteAuthorizationRepository`` against a real SQLite session,
     with controllable fake ``ProductWriter``/``SupplierInfoWriter`` boundaries
     -- proves the full issue -> claim -> consume -> scope-check chain, that
     the authorization is threaded into both underlying commands, and that a
     certain-no-write failure rolls back before reverting reservation state so
     the authorization survives a misconfiguration retry (mirrors
     ``ArchiveOneOffVendorUseCase``'s P0-PROD-09F fix).
  B. Repository-level: ``claim_and_consume``'s own row-locked single-use /
     idempotent-resume semantics and ``OdooProductWritePolicy``'s gate-bypass
     shape, exercised directly for CREATE_NEW_PRODUCT.
  C. HTTP-level wiring smoke test: the new ``authorization_id`` field reaches
     the command, using a fake use-case dependency override (no DB).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.api import dependencies
from app.api.routers.workbench import router
from app.api.security import AuthenticationMethod, Permission, RequestContext
from app.application.commands.product_remediation import CreateProductCommand, CreateSupplierInfoCommand
from app.application.dto.product_remediation import (
    ProductWriteResult,
    ProductWriteStatus,
    SupplierInfoWriteResult,
    SupplierInfoWriteStatus,
)
from app.application.exceptions.product_remediation import ProductWriteSafetyGateError
from app.application.workbench.dto import ReviewItem, ReviewStatus
from app.application.workbench.evidence import ReviewSourceInvoiceEvidence
from app.application.workbench.exceptions import ProductRemediationContractError, ReviewNotFoundError
from app.application.workbench.product_remediation import (
    CreateNewProductCommand,
    ExistingSupplierInfo,
    ProductRemediationReservation,
    ProductRemediationStatus,
    ProductReservationStatus,
)
from app.application.workbench.product_remediation_use_cases import CreateNewProductUseCase
from app.application.workbench.queries import ReviewDetailQuery
from app.application.workbench.supplier_remediation import SupplierPartnerWriteEffectStatus, SupplierRemediationEffect
from app.application.workbench.supplier_resolution import SupplierResolutionMode
from app.application.workbench.write_authorization import (
    WriteAuthorizationAlreadyConsumedError,
    WriteAuthorizationExpiredError,
    WriteAuthorizationNotFoundError,
    WriteAuthorizationOperationType,
    WriteAuthorizationRecord,
    WriteAuthorizationRevokedError,
    WriteAuthorizationScopeMismatchError,
    WriteAuthorizationStatus,
    product_remediation_authorization_consumer_id,
)
from app.application.workflow import ManualReviewReason, ManualReviewReasonCode, WorkflowType
from app.db.base import Base
from app.domain.invoice import Header, InternalInvoice, InvoiceLine, MonetaryTotals, Party, Tax
from app.erp.write.odoo_product_write_policy import OdooProductWritePolicy
from app.models.workbench_review_item import WorkbenchReviewItem
from app.models.workbench_review_product_identity_claim import WorkbenchReviewProductIdentityClaim
from app.models.workbench_review_product_remediation_reservation import (
    WorkbenchReviewProductRemediationReservation,
)
from app.models.workbench_review_supplier_remediation_effect import WorkbenchReviewSupplierRemediationEffect
from app.models.workbench_review_write_authorization import WorkbenchReviewWriteAuthorization
from app.persistence import (
    SqlAlchemyReviewProductIdentityClaimRepository,
    SqlAlchemyReviewProductRemediationReservationRepository,
    SqlAlchemyReviewSupplierRemediationEffectRepository,
    SqlAlchemyUnitOfWork,
    SqlAlchemyWriteAuthorizationRepository,
)

COMPANY_ID = 3
OTHER_COMPANY_ID = 4
REVIEW_ID = "review:product-remediation-auth"
OTHER_REVIEW_ID = "review:other"
ETTN = "AKYASAM-ETTN-PRODREM-AUTH-1"
VKN = "0430367181"
PARTNER_ID = 4010
ACTOR = "finance.operator"
SELLER_ITEM_CODE = "SKU-AUTH-100"

TABLES = [
    WorkbenchReviewItem.__table__,
    WorkbenchReviewSupplierRemediationEffect.__table__,
    WorkbenchReviewProductRemediationReservation.__table__,
    WorkbenchReviewProductIdentityClaim.__table__,
    WorkbenchReviewWriteAuthorization.__table__,
]


# --------------------------------------------------------------------------- builders


def _invoice(*, line_number: str = "1") -> InternalInvoice:
    return InternalInvoice(
        header=Header(
            invoice_number="AKY-1",
            invoice_uuid="00000000-0000-4000-8000-00000000e002",
            ettn=ETTN,
            issue_date=date(2026, 8, 20),
            currency_code="TRY",
        ),
        supplier=Party(name="AKYASAM", tax_number=VKN),
        customer=Party(name="ICT TEKNOLOJI", tax_number="1112223334"),
        totals=MonetaryTotals(payable_amount=Decimal("100.00")),
        lines=(
            InvoiceLine(
                line_number=line_number,
                description="Yillik aidat",
                seller_item_code=SELLER_ITEM_CODE,
                quantity=Decimal("1"),
                unit_code="C62",
                unit_price=Decimal("83.33"),
                taxes=(Tax(tax_type="KDV", rate=Decimal("20")),),
            ),
        ),
    )


def _source_evidence() -> ReviewSourceInvoiceEvidence:
    return ReviewSourceInvoiceEvidence(
        review_id=REVIEW_ID,
        company_id=COMPANY_ID,
        review_version=1,
        source_invoice_id=ETTN,
        invoice=_invoice(),
    )


def _review_item(*, version: int = 2) -> ReviewItem:
    return ReviewItem(
        review_id=REVIEW_ID,
        invoice_id=ETTN,
        invoice_number="AKY-1",
        supplier_tax_number=VKN,
        supplier_name="AKYASAM",
        invoice_date=date(2026, 8, 20),
        currency="TRY",
        total_amount=Decimal("100.00"),
        workflow=WorkflowType.MANUAL_REVIEW,
        status=ReviewStatus.PENDING_REVIEW,
        review_reasons=(
            ManualReviewReason(
                code=ManualReviewReasonCode.PRODUCT_NOT_FOUND,
                message="Product was not matched deterministically.",
                line_number="1",
                source="product_matching",
                candidate_count=0,
            ),
        ),
        version=version,
    )


def _remediation_effect() -> SupplierRemediationEffect:
    return SupplierRemediationEffect(
        review_id=REVIEW_ID,
        company_id=COMPANY_ID,
        review_version=1,
        source_invoice_id=ETTN,
        mode=SupplierResolutionMode.MATCH_EXISTING,
        resolved_partner_id=PARTNER_ID,
        partner_write_status=SupplierPartnerWriteEffectStatus.SELECTED,
    )


class _FakeReviewReader:
    def __init__(self, item: ReviewItem) -> None:
        self.item = item

    def get_review_item(self, query: ReviewDetailQuery) -> ReviewItem:
        if query.review_id != self.item.review_id:
            raise ReviewNotFoundError("Review item was not found.")
        return self.item


class _FakeSourceReader:
    def __init__(self, evidence: ReviewSourceInvoiceEvidence) -> None:
        self.evidence = evidence

    def get(self, *, review_id: str, company_id: int) -> ReviewSourceInvoiceEvidence:
        return self.evidence


class _FakeExistingSupplierInfoReader:
    async def find_existing(
        self, *, partner_id: int, product_code: str, company_id: int
    ) -> tuple[ExistingSupplierInfo, ...]:
        return ()


class _FakeProductWriter:
    """Records ``command.authorization`` on every call; can be forced to raise."""

    def __init__(self, *, fail: Exception | None = None) -> None:
        self.calls: list[CreateProductCommand] = []
        self.fail = fail
        self._next_template_id = 9001
        self._next_product_id = 9101

    async def create_product(self, command: CreateProductCommand) -> ProductWriteResult:
        self.calls.append(command)
        if self.fail is not None:
            raise self.fail
        result = ProductWriteResult(
            status=ProductWriteStatus.CREATED,
            template_id=self._next_template_id,
            product_id=self._next_product_id,
            name=command.name,
            default_code=command.default_code,
            safe_message="Product created in Odoo.",
        )
        self._next_template_id += 1
        self._next_product_id += 1
        return result


class _FakeSupplierInfoWriter:
    """Records ``command.authorization`` on every call; can be forced to raise."""

    def __init__(self, *, fail: Exception | None = None) -> None:
        self.calls: list[CreateSupplierInfoCommand] = []
        self.fail = fail
        self._next_id = 9501

    async def create_supplier_info(self, command: CreateSupplierInfoCommand) -> SupplierInfoWriteResult:
        self.calls.append(command)
        if self.fail is not None:
            raise self.fail
        result = SupplierInfoWriteResult(
            status=SupplierInfoWriteStatus.CREATED,
            supplierinfo_id=self._next_id,
            partner_id=command.partner_id,
            product_tmpl_id=command.product_tmpl_id,
            product_code=command.product_code,
            company_id=command.company_id,
            idempotency_key=command.idempotency_key,
            product_id=command.product_id,
        )
        self._next_id += 1
        return result


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=TABLES)
    factory = sessionmaker(bind=engine)
    with factory() as db_session:
        db_session.add(
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
                review_reasons=[{"code": "product_not_found", "message": "x", "line_number": "1"}],
                warnings=[],
                version=2,
                idempotency_key=f"uyumsoft:{COMPANY_ID}:{ETTN}",
            )
        )
        db_session.flush()
        yield db_session


class _Harness:
    def __init__(
        self,
        session: Session,
        *,
        product_writer: _FakeProductWriter | None = None,
        supplier_info_writer: _FakeSupplierInfoWriter | None = None,
        with_write_authorization_repository: bool = True,
    ) -> None:
        self.session = session
        self.reader = _FakeReviewReader(_review_item())
        self.source_reader = _FakeSourceReader(_source_evidence())
        self.effect_repo = SqlAlchemyReviewSupplierRemediationEffectRepository(session)
        self.effect_repo.create_remediation_effect(_remediation_effect())
        session.commit()
        self.reservation_repo = SqlAlchemyReviewProductRemediationReservationRepository(session)
        self.claim_repo = SqlAlchemyReviewProductIdentityClaimRepository(session)
        self.product_writer = product_writer or _FakeProductWriter()
        self.supplier_info_writer = supplier_info_writer or _FakeSupplierInfoWriter()
        self.write_auth_repo = (
            SqlAlchemyWriteAuthorizationRepository(session) if with_write_authorization_repository else None
        )
        self.use_case = CreateNewProductUseCase(
            review_reader=self.reader,
            source_invoice_reader=self.source_reader,
            remediation_effect_reader=self.effect_repo,
            reservation_writer=self.reservation_repo,
            identity_claim_writer=self.claim_repo,
            existing_supplier_info_reader=_FakeExistingSupplierInfoReader(),
            product_writer=self.product_writer,
            supplier_info_writer=self.supplier_info_writer,
            unit_of_work=SqlAlchemyUnitOfWork(session),
            write_authorization_repository=self.write_auth_repo,
        )

    def command(self, **kw) -> CreateNewProductCommand:
        base = {
            "review_id": REVIEW_ID,
            "company_id": COMPANY_ID,
            "expected_version": 2,
            "line_number": "1",
            "product_name": "Yillik Aidat Urunu",
            "product_type": "service",
            "uom_id": 1,
            "approved_by": ACTOR,
        }
        base.update(kw)
        return CreateNewProductCommand(**base)

    def issue_authorization(
        self,
        *,
        authorization_id: str = "auth-1",
        operation_type: WriteAuthorizationOperationType = WriteAuthorizationOperationType.CREATE_NEW_PRODUCT,
        target_version: int = 2,
        review_id: str = REVIEW_ID,
        company_id: int = COMPANY_ID,
        expires_at: datetime | None = None,
    ) -> WriteAuthorizationRecord:
        record = self.write_auth_repo.create(
            authorization_id=authorization_id,
            company_id=company_id,
            review_id=review_id,
            operation_type=operation_type,
            target_version=target_version,
            authorized_by=ACTOR,
            expires_at=expires_at or datetime.now(UTC) + timedelta(minutes=15),
        )
        self.session.commit()
        return record


# =================================================================== A: application layer


async def test_authorized_create_claims_and_threads_authorization_into_both_writes(session: Session) -> None:
    h = _Harness(session)
    h.issue_authorization()

    result = await h.use_case.execute(h.command(authorization_id="auth-1"))

    assert result.status is ProductRemediationStatus.COMPLETED
    assert len(h.product_writer.calls) == 1
    assert len(h.supplier_info_writer.calls) == 1
    product_authorization = h.product_writer.calls[0].authorization
    supplierinfo_authorization = h.supplier_info_writer.calls[0].authorization
    assert product_authorization is not None
    assert supplierinfo_authorization is not None
    assert product_authorization.authorization_id == "auth-1"
    assert supplierinfo_authorization.authorization_id == "auth-1"

    persisted = h.write_auth_repo.get_by_id(authorization_id="auth-1", company_id=COMPANY_ID)
    assert persisted.status is WriteAuthorizationStatus.CONSUMED
    # Claimed once before each of the two underlying Odoo writes -- same
    # authorization, idempotently re-consumed.
    assert persisted.use_count == 2


async def test_without_authorization_id_commands_carry_no_authorization(session: Session) -> None:
    h = _Harness(session)
    result = await h.use_case.execute(h.command())
    assert result.status is ProductRemediationStatus.COMPLETED
    assert h.product_writer.calls[0].authorization is None
    assert h.supplier_info_writer.calls[0].authorization is None


async def test_authorization_id_without_a_configured_repository_fails_closed(session: Session) -> None:
    h = _Harness(session, with_write_authorization_repository=False)
    with pytest.raises(ProductRemediationContractError):
        await h.use_case.execute(h.command(authorization_id="auth-1"))


async def test_certain_no_write_failure_rolls_back_before_reverting_reservation(session: Session) -> None:
    """P0-PROD-09G regression: a certain-no-write failure (e.g. the master kill
    switch) must not burn the authorization -- the reservation reverts to
    RESERVED and the authorization stays PENDING/unused, so a later retry with
    the same authorization_id can still succeed once the gate is fixed."""

    h = _Harness(session, product_writer=_FakeProductWriter(fail=ProductWriteSafetyGateError("gate closed")))
    h.issue_authorization()

    with pytest.raises(ProductWriteSafetyGateError):
        await h.use_case.execute(h.command(authorization_id="auth-1"))

    reservation = h.reservation_repo.find(review_id=REVIEW_ID, company_id=COMPANY_ID, review_version=2, line_number="1")
    assert reservation is not None
    assert reservation.status is ProductReservationStatus.RESERVED

    persisted = h.write_auth_repo.get_by_id(authorization_id="auth-1", company_id=COMPANY_ID)
    assert persisted.status is WriteAuthorizationStatus.PENDING
    assert persisted.use_count == 0

    # The authorization itself remains usable once the real misconfiguration is
    # fixed -- claim_and_consume still accepts it against the same scope/execution.
    reclaimed = h.write_auth_repo.claim_and_consume(
        company_id=COMPANY_ID,
        review_id=REVIEW_ID,
        operation_type=WriteAuthorizationOperationType.CREATE_NEW_PRODUCT,
        target_version=2,
        authorization_id="auth-1",
        execution_id=product_remediation_authorization_consumer_id(
            company_id=COMPANY_ID, review_id=REVIEW_ID, expected_version=2, line_number="1"
        ),
    )
    assert reclaimed.status is WriteAuthorizationStatus.CONSUMED


async def test_uncertain_product_write_failure_leaves_reservation_attempted_and_authorization_unconsumed(
    session: Session,
) -> None:
    h = _Harness(session, product_writer=_FakeProductWriter(fail=RuntimeError("timeout")))
    h.issue_authorization()

    with pytest.raises(RuntimeError):
        await h.use_case.execute(h.command(authorization_id="auth-1"))

    reservation = h.reservation_repo.find(review_id=REVIEW_ID, company_id=COMPANY_ID, review_version=2, line_number="1")
    assert reservation is not None
    assert reservation.status is ProductReservationStatus.CREATE_ATTEMPTED

    persisted = h.write_auth_repo.get_by_id(authorization_id="auth-1", company_id=COMPANY_ID)
    assert persisted.status is WriteAuthorizationStatus.PENDING


async def test_resume_from_product_created_reclaims_same_authorization_for_supplierinfo_write(
    session: Session,
) -> None:
    """A crash right after PRODUCT_CREATED, before supplierinfo, resumes on a new
    request that re-supplies the same authorization_id -- claim_and_consume's own
    idempotent-resume semantics let the same authorization cover the second write."""

    h = _Harness(session)
    h.issue_authorization()
    reservation = h.reservation_repo.reserve(
        ProductRemediationReservation(
            review_id=REVIEW_ID,
            company_id=COMPANY_ID,
            review_version=2,
            line_number="1",
            status=ProductReservationStatus.PRODUCT_CREATED,
            resolved_supplier_partner_id=PARTNER_ID,
            seller_item_code=SELLER_ITEM_CODE,
            product_name="Yillik Aidat Urunu",
            is_storable=False,
            approved_by=ACTOR,
            product_template_id=9001,
            product_id=9101,
        )
    )
    session.commit()
    assert reservation.status is ProductReservationStatus.PRODUCT_CREATED

    result = await h.use_case.execute(h.command(authorization_id="auth-1"))
    assert result.status is ProductRemediationStatus.COMPLETED
    assert len(h.product_writer.calls) == 0
    assert len(h.supplier_info_writer.calls) == 1
    assert h.supplier_info_writer.calls[0].authorization is not None

    persisted = h.write_auth_repo.get_by_id(authorization_id="auth-1", company_id=COMPANY_ID)
    assert persisted.status is WriteAuthorizationStatus.CONSUMED
    assert persisted.use_count == 1


# =================================================================== B: repository-level


def test_claim_rejects_wrong_review_scope(session: Session) -> None:
    h = _Harness(session)
    h.issue_authorization(review_id=REVIEW_ID)
    with pytest.raises(WriteAuthorizationScopeMismatchError):
        h.write_auth_repo.claim_and_consume(
            company_id=COMPANY_ID,
            review_id=OTHER_REVIEW_ID,
            operation_type=WriteAuthorizationOperationType.CREATE_NEW_PRODUCT,
            target_version=2,
            authorization_id="auth-1",
            execution_id="exec-1",
        )


def test_claim_rejects_wrong_version_scope(session: Session) -> None:
    h = _Harness(session)
    h.issue_authorization(target_version=2)
    with pytest.raises(WriteAuthorizationScopeMismatchError):
        h.write_auth_repo.claim_and_consume(
            company_id=COMPANY_ID,
            review_id=REVIEW_ID,
            operation_type=WriteAuthorizationOperationType.CREATE_NEW_PRODUCT,
            target_version=99,
            authorization_id="auth-1",
            execution_id="exec-1",
        )


def test_claim_rejects_wrong_operation_scope(session: Session) -> None:
    h = _Harness(session)
    h.issue_authorization(operation_type=WriteAuthorizationOperationType.CREATE_PERMANENT_SUPPLIER)
    with pytest.raises(WriteAuthorizationScopeMismatchError):
        h.write_auth_repo.claim_and_consume(
            company_id=COMPANY_ID,
            review_id=REVIEW_ID,
            operation_type=WriteAuthorizationOperationType.CREATE_NEW_PRODUCT,
            target_version=2,
            authorization_id="auth-1",
            execution_id="exec-1",
        )


def test_claim_rejects_wrong_company_even_when_authorization_id_is_a_guess(session: Session) -> None:
    h = _Harness(session)
    h.issue_authorization(company_id=COMPANY_ID)
    with pytest.raises(WriteAuthorizationNotFoundError):
        h.write_auth_repo.claim_and_consume(
            company_id=OTHER_COMPANY_ID,
            review_id=REVIEW_ID,
            operation_type=WriteAuthorizationOperationType.CREATE_NEW_PRODUCT,
            target_version=2,
            authorization_id="auth-1",
            execution_id="exec-1",
        )


def test_claim_rejects_expired_authorization(session: Session) -> None:
    h = _Harness(session)
    h.issue_authorization()
    model = session.query(WorkbenchReviewWriteAuthorization).filter_by(authorization_id="auth-1").one()
    model.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    session.commit()

    with pytest.raises(WriteAuthorizationExpiredError):
        h.write_auth_repo.claim_and_consume(
            company_id=COMPANY_ID,
            review_id=REVIEW_ID,
            operation_type=WriteAuthorizationOperationType.CREATE_NEW_PRODUCT,
            target_version=2,
            authorization_id="auth-1",
            execution_id="exec-1",
        )


def test_claim_rejects_revoked_authorization(session: Session) -> None:
    h = _Harness(session)
    h.issue_authorization()
    h.write_auth_repo.revoke(authorization_id="auth-1", company_id=COMPANY_ID, revoked_by=ACTOR, review_id=REVIEW_ID)
    session.commit()

    with pytest.raises(WriteAuthorizationRevokedError):
        h.write_auth_repo.claim_and_consume(
            company_id=COMPANY_ID,
            review_id=REVIEW_ID,
            operation_type=WriteAuthorizationOperationType.CREATE_NEW_PRODUCT,
            target_version=2,
            authorization_id="auth-1",
            execution_id="exec-1",
        )


def test_claim_replay_with_same_execution_id_is_idempotent(session: Session) -> None:
    h = _Harness(session)
    h.issue_authorization()
    kwargs = {
        "company_id": COMPANY_ID,
        "review_id": REVIEW_ID,
        "operation_type": WriteAuthorizationOperationType.CREATE_NEW_PRODUCT,
        "target_version": 2,
        "authorization_id": "auth-1",
        "execution_id": "exec-1",
    }
    first = h.write_auth_repo.claim_and_consume(**kwargs)
    session.commit()
    second = h.write_auth_repo.claim_and_consume(**kwargs)
    session.commit()

    assert first.status is WriteAuthorizationStatus.CONSUMED
    assert second.status is WriteAuthorizationStatus.CONSUMED
    assert second.use_count == 2


def test_claim_rejects_reuse_by_a_different_execution(session: Session) -> None:
    """Single authorization for one review line: a different line's execution_id
    (e.g. a different line_number) can never piggyback on an already-consumed one."""

    h = _Harness(session)
    h.issue_authorization()
    h.write_auth_repo.claim_and_consume(
        company_id=COMPANY_ID,
        review_id=REVIEW_ID,
        operation_type=WriteAuthorizationOperationType.CREATE_NEW_PRODUCT,
        target_version=2,
        authorization_id="auth-1",
        execution_id=product_remediation_authorization_consumer_id(
            company_id=COMPANY_ID, review_id=REVIEW_ID, expected_version=2, line_number="1"
        ),
    )
    session.commit()

    with pytest.raises(WriteAuthorizationAlreadyConsumedError):
        h.write_auth_repo.claim_and_consume(
            company_id=COMPANY_ID,
            review_id=REVIEW_ID,
            operation_type=WriteAuthorizationOperationType.CREATE_NEW_PRODUCT,
            target_version=2,
            authorization_id="auth-1",
            execution_id=product_remediation_authorization_consumer_id(
                company_id=COMPANY_ID, review_id=REVIEW_ID, expected_version=2, line_number="2"
            ),
        )


def test_consumer_id_helper_is_deterministic_and_line_scoped() -> None:
    kwargs = {"company_id": COMPANY_ID, "review_id": REVIEW_ID, "expected_version": 2}
    first = product_remediation_authorization_consumer_id(line_number="1", **kwargs)
    again = product_remediation_authorization_consumer_id(line_number="1", **kwargs)
    other_line = product_remediation_authorization_consumer_id(line_number="2", **kwargs)
    assert first == again
    assert first != other_line


# --------------------------------------------------------- policy gate-bypass shape


def _authorization_record(**overrides) -> WriteAuthorizationRecord:
    now = datetime.now(UTC)
    base = {
        "authorization_id": "auth-1",
        "company_id": COMPANY_ID,
        "review_id": REVIEW_ID,
        "operation_type": WriteAuthorizationOperationType.CREATE_NEW_PRODUCT,
        "target_version": 2,
        "status": WriteAuthorizationStatus.CONSUMED,
        "authorized_by": ACTOR,
        "created_at": now - timedelta(minutes=1),
        "expires_at": now + timedelta(minutes=14),
        "consumed_at": now,
        "consumed_by_execution_id": "exec-1",
        "use_count": 1,
    }
    base.update(overrides)
    return WriteAuthorizationRecord(**base)


def test_policy_bypasses_only_the_feature_flag_when_authorization_present() -> None:
    policy = OdooProductWritePolicy(
        product_remediation_write_enabled=False,
        production_operations_enabled=True,
        production_approval_ack=OdooProductWritePolicy().required_approval_ack,
    )
    policy.ensure_real_write_allowed(approved_by=ACTOR, write_authorization=_authorization_record())


def test_policy_kill_switch_still_blocks_write_with_valid_authorization() -> None:
    policy = OdooProductWritePolicy(
        product_remediation_write_enabled=False,
        production_operations_enabled=False,
        production_approval_ack=OdooProductWritePolicy().required_approval_ack,
    )
    with pytest.raises(ProductWriteSafetyGateError):
        policy.ensure_real_write_allowed(approved_by=ACTOR, write_authorization=_authorization_record())


def test_policy_approval_ack_still_required_with_valid_authorization() -> None:
    policy = OdooProductWritePolicy(
        product_remediation_write_enabled=False,
        production_operations_enabled=True,
        production_approval_ack="wrong-ack",
    )
    with pytest.raises(ProductWriteSafetyGateError):
        policy.ensure_real_write_allowed(approved_by=ACTOR, write_authorization=_authorization_record())


@pytest.mark.parametrize("approved_by", [None, "   "])
def test_policy_named_approver_still_required_with_valid_authorization(approved_by: str | None) -> None:
    policy = OdooProductWritePolicy(
        product_remediation_write_enabled=False,
        production_operations_enabled=True,
        production_approval_ack=OdooProductWritePolicy().required_approval_ack,
    )
    with pytest.raises(ProductWriteSafetyGateError):
        policy.ensure_real_write_allowed(approved_by=approved_by, write_authorization=_authorization_record())


def test_policy_without_authorization_still_requires_the_feature_flag() -> None:
    policy = OdooProductWritePolicy(
        product_remediation_write_enabled=False,
        production_operations_enabled=True,
        production_approval_ack=OdooProductWritePolicy().required_approval_ack,
    )
    with pytest.raises(ProductWriteSafetyGateError):
        policy.ensure_real_write_allowed(approved_by=ACTOR, write_authorization=None)


# =================================================================== C: HTTP-level wiring smoke test


class _ProductRemediationResultStub:
    review_id = REVIEW_ID
    company_id = COMPANY_ID
    review_version = 2
    line_number = "1"
    status = ProductRemediationStatus.COMPLETED
    product_template_id = 9001
    product_id = 9101
    supplierinfo_id = 9501
    created_product = True
    created_supplierinfo = True
    reused_existing_product = False
    already_applied = False
    safe_message = "ok"


class _FakeCreateNewProductUseCase:
    def __init__(self) -> None:
        self.calls: list[CreateNewProductCommand] = []

    async def execute(self, command: CreateNewProductCommand):
        self.calls.append(command)
        return _ProductRemediationResultStub()


def test_product_resolution_authorization_id_reaches_the_command() -> None:
    fake_use_case = _FakeCreateNewProductUseCase()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[dependencies.get_create_new_product_use_case] = lambda: fake_use_case
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
            f"/api/workbench/reviews/{REVIEW_ID}/product-resolution",
            json={
                "mode": "create_new_product",
                "expected_version": 2,
                "line_number": "1",
                "product_name": "Yillik Aidat Urunu",
                "product_type": "service",
                "uom_id": 1,
                "authorization_id": "11111111-1111-1111-1111-111111111111",
            },
        )
    assert response.status_code == 200, response.text
    assert len(fake_use_case.calls) == 1
    assert fake_use_case.calls[0].authorization_id == "11111111-1111-1111-1111-111111111111"
