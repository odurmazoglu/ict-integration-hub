import logging
import re
from decimal import Decimal
from time import perf_counter
from typing import Any

from app.connectors.exceptions import ConnectorError, ConnectorTimeoutError
from app.connectors.odoo.client import OdooJson2Client
from app.schemas.odoo_mapping import (
    OdooInvoiceLinePayload,
    OdooMappingPreview,
    OdooTaxCandidate,
)
from app.schemas.odoo_resolution import (
    OdooEntityResolution,
    OdooLineResolution,
    OdooResolutionIssue,
    OdooResolutionRequest,
    OdooResolutionResult,
)

logger = logging.getLogger(__name__)


class OdooResolutionError(Exception):
    error_category = "resolution_error"

    def __init__(self, safe_message: str) -> None:
        super().__init__(safe_message)
        self.safe_message = safe_message


class OdooResolutionValidationError(OdooResolutionError):
    error_category = "invalid_resolution_input"


class OdooResolutionConfigurationError(OdooResolutionError):
    error_category = "missing_required_configuration"


class OdooResolutionConnectorError(OdooResolutionError):
    error_category = "connector_error"


class OdooResolutionTimeoutError(OdooResolutionConnectorError):
    error_category = "timeout"


class OdooResolutionUnexpectedResponseError(OdooResolutionConnectorError):
    error_category = "unexpected_odoo_response"


class OdooUnsupportedTaxStructureError(OdooResolutionValidationError):
    error_category = "unsupported_tax_structure"


