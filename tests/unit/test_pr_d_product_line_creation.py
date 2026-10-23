"""PR D: Workbench new-product creation -- domain, policy, writer, use case, persistence.

The use-case tests drive the REAL ``CreateLineProductUseCase`` with the REAL
``OdooProductWriter`` / ``OdooSupplierInfoRepository`` over an in-memory Odoo that commits
``product.template`` + variant + inline ``seller_ids`` atomically, the REAL category policy,
the REAL SQLAlchemy creation/claim repositories (SQLite) and the #208 matcher-backed
reclassifier -- so "the line matches after creation" and "future invoices auto-match"
are proven against actual matcher semantics.
"""

from __future__ import annotations

import copy
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.application.commands.product_remediation import (
    CreateProductCommand,
    ValidatedProductCategory,
    ValidatedSupplierLink,
)
from app.application.effective_supplier import EffectiveSupplierResolver
from app.application.exceptions.product_remediation import ProductDataIntegrityError, ProductWriteValidationError
from app.application.workbench.exceptions import (
    ProductRemediationCategoryError,
    ProductRemediationContractError,
    ProductRemediationDataIntegrityError,
    ProductRemediationEligibilityError,
    ProductRemediationRaceError,
    ProductRemediationSupplierUnresolvedError,
    ReviewVersionConflictError,
)
from app.application.workbench.product_line_creation import (
    CreatedProductSnapshot,
    CreateLineProductCommand,
    NewProductIntent,
    NewProductKind,
    ProductLineCreateDuplicateError,
    ProductLineCreateInFlightError,
    ProductLineCreateManualOnlyError,
    ProductLineCreateOutcomeUnknownError,
    ProductLineCreatePolicyError,
    ProductLineCreateReconciliationRequiredError,
    ProductLineCreation,
    ProductLineCreationStatus,
)
from app.application.workbench.product_line_creation_policy import (
    ProductLineCreationPolicy,
    code_key,
    compile_manual_only_sku_patterns,
)
from app.application.workbench.product_line_creation_use_cases import CreateLineProductUseCase
from app.application.workbench.product_mapping import ProductMappingSellerCodeMissingError
from app.application.workbench.product_remediation import ProductIdentityClaim
from app.application.workbench.product_remediation_category import ProductRemediationCategoryPolicy
from app.application.workbench.purchase_account_discovery import (
    CategoryPurchaseAccountRecord,
    ProductPurchaseAccountRecord,
    PurchaseAccountRecord,
    category_configuration,
    resolve_product_purchase_account,
)
from app.application.workbench.purchase_purpose import PurchasePurpose, PurchasePurposeResolution
from app.application.workbench.write_authorization import (
    WriteAuthorizationOperationType,
    product_line_creation_authorization_consumer_id,
)
from app.connectors.exceptions import ConnectorAuthorizationError, ConnectorTimeoutError, ConnectorValidationError
from app.db.base import Base
from app.erp.models import Partner
from app.erp.odoo.existing_supplier_info_reader import OdooExistingSupplierInfoReader
from app.erp.write.odoo_product_write_policy import OdooProductWritePolicy
from app.erp.write.odoo_product_writer import (
    OdooProductTemplateRepository,
    OdooProductWriter,
    _reject_forbidden_tokens,
)
from app.erp.write.odoo_supplierinfo_writer import OdooSupplierInfoRepository
from app.models.workbench_product_line_creation import (
    PRODUCT_LINE_CREATION_KINDS,
    PRODUCT_LINE_CREATION_STATUSES,
    WorkbenchProductLineCreation,
)
from app.models.workbench_review_item import WorkbenchReviewItem
from app.models.workbench_review_product_identity_claim import WorkbenchReviewProductIdentityClaim
from app.persistence import (
    SqlAlchemyProductLineCreationRepository,
    SqlAlchemyReviewProductIdentityClaimRepository,
    SqlAlchemyUnitOfWork,
)
from tests.unit.test_product_not_found_operator_flow import (
    ACTOR,
    COMPANY,
    ICT_BULUT,
    REVIEW,
    TFZP_DESCRIPTION,
    Harness,
    InMemoryOdoo,
    _invoice,
    _line,
    _lines_with_reason,
    _matched,
    _matcher,
)

T0 = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
ACCOUNT_ID = 7123
RESALE_CATEGORY = 41
SOFTWARE = 50
MANAGEENGINE = 51
MANAGEENGINE_ITSM = 52
MANAGEENGINE_LOOKALIKE = 53
SERVICES = 60
UNITS = 1
ME_SKU = "85710.1SS3"
ME_DESCRIPTION = "ManageEngine ServiceDesk Plus Enterprise 1 Technician Subscription"
CATEGORY_NAMES = {
    RESALE_CATEGORY: "Cloud Services / Microsoft",
    SOFTWARE: "Software Licenses",
    MANAGEENGINE: "Software Licenses / ManageEngine",
    MANAGEENGINE_ITSM: "Software Licenses / ManageEngine / ITSM",
    MANAGEENGINE_LOOKALIKE: "Software Licenses / ManageEngineX",
    SERVICES: "Services / Implementation",
}


# --------------------------------------------------------------------------- in-memory Odoo


