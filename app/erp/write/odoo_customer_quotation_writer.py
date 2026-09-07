from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from app.application.quotation.execution import (
    CreateCustomerQuotationCommand,
    CustomerQuotationCreationResult,
    CustomerQuotationLine,
    CustomerQuotationPricelistResolver,
    CustomerQuotationWriter,
)
from app.connectors.exceptions import (
    ConnectorAuthenticationError,
    ConnectorAuthorizationError,
    ConnectorError,
    ConnectorTimeoutError,
    ConnectorValidationError,
)
from app.core.config import Settings
from app.core.runtime_checks import PRODUCTION_APPROVAL_ACK
from app.erp.write.exceptions import (
    CustomerQuotationWriteAuthenticationError,
    CustomerQuotationWriteAuthorizationError,
    CustomerQuotationWriteConfigurationError,
    CustomerQuotationWriteDuplicateError,
    CustomerQuotationWritePricelistError,
    CustomerQuotationWriteSafetyGateError,
    CustomerQuotationWriteTransportError,
    CustomerQuotationWriteUnexpectedErpError,
    CustomerQuotationWriteValidationError,
)

SALE_ORDER_MODEL = "sale.order"
PRODUCT_PRICELIST_MODEL = "product.pricelist"
RES_CURRENCY_MODEL = "res.currency"

FORBIDDEN_SALE_ORDER_TOKENS = frozenset(
    {
        "action_confirm",
        "action_cancel",
        "action_done",
        "client_order_ref",
        "unlink",
    }
)


