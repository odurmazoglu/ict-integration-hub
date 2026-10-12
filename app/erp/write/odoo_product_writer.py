from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from app.application.commands.product_remediation import (
    CreateProductCommand,
    ValidatedProductCategory,
    ValidatedSupplierLink,
)
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
# Read back only when a validated category was written (P0-PROD-18E-2), so the
# no-category create path keeps its exact existing read-back contract.
PRODUCT_TEMPLATE_CATEGORY_FIELDS = ["categ_id", "is_storable"]
PRODUCT_VARIANT_FIELDS = ["id"]
# The create payload is fixed and never caller-supplied; this scan is defense in depth.
# taxes/barcode/company are explicitly left to documented Odoo defaults (P0-PROD-07F),
# and a supplier's own product code must never be written here (see odoo_supplierinfo_writer.py).
# categ_id stays forbidden too: the single exemption is the exact top-level key carrying
# a ValidatedProductCategory from the command (P0-PROD-18E-2) -- see _reject_forbidden_tokens.
# seller_ids likewise: the single exemption is the exact one-row create command built from
# a ValidatedSupplierLink (PR D), checked against exact key allowlists (see below).
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
#: PR D payloads are checked against exact KEY allowlists instead of the token scan: their
#: values are operator/invoice text (e.g. "Active Directory ..."), which a value scan would
#: refuse for no reason, while an allowlist is strictly tighter on the field names.
SUPPLIER_LINKED_TEMPLATE_KEYS = frozenset(
    {"name", "type", "uom_id", "is_storable", "default_code", "categ_id", "seller_ids"}
)
SUPPLIER_LINK_KEYS = frozenset({"partner_id", "product_code", "company_id", "product_name"})
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
    categ_id: int | None = None
    is_storable: bool | None = None
    type: str | None = None
    uom_id: int | None = None