class CatalogOdoo(InMemoryOdoo):
    """#208 in-memory Odoo plus product.template create (variant + inline seller_ids, atomic)."""

    def __init__(self) -> None:
        super().__init__()
        self.templates: dict[int, dict[str, Any]] = {}
        for variant in self.variants.values():
            variant.setdefault("default_code", None)
            self.templates.setdefault(
                variant["tmpl"],
                {
                    "name": variant["name"],
                    "default_code": None,
                    "type": "service",
                    "uom_id": UNITS,
                    "categ_id": SERVICES,
                    "is_storable": False,
                    "sale_ok": True,
                    "purchase_ok": True,
                    "active": variant["active"],
                },
            )
        self.template_creates: list[dict[str, Any]] = []
        #: None | "refused" (certain no write) | "timeout" (nothing committed) | "lost_response" (committed)
        self.create_failure: str | None = None
        self.next_template = 900

    async def create_product_template(self, payload: dict[str, Any]) -> int:
        self.template_creates.append(copy.deepcopy(payload))
        failure, self.create_failure = self.create_failure, None
        if failure == "refused":
            raise ConnectorValidationError("Odoo rejected the values.")
        if failure == "timeout":
            raise ConnectorTimeoutError("Odoo request timed out.")
        template_id = self.next_template
        variant_id = template_id + 9000
        self.next_template += 1
        self.templates[template_id] = {
            "name": payload["name"],
            "default_code": payload.get("default_code"),
            "type": payload["type"],
            "uom_id": payload["uom_id"],
            "categ_id": payload.get("categ_id"),
            "is_storable": payload["is_storable"],
            # Odoo defaults when the key is omitted (pre-V1 payload).
            "sale_ok": payload.get("sale_ok", True),
            "purchase_ok": payload.get("purchase_ok", True),
            "active": True,
        }
        self.variants[variant_id] = {
            "tmpl": template_id,
            "name": payload["name"],
            "active": True,
            "company_id": None,
            "default_code": payload.get("default_code"),
        }
        for _zero, _also_zero, values in payload.get("seller_ids", []):
            self.supplierinfo.append(
                {"id": 100 + len(self.supplierinfo), "product_tmpl_id": template_id, "product_id": None, **values}
            )
        if failure == "lost_response":
            raise ConnectorTimeoutError("Odoo request timed out.")
        return template_id

    async def search_read(self, *, model, domain, fields, limit=20, offset=0):
        if model == "product.supplierinfo":
            return await super().search_read(model=model, domain=domain, fields=fields, limit=limit, offset=offset)
        if model == "product.template":
            rows = [{"id": tid, **row} for tid, row in self.templates.items()]
        elif model == "product.product":
            rows = [{"id": vid, "product_tmpl_id": row["tmpl"]} for vid, row in self.variants.items()]
        else:  # pragma: no cover - the writer reads nothing else
            raise AssertionError(model)
        for name, op, value in domain:
            assert op == "="
            rows = [row for row in rows if row.get(name) == value]
        return [{key: _odoo_value(key, row.get(key)) for key in fields} for row in rows][:limit]

    def new_templates(self) -> list[int]:
        return sorted(tid for tid in self.templates if tid >= 900)


def _odoo_value(key: str, value: Any) -> Any:
    if key in ("uom_id", "categ_id", "product_tmpl_id") and value:
        return [value, "x"]
    return False if value is None else value


class CatalogPort:
    """ProductCatalogReader + CreatedProductReader over the in-memory Odoo."""

    def __init__(self, odoo: CatalogOdoo) -> None:
        self.odoo = odoo

    def find_variant_ids_by_default_code(self, default_code: str, *, company_id: int) -> tuple[int, ...]:
        key = default_code.strip().casefold()
        return tuple(
            sorted(vid for vid, row in self.odoo.variants.items() if (row.get("default_code") or "").casefold() == key)
        )

    def find_template_ids_by_name(self, name: str, *, company_id: int) -> tuple[int, ...]:
        key = name.strip().casefold()
        return tuple(
            sorted(tid for tid, row in self.odoo.templates.items() if row["active"] and row["name"].casefold() == key)
        )

    def find_supplierinfo_ids_by_product_code(self, product_code: str, *, company_id: int) -> tuple[int, ...]:
        key = product_code.strip().casefold()
        return tuple(
            sorted(row["id"] for row in self.odoo.supplierinfo if (row.get("product_code") or "").casefold() == key)
        )

    def read_template(self, template_id: int) -> CreatedProductSnapshot | None:
        row = self.odoo.templates.get(template_id)
        if row is None:
            return None
        return CreatedProductSnapshot(
            template_id=template_id,
            name=row["name"],
            default_code=row["default_code"],
            product_type=row["type"],
            uom_id=row["uom_id"],
            categ_id=row["categ_id"],
            is_storable=row["is_storable"],
            variant_ids=tuple(sorted(vid for vid, v in self.odoo.variants.items() if v["tmpl"] == template_id)),
            sale_ok=row.get("sale_ok"),
            purchase_ok=row.get("purchase_ok"),
        )


def _accounts() -> dict[int, PurchaseAccountRecord]:
    return {
        ACCOUNT_ID: PurchaseAccountRecord(
            id=ACCOUNT_ID,
            code="770001",
            name="Purchase account",
            account_type="expense",
            company_ids=(COMPANY,),
            deprecated=False,
        )
    }


def _category_record(category_id: int, *, complete: bool = True) -> CategoryPurchaseAccountRecord:
    name = CATEGORY_NAMES[category_id]
    return CategoryPurchaseAccountRecord(
        id=category_id,
        name=name.rsplit(" / ", 1)[-1],
        expense_account_id=ACCOUNT_ID,
        complete_name=name if complete else None,
    )


class PartnerDirectory:
    """``find_by_ids_including_archived`` over a few production-shaped partners."""

    def __init__(self, partners: dict[int, Partner] | None = None) -> None:
        self.partners = dict(
            partners
            or {
                ICT_BULUT: Partner(
                    id=ICT_BULUT,
                    name="ICT Bulut",
                    tax_number="4650459971",
                    active=True,
                    commercial_partner_id=ICT_BULUT,
                ),
            }
        )
        self.calls: list[tuple[int, ...]] = []
        self.fail: Exception | None = None

    def find_by_ids_including_archived(self, ids):
        self.calls.append(tuple(ids))
        if self.fail is not None:
            raise self.fail
        return tuple(self.partners[i] for i in ids if i in self.partners)


class CategoryLister:
    def __init__(self, *, complete: bool = True, missing: frozenset[int] = frozenset()) -> None:
        self.complete = complete
        self.missing = missing

    def execute(self, query):
        return tuple(
            category_configuration(
                _category_record(category_id, complete=self.complete), company_id=COMPANY, accounts_by_id=_accounts()
            )
            for category_id in CATEGORY_NAMES
            if category_id not in self.missing
        )


class OdooAccountResolver:
    """Product purchase-account discovery answered from the in-memory Odoo templates."""

    def __init__(self, odoo: CatalogOdoo) -> None:
        self.odoo = odoo

    def execute(self, query):
        variant = self.odoo.variants[query.product_id]
        template = self.odoo.templates[variant["tmpl"]]
        product = ProductPurchaseAccountRecord(
            product_id=query.product_id,
            product_template_id=variant["tmpl"],
            name=template["name"],
            active=True,
            company_id=None,
            product_type=template["type"],
            category_id=template["categ_id"],
            override_account_id=None,
            is_storable=template["is_storable"],
        )
        return resolve_product_purchase_account(
            product,
            category=_category_record(template["categ_id"]),
            company_id=COMPANY,
            accounts_by_id=_accounts(),
        )


class PurposeReader:
    def __init__(self, resolutions: tuple[PurchasePurposeResolution, ...] = ()) -> None:
        self.resolutions = resolutions

    def list_purchase_purpose_resolutions(self, *, review_id: str, company_id: int):
        return self.resolutions


