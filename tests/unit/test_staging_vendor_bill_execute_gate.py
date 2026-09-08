"""Sanctioned non-production Vendor Bill execute gate.

Covers the narrow staging carve-out that allows a draft ``account.move`` write outside
``APP_ENV=production`` without ever reusing the production operation flags and without
widening any other executable workflow type.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from app.application.execution.contracts import (
    ExecutionApproval,
    ExecutionMode,
    ExecutionPlan,
    ExecutionStep,
    ExecutionStepType,
)
from app.application.execution.exceptions import ExecutionModeNotEnabledError
from app.application.execution.preflight import ExecutionPreflightPolicy
from app.core.config import Settings
from app.core.runtime_checks import (
    APPROVED_STAGING_ODOO_HOSTS,
    PRODUCTION_APPROVAL_ACK,
    runtime_configuration_errors,
    staging_vendor_bill_execute_sanctioned,
    validate_runtime_configuration,
)
from app.erp.write import AccountMoveDraft, OdooVendorBillWritePolicy, VendorBillWriteSafetyGateError

APPROVED_STAGING_HOST = "test-ictteknoloji.odoo.com"
STAGING_URL = f"https://{APPROVED_STAGING_HOST}"
UNAPPROVED_HOST_MESSAGE = (
    "STAGING_VENDOR_BILL_EXECUTE_ENABLED=true requires ODOO_BASE_URL to be an approved staging host."
)

_CLEARED_ENV = (
    "APP_ENV",
    "APP_ENV_FILE",
    "EXECUTION_EXECUTE_ENABLED",
    "CUSTOMER_INVOICE_EXECUTE_ENABLED",
    "CUSTOMER_QUOTATION_EXECUTE_ENABLED",
    "STAGING_VENDOR_BILL_EXECUTE_ENABLED",
    "PRODUCTION_OPERATIONS_ENABLED",
    "PRODUCTION_APPROVAL_ACK",
    "LIVE_CONNECTOR_READONLY",
    "ODOO_BASE_URL",
    "ODOO_DATABASE",
    "ODOO_API_KEY",
    "UYUMSOFT_ENVIRONMENT",
    "UYUMSOFT_USERNAME",
    "UYUMSOFT_PASSWORD",
    "IPP_AUTH_MODE",
)


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Force ``Settings`` to resolve from defaults plus explicit kwargs only."""

    for key in _CLEARED_ENV:
        monkeypatch.delenv(key, raising=False)
    empty_profile = tmp_path / "empty.env"
    empty_profile.write_text("", encoding="utf-8")
    monkeypatch.setenv("APP_ENV_FILE", str(empty_profile))


def _dev_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"app_env": "development", "odoo_base_url": "https://example.odoo.com"}
    base.update(overrides)
    return Settings(**base)


def _valid_prod_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "app_env": "production",
        "database_url": "postgresql+psycopg://ict:pw@db.internal:5432/ict",
        "ipp_auth_mode": "oidc_jwt",
        "ipp_oidc_issuer": "https://idp.example.com/realms/ict",
        "ipp_oidc_audience": "ict-integration-hub",
        "ipp_oidc_jwks_url": "https://idp.example.com/realms/ict/protocol/openid-connect/certs",
        "production_operations_enabled": True,
        "production_approval_ack": PRODUCTION_APPROVAL_ACK,
        "odoo_base_url": "https://odoo.example-tenant.com",
        "odoo_database": "ict-prod",
        "odoo_api_key": SecretStr("replace-with-real-secret"),
        "odoo_purchase_journal_id": 10,
        "uyumsoft_environment": "production",
        "uyumsoft_username": "uyumsoft-prod-user",
        "uyumsoft_password": SecretStr("replace-with-real-secret"),
    }
    base.update(overrides)
    return Settings(**base)


# --------------------------------------------------------------------------------------
# Runtime configuration
# --------------------------------------------------------------------------------------


def test_staging_flag_defaults_to_false() -> None:
    settings = _dev_settings()

    assert settings.staging_vendor_bill_execute_enabled is False
    assert staging_vendor_bill_execute_sanctioned(settings) is False


def test_non_production_without_staging_flag_is_valid() -> None:
    assert runtime_configuration_errors(_dev_settings()) == []