class OdooProductTemplateRepository:
    """Narrow JSON-2 repository for protected ``product.template`` create and read-back."""

    def __init__(self, *, client: ProductTemplateJson2Client) -> None:
        self._client = client

    async def create_template(self, payload: dict[str, Any]) -> int:
        created = await _translate_connector_errors(self._client.create_product_template(payload))
        return _require_positive_id(created, source="Odoo product.template create")

    async def read_template(self, template_id: int, *, include_category: bool = False) -> ProductTemplateRecord:
        template_id = _require_positive_id(template_id, source="product.template lookup")
        fields = PRODUCT_TEMPLATE_FIELDS + (PRODUCT_TEMPLATE_CATEGORY_FIELDS if include_category else [])
        records = await _translate_connector_errors(
            self._client.search_read(
                model=PRODUCT_TEMPLATE_MODEL,
                domain=[["id", "=", template_id]],
                fields=fields,
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
        self._policy.ensure_real_write_allowed(
            approved_by=command.approved_by, write_authorization=command.authorization
        )

        payload = _product_template_payload(command)
        _reject_forbidden_tokens(payload, category=command.category, supplier_link=command.supplier_link)

        template_id = await self._repository.create_template(payload)

        try:
            # Read the template back and validate identity fields never drifted from the request.
            template = await self._repository.read_template(template_id, include_category=command.category is not None)
            _validate_created_template(template, command=command, template_id=template_id)

            # product.template and product.product creation are two separate remote reads/writes;
            # this is read-after-write resolution of the variant Odoo created alongside the template.
            variant_id = await self._repository.read_variant_id(template_id)
        except (ProductWriteValidationError, ProductWriteAuthenticationError, ProductWriteAuthorizationError) as exc:
            # The template EXISTS: a validation/auth refusal of a read-back must never look like the
            # "certain no write" classes callers use to retry the create (PR D pre-merge review).
            raise ProductDataIntegrityError(
                "The product was created in Odoo but its read-back failed; the outcome must be reconciled."
            ) from exc

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
    if command.category is not None:
        payload["categ_id"] = command.category.categ_id
    if command.supplier_link is not None:
        # One Odoo (0, 0, values) create command: template, variant and supplierinfo commit together.
        payload["seller_ids"] = [[0, 0, _supplier_link_values(command.supplier_link)]]
    return payload


def _supplier_link_values(link: ValidatedSupplierLink) -> dict[str, Any]:
    values: dict[str, Any] = {
        "partner_id": link.partner_id,
        "product_code": link.product_code.strip(),
        "company_id": link.company_id,
    }
    if link.product_name is not None:
        values["product_name"] = link.product_name.strip()
    return values


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
    if command.category is not None:
        if template.categ_id != command.category.categ_id:
            raise ProductDataIntegrityError("Read-back product template category does not match the exact request.")
        if template.is_storable is not command.is_storable:
            raise ProductDataIntegrityError("Read-back product template storability does not match the exact request.")
    if command.supplier_link is not None:
        # PR D: the operator chose type and UoM explicitly; Odoo must not have substituted either.
        if template.type != command.type:
            raise ProductDataIntegrityError("Read-back product template type does not match the exact request.")
        if template.uom_id != command.uom_id:
            raise ProductDataIntegrityError("Read-back product template unit of measure does not match the request.")


def _template_record(record: dict[str, Any]) -> ProductTemplateRecord:
    is_storable = record.get("is_storable")
    return ProductTemplateRecord(
        id=_require_positive_id(record.get("id"), source="Odoo product.template"),
        name=_optional_text(record.get("name")),
        default_code=_optional_text(record.get("default_code")),
        categ_id=_optional_many2one_id(record.get("categ_id")),
        is_storable=is_storable if isinstance(is_storable, bool) else None,
        type=_optional_text(record.get("type")),
        uom_id=_optional_uom_id(record.get("uom_id")),
    )


def _reject_forbidden_tokens(
    payload: dict[str, Any],
    *,
    category: ValidatedProductCategory | None = None,
    supplier_link: ValidatedSupplierLink | None = None,
) -> None:
    if supplier_link is not None:
        _require_supplier_linked_payload(payload, category=category, supplier_link=supplier_link)
        return
    scanned = dict(payload)
    if category is not None:
        # The one narrow exemption: the exact validated id under the exact top-level key.
        # Anything else mentioning categ_id (or any other forbidden token) still fails.
        if type(scanned.get("categ_id")) is not int or scanned.pop("categ_id") != category.categ_id:
            raise ProductWriteValidationError("Product template payload category does not match the validated one.")
    payload_text = str(scanned).lower()
    for token in FORBIDDEN_PRODUCT_TEMPLATE_TOKENS:
        if token in payload_text:
            raise ProductWriteValidationError("Product template payload contains a forbidden field.")


def _require_supplier_linked_payload(
    payload: dict[str, Any],
    *,
    category: ValidatedProductCategory | None,
    supplier_link: ValidatedSupplierLink,
) -> None:
    """PR D: exact key allowlists, the validated category, and exactly one validated seller row."""

    if set(payload) - SUPPLIER_LINKED_TEMPLATE_KEYS:
        raise ProductWriteValidationError("Product template payload contains a forbidden field.")
    if category is None or type(payload.get("categ_id")) is not int or payload["categ_id"] != category.categ_id:
        raise ProductWriteValidationError("Product template payload category does not match the validated one.")
    expected_row = _supplier_link_values(supplier_link)
    if payload.get("seller_ids") != [[0, 0, expected_row]] or set(expected_row) - SUPPLIER_LINK_KEYS:
        raise ProductWriteValidationError("Product template supplier link does not match the validated one.")


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


def _optional_many2one_id(value: object) -> int | None:
    if value is None or value is False:
        return None
    if isinstance(value, (list, tuple)) and value:
        value = value[0]
    if type(value) is not int or value <= 0:
        raise ProductDataIntegrityError("Odoo product.template returned an invalid category reference.")
    return value


def _optional_uom_id(value: object) -> int | None:
    # Unlike categ_id, a malformed UoM is reported as unknown; only the PR D read-back compares it.
    if isinstance(value, (list, tuple)) and value:
        value = value[0]
    return value if type(value) is int and value > 0 else None


def _optional_text(value: object) -> str | None:
    if value is None or value is False:
        return None
    text = str(value).strip()
    return text or None