def _resale(version: int = 2) -> tuple[PurchasePurposeResolution, ...]:
    return (
        PurchasePurposeResolution(
            review_id=REVIEW,
            company_id=COMPANY,
            review_version=version,
            source_invoice_id="ICF2026000008699",
            purchase_purpose=PurchasePurpose.RESALE,
            approved_by=ACTOR,
        ),
    )


def _pr_d_invoice():
    return _invoice(
        _line("1", "TFZP", TFZP_DESCRIPTION, "2"),
        _line("2", ME_SKU, ME_DESCRIPTION),
        _line("3", "01.0001", "CLOUD HİZMETİ"),
        _line("4", None, "Kodsuz satır"),
    )


def _intent(**overrides: Any) -> NewProductIntent:
    values: dict[str, Any] = {
        "product_name": "Windows Server Standard Lisans (VM başına)",
        "kind": NewProductKind.SERVICE,
        "categ_id": SERVICES,
        "uom_id": UNITS,
        "internal_reference": None,
        "sale_ok": False,
    }
    values.update(overrides)
    return NewProductIntent(**values)


def _session() -> Session:
    engine = create_engine("sqlite://")

    # pysqlite defers BEGIN and breaks SAVEPOINT semantics (the repositories use
    # begin_nested); SQLAlchemy's documented workaround gives PostgreSQL-like transactions,
    # so rollback-after-race assertions test the code, not the driver.
    @event.listens_for(engine, "connect")
    def _disable_pysqlite_transactions(dbapi_connection, _record) -> None:
        dbapi_connection.isolation_level = None

    @event.listens_for(engine, "begin")
    def _emit_begin(connection) -> None:
        connection.exec_driver_sql("BEGIN")

    Base.metadata.create_all(
        engine,
        tables=[
            WorkbenchReviewItem.__table__,
            WorkbenchProductLineCreation.__table__,
            WorkbenchReviewProductIdentityClaim.__table__,
        ],
    )
    return sessionmaker(bind=engine)()


class World:
    def __init__(
        self,
        *,
        purposes: tuple[PurchasePurposeResolution, ...] = (),
        manual_roots: tuple[int, ...] = (MANAGEENGINE,),
        uoms: tuple[int, ...] = (UNITS,),
        lister: CategoryLister | None = None,
        uom_unit_codes: dict[int, tuple[str, ...]] | None = None,
        manual_only_suppliers: tuple[int, ...] = (),
        partners: PartnerDirectory | None = None,
        invoice: Any = None,
        partner_id: int = ICT_BULUT,
    ) -> None:
        self.odoo = CatalogOdoo()
        self.harness = Harness(invoice or _pr_d_invoice(), odoo=self.odoo, partner_match=_matched(partner_id))
        self.partners = partners or PartnerDirectory()
        self.session = _session()
        self.now = T0
        self.catalog = CatalogPort(self.odoo)
        self.repository = SqlAlchemyProductLineCreationRepository(self.session)
        self.claims = SqlAlchemyReviewProductIdentityClaimRepository(self.session)
        category_policy = ProductRemediationCategoryPolicy(
            purpose_reader=PurposeReader(purposes),
            category_lister=lister or CategoryLister(),
            product_account_resolver=OdooAccountResolver(self.odoo),
            approved_category_ids={RESALE_CATEGORY},
        )
        self.policy = ProductLineCreationPolicy(
            category_policy=category_policy,
            category_lister=lister or CategoryLister(),
            catalog_reader=self.catalog,
            allowed_uom_ids=uoms,
            uom_unit_codes=uom_unit_codes,
            manual_only_supplier_ids=manual_only_suppliers,
            supplier_reader=self.partners,
            manual_only_category_ids=manual_roots,
        )
        self.use_case = CreateLineProductUseCase(
            review_reader=self.harness,
            source_invoice_reader=self.harness,
            effective_supplier_resolver=EffectiveSupplierResolver(
                partner_matcher=self.harness, accepted_supplier_reader=self.harness.accepted_supplier_reader
            ),
            policy=self.policy,
            category_verifier=category_policy,
            creation_repository=self.repository,
            identity_claims=self.claims,
            existing_supplier_info_reader=OdooExistingSupplierInfoReader(
                repository=OdooSupplierInfoRepository(client=self.odoo)
            ),
            product_writer=OdooProductWriter(
                repository=OdooProductTemplateRepository(client=self.odoo),
                policy=OdooProductWritePolicy(
                    product_remediation_write_enabled=True, app_env="staging", odoo_host="test-ictteknoloji.odoo.com"
                ),
            ),
            created_product_reader=self.catalog,
            reclassifier=self.harness,
            unit_of_work=SqlAlchemyUnitOfWork(self.session),
            write_authorization_repository=self.harness,
            clock=lambda: self.now,
        )

    def command(self, line: str = "1", *, version: int | None = None, **intent: Any) -> CreateLineProductCommand:
        return CreateLineProductCommand(
            review_id=REVIEW,
            company_id=COMPANY,
            expected_version=self.harness.review.version if version is None else version,
            line_number=line,
            intent=_intent(**intent),
            approved_by=ACTOR,
            authorization_id="auth-1",
        )

    async def run(self, command: CreateLineProductCommand):
        return await self.use_case.execute(command)

    def creation(self, line: str = "1", version: int = 2) -> ProductLineCreation | None:
        return self.repository.find(review_id=REVIEW, company_id=COMPANY, review_version=version, line_number=line)

    def claim(self, code: str = "TFZP") -> ProductIdentityClaim | None:
        return self.claims.find(company_id=COMPANY, resolved_supplier_partner_id=ICT_BULUT, seller_item_code=code)

    def code_rows(self, code: str = "TFZP") -> list[dict[str, Any]]:
        return [row for row in self.odoo.supplierinfo if row.get("product_code") == code]


@pytest.fixture
def world() -> Iterator[World]:
    w = World()
    assert _lines_with_reason(w.harness.review) == ["1", "2", "3", "4"]
    yield w
    w.session.close()


def _assert_nothing_written(w: World) -> None:
    assert w.odoo.template_creates == []
    assert w.odoo.new_templates() == []
    assert w.creation() is None
    assert w.claim() is None
    assert w.harness.reclassify_calls == []


# --------------------------------------------------------------------------- domain


