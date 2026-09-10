from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlparse

from app.application.commands.supplier_partner import CreateSupplierPartnerCommand
from app.application.dto.supplier_partner import SupplierPartnerWriteResult, SupplierPartnerWriteStatus
from app.application.exceptions.supplier_partner import (
    SupplierPartnerAmbiguityError,
    SupplierPartnerDataIntegrityError,
    SupplierPartnerDuplicateRaceError,
    SupplierPartnerInactiveError,
    SupplierPartnerWriteAuthenticationError,
    SupplierPartnerWriteAuthorizationError,
    SupplierPartnerWriteSafetyGateError,
    SupplierPartnerWriteTransportError,
    SupplierPartnerWriteUnexpectedErpError,
    SupplierPartnerWriteValidationError,
)
from app.application.ports.supplier_partner_writer import SupplierPartnerWriter
from app.connectors.exceptions import (
    ConnectorAuthenticationError,
    ConnectorAuthorizationError,
    ConnectorError,
    ConnectorTimeoutError,
    ConnectorValidationError,
)
from app.core.config import Settings
from app.core.runtime_checks import APPROVED_STAGING_ODOO_HOSTS, PRODUCTION_APPROVAL_ACK

RES_PARTNER_MODEL = "res.partner"
PARTNER_FIELDS = ["id", "name", "vat", "active", "company_id"]
# res.partner legal-entity typing: a VKN/TCKN holder is an organization, not a person.
SUPPLIER_COMPANY_TYPE = "company"
# The create payload is fixed and never caller-supplied; this scan is defense in depth.
FORBIDDEN_RES_PARTNER_TOKENS = frozenset({"active", "unlink", "action_", "message_", "__", "parent_id"})
_EXACT_VAT_LOOKUP_LIMIT = 5


class SupplierPartnerJson2Client(Protocol):
    async def create_res_partner(self, payload: dict[str, Any]) -> int:
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
class SupplierPartnerRecord:
    id: int
    name: str | None
    vat: str | None
    active: bool
    company_id: int | None


@dataclass(frozen=True, slots=True)
class OdooSupplierPartnerWritePolicy:
    """Dedicated authorization for the first Odoo supplier master-data write.

    This gate is independent of Vendor Bill / Customer Invoice / Customer Quotation
    execution. It never reads ``execution_execute_enabled`` and never weakens the
    production operation acknowledgements.
    """

    supplier_remediation_write_enabled: bool = False
    production_operations_enabled: bool = False
    production_approval_ack: str = ""
    required_approval_ack: str = PRODUCTION_APPROVAL_ACK
    app_env: str = "development"
    odoo_host: str = ""
    approved_staging_hosts: frozenset[str] = APPROVED_STAGING_ODOO_HOSTS

    @classmethod
    def from_settings(cls, settings: Settings) -> OdooSupplierPartnerWritePolicy:
        return cls(
            supplier_remediation_write_enabled=settings.supplier_remediation_write_enabled,
            production_operations_enabled=settings.production_operations_enabled,
            production_approval_ack=settings.production_approval_ack,
            app_env=settings.app_env,
            odoo_host=_normalized_host(settings.odoo_base_url),
        )

    @property
    def staging_write_sanctioned(self) -> bool:
        return (
            self.app_env != "production"
            and self.supplier_remediation_write_enabled
            and self.odoo_host in self.approved_staging_hosts
        )

    def ensure_real_write_allowed(self, *, approved_by: str | None) -> None:
        if not self.supplier_remediation_write_enabled:
            raise SupplierPartnerWriteSafetyGateError(
                "Supplier remediation master-data write must be explicitly enabled."
            )
        if self.staging_write_sanctioned:
            _ensure_named_approver(approved_by)
            return
        if not self.production_operations_enabled:
            raise SupplierPartnerWriteSafetyGateError("Production operations must be explicitly enabled.")
        if self.production_approval_ack != self.required_approval_ack:
            raise SupplierPartnerWriteSafetyGateError("Production approval acknowledgement is required.")
        _ensure_named_approver(approved_by)


