from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import Settings

PRODUCTION_APPROVAL_ACK = "APPROVED_FOR_PRODUCTION"
APPROVED_UYUMSOFT_PRODUCTION_HOSTS = frozenset({"efatura.uyumsoft.com.tr"})
PLACEHOLDER_VALUES = frozenset({"", "change-me", "example", "placeholder", "todo"})
RuntimeCheckStatus = Literal["ok", "error"]


class RuntimeConfigurationError(RuntimeError):
    def __init__(self, messages: list[str]) -> None:
        super().__init__("Runtime configuration is unsafe.")
        self.messages = messages


@dataclass(frozen=True)
class RuntimeCheck:
    name: str
    status: RuntimeCheckStatus
    message: str


def validate_runtime_configuration(settings: Settings) -> None:
    messages = runtime_configuration_errors(settings)
    if messages:
        raise RuntimeConfigurationError(messages)


def runtime_configuration_errors(settings: Settings) -> list[str]:
    errors: list[str] = []
    _validate_common_settings(settings, errors)
    if settings.app_env == "production":
        _validate_production_settings(settings, errors)
    else:
        _validate_non_production_settings(settings, errors)
    return errors


def configuration_check(settings: Settings) -> RuntimeCheck:
    errors = runtime_configuration_errors(settings)
    if errors:
        return RuntimeCheck(name="configuration", status="error", message="; ".join(errors))
    return RuntimeCheck(name="configuration", status="ok", message="Runtime configuration is valid.")


def database_check(database_url: str) -> RuntimeCheck:
    try:
        engine = create_engine(database_url, pool_pre_ping=True)
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        finally:
            engine.dispose()
    except SQLAlchemyError:
        return RuntimeCheck(name="database", status="error", message="Database connectivity check failed.")
    return RuntimeCheck(name="database", status="ok", message="Database connectivity check passed.")


def document_storage_check(root: Path) -> RuntimeCheck:
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".readiness"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError:
        return RuntimeCheck(name="document_storage", status="error", message="Document storage is not writable.")
    return RuntimeCheck(name="document_storage", status="ok", message="Document storage is writable.")


def _validate_common_settings(settings: Settings, errors: list[str]) -> None:
    if not settings.database_url.strip():
        errors.append("DATABASE_URL is required.")
    if not settings.document_storage_root:
        errors.append("DOCUMENT_STORAGE_ROOT is required.")
    if settings.odoo_timeout_seconds <= 0:
        errors.append("ODOO_TIMEOUT_SECONDS must be greater than zero.")
    if settings.uyumsoft_timeout_seconds <= 0:
        errors.append("UYUMSOFT_TIMEOUT_SECONDS must be greater than zero.")
    if settings.uyumsoft_retry_attempts < 1 or settings.uyumsoft_retry_attempts > 5:
        errors.append("UYUMSOFT_RETRY_ATTEMPTS must be between 1 and 5.")
    if settings.uyumsoft_retry_backoff_seconds < 0 or settings.uyumsoft_retry_backoff_seconds > 5:
        errors.append("UYUMSOFT_RETRY_BACKOFF_SECONDS must be between 0 and 5.")
    if not settings.odoo_database.strip():
        errors.append("ODOO_DATABASE must be configured.")
    if not settings.odoo_api_key.get_secret_value().strip():
        errors.append("ODOO_API_KEY must be configured.")
    if not settings.uyumsoft_username.strip():
        errors.append("UYUMSOFT_USERNAME must be configured.")
    if not settings.uyumsoft_password.get_secret_value().strip():
        errors.append("UYUMSOFT_PASSWORD must be configured.")


def _validate_production_settings(settings: Settings, errors: list[str]) -> None:
    if not settings.production_operations_enabled:
        errors.append("PRODUCTION_OPERATIONS_ENABLED must be true in production.")
    if settings.production_approval_ack != PRODUCTION_APPROVAL_ACK:
        errors.append("PRODUCTION_APPROVAL_ACK must confirm manual production approval.")
    if settings.uyumsoft_environment != "production":
        errors.append("UYUMSOFT_ENVIRONMENT must be production when APP_ENV=production.")
    if _host(settings.uyumsoft_prod_wsdl_url) not in APPROVED_UYUMSOFT_PRODUCTION_HOSTS:
        errors.append("UYUMSOFT_PROD_WSDL_URL host is not approved for production.")
    if _host(settings.uyumsoft_test_wsdl_url) in APPROVED_UYUMSOFT_PRODUCTION_HOSTS:
        errors.append("UYUMSOFT_TEST_WSDL_URL must not point to the production host.")
    if _is_example_host(_host(settings.odoo_base_url)):
        errors.append("ODOO_BASE_URL must be a real approved production host.")
    if _is_placeholder(settings.odoo_database):
        errors.append("ODOO_DATABASE must not be a placeholder in production.")
    if _is_placeholder(settings.odoo_api_key.get_secret_value()):
        errors.append("ODOO_API_KEY must not be a placeholder in production.")
    if _is_placeholder(settings.uyumsoft_username):
        errors.append("UYUMSOFT_USERNAME must not be a placeholder in production.")
    if _is_placeholder(settings.uyumsoft_password.get_secret_value()):
        errors.append("UYUMSOFT_PASSWORD must not be a placeholder in production.")
    if _is_localhost_database(settings.database_url):
        errors.append("DATABASE_URL must not point to localhost in production.")


def _validate_non_production_settings(settings: Settings, errors: list[str]) -> None:
    if settings.production_operations_enabled:
        errors.append("PRODUCTION_OPERATIONS_ENABLED must be false outside production.")
    if settings.production_approval_ack:
        errors.append("PRODUCTION_APPROVAL_ACK must be empty outside production.")
    if settings.uyumsoft_environment == "production":
        if not settings.live_connector_readonly:
            errors.append("UYUMSOFT_ENVIRONMENT=production outside production requires LIVE_CONNECTOR_READONLY=true.")
        _validate_live_readonly_connector_settings(settings, errors)


def _validate_live_readonly_connector_settings(settings: Settings, errors: list[str]) -> None:
    if _host(settings.uyumsoft_prod_wsdl_url) not in APPROVED_UYUMSOFT_PRODUCTION_HOSTS:
        errors.append("UYUMSOFT_PROD_WSDL_URL host is not approved for production.")
    if _is_placeholder(settings.uyumsoft_username):
        errors.append("UYUMSOFT_USERNAME must not be a placeholder for live connector readonly mode.")
    if _is_placeholder(settings.uyumsoft_password.get_secret_value()):
        errors.append("UYUMSOFT_PASSWORD must not be a placeholder for live connector readonly mode.")


def _host(value: object) -> str:
    parsed = urlparse(str(value))
    return (parsed.hostname or "").lower()


def _is_placeholder(value: str) -> bool:
    return value.strip().lower() in PLACEHOLDER_VALUES


def _is_example_host(host: str) -> bool:
    return host in {"", "example.com", "example.odoo.com"} or host.endswith(".example.com")


def _is_localhost_database(database_url: str) -> bool:
    parsed = urlparse(database_url)
    return (parsed.hostname or "").lower() in {"localhost", "127.0.0.1", "::1"}