def test_intent_is_normalized_and_its_digest_binds_every_choice() -> None:
    intent = _intent(product_name="  Windows Server  ", internal_reference="  WS-VM  ")

    assert (intent.product_name, intent.internal_reference) == ("Windows Server", "WS-VM")
    assert _intent(internal_reference="   ").internal_reference is None
    assert intent.digest() == _intent(product_name="Windows Server", internal_reference="WS-VM").digest()
    for change in (
        {"product_name": "Other"},
        {"kind": NewProductKind.GOODS},
        {"categ_id": SOFTWARE},
        {"uom_id": 4},
        {"internal_reference": "WS-VM2"},
    ):
        assert _intent(**{"product_name": "Windows Server", "internal_reference": "WS-VM", **change}).digest() != (
            intent.digest()
        )


@pytest.mark.parametrize(
    ("kind", "product_type", "storable"),
    [
        (NewProductKind.SERVICE, "service", False),
        (NewProductKind.GOODS, "consu", False),
        (NewProductKind.STOCKED_GOODS, "consu", True),
    ],
)
def test_kind_is_one_explicit_choice_of_type_and_storability(kind, product_type, storable) -> None:
    intent = _intent(kind=kind)
    assert (intent.product_type, intent.is_storable) == (product_type, storable)


@pytest.mark.parametrize(
    "overrides",
    [
        {"product_name": "  "},
        {"product_name": "İki\nsatır"},
        {"product_name": "x" * 256},
        {"kind": "service"},
        {"categ_id": 0},
        {"categ_id": True},
        {"uom_id": None},
        {"internal_reference": "x" * 65},
        {"internal_reference": "A\tB"},
    ],
)
def test_invalid_intents_are_contract_errors(overrides: dict[str, Any]) -> None:
    with pytest.raises(ProductRemediationContractError):
        _intent(**overrides)


def test_model_value_tuples_equal_the_application_enums() -> None:
    assert set(PRODUCT_LINE_CREATION_STATUSES) == {status.value for status in ProductLineCreationStatus}
    assert set(PRODUCT_LINE_CREATION_KINDS) == {kind.value for kind in NewProductKind}


def test_consumer_id_binds_the_intent_and_is_disjoint_from_rest_create() -> None:
    base = {"company_id": COMPANY, "review_id": REVIEW, "expected_version": 2, "line_number": "1"}
    one = product_line_creation_authorization_consumer_id(**base, intent_digest=_intent().digest())
    assert one == product_line_creation_authorization_consumer_id(**base, intent_digest=_intent().digest())
    assert one != product_line_creation_authorization_consumer_id(
        **base, intent_digest=_intent(categ_id=SOFTWARE).digest()
    )
    assert one.startswith("product-line-create:")


# --------------------------------------------------------------------------- policy (pure SKU rules)


@pytest.mark.parametrize("code", [ME_SKU, "85510.0SU2", "67005.6MPMFA4", "6705.5MEE", "702012.0SUP6", "85510.0mbl5"])
def test_managemengine_license_skus_are_manual_only(world: World, code: str) -> None:
    with pytest.raises(ProductLineCreateManualOnlyError, match="manuel"):
        world.policy.require_codes_allowed(seller_item_code=code, intent=_intent())


@pytest.mark.parametrize("code", ["TFZP", "TGDR", "01.0001", "HBV000006MHLQ", "CFQ7TTC0LH18:0001", "100021"])
def test_ordinary_supplier_codes_are_not_manual_only(world: World, code: str) -> None:
    world.policy.require_codes_allowed(seller_item_code=code, intent=_intent())


@pytest.mark.parametrize("reference", ["TFZP", "tfzp", " T F Z P "])
def test_supplier_code_is_never_accepted_as_internal_reference(world: World, reference: str) -> None:
    with pytest.raises(ProductLineCreatePolicyError, match="tedarikçinin ürün koduyla"):
        world.policy.require_codes_allowed(seller_item_code="TFZP", intent=_intent(internal_reference=reference))


def test_license_sku_shaped_internal_reference_is_refused(world: World) -> None:
    with pytest.raises(ProductLineCreateManualOnlyError, match="İç referans"):
        world.policy.require_codes_allowed(seller_item_code="TFZP", intent=_intent(internal_reference="85710.1SS3"))
    world.policy.require_codes_allowed(seller_item_code="TFZP", intent=_intent(internal_reference="ICT-WS-VM"))


def test_extra_patterns_extend_but_never_replace_the_builtin_shape() -> None:
    patterns = compile_manual_only_sku_patterns([r"^CYB-LIC-\d+$"])
    assert any(p.fullmatch("CYB-LIC-12") for p in patterns)
    assert any(p.fullmatch(ME_SKU) for p in patterns)
    with pytest.raises(ValueError):
        compile_manual_only_sku_patterns([" "])
    assert code_key(" Ab C ") == "abc"


# --------------------------------------------------------------------------- policy (Odoo reads)


def _validate(world: World, code: str = "TFZP", **intent: Any):
    return world.policy.validate(
        review_id=REVIEW, company_id=COMPANY, review_version=2, seller_item_code=code, intent=_intent(**intent)
    )


def test_valid_intent_returns_the_validated_category(world: World) -> None:
    validated = _validate(world)
    assert validated.category == ValidatedProductCategory(categ_id=SERVICES)
    assert validated.resale is False


@pytest.mark.parametrize("uoms", [(4,), ()])
def test_uom_outside_the_allowlist_is_refused(uoms: tuple[int, ...]) -> None:
    with pytest.raises(ProductLineCreatePolicyError, match="ölçü birimi"):
        _validate(World(uoms=uoms))


@pytest.mark.parametrize("category", [MANAGEENGINE, MANAGEENGINE_ITSM])
def test_manual_only_category_subtree_is_refused(world: World, category: int) -> None:
    with pytest.raises(ProductLineCreateManualOnlyError, match="ManageEngine"):
        _validate(world, categ_id=category)


def test_lookalike_sibling_category_is_not_inside_the_manual_only_subtree(world: World) -> None:
    assert _validate(world, categ_id=MANAGEENGINE_LOOKALIKE).category.categ_id == MANAGEENGINE_LOOKALIKE
    assert _validate(world, categ_id=SOFTWARE).category.categ_id == SOFTWARE


def test_unprovable_category_subtree_fails_closed() -> None:
    with pytest.raises(ProductLineCreatePolicyError, match="hiyerarşisi"):
        _validate(World(lister=CategoryLister(complete=False)), categ_id=SOFTWARE)
    with pytest.raises(ProductLineCreatePolicyError, match="doğrulanamadı"):
        _validate(World(lister=CategoryLister(missing=frozenset({MANAGEENGINE}))), categ_id=SOFTWARE)


