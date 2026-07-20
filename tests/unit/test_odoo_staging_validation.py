from typing import Any

from pydantic import SecretStr

from app.connectors.exceptions import ConnectorError
from app.core.config import Settings
from app.schemas.odoo import OdooProbeResponse
from app.services.odoo_staging_validation import STAGING_ODOO_HOST, OdooStagingValidationService


class FakeValidationOdooClient:
    def __init__(
        self,
        *,
        records: dict[str, list[dict[str, Any]]] | None = None,
        probe_error: Exception | None = None,
        model_errors: dict[str, Exception] | None = None,
    ) -> None:
        self.records = records or _records()
        self.probe_error = probe_error
        self.model_errors = model_errors or {}
        self.calls: list[dict[str, Any]] = []

    async def probe(self) -> OdooProbeResponse:
        self.calls.append({"method": "probe"})
        if self.probe_error is not None:
            raise self.probe_error
        return OdooProbeResponse(status="ok", company_id=7, company_name="Secret Company Name")

    async def search_read(
        self,
        *,
        model: str,
        domain: list[Any],
        fields: list[str],
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        self.calls.append({"method": "search_read", "model": model, "domain": domain, "fields": fields, "limit": limit})
        if model in self.model_errors:
            raise self.model_errors[model]
        return self.records.get(model, [])


async def test_staging_host_accepted_and_models_validated() -> None:
    client = FakeValidationOdooClient()
    report = await _validate(client=client)

    assert report["overall_status"] == "ok"
    assert report["target_host"] == STAGING_ODOO_HOST
    assert report["authentication"]["status"] == "ok"
    assert report["database_access"]["status"] == "ok"
    assert report["company"] == {"status": "ok", "company_id": 7}
    assert set(report["models"]) == {
        "res.company",
        "res.partner",
        "product.product",
        "account.tax",
        "res.currency",
        "account.journal",
    }
    assert all(model_report["status"] == "ok" for model_report in report["models"].values())
    assert report["purchase_journal"]["status"] == "ok"
    assert report["safety"]["no_write_operation_attempted"] is True
    assert all(call["method"] in {"probe", "search_read"} for call in client.calls)


async def test_production_host_rejected_before_network_calls() -> None:
    client = FakeValidationOdooClient()
    report = await _validate(settings=_settings(odoo_base_url="https://ictteknoloji.odoo.com"), client=client)

    assert report["overall_status"] == "failed"
    assert "ODOO_BASE_URL must point to" in report["configuration_failures"][0]
    assert client.calls == []


async def test_unsafe_host_rejected_before_network_calls() -> None:
    client = FakeValidationOdooClient()
    report = await _validate(settings=_settings(odoo_base_url="https://evil.example.com"), client=client)

    assert report["overall_status"] == "failed"
    assert report["target_host"] == "evil.example.com"
    assert client.calls == []


async def test_purchase_journal_id_and_code_rejected_before_network_calls() -> None:
    client = FakeValidationOdooClient()
    report = await _validate(
        settings=_settings(odoo_purchase_journal_id=55, odoo_purchase_journal_code="BILL"),
        client=client,
    )

    assert report["overall_status"] == "failed"
    assert report["configuration_failures"] == [
        "Configure only one of ODOO_PURCHASE_JOURNAL_ID or ODOO_PURCHASE_JOURNAL_CODE."
    ]
    assert report["authentication"]["status"] == "not_run"
    assert report["purchase_journal"]["status"] == "not_run"
    assert client.calls == []


async def test_authentication_failure_is_structured() -> None:
    report = await _validate(client=FakeValidationOdooClient(probe_error=ConnectorError("Odoo returned HTTP 401.")))

    assert report["overall_status"] == "failed"
    assert report["authentication"]["status"] == "permission_failure"
    assert report["database_access"]["status"] == "not_run"
    assert report["permission_failures"] == [{"target": "authentication", "message": "Odoo returned HTTP 401."}]


async def test_model_read_permission_failure_is_structured() -> None:
    report = await _validate(
        client=FakeValidationOdooClient(model_errors={"account.tax": ConnectorError("Odoo returned HTTP 403.")})
    )

    assert report["overall_status"] == "failed"
    assert report["models"]["account.tax"]["status"] == "permission_failure"
    assert {"target": "account.tax", "message": "Odoo returned HTTP 403."} in report["permission_failures"]


async def test_empty_model_read_is_reported_without_blocking_validation() -> None:
    report = await _validate(client=FakeValidationOdooClient(records={**_records(), "product.product": []}))

    assert report["overall_status"] == "ok"
    assert report["models"]["product.product"] == {"status": "empty", "records_sampled": 0}
    assert "One or more required Odoo models could not be read." not in report["blockers_for_resolution_validation"]


async def test_purchase_journal_missing_and_ambiguous_are_blockers() -> None:
    missing = await _validate(client=FakeValidationOdooClient(records={**_records(), "account.journal": []}))
    ambiguous = await _validate(
        client=FakeValidationOdooClient(
            records={
                **_records(),
                "account.journal": [
                    {"id": 55, "code": "BILL", "type": "purchase", "name": "Vendor Bills"},
                    {"id": 56, "code": "BILL", "type": "purchase", "name": "Vendor Bills 2"},
                ],
            }
        ),
        settings=_settings(odoo_purchase_journal_id=None, odoo_purchase_journal_code="BILL"),
    )

    assert missing["purchase_journal"]["status"] == "missing"
    assert missing["overall_status"] == "failed"
    assert ambiguous["purchase_journal"]["status"] == "ambiguous"
    assert ambiguous["overall_status"] == "failed"


async def test_secret_redaction_by_omission() -> None:
    report = await _validate(
        settings=_settings(
            odoo_api_key=SecretStr("super-secret-api-key"),
            database_url="postgresql+psycopg://user:secret-db-pass@localhost:5432/sensitive_db",
            odoo_database="sensitive-full-db-name",
        )
    )

    serialized = str(report)
    assert "super-secret-api-key" not in serialized
    assert "secret-db-pass" not in serialized
    assert "sensitive-full-db-name" not in serialized


async def test_no_write_capable_method_or_action_post_invoked() -> None:
    client = FakeValidationOdooClient()
    await _validate(client=client)

    assert {call["method"] for call in client.calls} == {"probe", "search_read"}
    assert "action_post" not in str(client.calls)
    assert "create" not in str(client.calls)
    assert "write" not in str(client.calls)
    assert "unlink" not in str(client.calls)


async def _validate(
    *,
    settings: Settings | None = None,
    client: FakeValidationOdooClient | None = None,
) -> dict[str, Any]:
    return await OdooStagingValidationService(
        settings=settings or _settings(),
        client=client or FakeValidationOdooClient(),
    ).validate()


def _settings(**overrides: Any) -> Settings:
    defaults: dict[str, Any] = {
        "app_env": "development",
        "odoo_base_url": f"https://{STAGING_ODOO_HOST}",
        "odoo_database": "staging-db",
        "odoo_api_key": SecretStr("secret"),
        "odoo_purchase_journal_id": 55,
        "database_url": "postgresql+psycopg://ict:ict@localhost:5432/ict_integration_hub",
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _records() -> dict[str, list[dict[str, Any]]]:
    return {
        "res.company": [{"id": 7, "name": "Company"}],
        "res.partner": [{"id": 101, "name": "Supplier", "active": True, "company_id": [7, "Company"]}],
        "product.product": [{"id": 301, "name": "Product", "default_code": "P1", "active": True}],
        "account.tax": [{"id": 401, "name": "VAT", "amount": 20, "type_tax_use": "purchase", "active": True}],
        "res.currency": [{"id": 33, "name": "TRY", "active": True}],
        "account.journal": [{"id": 55, "code": "BILL", "type": "purchase", "name": "Vendor Bills"}],
    }
