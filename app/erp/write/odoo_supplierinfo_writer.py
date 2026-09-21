from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from app.application.commands.product_remediation import CreateSupplierInfoCommand
from app.application.dto.product_remediation import SupplierInfoWriteResult, SupplierInfoWriteStatus
from app.application.exceptions.product_remediation import (
    SupplierInfoAmbiguityError,
    SupplierInfoDataIntegrityError,
    SupplierInfoDuplicateRaceError,
    SupplierInfoWriteAuthenticationError,
    SupplierInfoWriteAuthorizationError,
    SupplierInfoWriteTransportError,
    SupplierInfoWriteUnexpectedErpError,
    SupplierInfoWriteValidationError,
)
from app.application.ports.supplier_info_writer import SupplierInfoWriter
from app.connectors.exceptions import (
    ConnectorAuthenticationError,
    ConnectorAuthorizationError,
    ConnectorError,
    ConnectorTimeoutError,
    ConnectorValidationError,
)
from app.erp.write.odoo_product_write_policy import OdooProductWritePolicy

SUPPLIERINFO_MODEL = "product.supplierinfo"
SUPPLIERINFO_FIELDS = ["id", "partner_id", "product_tmpl_id", "product_id", "product_code", "company_id"]
# The create payload is fixed and never caller-supplied; this scan is defense in depth.
# ``default_code`` must never appear here: supplier identity (product_code) is strictly
# separate from ICT product identity (product.template.default_code).
FORBIDDEN_SUPPLIERINFO_TOKENS = frozenset({"default_code", "x_studio_", "active", "unlink", "__"})
_EXACT_IDENTITY_LOOKUP_LIMIT = 5


class SupplierInfoJson2Client(Protocol):
    async def create_supplierinfo(self, payload: dict[str, Any]) -> int:
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
class SupplierInfoRecord:
    id: int
    partner_id: int | None
    product_tmpl_id: int | None
    product_id: int | None
    product_code: str | None
    company_id: int | None


class OdooSupplierInfoRepository:
    """Narrow JSON-2 repository for vendor/product-code ``product.supplierinfo`` lookup and create.

    Company-scoped lookups always include ``company_id = False`` alongside the exact
    company id: an unset ``company_id`` on ``product.supplierinfo`` represents data
    shared across companies in Odoo, and equality-only matching would miss it.
    """

    def __init__(self, *, client: SupplierInfoJson2Client) -> None:
        self._client = client

    async def find_by_partner_and_code(
        self,
        *,
        partner_id: int,
        product_code: str,
        company_id: int,
    ) -> tuple[SupplierInfoRecord, ...]:
        partner_id = _require_positive_id(partner_id, source="partner_id")
        product_code = _require_text(product_code, field="product_code")
        company_id = _require_positive_id(company_id, source="company_id")
        records = await _translate_connector_errors(
            self._client.search_read(
                model=SUPPLIERINFO_MODEL,
                domain=[
                    ["partner_id", "=", partner_id],
                    ["product_code", "=", product_code],
                    ["company_id", "in", [company_id, False]],
                ],
                fields=SUPPLIERINFO_FIELDS,
                limit=_EXACT_IDENTITY_LOOKUP_LIMIT,
            )
        )
        return tuple(_record(record) for record in records)

    async def read_supplierinfo(self, supplierinfo_id: int) -> SupplierInfoRecord:
        supplierinfo_id = _require_positive_id(supplierinfo_id, source="product.supplierinfo id")
        records = await _translate_connector_errors(
            self._client.search_read(
                model=SUPPLIERINFO_MODEL,
                domain=[["id", "=", supplierinfo_id]],
                fields=SUPPLIERINFO_FIELDS,
                limit=2,
            )
        )
        if not records:
            raise SupplierInfoDataIntegrityError("The created supplierinfo could not be read back.")
        if len(records) > 1:
            raise SupplierInfoDataIntegrityError("Supplierinfo read-back returned more than one record.")
        return _record(records[0])

    async def create_supplierinfo(self, payload: dict[str, Any]) -> int:
        created = await _translate_connector_errors(self._client.create_supplierinfo(payload))
        return _require_positive_id(created, source="Odoo product.supplierinfo create")