def test_unknown_category_is_refused(world: World) -> None:
    with pytest.raises(ProductRemediationCategoryError):
        _validate(world, categ_id=999)


def test_resale_requires_an_allowlisted_non_storable_category() -> None:
    world = World(purposes=_resale())
    assert _validate(world, categ_id=RESALE_CATEGORY).resale is True
    with pytest.raises(ProductRemediationCategoryError, match="approved RESALE"):
        _validate(world, categ_id=SERVICES)
    with pytest.raises(ProductRemediationCategoryError, match="non-storable"):
        _validate(world, categ_id=RESALE_CATEGORY, kind=NewProductKind.STOCKED_GOODS)


def test_code_already_an_odoo_internal_reference_is_a_duplicate(world: World) -> None:
    world.odoo.variants[501]["default_code"] = "tfzp"
    with pytest.raises(ProductLineCreateDuplicateError, match="#501"):
        _validate(world)


def test_internal_reference_in_use_or_same_name_is_a_duplicate(world: World) -> None:
    world.odoo.variants[502]["default_code"] = "ICT-WS"
    with pytest.raises(ProductLineCreateDuplicateError, match="İç referans"):
        _validate(world, internal_reference="ict-ws")
    with pytest.raises(ProductLineCreateDuplicateError, match="adlı aktif"):
        _validate(world, product_name="windows server standard (vm)")


# --------------------------------------------------------------------------- writer


def _link(**overrides: Any) -> ValidatedSupplierLink:
    values = {"partner_id": ICT_BULUT, "product_code": "TFZP", "company_id": COMPANY, "product_name": "Kaynak"}
    values.update(overrides)
    return ValidatedSupplierLink(**values)


def _create_command(**overrides: Any) -> CreateProductCommand:
    values: dict[str, Any] = {
        "name": "Active Directory Audit Service",
        "type": "service",
        "uom_id": UNITS,
        "is_storable": False,
        "approved_by": ACTOR,
        "category": ValidatedProductCategory(categ_id=SERVICES),
        "supplier_link": _link(),
    }
    values.update(overrides)
    return CreateProductCommand(**values)


async def test_writer_sends_one_atomic_create_with_the_supplier_code_only_on_the_seller_row() -> None:
    odoo = CatalogOdoo()
    writer = OdooProductWriter(
        repository=OdooProductTemplateRepository(client=odoo),
        policy=OdooProductWritePolicy(
            product_remediation_write_enabled=True, app_env="staging", odoo_host="test-ictteknoloji.odoo.com"
        ),
    )

    result = await writer.create_product(_create_command(default_code="ICT-ADA"))

    (payload,) = odoo.template_creates
    assert payload == {
        "name": "Active Directory Audit Service",  # a value containing "active" is fine on the allowlisted path
        "type": "service",
        "uom_id": UNITS,
        "is_storable": False,
        "default_code": "ICT-ADA",
        "categ_id": SERVICES,
        "seller_ids": [
            [0, 0, {"partner_id": ICT_BULUT, "product_code": "TFZP", "company_id": COMPANY, "product_name": "Kaynak"}]
        ],
    }
    assert (result.template_id, result.product_id) == (900, 9900)


@pytest.mark.parametrize("default_code", ["TFZP", "tfzp", "T FZP"])
def test_command_refuses_internal_reference_equal_to_the_supplier_code(default_code: str) -> None:
    with pytest.raises(ProductWriteValidationError, match="supplier's own product code"):
        _create_command(default_code=default_code)


def test_supplier_link_requires_a_validated_category() -> None:
    with pytest.raises(ProductWriteValidationError, match="requires a category"):
        _create_command(category=None)
    with pytest.raises(ProductWriteValidationError):
        _link(product_code=" ")


@pytest.mark.parametrize(
    "tamper",
    [
        lambda p: p.update(taxes_id=[1]),
        lambda p: p.update(x_studio_ana_tedarikci=7),
        lambda p: p.update(categ_id=SOFTWARE),
        lambda p: p["seller_ids"].append([0, 0, {"partner_id": 1, "product_code": "X", "company_id": 1}]),
        lambda p: p["seller_ids"][0][2].update(default_code="TFZP"),
    ],
)
def test_supplier_linked_payload_is_an_exact_allowlist(tamper) -> None:
    command = _create_command()
    from app.erp.write.odoo_product_writer import _product_template_payload

    payload = _product_template_payload(command)
    tamper(payload)
    with pytest.raises(ProductWriteValidationError):
        _reject_forbidden_tokens(payload, category=command.category, supplier_link=command.supplier_link)


async def test_writer_read_back_refuses_a_substituted_type_or_uom() -> None:
    class Substituting(CatalogOdoo):
        async def create_product_template(self, payload):
            template_id = await super().create_product_template(payload)
            self.templates[template_id]["uom_id"] = 4
            return template_id

    odoo = Substituting()
    writer = OdooProductWriter(
        repository=OdooProductTemplateRepository(client=odoo),
        policy=OdooProductWritePolicy(
            product_remediation_write_enabled=True, app_env="staging", odoo_host="test-ictteknoloji.odoo.com"
        ),
    )
    with pytest.raises(ProductDataIntegrityError, match="unit of measure"):
        await writer.create_product(_create_command())


# --------------------------------------------------------------------------- use case: success


async def test_new_product_is_created_with_its_supplier_mapping_and_the_line_matches(world: World) -> None:
    result = await world.run(world.command(internal_reference="ICT-WS-VM"))

    (payload,) = world.odoo.template_creates
    assert payload["default_code"] == "ICT-WS-VM"
    assert payload["seller_ids"] == [
        [
            0,
            0,
            {"partner_id": ICT_BULUT, "product_code": "TFZP", "company_id": COMPANY, "product_name": TFZP_DESCRIPTION},
        ]
    ]
    (row,) = world.code_rows()
    assert (row["product_tmpl_id"], row["product_id"], row["partner_id"]) == (900, None, ICT_BULUT)
    assert result.created_product is True and result.already_applied is False
    assert (result.product_template_id, result.product_id, result.supplierinfo_id) == (900, 9900, row["id"])
    assert (result.previous_version, result.current_version) == (2, 3)
    assert result.line_resolved is True
    assert result.remaining_product_lines == ("2", "3", "4")
    assert _lines_with_reason(world.harness.review) == ["2", "3", "4"]

    creation = world.creation()
    assert creation.status is ProductLineCreationStatus.COMPLETED
    assert creation.supplierinfo_id == row["id"]
    assert world.claim().owner_line_number == "1"
    (consumed,) = world.harness.consumed
    assert consumed["operation_type"] is WriteAuthorizationOperationType.CREATE_NEW_PRODUCT
    assert consumed["target_version"] == 2
    assert consumed["execution_id"] == product_line_creation_authorization_consumer_id(
        company_id=COMPANY,
        review_id=REVIEW,
        expected_version=2,
        line_number="1",
        intent_digest=_intent(internal_reference="ICT-WS-VM").digest(),
    )
    (reclass,) = world.harness.reclassify_calls
    assert reclass.trigger.value == "master_data_changed"