def test_non_production_staging_flag_with_exact_approved_host_is_valid() -> None:
    settings = _dev_settings(
        staging_vendor_bill_execute_enabled=True,
        odoo_base_url=STAGING_URL,
        execution_execute_enabled=True,
    )

    assert runtime_configuration_errors(settings) == []
    assert staging_vendor_bill_execute_sanctioned(settings) is True
    validate_runtime_configuration(settings)


def test_execution_execute_enabled_without_staging_sanction_is_rejected() -> None:
    settings = _dev_settings(execution_execute_enabled=True)

    assert "EXECUTION_EXECUTE_ENABLED must be false outside production." in runtime_configuration_errors(settings)


def test_staging_flag_with_unknown_host_is_rejected() -> None:
    settings = _dev_settings(staging_vendor_bill_execute_enabled=True, odoo_base_url="https://unknown-tenant.odoo.com")

    assert UNAPPROVED_HOST_MESSAGE in runtime_configuration_errors(settings)
    assert staging_vendor_bill_execute_sanctioned(settings) is False


def test_staging_flag_with_production_like_host_is_rejected() -> None:
    settings = _dev_settings(staging_vendor_bill_execute_enabled=True, odoo_base_url="https://odoo.example-tenant.com")

    assert UNAPPROVED_HOST_MESSAGE in runtime_configuration_errors(settings)


def test_staging_flag_with_localhost_is_rejected() -> None:
    settings = _dev_settings(staging_vendor_bill_execute_enabled=True, odoo_base_url="http://localhost:8069")

    assert UNAPPROVED_HOST_MESSAGE in runtime_configuration_errors(settings)


def test_staging_flag_with_example_host_is_rejected() -> None:
    settings = _dev_settings(staging_vendor_bill_execute_enabled=True, odoo_base_url="https://example.odoo.com")

    assert UNAPPROVED_HOST_MESSAGE in runtime_configuration_errors(settings)


@pytest.mark.parametrize(
    "host",
    [
        "ictteknoloji.odoo.com",
        "evil.test-ictteknoloji.odoo.com",
        "test-ictteknoloji.odoo.com.example.com",
        "127.0.0.1",
    ],
)
def test_staging_flag_rejects_near_miss_hosts(host: str) -> None:
    settings = _dev_settings(staging_vendor_bill_execute_enabled=True, odoo_base_url=f"https://{host}")

    assert UNAPPROVED_HOST_MESSAGE in runtime_configuration_errors(settings)
    assert staging_vendor_bill_execute_sanctioned(settings) is False


def test_production_with_staging_flag_true_is_rejected() -> None:
    settings = _valid_prod_settings(staging_vendor_bill_execute_enabled=True)

    errors = runtime_configuration_errors(settings)

    assert "STAGING_VENDOR_BILL_EXECUTE_ENABLED must be false in production." in errors
    assert staging_vendor_bill_execute_sanctioned(settings) is False


def test_production_with_staging_flag_true_on_staging_host_is_still_rejected() -> None:
    settings = _valid_prod_settings(staging_vendor_bill_execute_enabled=True, odoo_base_url=STAGING_URL)

    assert "STAGING_VENDOR_BILL_EXECUTE_ENABLED must be false in production." in runtime_configuration_errors(settings)


def test_production_valid_configuration_still_passes_unchanged() -> None:
    validate_runtime_configuration(_valid_prod_settings())


def test_production_missing_operations_gate_still_rejected() -> None:
    errors = runtime_configuration_errors(_valid_prod_settings(production_operations_enabled=False))

    assert "PRODUCTION_OPERATIONS_ENABLED must be true in production." in errors


def test_production_missing_approval_ack_still_rejected() -> None:
    errors = runtime_configuration_errors(_valid_prod_settings(production_approval_ack=""))

    assert "PRODUCTION_APPROVAL_ACK must confirm manual production approval." in errors


def test_non_production_still_rejects_production_operations_flag_even_with_staging_sanction() -> None:
    settings = _dev_settings(
        staging_vendor_bill_execute_enabled=True,
        odoo_base_url=STAGING_URL,
        production_operations_enabled=True,
    )

    assert "PRODUCTION_OPERATIONS_ENABLED must be false outside production." in runtime_configuration_errors(settings)


