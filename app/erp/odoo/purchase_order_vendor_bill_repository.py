from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol

from app.connectors.exceptions import (
    ConnectorAuthenticationError,
    ConnectorAuthorizationError,
    ConnectorError,
    ConnectorTimeoutError,
    ConnectorValidationError,
)
from app.erp.write.account_move_repository import AccountMoveDraft
from app.erp.write.exceptions import (
    VendorBillWriteAuthenticationError,
    VendorBillWriteAuthorizationError,
    VendorBillWriteDuplicateError,
    VendorBillWriteTransportError,
    VendorBillWriteUnexpectedErpError,
    VendorBillWriteValidationError,
)

ALLOWED_BILLABLE_PURCHASE_ORDER_STATES = frozenset({"purchase"})


@dataclass(frozen=True, slots=True)
class PurchaseOrderVendorBillWriteResult:
    move: AccountMoveDraft
    created: bool


class PurchaseOrderVendorBillClient(Protocol):
    async def call_model_method(
        self,
        *,
        model: str,
        method: str,
        ids: list[int] | None = None,
        args: list[Any] | None = None,
        kwargs: dict[str, Any] | None = None,
    ) -> Any:
        pass

    async def write_account_move(self, *, record_id: int, values: dict[str, Any]) -> bool:
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


class PurchaseOrderVendorBillRepository:
    """Create or recover a draft Vendor Bill generated from an existing Odoo purchase order."""

    def __init__(self, *, client: PurchaseOrderVendorBillClient) -> None:
        self._client = client

    async def read_billable_purchase_order(
        self,
        *,
        purchase_order_id: int,
        company_id: int,
        partner_id: int,
    ) -> dict[str, Any]:
        _validate_company_id(company_id)
        _validate_partner_id(partner_id)
        _validate_purchase_order_id(purchase_order_id)
        records = await _translate_connector_errors(
            self._client.search_read(
                model="purchase.order",
                domain=[
                    ["id", "=", purchase_order_id],
                    ["company_id", "=", company_id],
                    ["partner_id", "=", partner_id],
                ],
                fields=["id", "name", "state", "company_id", "partner_id"],
                limit=1,
            )
        )
        if not records:
            raise VendorBillWriteValidationError(
                "Purchase Order is missing or does not match the authoritative company and supplier."
            )
        record = records[0]
        actual_company_id = _extract_many2one_id(record.get("company_id"))
        actual_partner_id = _extract_many2one_id(record.get("partner_id"))
        if actual_company_id != company_id:
            raise VendorBillWriteValidationError("Purchase Order company mismatch detected before billing.")
        if actual_partner_id != partner_id:
            raise VendorBillWriteValidationError("Purchase Order supplier mismatch detected before billing.")

        state = record.get("state")
        if state not in ALLOWED_BILLABLE_PURCHASE_ORDER_STATES:
            raise VendorBillWriteValidationError("Purchase Order is not in a billable state for standard Odoo billing.")
        return record

    async def find_existing_vendor_bill(
        self,
        *,
        company_id: int,
        partner_id: int,
        idempotency_key: str,
        purchase_order_id: int | None = None,
    ) -> AccountMoveDraft | None:
        _validate_company_id(company_id)
        _validate_partner_id(partner_id)
        _validate_idempotency_key(idempotency_key)
        domain: list[Any]
        if purchase_order_id is not None:
            _validate_purchase_order_id(purchase_order_id)
            domain = [
                ["move_type", "=", "in_invoice"],
                ["purchase_id", "=", purchase_order_id],
                ["company_id", "=", company_id],
                ["partner_id", "=", partner_id],
            ]
        else:
            domain = [
                ["move_type", "=", "in_invoice"],
                ["invoice_origin", "=", idempotency_key],
                ["company_id", "=", company_id],
                ["partner_id", "=", partner_id],
            ]
        records = await _translate_connector_errors(
            self._client.search_read(
                model="account.move",
                domain=domain,
                fields=[
                    "id",
                    "name",
                    "move_type",
                    "company_id",
                    "partner_id",
                    "purchase_id",
                    "invoice_origin",
                ],
                limit=2,
            )
        )
        if not records:
            return None
        if len(records) > 1:
            raise VendorBillWriteDuplicateError(
                "Multiple existing Odoo Vendor Bills were found for the purchase order."
            )
        move_id = records[0].get("id")
        if not isinstance(move_id, int) or isinstance(move_id, bool):
            raise VendorBillWriteUnexpectedErpError("Odoo returned an invalid account.move id.")
        return AccountMoveDraft(id=move_id, name=_optional_text(records[0].get("name")))

    async def create_or_recover_vendor_bill_from_purchase_order(
        self,
        *,
        purchase_order_id: int,
        company_id: int,
        partner_id: int,
        idempotency_key: str,
        invoice_reference: str | None = None,
        invoice_date: date | None = None,
    ) -> PurchaseOrderVendorBillWriteResult:
        _validate_company_id(company_id)
        _validate_partner_id(partner_id)
        _validate_purchase_order_id(purchase_order_id)
        _validate_idempotency_key(idempotency_key)
        await self.read_billable_purchase_order(
            purchase_order_id=purchase_order_id,
            company_id=company_id,
            partner_id=partner_id,
        )
        existing = await self.find_existing_vendor_bill(
            company_id=company_id,
            partner_id=partner_id,
            idempotency_key=idempotency_key,
            purchase_order_id=purchase_order_id,
        )
        if existing is not None:
            return PurchaseOrderVendorBillWriteResult(move=existing, created=False)

        result = await _translate_connector_errors(
            self._client.call_model_method(
                model="purchase.order",
                method="action_create_invoice",
                ids=[purchase_order_id],
            )
        )
        move_id = _extract_move_id(result)
        values: dict[str, Any] = {}
        if invoice_reference is not None and invoice_reference.strip():
            values["ref"] = invoice_reference.strip()
        if invoice_date is not None:
            values["invoice_date"] = invoice_date.isoformat()
        if values:
            await _translate_connector_errors(
                self._client.write_account_move(
                    record_id=move_id,
                    values=values,
                )
            )
        return PurchaseOrderVendorBillWriteResult(move=AccountMoveDraft(id=move_id), created=True)

    async def create_vendor_bill_from_purchase_order(
        self,
        *,
        purchase_order_id: int,
        company_id: int,
        partner_id: int,
        idempotency_key: str,
        invoice_reference: str | None = None,
        invoice_date: date | None = None,
    ) -> AccountMoveDraft:
        result = await self.create_or_recover_vendor_bill_from_purchase_order(
            purchase_order_id=purchase_order_id,
            company_id=company_id,
            partner_id=partner_id,
            idempotency_key=idempotency_key,
            invoice_reference=invoice_reference,
            invoice_date=invoice_date,
        )
        return result.move