class OdooSupplierInfoWriter(SupplierInfoWriter):
    """Protected, read-before-write, vendor/code-idempotent Odoo supplierinfo creator."""

    def __init__(
        self,
        *,
        repository: OdooSupplierInfoRepository,
        policy: OdooProductWritePolicy,
    ) -> None:
        self._repository = repository
        self._policy = policy

    async def create_supplier_info(self, command: CreateSupplierInfoCommand) -> SupplierInfoWriteResult:
        if not isinstance(command, CreateSupplierInfoCommand):
            raise SupplierInfoWriteValidationError("A canonical CreateSupplierInfoCommand is required.")

        # Dedicated master-data write gate. With default settings this raises before any Odoo call.
        self._policy.ensure_real_write_allowed(
            approved_by=command.approved_by, write_authorization=command.authorization
        )

        # Read before write, always. Natural identity is (partner_id, product_code) within
        # this company or a shared (company_id=False) record.
        existing = await self._repository.find_by_partner_and_code(
            partner_id=command.partner_id,
            product_code=command.product_code,
            company_id=command.company_id,
        )
        if len(existing) > 1:
            raise SupplierInfoAmbiguityError(
                "Multiple Odoo supplierinfo records share this exact vendor/product-code identity."
            )
        if len(existing) == 1:
            return _already_exists_result(existing[0], command=command)

        payload = _supplierinfo_payload(command)
        _reject_forbidden_tokens(payload)
        created_id = await self._repository.create_supplierinfo(payload)

        # Post-create identity re-query: detect a create race and read the record back.
        rechecked = await self._repository.find_by_partner_and_code(
            partner_id=command.partner_id,
            product_code=command.product_code,
            company_id=command.company_id,
        )
        if len(rechecked) > 1:
            raise SupplierInfoDuplicateRaceError(
                "A concurrent create produced more than one supplierinfo for this vendor/product-code identity."
            )
        if len(rechecked) != 1 or rechecked[0].id != created_id:
            raise SupplierInfoDataIntegrityError(
                "Post-create verification did not resolve to exactly the created supplierinfo."
            )
        _validate_created_supplierinfo(rechecked[0], created_id=created_id, command=command)

        return SupplierInfoWriteResult(
            status=SupplierInfoWriteStatus.CREATED,
            supplierinfo_id=created_id,
            partner_id=command.partner_id,
            product_tmpl_id=command.product_tmpl_id,
            product_id=command.product_id,
            product_code=command.product_code,
            company_id=command.company_id,
            idempotency_key=command.idempotency_key,
            safe_message="Supplierinfo created in Odoo.",
        )


def _already_exists_result(
    existing: SupplierInfoRecord,
    *,
    command: CreateSupplierInfoCommand,
) -> SupplierInfoWriteResult:
    _validate_existing_supplierinfo(existing, command=command)
    return SupplierInfoWriteResult(
        status=SupplierInfoWriteStatus.ALREADY_EXISTS,
        supplierinfo_id=existing.id,
        partner_id=command.partner_id,
        product_tmpl_id=command.product_tmpl_id,
        product_id=existing.product_id,
        product_code=command.product_code,
        company_id=command.company_id,
        idempotency_key=command.idempotency_key,
        safe_message="A supplierinfo already exists for this vendor/product-code identity.",
    )


def _supplierinfo_payload(command: CreateSupplierInfoCommand) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "partner_id": command.partner_id,
        "product_tmpl_id": command.product_tmpl_id,
        "product_code": command.product_code.strip(),
        "company_id": command.company_id,
    }
    if command.product_id is not None:
        payload["product_id"] = command.product_id
    if command.product_name is not None:
        payload["product_name"] = command.product_name.strip()
    if command.currency_id is not None:
        payload["currency_id"] = command.currency_id
    if command.delay is not None:
        payload["delay"] = command.delay
    if command.min_qty is not None:
        payload["min_qty"] = command.min_qty
    if command.price is not None:
        payload["price"] = command.price
    return payload