class CustomerQuotationJson2Client(Protocol):
    async def create_sale_order(self, payload: dict[str, Any]) -> int:
        pass

    async def search_read(
        self,
        *,
        model: str,
        domain: list[Any],
        fields: list[str],
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        pass


@dataclass(frozen=True, slots=True)
class SaleOrderDraft:
    id: int
    name: str | None = None


@dataclass(frozen=True, slots=True)
class OdooCustomerQuotationFieldMapping:
    """Deployment field mapping for the customer quotation ``sale.order`` writer.

    ``execution_key_field`` must name a pre-existing manually created technical
    field on ``sale.order`` (recommended: ``x_studio_ict_hub_execution_key``,
    a stored, indexed Char). The Hub never creates Studio schema. When it is not
    configured, real creation and lookup fail closed. ``client_order_ref`` is
    never used as the technical key.
    """

    execution_key_field: str | None = None
    opportunity_field: str | None = None
    source_reference_field: str | None = None
    line_uom_field: str = "product_uom_id"

    @classmethod
    def from_environment(cls, *, prefix: str = "ODOO_CUSTOMER_QUOTATION_") -> OdooCustomerQuotationFieldMapping:
        line_uom_field = os.environ.get(f"{prefix}LINE_UOM_FIELD", "").strip() or "product_uom_id"
        return cls(
            execution_key_field=_env_optional(prefix, "EXECUTION_KEY_FIELD"),
            opportunity_field=_env_optional(prefix, "OPPORTUNITY_FIELD"),
            source_reference_field=_env_optional(prefix, "SOURCE_REFERENCE_FIELD"),
            line_uom_field=line_uom_field,
        )

    def require_execution_key_field(self) -> str:
        field = (self.execution_key_field or "").strip()
        if not field:
            raise CustomerQuotationWriteConfigurationError(
                "A pre-existing sale.order technical execution-key field must be configured "
                "(ODOO_CUSTOMER_QUOTATION_EXECUTION_KEY_FIELD). Hub does not create Studio schema."
            )
        if field == "client_order_ref":
            raise CustomerQuotationWriteConfigurationError(
                "client_order_ref must not be used as the technical execution-key field."
            )
        return field


@dataclass(frozen=True, slots=True)
class OdooCustomerQuotationWritePolicy:
    production_operations_enabled: bool = False
    production_approval_ack: str = ""
    customer_quotation_execute_enabled: bool = False
    required_approval_ack: str = PRODUCTION_APPROVAL_ACK

    @classmethod
    def from_settings(cls, settings: Settings) -> OdooCustomerQuotationWritePolicy:
        return cls(
            production_operations_enabled=settings.production_operations_enabled,
            production_approval_ack=settings.production_approval_ack,
            customer_quotation_execute_enabled=settings.customer_quotation_execute_enabled,
        )

    def ensure_real_write_allowed(self, *, approved_by: str | None) -> None:
        if not self.production_operations_enabled:
            raise CustomerQuotationWriteSafetyGateError("Production operations must be explicitly enabled.")
        if self.production_approval_ack != self.required_approval_ack:
            raise CustomerQuotationWriteSafetyGateError("Production approval acknowledgement is required.")
        if not self.customer_quotation_execute_enabled:
            raise CustomerQuotationWriteSafetyGateError("Customer quotation execution must be explicitly enabled.")
        if approved_by is None or not approved_by.strip():
            raise CustomerQuotationWriteSafetyGateError("A named approver is required for customer quotation creation.")


class OdooCustomerQuotationPricelistResolver(CustomerQuotationPricelistResolver):
    """Resolve exactly one existing Odoo pricelist for ``company_id`` + currency."""

    def __init__(self, *, client: CustomerQuotationJson2Client) -> None:
        self._client = client

    async def resolve_pricelist_id(self, *, company_id: int, currency: str) -> int:
        company_id = _require_company_id(company_id)
        currency_code = _require_currency(currency)
        currency_records = await _translate_connector_errors(
            self._client.search_read(
                model=RES_CURRENCY_MODEL,
                domain=[["name", "=", currency_code], ["active", "in", [True, False]]],
                fields=["id", "name"],
                limit=2,
            )
        )
        currency_id = _exactly_one_id(
            currency_records,
            missing="No Odoo currency matches the immutable scenario currency.",
            ambiguous="Multiple Odoo currencies match the immutable scenario currency.",
        )
        pricelist_records = await _translate_connector_errors(
            self._client.search_read(
                model=PRODUCT_PRICELIST_MODEL,
                domain=[
                    ["currency_id", "=", currency_id],
                    ["company_id", "in", [company_id, False]],
                    ["active", "in", [True, False]],
                ],
                fields=["id", "name", "currency_id", "company_id"],
                limit=2,
            )
        )
        return _exactly_one_id(
            pricelist_records,
            missing="No Odoo pricelist matches this company and currency.",
            ambiguous="Multiple Odoo pricelists match this company and currency.",
        )


class OdooCustomerQuotationRepository:
    """Narrow JSON-2 repository for deterministic draft ``sale.order`` lookup/create."""

    def __init__(
        self,
        *,
        client: CustomerQuotationJson2Client,
        mapping: OdooCustomerQuotationFieldMapping,
    ) -> None:
        self._client = client
        self._mapping = mapping

    async def find_by_execution_key(
        self,
        *,
        company_id: int,
        execution_key: str,
    ) -> SaleOrderDraft | None:
        company_id = _require_company_id(company_id)
        execution_key = _require_execution_key(execution_key)
        key_field = self._mapping.require_execution_key_field()
        records = await _translate_connector_errors(
            self._client.search_read(
                model=SALE_ORDER_MODEL,
                domain=[[key_field, "=", execution_key], ["company_id", "=", company_id]],
                fields=["id", "name", key_field],
                limit=2,
            )
        )
        if not records:
            return None
        if len(records) > 1:
            raise CustomerQuotationWriteDuplicateError(
                "Multiple existing Odoo customer quotations share this execution key."
            )
        first = records[0]
        draft_id = first.get("id")
        if type(draft_id) is not int or draft_id <= 0:
            raise CustomerQuotationWriteUnexpectedErpError("Odoo returned an invalid sale.order id.")
        return SaleOrderDraft(id=draft_id, name=_optional_text(first.get("name")))

    async def create_draft(
        self,
        *,
        command: CreateCustomerQuotationCommand,
        pricelist_id: int,
        execution_key: str,
    ) -> SaleOrderDraft:
        payload = build_sale_order_payload(
            command,
            pricelist_id=pricelist_id,
            execution_key=execution_key,
            mapping=self._mapping,
        )
        order_id = await _translate_connector_errors(self._client.create_sale_order(payload))
        if type(order_id) is not int or order_id <= 0:
            raise CustomerQuotationWriteUnexpectedErpError("Odoo returned an invalid sale.order id.")
        return SaleOrderDraft(id=order_id)


class OdooCustomerQuotationWriter(CustomerQuotationWriter):
    """Idempotent draft customer Sales Quotation writer for one immutable scenario."""

    def __init__(
        self,
        *,
        repository: OdooCustomerQuotationRepository,
        pricelist_resolver: CustomerQuotationPricelistResolver,
        policy: OdooCustomerQuotationWritePolicy,
    ) -> None:
        self._repository = repository
        self._pricelist_resolver = pricelist_resolver
        self._policy = policy

    async def create_quotation(
        self,
        command: CreateCustomerQuotationCommand,
    ) -> CustomerQuotationCreationResult:
        if not isinstance(command, CreateCustomerQuotationCommand):
            raise CustomerQuotationWriteValidationError("A canonical CreateCustomerQuotationCommand is required.")

        self._policy.ensure_real_write_allowed(approved_by=command.approved_by)
        draft = command.draft
        execution_key = command.execution_key

        existing = await self._repository.find_by_execution_key(
            company_id=draft.company_id,
            execution_key=execution_key,
        )
        if existing is not None:
            return CustomerQuotationCreationResult(
                external_quotation_id=existing.id,
                execution_key=execution_key,
                created=False,
                external_reference=existing.name,
            )

        pricelist_id = await self._pricelist_resolver.resolve_pricelist_id(
            company_id=draft.company_id,
            currency=draft.currency,
        )
        created = await self._repository.create_draft(
            command=command,
            pricelist_id=pricelist_id,
            execution_key=execution_key,
        )
        return CustomerQuotationCreationResult(
            external_quotation_id=created.id,
            execution_key=execution_key,
            created=True,
            external_reference=created.name,
        )


def build_sale_order_payload(
    command: CreateCustomerQuotationCommand,
    *,
    pricelist_id: int,
    execution_key: str,
    mapping: OdooCustomerQuotationFieldMapping,
) -> dict[str, Any]:
    """Build the one-call nested ``sale.order`` create payload.

    The order and all of its lines are created in a single ``create`` call using
    Odoo one2many command tuples, so there is no multi-call partial-write window.
    """

    key_field = mapping.require_execution_key_field()
    pricelist_id = _require_pricelist_id(pricelist_id)
    execution_key = _require_execution_key(execution_key)
    draft = command.draft

    payload: dict[str, Any] = {
        "company_id": draft.company_id,
        "partner_id": draft.customer_id,
        "pricelist_id": pricelist_id,
        key_field: execution_key,
        "order_line": [(0, 0, _sale_order_line_payload(line, mapping=mapping)) for line in draft.lines],
    }
    if mapping.opportunity_field and draft.opportunity_id is not None:
        payload[mapping.opportunity_field.strip()] = draft.opportunity_id
    if mapping.source_reference_field:
        payload[mapping.source_reference_field.strip()] = draft.scenario_name

    _reject_forbidden_tokens(payload)
    return payload


def _sale_order_line_payload(
    line: CustomerQuotationLine,
    *,
    mapping: OdooCustomerQuotationFieldMapping,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "product_id": line.product_variant_id,
        "product_uom_qty": _decimal_text(line.quantity),
        "price_unit": _decimal_text(line.sales_unit_price),
    }
    if line.uom_id is not None:
        payload[mapping.line_uom_field] = line.uom_id
    if line.description is not None:
        payload["name"] = line.description
    return payload


def _reject_forbidden_tokens(payload: dict[str, Any]) -> None:
    payload_text = str(payload).lower()
    for token in FORBIDDEN_SALE_ORDER_TOKENS:
        if token in payload_text:
            raise CustomerQuotationWriteValidationError(
                "Customer quotation payload contains a forbidden write operation."
            )


async def _translate_connector_errors[T](awaitable: Any) -> T:
    try:
        return await awaitable
    except ConnectorAuthenticationError as exc:
        raise CustomerQuotationWriteAuthenticationError(exc.safe_message) from exc
    except ConnectorAuthorizationError as exc:
        raise CustomerQuotationWriteAuthorizationError(exc.safe_message) from exc
    except ConnectorValidationError as exc:
        raise CustomerQuotationWriteValidationError(exc.safe_message) from exc
    except ConnectorTimeoutError as exc:
        raise CustomerQuotationWriteTransportError(exc.safe_message) from exc
    except ConnectorError as exc:
        raise CustomerQuotationWriteUnexpectedErpError(exc.safe_message) from exc
    except Exception as exc:
        raise CustomerQuotationWriteUnexpectedErpError("Odoo customer quotation write failed unexpectedly.") from exc


def _exactly_one_id(records: list[dict[str, Any]], *, missing: str, ambiguous: str) -> int:
    if not records:
        raise CustomerQuotationWritePricelistError(missing)
    if len(records) > 1:
        raise CustomerQuotationWritePricelistError(ambiguous)
    value = records[0].get("id")
    if type(value) is not int or value <= 0:
        raise CustomerQuotationWriteUnexpectedErpError("Odoo returned an invalid record id.")
    return value


def _require_company_id(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise CustomerQuotationWriteValidationError("company_id is required for customer quotation write operations.")
    return value


def _require_pricelist_id(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise CustomerQuotationWriteValidationError("A resolved pricelist_id is required.")
    return value


def _require_execution_key(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CustomerQuotationWriteValidationError("A technical execution key is required.")
    return value


def _require_currency(value: object) -> str:
    if not isinstance(value, str) or len(value.strip()) != 3 or not value.strip().isalpha():
        raise CustomerQuotationWriteValidationError("A three-letter currency code is required.")
    return value.strip().upper()


def _decimal_text(value: Decimal) -> str:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise CustomerQuotationWriteValidationError("A canonical Decimal value is required.")
    return format(value, "f")


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _env_optional(prefix: str, name: str) -> str | None:
    value = os.environ.get(f"{prefix}{name}", "").strip()
    return value or None