class OdooResolutionService:
    def __init__(self, *, client: OdooJson2Client) -> None:
        self._client = client

    async def resolve(self, request: OdooResolutionRequest) -> OdooResolutionResult:
        started = perf_counter()
        try:
            result = await self._resolve(request)
        except ConnectorTimeoutError as exc:
            _log_resolution(
                started=started,
                preview=request.preview,
                entity_type="partner",
                status="invalid",
                match_method=None,
                candidate_count=0,
                error_category="timeout",
            )
            raise OdooResolutionTimeoutError(exc.safe_message) from exc
        except ConnectorError as exc:
            _log_resolution(
                started=started,
                preview=request.preview,
                entity_type="partner",
                status="invalid",
                match_method=None,
                candidate_count=0,
                error_category="connector_error",
            )
            raise OdooResolutionConnectorError(exc.safe_message) from exc

        for resolution in _all_entity_resolutions(result):
            _log_resolution(
                started=started,
                preview=request.preview,
                entity_type=resolution.entity_type,
                status=resolution.status,
                match_method=resolution.match_method,
                candidate_count=resolution.candidate_count,
                error_category=None,
            )
        return result

    async def _resolve(self, request: OdooResolutionRequest) -> OdooResolutionResult:
        _validate_preview(request.preview)
        partner = await self._resolve_partner(request.preview)
        currency = await self._resolve_currency(request.preview)
        journal = await self._resolve_journal(request)
        lines = [
            await self._resolve_line(line=line, index=index, request=request)
            for index, line in enumerate(request.preview.lines)
        ]
        reviewed_preview = _apply_resolutions(
            request.preview,
            partner=partner,
            currency=currency,
            journal=journal,
            lines=lines,
        )
        warnings, missing, ambiguous = _issues(partner=partner, currency=currency, journal=journal, lines=lines)
        has_invalid = _invalid_resolutions(partner, currency, journal, lines)
        status = "resolved" if not missing and not ambiguous and not has_invalid else "needs_review"
        if has_invalid:
            status = "invalid"
        return OdooResolutionResult(
            resolution_status=status,
            reviewed_preview=reviewed_preview,
            partner=partner,
            currency=currency,
            journal=journal,
            lines=lines,
            warnings=warnings,
            missing_matches=missing,
            ambiguous_matches=ambiguous,
        )

    async def _resolve_partner(self, preview: OdooMappingPreview) -> OdooEntityResolution:
        partner = preview.invoice.partner
        if partner is None:
            return _entity("partner", "unresolved", "invoice.partner", "Partner candidate is missing.")
        vat = _normalize_vat(partner.tax_id)
        if vat:
            records = await self._search_read(
                model="res.partner",
                domain=[["vat", "=", vat], ["active", "=", True]],
                fields=["id", "vat", "name", "active"],
            )
            filtered = [record for record in records if _normalize_vat(_text(record.get("vat"))) == vat]
            resolution = _single_or_diagnostic("partner", filtered, "vat", "invoice.partner", "Partner VAT match")
            if resolution.status != "unresolved":
                return resolution
        name = _normalize_name(partner.name)
        if name:
            records = await self._search_read(
                model="res.partner",
                domain=[["name", "=", partner.name], ["active", "=", True]],
                fields=["id", "name", "active"],
            )
            filtered = [record for record in records if _normalize_name(_text(record.get("name"))) == name]
            return _single_or_diagnostic("partner", filtered, "name", "invoice.partner", "Partner name match")
        return _entity("partner", "unresolved", "invoice.partner", "Partner match key is missing.")

    async def _resolve_currency(self, preview: OdooMappingPreview) -> OdooEntityResolution:
        code = _normalize_code(preview.invoice.currency)
        if code is None:
            return _entity("currency", "unresolved", "invoice.currency", "Currency code is missing.")
        records = await self._search_read(
            model="res.currency",
            domain=[["name", "=", code], ["active", "=", True]],
            fields=["id", "name", "active"],
        )
        filtered = [
            record
            for record in records
            if _normalize_code(_text(record.get("name"))) == code and record.get("active") is True
        ]
        return _single_or_diagnostic("currency", filtered, "iso_code", "invoice.currency", "Currency code match")

    async def _resolve_journal(self, request: OdooResolutionRequest) -> OdooEntityResolution:
        journal_id = request.purchase_journal_id
        journal_code = _normalize_code(request.purchase_journal_code)
        if journal_id is None and journal_code is None:
            return _entity("journal", "invalid", "purchase_journal", "Purchase journal configuration is missing.")
        if journal_id is not None:
            domain: list[Any] = [["id", "=", journal_id], ["type", "=", "purchase"]]
            method = "configured_id"
        else:
            domain = [["code", "=", journal_code], ["type", "=", "purchase"]]
            method = "configured_code"
        records = await self._search_read(
            model="account.journal",
            domain=domain,
            fields=["id", "code", "type", "name"],
        )
        filtered = [record for record in records if record.get("type") == "purchase"]
        if journal_id is not None:
            filtered = [record for record in filtered if record.get("id") == journal_id]
        if journal_code is not None:
            filtered = [record for record in filtered if _normalize_code(_text(record.get("code"))) == journal_code]
        return _single_or_diagnostic("journal", filtered, method, "purchase_journal", "Purchase journal match")

    async def _resolve_line(
        self,
        *,
        line: OdooInvoiceLinePayload,
        index: int,
        request: OdooResolutionRequest,
    ) -> OdooLineResolution:
        product = await self._resolve_product(
            line=line,
            index=index,
            allow_productless=request.allow_productless_lines,
        )
        taxes = [
            await self._resolve_tax(tax=tax, line_index=index, tax_index=tax_index, request=request)
            for tax_index, tax in enumerate(line.taxes)
        ]
        return OdooLineResolution(sequence=line.sequence, product=product, taxes=taxes)

    async def _resolve_product(
        self,
        *,
        line: OdooInvoiceLinePayload,
        index: int,
        allow_productless: bool,
    ) -> OdooEntityResolution:
        field_path = f"lines[{index}].product"
        if line.product is None:
            if allow_productless:
                return _entity("product", "not_required", field_path, "Productless expense line is allowed.")
            return _entity("product", "unresolved", field_path, "Product candidate is missing.")
        default_code = _normalize_code(line.product.default_code)
        if default_code is not None:
            records = await self._search_read(
                model="product.product",
                domain=[["default_code", "=", default_code], ["active", "=", True]],
                fields=["id", "default_code", "name", "active"],
            )
            filtered = [
                record for record in records if _normalize_code(_text(record.get("default_code"))) == default_code
            ]
            resolution = _single_or_diagnostic(
                "product",
                filtered,
                "default_code",
                field_path,
                "Product default code match",
            )
            if resolution.status != "unresolved":
                return resolution
        name = _normalize_name(line.product.name)
        if name:
            records = await self._search_read(
                model="product.product",
                domain=[["name", "=", line.product.name], ["active", "=", True]],
                fields=["id", "default_code", "name", "active"],
            )
            filtered = [record for record in records if _normalize_name(_text(record.get("name"))) == name]
            return _single_or_diagnostic("product", filtered, "name", field_path, "Product name match")
        return _entity("product", "unresolved", field_path, "Product match key is missing.")

    async def _resolve_tax(
        self,
        *,
        tax: OdooTaxCandidate,
        line_index: int,
        tax_index: int,
        request: OdooResolutionRequest,
    ) -> OdooEntityResolution:
        field_path = f"lines[{line_index}].taxes[{tax_index}]"
        if tax.percent is None:
            raise OdooUnsupportedTaxStructureError("Only percentage tax resolution is supported.")
        amount = Decimal(tax.percent)
        domain: list[Any] = [
            ["type_tax_use", "=", "purchase"],
            ["amount_type", "=", "percent"],
            ["amount", "=", float(amount)],
            ["active", "=", True],
        ]
        if request.company_id is not None:
            domain.append(["company_id", "=", request.company_id])
        if tax.price_include is not None:
            domain.append(["price_include", "=", tax.price_include])
        records = await self._search_read(
            model="account.tax",
            domain=domain,
            fields=["id", "name", "amount", "amount_type", "type_tax_use", "price_include", "active", "company_id"],
        )
        filtered = [
            record
            for record in records
            if record.get("type_tax_use") == "purchase"
            and record.get("amount_type") == "percent"
            and _decimal_equal(record.get("amount"), amount)
            and record.get("active") is True
            and (request.company_id is None or _rel_id(record.get("company_id")) == request.company_id)
            and (tax.price_include is None or record.get("price_include") is tax.price_include)
        ]
        return _single_or_diagnostic("tax", filtered, "purchase_percent", field_path, "Purchase tax match")

    async def _search_read(
        self,
        *,
        model: str,
        domain: list[Any],
        fields: list[str],
    ) -> list[dict[str, Any]]:
        records = await self._client.search_read(model=model, domain=domain, fields=fields, limit=20)
        return records


