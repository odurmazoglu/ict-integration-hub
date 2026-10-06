import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AnyHttpUrl, Field, SecretStr, StrictInt
from pydantic_settings import BaseSettings, SettingsConfigDict

UyumsoftEnvironment = Literal["test", "production"]
AuthenticationMode = Literal["disabled", "development_headers", "oidc_jwt"]
#: A positive Odoo record id; strict so booleans, floats and numeric strings are refused.
OdooRecordId = Annotated[StrictInt, Field(gt=0)]


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
    customer_invoice_execute_enabled: bool = False
    customer_quotation_execute_enabled: bool = False
    staging_vendor_bill_execute_enabled: bool = False
    supplier_remediation_write_enabled: bool = False
    product_remediation_write_enabled: bool = False
    uyumsoft_sync_execute_enabled: bool = False
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
    odoo_workbench_projection_publish_enabled: bool = False
    #: Pre-existing Odoo Studio selection field on ``res.partner`` holding ICT's business
    #: relationship classification (production: ``x_studio_musteri_tipi``). Unset means
    #: every Hub supplier-partner create fails closed -- the Hub never relies on an Odoo
    #: ``ir.default`` and never creates Studio schema.
    odoo_partner_classification_field: str | None = None
    #: Exact Odoo ``product.category`` ids approved for RESALE (P0-PROD-18E-1A). Empty means
    #: no category is approved. Never hierarchical: approving a parent approves no child.
    #: Holds category ids only -- the purchase account stays Odoo's category configuration.
    resale_product_category_ids: frozenset[OdooRecordId] = frozenset()
    #: Exact Odoo ``account.account`` ids an operator may select as the fixed-asset account of
    #: a CAPITALIZE_FIXED_ASSET accounting resolution. Empty means fixed-asset accounting is
    #: unavailable (fails closed). Keeps e.g. accumulated-depreciation accounts unselectable.
    odoo_fixed_asset_account_ids: frozenset[OdooRecordId] = frozenset()

    uyumsoft_environment: UyumsoftEnvironment = "test"
    uyumsoft_test_wsdl_url: AnyHttpUrl = Field(default="https://efatura-test.uyumsoft.com.tr/Services/Integration?wsdl")
    uyumsoft_prod_wsdl_url: AnyHttpUrl = Field(default="https://efatura.uyumsoft.com.tr/Services/Integration?wsdl")
    uyumsoft_username: str = "change-me"
    uyumsoft_password: SecretStr = SecretStr("change-me")
    uyumsoft_timeout_seconds: float = 20
    uyumsoft_retry_attempts: int = Field(default=3, ge=1, le=5)
    uyumsoft_retry_backoff_seconds: float = Field(default=0.2, ge=0, le=5)
    #: Hub-owned inbound poller (separate worker process). Off by default; enabling it
    #: in production is a separate operator step.
    uyumsoft_inbound_poll_enabled: bool = False
    uyumsoft_inbound_poll_interval_seconds: int = Field(default=180, ge=60, le=3600)
    #: Bounded lookback on the Uyumsoft execution (invoice) date. Covers late-arriving
    #: invoices without a mutable watermark; known invoices are skipped by identity.
    uyumsoft_inbound_poll_lookback_days: int = Field(default=10, ge=1, le=30)
    uyumsoft_inbound_poll_page_size: int = Field(default=100, ge=1, le=100)
    uyumsoft_inbound_poll_max_pages: int = Field(default=10, ge=1, le=10)
    #: ADR-0013 Odoo Workbench operator requests: a second, independent tick in the same
    #: poller process. Off by default; enabling it in production is a separate step.
    odoo_workbench_operator_requests_enabled: bool = False
    odoo_workbench_operator_requests_interval_seconds: int = Field(default=60, ge=30, le=3600)
    #: Hub company whose Workbench requests the tick consumes. Required when enabled --
    #: the company scope is Hub configuration, never taken from the Odoo row.
    odoo_workbench_operator_requests_company_id: int | None = Field(default=None, ge=1)
    #: JSON ``{"<odoo_user_id>": {"actor": "<name>", "permissions": [...]}}`` mapping Odoo
    #: users to Hub actors with existing permission names. Empty: every request is refused.
    odoo_operator_request_actors: str | None = None

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
