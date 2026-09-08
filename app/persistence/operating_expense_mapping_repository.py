from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.application.expense_mapping.contracts import OperatingExpenseMapping
from app.application.expense_mapping.exceptions import (
    OperatingExpenseMappingDataIntegrityError,
    OperatingExpenseMappingError,
)
from app.models.operating_expense_mapping import OperatingExpenseMappingRecord

SAFE_LOOKUP_ERROR = "Operating expense mapping lookup failed."
SAFE_MULTIPLE_ACTIVE = "More than one enabled operating expense mapping exists for the supplier."


class SqlAlchemyOperatingExpenseMappingRepository:
    """Deterministic read adapter over ``operating_expense_mappings``.

    Lookups are exact on ``company_id`` and ``vendor_partner_id`` and restricted to
    ``enabled`` rows. Supplier VAT/name lookup is out of scope here; partner
    resolution remains ``PartnerMatchingEngine``'s responsibility.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def find_for_supplier(
        self,
        *,
        company_id: int,
        vendor_partner_id: int,
    ) -> OperatingExpenseMapping | None:
        _require_positive(company_id, "company_id must be a positive integer.")
        _require_positive(vendor_partner_id, "vendor_partner_id must be a positive integer.")
        try:
            records = list(
                self._session.scalars(
                    select(OperatingExpenseMappingRecord).where(
                        OperatingExpenseMappingRecord.company_id == company_id,
                        OperatingExpenseMappingRecord.vendor_partner_id == vendor_partner_id,
                        OperatingExpenseMappingRecord.enabled.is_(True),
                    )
                ).all()
            )
        except SQLAlchemyError as exc:
            raise OperatingExpenseMappingError(SAFE_LOOKUP_ERROR) from exc

        if not records:
            return None
        if len(records) > 1:
            raise OperatingExpenseMappingDataIntegrityError(SAFE_MULTIPLE_ACTIVE)
        return _mapping_from_record(records[0])


def _mapping_from_record(record: OperatingExpenseMappingRecord) -> OperatingExpenseMapping:
    return OperatingExpenseMapping(
        id=int(record.id),
        company_id=int(record.company_id),
        vendor_partner_id=int(record.vendor_partner_id),
        expense_account_id=int(record.expense_account_id),
        expense_category=str(record.expense_category),
        enabled=bool(record.enabled),
    )


def _require_positive(value: object, message: str) -> None:
    if type(value) is not int or value <= 0:
        raise OperatingExpenseMappingError(message)