def _validate_preview(preview: OdooMappingPreview) -> None:
    if preview.invoice.ettn is None or not preview.invoice.ettn.strip():
        raise OdooResolutionValidationError("ETTN is required for resolution.")
    if preview.mapping_status not in {"ready", "needs_review"}:
        raise OdooResolutionValidationError("Mapping preview status is invalid.")


def _single_or_diagnostic(
    entity_type: str,
    records: list[dict[str, Any]],
    match_method: str,
    field_path: str,
    label: str,
) -> OdooEntityResolution:
    if len(records) == 1:
        odoo_id = records[0].get("id")
        if not isinstance(odoo_id, int):
            return _entity(
                entity_type,
                "invalid",
                field_path,
                "Odoo record id is invalid.",
                len(records),
                match_method,
            )
        return _entity(entity_type, "resolved", field_path, label, len(records), match_method, odoo_id)
    if len(records) > 1:
        return _entity(
            entity_type,
            "ambiguous",
            field_path,
            "Multiple exact candidates were found.",
            len(records),
            match_method,
        )
    return _entity(entity_type, "unresolved", field_path, "No exact match was found.", 0, match_method)


def _entity(
    entity_type: str,
    status: str,
    field_path: str,
    safe_message: str,
    candidate_count: int = 0,
    match_method: str | None = None,
    odoo_id: int | None = None,
) -> OdooEntityResolution:
    return OdooEntityResolution(
        entity_type=entity_type,
        status=status,
        odoo_id=odoo_id,
        match_method=match_method,
        candidate_count=candidate_count,
        field_path=field_path,
        safe_message=safe_message,
    )


def _apply_resolutions(
    preview: OdooMappingPreview,
    *,
    partner: OdooEntityResolution,
    currency: OdooEntityResolution,
    journal: OdooEntityResolution,
    lines: list[OdooLineResolution],
) -> OdooMappingPreview:
    reviewed_lines = []
    for line, resolution in zip(preview.lines, lines, strict=True):
        product = line.product.model_copy(update={"odoo_id": resolution.product.odoo_id}) if line.product else None
        taxes = [
            tax.model_copy(update={"odoo_id": tax_resolution.odoo_id})
            for tax, tax_resolution in zip(line.taxes, resolution.taxes, strict=True)
        ]
        reviewed_lines.append(line.model_copy(update={"product": product, "taxes": taxes}))
    invoice = preview.invoice.model_copy(
        update={
            "partner": preview.invoice.partner.model_copy(update={"odoo_id": partner.odoo_id})
            if preview.invoice.partner
            else None,
            "currency_id": currency.odoo_id,
            "journal": preview.invoice.journal.model_copy(update={"odoo_id": journal.odoo_id})
            if preview.invoice.journal
            else None,
            "invoice_lines": reviewed_lines,
            "taxes": _resolved_invoice_taxes(preview, lines),
        }
    )
    return preview.model_copy(update={"invoice": invoice, "lines": reviewed_lines})