class OdooSupplierPartnerRepository:
    """Narrow JSON-2 repository for exact-VAT ``res.partner`` lookup and protected create."""

    def __init__(self, *, client: SupplierPartnerJson2Client) -> None:
        self._client = client

    async def find_by_exact_vat(
        self,
        *,
        normalized_vat: str,
        company_id: int,
    ) -> tuple[SupplierPartnerRecord, ...]:
        normalized_vat = _require_normalized_vat(normalized_vat)
        company_id = _require_company_id(company_id)
        records = await _translate_connector_errors(
            self._client.search_read(
                model=RES_PARTNER_MODEL,
                domain=[
                    ["vat", "=", normalized_vat],
                    ["company_id", "in", [company_id, False]],
                    ["active", "in", [True, False]],
                ],
                fields=PARTNER_FIELDS,
                limit=_EXACT_VAT_LOOKUP_LIMIT,
            )
        )
        return tuple(_record(record) for record in records)

    async def read_partner(self, partner_id: int) -> SupplierPartnerRecord:
        partner_id = _require_partner_id(partner_id)
        records = await _translate_connector_errors(
            self._client.search_read(
                model=RES_PARTNER_MODEL,
                domain=[["id", "=", partner_id], ["active", "in", [True, False]]],
                fields=PARTNER_FIELDS,
                limit=2,
            )
        )
        if not records:
            raise SupplierPartnerDataIntegrityError("The created supplier partner could not be read back.")
        if len(records) > 1:
            raise SupplierPartnerDataIntegrityError("Supplier partner read-back returned more than one record.")
        return _record(records[0])

    async def create_supplier_partner(self, *, name: str, normalized_vat: str) -> int:
        payload = {
            "name": _require_name(name),
            "vat": _require_normalized_vat(normalized_vat),
            "company_type": SUPPLIER_COMPANY_TYPE,
        }
        _reject_forbidden_tokens(payload)
        created = await _translate_connector_errors(self._client.create_res_partner(payload))
        return _require_partner_id(created, source="Odoo res.partner create")


class OdooSupplierPartnerWriter(SupplierPartnerWriter):
    """Protected, read-before-write, VAT-idempotent Odoo supplier partner creator."""

    def __init__(
        self,
        *,
        repository: OdooSupplierPartnerRepository,
        policy: OdooSupplierPartnerWritePolicy,
    ) -> None:
        self._repository = repository
        self._policy = policy

    async def create_supplier(self, command: CreateSupplierPartnerCommand) -> SupplierPartnerWriteResult:
        if not isinstance(command, CreateSupplierPartnerCommand):
            raise SupplierPartnerWriteValidationError("A canonical CreateSupplierPartnerCommand is required.")

        normalized_vat = _normalize_supplier_vat(command.supplier_tax_number)
        supplier_name = command.supplier_name.strip()

        # Dedicated master-data write gate. With default settings this raises before any Odoo call.
        self._policy.ensure_real_write_allowed(approved_by=command.approved_by)

        # Read before write, always.
        existing = await self._repository.find_by_exact_vat(
            normalized_vat=normalized_vat,
            company_id=command.company_id,
        )
        if len(existing) > 1:
            raise SupplierPartnerAmbiguityError("Multiple Odoo supplier partners share this exact tax number.")
        if len(existing) == 1:
            return self._already_exists_result(existing[0], command=command, normalized_vat=normalized_vat)

        created_id = await self._repository.create_supplier_partner(
            name=supplier_name,
            normalized_vat=normalized_vat,
        )

        # Post-create exact-VAT re-query: detect a create race and read the record back.
        rechecked = await self._repository.find_by_exact_vat(
            normalized_vat=normalized_vat,
            company_id=command.company_id,
        )
        if len(rechecked) > 1:
            raise SupplierPartnerDuplicateRaceError(
                "A concurrent create produced more than one Odoo supplier partner for this tax number."
            )
        if len(rechecked) != 1 or rechecked[0].id != created_id:
            raise SupplierPartnerDataIntegrityError(
                "Post-create verification did not resolve to exactly the created supplier partner."
            )
        _validate_created_partner(rechecked[0], created_id=created_id, normalized_vat=normalized_vat, command=command)

        return SupplierPartnerWriteResult(
            status=SupplierPartnerWriteStatus.CREATED,
            partner_id=created_id,
            company_id=command.company_id,
            supplier_name=command.supplier_name,
            supplier_tax_number=normalized_vat,
            idempotency_key=command.idempotency_key,
            safe_message="Supplier partner created in Odoo.",
        )

    def _already_exists_result(
        self,
        existing: SupplierPartnerRecord,
        *,
        command: CreateSupplierPartnerCommand,
        normalized_vat: str,
    ) -> SupplierPartnerWriteResult:
        _validate_existing_partner(existing, normalized_vat=normalized_vat, command=command)
        if not existing.active:
            raise SupplierPartnerInactiveError(
                "The existing Odoo supplier partner for this tax number is archived; resolve it manually."
            )
        name_mismatch = _names_differ(existing.name, command.supplier_name)
        warnings: tuple[str, ...] = ()
        if name_mismatch:
            warnings = ("Supplier legal name on file differs from the invoice supplier name; not changed.",)
        return SupplierPartnerWriteResult(
            status=SupplierPartnerWriteStatus.ALREADY_EXISTS,
            partner_id=existing.id,
            company_id=command.company_id,
            supplier_name=command.supplier_name,
            supplier_tax_number=normalized_vat,
            idempotency_key=command.idempotency_key,
            existing_by="vat",
            name_mismatch=name_mismatch,
            safe_message="An Odoo supplier partner already exists for this tax number.",
            warnings=warnings,
        )