def test_non_production_still_rejects_production_approval_ack_even_with_staging_sanction() -> None:
    settings = _dev_settings(
        staging_vendor_bill_execute_enabled=True,
        odoo_base_url=STAGING_URL,
        production_approval_ack=PRODUCTION_APPROVAL_ACK,
    )

    assert "PRODUCTION_APPROVAL_ACK must be empty outside production." in runtime_configuration_errors(settings)


def test_approved_staging_host_allowlist_is_exact_and_code_owned() -> None:
    assert APPROVED_STAGING_ODOO_HOSTS == frozenset({APPROVED_STAGING_HOST})


# --------------------------------------------------------------------------------------
# Vendor Bill write policy
# --------------------------------------------------------------------------------------


def _staging_policy(**overrides: object) -> OdooVendorBillWritePolicy:
    base: dict[str, object] = {
        "app_env": "development",
        "staging_vendor_bill_execute_enabled": True,
        "odoo_host": APPROVED_STAGING_HOST,
    }
    base.update(overrides)
    return OdooVendorBillWritePolicy(**base)


def _production_policy(**overrides: object) -> OdooVendorBillWritePolicy:
    base: dict[str, object] = {
        "app_env": "production",
        "production_operations_enabled": True,
        "production_approval_ack": PRODUCTION_APPROVAL_ACK,
    }
    base.update(overrides)
    return OdooVendorBillWritePolicy(**base)


def test_production_policy_allows_real_write_with_full_gates() -> None:
    _production_policy().ensure_real_write_allowed(approved_by="finance.lead")


@pytest.mark.parametrize(
    "overrides",
    [{"production_operations_enabled": False}, {"production_approval_ack": ""}],
)
def test_production_policy_rejects_incomplete_gates(overrides: dict[str, object]) -> None:
    with pytest.raises(VendorBillWriteSafetyGateError):
        _production_policy(**overrides).ensure_real_write_allowed(approved_by="finance.lead")


def test_staging_policy_allows_real_write_with_flag_host_and_approver() -> None:
    policy = _staging_policy()

    policy.ensure_real_write_allowed(approved_by="staging.operator")

    assert policy.staging_write_sanctioned is True


def test_staging_policy_without_flag_falls_back_to_production_gate() -> None:
    with pytest.raises(VendorBillWriteSafetyGateError):
        _staging_policy(staging_vendor_bill_execute_enabled=False).ensure_real_write_allowed(
            approved_by="staging.operator"
        )


@pytest.mark.parametrize(
    "host",
    ["ictteknoloji.odoo.com", "evil.test-ictteknoloji.odoo.com", "test-ictteknoloji.odoo.com.example.com", ""],
)
def test_staging_policy_rejects_wrong_host(host: str) -> None:
    policy = _staging_policy(odoo_host=host)

    assert policy.staging_write_sanctioned is False
    with pytest.raises(VendorBillWriteSafetyGateError):
        policy.ensure_real_write_allowed(approved_by="staging.operator")


@pytest.mark.parametrize("approver", [None, "", "   "])
def test_staging_policy_requires_named_approver(approver: str | None) -> None:
    with pytest.raises(VendorBillWriteSafetyGateError):
        _staging_policy().ensure_real_write_allowed(approved_by=approver)


def test_staging_policy_does_not_require_production_approval_ack() -> None:
    policy = _staging_policy(production_approval_ack="")

    policy.ensure_real_write_allowed(approved_by="staging.operator")


def test_staging_policy_does_not_require_production_operations_enabled() -> None:
    policy = _staging_policy(production_operations_enabled=False)

    policy.ensure_real_write_allowed(approved_by="staging.operator")


def test_staging_policy_is_inert_in_production_environment() -> None:
    policy = _staging_policy(app_env="production")

    assert policy.staging_write_sanctioned is False
    with pytest.raises(VendorBillWriteSafetyGateError):
        policy.ensure_real_write_allowed(approved_by="staging.operator")