def _resolved_invoice_taxes(preview: OdooMappingPreview, lines: list[OdooLineResolution]) -> list[OdooTaxCandidate]:
    resolved: dict[tuple[str | None, Decimal | None], OdooTaxCandidate] = {}
    for line, resolution in zip(preview.lines, lines, strict=True):
        for tax, tax_resolution in zip(line.taxes, resolution.taxes, strict=True):
            key = (tax.name, tax.percent)
            if key not in resolved:
                resolved[key] = tax.model_copy(update={"odoo_id": tax_resolution.odoo_id})
    return list(resolved.values())


def _issues(
    *,
    partner: OdooEntityResolution,
    currency: OdooEntityResolution,
    journal: OdooEntityResolution,
    lines: list[OdooLineResolution],
) -> tuple[list[OdooResolutionIssue], list[OdooResolutionIssue], list[OdooResolutionIssue]]:
    warnings: list[OdooResolutionIssue] = []
    missing: list[OdooResolutionIssue] = []
    ambiguous: list[OdooResolutionIssue] = []
    for resolution in _line_entities(partner, currency, journal, lines):
        issue = OdooResolutionIssue(
            code=f"{resolution.entity_type}_{resolution.status}",
            message=resolution.safe_message or "Resolution requires review.",
            field_path=resolution.field_path,
            entity_type=resolution.entity_type,
        )
        if resolution.status == "unresolved":
            missing.append(issue)
        elif resolution.status == "ambiguous":
            ambiguous.append(issue)
        elif resolution.status in {"invalid", "not_required"}:
            warnings.append(issue)
    return warnings, missing, ambiguous


def _invalid_resolutions(
    partner: OdooEntityResolution,
    currency: OdooEntityResolution,
    journal: OdooEntityResolution,
    lines: list[OdooLineResolution],
) -> bool:
    return any(resolution.status == "invalid" for resolution in _line_entities(partner, currency, journal, lines))


def _line_entities(
    partner: OdooEntityResolution,
    currency: OdooEntityResolution,
    journal: OdooEntityResolution,
    lines: list[OdooLineResolution],
) -> list[OdooEntityResolution]:
    return [
        partner,
        currency,
        journal,
        *[line.product for line in lines],
        *[tax for line in lines for tax in line.taxes],
    ]


def _all_entity_resolutions(result: OdooResolutionResult) -> list[OdooEntityResolution]:
    return [
        result.partner,
        result.currency,
        result.journal,
        *[line.product for line in result.lines],
        *[tax for line in result.lines for tax in line.taxes],
    ]


def _normalize_vat(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = re.sub(r"[^A-Za-z0-9]", "", value).upper()
    return normalized or None


def _normalize_name(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = " ".join(value.casefold().split())
    return normalized or None


def _normalize_code(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().upper()
    return normalized or None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _rel_id(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, list) and value and isinstance(value[0], int):
        return value[0]
    return None


def _decimal_equal(value: Any, expected: Decimal) -> bool:
    try:
        return Decimal(str(value)) == expected
    except Exception:
        return False


def _log_resolution(
    *,
    started: float,
    preview: OdooMappingPreview,
    entity_type: str,
    status: str,
    match_method: str | None,
    candidate_count: int,
    error_category: str | None,
) -> None:
    extra: dict[str, Any] = {
        "invoice_id": preview.invoice.invoice_number,
        "ettn": preview.invoice.ettn,
        "entity_type": entity_type,
        "resolution_status": status,
        "match_method": match_method,
        "candidate_count": candidate_count,
        "duration_ms": round((perf_counter() - started) * 1000, 2),
    }
    if error_category is not None:
        extra["safe_error_category"] = error_category
    logger.info("odoo_resolution_completed", extra=extra)
