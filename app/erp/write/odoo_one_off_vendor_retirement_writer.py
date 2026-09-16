from __future__ import annotations

from typing import Any, Protocol

from app.application.commands.one_off_vendor_retirement import ArchiveOneOffVendorPartnerCommand
from app.application.dto.one_off_vendor_retirement import OneOffVendorArchiveWriteResult, OneOffVendorArchiveWriteStatus
from app.application.exceptions.supplier_partner import (
    SupplierPartnerDataIntegrityError,
    SupplierPartnerWriteAuthenticationError,
    SupplierPartnerWriteAuthorizationError,
    SupplierPartnerWriteTransportError,
    SupplierPartnerWriteUnexpectedErpError,
    SupplierPartnerWriteValidationError,
)
from app.connectors.exceptions import (
    ConnectorAuthenticationError,
    ConnectorAuthorizationError,
    ConnectorError,
    ConnectorTimeoutError,
    ConnectorValidationError,
)
from app.erp.write.odoo_supplier_partner_writer import OdooSupplierPartnerRepository, OdooSupplierPartnerWritePolicy


class ArchiveClient(Protocol):
    async def archive_res_partner(self, *, partner_id: int) -> bool:
        pass


class OdooOneOffVendorRetirementWriter:
    """Protected, read-before-write, idempotent Odoo partner archiver (P0-PROD-08H).

    Reuses the existing ``OdooSupplierPartnerRepository.read_partner`` (already the
    sanctioned archived-inclusive lookup) for the read-back-first idempotency check,
    and the existing ``OdooSupplierPartnerWritePolicy`` gate -- archiving a
    Hub-owned one-off partner is the same class of supplier master-data write as
    creating one, so it is protected by the same authorization, not a broader or
    weaker one.
    """

    def __init__(
        self,
        *,
        repository: OdooSupplierPartnerRepository,
        client: ArchiveClient,
        policy: OdooSupplierPartnerWritePolicy,
    ) -> None:
        self._repository = repository
        self._client = client
        self._policy = policy

    async def archive_partner(self, command: ArchiveOneOffVendorPartnerCommand) -> OneOffVendorArchiveWriteResult:
        if not isinstance(command, ArchiveOneOffVendorPartnerCommand):
            raise SupplierPartnerWriteValidationError("A canonical ArchiveOneOffVendorPartnerCommand is required.")

        # Read-back-first: an already-inactive partner is a no-op success. This is
        # the whole reason archiving needs no NEEDS_RECONCILIATION state for a merely
        # uncertain write outcome -- this check is always available and conclusive.
        existing = await _translate_connector_errors(self._repository.read_partner(command.partner_id))
        if existing.id != command.partner_id:
            raise SupplierPartnerDataIntegrityError("Odoo partner read-back resolved to a different id.")
        if not existing.active:
            return OneOffVendorArchiveWriteResult(
                status=OneOffVendorArchiveWriteStatus.ALREADY_ARCHIVED,
                partner_id=command.partner_id,
                safe_message="The Odoo supplier partner is already inactive.",
            )

        # Dedicated master-data write gate, shared with supplier-partner creation.
        # With default settings this raises before any Odoo call.
        self._policy.ensure_real_write_allowed(approved_by=command.approved_by)

        wrote = await _translate_connector_errors(self._client.archive_res_partner(partner_id=command.partner_id))
        if not wrote:
            raise SupplierPartnerWriteUnexpectedErpError("Odoo res.partner archive write reported failure.")

        # Read back again: the write's own HTTP success does not by itself prove the
        # partner is now inactive -- confirm before claiming ARCHIVED.
        confirmed = await _translate_connector_errors(self._repository.read_partner(command.partner_id))
        if confirmed.active:
            raise SupplierPartnerDataIntegrityError(
                "Odoo res.partner is still active after an archive write reported success."
            )
        return OneOffVendorArchiveWriteResult(
            status=OneOffVendorArchiveWriteStatus.ARCHIVED,
            partner_id=command.partner_id,
            safe_message="Odoo supplier partner archived.",
        )


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
    except SupplierPartnerDataIntegrityError:
        raise
    except Exception as exc:  # noqa: BLE001 - translated to a safe supplier partner write error
        raise SupplierPartnerWriteUnexpectedErpError("Odoo partner archive failed unexpectedly.") from exc
