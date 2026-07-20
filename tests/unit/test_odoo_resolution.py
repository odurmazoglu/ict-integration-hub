from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient

from app.api.dependencies import get_odoo_client, get_settings
from app.connectors.exceptions import ConnectorError, ConnectorTimeoutError
from app.core.config import Settings
from app.main import app
from app.schemas.odoo_resolution import OdooResolutionRequest
from app.services import odoo_resolution
from app.services.document_parser import UblInvoiceParser
from app.services.odoo_mapping_preview import OdooMappingPreviewService
from app.services.odoo_resolution import (
    OdooResolutionConnectorError,
    OdooResolutionService,
    OdooResolutionTimeoutError,
    OdooUnsupportedTaxStructureError,
)

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "ubl"


class FakeReadOnlyOdooClient:
    def __init__(self, records: dict[str, list[dict[str, Any]]] | None = None, error: Exception | None = None) -> None:
        self.records = records or _resolved_records()
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def search_read(
        self,
        *,
        model: str,
        domain: list[Any],
        fields: list[str],
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        self.calls.append({"model": model, "domain": domain, "fields": fields, "limit": limit})
        if self.error is not None:
            raise self.error
        return self.records.get(model, [])


async def test_partner_exact_vat_match_and_normalized_vat_behavior() -> None:
    preview = _preview()
    partner = preview.invoice.partner.model_copy(update={"tax_id": "111 111-1111"}) if preview.invoice.partner else None
    result = await _resolve(
        preview.model_copy(update={"invoice": preview.invoice.model_copy(update={"partner": partner})})
    )

    assert result.partner.status == "resolved"
    assert result.partner.odoo_id == 101
    assert result.partner.match_method == "vat"
    assert result.reviewed_preview.invoice.partner.odoo_id == 101


async def test_partner_exact_name_fallback() -> None:
    preview = _preview()
    partner = preview.invoice.partner.model_copy(update={"tax_id": None}) if preview.invoice.partner else None
    client = FakeReadOnlyOdooClient(
        records={
            **_resolved_records(),
            "res.partner": [{"id": 102, "name": "Synthetic Supplier Ltd", "active": True}],
        }
    )

    result = await OdooResolutionService(client=client).resolve(
        _request(preview.model_copy(update={"invoice": preview.invoice.model_copy(update={"partner": partner})}))
    )

    assert result.partner.status == "resolved"
    assert result.partner.odoo_id == 102
    assert result.partner.match_method == "name"


async def test_partner_no_match_and_ambiguous_matches() -> None:
    no_match = await _resolve(records={**_resolved_records(), "res.partner": []})
    ambiguous = await _resolve(
        records={
            **_resolved_records(),
            "res.partner": [
                {"id": 101, "vat": "1111111111", "name": "A", "active": True},
                {"id": 102, "vat": "1111111111", "name": "B", "active": True},
            ],
        }
    )

    assert no_match.partner.status == "unresolved"
    assert no_match.missing_matches[0].entity_type == "partner"
    assert ambiguous.partner.status == "ambiguous"
    assert ambiguous.partner.odoo_id is None
    assert ambiguous.ambiguous_matches[0].entity_type == "partner"


async def test_partner_ambiguous_name_fallback() -> None:
    preview = _preview()
    partner = preview.invoice.partner.model_copy(update={"tax_id": None}) if preview.invoice.partner else None
    result = await _resolve(
        preview=preview.model_copy(update={"invoice": preview.invoice.model_copy(update={"partner": partner})}),
        records={
            **_resolved_records(),
            "res.partner": [
                {"id": 101, "name": "Synthetic Supplier Ltd", "active": True},
                {"id": 102, "name": "Synthetic Supplier Ltd", "active": True},
            ],
        },
    )

    assert result.partner.status == "ambiguous"


async def test_product_exact_default_code_match_name_fallback_and_variant() -> None:
    preview = _preview_with_product_code("CONSULT-001")
    result = await _resolve(preview=preview)
    fallback = await _resolve(
        records={
            **_resolved_records(),
            "product.product": [{"id": 302, "name": "Consulting", "active": True}],
        }
    )

    assert result.lines[0].product.status == "resolved"
    assert result.lines[0].product.match_method == "default_code"
    assert result.lines[0].product.odoo_id == 301
    assert fallback.lines[0].product.status == "resolved"
    assert fallback.lines[0].product.match_method == "name"
    assert fallback.lines[0].product.odoo_id == 302


async def test_product_optional_productless_line_no_match_and_ambiguous() -> None:
    preview = _preview()
    line = preview.lines[0].model_copy(update={"product": None})
    productless = await _resolve(preview=preview.model_copy(update={"lines": [line]}), allow_productless_lines=True)
    no_match = await _resolve(records={**_resolved_records(), "product.product": []})
    ambiguous = await _resolve(
        records={
            **_resolved_records(),
            "product.product": [
                {"id": 301, "name": "Consulting", "active": True},
                {"id": 302, "name": "Consulting", "active": True},
            ],
        }
    )

    assert productless.lines[0].product.status == "not_required"
    assert no_match.lines[0].product.status == "unresolved"
    assert ambiguous.lines[0].product.status == "ambiguous"


async def test_tax_exact_purchase_company_rate_and_price_include_match() -> None:
    preview = _preview()
    line = preview.lines[0]
    taxes = [line.taxes[0].model_copy(update={"price_include": False})]
    preview = preview.model_copy(update={"lines": [line.model_copy(update={"taxes": taxes})]})

    result = await _resolve(preview=preview, company_id=7)

    assert result.lines[0].taxes[0].status == "resolved"
    assert result.lines[0].taxes[0].odoo_id == 401
    assert result.lines[0].taxes[0].match_method == "purchase_percent"


async def test_tax_no_match_ambiguous_and_unsupported_structure() -> None:
    no_match = await _resolve(records={**_resolved_records(), "account.tax": []})
    ambiguous = await _resolve(
        records={
            **_resolved_records(),
            "account.tax": [
                _tax_record(401),
                _tax_record(402),
            ],
        }
    )
    preview = _preview()
    line = preview.lines[0]
    preview = preview.model_copy(
        update={"lines": [line.model_copy(update={"taxes": [line.taxes[0].model_copy(update={"percent": None})]})]}
    )

    assert no_match.lines[0].taxes[0].status == "unresolved"
    assert ambiguous.lines[0].taxes[0].status == "ambiguous"
    with pytest.raises(OdooUnsupportedTaxStructureError):
        await _resolve(preview=preview)


async def test_currency_exact_iso_missing_inactive_and_unexpected_ambiguous() -> None:
    resolved = await _resolve()
    missing = await _resolve(records={**_resolved_records(), "res.currency": []})
    inactive = await _resolve(
        records={**_resolved_records(), "res.currency": [{"id": 33, "name": "TRY", "active": False}]}
    )
    ambiguous = await _resolve(
        records={
            **_resolved_records(),
            "res.currency": [
                {"id": 33, "name": "TRY", "active": True},
                {"id": 34, "name": "TRY", "active": True},
            ],
        }
    )

    assert resolved.currency.status == "resolved"
    assert missing.currency.status == "unresolved"
    assert inactive.currency.status == "unresolved"
    assert ambiguous.currency.status == "ambiguous"


async def test_journal_configured_purchase_wrong_type_missing_configuration_and_multiple_safety() -> None:
    resolved = await _resolve(purchase_journal_id=55)
    wrong_type = await _resolve(
        records={**_resolved_records(), "account.journal": [{"id": 55, "code": "BILL", "type": "sale"}]}
    )
    missing = await OdooResolutionService(client=FakeReadOnlyOdooClient()).resolve(
        _request(purchase_journal_id=None, purchase_journal_code=None)
    )
    ambiguous = await _resolve(
        purchase_journal_id=None,
        purchase_journal_code="BILL",
        records={
            **_resolved_records(),
            "account.journal": [
                {"id": 55, "code": "BILL", "type": "purchase"},
                {"id": 56, "code": "BILL", "type": "purchase"},
            ],
        },
    )

    assert resolved.journal.status == "resolved"
    assert wrong_type.journal.status == "unresolved"
    assert missing.journal.status == "invalid"
    assert ambiguous.journal.status == "ambiguous"


async def test_deterministic_output_and_reviewed_preview_ids() -> None:
    result_one = await _resolve()
    result_two = await _resolve()

    assert result_one.model_dump() == result_two.model_dump()
    assert result_one.resolution_status == "resolved"
    assert result_one.reviewed_preview.invoice.currency_id == 33
    assert result_one.reviewed_preview.invoice.journal.odoo_id == 55
    assert result_one.reviewed_preview.lines[0].product.odoo_id == 301
    assert result_one.reviewed_preview.lines[0].taxes[0].odoo_id == 401


async def test_connector_errors_and_timeout_are_structured() -> None:
    with pytest.raises(OdooResolutionConnectorError):
        await OdooResolutionService(client=FakeReadOnlyOdooClient(error=ConnectorError("Odoo auth failed."))).resolve(
            _request()
        )
    with pytest.raises(OdooResolutionTimeoutError):
        await OdooResolutionService(
            client=FakeReadOnlyOdooClient(error=ConnectorTimeoutError("Odoo request timed out."))
        ).resolve(_request())


async def test_safe_logging_no_payload_leakage(monkeypatch: pytest.MonkeyPatch) -> None:
    log_calls: list[dict[str, Any]] = []

    def capture_log(message: str, *args: Any, **kwargs: Any) -> None:
        log_calls.append({"message": message, **kwargs})

    monkeypatch.setattr(odoo_resolution.logger, "info", capture_log)
    await _resolve()

    assert log_calls
    assert "Synthetic Supplier" not in str(log_calls)
    assert "Supplier Street" not in str(log_calls)
    assert "invoice_line_ids" not in str(log_calls)
    assert "<Invoice" not in str(log_calls)


def test_provider_independence_and_no_write_operations() -> None:
    service_source = (Path(__file__).resolve().parents[2] / "app" / "services" / "odoo_resolution.py").read_text()
    client_source = (Path(__file__).resolve().parents[2] / "app" / "connectors" / "odoo" / "client.py").read_text()

    assert "uyumsoft" not in service_source.lower()
    assert "xml" not in service_source.lower()
    assert "action_post" not in service_source
    assert "/create" not in service_source
    assert "/write" not in service_source
    assert "/unlink" not in service_source
    assert "res.partner/create" not in client_source
    assert "product.product/create" not in client_source
    assert "account.tax/create" not in client_source


async def test_resolution_endpoint_uses_settings_journal_and_mocked_odoo(api_client: AsyncClient) -> None:
    fake_client = FakeReadOnlyOdooClient()
    app.dependency_overrides[get_settings] = lambda: Settings(odoo_purchase_journal_id=55)
    app.dependency_overrides[get_odoo_client] = lambda: fake_client
    try:
        response = await api_client.post(
            "/api/v1/odoo/resolution",
            json=OdooResolutionRequest(preview=_preview()).model_dump(mode="json"),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["resolution_status"] == "resolved"
    assert body["reviewed_preview"]["invoice"]["journal"]["odoo_id"] == 55
    assert all(call["model"] != "account.move" for call in fake_client.calls)


async def _resolve(
    preview=None,
    records: dict[str, list[dict[str, Any]]] | None = None,
    company_id: int | None = None,
    purchase_journal_id: int | None = 55,
    purchase_journal_code: str | None = None,
    allow_productless_lines: bool = False,
):
    return await OdooResolutionService(client=FakeReadOnlyOdooClient(records=records)).resolve(
        _request(
            preview=preview,
            company_id=company_id,
            purchase_journal_id=purchase_journal_id,
            purchase_journal_code=purchase_journal_code,
            allow_productless_lines=allow_productless_lines,
        )
    )


def _request(
    preview=None,
    company_id: int | None = None,
    purchase_journal_id: int | None = 55,
    purchase_journal_code: str | None = None,
    allow_productless_lines: bool = False,
) -> OdooResolutionRequest:
    return OdooResolutionRequest(
        preview=preview or _preview(),
        company_id=company_id,
        purchase_journal_id=purchase_journal_id,
        purchase_journal_code=purchase_journal_code,
        allow_productless_lines=allow_productless_lines,
    )


def _preview():
    invoice = UblInvoiceParser().parse((FIXTURE_ROOT / "valid_invoice.xml").read_bytes())
    preview = OdooMappingPreviewService().build_preview(invoice)
    line = preview.lines[0]
    invoice_payload = preview.invoice.model_copy(update={"invoice_lines": [line], "taxes": [line.taxes[0]]})
    return preview.model_copy(update={"invoice": invoice_payload, "lines": [line]})


def _preview_with_product_code(default_code: str):
    preview = _preview()
    line = preview.lines[0]
    product = line.product.model_copy(update={"default_code": default_code}) if line.product else None
    return preview.model_copy(update={"lines": [line.model_copy(update={"product": product})]})


def _resolved_records() -> dict[str, list[dict[str, Any]]]:
    return {
        "res.partner": [{"id": 101, "vat": "1111111111", "name": "Synthetic Supplier Ltd", "active": True}],
        "product.product": [{"id": 301, "default_code": "CONSULT-001", "name": "Consulting", "active": True}],
        "account.tax": [_tax_record(401)],
        "res.currency": [{"id": 33, "name": "TRY", "active": True}],
        "account.journal": [{"id": 55, "code": "BILL", "type": "purchase", "name": "Vendor Bills"}],
    }


def _tax_record(record_id: int) -> dict[str, Any]:
    return {
        "id": record_id,
        "name": "KDV 18%",
        "amount": 18,
        "amount_type": "percent",
        "type_tax_use": "purchase",
        "price_include": False,
        "active": True,
        "company_id": [7, "Company"],
    }
