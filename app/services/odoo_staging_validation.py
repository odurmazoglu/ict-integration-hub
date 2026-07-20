from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from app.connectors.exceptions import ConnectorError, ConnectorTimeoutError
from app.connectors.odoo.client import OdooJson2Client
from app.core.config import Settings

STAGING_ODOO_HOST = "test-ictteknoloji.odoo.com"
READ_ONLY_VALIDATION_MODELS: tuple[str, ...] = (
    "res.company",
    "res.partner",
    "product.product",
    "account.tax",
    "res.currency",
    "account.journal",
)
MODEL_FIELDS: dict[str, list[str]] = {
    "res.company": ["id", "name"],
    "res.partner": ["id", "name", "active", "company_id"],
    "product.product": ["id", "name", "default_code", "active", "company_id"],
    "account.tax": ["id", "name", "amount", "amount_type", "type_tax_use", "active", "company_id"],
    "res.currency": ["id", "name", "active"],
    "account.journal": ["id", "name", "code", "type", "company_id", "active"],
}
FORBIDDEN_ODOO_METHODS = ("create", "write", "unlink", "action_post")


@dataclass(frozen=True)
class OdooStagingValidationService:
    settings: Settings
    client: OdooJson2Client

    async def validate(self) -> dict[str, Any]:
        target_host = _host(self.settings.odoo_base_url)
        config_failures = _configuration_failures(self.settings, target_host)
        result: dict[str, Any] = {
            "environment": self.settings.app_env,
            "target_host": target_host,
            "authentication": {"status": "not_run" if config_failures else "pending"},
            "database_access": {"status": "not_run" if config_failures else "pending"},
            "company": {"status": "not_run" if config_failures else "pending"},
            "models": {
                model: {"status": "not_run" if config_failures else "pending"} for model in READ_ONLY_VALIDATION_MODELS
            },
            "purchase_journal": {"status": "not_run" if config_failures else "pending"},
            "permission_failures": [],
            "configuration_failures": config_failures,
            "blockers_for_resolution_validation": list(config_failures),
            "safety": {
                "staging_host_required": STAGING_ODOO_HOST,
                "no_write_operation_attempted": True,
                "forbidden_methods_not_attempted": list(FORBIDDEN_ODOO_METHODS),
            },
            "overall_status": "failed" if config_failures else "pending",
        }
        if config_failures:
            return result

        try:
            probe = await self.client.probe()
        except (ConnectorTimeoutError, ConnectorError) as exc:
            status = _failure_status(exc)
            safe_message = _safe_connector_message(exc)
            result["authentication"] = {"status": status, "message": safe_message}
            result["database_access"] = {"status": "not_run"}
            result["company"] = {"status": "not_run"}
            result["blockers_for_resolution_validation"].append("Odoo authentication or company access failed.")
            if status == "permission_failure":
                result["permission_failures"].append({"target": "authentication", "message": safe_message})
            result["overall_status"] = "failed"
            return result

        result["authentication"] = {"status": "ok"}
        result["database_access"] = {"status": "ok"}
        result["company"] = {"status": "ok", "company_id": probe.company_id}

        for model in READ_ONLY_VALIDATION_MODELS:
            result["models"][model] = await self._read_model(model)
            if result["models"][model]["status"] == "permission_failure":
                result["permission_failures"].append({"target": model, "message": result["models"][model]["message"]})

        result["purchase_journal"] = await self._validate_purchase_journal()
        if result["purchase_journal"]["status"] != "ok":
            result["blockers_for_resolution_validation"].append(result["purchase_journal"]["message"])
        if result["purchase_journal"]["status"] == "permission_failure":
            result["permission_failures"].append(
                {"target": "purchase_journal", "message": result["purchase_journal"]["message"]}
            )

        failed_models = [
            model for model, model_result in result["models"].items() if model_result["status"] not in {"ok", "empty"}
        ]
        if failed_models:
            result["blockers_for_resolution_validation"].append("One or more required Odoo models could not be read.")
        result["overall_status"] = (
            "ok"
            if not result["configuration_failures"]
            and not result["permission_failures"]
            and not result["blockers_for_resolution_validation"]
            else "failed"
        )
        return result

    async def _read_model(self, model: str) -> dict[str, Any]:
        try:
            records = await self.client.search_read(model=model, domain=[], fields=MODEL_FIELDS[model], limit=1)
        except (ConnectorTimeoutError, ConnectorError) as exc:
            return {"status": _failure_status(exc), "message": _safe_connector_message(exc)}
        return {"status": "ok", "records_sampled": len(records)}

    async def _validate_purchase_journal(self) -> dict[str, Any]:
        journal_id = self.settings.odoo_purchase_journal_id
        journal_code = _normalized_code(self.settings.odoo_purchase_journal_code)
        if journal_id is None and journal_code is None:
            return {
                "status": "not_configured",
                "message": "ODOO_PURCHASE_JOURNAL_ID or ODOO_PURCHASE_JOURNAL_CODE is required.",
            }

        if journal_id is not None:
            domain: list[Any] = [["id", "=", journal_id], ["type", "=", "purchase"]]
            match_key = "configured_id"
        else:
            domain = [["code", "=", journal_code], ["type", "=", "purchase"]]
            match_key = "configured_code"
        try:
            records = await self.client.search_read(
                model="account.journal",
                domain=domain,
                fields=["id", "name", "code", "type", "company_id"],
                limit=2,
            )
        except (ConnectorTimeoutError, ConnectorError) as exc:
            return {"status": _failure_status(exc), "message": _safe_connector_message(exc)}

        matches = [record for record in records if record.get("type") == "purchase"]
        if journal_id is not None:
            matches = [record for record in matches if record.get("id") == journal_id]
        if journal_code is not None:
            matches = [record for record in matches if _normalized_code(str(record.get("code", ""))) == journal_code]
        if len(matches) == 1:
            return {
                "status": "ok",
                "match_key": match_key,
                "journal_id": matches[0].get("id"),
            }
        if not matches:
            return {"status": "missing", "message": "Configured purchase journal was not found."}
        return {"status": "ambiguous", "message": "Configured purchase journal resolved to multiple records."}


def _configuration_failures(settings: Settings, target_host: str) -> list[str]:
    failures: list[str] = []
    if settings.app_env == "production":
        failures.append("APP_ENV=production is not allowed for staging validation.")
    if target_host != STAGING_ODOO_HOST:
        failures.append(f"ODOO_BASE_URL must point to {STAGING_ODOO_HOST}.")
    if not settings.odoo_database.strip():
        failures.append("ODOO_DATABASE must be configured.")
    if not settings.odoo_api_key.get_secret_value().strip():
        failures.append("ODOO_API_KEY must be configured.")
    return failures


def _host(value: object) -> str:
    return (urlparse(str(value)).hostname or "").lower()


def _normalized_code(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().upper()
    return normalized or None


def _failure_status(exc: Exception) -> str:
    message = _safe_connector_message(exc)
    if "HTTP 401" in message or "HTTP 403" in message:
        return "permission_failure"
    return "failed"


def _safe_connector_message(exc: Exception) -> str:
    if isinstance(exc, (ConnectorError, ConnectorTimeoutError)):
        return exc.safe_message
    return "Odoo validation failed."
