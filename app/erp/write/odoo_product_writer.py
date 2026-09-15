from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from app.application.commands.product_remediation import CreateProductCommand
from app.application.dto.product_remediation import ProductWriteResult, ProductWriteStatus
from app.application.exceptions.product_remediation import (
    ProductDataIntegrityError,
    ProductVariantResolutionError,
    ProductWriteAuthenticationError,
    ProductWriteAuthorizationError,
    ProductWriteTransportError,
    ProductWriteUnexpectedErpError,
    ProductWriteValidationError,
)
from app.application.ports.product_writer import ProductWriter
from app.connectors.exceptions import (
    ConnectorAuthenticationError,
    ConnectorAuthorizationError,
    ConnectorError,
    ConnectorTimeoutError,
    ConnectorValidationError,
)
from app.erp.write.odoo_product_write_policy import OdooProductWritePolicy

PRODUCT_TEMPLATE_MODEL = "product.template"
PRODUCT_VARIANT_MODEL = "product.product"
PRODUCT_TEMPLATE_FIELDS = ["id", "name", "default_code", "type", "uom_id"]
PRODUCT_VARIANT_FIELDS = ["id"]
# The create payload is fixed and never caller-supplied; this scan is defense in depth.
# category/taxes/barcode/company are explicitly left to documented Odoo defaults (P0-PROD-07F),
# and a supplier's own product code must never be written here (see odoo_supplierinfo_writer.py).
FORBIDDEN_PRODUCT_TEMPLATE_TOKENS = frozenset(
    {
        "categ_id",
        "taxes_id",
        "supplier_taxes_id",
        "barcode",
        "company_id",
        "seller_ids",
        "seller_item_code",
        "x_studio_",
        "active",
        "unlink",
        "__",
    }
)
_MAX_VARIANT_LOOKUP_LIMIT = 5


class ProductTemplateJson2Client(Protocol):
    async def create_product_template(self, payload: dict[str, Any]) -> int:
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
class ProductTemplateRecord:
    id: int
    name: str | None
    default_code: str | None


class OdooProductTemplateRepository:
    """Narrow JSON-2 repository for protected ``product.template`` create and read-back."""

    def __init__(self, *, client: ProductTemplateJson2Client) -> None:
        self._client = client

    async def create_template(self, payload: dict[str, Any]) -> int:
        created = await _translate_connector_errors(self._client.create_product_template(payload))
        return _require_positive_id(created, source="Odoo product.template create")

    async def read_template(self, template_id: int) -> ProductTemplateRecord:
        template_id = _require_positive_id(template_id, source="product.template lookup")
        records = await _translate_connector_errors(
            self._client.search_read(
                model=PRODUCT_TEMPLATE_MODEL,
                domain=[["id", "=", template_id]],
                fields=PRODUCT_TEMPLATE_FIELDS,
                limit=2,
            )
        )
        if not records:
            raise ProductDataIntegrityError("The created product template could not be read back.")
        if len(records) > 1:
            raise ProductDataIntegrityError("Product template read-back returned more than one record.")
        return _template_record(records[0])

    async def read_variant_id(self, template_id: int) -> int:
        """Deterministically resolve the single ``product.product`` variant for a template.

        Fails closed on zero or more than one variant -- CREATE_NEW_PRODUCT v1 is for a
        simple no-attribute product only, which Odoo always represents as exactly one
        variant.
        """

        template_id = _require_positive_id(template_id, source="product.template")
        records = await _translate_connector_errors(
            self._client.search_read(
                model=PRODUCT_VARIANT_MODEL,
                domain=[["product_tmpl_id", "=", template_id]],
                fields=PRODUCT_VARIANT_FIELDS,
                limit=_MAX_VARIANT_LOOKUP_LIMIT,
            )
        )
        if not records:
            raise ProductVariantResolutionError("The created product template has zero product.product variants.")
        if len(records) > 1:
            raise ProductVariantResolutionError(
                "The created product template resolved to more than one product.product variant."
            )
        return _require_positive_id(records[0].get("id"), source="Odoo product.product")


