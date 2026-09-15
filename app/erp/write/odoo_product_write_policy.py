from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from app.application.exceptions.product_remediation import ProductWriteSafetyGateError
from app.core.config import Settings
from app.core.runtime_checks import APPROVED_STAGING_ODOO_HOSTS, PRODUCTION_APPROVAL_ACK


@dataclass(frozen=True, slots=True)
class OdooProductWritePolicy:
    """Dedicated authorization for the Odoo product remediation master-data writes.

    Governs both the ``product.template`` writer and the ``product.supplierinfo``
    writer -- there is a single gate for "product write infrastructure", mirroring
    the supplier-remediation safety architecture. This gate is independent of
    Vendor Bill / Customer Invoice / Customer Quotation execution and of supplier
    partner remediation: it never reads ``execution_execute_enabled`` or
    ``supplier_remediation_write_enabled``, and never weakens the production
    operation acknowledgements.
    """

    product_remediation_write_enabled: bool = False
    production_operations_enabled: bool = False
    production_approval_ack: str = ""
    required_approval_ack: str = PRODUCTION_APPROVAL_ACK
    app_env: str = "development"
    odoo_host: str = ""
    approved_staging_hosts: frozenset[str] = APPROVED_STAGING_ODOO_HOSTS

    @classmethod
    def from_settings(cls, settings: Settings) -> OdooProductWritePolicy:
        return cls(
            product_remediation_write_enabled=settings.product_remediation_write_enabled,
            production_operations_enabled=settings.production_operations_enabled,
            production_approval_ack=settings.production_approval_ack,
            app_env=settings.app_env,
            odoo_host=_normalized_host(settings.odoo_base_url),
        )

    @property
    def staging_write_sanctioned(self) -> bool:
        return (
            self.app_env != "production"
            and self.product_remediation_write_enabled
            and self.odoo_host in self.approved_staging_hosts
        )

    def ensure_real_write_allowed(self, *, approved_by: str | None) -> None:
        if not self.product_remediation_write_enabled:
            raise ProductWriteSafetyGateError("Product remediation master-data write must be explicitly enabled.")
        if self.staging_write_sanctioned:
            _ensure_named_approver(approved_by)
            return
        if not self.production_operations_enabled:
            raise ProductWriteSafetyGateError("Production operations must be explicitly enabled.")
        if self.production_approval_ack != self.required_approval_ack:
            raise ProductWriteSafetyGateError("Production approval acknowledgement is required.")
        _ensure_named_approver(approved_by)


def _ensure_named_approver(approved_by: str | None) -> None:
    if approved_by is None or not approved_by.strip():
        raise ProductWriteSafetyGateError("A named approver is required for product remediation writes.")


def _normalized_host(value: object) -> str:
    return (urlparse(str(value)).hostname or "").lower()