@pytest.mark.parametrize(
    ("url", "sanctioned"),
    [
        (STAGING_URL, True),
        ("https://TEST-ICTTEKNOLOJI.odoo.com", True),
        ("https://test-ictteknoloji.odoo.com/", True),
        ("https://ictteknoloji.odoo.com", False),
        ("https://evil.test-ictteknoloji.odoo.com", False),
        ("https://test-ictteknoloji.odoo.com.example.com", False),
        ("http://localhost", False),
        ("http://127.0.0.1:8069", False),
        ("https://example.odoo.com", False),
    ],
)
def test_from_settings_normalizes_host_and_sanctions_only_exact_match(url: str, sanctioned: bool) -> None:
    policy = OdooVendorBillWritePolicy.from_settings(
        _dev_settings(staging_vendor_bill_execute_enabled=True, odoo_base_url=url)
    )

    assert policy.staging_write_sanctioned is sanctioned


def test_from_settings_carries_production_gate_semantics() -> None:
    policy = OdooVendorBillWritePolicy.from_settings(_valid_prod_settings())

    assert policy.staging_write_sanctioned is False
    policy.ensure_real_write_allowed(approved_by="finance.lead")


# --------------------------------------------------------------------------------------
# Execution preflight scope isolation
# --------------------------------------------------------------------------------------

_APPROVAL = ExecutionApproval(approved_by="staging.operator")


def _plan(*step_types: ExecutionStepType, mode: ExecutionMode = ExecutionMode.EXECUTE) -> ExecutionPlan:
    steps = tuple(
        ExecutionStep(
            step_key=f"review-1:2:{step_type.value}:{index}",
            step_type=step_type,
            allocation_keys=(),
            sequence=index + 1,
            execute_supported=True,
            writer_required=True,
            customer_quotation_scenario_id=(
                f"scn-{index}" if step_type is ExecutionStepType.CREATE_CUSTOMER_QUOTATION else None
            ),
        )
        for index, step_type in enumerate(step_types)
    )
    return ExecutionPlan(
        execution_id="exec-1",
        review_id="review-1",
        company_id=7,
        decision_version=2,
        mode=mode,
        steps=steps,
    )


def _staging_preflight() -> ExecutionPreflightPolicy:
    return ExecutionPreflightPolicy(
        production_execution_enabled=True,
        real_write_gates={ExecutionStepType.VENDOR_BILL: _staging_policy()},
        staging_execution_step_types=(ExecutionStepType.VENDOR_BILL,),
    )


def test_staging_preflight_allows_vendor_bill_execute_step() -> None:
    _staging_preflight().ensure_execute_allowed(plan=_plan(ExecutionStepType.VENDOR_BILL), approval=_APPROVAL)


@pytest.mark.parametrize(
    "step_type",
    [
        ExecutionStepType.CUSTOMER_RECHARGE,
        ExecutionStepType.EXISTING_PURCHASE_ORDER,
        ExecutionStepType.NEW_RFQ_PURCHASE,
        ExecutionStepType.SUBSCRIPTION_SERVICE,
        ExecutionStepType.OPERATING_EXPENSE,
        ExecutionStepType.FIXED_ASSET,
        ExecutionStepType.PROJECT_COST,
        ExecutionStepType.INTERNAL_COST,
        ExecutionStepType.SALES_ORDER_COST_LINK,
        ExecutionStepType.CREATE_CUSTOMER_QUOTATION,
    ],
)
def test_staging_preflight_blocks_every_non_vendor_bill_step(step_type: ExecutionStepType) -> None:
    with pytest.raises(ExecutionModeNotEnabledError):
        _staging_preflight().ensure_execute_allowed(plan=_plan(step_type), approval=_APPROVAL)


def test_staging_preflight_blocks_mixed_plan_containing_non_vendor_bill_step() -> None:
    with pytest.raises(ExecutionModeNotEnabledError):
        _staging_preflight().ensure_execute_allowed(
            plan=_plan(ExecutionStepType.VENDOR_BILL, ExecutionStepType.EXISTING_PURCHASE_ORDER),
            approval=_APPROVAL,
        )


def test_staging_preflight_ignores_dry_run_plans() -> None:
    _staging_preflight().ensure_execute_allowed(
        plan=_plan(ExecutionStepType.EXISTING_PURCHASE_ORDER, mode=ExecutionMode.DRY_RUN),
        approval=None,
    )


def test_non_staging_preflight_is_unchanged_for_vendor_bill_plan() -> None:
    policy = ExecutionPreflightPolicy(
        production_execution_enabled=True,
        real_write_gates={ExecutionStepType.VENDOR_BILL: _production_policy()},
    )

    policy.ensure_execute_allowed(
        plan=_plan(ExecutionStepType.VENDOR_BILL),
        approval=ExecutionApproval(approved_by="finance.lead"),
    )