class OdooProductWriter(ProductWriter):
    """Protected, minimal-payload Odoo ``product.template`` creator with variant resolution."""

    def __init__(
        self,
        *,
        repository: OdooProductTemplateRepository,
        policy: OdooProductWritePolicy,
    ) -> None:
        self._repository = repository
        self._policy = policy

    async def create_product(self, command: CreateProductCommand) -> ProductWriteResult:
        if not isinstance(command, CreateProductCommand):
            raise ProductWriteValidationError("A canonical CreateProductCommand is required.")

        # Dedicated master-data write gate. With default settings this raises before any Odoo call.
        self._policy.ensure_real_write_allowed(approved_by=command.approved_by)

        payload = _product_template_payload(command)
        _reject_forbidden_tokens(payload)

        template_id = await self._repository.create_template(payload)

        # Read the template back and validate identity fields never drifted from the request.
        template = await self._repository.read_template(template_id)
        _validate_created_template(template, command=command, template_id=template_id)

        # product.template and product.product creation are two separate remote reads/writes;
        # this is read-after-write resolution of the variant Odoo created alongside the template.
        variant_id = await self._repository.read_variant_id(template_id)

        return ProductWriteResult(
            status=ProductWriteStatus.CREATED,
            template_id=template_id,
            product_id=variant_id,
            name=command.name,
            default_code=command.default_code,
            safe_message="Product created in Odoo.",
        )


def _product_template_payload(command: CreateProductCommand) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": command.name.strip(),
        "type": command.type,
        "uom_id": command.uom_id,
        "is_storable": command.is_storable,
    }
    # Blank/omitted internal reference stays blank/omitted -- never derived from anything.
    if command.default_code is not None:
        payload["default_code"] = command.default_code.strip()
    return payload


def _validate_created_template(
    template: ProductTemplateRecord,
    *,
    command: CreateProductCommand,
    template_id: int,
) -> None:
    if template.id != template_id:
        raise ProductDataIntegrityError("Read-back product template resolved to a different id.")
    if not (template.name or "").strip():
        raise ProductDataIntegrityError("Read-back product template has no name.")
    expected_default_code = command.default_code.strip() if command.default_code is not None else None
    actual_default_code = _optional_text(template.default_code)
    if actual_default_code != expected_default_code:
        raise ProductDataIntegrityError("Read-back product template default_code does not match the exact request.")


def _template_record(record: dict[str, Any]) -> ProductTemplateRecord:
    return ProductTemplateRecord(
        id=_require_positive_id(record.get("id"), source="Odoo product.template"),
        name=_optional_text(record.get("name")),
        default_code=_optional_text(record.get("default_code")),
    )


def _reject_forbidden_tokens(payload: dict[str, Any]) -> None:
    payload_text = str(payload).lower()
    for token in FORBIDDEN_PRODUCT_TEMPLATE_TOKENS:
        if token in payload_text:
            raise ProductWriteValidationError("Product template payload contains a forbidden field.")


async def _translate_connector_errors[T](awaitable: Any) -> T:
    try:
        return await awaitable
    except ConnectorAuthenticationError as exc:
        raise ProductWriteAuthenticationError(exc.safe_message) from exc
    except ConnectorAuthorizationError as exc:
        raise ProductWriteAuthorizationError(exc.safe_message) from exc
    except ConnectorValidationError as exc:
        raise ProductWriteValidationError(exc.safe_message) from exc
    except ConnectorTimeoutError as exc:
        raise ProductWriteTransportError(exc.safe_message) from exc
    except ConnectorError as exc:
        raise ProductWriteUnexpectedErpError(exc.safe_message) from exc
    except Exception as exc:  # noqa: BLE001 - translated to a safe product write error
        raise ProductWriteUnexpectedErpError("Odoo product write failed unexpectedly.") from exc


def _require_positive_id(value: object, *, source: str) -> int:
    if type(value) is not int or isinstance(value, bool) or value <= 0:
        raise ProductDataIntegrityError(f"{source} returned an invalid id.")
    return value


def _optional_text(value: object) -> str | None:
    if value is None or value is False:
        return None
    text = str(value).strip()
    return text or None