async def test_future_invoices_of_the_same_supplier_and_code_match_automatically(world: World) -> None:
    await world.run(world.command())

    later = _invoice(_line("1", "TFZP", "Windows Server again"), number="ICF2026000009000")
    (line,) = (
        _matcher(world.odoo).match_invoice(later, company_id=COMPANY, partner_match=_matched(ICT_BULUT)).line_results
    )
    assert line.result.product_id == 9900


async def test_resale_review_creates_into_an_allowlisted_category() -> None:
    world = World(purposes=_resale())
    result = await world.run(world.command(categ_id=RESALE_CATEGORY, kind=NewProductKind.GOODS))
    assert world.odoo.templates[result.product_template_id]["categ_id"] == RESALE_CATEGORY
    assert world.odoo.templates[result.product_template_id]["type"] == "consu"


# --------------------------------------------------------------------------- use case: refusals (no write)


async def test_stale_version_is_never_reinterpreted(world: World) -> None:
    with pytest.raises(ReviewVersionConflictError):
        await world.run(world.command(version=1))
    _assert_nothing_written(world)


async def test_line_without_product_not_found_is_refused(world: World) -> None:
    with pytest.raises(ProductRemediationEligibilityError, match="gerekmiyor"):
        await world.run(world.command(line="9"))
    _assert_nothing_written(world)


async def test_line_without_a_seller_code_is_refused(world: World) -> None:
    with pytest.raises(ProductMappingSellerCodeMissingError):
        await world.run(world.command(line="4"))
    _assert_nothing_written(world)


async def test_unresolved_supplier_is_refused(world: World) -> None:
    world.harness.partner_match = None
    with pytest.raises(ProductRemediationSupplierUnresolvedError):
        await world.run(world.command())
    _assert_nothing_written(world)


async def test_managemengine_license_line_is_manual_only(world: World) -> None:
    with pytest.raises(ProductLineCreateManualOnlyError):
        await world.run(world.command(line="2"))
    assert world.odoo.template_creates == [] and world.creation("2") is None


async def test_existing_supplier_mapping_of_the_code_is_a_duplicate(world: World) -> None:
    world.odoo.supplierinfo.append(
        {
            "id": 77,
            "partner_id": ICT_BULUT,
            "product_tmpl_id": 301,
            "product_id": None,
            "product_code": "TFZP",
            "company_id": 1,
        }
    )
    with pytest.raises(ProductLineCreateDuplicateError, match="#77"):
        await world.run(world.command())
    assert world.odoo.template_creates == [] and world.creation() is None


async def test_identity_claimed_by_another_review_line_is_a_duplicate(world: World) -> None:
    world.claims.claim(
        ProductIdentityClaim(
            company_id=COMPANY,
            resolved_supplier_partner_id=ICT_BULUT,
            seller_item_code="TFZP",
            owner_review_id="review:other",
            owner_company_id=COMPANY,
            owner_review_version=1,
            owner_line_number="5",
        )
    )
    world.session.commit()
    with pytest.raises(ProductLineCreateDuplicateError, match="review:other"):
        await world.run(world.command())
    assert world.odoo.template_creates == [] and world.creation() is None


# --------------------------------------------------------------------------- use case: crash safety


async def test_certain_no_write_discards_the_attempt_and_releases_the_claim(world: World) -> None:
    world.odoo.create_failure = "refused"
    with pytest.raises(ProductWriteValidationError):
        await world.run(world.command())

    assert world.odoo.new_templates() == []
    assert world.creation() is None and world.claim() is None
    # a corrected resubmission creates normally
    result = await world.run(world.command())
    assert result.created_product and world.odoo.new_templates() == [900]


async def test_lost_response_is_recovered_from_the_atomic_mapping_without_a_second_create(world: World) -> None:
    world.odoo.create_failure = "lost_response"
    with pytest.raises(ProductLineCreateOutcomeUnknownError, match="ikinci bir ürün oluşturmaz"):
        await world.run(world.command())
    assert world.creation().status is ProductLineCreationStatus.CREATE_ATTEMPTED
    assert world.harness.reclassify_calls == []

    result = await world.run(world.command())

    assert len(world.odoo.template_creates) == 1
    assert world.odoo.new_templates() == [900]
    assert result.created_product is True and result.product_id == 9900
    assert world.creation().status is ProductLineCreationStatus.COMPLETED


async def test_unknown_outcome_without_commit_waits_for_the_grace_then_creates_exactly_once(world: World) -> None:
    world.odoo.create_failure = "timeout"
    with pytest.raises(ProductLineCreateOutcomeUnknownError):
        await world.run(world.command())

    world.now = T0 + timedelta(minutes=29)
    with pytest.raises(ProductLineCreateInFlightError, match="09:30 UTC"):
        await world.run(world.command())
    assert len(world.odoo.template_creates) == 1

    world.now = T0 + timedelta(minutes=31)
    result = await world.run(world.command())

    assert len(world.odoo.template_creates) == 2
    assert world.odoo.new_templates() == [900]
    assert result.created_product is True
    assert world.creation().create_attempted_at.replace(tzinfo=UTC) == world.now


async def test_mapping_that_contradicts_the_intent_needs_reconciliation(world: World) -> None:
    world.odoo.create_failure = "lost_response"
    with pytest.raises(ProductLineCreateOutcomeUnknownError):
        await world.run(world.command())
    world.odoo.templates[900]["name"] = "Someone renamed it"

    with pytest.raises(ProductLineCreateReconciliationRequiredError):
        await world.run(world.command())
    assert world.creation().status is ProductLineCreationStatus.NEEDS_RECONCILIATION
    with pytest.raises(ProductLineCreateReconciliationRequiredError, match="teknik inceleme"):
        await world.run(world.command())
    assert len(world.odoo.template_creates) == 1
    assert world.harness.reclassify_calls == []


