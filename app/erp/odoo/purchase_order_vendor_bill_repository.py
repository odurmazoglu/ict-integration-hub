from __future__ import annotations

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

    async def find_existing_vendor_bill(
        self,
        *,
        company_id: int,
        partner_id: int,
        idempotency_key: str,
    ) -> AccountMoveDraft | None:
        _validate_company_id(company_id)
        _validate_partner_id(partner_id)
        _validate_idempotency_key(idempotency_key)
        records = await _translate_connector_errors(
            self._client.search_read(
                model="account.move",
                domain=[
                    ["move_type", "=", "in_invoice"],
                    ["invoice_origin", "=", idempotency_key],
                    ["company_id", "=", company_id],
                    ["partner_id", "=", partner_id],
                ],
                fields=["id", "name", "move_type", "invoice_origin", "company_id", "partner_id"],
                limit=2,
            )
        )
        if not records:
            return None
        if len(records) > 1:
            raise VendorBillWriteDuplicateError("Multiple existing Odoo Vendor Bills were found.")
        move_id = records[0].get("id")
        if not isinstance(move_id, int) or isinstance(move_id, bool):
            raise VendorBillWriteUnexpectedErpError("Odoo returned an invalid account.move id.")
        return AccountMoveDraft(id=move_id, name=_optional_text(records[0].get("name")))

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
        _validate_company_id(company_id)
        _validate_partner_id(partner_id)
        _validate_purchase_order_id(purchase_order_id)
        _validate_idempotency_key(idempotency_key)
        existing = await self.find_existing_vendor_bill(
            company_id=company_id,
            partner_id=partner_id,
            idempotency_key=idempotency_key,
        )
        if existing is not None:
            return existing

        result = await _translate_connector_errors(
            self._client.call_model_method(
                model="purchase.order",
                method="action_create_invoice",
                ids=[purchase_order_id],
            )
        )
        move_id = _extract_move_id(result)
        values: dict[str, Any] = {"invoice_origin": idempotency_key}
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
        return AccountMoveDraft(id=move_id)


def _extract_move_id(result: Any) -> int:
    if isinstance(result, int) and not isinstance(result, bool):
        return result
    if isinstance(result, dict):
        for key in ("res_id", "id"):
            value = result.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
    if isinstance(result, list) and result and isinstance(result[0], int) and not isinstance(result[0], bool):
        return result[0]
    if isinstance(result, dict) and isinstance(result.get("result"), dict):
        return _extract_move_id(result["result"])
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


def _optional_text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None