def _validate_existing_supplierinfo(
    existing: SupplierInfoRecord,
    *,
    command: CreateSupplierInfoCommand,
) -> None:
    if type(existing.id) is not int or existing.id <= 0:
        raise SupplierInfoDataIntegrityError("Odoo returned an invalid supplierinfo id.")
    if existing.partner_id != command.partner_id:
        raise SupplierInfoDataIntegrityError("Odoo supplierinfo partner does not match the exact query.")
    if _optional_text(existing.product_code) != command.product_code.strip():
        raise SupplierInfoDataIntegrityError("Odoo supplierinfo product_code does not match the exact query.")
    if existing.product_tmpl_id != command.product_tmpl_id:
        raise SupplierInfoDataIntegrityError(
            "An existing supplierinfo for this vendor/product-code is linked to a different product."
        )


def _validate_created_supplierinfo(
    record: SupplierInfoRecord,
    *,
    created_id: int,
    command: CreateSupplierInfoCommand,
) -> None:
    if record.id != created_id:
        raise SupplierInfoDataIntegrityError("Read-back resolved to a different supplierinfo id.")
    if record.partner_id != command.partner_id:
        raise SupplierInfoDataIntegrityError("Read-back supplierinfo partner does not match the request.")
    if _optional_text(record.product_code) != command.product_code.strip():
        raise SupplierInfoDataIntegrityError("Read-back supplierinfo product_code does not match the request.")
    if record.product_tmpl_id != command.product_tmpl_id:
        raise SupplierInfoDataIntegrityError("Read-back supplierinfo is linked to a different product.")


def _record(record: dict[str, Any]) -> SupplierInfoRecord:
    return SupplierInfoRecord(
        id=_require_positive_id(record.get("id"), source="Odoo product.supplierinfo"),
        partner_id=_many2one_id(record.get("partner_id")),
        product_tmpl_id=_many2one_id(record.get("product_tmpl_id")),
        product_id=_many2one_id(record.get("product_id")),
        product_code=_optional_text(record.get("product_code")),
        company_id=_many2one_id(record.get("company_id")),
    )


def _many2one_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, list | tuple) and value and isinstance(value[0], int) and not isinstance(value[0], bool):
        return value[0]
    return None


def _reject_forbidden_tokens(payload: dict[str, Any]) -> None:
    payload_text = str(payload).lower()
    for token in FORBIDDEN_SUPPLIERINFO_TOKENS:
        if token in payload_text:
            raise SupplierInfoWriteValidationError("Supplierinfo payload contains a forbidden field.")


async def _translate_connector_errors[T](awaitable: Any) -> T:
    try:
        return await awaitable
    except ConnectorAuthenticationError as exc:
        raise SupplierInfoWriteAuthenticationError(exc.safe_message) from exc
    except ConnectorAuthorizationError as exc:
        raise SupplierInfoWriteAuthorizationError(exc.safe_message) from exc
    except ConnectorValidationError as exc:
        raise SupplierInfoWriteValidationError(exc.safe_message) from exc
    except ConnectorTimeoutError as exc:
        raise SupplierInfoWriteTransportError(exc.safe_message) from exc
    except ConnectorError as exc:
        raise SupplierInfoWriteUnexpectedErpError(exc.safe_message) from exc
    except Exception as exc:  # noqa: BLE001 - translated to a safe supplierinfo write error
        raise SupplierInfoWriteUnexpectedErpError("Odoo supplierinfo write failed unexpectedly.") from exc


def _require_positive_id(value: object, *, source: str) -> int:
    if type(value) is not int or isinstance(value, bool) or value <= 0:
        raise SupplierInfoDataIntegrityError(f"{source} is invalid.")
    return value


def _require_text(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SupplierInfoWriteValidationError(f"{field} is required.")
    return value.strip()


def _optional_text(value: object) -> str | None:
    if value is None or value is False:
        return None
    text = str(value).strip()
    return text or None
