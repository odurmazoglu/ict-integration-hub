from pathlib import Path

from pydantic import SecretStr

from app.core.config import Settings
from app.core.logging import RedactionFilter
from app.core.runtime_checks import (
    PRODUCTION_APPROVAL_ACK,
    RuntimeConfigurationError,
    runtime_configuration_errors,
    validate_runtime_configuration,
)


def test_production_configuration_requires_multiple_explicit_gates() -> None:
    settings = Settings(
        app_env="production",
        database_url="postgresql+psycopg://user:super-secret@localhost:5432/prod",
        odoo_base_url="https://example.odoo.com",
        odoo_database="example",
        odoo_api_key=SecretStr("super-secret-api-key"),
        uyumsoft_environment="test",
        uyumsoft_username="super-secret-user",
        uyumsoft_password=SecretStr("super-secret-password"),
    )

    errors = runtime_configuration_errors(settings)

    assert "PRODUCTION_OPERATIONS_ENABLED must be true in production." in errors
    assert "PRODUCTION_APPROVAL_ACK must confirm manual production approval." in errors
    assert "UYUMSOFT_ENVIRONMENT must be production when APP_ENV=production." in errors
    assert "ODOO_BASE_URL must be a real approved production host." in errors
    assert "DATABASE_URL must not point to localhost in production." in errors
    assert "super-secret" not in " ".join(errors)


def test_production_configuration_accepts_approved_separated_runtime(tmp_path: Path) -> None:
    settings = Settings(
        app_env="production",
        database_url="postgresql+psycopg://ict:<password>@db.internal:5432/ict",
        document_storage_root=tmp_path / "documents",
        production_operations_enabled=True,
        production_approval_ack=PRODUCTION_APPROVAL_ACK,
        odoo_base_url="https://odoo.example-tenant.com",
        odoo_database="ict-prod",
        odoo_api_key=SecretStr("replace-with-real-secret"),
        odoo_purchase_journal_id=10,
        uyumsoft_environment="production",
        uyumsoft_username="uyumsoft-prod-user",
        uyumsoft_password=SecretStr("replace-with-real-secret"),
    )

    validate_runtime_configuration(settings)


def test_non_production_rejects_production_flags_and_provider_environment() -> None:
    settings = Settings(
        app_env="development",
        production_operations_enabled=True,
        production_approval_ack=PRODUCTION_APPROVAL_ACK,
        uyumsoft_environment="production",
    )

    errors = runtime_configuration_errors(settings)

    assert "PRODUCTION_OPERATIONS_ENABLED must be false outside production." in errors
    assert "PRODUCTION_APPROVAL_ACK must be empty outside production." in errors
    assert "UYUMSOFT_ENVIRONMENT=production is only allowed when APP_ENV=production." in errors


def test_runtime_configuration_error_exposes_only_safe_messages() -> None:
    settings = Settings(
        app_env="production",
        database_url="postgresql+psycopg://user:secret@localhost:5432/prod",
        odoo_api_key=SecretStr("secret-api-key"),
        uyumsoft_password=SecretStr("secret-password"),
    )

    try:
        validate_runtime_configuration(settings)
    except RuntimeConfigurationError as exc:
        joined = " ".join(exc.messages)
    else:
        raise AssertionError("Expected unsafe production settings to fail.")

    assert "secret-api-key" not in joined
    assert "secret-password" not in joined
    assert "Runtime configuration is unsafe." not in joined


def test_logging_redaction_filter_removes_secret_values_and_xml_payloads() -> None:
    import logging

    filter_ = RedactionFilter()
    secret_record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="api_key=super-secret-token",
        args=(),
        exc_info=None,
    )
    secret_record.client_secret = "do-not-log"
    xml_record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="<Invoice><ID>INV-1</ID></Invoice>",
        args=(),
        exc_info=None,
    )

    assert filter_.filter(secret_record)
    assert filter_.filter(xml_record)

    assert "super-secret-token" not in secret_record.msg
    assert "<redacted>" in secret_record.msg
    assert secret_record.client_secret == "<redacted>"
    assert xml_record.msg == "<redacted-payload>"


def test_example_environment_files_contain_placeholders_only() -> None:
    for path in [Path(".env.example"), Path(".env.production.example")]:
        content = path.read_text(encoding="utf-8")

        assert "super-secret" not in content
        assert "real-" not in content
        assert "ODOO_API_KEY=change-me" in content or "ODOO_API_KEY=<odoo-api-key>" in content
        assert "UYUMSOFT_PASSWORD=change-me" in content or "UYUMSOFT_PASSWORD=<uyumsoft-password>" in content
