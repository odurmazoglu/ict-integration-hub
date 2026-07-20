from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

UyumsoftEnvironment = Literal["test", "production"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    database_url: str = "postgresql+psycopg://ict:ict@localhost:5432/ict_integration_hub"
    document_storage_root: Path = Path("var/document_storage")

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