def _extract_move_id(result: Any) -> int:
    if isinstance(result, int) and not isinstance(result, bool):
        return result
    if isinstance(result, dict):
        for key in ("res_id", "id"):
            value = result.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
        if isinstance(result.get("result"), dict):
            return _extract_move_id(result["result"])
        if isinstance(result.get("result"), int) and not isinstance(result.get("result"), bool):
            return int(result["result"])
    if isinstance(result, list) and result and isinstance(result[0], int) and not isinstance(result[0], bool):
        return result[0]
    raise VendorBillWriteUnexpectedErpError(
        "Odoo purchase.order action_create_invoice returned an invalid Vendor Bill id."
    )


async def _translate_connector_errors[T](awaitable: Any) -> T:
    try:
        return await awaitable
    except ConnectorAuthenticationError as exc:
        raise VendorBillWriteAuthenticationError(exc.safe_message) from exc
    except ConnectorAuthorizationError as exc:
        raise VendorBillWriteAuthorizationError(exc.safe_message) from exc
    except ConnectorValidationError as exc:
        raise VendorBillWriteValidationError(exc.safe_message) from exc
    except ConnectorTimeoutError as exc:
        raise VendorBillWriteTransportError(exc.safe_message) from exc
    except ConnectorError as exc:
        raise VendorBillWriteUnexpectedErpError(exc.safe_message) from exc
    except Exception as exc:
        raise VendorBillWriteUnexpectedErpError("Odoo Vendor Bill write failed unexpectedly.") from exc


def _validate_company_id(company_id: object) -> int:
    if type(company_id) is not int or company_id <= 0:
        raise VendorBillWriteValidationError("Vendor Bill company_id is required.")
    return company_id


def _validate_partner_id(partner_id: object) -> int:
    if type(partner_id) is not int or partner_id <= 0:
        raise VendorBillWriteValidationError("Vendor Bill partner_id is required.")
    return partner_id


def _validate_purchase_order_id(purchase_order_id: object) -> int:
    if type(purchase_order_id) is not int or purchase_order_id <= 0:
        raise VendorBillWriteValidationError("Purchase Order id is required.")
    return purchase_order_id


def _validate_idempotency_key(idempotency_key: str) -> None:
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise VendorBillWriteValidationError("Vendor Bill idempotency key is required.")


def _extract_many2one_id(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, (list, tuple)) and len(value) >= 2 and isinstance(value[0], int):
        return value[0]
    return None


def _optional_text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None