def _normalize_supplier_vat(value: object) -> str:
    """Reuse the exact normalization PartnerMatchingEngine applies: whitespace strip only.

    No prefix stripping and no digit extraction -- ``0430367181`` is compared verbatim,
    matching the deterministic read-only matcher's semantics.
    """

    if not isinstance(value, str):
        raise SupplierPartnerWriteValidationError("supplier_tax_number must be text.")
    normalized = value.strip()
    if not normalized:
        raise SupplierPartnerWriteValidationError("supplier_tax_number is required.")
    return normalized


def _validate_existing_partner(
    partner: SupplierPartnerRecord,
    *,
    normalized_vat: str,
    command: CreateSupplierPartnerCommand,
) -> None:
    if type(partner.id) is not int or partner.id <= 0:
        raise SupplierPartnerDataIntegrityError("Odoo returned an invalid supplier partner id.")
    if _normalize_supplier_vat(partner.vat or "") != normalized_vat:
        raise SupplierPartnerDataIntegrityError("Odoo supplier partner tax number does not match the exact query.")
    if partner.company_id not in (None, command.company_id):
        raise SupplierPartnerDataIntegrityError("Odoo supplier partner is scoped to a different company.")


def _validate_created_partner(
    partner: SupplierPartnerRecord,
    *,
    created_id: int,
    normalized_vat: str,
    command: CreateSupplierPartnerCommand,
) -> None:
    if partner.id != created_id:
        raise SupplierPartnerDataIntegrityError("Read-back resolved to a different supplier partner id.")
    if _normalize_supplier_vat(partner.vat or "") != normalized_vat:
        raise SupplierPartnerDataIntegrityError("Read-back supplier partner tax number does not match the request.")
    if not (partner.name or "").strip():
        raise SupplierPartnerDataIntegrityError("Read-back supplier partner has no name.")
    if partner.company_id not in (None, command.company_id):
        raise SupplierPartnerDataIntegrityError("Created supplier partner is scoped to an unexpected company.")


def _names_differ(existing_name: str | None, requested_name: str) -> bool:
    return _folded(existing_name) != _folded(requested_name)


def _folded(value: str | None) -> str:
    return " ".join((value or "").split()).casefold()


def _record(record: dict[str, Any]) -> SupplierPartnerRecord:
    raw_id = record.get("id")
    if type(raw_id) is not int or isinstance(raw_id, bool) or raw_id <= 0:
        raise SupplierPartnerDataIntegrityError("Odoo returned an invalid res.partner id.")
    return SupplierPartnerRecord(
        id=raw_id,
        name=_optional_text(record.get("name")),
        vat=_optional_text(record.get("vat")),
        active=bool(record.get("active", True)),
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
    for token in FORBIDDEN_RES_PARTNER_TOKENS:
        if token in payload_text:
            raise SupplierPartnerWriteValidationError("Supplier partner payload contains a forbidden field.")


async def _translate_connector_errors[T](awaitable: Any) -> T:
    try:
        return await awaitable
    except ConnectorAuthenticationError as exc:
        raise SupplierPartnerWriteAuthenticationError(exc.safe_message) from exc
    except ConnectorAuthorizationError as exc:
        raise SupplierPartnerWriteAuthorizationError(exc.safe_message) from exc
    except ConnectorValidationError as exc:
        raise SupplierPartnerWriteValidationError(exc.safe_message) from exc
    except ConnectorTimeoutError as exc:
        raise SupplierPartnerWriteTransportError(exc.safe_message) from exc
    except ConnectorError as exc:
        raise SupplierPartnerWriteUnexpectedErpError(exc.safe_message) from exc
    except Exception as exc:  # noqa: BLE001 - translated to a safe supplier partner write error
        raise SupplierPartnerWriteUnexpectedErpError("Odoo supplier partner write failed unexpectedly.") from exc


def _require_name(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SupplierPartnerWriteValidationError("supplier_name is required.")
    return value.strip()


def _require_normalized_vat(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SupplierPartnerWriteValidationError("A normalized supplier tax number is required.")
    return value.strip()


def _require_company_id(value: object) -> int:
    if type(value) is not int or isinstance(value, bool) or value <= 0:
        raise SupplierPartnerWriteValidationError("A positive company_id is required.")
    return value


def _require_partner_id(value: object, *, source: str = "Odoo res.partner") -> int:
    if type(value) is not int or isinstance(value, bool) or value <= 0:
        raise SupplierPartnerDataIntegrityError(f"{source} returned an invalid partner id.")
    return value


def _ensure_named_approver(approved_by: str | None) -> None:
    if approved_by is None or not approved_by.strip():
        raise SupplierPartnerWriteSafetyGateError("A named approver is required for supplier partner creation.")


def _normalized_host(value: object) -> str:
    return (urlparse(str(value)).hostname or "").lower()


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
