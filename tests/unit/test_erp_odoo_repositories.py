from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.connectors.exceptions import ConnectorError, ConnectorTimeoutError
from app.erp import Company, Currency, Partner, Product, StaticRepositoryProvider
from app.erp.exceptions import ErpReadonlyViolationError, ErpRepositoryError, ErpRepositoryTimeoutError
from app.erp.odoo.adapter import OdooReadOnlyAdapter
from app.erp.odoo.company_repository import OdooCompanyRepository
from app.erp.odoo.currency_repository import OdooCurrencyRepository
from app.erp.odoo.partner_repository import OdooPartnerRepository
from app.erp.odoo.product_repository import OdooProductRepository
from app.erp.odoo.provider import OdooRepositoryProvider
from app.erp.odoo.tax_repository import OdooTaxRepository
from app.tax_mapping.result import TaxType


class RecordingAdapter:
    def __init__(self, records: dict[str, list[dict[str, Any]]] | None = None, error: Exception | None = None) -> None:
        self.records = records or {}
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def search_read(
        self,
        *,
        model: str,
        domain: list[Any],
        fields: list[str],
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[dict[str, Any], ...]:
        self.calls.append(
            {
                "method": "search_read",
                "model": model,
                "domain": domain,
                "fields": fields,
                "limit": limit,
                "offset": offset,
            }
        )
        if self.error:
            raise self.error
        return tuple(self.records.get(model, []))

    def search_read_all(
        self,
        *,
        model: str,
        domain: list[Any],
        fields: list[str],
        page_size: int | None = None,
        max_records: int | None = None,
    ) -> tuple[dict[str, Any], ...]:
        self.calls.append(
            {
                "method": "search_read_all",
                "model": model,
                "domain": domain,
                "fields": fields,
                "page_size": page_size,
                "max_records": max_records,
            }
        )
        if self.error:
            raise self.error
        records = tuple(self.records.get(model, []))
        if max_records is None:
            return records
        return records[:max_records]


class FlakySearchReadClient:
    def __init__(self, *, failures: int = 0, timeout: bool = False) -> None:
        self.failures = failures
        self.timeout = timeout
        self.calls: list[dict[str, Any]] = []

    async def search_read(
        self,
        *,
        model: str,
        domain: list[Any],
        fields: list[str],
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        self.calls.append({"model": model, "domain": domain, "fields": fields, "limit": limit, "offset": offset})
        if len(self.calls) <= self.failures:
            if self.timeout:
                raise ConnectorTimeoutError("Odoo request timed out.")
            raise ConnectorError("Odoo returned HTTP 503.")
        return [{"id": offset + 1, "name": f"Record {offset + 1}"}]


class PagingClient:
    async def search_read(
        self,
        *,
        model: str,
        domain: list[Any],
        fields: list[str],
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        del model, domain, fields
        records = [
            {"id": 1, "name": "A"},
            {"id": 2, "name": "B"},
            {"id": 3, "name": "C"},
        ]
        return records[offset : offset + limit]


def test_partner_lookup_returns_immutable_dtos() -> None:
    adapter = RecordingAdapter(
        {"res.partner": [{"id": 10, "name": "Supplier", "vat": "123", "active": True, "company_id": [7, "Main"]}]}
    )

    partners = OdooPartnerRepository(adapter=adapter).find_by_tax_number("123", company_id=7)

    assert partners == (Partner(id=10, name="Supplier", tax_number="123", active=True, company_id=7),)
    assert adapter.calls[0]["model"] == "res.partner"
    assert adapter.calls[0]["domain"] == [["vat", "=", "123"], ["company_id", "in", [7, False]]]
    with pytest.raises(FrozenInstanceError):
        partners[0].name = "Changed"  # type: ignore[misc]


def test_product_lookup_by_code_barcode_and_ids() -> None:
    adapter = RecordingAdapter(
        {
            "product.product": [
                {
                    "id": 20,
                    "name": "Product",
                    "default_code": "SKU-1",
                    "barcode": "869",
                    "active": True,
                    "company_id": False,
                }
            ]
        }
    )
    repository = OdooProductRepository(adapter=adapter)

    by_code = repository.find_by_default_code("SKU-1")
    by_barcode = repository.find_by_barcode("869")
    by_ids = repository.find_by_ids([20])

    assert by_code == (Product(id=20, name="Product", default_code="SKU-1", barcode="869", active=True),)
    assert by_barcode == by_code
    assert by_ids == by_code
    assert [call["domain"][0] for call in adapter.calls] == [
        ["default_code", "=", "SKU-1"],
        ["barcode", "=", "869"],
        ["id", "in", [20]],
    ]


def test_tax_lookup_implements_tax_mapping_repository_protocol() -> None:
    adapter = RecordingAdapter(
        {
            "account.tax": [
                {"id": 30, "amount": "20.0", "active": True, "type_tax_use": "purchase", "company_id": [7, "Main"]}
            ]
        }
    )

    candidates = OdooTaxRepository(adapter=adapter).find_candidates(
        company_id=7,
        rate=Decimal("20"),
        tax_type=TaxType.VAT,
    )

    assert candidates[0].tax_id == 30
    assert candidates[0].company_id == 7
    assert candidates[0].tax_type is TaxType.VAT
    assert candidates[0].rate == Decimal("20.0")
    assert adapter.calls[0]["domain"] == [
        ["amount", "=", 20.0],
        ["type_tax_use", "=", "purchase"],
        ["company_id", "in", [7, False]],
    ]


def test_currency_and_company_lookup() -> None:
    adapter = RecordingAdapter(
        {
            "res.currency": [{"id": 4, "name": "TRY", "active": True}],
            "res.company": [{"id": 7, "name": "Main Company"}],
        }
    )

    currency = OdooCurrencyRepository(adapter=adapter).find_by_code("try")
    company = OdooCompanyRepository(adapter=adapter).find_by_id(7)
    default_company = OdooCompanyRepository(adapter=adapter).find_default()

    assert currency == Currency(id=4, code="TRY", active=True)
    assert company == Company(id=7, name="Main Company")
    assert default_company == company
    assert adapter.calls[0]["domain"] == [["name", "=", "TRY"]]
    assert adapter.calls[1]["domain"] == [["id", "=", 7]]
    assert adapter.calls[2]["domain"] == []


def test_provider_exposes_repositories() -> None:
    adapter = RecordingAdapter()
    provider = OdooRepositoryProvider.from_adapter(adapter)
    static_provider = StaticRepositoryProvider(
        partner_repository=provider.partner_repository,
        product_repository=provider.product_repository,
        tax_repository=provider.tax_repository,
        currency_repository=provider.currency_repository,
        company_repository=provider.company_repository,
    )

    assert isinstance(provider.partner_repository, OdooPartnerRepository)
    assert static_provider.company_repository is provider.company_repository


def test_adapter_paginates_search_read() -> None:
    adapter = OdooReadOnlyAdapter(client=PagingClient(), page_size=2, retry_backoff_seconds=0)

    records = adapter.search_read_all(model="res.company", domain=[], fields=["id", "name"])

    assert records == (
        {"id": 1, "name": "A"},
        {"id": 2, "name": "B"},
        {"id": 3, "name": "C"},
    )


def test_adapter_retries_connector_errors() -> None:
    client = FlakySearchReadClient(failures=1)
    adapter = OdooReadOnlyAdapter(client=client, retry_attempts=2, retry_backoff_seconds=0)

    assert adapter.search_read(model="res.company", domain=[], fields=["id"], limit=1) == (
        {"id": 1, "name": "Record 1"},
    )
    assert len(client.calls) == 2


def test_adapter_translates_timeout_after_retries() -> None:
    client = FlakySearchReadClient(failures=2, timeout=True)
    adapter = OdooReadOnlyAdapter(client=client, retry_attempts=2, retry_backoff_seconds=0)

    with pytest.raises(ErpRepositoryTimeoutError) as exc_info:
        adapter.search_read(model="res.company", domain=[], fields=["id"], limit=1)

    assert exc_info.value.safe_message == "Odoo request timed out."


def test_adapter_rejects_non_readonly_method_guard() -> None:
    with pytest.raises(ErpReadonlyViolationError):
        OdooReadOnlyAdapter._ensure_readonly(method="create")


def test_repository_errors_are_sanitized_and_propagated() -> None:
    repository = OdooPartnerRepository(adapter=RecordingAdapter(error=ErpRepositoryError("Odoo returned HTTP 403.")))

    with pytest.raises(ErpRepositoryError) as exc_info:
        repository.find_by_ids([1])

    assert exc_info.value.safe_message == "Odoo returned HTTP 403."


def test_erp_package_does_not_import_provider_or_persistence_layers() -> None:
    package_root = Path(__file__).resolve().parents[2] / "app" / "erp"
    combined_source = "\n".join(path.read_text() for path in package_root.rglob("*.py"))
    repository_source = "\n".join(path.read_text() for path in (package_root / "odoo").glob("*_repository.py"))

    assert "sqlalchemy" not in combined_source.lower()
    assert "app.models" not in combined_source
    assert "app.db" not in combined_source
    assert "parse_ubl_invoice" not in combined_source
    assert "create_account_move" not in combined_source
    assert ".create(" not in repository_source
    assert ".write(" not in repository_source
    assert "unlink" not in repository_source
    assert "action_post" not in repository_source
    assert "button_validate" not in repository_source
