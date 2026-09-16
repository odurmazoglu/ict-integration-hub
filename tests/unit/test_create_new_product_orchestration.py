"""Crash-safe, retry-safe, concurrency-safe CREATE_NEW_PRODUCT orchestration (P0-PROD-07G).

Covers the full test matrix from the P0-PROD-07G brief: fresh success, full replay,
same-review-line concurrency, cross-review supplier-product identity concurrency,
pre-existing Odoo master data reuse, crash/resume at every state-machine boundary,
the uncertain-remote-outcome reconciliation path, fail-closed eligibility/identity
checks, seller_item_code/default_code separation, the dedicated write gate, and the
DB-enforced uniqueness of both reservation identities.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.application.commands.product_remediation import CreateProductCommand, CreateSupplierInfoCommand
from app.application.dto.product_remediation import (
    ProductWriteResult,
    ProductWriteStatus,
    SupplierInfoWriteResult,
    SupplierInfoWriteStatus,
)
from app.application.exceptions.product_remediation import (
    ProductWriteSafetyGateError,
    ProductWriteTransportError,
)
from app.application.workbench.dto import ReviewItem, ReviewStatus
from app.application.workbench.evidence import ReviewSourceInvoiceEvidence
from app.application.workbench.exceptions import (
    ProductRemediationContractError,
    ProductRemediationEligibilityError,
    ProductRemediationIdentityAmbiguousError,
    ProductRemediationRaceError,
    ProductRemediationSupplierUnresolvedError,
    ReviewNotFoundError,
)
from app.application.workbench.product_remediation import (
    CreateNewProductCommand,
    ExistingSupplierInfo,
    ProductIdentityClaim,
    ProductRemediationReservation,
    ProductRemediationStatus,
    ProductReservationStatus,
)
from app.application.workbench.product_remediation_use_cases import CreateNewProductUseCase
from app.application.workbench.queries import ReviewDetailQuery
from app.application.workbench.supplier_remediation import (
    SupplierPartnerWriteEffectStatus,
    SupplierRemediationEffect,
)
from app.application.workbench.supplier_resolution import SupplierResolutionMode
from app.application.workflow import ManualReviewReason, ManualReviewReasonCode, WorkflowType
from app.db.base import Base
from app.domain.invoice import Header, InternalInvoice, InvoiceLine, MonetaryTotals, Party, Tax
from app.models.workbench_review_item import WorkbenchReviewItem
from app.models.workbench_review_product_identity_claim import WorkbenchReviewProductIdentityClaim
from app.models.workbench_review_product_remediation_reservation import (
    WorkbenchReviewProductRemediationReservation,
)
from app.models.workbench_review_supplier_remediation_effect import WorkbenchReviewSupplierRemediationEffect
from app.persistence import (
    SqlAlchemyReviewProductIdentityClaimRepository,
    SqlAlchemyReviewProductRemediationReservationRepository,
    SqlAlchemyReviewSupplierRemediationEffectRepository,
    SqlAlchemyUnitOfWork,
)

COMPANY_ID = 7
REVIEW_ID = "review:product-remediation-1"
REVIEW_ID_2 = "review:product-remediation-2"
ETTN = "AKYASAM-ETTN-PRODREM-1"
ETTN_2 = "AKYASAM-ETTN-PRODREM-2"
VKN = "0430367181"
PARTNER_ID = 4010
ACTOR = "finance.operator"
SELLER_ITEM_CODE = "SKU-100"

TABLES = [
    WorkbenchReviewItem.__table__,
    WorkbenchReviewSupplierRemediationEffect.__table__,
    WorkbenchReviewProductRemediationReservation.__table__,
    WorkbenchReviewProductIdentityClaim.__table__,
]


# --------------------------------------------------------------------------- builders


def _invoice(
    *, line_number: str = "1", seller_item_code: str | None = SELLER_ITEM_CODE, ettn: str = ETTN
) -> InternalInvoice:
    return InternalInvoice(
        header=Header(
            invoice_number="AKY-1",
            invoice_uuid="00000000-0000-4000-8000-00000000e001",
            ettn=ettn,
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
                seller_item_code=seller_item_code,
                quantity=Decimal("1"),
                unit_code="C62",
                unit_price=Decimal("83.33"),
                taxes=(Tax(tax_type="KDV", rate=Decimal("20")),),
            ),
        ),
    )


def _source_evidence(*, review_id: str = REVIEW_ID, ettn: str = ETTN, **invoice_kwargs) -> ReviewSourceInvoiceEvidence:
    return ReviewSourceInvoiceEvidence(
        review_id=review_id,
        company_id=COMPANY_ID,
        review_version=1,
        source_invoice_id=ettn,
        invoice=_invoice(ettn=ettn, **invoice_kwargs),
    )


def _product_not_found_reason(line_number: str = "1") -> ManualReviewReason:
    return ManualReviewReason(
        code=ManualReviewReasonCode.PRODUCT_NOT_FOUND,
        message="Product was not matched deterministically.",
        line_number=line_number,
        source="product_matching",
        candidate_count=0,
    )


def _review_item(
    *,
    review_id: str = REVIEW_ID,
    ettn: str = ETTN,
    version: int = 2,
    status: ReviewStatus = ReviewStatus.PENDING_REVIEW,
    reasons: tuple[ManualReviewReason, ...] | None = None,
) -> ReviewItem:
    return ReviewItem(
        review_id=review_id,
        invoice_id=ettn,
        invoice_number="AKY-1",
        supplier_tax_number=VKN,
        supplier_name="AKYASAM",
        invoice_date=date(2026, 8, 20),
        currency="TRY",
        total_amount=Decimal("100.00"),
        workflow=WorkflowType.MANUAL_REVIEW,
        status=status,
        review_reasons=reasons if reasons is not None else (_product_not_found_reason(),),
        version=version,
    )


def _remediation_effect(
    *,
    review_id: str = REVIEW_ID,
    partner_id: int = PARTNER_ID,
) -> SupplierRemediationEffect:
    return SupplierRemediationEffect(
        review_id=review_id,
        company_id=COMPANY_ID,
        review_version=1,
        source_invoice_id=ETTN,
        mode=SupplierResolutionMode.MATCH_EXISTING,
        resolved_partner_id=partner_id,
        partner_write_status=SupplierPartnerWriteEffectStatus.SELECTED,
    )


class _FakeReviewReader:
    def __init__(self, item: ReviewItem) -> None:
        self.item = item
        self.calls = 0

    def get_review_item(self, query: ReviewDetailQuery) -> ReviewItem:
        self.calls += 1
        if query.review_id != self.item.review_id:
            raise ReviewNotFoundError("Review item was not found.")
        return self.item


class _FakeSourceReader:
    def __init__(self, evidence: ReviewSourceInvoiceEvidence) -> None:
        self.evidence = evidence

    def get(self, *, review_id: str, company_id: int) -> ReviewSourceInvoiceEvidence:
        if review_id != self.evidence.review_id:
            raise ReviewNotFoundError("Source invoice evidence was not found.")
        return self.evidence


class _FakeProductWriter:
    def __init__(
        self, *, next_template_id: int = 9001, next_product_id: int = 9101, fail: Exception | None = None
    ) -> None:
        self.calls: list[CreateProductCommand] = []
        self._next_template_id = next_template_id
        self._next_product_id = next_product_id
        self.fail = fail

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
    def __init__(self, *, next_id: int = 9501, fail: Exception | None = None) -> None:
        self.calls: list[CreateSupplierInfoCommand] = []
        self._next_id = next_id
        self.fail = fail
        self._created: dict[tuple[int, str], int] = {}

    async def create_supplier_info(self, command: CreateSupplierInfoCommand) -> SupplierInfoWriteResult:
        self.calls.append(command)
        if self.fail is not None:
            raise self.fail
        key = (command.partner_id, command.product_code)
        if key in self._created:
            return SupplierInfoWriteResult(
                status=SupplierInfoWriteStatus.ALREADY_EXISTS,
                supplierinfo_id=self._created[key],
                partner_id=command.partner_id,
                product_tmpl_id=command.product_tmpl_id,
                product_code=command.product_code,
                company_id=command.company_id,
                idempotency_key=command.idempotency_key,
                product_id=command.product_id,
            )
        self._created[key] = self._next_id
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


class _FakeExistingSupplierInfoReader:
    def __init__(self, *, records: tuple[ExistingSupplierInfo, ...] = ()) -> None:
        self.records = records
        self.calls: list[tuple[int, str, int]] = []

    async def find_existing(
        self, *, partner_id: int, product_code: str, company_id: int
    ) -> tuple[ExistingSupplierInfo, ...]:
        self.calls.append((partner_id, product_code, company_id))
        return self.records


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=TABLES)
    factory = sessionmaker(bind=engine)
    with factory() as db_session:
        _seed_review_item(db_session, review_id=REVIEW_ID, ettn=ETTN)
        db_session.flush()
        yield db_session


def _seed_review_item(db_session: Session, *, review_id: str, ettn: str) -> None:
    db_session.add(
        WorkbenchReviewItem(
            review_id=review_id,
            company_id=COMPANY_ID,
            invoice_id=ettn,
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
            idempotency_key=f"uyumsoft:{COMPANY_ID}:{ettn}",
        )
    )


class _Harness:
    def __init__(
        self,
        session: Session,
        *,
        review: ReviewItem | None = None,
        source: ReviewSourceInvoiceEvidence | None = None,
        effect: SupplierRemediationEffect | None | bool = None,
        product_writer: _FakeProductWriter | None = None,
        supplier_info_writer: _FakeSupplierInfoWriter | None = None,
        existing_reader: _FakeExistingSupplierInfoReader | None = None,
        after_precheck_hook=None,
    ) -> None:
        self.session = session
        self.reader = _FakeReviewReader(review or _review_item())
        self.source_reader = _FakeSourceReader(source or _source_evidence())
        self.effect_repo = SqlAlchemyReviewSupplierRemediationEffectRepository(session)
        if effect is not False:
            self.effect_repo.create_remediation_effect(effect or _remediation_effect())
            session.commit()
        self.reservation_repo = SqlAlchemyReviewProductRemediationReservationRepository(session)
        self.claim_repo = SqlAlchemyReviewProductIdentityClaimRepository(session)
        self.product_writer = product_writer or _FakeProductWriter()
        self.supplier_info_writer = supplier_info_writer or _FakeSupplierInfoWriter()
        self.existing_reader = existing_reader or _FakeExistingSupplierInfoReader()
        self.use_case = CreateNewProductUseCase(
            review_reader=self.reader,
            source_invoice_reader=self.source_reader,
            remediation_effect_reader=self.effect_repo,
            reservation_writer=self.reservation_repo,
            identity_claim_writer=self.claim_repo,
            existing_supplier_info_reader=self.existing_reader,
            product_writer=self.product_writer,
            supplier_info_writer=self.supplier_info_writer,
            unit_of_work=SqlAlchemyUnitOfWork(session),
            _after_precheck_hook=after_precheck_hook,
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


# --------------------------------------------------------------------------- A, N, O


async def test_a_eligible_line_creates_product_and_supplierinfo_once(session: Session) -> None:
    h = _Harness(session)
    result = await h.use_case.execute(h.command())

    assert result.status is ProductRemediationStatus.COMPLETED
    assert result.created_product is True
    assert result.created_supplierinfo is True
    assert result.reused_existing_product is False
    assert result.already_applied is False
    assert result.product_template_id == 9001
    assert result.product_id == 9101
    assert result.supplierinfo_id == 9501
    assert len(h.product_writer.calls) == 1
    assert len(h.supplier_info_writer.calls) == 1
    assert h.supplier_info_writer.calls[0].product_code == SELLER_ITEM_CODE
    assert h.supplier_info_writer.calls[0].partner_id == PARTNER_ID

    persisted = h.reservation_repo.find(review_id=REVIEW_ID, company_id=COMPANY_ID, review_version=2, line_number="1")
    assert persisted is not None
    assert persisted.status is ProductReservationStatus.COMPLETED


async def test_n_no_internal_reference_never_copies_seller_item_code(session: Session) -> None:
    h = _Harness(session)
    await h.use_case.execute(h.command())
    assert h.product_writer.calls[0].default_code is None


async def test_o_explicit_internal_reference_preserved_exactly(session: Session) -> None:
    h = _Harness(session)
    await h.use_case.execute(h.command(internal_reference="ICT-SKU-0099"))
    assert h.product_writer.calls[0].default_code == "ICT-SKU-0099"


async def test_operator_confirmed_product_type_service_reaches_odoo_command(session: Session) -> None:
    h = _Harness(session)
    result = await h.use_case.execute(h.command(product_type="service"))
    assert result.status is ProductRemediationStatus.COMPLETED
    assert h.product_writer.calls[0].type == "service"


async def test_operator_confirmed_product_type_consu_reaches_odoo_command(session: Session) -> None:
    h = _Harness(session)
    result = await h.use_case.execute(h.command(product_type="consu"))
    assert result.status is ProductRemediationStatus.COMPLETED
    assert h.product_writer.calls[0].type == "consu"


async def test_product_type_is_never_guessed_invalid_value_fails_closed_before_any_write(session: Session) -> None:
    h = _Harness(session)
    with pytest.raises(ProductRemediationContractError):
        await h.use_case.execute(h.command(product_type="combo"))
    assert h.product_writer.calls == []
    assert (
        h.reservation_repo.find(review_id=REVIEW_ID, company_id=COMPANY_ID, review_version=2, line_number="1") is None
    )


# --------------------------------------------------------------------------- B


async def test_b_full_replay_after_completion_makes_no_odoo_calls(session: Session) -> None:
    h = _Harness(session)
    first = await h.use_case.execute(h.command())
    second = await h.use_case.execute(h.command())

    assert second.already_applied is True
    assert second.status is first.status is ProductRemediationStatus.COMPLETED
    assert second.product_template_id == first.product_template_id
    assert second.product_id == first.product_id
    assert second.supplierinfo_id == first.supplierinfo_id
    assert len(h.product_writer.calls) == 1
    assert len(h.supplier_info_writer.calls) == 1


# --------------------------------------------------------------------------- E


async def test_e_existing_supplierinfo_is_reused_with_zero_product_creates(session: Session) -> None:
    existing = ExistingSupplierInfo(
        id=7001,
        partner_id=PARTNER_ID,
        product_tmpl_id=8001,
        product_id=8101,
        product_code=SELLER_ITEM_CODE,
        company_id=COMPANY_ID,
    )
    h = _Harness(session, existing_reader=_FakeExistingSupplierInfoReader(records=(existing,)))
    result = await h.use_case.execute(h.command())

    assert result.status is ProductRemediationStatus.COMPLETED
    assert result.reused_existing_product is True
    assert result.created_product is False
    assert result.created_supplierinfo is False
    assert result.product_template_id == 8001
    assert result.product_id == 8101
    assert result.supplierinfo_id == 7001
    assert h.product_writer.calls == []
    assert h.supplier_info_writer.calls == []


# --------------------------------------------------------------------------- F, G


async def test_f_supplierinfo_failure_after_product_persisted_then_retry_resumes(session: Session) -> None:
    failing = _FakeSupplierInfoWriter(fail=ProductWriteTransportError("timeout"))
    h = _Harness(session, supplier_info_writer=failing)
    with pytest.raises(ProductWriteTransportError):
        await h.use_case.execute(h.command())

    persisted = h.reservation_repo.find(review_id=REVIEW_ID, company_id=COMPANY_ID, review_version=2, line_number="1")
    assert persisted.status is ProductReservationStatus.PRODUCT_CREATED
    assert len(h.product_writer.calls) == 1

    h.supplier_info_writer = _FakeSupplierInfoWriter()
    h.use_case._supplier_info_writer = h.supplier_info_writer  # swap in a working writer for the retry
    result = await h.use_case.execute(h.command())

    assert result.status is ProductRemediationStatus.COMPLETED
    assert result.created_product is False  # resumed, not re-created
    assert result.created_supplierinfo is True
    assert len(h.product_writer.calls) == 1  # never called again


async def test_g_crash_after_product_created_before_supplierinfo_then_resume(session: Session) -> None:
    h = _Harness(session)
    h.reservation_repo.reserve(
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
            product_template_id=5001,
            product_id=5101,
        )
    )
    session.commit()

    result = await h.use_case.execute(h.command())

    assert result.status is ProductRemediationStatus.COMPLETED
    assert result.product_template_id == 5001
    assert result.product_id == 5101
    assert h.product_writer.calls == []  # never attempted -- resumed directly into supplierinfo
    assert len(h.supplier_info_writer.calls) == 1


# --------------------------------------------------------------------------- H


async def test_h_uncertain_create_outcome_never_blindly_retries(session: Session) -> None:
    unreliable = _FakeProductWriter(fail=ProductWriteTransportError("connection reset"))
    h = _Harness(session, product_writer=unreliable)
    with pytest.raises(ProductWriteTransportError):
        await h.use_case.execute(h.command())
    assert len(h.product_writer.calls) == 1

    persisted = h.reservation_repo.find(review_id=REVIEW_ID, company_id=COMPANY_ID, review_version=2, line_number="1")
    assert persisted.status is ProductReservationStatus.CREATE_ATTEMPTED

    result = await h.use_case.execute(h.command())

    assert result.status is ProductRemediationStatus.RECONCILIATION_REQUIRED
    assert result.already_applied is True
    assert len(h.product_writer.calls) == 1  # never retried
    assert h.supplier_info_writer.calls == []

    again = await h.use_case.execute(h.command())
    assert again.status is ProductRemediationStatus.RECONCILIATION_REQUIRED
    assert len(h.product_writer.calls) == 1


# --------------------------------------------------------------------------- I


async def test_i_ambiguous_existing_supplierinfo_fails_closed(session: Session) -> None:
    dup_a = ExistingSupplierInfo(
        id=1,
        partner_id=PARTNER_ID,
        product_tmpl_id=100,
        product_id=101,
        product_code=SELLER_ITEM_CODE,
        company_id=COMPANY_ID,
    )
    dup_b = ExistingSupplierInfo(
        id=2,
        partner_id=PARTNER_ID,
        product_tmpl_id=200,
        product_id=201,
        product_code=SELLER_ITEM_CODE,
        company_id=COMPANY_ID,
    )
    h = _Harness(session, existing_reader=_FakeExistingSupplierInfoReader(records=(dup_a, dup_b)))
    with pytest.raises(ProductRemediationIdentityAmbiguousError):
        await h.use_case.execute(h.command())
    assert h.product_writer.calls == []


async def test_i_existing_supplierinfo_with_no_linked_product_fails_closed(session: Session) -> None:
    orphan = ExistingSupplierInfo(
        id=1,
        partner_id=PARTNER_ID,
        product_tmpl_id=None,
        product_id=None,
        product_code=SELLER_ITEM_CODE,
        company_id=COMPANY_ID,
    )
    h = _Harness(session, existing_reader=_FakeExistingSupplierInfoReader(records=(orphan,)))
    with pytest.raises(ProductRemediationIdentityAmbiguousError):
        await h.use_case.execute(h.command())
    assert h.product_writer.calls == []


# --------------------------------------------------------------------------- J, K, L, M


async def test_j_unresolved_supplier_fails_closed_before_any_write(session: Session) -> None:
    h = _Harness(session, effect=False)
    with pytest.raises(ProductRemediationSupplierUnresolvedError):
        await h.use_case.execute(h.command())
    assert h.product_writer.calls == []
    assert (
        h.reservation_repo.find(review_id=REVIEW_ID, company_id=COMPANY_ID, review_version=2, line_number="1") is None
    )


async def test_k_review_version_mismatch_fails_closed(session: Session) -> None:
    h = _Harness(session, review=_review_item(version=3))
    with pytest.raises(ProductRemediationEligibilityError):
        await h.use_case.execute(h.command(expected_version=2))
    assert h.product_writer.calls == []


async def test_l_line_not_product_not_found_fails_closed(session: Session) -> None:
    h = _Harness(session, review=_review_item(reasons=()))
    with pytest.raises(ProductRemediationEligibilityError):
        await h.use_case.execute(h.command())
    assert h.product_writer.calls == []


async def test_m_missing_seller_item_code_fails_closed(session: Session) -> None:
    h = _Harness(session, source=_source_evidence(seller_item_code=None))
    with pytest.raises(ProductRemediationEligibilityError):
        await h.use_case.execute(h.command())
    assert h.product_writer.calls == []


# --------------------------------------------------------------------------- P


async def test_p_write_gate_disabled_makes_no_odoo_write(session: Session) -> None:
    gated = _FakeProductWriter(
        fail=ProductWriteSafetyGateError("Product remediation master-data write must be explicitly enabled.")
    )
    h = _Harness(session, product_writer=gated)
    with pytest.raises(ProductWriteSafetyGateError):
        await h.use_case.execute(h.command())
    assert h.supplier_info_writer.calls == []

    # Certain-no-write outcome: the reservation reverts to RESERVED, so a later retry
    # (e.g. once the gate is enabled) can cleanly reattempt without reconciliation.
    persisted = h.reservation_repo.find(review_id=REVIEW_ID, company_id=COMPANY_ID, review_version=2, line_number="1")
    assert persisted.status is ProductReservationStatus.RESERVED


# --------------------------------------------------------------------------- S


async def test_s_success_does_not_touch_any_decision_or_evidence_port(session: Session) -> None:
    h = _Harness(session)
    result = await h.use_case.execute(h.command())
    assert result.status is ProductRemediationStatus.COMPLETED
    # The use case has no dependency capable of submitting a review decision or pinning
    # execution evidence -- structurally verified by its constructor signature.
    import inspect

    params = set(inspect.signature(CreateNewProductUseCase.__init__).parameters)
    assert "decision_writer" not in params
    assert "review_decision_writer" not in params
    assert "execution_evidence_writer" not in params
    # The review item handed in was never mutated.
    assert h.reader.item.review_reasons == (_product_not_found_reason(),)


# --------------------------------------------------------------------------- T (schema)


def test_t_reservation_line_identity_is_db_unique(session: Session) -> None:
    session.add(
        WorkbenchReviewProductRemediationReservation(
            review_id=REVIEW_ID,
            company_id=COMPANY_ID,
            review_version=2,
            line_number="1",
            status="reserved",
            resolved_supplier_partner_id=PARTNER_ID,
            seller_item_code=SELLER_ITEM_CODE,
            product_name="X",
            is_storable=False,
        )
    )
    session.flush()
    session.add(
        WorkbenchReviewProductRemediationReservation(
            review_id=REVIEW_ID,
            company_id=COMPANY_ID,
            review_version=2,
            line_number="1",
            status="reserved",
            resolved_supplier_partner_id=PARTNER_ID,
            seller_item_code=SELLER_ITEM_CODE,
            product_name="Y",
            is_storable=False,
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_t_identity_claim_is_db_unique(session: Session) -> None:
    session.add(
        WorkbenchReviewProductRemediationReservation(
            review_id=REVIEW_ID,
            company_id=COMPANY_ID,
            review_version=2,
            line_number="1",
            status="reserved",
            resolved_supplier_partner_id=PARTNER_ID,
            seller_item_code=SELLER_ITEM_CODE,
            product_name="X",
            is_storable=False,
        )
    )
    session.add(
        WorkbenchReviewProductRemediationReservation(
            review_id=REVIEW_ID,
            company_id=COMPANY_ID,
            review_version=2,
            line_number="2",
            status="reserved",
            resolved_supplier_partner_id=PARTNER_ID,
            seller_item_code=SELLER_ITEM_CODE,
            product_name="X2",
            is_storable=False,
        )
    )
    session.flush()
    session.add(
        WorkbenchReviewProductIdentityClaim(
            company_id=COMPANY_ID,
            resolved_supplier_partner_id=PARTNER_ID,
            seller_item_code=SELLER_ITEM_CODE,
            owner_review_id=REVIEW_ID,
            owner_company_id=COMPANY_ID,
            owner_review_version=2,
            owner_line_number="1",
        )
    )
    session.flush()
    session.add(
        WorkbenchReviewProductIdentityClaim(
            company_id=COMPANY_ID,
            resolved_supplier_partner_id=PARTNER_ID,
            seller_item_code=SELLER_ITEM_CODE,
            owner_review_id=REVIEW_ID,
            owner_company_id=COMPANY_ID,
            owner_review_version=2,
            owner_line_number="2",
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


# --------------------------------------------------------------------------- Q, R


async def test_q_same_code_different_suppliers_are_independent_identities(session: Session) -> None:
    other_partner = PARTNER_ID + 1
    h1 = _Harness(session)
    result1 = await h1.use_case.execute(h1.command())
    assert result1.status is ProductRemediationStatus.COMPLETED

    session.add(
        WorkbenchReviewItem(
            review_id=REVIEW_ID_2,
            company_id=COMPANY_ID,
            invoice_id=ETTN_2,
            invoice_number="AKY-2",
            supplier_tax_number="9999999999",
            supplier_name="OTHER SUPPLIER",
            invoice_date=date(2026, 8, 21),
            currency="TRY",
            total_amount=Decimal("50.00"),
            workflow="manual_review",
            status="pending_review",
            review_reasons=[{"code": "product_not_found", "message": "x", "line_number": "1"}],
            warnings=[],
            version=2,
            idempotency_key=f"uyumsoft:{COMPANY_ID}:{ETTN_2}",
        )
    )
    session.commit()
    h2 = _Harness(
        session,
        review=_review_item(review_id=REVIEW_ID_2, ettn=ETTN_2),
        source=_source_evidence(review_id=REVIEW_ID_2, ettn=ETTN_2),
        effect=_remediation_effect(review_id=REVIEW_ID_2, partner_id=other_partner),
        product_writer=h1.product_writer,
        supplier_info_writer=h1.supplier_info_writer,
    )
    result2 = await h2.use_case.execute(h2.command(review_id=REVIEW_ID_2))

    assert result2.status is ProductRemediationStatus.COMPLETED
    assert result2.reused_existing_product is False
    assert result2.product_template_id != result1.product_template_id
    assert len(h1.product_writer.calls) == 2


async def test_r_same_supplier_code_different_company_no_collision(session: Session) -> None:
    other_company = COMPANY_ID + 1
    h1 = _Harness(session)
    result1 = await h1.use_case.execute(h1.command())
    assert result1.status is ProductRemediationStatus.COMPLETED

    session.add(
        WorkbenchReviewItem(
            review_id=REVIEW_ID_2,
            company_id=other_company,
            invoice_id=ETTN_2,
            invoice_number="AKY-2",
            supplier_tax_number=VKN,
            supplier_name="AKYASAM",
            invoice_date=date(2026, 8, 21),
            currency="TRY",
            total_amount=Decimal("50.00"),
            workflow="manual_review",
            status="pending_review",
            review_reasons=[{"code": "product_not_found", "message": "x", "line_number": "1"}],
            warnings=[],
            version=2,
            idempotency_key=f"uyumsoft:{other_company}:{ETTN_2}",
        )
    )
    session.commit()
    h2 = _Harness(
        session,
        review=ReviewItem(
            review_id=REVIEW_ID_2,
            invoice_id=ETTN_2,
            invoice_number="AKY-2",
            supplier_tax_number=VKN,
            supplier_name="AKYASAM",
            invoice_date=date(2026, 8, 21),
            currency="TRY",
            total_amount=Decimal("50.00"),
            workflow=WorkflowType.MANUAL_REVIEW,
            status=ReviewStatus.PENDING_REVIEW,
            review_reasons=(_product_not_found_reason(),),
            version=2,
        ),
        source=ReviewSourceInvoiceEvidence(
            review_id=REVIEW_ID_2,
            company_id=other_company,
            review_version=1,
            source_invoice_id=ETTN_2,
            invoice=_invoice(ettn=ETTN_2),
        ),
        effect=SupplierRemediationEffect(
            review_id=REVIEW_ID_2,
            company_id=other_company,
            review_version=1,
            source_invoice_id=ETTN_2,
            mode=SupplierResolutionMode.MATCH_EXISTING,
            resolved_partner_id=PARTNER_ID,
            partner_write_status=SupplierPartnerWriteEffectStatus.SELECTED,
        ),
        product_writer=h1.product_writer,
        supplier_info_writer=h1.supplier_info_writer,
    )
    result2 = await h2.use_case.execute(h2.command(review_id=REVIEW_ID_2, company_id=other_company))

    assert result2.status is ProductRemediationStatus.COMPLETED
    assert result2.reused_existing_product is False
    assert result2.product_template_id != result1.product_template_id
    assert len(h1.product_writer.calls) == 2


# --------------------------------------------------------------------------- C, D (real two-session concurrency)


@pytest.fixture()
def shared_db_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'prodremed.db'}")
    Base.metadata.create_all(engine, tables=TABLES)
    factory = sessionmaker(bind=engine)
    with factory() as seed:
        _seed_review_item(seed, review_id=REVIEW_ID, ettn=ETTN)
        seed.commit()
    try:
        yield factory
    finally:
        engine.dispose()


async def test_c_concurrent_same_review_line_yields_exactly_one_product_create(shared_db_factory) -> None:
    product_writer = _FakeProductWriter()  # shared across both "processes"
    supplier_info_writer = _FakeSupplierInfoWriter()
    session_a = shared_db_factory()
    session_b = shared_db_factory()
    try:
        h_a = _Harness(session_a, product_writer=product_writer, supplier_info_writer=supplier_info_writer)

        async def _winner_commits() -> None:
            await h_a.use_case.execute(h_a.command())

        h_b = _Harness(
            session_b,
            product_writer=product_writer,
            supplier_info_writer=supplier_info_writer,
            after_precheck_hook=_winner_commits,
        )
        result_b = await h_b.use_case.execute(h_b.command())

        assert result_b.already_applied is True
        assert len(product_writer.calls) == 1
        assert session_b.query(WorkbenchReviewProductRemediationReservation).count() == 1
    finally:
        session_a.close()
        session_b.close()


async def test_d_in_flight_identity_race_fails_closed_without_creating_a_duplicate(session: Session) -> None:
    """Review 1 owns the identity claim but has not resolved a product yet (still mid-flight).

    Review 2's claim attempt must lose the race and fail closed/retryable -- never
    create a second product while the real owner is still working.
    """

    session.add(
        WorkbenchReviewItem(
            review_id=REVIEW_ID_2,
            company_id=COMPANY_ID,
            invoice_id=ETTN_2,
            invoice_number="AKY-2",
            supplier_tax_number=VKN,
            supplier_name="AKYASAM",
            invoice_date=date(2026, 8, 21),
            currency="TRY",
            total_amount=Decimal("50.00"),
            workflow="manual_review",
            status="pending_review",
            review_reasons=[{"code": "product_not_found", "message": "x", "line_number": "1"}],
            warnings=[],
            version=2,
            idempotency_key=f"uyumsoft:{COMPANY_ID}:{ETTN_2}",
        )
    )
    reservation_repo = SqlAlchemyReviewProductRemediationReservationRepository(session)
    claim_repo = SqlAlchemyReviewProductIdentityClaimRepository(session)
    reservation_repo.reserve(
        ProductRemediationReservation(
            review_id=REVIEW_ID,
            company_id=COMPANY_ID,
            review_version=2,
            line_number="1",
            status=ProductReservationStatus.CREATE_ATTEMPTED,  # in-flight: no product identity yet
            resolved_supplier_partner_id=PARTNER_ID,
            seller_item_code=SELLER_ITEM_CODE,
            product_name="Yillik Aidat Urunu",
            is_storable=False,
            approved_by=ACTOR,
        )
    )
    claim_repo.claim(
        ProductIdentityClaim(
            company_id=COMPANY_ID,
            resolved_supplier_partner_id=PARTNER_ID,
            seller_item_code=SELLER_ITEM_CODE,
            owner_review_id=REVIEW_ID,
            owner_company_id=COMPANY_ID,
            owner_review_version=2,
            owner_line_number="1",
        )
    )
    session.commit()

    h = _Harness(
        session,
        review=_review_item(review_id=REVIEW_ID_2, ettn=ETTN_2),
        source=_source_evidence(review_id=REVIEW_ID_2, ettn=ETTN_2),
        effect=_remediation_effect(review_id=REVIEW_ID_2, partner_id=PARTNER_ID),
    )

    with pytest.raises(ProductRemediationRaceError):
        await h.use_case.execute(h.command(review_id=REVIEW_ID_2))
    assert h.product_writer.calls == []


async def test_d_sequential_second_review_reuses_the_first_reviews_completed_product(session: Session) -> None:
    """Once review 1's product/supplierinfo is fully resolved, review 2 for the same

    identity converges on it via the DB-enforced identity claim -- not the Odoo
    pre-check (its fake reader stays empty), proving the DB barrier alone is sufficient.
    """

    h_a = _Harness(session)
    result_a = await h_a.use_case.execute(h_a.command())
    assert result_a.status is ProductRemediationStatus.COMPLETED

    session.add(
        WorkbenchReviewItem(
            review_id=REVIEW_ID_2,
            company_id=COMPANY_ID,
            invoice_id=ETTN_2,
            invoice_number="AKY-2",
            supplier_tax_number=VKN,
            supplier_name="AKYASAM",
            invoice_date=date(2026, 8, 21),
            currency="TRY",
            total_amount=Decimal("50.00"),
            workflow="manual_review",
            status="pending_review",
            review_reasons=[{"code": "product_not_found", "message": "x", "line_number": "1"}],
            warnings=[],
            version=2,
            idempotency_key=f"uyumsoft:{COMPANY_ID}:{ETTN_2}",
        )
    )
    session.commit()
    h_b = _Harness(
        session,
        review=_review_item(review_id=REVIEW_ID_2, ettn=ETTN_2),
        source=_source_evidence(review_id=REVIEW_ID_2, ettn=ETTN_2),
        effect=_remediation_effect(review_id=REVIEW_ID_2, partner_id=PARTNER_ID),
        product_writer=h_a.product_writer,
        supplier_info_writer=h_a.supplier_info_writer,
    )
    result_b = await h_b.use_case.execute(h_b.command(review_id=REVIEW_ID_2))

    assert result_b.status is ProductRemediationStatus.COMPLETED
    assert result_b.reused_existing_product is True
    assert result_b.product_template_id == result_a.product_template_id
    assert result_b.product_id == result_a.product_id
    assert len(h_a.product_writer.calls) == 1  # still just the one create, from review 1
