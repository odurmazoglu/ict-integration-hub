"""Controlled Odoo supplier partner writer (P0-3D2C).

A narrow, gated, VAT-idempotent capability to create exactly one ``res.partner``
from immutable supplier identity. ``PartnerMatchingEngine`` stays read-only; this
writer is the only sanctioned ``res.partner`` create path and is impossible to
reach with default settings.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from app.application.commands import CreateSupplierPartnerCommand
from app.application.dto import SupplierPartnerWriteStatus
from app.application.exceptions.supplier_partner import (
    SupplierPartnerAmbiguityError,
    SupplierPartnerClassificationUnavailableError,
    SupplierPartnerDataIntegrityError,
    SupplierPartnerDuplicateRaceError,
    SupplierPartnerInactiveError,
    SupplierPartnerWriteSafetyGateError,
    SupplierPartnerWriteValidationError,
)
from app.application.partner_classification import PartnerClassificationOutcome, SupplierPartnerClassification
from app.connectors.exceptions import ConnectorAuthenticationError
from app.core.config import Settings
from app.core.runtime_checks import PRODUCTION_APPROVAL_ACK
from app.erp.write import (
    OdooPartnerClassificationFieldConfig,
    OdooSupplierPartnerRepository,
    OdooSupplierPartnerWritePolicy,
    OdooSupplierPartnerWriter,
)

COMPANY_ID = 1
VKN = "0430367181"
NAME = "Akyaşam Yönetim Hizmetleri A.Ş."
STAGING_HOST_URL = "https://test-ictteknoloji.odoo.com"
SECRET_MARKER = "sk-super-secret-odoo-key"
FIELD = "x_studio_musteri_tipi"
#: Production selection keys (verified read-only) plus the new expense_vendor key.
PRODUCTION_KEYS = ("customer", "prospect", "vendor", "partner", "Karma", "expense_vendor")


class FakeJson2Client:
    def __init__(
        self,
        *,
        create_result: Any = 501,
        search_results: list[Any] | None = None,
        search_sequence: list[Any] | None = None,
        field_metadata: list[dict[str, Any]] | None = None,
        selection_values: tuple[str, ...] = PRODUCTION_KEYS,
    ) -> None:
        self.field_metadata = field_metadata if field_metadata is not None else [{"name": FIELD, "ttype": "selection"}]
        self.selection_values = selection_values
        self.metadata_calls: list[dict[str, Any]] = []
        self.create_result = create_result
        self.search_results = search_results
        self.search_sequence = list(search_sequence) if search_sequence is not None else None
        self.create_calls: list[dict[str, Any]] = []
        self.search_calls: list[dict[str, Any]] = []

    async def create_res_partner(self, payload: dict[str, Any]) -> int:
        self.create_calls.append(payload)
        if isinstance(self.create_result, Exception):
            raise self.create_result
        return self.create_result

    async def search_read(
        self,
        *,
        model: str,
        domain: list[Any],
        fields: list[str],
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        self.search_calls.append({"model": model, "domain": domain, "fields": fields, "limit": limit})
        if self.search_sequence is not None:
            result = self.search_sequence.pop(0)
        else:
            result = self.search_results if self.search_results is not None else []
        if isinstance(result, Exception):
            raise result
        return result

    async def read_model_field_metadata(self, *, model: str, field_name: str) -> list[dict[str, Any]]:
        self.metadata_calls.append({"kind": "field", "model": model, "field_name": field_name})
        return self.field_metadata

    async def read_field_selection_values(self, *, model: str, field_name: str) -> tuple[str, ...]:
        self.metadata_calls.append({"kind": "selection", "model": model, "field_name": field_name})
        return self.selection_values


def _partner_row(
    *,
    partner_id: int = 501,
    name: str = NAME,
    vat: str = VKN,
    active: bool = True,
    company_id: Any = False,
    classification: Any = "vendor",
) -> dict[str, Any]:
    return {
        "id": partner_id,
        "name": name,
        "vat": vat,
        "active": active,
        "company_id": company_id,
        FIELD: classification,
    }


def _command(**overrides: Any) -> CreateSupplierPartnerCommand:
    kwargs: dict[str, Any] = {
        "company_id": COMPANY_ID,
        "supplier_name": NAME,
        "supplier_tax_number": VKN,
        "idempotency_key": "supplier-remediation:1:0430367181",
        "classification": SupplierPartnerClassification.VENDOR,
        "approved_by": "finance.operator",
    }
    kwargs.update(overrides)
    return CreateSupplierPartnerCommand(**kwargs)


def _enabled_policy() -> OdooSupplierPartnerWritePolicy:
    return OdooSupplierPartnerWritePolicy(
        supplier_remediation_write_enabled=True,
        app_env="staging",
        odoo_host="test-ictteknoloji.odoo.com",
    )


def _writer(
    client: FakeJson2Client,
    *,
    policy: OdooSupplierPartnerWritePolicy | None = None,
    field_name: str | None = FIELD,
) -> OdooSupplierPartnerWriter:
    return OdooSupplierPartnerWriter(
        repository=OdooSupplierPartnerRepository(client=client),
        policy=policy if policy is not None else _enabled_policy(),
        classification_config=OdooPartnerClassificationFieldConfig(field_name=field_name),
    )


# --------------------------------------------------------- Phase 24: CREATED


async def test_creates_supplier_partner_when_no_exact_vat_match(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeJson2Client(create_result=777, search_sequence=[[], [_partner_row(partner_id=777)]])
    result = await _writer(client).create_supplier(_command())

    assert result.status is SupplierPartnerWriteStatus.CREATED
    assert result.partner_id == 777
    assert result.company_id == COMPANY_ID
    assert result.supplier_tax_number == VKN
    assert len(client.create_calls) == 1
    assert client.create_calls[0] == {"name": NAME, "vat": VKN, FIELD: "vendor"}
    assert result.classification_outcome is PartnerClassificationOutcome.CLASSIFIED_ON_CREATE
    assert result.classification_value == "vendor"
    # exact-VAT search before create, then the post-create re-query/read-back.
    assert len(client.search_calls) == 2
    assert client.search_calls[0]["domain"] == [
        ["vat", "=", VKN],
        ["company_id", "in", [COMPANY_ID, False]],
        ["active", "in", [True, False]],
    ]


# --------------------------------------------------------- Phase 25: ALREADY_EXISTS


async def test_returns_already_exists_for_single_exact_vat_match() -> None:
    client = FakeJson2Client(search_results=[_partner_row(partner_id=42)])
    result = await _writer(client).create_supplier(_command())

    assert result.status is SupplierPartnerWriteStatus.ALREADY_EXISTS
    assert result.partner_id == 42
    assert result.existing_by == "vat"
    assert result.name_mismatch is False
    assert client.create_calls == []


# --------------------------------------------------------- Phase 26: AMBIGUOUS


async def test_multiple_exact_vat_matches_fail_closed() -> None:
    client = FakeJson2Client(search_results=[_partner_row(partner_id=1), _partner_row(partner_id=2)])
    with pytest.raises(SupplierPartnerAmbiguityError):
        await _writer(client).create_supplier(_command())
    assert client.create_calls == []


# --------------------------------------------------------- Phase 27: INACTIVE


async def test_archived_exact_vat_partner_fails_closed_without_reactivation() -> None:
    client = FakeJson2Client(search_results=[_partner_row(partner_id=9, active=False)])
    with pytest.raises(SupplierPartnerInactiveError):
        await _writer(client).create_supplier(_command())
    # No create and no write of any kind: no reactivation, no duplicate.
    assert client.create_calls == []


# ------------------------------------------- archived match: always fail closed (09C reuse retired)


def test_create_command_no_longer_accepts_an_inactive_reuse_predicate() -> None:
    """The P0-PROD-09C archived-reuse predicate was retired with the archive lifecycle."""

    with pytest.raises(TypeError):
        _command(authorize_inactive_reuse=lambda partner_id: True)


async def test_archived_exact_vat_partner_fails_closed_for_expense_vendor_too() -> None:
    client = FakeJson2Client(search_results=[_partner_row(partner_id=9, active=False, classification="expense_vendor")])
    with pytest.raises(SupplierPartnerInactiveError):
        await _writer(client).create_supplier(_command(classification=SupplierPartnerClassification.EXPENSE_VENDOR))
    assert client.create_calls == []


# --------------------------------------------------------- Phase 28: NAME MISMATCH


async def test_name_mismatch_on_existing_vat_does_not_rename_or_create() -> None:
    client = FakeJson2Client(search_results=[_partner_row(partner_id=55, name="AKYASAM YONETIM HIZMETLERI ANONIM")])
    result = await _writer(client).create_supplier(_command())

    assert result.status is SupplierPartnerWriteStatus.ALREADY_EXISTS
    assert result.partner_id == 55
    assert result.name_mismatch is True
    assert any("differs" in warning for warning in result.warnings)
    assert client.create_calls == []


# --------------------------------------------------------- Phase 29 / 15: GATE OFF


async def test_default_settings_cannot_create_supplier() -> None:
    default_policy = OdooSupplierPartnerWritePolicy.from_settings(Settings())
    assert default_policy.supplier_remediation_write_enabled is False
    client = FakeJson2Client()
    with pytest.raises(SupplierPartnerWriteSafetyGateError):
        await _writer(client, policy=default_policy).create_supplier(_command())
    assert client.create_calls == []
    assert client.search_calls == []
    assert client.metadata_calls == []


async def test_bare_default_policy_cannot_create_supplier() -> None:
    client = FakeJson2Client()
    with pytest.raises(SupplierPartnerWriteSafetyGateError):
        await _writer(client, policy=OdooSupplierPartnerWritePolicy()).create_supplier(_command())
    assert client.create_calls == []
    assert client.search_calls == []
    assert client.metadata_calls == []


async def test_flag_off_policy_refuses_before_any_odoo_call() -> None:
    policy = OdooSupplierPartnerWritePolicy(supplier_remediation_write_enabled=False, app_env="staging")
    client = FakeJson2Client()
    with pytest.raises(SupplierPartnerWriteSafetyGateError):
        await _writer(client, policy=policy).create_supplier(_command())
    assert client.create_calls == []
    assert client.search_calls == []
    assert client.metadata_calls == []


async def test_staging_gate_requires_named_approver() -> None:
    client = FakeJson2Client(search_results=[])
    with pytest.raises(SupplierPartnerWriteSafetyGateError):
        await _writer(client).create_supplier(_command(approved_by=None))


async def test_production_gate_requires_full_acknowledgement() -> None:
    incomplete = OdooSupplierPartnerWritePolicy(
        supplier_remediation_write_enabled=True,
        app_env="production",
        production_operations_enabled=True,
        production_approval_ack="",
    )
    with pytest.raises(SupplierPartnerWriteSafetyGateError):
        await _writer(FakeJson2Client(), policy=incomplete).create_supplier(_command())

    complete = OdooSupplierPartnerWritePolicy(
        supplier_remediation_write_enabled=True,
        app_env="production",
        production_operations_enabled=True,
        production_approval_ack=PRODUCTION_APPROVAL_ACK,
    )
    client = FakeJson2Client(search_results=[_partner_row(partner_id=88)])
    result = await _writer(client, policy=complete).create_supplier(_command())
    assert result.status is SupplierPartnerWriteStatus.ALREADY_EXISTS


# --------------------------------------------------------- Phase 30: create response shape
#
# The authoritative JSON-2 create-response shape rejection (bool / [] / [a,b] / dict / str)
# is asserted against the real client in
# test_odoo_client.py::test_create_res_partner_rejects_unexpected_response_shapes.
# Here we only confirm the writer fails closed on a non-positive id from its client.


async def test_create_returning_non_positive_id_is_data_integrity_error() -> None:
    client = FakeJson2Client(create_result=0, search_sequence=[[]])
    with pytest.raises(SupplierPartnerDataIntegrityError):
        await _writer(client).create_supplier(_command())


async def test_create_returning_bool_id_is_data_integrity_error() -> None:
    client = FakeJson2Client(create_result=True, search_sequence=[[]])
    with pytest.raises(SupplierPartnerDataIntegrityError):
        await _writer(client).create_supplier(_command())


# --------------------------------------------------------- Phase 31: RETRY / idempotency


async def test_retry_after_create_returns_already_exists_without_second_create() -> None:
    first_client = FakeJson2Client(create_result=123, search_sequence=[[], [_partner_row(partner_id=123)]])
    first = await _writer(first_client).create_supplier(_command())
    assert first.status is SupplierPartnerWriteStatus.CREATED
    assert len(first_client.create_calls) == 1

    retry_client = FakeJson2Client(search_results=[_partner_row(partner_id=123)])
    retry = await _writer(retry_client).create_supplier(_command())
    assert retry.status is SupplierPartnerWriteStatus.ALREADY_EXISTS
    assert retry.partner_id == 123
    assert retry_client.create_calls == []


# --------------------------------------------------------- Phase 32: DUPLICATE RACE


async def test_post_create_duplicate_recheck_fails_closed() -> None:
    client = FakeJson2Client(
        create_result=200,
        search_sequence=[[], [_partner_row(partner_id=200), _partner_row(partner_id=201)]],
    )
    with pytest.raises(SupplierPartnerDuplicateRaceError):
        await _writer(client).create_supplier(_command())
    assert len(client.create_calls) == 1  # the create happened, but success is never reported


async def test_post_create_readback_mismatch_is_data_integrity_error() -> None:
    client = FakeJson2Client(create_result=200, search_sequence=[[], [_partner_row(partner_id=999)]])
    with pytest.raises(SupplierPartnerDataIntegrityError):
        await _writer(client).create_supplier(_command())


# --------------------------------------------------------- Phase 33: minimal payload


async def test_create_payload_contains_only_sanctioned_keys() -> None:
    """P0-PROD-08J: the payload is exactly {name, vat, <classification field>} -- not
    merely "no forbidden keys present". company_type does not exist on production
    res.partner ("Invalid field 'company_type' on 'res.partner'"); is_company was
    reported readonly and is not a safe substitute -- neither is ever written."""

    client = FakeJson2Client(create_result=1, search_sequence=[[], [_partner_row(partner_id=1)]])
    await _writer(client).create_supplier(_command())

    assert set(client.create_calls[0]) == {"name", "vat", FIELD}
    assert "company_type" not in client.create_calls[0]
    assert "is_company" not in client.create_calls[0]
    forbidden = {
        "email",
        "phone",
        "mobile",
        "street",
        "street2",
        "city",
        "zip",
        "country_id",
        "state_id",
        "property_account_payable_id",
        "property_account_receivable_id",
        "property_payment_term_id",
        "property_supplier_payment_term_id",
        "bank_ids",
        "category_id",
        "user_id",
        "supplier_rank",
        "customer_rank",
        "currency_id",
        "property_account_position_id",
        "company_id",
        "active",
        "company_type",
        "is_company",
    }
    assert forbidden.isdisjoint(client.create_calls[0])


def test_forbidden_token_guard_rejects_company_type_and_is_company_if_reintroduced() -> None:
    """P0-PROD-08J defense-in-depth: prove the guard itself works, not merely that
    today's payload happens to be clean. A future edit that reintroduces either field
    must fail loudly rather than silently reach Odoo."""

    from app.erp.write.odoo_supplier_partner_writer import _reject_forbidden_tokens

    with pytest.raises(SupplierPartnerWriteValidationError):
        _reject_forbidden_tokens({"name": NAME, "vat": VKN, "company_type": "company"})
    with pytest.raises(SupplierPartnerWriteValidationError):
        _reject_forbidden_tokens({"name": NAME, "vat": VKN, "is_company": True})


def test_writer_module_never_references_company_type_or_is_company_as_a_value() -> None:
    """Source-level regression guard: SUPPLIER_COMPANY_TYPE and the literal
    'company_type'/'is_company' res.partner fields must never reappear in this
    module outside of the forbidden-token guard itself and its explanatory
    comments (both of which legitimately name them)."""

    source = Path("app/erp/write/odoo_supplier_partner_writer.py").read_text(encoding="utf-8")
    assert "SUPPLIER_COMPANY_TYPE" not in source
    # The only reference allowed outside comments/docstrings is inside the
    # FORBIDDEN_RES_PARTNER_TOKENS frozenset itself -- check the actual payload dict
    # literal in isolation so the explanatory comment above it cannot mask a regression.
    payload_literal = source[source.index("payload = {") : source.index("_reject_forbidden_tokens(payload)")]
    assert "company_type" not in payload_literal
    assert "is_company" not in payload_literal


# --------------------------------------------------------- Phase 34: security / no secret leakage


async def test_connector_errors_are_translated_without_leaking_secrets() -> None:
    client = FakeJson2Client(search_results=ConnectorAuthenticationError("Odoo authentication failed."))
    with pytest.raises(Exception) as exc_info:  # noqa: B017 - translated safe error
        await _writer(client).create_supplier(_command())
    message = getattr(exc_info.value, "safe_message", str(exc_info.value))
    for marker in ("api_key", "authorization", "bearer", "password", "token", "secret", SECRET_MARKER):
        assert marker not in message.lower()


def test_writer_module_never_touches_secret_material() -> None:
    """Guards against embedding an HTTP Authorization header/bearer-token literal.

    P0-PROD-09F: the writer now takes a `write_authorization: WriteAuthorizationRecord`
    parameter (narrow runtime write authorization, unrelated to any HTTP header) --
    a bare `"authorization:"` substring match would false-positive on that Python
    type annotation. `'"authorization"'` (quoted) still catches an actual header-name
    or dict-key literal (e.g. `{"Authorization": f"Bearer {key}"}`) while leaving a
    plain identifier's own type annotation alone.
    """

    source = Path("app/erp/write/odoo_supplier_partner_writer.py").read_text(encoding="utf-8").lower()
    for marker in ("odoo_api_key", "get_secret_value", "x-odoo-database", '"authorization"', SECRET_MARKER.lower()):
        assert marker not in source


# --------------------------------------------------------- command DTO validation


@pytest.mark.parametrize(
    "overrides",
    [
        {"company_id": 0},
        {"company_id": True},
        {"supplier_name": "   "},
        {"supplier_tax_number": ""},
        {"idempotency_key": " "},
        {"approved_by": "  "},
        {"classification": "vendor"},
        {"classification": None},
    ],
)
def test_create_supplier_command_rejects_invalid_identity(overrides: dict[str, Any]) -> None:
    with pytest.raises(SupplierPartnerWriteValidationError):
        _command(**overrides)


def test_create_supplier_command_normalization_is_whitespace_strip_only() -> None:
    # Same semantics as PartnerMatchingEngine._clean: strip only, no prefix removal.
    from app.erp.write.odoo_supplier_partner_writer import _normalize_supplier_vat

    assert _normalize_supplier_vat("  0430367181 ") == "0430367181"
    assert _normalize_supplier_vat("TR0430367181") == "TR0430367181"


# --------------------------------------------------------- Phase 21/22/23: no wiring


def test_partner_matching_engine_never_creates_a_partner() -> None:
    source = Path("app/matching/partner.py").read_text(encoding="utf-8")
    for token in ("create", "SupplierPartnerWriter", "OdooSupplierPartnerWriter", "res.partner/create"):
        assert token not in source


@pytest.mark.parametrize(
    "module_path",
    [
        "app/application/use_cases/import_invoice.py",
        "app/application/use_cases/reclassify_review.py",
        "app/application/use_cases/review_classification_outcome.py",
        "app/application/rules/deterministic.py",
    ],
)
def test_import_and_reclassification_do_not_reference_the_supplier_writer(module_path: str) -> None:
    tree = ast.parse(Path(module_path).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    for name in imported:
        assert "supplier_partner" not in name
        assert "SupplierPartnerWriter" not in name


def test_no_api_router_exposes_supplier_creation() -> None:
    for router in Path("app/api/routers").glob("*.py"):
        source = router.read_text(encoding="utf-8")
        assert "SupplierPartnerWriter" not in source
        assert "create_supplier" not in source
        assert "resolve-supplier" not in source
        assert "create-supplier" not in source


# --------------------------------------------------------- ICT partner classification


class _DefaultApplyingOdoo(FakeJson2Client):
    """Stateful fake: applies a production-style ``ir.default`` (``customer``) to the
    classification field only when the create payload omits it -- exactly how Odoo
    defaults behave -- and stores whatever value results."""

    def __init__(self, *, default_value: str = "customer", force_value: str | None = None) -> None:
        super().__init__()
        self.records: list[dict[str, Any]] = []
        self.default_value = default_value
        self.force_value = force_value  # simulates an automation overwriting the value

    async def create_res_partner(self, payload: dict[str, Any]) -> int:
        self.create_calls.append(payload)
        record = {"id": 700 + len(self.records), "active": True, "company_id": False, **payload}
        record.setdefault(FIELD, self.default_value)
        if self.force_value is not None:
            record[FIELD] = self.force_value
        self.records.append(record)
        return record["id"]

    async def search_read(self, *, model, domain, fields, limit=20, offset=0):
        self.search_calls.append({"model": model, "domain": domain, "fields": fields, "limit": limit})
        vat = domain[0][2]
        return [{key: record.get(key, False) for key in fields} for record in self.records if record["vat"] == vat]


@pytest.mark.parametrize(
    ("classification", "expected"),
    [
        (SupplierPartnerClassification.VENDOR, "vendor"),
        (SupplierPartnerClassification.EXPENSE_VENDOR, "expense_vendor"),
    ],
)
async def test_create_sets_explicit_classification_so_odoo_default_customer_never_applies(
    classification: SupplierPartnerClassification, expected: str
) -> None:
    odoo = _DefaultApplyingOdoo(default_value="customer")
    result = await _writer(odoo).create_supplier(_command(classification=classification))

    assert odoo.create_calls == [{"name": NAME, "vat": VKN, FIELD: expected}]
    assert odoo.records[0][FIELD] == expected  # the ir.default never applied
    assert result.status is SupplierPartnerWriteStatus.CREATED
    assert result.classification_outcome is PartnerClassificationOutcome.CLASSIFIED_ON_CREATE
    assert result.classification_value == expected


async def test_readback_classification_overridden_by_odoo_fails_closed() -> None:
    """If Odoo (an automation, a default) stores anything but the Hub's explicit value,
    the create is never reported as a success."""

    odoo = _DefaultApplyingOdoo(force_value="customer")
    with pytest.raises(SupplierPartnerDataIntegrityError):
        await _writer(odoo).create_supplier(_command(classification=SupplierPartnerClassification.EXPENSE_VENDOR))


async def test_second_create_for_same_vat_reuses_the_partner_without_a_second_create() -> None:
    odoo = _DefaultApplyingOdoo()
    first = await _writer(odoo).create_supplier(_command(classification=SupplierPartnerClassification.EXPENSE_VENDOR))
    second = await _writer(odoo).create_supplier(_command(classification=SupplierPartnerClassification.EXPENSE_VENDOR))

    assert first.status is SupplierPartnerWriteStatus.CREATED
    assert second.status is SupplierPartnerWriteStatus.ALREADY_EXISTS
    assert second.partner_id == first.partner_id
    assert len(odoo.create_calls) == 1
    assert len(odoo.records) == 1
    assert second.classification_outcome is PartnerClassificationOutcome.ALREADY_CLASSIFIED


@pytest.mark.parametrize("existing", ["customer", "vendor", "partner", "Karma", "prospect", "something_new"])
async def test_existing_meaningful_classification_is_preserved_never_overwritten(existing: str) -> None:
    client = FakeJson2Client(search_results=[_partner_row(partner_id=42, classification=existing)])
    result = await _writer(client).create_supplier(
        _command(classification=SupplierPartnerClassification.EXPENSE_VENDOR)
    )

    assert result.status is SupplierPartnerWriteStatus.ALREADY_EXISTS
    assert result.classification_value == existing
    assert result.classification_outcome is PartnerClassificationOutcome.DIFFERENT_CLASSIFICATION_PRESERVED
    assert any("preserved" in warning for warning in result.warnings)
    assert client.create_calls == []  # and the writer has no update capability at all


@pytest.mark.parametrize("empty", [False, None, "", "   "])
async def test_existing_unclassified_partner_is_preserved_and_surfaced(empty: Any) -> None:
    client = FakeJson2Client(search_results=[_partner_row(partner_id=42, classification=empty)])
    result = await _writer(client).create_supplier(
        _command(classification=SupplierPartnerClassification.EXPENSE_VENDOR)
    )

    assert result.classification_outcome is PartnerClassificationOutcome.UNCLASSIFIED_PRESERVED
    assert result.classification_value is None
    assert client.create_calls == []


async def test_existing_partner_already_carrying_target_classification_has_no_warning() -> None:
    client = FakeJson2Client(search_results=[_partner_row(partner_id=42, classification="expense_vendor")])
    result = await _writer(client).create_supplier(
        _command(classification=SupplierPartnerClassification.EXPENSE_VENDOR)
    )

    assert result.classification_outcome is PartnerClassificationOutcome.ALREADY_CLASSIFIED
    assert result.warnings == ()


@pytest.mark.parametrize("field_name", [None, "", "   ", "active", "vat", "supplier_rank", "x__studio", "X_STUDIO_A"])
async def test_missing_or_malformed_classification_field_config_fails_before_any_odoo_call(
    field_name: str | None,
) -> None:
    client = FakeJson2Client(search_results=[])
    with pytest.raises(SupplierPartnerClassificationUnavailableError):
        await _writer(client, field_name=field_name).create_supplier(_command())
    assert client.metadata_calls == []
    assert client.search_calls == []
    assert client.create_calls == []


@pytest.mark.parametrize(
    "metadata",
    [
        [],
        [{"name": FIELD, "ttype": "char"}],
        [{"name": FIELD, "ttype": "selection"}, {"name": FIELD, "ttype": "selection"}],
    ],
)
async def test_classification_field_missing_or_not_selection_in_odoo_fails_closed(
    metadata: list[dict[str, Any]],
) -> None:
    client = FakeJson2Client(search_results=[], field_metadata=metadata)
    with pytest.raises(SupplierPartnerClassificationUnavailableError):
        await _writer(client).create_supplier(_command())
    assert client.search_calls == []
    assert client.create_calls == []


async def test_expense_vendor_key_not_yet_added_in_odoo_fails_closed() -> None:
    """Production today offers customer/prospect/vendor/partner/Karma only: ONE_OFF_VENDOR
    must refuse until the Studio key expense_vendor is added -- never fall back."""

    current_production_keys = ("customer", "prospect", "vendor", "partner", "Karma")
    client = FakeJson2Client(search_results=[], selection_values=current_production_keys)
    with pytest.raises(SupplierPartnerClassificationUnavailableError):
        await _writer(client).create_supplier(_command(classification=SupplierPartnerClassification.EXPENSE_VENDOR))
    assert client.search_calls == []
    assert client.create_calls == []

    # vendor is already offered, so CREATE_PERMANENT_SUPPLIER works against today's keys.
    ok = FakeJson2Client(
        create_result=5,
        search_sequence=[[], [_partner_row(partner_id=5, classification="vendor")]],
        selection_values=current_production_keys,
    )
    result = await _writer(ok).create_supplier(_command(classification=SupplierPartnerClassification.VENDOR))
    assert result.status is SupplierPartnerWriteStatus.CREATED


def test_classification_config_reads_settings_field() -> None:
    config = OdooPartnerClassificationFieldConfig.from_settings(Settings(odoo_partner_classification_field=FIELD))
    assert config.require_field() == FIELD
    with pytest.raises(SupplierPartnerClassificationUnavailableError):
        OdooPartnerClassificationFieldConfig.from_settings(Settings()).require_field()