def test_non_staging_preflight_still_blocks_execute_when_execution_disabled() -> None:
    policy = ExecutionPreflightPolicy(production_execution_enabled=False)

    with pytest.raises(ExecutionModeNotEnabledError):
        policy.ensure_execute_allowed(plan=_plan(ExecutionStepType.VENDOR_BILL), approval=_APPROVAL)


# --------------------------------------------------------------------------------------
# Architecture guards
# --------------------------------------------------------------------------------------


def test_composition_derives_staging_scope_from_write_policy() -> None:
    source = Path("app/composition/execution.py").read_text(encoding="utf-8")

    assert "vendor_bill_policy.staging_write_sanctioned" in source
    assert "staging_execution_step_types=(" in source
    assert "(ExecutionStepType.VENDOR_BILL,) if staging_vendor_bill_execute else ()" in source


@pytest.mark.parametrize(
    "path",
    [
        "app/application/execution/vendor_bill_strategy.py",
        "app/application/execution/strategy.py",
        "app/erp/write/account_move_repository.py",
        "app/api/routers/workbench.py",
    ],
)
def test_no_staging_authorization_logic_leaks_into_strategy_router_or_repository(path: str) -> None:
    text = Path(path).read_text(encoding="utf-8").lower()

    assert "staging_vendor_bill_execute" not in text
    assert "staging_write_sanctioned" not in text
    assert "staging_execution_step_types" not in text
    assert "approved_staging_odoo_hosts" not in text


def test_account_move_repository_still_forbids_post_and_destructive_fields() -> None:
    from app.erp.write.account_move_repository import (
        FORBIDDEN_ACCOUNT_MOVE_FIELDS,
        IDEMPOTENCY_FIELD,
        _validate_payload,
    )
    from app.erp.write.exceptions import VendorBillWriteValidationError

    assert {"action_post", "unlink", "payment_id"}.issubset(FORBIDDEN_ACCOUNT_MOVE_FIELDS)
    assert IDEMPOTENCY_FIELD == "invoice_origin"
    with pytest.raises(VendorBillWriteValidationError):
        _validate_payload(
            {
                "move_type": "in_invoice",
                "company_id": 7,
                "partner_id": 11,
                "ref": "INV-1",
                "invoice_line_ids": [(0, 0, {"name": "x"})],
                "action_post": True,
            }
        )


class _RecordingDraftRepository:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def find_existing_vendor_bill(
        self,
        *,
        vendor_bill: object,
        idempotency_key: str,
        company_id: int | None = None,
    ) -> AccountMoveDraft | None:
        self.calls.append(("find", idempotency_key))
        return None

    async def create_draft_vendor_bill(
        self,
        *,
        vendor_bill: object,
        idempotency_key: str,
        company_id: int | None = None,
    ) -> AccountMoveDraft:
        self.calls.append(("create", idempotency_key))
        return AccountMoveDraft(id=4321)


async def test_staging_write_still_runs_idempotency_check_before_create() -> None:
    from datetime import date
    from decimal import Decimal

    from app.application.commands import VendorBillWriteCommand
    from app.billing import VendorBill, VendorBillLine
    from app.erp.write import OdooVendorBillWriter

    repository = _RecordingDraftRepository()
    writer = OdooVendorBillWriter(repository=repository, policy=_staging_policy())
    vendor_bill = VendorBill(
        supplier_id=101,
        invoice_number="INV-1",
        invoice_date=date(2026, 8, 1),
        currency="TRY",
        external_uuid="uuid-1",
        reference="INV-1",
        company_id=7,
        invoice_lines=(
            VendorBillLine(
                product_id=501,
                quantity=Decimal("1"),
                uom="NIU",
                unit_price=Decimal("10.00"),
                tax_ids=(401,),
                description="Line 1",
            ),
        ),
    )

    result = await writer.write_vendor_bill(
        VendorBillWriteCommand(
            vendor_bill=vendor_bill,
            idempotency_key="ettn-1",
            dry_run=False,
            approved_by="staging.operator",
        )
    )

    assert result.status == "created"
    assert repository.calls == [("find", "ettn-1"), ("create", "ettn-1")]
