import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

UyumsoftEnvironment = Literal["test", "production"]
AuthenticationMode = Literal["disabled", "development_headers", "oidc_jwt"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file_encoding="utf-8", extra="ignore")

    def __init__(self, **values: object) -> None:
        super().__init__(_env_file=_selected_env_file(), **values)

    app_env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    database_url: str = "postgresql+psycopg://ict:ict@localhost:5432/ict_integration_hub"
    document_storage_root: Path = Path("var/document_storage")
    live_connector_readonly: bool = False
    execution_execute_enabled: bool = False
    production_operations_enabled: bool = False
    production_approval_ack: str = ""
    ipp_auth_mode: AuthenticationMode = "disabled"
    ipp_enable_development_header_auth: bool = False
    ipp_oidc_issuer: str = ""
    ipp_oidc_audience: str = ""
    ipp_oidc_jwks_url: str = ""
    ipp_oidc_discovery_url: str = ""
    ipp_oidc_clock_skew_seconds: int = Field(default=60, ge=0, le=300)
    ipp_oidc_jwks_cache_seconds: int = Field(default=300, ge=1, le=86400)
    ipp_oidc_company_id_claim: str = "ipp_company_id"
    ipp_oidc_permissions_claim: str = "ipp_permissions"
    ipp_oidc_username_claim: str = "preferred_username"
    ipp_oidc_allowed_algorithms: tuple[str, ...] = ("RS256",)

    odoo_base_url: AnyHttpUrl = Field(default="https://example.odoo.com")
    odoo_database: str = "example"
    odoo_api_key: SecretStr = SecretStr("change-me")
    odoo_timeout_seconds: float = 10
    odoo_purchase_journal_id: int | None = None
    odoo_purchase_journal_code: str | None = None

    uyumsoft_environment: UyumsoftEnvironment = "test"
    uyumsoft_test_wsdl_url: AnyHttpUrl = Field(default="https://efatura-test.uyumsoft.com.tr/Services/Integration?wsdl")
    uyumsoft_prod_wsdl_url: AnyHttpUrl = Field(default="https://efatura.uyumsoft.com.tr/Services/Integration?wsdl")
    uyumsoft_username: str = "change-me"
    uyumsoft_password: SecretStr = SecretStr("change-me")
    uyumsoft_timeout_seconds: float = 20
    uyumsoft_retry_attempts: int = Field(default=3, ge=1, le=5)
    uyumsoft_retry_backoff_seconds: float = Field(default=0.2, ge=0, le=5)

    @property
    def uyumsoft_wsdl_url(self) -> str:
        if self.uyumsoft_environment == "production":
            return str(self.uyumsoft_prod_wsdl_url)
        return str(self.uyumsoft_test_wsdl_url)

    @property
    def is_development(self) -> bool:
        return self.app_env == "development"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def _selected_env_file() -> str:
    return os.getenv("APP_ENV_FILE", ".env.local")