async def test_reclassification_conflict_keeps_the_product_and_a_resubmission_completes(world: World) -> None:
    world.harness.reclassify_error = ReviewVersionConflictError("moved")
    with pytest.raises(ReviewVersionConflictError):
        await world.run(world.command())
    assert world.creation().status is ProductLineCreationStatus.PRODUCT_CREATED

    result = await world.run(world.command())

    assert len(world.odoo.template_creates) == 1
    assert result.created_product is False and result.line_resolved
    assert world.creation().status is ProductLineCreationStatus.COMPLETED


async def test_completed_request_is_idempotent_and_a_different_intent_is_refused(world: World) -> None:
    await world.run(world.command())

    again = await world.run(world.command(version=2))
    assert again.already_applied is True and again.created_product is False
    with pytest.raises(ProductLineCreateDuplicateError, match="zaten yeni ürün"):
        await world.run(world.command(version=2, product_name="Başka ad"))
    assert len(world.odoo.template_creates) == 1
    assert len(world.harness.reclassify_calls) == 1


async def test_different_intent_on_an_unresolved_attempt_is_refused(world: World) -> None:
    world.odoo.create_failure = "timeout"
    with pytest.raises(ProductLineCreateOutcomeUnknownError):
        await world.run(world.command())
    with pytest.raises(ProductLineCreateInFlightError, match="farklı bilgilerle"):
        await world.run(world.command(product_name="Başka ad"))
    assert len(world.odoo.template_creates) == 1


# --------------------------------------------------------------------------- persistence


def _creation(**overrides: Any) -> ProductLineCreation:
    values: dict[str, Any] = {
        "review_id": REVIEW,
        "company_id": COMPANY,
        "review_version": 2,
        "line_number": "1",
        "status": ProductLineCreationStatus.CREATE_ATTEMPTED,
        "supplier_partner_id": ICT_BULUT,
        "seller_item_code": "TFZP",
        "intent": _intent(),
        "approved_by": ACTOR,
        "create_attempted_at": T0,
    }
    values.update(overrides)
    return ProductLineCreation(**values)


def test_repository_round_trip_and_compare_and_swap() -> None:
    session = _session()
    repository = SqlAlchemyProductLineCreationRepository(session)
    inserted = repository.insert_attempt(_creation())
    assert inserted.intent == _intent()

    with pytest.raises(ProductRemediationRaceError):
        repository.insert_attempt(_creation())
    with pytest.raises(ProductRemediationDataIntegrityError):
        repository.advance(
            inserted,
            expected_status=ProductLineCreationStatus.PRODUCT_CREATED,
            new_status=ProductLineCreationStatus.COMPLETED,
        )
    created = repository.advance(
        inserted,
        expected_status=ProductLineCreationStatus.CREATE_ATTEMPTED,
        new_status=ProductLineCreationStatus.PRODUCT_CREATED,
        product_template_id=900,
        product_id=9900,
    )
    assert (created.product_template_id, created.product_id) == (900, 9900)
    with pytest.raises(ProductRemediationDataIntegrityError, match="refusing to discard"):
        repository.discard_attempt(created)


def test_repository_refuses_a_row_whose_intent_no_longer_matches_its_digest() -> None:
    session = _session()
    repository = SqlAlchemyProductLineCreationRepository(session)
    repository.insert_attempt(_creation())
    session.query(WorkbenchProductLineCreation).update({"categ_id": SOFTWARE})
    with pytest.raises(ProductRemediationDataIntegrityError, match="digest"):
        repository.find(review_id=REVIEW, company_id=COMPANY, review_version=2, line_number="1")


def test_claim_release_deletes_only_the_owners_own_claim() -> None:
    session = _session()
    claims = SqlAlchemyReviewProductIdentityClaimRepository(session)
    mine = _creation().identity_claim()
    claims.claim(mine)
    other_owner = replace(mine, owner_line_number="9")

    claims.release(other_owner)
    assert claims.find(company_id=COMPANY, resolved_supplier_partner_id=ICT_BULUT, seller_item_code="TFZP") == mine
    claims.release(mine)
    assert claims.find(company_id=COMPANY, resolved_supplier_partner_id=ICT_BULUT, seller_item_code="TFZP") is None


# --------------------------------------------------------------------------- pre-merge review hardening


async def test_writer_read_back_refusal_after_create_is_never_a_certain_no_write() -> None:
    class ReadBackRefused(CatalogOdoo):
        async def search_read(self, *, model, domain, fields, limit=20, offset=0):
            if model == "product.template":
                raise ConnectorAuthorizationError("Odoo authorization failed.")
            return await super().search_read(model=model, domain=domain, fields=fields, limit=limit, offset=offset)

    odoo = ReadBackRefused()
    writer = OdooProductWriter(
        repository=OdooProductTemplateRepository(client=odoo),
        policy=OdooProductWritePolicy(
            product_remediation_write_enabled=True, app_env="staging", odoo_host="test-ictteknoloji.odoo.com"
        ),
    )
    # both the PR D path and the v1 path (no supplier link)
    for command in (_create_command(), _create_command(supplier_link=None, category=None, name="Other")):
        with pytest.raises(ProductDataIntegrityError, match="read-back failed") as caught:
            await writer.create_product(command)
        assert not isinstance(caught.value, ProductWriteValidationError)
    assert len(odoo.new_templates()) == 2


async def test_read_back_refusal_keeps_the_attempt_and_the_next_submission_adopts(world: World) -> None:
    original = world.odoo.search_read
    calls = {"n": 0}

    async def refuse_first_template_read(*, model, domain, fields, limit=20, offset=0):
        if model == "product.template" and calls["n"] == 0:
            calls["n"] += 1
            raise ConnectorValidationError("Odoo rejected the request payload.")
        return await original(model=model, domain=domain, fields=fields, limit=limit, offset=offset)

    world.odoo.search_read = refuse_first_template_read
    with pytest.raises(ProductLineCreateOutcomeUnknownError):
        await world.run(world.command())
    assert world.creation().status is ProductLineCreationStatus.CREATE_ATTEMPTED
    assert world.claim() is not None  # not released: the product exists

    result = await world.run(world.command())

    assert result.created_product and len(world.odoo.template_creates) == 1


async def test_internal_reference_may_not_be_any_vendor_code(world: World) -> None:
    with pytest.raises(ProductLineCreatePolicyError, match=r"\(01\.0001\)"):
        await world.run(world.command(internal_reference="01.0001"))  # line 3's seller code
    world.odoo.supplierinfo.append(
        {
            "id": 88,
            "partner_id": 99,
            "product_tmpl_id": 302,
            "product_id": None,
            "product_code": "XYZ-9",
            "company_id": 1,
        }
    )
    with pytest.raises(ProductLineCreatePolicyError, match="#88"):
        await world.run(world.command(internal_reference="xyz-9"))
    assert world.odoo.template_creates == []


