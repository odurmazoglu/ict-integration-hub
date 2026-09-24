"""P0-PROD-18E-2: CREATE_NEW_PRODUCT category support, incl. RESALE product creation.

Covers, against the real ``CreateNewProductUseCase`` + SQLite repositories with fake
Odoo boundaries:

* command/API ``categ_id`` contract (strict positive id, optional in general);
* RESALE rules under an exact current-version purpose (required, exact allowlist
  membership, no parent->child approval, non-storable only, stale purpose ignored);
* read-only pre-write category validation through P0-PROD-18D discovery;
* reservation/idempotency/recovery binding of ``categ_id``;
* write-authorization consumer binding of ``categ_id``;
* the narrow ``OdooProductWriter`` ``categ_id`` exemption and its read-back;
* post-create verification (18D discovery + 18E-1A eligibility), never re-creating.

Account ids/codes below are deliberately arbitrary: nothing may depend on a specific
Odoo account.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from alembic import command as alembic_command
from app.api import dependencies
from app.api.routers.workbench import router
from app.api.security import AuthenticationMethod, Permission, RequestContext
from app.application.commands.product_remediation import CreateProductCommand, ValidatedProductCategory
from app.application.exceptions.product_remediation import (
    ProductDataIntegrityError,
    ProductWriteValidationError,
)
from app.application.workbench.exceptions import (
    ProductRemediationCategoryError,
    ProductRemediationConflictError,
    ProductRemediationContractError,
    ProductRemediationVerificationError,
    PurchaseAccountProductNotFoundError,
)
from app.application.workbench.product_remediation import (
    CreateNewProductCommand,
    ProductRemediationReservation,
    ProductRemediationStatus,
    ProductReservationStatus,
)
from app.application.workbench.product_remediation_category import ProductRemediationCategoryPolicy
from app.application.workbench.product_remediation_use_cases import CreateNewProductUseCase
from app.application.workbench.purchase_account_discovery import (
    CategoryPurchaseAccountConfiguration,
    CategoryPurchaseAccountRecord,
    GetProductPurchaseAccountQuery,
    ListCategoryPurchaseAccountsQuery,
    ProductPurchaseAccountRecord,
    ProductPurchaseAccountResolution,
    PurchaseAccountRecord,
    category_configuration,
    resolve_product_purchase_account,
)
from app.application.workbench.purchase_purpose import PurchasePurpose, PurchasePurposeResolution
from app.application.workbench.write_authorization import (
    WriteAuthorizationAlreadyConsumedError,
    WriteAuthorizationOperationType,
    WriteAuthorizationStatus,
    product_remediation_authorization_consumer_id,
)
from app.core.config import get_settings
from app.erp.write.odoo_product_writer import (
    PRODUCT_TEMPLATE_CATEGORY_FIELDS,
    PRODUCT_TEMPLATE_FIELDS,
    _reject_forbidden_tokens,
)
from app.persistence import (
    SqlAlchemyReviewProductIdentityClaimRepository,
    SqlAlchemyReviewProductRemediationReservationRepository,
    SqlAlchemyReviewSupplierRemediationEffectRepository,
    SqlAlchemyUnitOfWork,
    SqlAlchemyWriteAuthorizationRepository,
)
from tests.unit.test_odoo_product_writer import FakeProductJson2Client, _variant_row, _writer
from tests.unit.test_p0_prod_09g_product_remediation_narrow_authorization import (
    ACTOR,
    COMPANY_ID,
    ETTN,
    REVIEW_ID,
    _FakeExistingSupplierInfoReader,
    _FakeProductWriter,
    _FakeReviewReader,
    _FakeSourceReader,
    _FakeSupplierInfoWriter,
    _remediation_effect,
    _review_item,
    _source_evidence,
)
from tests.unit.test_p0_prod_09g_product_remediation_narrow_authorization import (
    session as _p0_prod_09g_session,
)

REVIEW_VERSION = 2
PARENT_CATEGORY_ID = 41
CHILD_CATEGORY_ID = 42
OTHER_CATEGORY_ID = 43
UNCONFIGURED_CATEGORY_ID = 44
# Arbitrary on purpose -- the flow must accept whatever valid account Odoo resolves.
ARBITRARY_ACCOUNT_ID = 7123
ARBITRARY_ACCOUNT_CODE = "770001"
TEMPLATE_ID = 9001
PRODUCT_ID = 9101


# --------------------------------------------------------------------------- discovery fakes


def _accounts(*, deprecated: bool = False) -> dict[int, PurchaseAccountRecord]:
    return {
        ARBITRARY_ACCOUNT_ID: PurchaseAccountRecord(
            id=ARBITRARY_ACCOUNT_ID,
            code=ARBITRARY_ACCOUNT_CODE,
            name="Arbitrary purchase account",
            account_type="expense",
            company_ids=(COMPANY_ID,),
            deprecated=deprecated,
        )
    }


def _category_record(
    category_id: int, *, account_id: int | None = ARBITRARY_ACCOUNT_ID
) -> CategoryPurchaseAccountRecord:
    return CategoryPurchaseAccountRecord(id=category_id, name=f"Category {category_id}", expense_account_id=account_id)


def _categories() -> tuple[CategoryPurchaseAccountConfiguration, ...]:
    records = (
        _category_record(PARENT_CATEGORY_ID),
        _category_record(CHILD_CATEGORY_ID),
        _category_record(OTHER_CATEGORY_ID),
        _category_record(UNCONFIGURED_CATEGORY_ID, account_id=None),
    )
    return tuple(category_configuration(r, company_id=COMPANY_ID, accounts_by_id=_accounts()) for r in records)


def _resolution(
    *,
    product_id: int = PRODUCT_ID,
    template_id: int = TEMPLATE_ID,
    categ_id: int | None = CHILD_CATEGORY_ID,
    is_storable: bool | None = False,
    company_id: int | None = None,
    deprecated: bool = False,
) -> ProductPurchaseAccountResolution:
    product = ProductPurchaseAccountRecord(
        product_id=product_id,
        product_template_id=template_id,
        name="Resale Product",
        active=True,
        company_id=company_id,
        product_type="consu",
        category_id=categ_id,
        override_account_id=None,
        is_storable=is_storable,
    )
    category = _category_record(categ_id) if categ_id is not None else None
    return resolve_product_purchase_account(
        product, category=category, company_id=COMPANY_ID, accounts_by_id=_accounts(deprecated=deprecated)
    )


class _FakePurposeReader:
    def __init__(self, resolutions: tuple[PurchasePurposeResolution, ...] = ()) -> None:
        self.resolutions = resolutions

    def list_purchase_purpose_resolutions(self, *, review_id: str, company_id: int):
        return self.resolutions


class _FakeCategoryLister:
    def __init__(self, categories: tuple[CategoryPurchaseAccountConfiguration, ...] | None = None) -> None:
        self.categories = _categories() if categories is None else categories
        self.calls: list[ListCategoryPurchaseAccountsQuery] = []

    def execute(self, query: ListCategoryPurchaseAccountsQuery):
        self.calls.append(query)
        return self.categories


class _FakeProductAccountResolver:
    def __init__(self, resolution: ProductPurchaseAccountResolution | Exception | None = None) -> None:
        self.resolution = _resolution() if resolution is None else resolution
        self.calls: list[GetProductPurchaseAccountQuery] = []

    def execute(self, query: GetProductPurchaseAccountQuery) -> ProductPurchaseAccountResolution:
        self.calls.append(query)
        if isinstance(self.resolution, Exception):
            raise self.resolution
        return self.resolution


def _purpose(purpose: PurchasePurpose, *, review_version: int = REVIEW_VERSION) -> PurchasePurposeResolution:
    return PurchasePurposeResolution(
        review_id=REVIEW_ID,
        company_id=COMPANY_ID,
        review_version=review_version,
        source_invoice_id=ETTN,
        purchase_purpose=purpose,
        approved_by=ACTOR,
    )


RESALE = (_purpose(PurchasePurpose.RESALE),)


# --------------------------------------------------------------------------- harness


@pytest.fixture()
def session() -> Iterator[Session]:
    # Same SQLite schema + seeded review (version 2) as the P0-PROD-09G harness.
    yield from _p0_prod_09g_session.__wrapped__()


class _Harness:
    def __init__(
        self,
        db: Session,
        *,
        purposes: tuple[PurchasePurposeResolution, ...] = (),
        allowlist: tuple[int, ...] = (CHILD_CATEGORY_ID,),
        categories: tuple[CategoryPurchaseAccountConfiguration, ...] | None = None,
        resolution: ProductPurchaseAccountResolution | Exception | None = None,
        product_writer: _FakeProductWriter | None = None,
        with_policy: bool = True,
    ) -> None:
        self.session = db
        effect_repo = SqlAlchemyReviewSupplierRemediationEffectRepository(db)
        effect_repo.create_remediation_effect(_remediation_effect())
        db.commit()
        self.reservation_repo = SqlAlchemyReviewProductRemediationReservationRepository(db)
        self.write_auth_repo = SqlAlchemyWriteAuthorizationRepository(db)
        self.product_writer = product_writer or _FakeProductWriter()
        self.supplier_info_writer = _FakeSupplierInfoWriter()
        self.purpose_reader = _FakePurposeReader(purposes)
        self.category_lister = _FakeCategoryLister(categories)
        self.resolver = _FakeProductAccountResolver(resolution)
        self.policy = ProductRemediationCategoryPolicy(
            purpose_reader=self.purpose_reader,
            category_lister=self.category_lister,
            product_account_resolver=self.resolver,
            approved_category_ids=allowlist,
        )
        self.use_case = CreateNewProductUseCase(
            review_reader=_FakeReviewReader(_review_item()),
            source_invoice_reader=_FakeSourceReader(_source_evidence()),
            remediation_effect_reader=effect_repo,
            reservation_writer=self.reservation_repo,
            identity_claim_writer=SqlAlchemyReviewProductIdentityClaimRepository(db),
            existing_supplier_info_reader=_FakeExistingSupplierInfoReader(),
            product_writer=self.product_writer,
            supplier_info_writer=self.supplier_info_writer,
            unit_of_work=SqlAlchemyUnitOfWork(db),
            write_authorization_repository=self.write_auth_repo,
            category_policy=self.policy if with_policy else None,
        )

    def command(self, **kw: Any) -> CreateNewProductCommand:
        base: dict[str, Any] = {
            "review_id": REVIEW_ID,
            "company_id": COMPANY_ID,
            "expected_version": REVIEW_VERSION,
            "line_number": "1",
            "product_name": "Resale Urunu",
            "product_type": "consu",
            "uom_id": 1,
            "approved_by": ACTOR,
        }
        base.update(kw)
        return CreateNewProductCommand(**base)

    def reservation(self) -> ProductRemediationReservation | None:
        return self.reservation_repo.find(
            review_id=REVIEW_ID, company_id=COMPANY_ID, review_version=REVIEW_VERSION, line_number="1"
        )

    def seed_reservation(self, **kw: Any) -> ProductRemediationReservation:
        base: dict[str, Any] = {
            "review_id": REVIEW_ID,
            "company_id": COMPANY_ID,
            "review_version": REVIEW_VERSION,
            "line_number": "1",
            "status": ProductReservationStatus.RESERVED,
            "resolved_supplier_partner_id": _remediation_effect().resolved_partner_id,
            "seller_item_code": _source_evidence().invoice.lines[0].seller_item_code,
            "product_name": "Resale Urunu",
            "is_storable": False,
            "approved_by": ACTOR,
        }
        base.update(kw)
        reserved = self.reservation_repo.reserve(ProductRemediationReservation(**base))
        self.session.commit()
        return reserved

    def issue_authorization(self, authorization_id: str = "auth-1") -> None:
        from datetime import UTC, datetime, timedelta

        self.write_auth_repo.create(
            authorization_id=authorization_id,
            company_id=COMPANY_ID,
            review_id=REVIEW_ID,
            operation_type=WriteAuthorizationOperationType.CREATE_NEW_PRODUCT,
            target_version=REVIEW_VERSION,
            authorized_by=ACTOR,
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
        )
        self.session.commit()


def _assert_no_writes(h: _Harness) -> None:
    assert h.product_writer.calls == []
    assert h.supplier_info_writer.calls == []


# =========================================================================== command / API


@pytest.mark.parametrize("bad", [0, -1, True, "42", 4.2])
def test_command_rejects_non_positive_or_non_int_categ_id(bad: object) -> None:
    with pytest.raises(ProductRemediationContractError):
        CreateNewProductCommand(
            review_id=REVIEW_ID,
            company_id=COMPANY_ID,
            expected_version=REVIEW_VERSION,
            line_number="1",
            product_name="X",
            product_type="consu",
            uom_id=1,
            approved_by=ACTOR,
            categ_id=bad,  # type: ignore[arg-type]
        )


async def test_non_resale_without_categ_id_behaves_exactly_as_before(session: Session) -> None:
    h = _Harness(session, purposes=(_purpose(PurchasePurpose.INTERNAL_USE),))
    result = await h.use_case.execute(h.command(product_type="service"))

    assert result.status is ProductRemediationStatus.COMPLETED
    assert h.product_writer.calls[0].category is None
    assert h.reservation().categ_id is None
    # No category means no discovery at all -- the legacy path is untouched.
    assert h.category_lister.calls == []
    assert h.resolver.calls == []


async def test_non_resale_positive_categ_id_is_validated_and_written(session: Session) -> None:
    h = _Harness(session, allowlist=(), resolution=_resolution(categ_id=UNCONFIGURED_CATEGORY_ID))
    result = await h.use_case.execute(h.command(categ_id=UNCONFIGURED_CATEGORY_ID))

    assert result.status is ProductRemediationStatus.COMPLETED
    assert h.product_writer.calls[0].category == ValidatedProductCategory(categ_id=UNCONFIGURED_CATEGORY_ID)
    assert h.reservation().categ_id == UNCONFIGURED_CATEGORY_ID


async def test_categ_id_without_configured_policy_fails_closed(session: Session) -> None:
    h = _Harness(session, with_policy=False)
    with pytest.raises(ProductRemediationContractError):
        await h.use_case.execute(h.command(categ_id=CHILD_CATEGORY_ID))
    assert h.reservation() is None
    _assert_no_writes(h)


async def test_unknown_category_is_rejected_before_any_write(session: Session) -> None:
    h = _Harness(session)
    with pytest.raises(ProductRemediationCategoryError):
        await h.use_case.execute(h.command(categ_id=999))
    assert h.reservation() is None
    _assert_no_writes(h)


# =========================================================================== RESALE rules


async def test_resale_requires_categ_id(session: Session) -> None:
    h = _Harness(session, purposes=RESALE)
    with pytest.raises(ProductRemediationCategoryError, match="requires an explicit categ_id"):
        await h.use_case.execute(h.command())
    assert h.reservation() is None
    _assert_no_writes(h)


async def test_resale_empty_allowlist_rejects(session: Session) -> None:
    h = _Harness(session, purposes=RESALE, allowlist=())
    with pytest.raises(ProductRemediationCategoryError, match="No product category is approved"):
        await h.use_case.execute(h.command(categ_id=CHILD_CATEGORY_ID))
    _assert_no_writes(h)


async def test_resale_non_allowlisted_categ_id_rejects(session: Session) -> None:
    h = _Harness(session, purposes=RESALE)
    with pytest.raises(ProductRemediationCategoryError, match="not an approved RESALE"):
        await h.use_case.execute(h.command(categ_id=OTHER_CATEGORY_ID))
    _assert_no_writes(h)


async def test_resale_parent_allowlisted_does_not_approve_child(session: Session) -> None:
    h = _Harness(session, purposes=RESALE, allowlist=(PARENT_CATEGORY_ID,))
    with pytest.raises(ProductRemediationCategoryError, match="not an approved RESALE"):
        await h.use_case.execute(h.command(categ_id=CHILD_CATEGORY_ID))
    _assert_no_writes(h)


async def test_resale_storable_product_rejects(session: Session) -> None:
    h = _Harness(session, purposes=RESALE)
    with pytest.raises(ProductRemediationCategoryError, match="non-storable"):
        await h.use_case.execute(h.command(categ_id=CHILD_CATEGORY_ID, is_storable=True))
    _assert_no_writes(h)


async def test_resale_category_without_valid_purchase_account_rejects(session: Session) -> None:
    h = _Harness(session, purposes=RESALE, allowlist=(UNCONFIGURED_CATEGORY_ID,))
    with pytest.raises(ProductRemediationCategoryError, match="no valid purchase account"):
        await h.use_case.execute(h.command(categ_id=UNCONFIGURED_CATEGORY_ID))
    _assert_no_writes(h)


async def test_resale_exact_allowlisted_categ_id_creates_verified_product(session: Session) -> None:
    h = _Harness(session, purposes=RESALE)
    result = await h.use_case.execute(h.command(categ_id=CHILD_CATEGORY_ID))

    assert result.status is ProductRemediationStatus.COMPLETED
    assert result.created_product is True
    assert h.product_writer.calls[0].category == ValidatedProductCategory(categ_id=CHILD_CATEGORY_ID)
    assert h.product_writer.calls[0].is_storable is False
    assert h.category_lister.calls == [ListCategoryPurchaseAccountsQuery(company_id=COMPANY_ID)]
    # Post-create verification re-read the created product via 18D discovery.
    assert h.resolver.calls == [GetProductPurchaseAccountQuery(company_id=COMPANY_ID, product_id=PRODUCT_ID)]
    assert len(h.supplier_info_writer.calls) == 1


async def test_stale_version_resale_purpose_does_not_apply(session: Session) -> None:
    h = _Harness(session, purposes=(_purpose(PurchasePurpose.RESALE, review_version=1),))
    result = await h.use_case.execute(h.command(product_type="service"))

    assert result.status is ProductRemediationStatus.COMPLETED
    assert h.product_writer.calls[0].category is None
    assert h.category_lister.calls == []
    assert h.resolver.calls == []


async def test_ambiguous_current_version_purpose_fails_closed(session: Session) -> None:
    h = _Harness(session, purposes=RESALE + (_purpose(PurchasePurpose.INTERNAL_USE),))
    with pytest.raises(ProductRemediationCategoryError):
        await h.use_case.execute(h.command(categ_id=CHILD_CATEGORY_ID))
    _assert_no_writes(h)


# =========================================================================== reservation / idempotency


async def test_categ_id_is_stored_and_same_categ_replay_is_idempotent(session: Session) -> None:
    h = _Harness(session, purposes=RESALE)
    await h.use_case.execute(h.command(categ_id=CHILD_CATEGORY_ID))
    replay = await h.use_case.execute(h.command(categ_id=CHILD_CATEGORY_ID))

    assert h.reservation().categ_id == CHILD_CATEGORY_ID
    assert replay.already_applied is True
    assert len(h.product_writer.calls) == 1


async def test_different_categ_replay_rejects_as_intent_mismatch(session: Session) -> None:
    h = _Harness(session, allowlist=(CHILD_CATEGORY_ID, OTHER_CATEGORY_ID), purposes=RESALE)
    await h.use_case.execute(h.command(categ_id=CHILD_CATEGORY_ID))
    with pytest.raises(ProductRemediationConflictError):
        await h.use_case.execute(h.command(categ_id=OTHER_CATEGORY_ID))
    with pytest.raises(ProductRemediationConflictError):
        await h.use_case.execute(h.command())
    assert len(h.product_writer.calls) == 1


def test_repository_fingerprint_rejects_concurrent_categ_drift(session: Session) -> None:
    h = _Harness(session)
    h.seed_reservation(categ_id=CHILD_CATEGORY_ID)
    with pytest.raises(ProductRemediationConflictError):
        h.seed_reservation(categ_id=OTHER_CATEGORY_ID)
    assert h.seed_reservation(categ_id=CHILD_CATEGORY_ID).categ_id == CHILD_CATEGORY_ID


async def test_existing_null_categ_reservation_stays_compatible(session: Session) -> None:
    h = _Harness(session)
    h.seed_reservation(
        status=ProductReservationStatus.COMPLETED,
        product_template_id=TEMPLATE_ID,
        product_id=PRODUCT_ID,
        supplierinfo_id=9501,
    )
    result = await h.use_case.execute(h.command())

    assert result.already_applied is True
    assert h.reservation().categ_id is None
    _assert_no_writes(h)


async def test_recovery_from_reserved_uses_the_reserved_categ_id(session: Session) -> None:
    h = _Harness(session, purposes=RESALE)
    h.seed_reservation(categ_id=CHILD_CATEGORY_ID)

    result = await h.use_case.execute(h.command(categ_id=CHILD_CATEGORY_ID))

    assert result.status is ProductRemediationStatus.COMPLETED
    assert h.product_writer.calls[0].category == ValidatedProductCategory(categ_id=CHILD_CATEGORY_ID)
    # The reserved category is re-validated before the (first) Odoo write.
    assert len(h.category_lister.calls) == 1


async def test_recovery_of_legacy_null_reservation_under_resale_fails_closed(session: Session) -> None:
    h = _Harness(session, purposes=RESALE)
    h.seed_reservation()
    with pytest.raises(ProductRemediationCategoryError, match="requires an explicit categ_id"):
        await h.use_case.execute(h.command())
    _assert_no_writes(h)


# =========================================================================== authorization


def test_consumer_id_binds_categ_id_and_is_unchanged_without_one() -> None:
    kwargs = {"company_id": COMPANY_ID, "review_id": REVIEW_ID, "expected_version": REVIEW_VERSION, "line_number": "1"}
    legacy = product_remediation_authorization_consumer_id(**kwargs)
    assert product_remediation_authorization_consumer_id(**kwargs, categ_id=None) == legacy
    child = product_remediation_authorization_consumer_id(**kwargs, categ_id=CHILD_CATEGORY_ID)
    assert child == product_remediation_authorization_consumer_id(**kwargs, categ_id=CHILD_CATEGORY_ID)
    assert child != legacy
    assert child != product_remediation_authorization_consumer_id(**kwargs, categ_id=OTHER_CATEGORY_ID)


async def test_authorized_resale_create_binds_the_reserved_categ_id(session: Session) -> None:
    h = _Harness(session, purposes=RESALE)
    h.issue_authorization()

    result = await h.use_case.execute(h.command(categ_id=CHILD_CATEGORY_ID, authorization_id="auth-1"))

    assert result.status is ProductRemediationStatus.COMPLETED
    record = h.write_auth_repo.get_by_id(authorization_id="auth-1", company_id=COMPANY_ID)
    assert record.status is WriteAuthorizationStatus.CONSUMED
    assert record.consumed_by_execution_id == product_remediation_authorization_consumer_id(
        company_id=COMPANY_ID,
        review_id=REVIEW_ID,
        expected_version=REVIEW_VERSION,
        line_number="1",
        categ_id=CHILD_CATEGORY_ID,
    )
    assert h.product_writer.calls[0].authorization.authorization_id == "auth-1"


async def test_authorization_consumed_for_one_category_rejects_another(session: Session) -> None:
    h = _Harness(session, allowlist=(CHILD_CATEGORY_ID, OTHER_CATEGORY_ID), purposes=RESALE)
    h.issue_authorization()
    h.write_auth_repo.claim_and_consume(
        company_id=COMPANY_ID,
        review_id=REVIEW_ID,
        operation_type=WriteAuthorizationOperationType.CREATE_NEW_PRODUCT,
        target_version=REVIEW_VERSION,
        authorization_id="auth-1",
        execution_id=product_remediation_authorization_consumer_id(
            company_id=COMPANY_ID,
            review_id=REVIEW_ID,
            expected_version=REVIEW_VERSION,
            line_number="1",
            categ_id=CHILD_CATEGORY_ID,
        ),
    )
    session.commit()

    with pytest.raises(WriteAuthorizationAlreadyConsumedError):
        await h.use_case.execute(h.command(categ_id=OTHER_CATEGORY_ID, authorization_id="auth-1"))
    _assert_no_writes(h)
    record = h.write_auth_repo.get_by_id(authorization_id="auth-1", company_id=COMPANY_ID)
    assert record.use_count == 1


# =========================================================================== Odoo writer


def _category_template_row(*, categ_id: Any = CHILD_CATEGORY_ID, is_storable: Any = False) -> dict[str, Any]:
    return {
        "id": 900,
        "name": "Resale Urunu",
        "default_code": False,
        "type": "consu",
        "uom_id": [1, "Units"],
        "categ_id": categ_id,
        "is_storable": is_storable,
    }


def _category_command(**overrides: Any) -> CreateProductCommand:
    kwargs: dict[str, Any] = {
        "name": "Resale Urunu",
        "type": "consu",
        "uom_id": 1,
        "is_storable": False,
        "approved_by": ACTOR,
        "category": ValidatedProductCategory(categ_id=CHILD_CATEGORY_ID),
    }
    kwargs.update(overrides)
    return CreateProductCommand(**kwargs)


async def test_writer_sends_validated_categ_id_and_verifies_read_back() -> None:
    client = FakeProductJson2Client(
        search_sequence=[[_category_template_row(categ_id=[CHILD_CATEGORY_ID, "Resale"])], [_variant_row()]]
    )
    result = await _writer(client).create_product(_category_command())

    assert result.template_id == 900
    assert client.create_calls == [
        {"name": "Resale Urunu", "type": "consu", "uom_id": 1, "is_storable": False, "categ_id": CHILD_CATEGORY_ID}
    ]
    assert client.search_calls[0]["fields"] == PRODUCT_TEMPLATE_FIELDS + PRODUCT_TEMPLATE_CATEGORY_FIELDS


async def test_writer_without_category_keeps_the_exact_legacy_payload_and_read_back() -> None:
    client = FakeProductJson2Client(search_sequence=[[_category_template_row()], [_variant_row()]])
    await _writer(client).create_product(_category_command(category=None))

    assert client.create_calls == [{"name": "Resale Urunu", "type": "consu", "uom_id": 1, "is_storable": False}]
    assert client.search_calls[0]["fields"] == PRODUCT_TEMPLATE_FIELDS


def test_writer_payload_never_contains_accounting_or_supplier_fields() -> None:
    from app.erp.write.odoo_product_writer import _product_template_payload

    payload = _product_template_payload(_category_command(default_code="ICT-1"))
    assert set(payload) == {"name", "type", "uom_id", "is_storable", "default_code", "categ_id"}


@pytest.mark.parametrize("raw", [CHILD_CATEGORY_ID, "42", {"categ_id": CHILD_CATEGORY_ID}])
def test_raw_categ_id_cannot_bypass_application_validation(raw: object) -> None:
    with pytest.raises(ProductWriteValidationError):
        _category_command(category=raw)


def test_categ_id_stays_forbidden_without_a_validated_category() -> None:
    with pytest.raises(ProductWriteValidationError):
        _reject_forbidden_tokens({"name": "X", "categ_id": CHILD_CATEGORY_ID})


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "X", "categ_id": OTHER_CATEGORY_ID},  # drifted from the validated category
        {"name": "X", "categ_id": [CHILD_CATEGORY_ID]},  # not the exact id
        {"name": "X", "categ_id": CHILD_CATEGORY_ID, "taxes_id": [1]},
        {"name": "X", "categ_id": CHILD_CATEGORY_ID, "company_id": COMPANY_ID},
        {"name": "X", "categ_id": CHILD_CATEGORY_ID, "x_studio_ana_tedarikci": 1},
        {"name": "X", "categ_id": CHILD_CATEGORY_ID, "seller_ids": []},
        {"name": "X", "categ_id": CHILD_CATEGORY_ID, "nested": {"categ_id": OTHER_CATEGORY_ID}},
    ],
)
def test_unrelated_forbidden_fields_remain_forbidden_with_a_validated_category(payload: dict[str, Any]) -> None:
    with pytest.raises(ProductWriteValidationError):
        _reject_forbidden_tokens(payload, category=ValidatedProductCategory(categ_id=CHILD_CATEGORY_ID))


@pytest.mark.parametrize(
    "row",
    [
        _category_template_row(categ_id=[OTHER_CATEGORY_ID, "Other"]),
        _category_template_row(categ_id=False),
        _category_template_row(is_storable=True),
        _category_template_row(is_storable=None),
    ],
)
async def test_writer_read_back_mismatch_fails_closed(row: dict[str, Any]) -> None:
    client = FakeProductJson2Client(search_sequence=[[row], [_variant_row()]])
    with pytest.raises(ProductDataIntegrityError):
        await _writer(client).create_product(_category_command())
    assert len(client.create_calls) == 1


async def test_writer_read_back_failure_leads_to_reconciliation_never_a_second_create(session: Session) -> None:
    h = _Harness(
        session,
        purposes=RESALE,
        product_writer=_FakeProductWriter(fail=ProductDataIntegrityError("category mismatch")),
    )
    with pytest.raises(ProductDataIntegrityError):
        await h.use_case.execute(h.command(categ_id=CHILD_CATEGORY_ID))
    assert h.reservation().status is ProductReservationStatus.CREATE_ATTEMPTED

    result = await h.use_case.execute(h.command(categ_id=CHILD_CATEGORY_ID))
    assert result.status is ProductRemediationStatus.RECONCILIATION_REQUIRED
    assert len(h.product_writer.calls) == 1


# =========================================================================== post-create verification


@pytest.mark.parametrize(
    ("resolution", "message"),
    [
        (_resolution(categ_id=OTHER_CATEGORY_ID), "not in the reserved category"),
        (PurchaseAccountProductNotFoundError("gone"), "could not be read back"),
        (_resolution(company_id=COMPANY_ID + 1), "another company"),
        (_resolution(template_id=TEMPLATE_ID + 5), "different identity"),
        (_resolution(deprecated=True), "not eligible for RESALE"),
        (_resolution(is_storable=None), "not eligible for RESALE"),
    ],
)
async def test_post_create_verification_failure_never_recreates(
    session: Session, resolution: object, message: str
) -> None:
    h = _Harness(session, purposes=RESALE, resolution=resolution)  # type: ignore[arg-type]
    with pytest.raises(ProductRemediationVerificationError, match=message):
        await h.use_case.execute(h.command(categ_id=CHILD_CATEGORY_ID))

    persisted = h.reservation()
    assert persisted.status is ProductReservationStatus.PRODUCT_CREATED
    assert (persisted.product_template_id, persisted.product_id) == (TEMPLATE_ID, PRODUCT_ID)
    assert h.supplier_info_writer.calls == []

    with pytest.raises(ProductRemediationVerificationError):
        await h.use_case.execute(h.command(categ_id=CHILD_CATEGORY_ID))
    assert len(h.product_writer.calls) == 1
    assert h.supplier_info_writer.calls == []


async def test_post_create_resume_after_odoo_fix_links_supplierinfo_without_recreating(session: Session) -> None:
    h = _Harness(session, purposes=RESALE, resolution=_resolution(deprecated=True))
    with pytest.raises(ProductRemediationVerificationError):
        await h.use_case.execute(h.command(categ_id=CHILD_CATEGORY_ID))

    h.resolver.resolution = _resolution()
    result = await h.use_case.execute(h.command(categ_id=CHILD_CATEGORY_ID))

    assert result.status is ProductRemediationStatus.COMPLETED
    assert result.created_product is False
    assert len(h.product_writer.calls) == 1
    assert len(h.supplier_info_writer.calls) == 1


async def test_non_resale_post_create_checks_category_but_not_resale_eligibility(session: Session) -> None:
    # An unconfigured category account is fine outside RESALE; only identity/category drift fails.
    h = _Harness(session, allowlist=())
    h.resolver.resolution = resolve_product_purchase_account(
        ProductPurchaseAccountRecord(
            product_id=PRODUCT_ID,
            product_template_id=TEMPLATE_ID,
            name="Internal Product",
            active=True,
            company_id=None,
            product_type="consu",
            category_id=UNCONFIGURED_CATEGORY_ID,
            override_account_id=None,
            is_storable=False,
        ),
        category=_category_record(UNCONFIGURED_CATEGORY_ID, account_id=None),
        company_id=COMPANY_ID,
        accounts_by_id={},
    )
    result = await h.use_case.execute(h.command(categ_id=UNCONFIGURED_CATEGORY_ID))
    assert result.status is ProductRemediationStatus.COMPLETED


# =========================================================================== HTTP + composition + migration


class _ResultStub:
    review_id = REVIEW_ID
    company_id = COMPANY_ID
    review_version = REVIEW_VERSION
    line_number = "1"
    status = ProductRemediationStatus.COMPLETED
    product_template_id = TEMPLATE_ID
    product_id = PRODUCT_ID
    supplierinfo_id = 9501
    created_product = True
    created_supplierinfo = True
    reused_existing_product = False
    already_applied = False
    safe_message = "ok"


class _RecordingUseCase:
    def __init__(self) -> None:
        self.calls: list[CreateNewProductCommand] = []

    async def execute(self, command: CreateNewProductCommand):
        self.calls.append(command)
        return _ResultStub()


def _post_product_resolution(body_overrides: dict[str, Any]) -> tuple[Any, _RecordingUseCase]:
    fake = _RecordingUseCase()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[dependencies.get_create_new_product_use_case] = lambda: fake
    app.dependency_overrides[dependencies.get_request_context] = lambda: RequestContext(
        user_id="finance",
        user_name="Finance",
        company_id=COMPANY_ID,
        permissions=(Permission.WORKBENCH_REVIEW_DECIDE,),
        trace_id="p0-prod-18e-2",
        authentication_method=AuthenticationMethod.JWT,
    )
    body = {
        "mode": "create_new_product",
        "expected_version": REVIEW_VERSION,
        "line_number": "1",
        "product_name": "Resale Urunu",
        "product_type": "consu",
        "uom_id": 1,
    }
    body.update(body_overrides)
    with TestClient(app) as client:
        return client.post(f"/api/workbench/reviews/{REVIEW_ID}/product-resolution", json=body), fake


def test_api_categ_id_is_optional_and_reaches_the_command() -> None:
    response, fake = _post_product_resolution({})
    assert response.status_code == 200, response.text
    assert fake.calls[0].categ_id is None

    response, fake = _post_product_resolution({"categ_id": CHILD_CATEGORY_ID})
    assert response.status_code == 200, response.text
    assert fake.calls[0].categ_id == CHILD_CATEGORY_ID


@pytest.mark.parametrize("bad", [0, -3, "42", True, 4.5, "Resale"])
def test_api_rejects_invalid_categ_id(bad: object) -> None:
    response, fake = _post_product_resolution({"categ_id": bad})
    assert response.status_code == 422, response.text
    assert fake.calls == []


def test_composition_wires_category_policy_from_settings() -> None:
    from unittest.mock import MagicMock

    from app.composition.product_remediation import build_create_new_product_use_case
    from app.connectors.odoo.client import OdooJson2Client
    from app.core.config import Settings

    settings = Settings(resale_product_category_ids=[CHILD_CATEGORY_ID])
    use_case = build_create_new_product_use_case(
        session=MagicMock(), settings=settings, odoo_client=OdooJson2Client.from_settings(settings)
    )
    policy = use_case._category_policy
    assert isinstance(policy, ProductRemediationCategoryPolicy)
    assert policy._approved_category_ids == frozenset({CHILD_CATEGORY_ID})


def test_migration_adds_nullable_categ_id_and_downgrades_cleanly(tmp_path: Path, monkeypatch) -> None:
    database_url = f"sqlite:///{tmp_path / 'p0_prod_18e_2.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    config = Config("alembic.ini")
    table = "workbench_review_product_remediation_reservations"
    try:
        alembic_command.upgrade(config, "202607170032")
        inspector = inspect(create_engine(database_url))
        columns = {column["name"]: column for column in inspector.get_columns(table)}
        assert columns["categ_id"]["nullable"] is True
        assert "ck_wrpr_reservations_categ_id_positive" in {c["name"] for c in inspector.get_check_constraints(table)}

        alembic_command.downgrade(config, "202607170031")
        inspector = inspect(create_engine(database_url))
        assert "categ_id" not in {column["name"] for column in inspector.get_columns(table)}

        alembic_command.upgrade(config, "head")
    finally:
        get_settings.cache_clear()
