from pathlib import Path

from pydantic import SecretStr

from app.core.config import Settings
from app.core.runtime_checks import (
    PRODUCTION_APPROVAL_ACK,
    runtime_configuration_errors,
    validate_runtime_configuration,
)

ENV_KEYS = (
    "APP_ENV",
    "APP_ENV_FILE",
    "LIVE_CONNECTOR_READONLY",
    "PRODUCTION_OPERATIONS_ENABLED",
    "PRODUCTION_APPROVAL_ACK",
    "ODOO_BASE_URL",
    "ODOO_DATABASE",
    "ODOO_API_KEY",
    "UYUMSOFT_ENVIRONMENT",
    "UYUMSOFT_USERNAME",
    "UYUMSOFT_PASSWORD",
)


def test_default_profile_resolves_to_env_local(monkeypatch, tmp_path: Path) -> None:
    _clear_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env.local").write_text(
        "\n".join(
            [
                "APP_ENV=test",
                "ODOO_DATABASE=local-profile",
                "UYUMSOFT_USERNAME=local-user",
                "UYUMSOFT_PASSWORD=local-password",
            ]
        ),
        encoding="utf-8",
    )

    settings = Settings()

    assert settings.app_env == "test"
    assert settings.odoo_database == "local-profile"
    assert settings.uyumsoft_username == "local-user"


def test_app_env_file_selects_custom_profile(monkeypatch, tmp_path: Path) -> None:
    _clear_env(monkeypatch)
    profile = tmp_path / "custom.env"
    profile.write_text(
        "\n".join(
            [
                "APP_ENV=development",
                "ODOO_DATABASE=custom-profile",
                "UYUMSOFT_USERNAME=custom-user",
                "UYUMSOFT_PASSWORD=custom-password",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("APP_ENV_FILE", str(profile))

    settings = Settings()

    assert settings.app_env == "development"
    assert settings.odoo_database == "custom-profile"
    assert settings.uyumsoft_username == "custom-user"


def test_process_environment_overrides_selected_file(monkeypatch, tmp_path: Path) -> None:
    _clear_env(monkeypatch)
    profile = tmp_path / "selected.env"
    profile.write_text("ODOO_DATABASE=file-value\nUYUMSOFT_USERNAME=file-user\n", encoding="utf-8")
    monkeypatch.setenv("APP_ENV_FILE", str(profile))
    monkeypatch.setenv("ODOO_DATABASE", "process-value")

    settings = Settings()

    assert settings.odoo_database == "process-value"
    assert settings.uyumsoft_username == "file-user"


def test_only_selected_env_profile_is_loaded(monkeypatch, tmp_path: Path) -> None:
    _clear_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env.local").write_text("ODOO_DATABASE=local-profile\n", encoding="utf-8")
    selected = tmp_path / ".env.selected"
    selected.write_text("APP_ENV=test\n", encoding="utf-8")
    monkeypatch.setenv("APP_ENV_FILE", str(selected))

    settings = Settings()

    assert settings.app_env == "test"
    assert settings.odoo_database == "example"


def test_dotenv_is_not_implicitly_preferred(monkeypatch, tmp_path: Path) -> None:
    _clear_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("APP_ENV=production\nODOO_DATABASE=dotenv-profile\n", encoding="utf-8")
    (tmp_path / ".env.local").write_text("APP_ENV=test\nODOO_DATABASE=local-profile\n", encoding="utf-8")

    settings = Settings()

    assert settings.app_env == "test"
    assert settings.odoo_database == "local-profile"


def test_development_uyumsoft_production_live_readonly_profile_is_valid() -> None:
    settings = Settings(
        app_env="development",
        live_connector_readonly=True,
        production_operations_enabled=False,
        production_approval_ack="",
        uyumsoft_environment="production",
        uyumsoft_username="live-user",
        uyumsoft_password=SecretStr("live-password"),
        odoo_base_url="https://test-ictteknoloji.odoo.com",
        odoo_database="staging-db",
        odoo_api_key=SecretStr("staging-key"),
    )

    validate_runtime_configuration(settings)


def test_strict_production_runtime_validation_remains_unchanged() -> None:
    settings = Settings(
        app_env="production",
        production_operations_enabled=False,
        production_approval_ack="",
        uyumsoft_environment="production",
        uyumsoft_username="live-user",
        uyumsoft_password=SecretStr("live-password"),
    )

    errors = runtime_configuration_errors(settings)

    assert "PRODUCTION_OPERATIONS_ENABLED must be true in production." in errors
    assert "PRODUCTION_APPROVAL_ACK must confirm manual production approval." in errors


def test_live_readonly_profile_rejects_production_write_enablement() -> None:
    settings = Settings(
        app_env="development",
        live_connector_readonly=True,
        production_operations_enabled=True,
        production_approval_ack=PRODUCTION_APPROVAL_ACK,
        uyumsoft_environment="production",
        uyumsoft_username="live-user",
        uyumsoft_password=SecretStr("live-password"),
        odoo_base_url="https://test-ictteknoloji.odoo.com",
    )

    errors = runtime_configuration_errors(settings)

    assert "PRODUCTION_OPERATIONS_ENABLED must be false outside production." in errors
    assert "PRODUCTION_APPROVAL_ACK must be empty outside production." in errors


def test_live_readonly_profile_accepts_odoo_staging_url() -> None:
    settings = Settings(
        app_env="development",
        live_connector_readonly=True,
        production_operations_enabled=False,
        production_approval_ack="",
        uyumsoft_environment="production",
        uyumsoft_username="live-user",
        uyumsoft_password=SecretStr("live-password"),
        odoo_base_url="https://test-ictteknoloji.odoo.com",
    )

    errors = runtime_configuration_errors(settings)

    assert errors == []


def _clear_env(monkeypatch) -> None:
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