async def test_uom_allowlist_names_no_universal_unit_and_unit_codes_must_fit() -> None:
    world = World(uoms=(UNITS, 5), uom_unit_codes={5: ("DAY",), UNITS: ("C62",)})
    with pytest.raises(ProductLineCreatePolicyError, match=r"onaylı birimler: #1, #5"):
        await world.run(world.command(uom_id=4))
    with pytest.raises(ProductLineCreatePolicyError, match=r"faturadaki birimle \(C62\) uyumlu değil"):
        await world.run(world.command(uom_id=5))
    assert world.odoo.template_creates == []

    result = await world.run(world.command(uom_id=UNITS))
    assert world.odoo.templates[result.product_template_id]["uom_id"] == UNITS


async def test_uom_without_a_unit_code_entry_is_only_allowlist_checked() -> None:
    world = World(uoms=(UNITS, 5), uom_unit_codes={5: ("DAY",)})
    result = await world.run(world.command(uom_id=UNITS))
    assert result.created_product


async def test_own_unresolved_attempt_from_an_older_version_is_recovered_after_the_grace(world: World) -> None:
    world.odoo.create_failure = "timeout"
    with pytest.raises(ProductLineCreateOutcomeUnknownError):
        await world.run(world.command())
    world.harness.review = replace(world.harness.review, version=3)  # the review moved meanwhile

    world.now = T0 + timedelta(minutes=5)
    with pytest.raises(ProductLineCreateInFlightError, match="önceki ürün oluşturma denemesinin"):
        await world.run(world.command())
    world.now = T0 + timedelta(minutes=31)
    result = await world.run(world.command())

    assert world.creation(version=2) is None
    assert world.creation(version=3).status is ProductLineCreationStatus.COMPLETED
    assert world.claim().owner_review_version == 3
    assert world.odoo.new_templates() == [900] and result.created_product


async def test_own_completed_creation_from_an_older_version_is_never_repeated(world: World) -> None:
    reasons = world.harness.review.review_reasons
    await world.run(world.command())
    # contrived: the mapping vanished and the line is unmatched again at the new version
    world.odoo.supplierinfo = [row for row in world.odoo.supplierinfo if row.get("product_code") != "TFZP"]
    world.harness.review = replace(world.harness.review, review_reasons=reasons)

    with pytest.raises(ProductLineCreateDuplicateError, match="önceki bir sürümde zaten yeni ürün"):
        await world.run(world.command(product_name="Başka Ad"))
    assert len(world.odoo.template_creates) == 1


class _StaleClaims:
    """A claim store whose read misses a concurrently committed claim (race window)."""

    def __init__(self, inner: SqlAlchemyReviewProductIdentityClaimRepository) -> None:
        self.inner = inner

    def find(self, **kwargs):
        return None

    def claim(self, claim):
        return self.inner.claim(claim)

    def release(self, claim):
        return self.inner.release(claim)


async def test_concurrent_claim_by_another_line_loses_cleanly(world: World) -> None:
    world.claims.claim(replace(_creation().identity_claim(), owner_review_id="review:other"))
    world.session.commit()
    world.use_case._claims = _StaleClaims(world.claims)

    with pytest.raises(ProductLineCreateInFlightError, match="başka bir yeni ürün isteği"):
        await world.run(world.command())

    assert world.odoo.template_creates == [] and world.creation() is None
    assert world.harness.consumed == []  # the authorization claim was rolled back too


async def test_concurrent_insert_for_the_same_line_loses_cleanly(world: World) -> None:
    world.repository.insert_attempt(_creation(intent=_intent(product_name="Diğer istek")))
    world.session.commit()

    class _StaleRepository:
        def __init__(self, inner):
            self.inner = inner

        def find(self, **kwargs):
            return None

        def __getattr__(self, name):
            return getattr(self.inner, name)

    world.use_case._creations = _StaleRepository(world.repository)

    with pytest.raises(ProductLineCreateInFlightError):
        await world.run(world.command())

    assert world.odoo.template_creates == []
    assert world.claim() is None  # this request's claim was rolled back with the failed insert


async def test_long_invoice_description_is_bounded_on_the_supplier_mapping(world: World) -> None:
    world.harness.invoice = _invoice(_line("1", "TFZP", "Ç" * 600, "2"), *world.harness.invoice.lines[1:])

    await world.run(world.command())

    (payload,) = world.odoo.template_creates
    assert len(payload["seller_ids"][0][2]["product_name"]) == 512
    assert len(world.creation().supplier_product_name) == 512


def test_identity_claims_name_their_owner_table() -> None:
    session = _session()
    claims = SqlAlchemyReviewProductIdentityClaimRepository(session)
    line_claim = _creation().identity_claim()
    assert line_claim.owner_kind == "product_line_creation"
    claims.claim(line_claim)
    v1_claim = replace(line_claim, seller_item_code="OTHER", owner_kind="remediation_reservation")
    claims.claim(v1_claim)
    assert (
        claims.find(company_id=COMPANY, resolved_supplier_partner_id=ICT_BULUT, seller_item_code="TFZP") == line_claim
    )
    assert claims.find(company_id=COMPANY, resolved_supplier_partner_id=ICT_BULUT, seller_item_code="OTHER") == v1_claim
    # release matches the owner kind too: a v1 claim is never released by PR D
    claims.release(replace(v1_claim, owner_kind="product_line_creation"))
    assert claims.find(company_id=COMPANY, resolved_supplier_partner_id=ICT_BULUT, seller_item_code="OTHER") == v1_claim
    with pytest.raises(ProductRemediationContractError):
        replace(line_claim, owner_kind="something_else")
    assert (
        ProductIdentityClaim(
            **{
                f: getattr(line_claim, f)
                for f in (
                    "company_id",
                    "resolved_supplier_partner_id",
                    "seller_item_code",
                    "owner_review_id",
                    "owner_company_id",
                    "owner_review_version",
                    "owner_line_number",
                )
            }
        ).owner_kind
        == "remediation_reservation"
    )


async def test_rest_create_claim_on_the_same_line_is_never_taken_over(world: World) -> None:
    world.claims.claim(replace(_creation(review_version=1).identity_claim(), owner_kind="remediation_reservation"))
    world.session.commit()

    with pytest.raises(ProductLineCreateDuplicateError, match="başka bir inceleme satırında"):
        await world.run(world.command())
    assert world.odoo.template_creates == []
