from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from app.billing import CustomerInvoice, VendorBill, to_odoo_account_move_payload, to_odoo_customer_invoice_payload
from app.connectors.exceptions import (
    ConnectorAuthenticationError,
    ConnectorAuthorizationError,
    ConnectorError,
    ConnectorTimeoutError,
    ConnectorValidationError,
)
from app.erp.write.exceptions import (
    CustomerInvoiceWriteAuthenticationError,
    CustomerInvoiceWriteAuthorizationError,
    CustomerInvoiceWriteDuplicateError,
    CustomerInvoiceWriteTransportError,
    CustomerInvoiceWriteUnexpectedErpError,
    CustomerInvoiceWriteValidationError,
    VendorBillWriteAuthenticationError,
    VendorBillWriteAuthorizationError,
    VendorBillWriteDuplicateError,
    VendorBillWriteTransportError,
    VendorBillWriteUnexpectedErpError,
    VendorBillWriteValidationError,
)

ACCOUNT_MOVE_MODEL = "account.move"
VENDOR_BILL_MOVE_TYPE = "in_invoice"
CUSTOMER_INVOICE_MOVE_TYPE = "out_invoice"
IDEMPOTENCY_FIELD = "invoice_origin"
FORBIDDEN_ACCOUNT_MOVE_FIELDS = frozenset(
    {
        "action_post",
        "account.payment",
        "payment_id",
        "payment_ids",
        "reconciled",
        "unlink",
    }
)


class AccountMoveJson2Client(Protocol):
    async def create_account_move(self, payload: dict[str, Any]) -> int:
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
class AccountMoveDraft:
    id: int
    name: str | None = None


class AccountMoveRepository:
    """Odoo account.move repository for draft account.move writes."""

    def __init__(self, *, client: AccountMoveJson2Client) -> None:
        self._client = client

    async def find_existing_vendor_bill(
        self,
        *,
        vendor_bill: VendorBill,
        idempotency_key: str,
        company_id: int | None = None,
    ) -> AccountMoveDraft | None:
        _validate_idempotency_key(idempotency_key)
        company_id = _validate_company_id(
            company_id if company_id is not None else vendor_bill.company_id,
            "Vendor Bill",
        )
        records = await _translate_connector_errors(
            self._client.search_read(
                model=ACCOUNT_MOVE_MODEL,
                domain=[
                    ["move_type", "=", VENDOR_BILL_MOVE_TYPE],
                    [IDEMPOTENCY_FIELD, "=", idempotency_key],
                    ["company_id", "=", company_id],
                    ["partner_id", "=", vendor_bill.supplier_id],
                ],
                fields=["id", "name", "move_type", IDEMPOTENCY_FIELD, "company_id", "partner_id"],
                limit=2,
            )
        )
        if not records:
            return None
        if len(records) > 1:
            raise VendorBillWriteDuplicateError("Multiple existing Odoo Vendor Bills were found.")
        first = records[0]
        draft_id = first.get("id")
        if not isinstance(draft_id, int) or isinstance(draft_id, bool):
            raise VendorBillWriteUnexpectedErpError("Odoo returned an invalid account.move id.")
        return AccountMoveDraft(id=draft_id, name=_optional_text(first.get("name")))

    async def create_draft_vendor_bill(
        self,
        *,
        vendor_bill: VendorBill,
        idempotency_key: str,
        company_id: int | None = None,
    ) -> AccountMoveDraft:
        payload = self._draft_payload(
            vendor_bill=vendor_bill,
            idempotency_key=idempotency_key,
            company_id=company_id,
        )
        move_id = await _translate_connector_errors(self._client.create_account_move(payload))
        return AccountMoveDraft(id=move_id)

    async def find_existing_customer_invoice(
        self,
        *,
        customer_invoice: CustomerInvoice,
        idempotency_key: str,
    ) -> AccountMoveDraft | None:
        _validate_customer_invoice_idempotency_key(idempotency_key)
        records = await _translate_customer_invoice_connector_errors(
            self._client.search_read(
                model=ACCOUNT_MOVE_MODEL,
                domain=[
                    ["move_type", "=", CUSTOMER_INVOICE_MOVE_TYPE],
                    [IDEMPOTENCY_FIELD, "=", idempotency_key],
                    ["company_id", "=", customer_invoice.company_id],
                    ["partner_id", "=", customer_invoice.customer_id],
                ],
                fields=["id", "name", "move_type", IDEMPOTENCY_FIELD, "company_id", "partner_id"],
                limit=2,
            )
        )
        if not records:
            return None
        if len(records) > 1:
            raise CustomerInvoiceWriteDuplicateError("Multiple existing Odoo Customer Invoices were found.")
        first = records[0]
        draft_id = first.get("id")
        if not isinstance(draft_id, int) or isinstance(draft_id, bool):
            raise CustomerInvoiceWriteUnexpectedErpError("Odoo returned an invalid account.move id.")
        return AccountMoveDraft(id=draft_id, name=_optional_text(first.get("name")))

    async def create_draft_customer_invoice(
        self,
        *,
        customer_invoice: CustomerInvoice,
        idempotency_key: str,
    ) -> AccountMoveDraft:
        payload = self._customer_invoice_draft_payload(
            customer_invoice=customer_invoice,
            idempotency_key=idempotency_key,
        )
        move_id = await _translate_customer_invoice_connector_errors(self._client.create_account_move(payload))
        return AccountMoveDraft(id=move_id)

    def _draft_payload(
        self,
        *,
        vendor_bill: VendorBill,
        idempotency_key: str,
        company_id: int | None = None,
    ) -> dict[str, Any]:
        _validate_idempotency_key(idempotency_key)
        company_id = _validate_company_id(
            company_id if company_id is not None else vendor_bill.company_id,
            "Vendor Bill",
        )
        payload = to_odoo_account_move_payload(vendor_bill)
        payload["company_id"] = company_id
        payload[IDEMPOTENCY_FIELD] = idempotency_key
        _validate_payload(payload)
        return payload

    def _customer_invoice_draft_payload(
        self,
        *,
        customer_invoice: CustomerInvoice,
        idempotency_key: str,
    ) -> dict[str, Any]:
        _validate_customer_invoice_idempotency_key(idempotency_key)
        payload = to_odoo_customer_invoice_payload(customer_invoice)
        payload[IDEMPOTENCY_FIELD] = idempotency_key
        _validate_customer_invoice_payload(payload)
        return payload


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


async def _translate_customer_invoice_connector_errors[T](awaitable: Any) -> T:
    try:
        return await awaitable
    except ConnectorAuthenticationError as exc:
        raise CustomerInvoiceWriteAuthenticationError(exc.safe_message) from exc
    except ConnectorAuthorizationError as exc:
        raise CustomerInvoiceWriteAuthorizationError(exc.safe_message) from exc
    except ConnectorValidationError as exc:
        raise CustomerInvoiceWriteValidationError(exc.safe_message) from exc
    except ConnectorTimeoutError as exc:
        raise CustomerInvoiceWriteTransportError(exc.safe_message) from exc
    except ConnectorError as exc:
        raise CustomerInvoiceWriteUnexpectedErpError(exc.safe_message) from exc
    except Exception as exc:
        raise CustomerInvoiceWriteUnexpectedErpError("Odoo Customer Invoice write failed unexpectedly.") from exc


def _validate_idempotency_key(idempotency_key: str) -> None:
    if not idempotency_key.strip():
        raise VendorBillWriteValidationError("Vendor Bill idempotency key is required.")


def _validate_customer_invoice_idempotency_key(idempotency_key: str) -> None:
    if not idempotency_key.strip():
        raise CustomerInvoiceWriteValidationError("Customer Invoice idempotency key is required.")


def _validate_company_id(company_id: object, label: str) -> int:
    if type(company_id) is not int or company_id <= 0:
        raise VendorBillWriteValidationError(f"{label} company_id is required.")
    return company_id


def _validate_payload(payload: dict[str, Any]) -> None:
    if payload.get("move_type") != VENDOR_BILL_MOVE_TYPE:
        raise VendorBillWriteValidationError("Only draft Vendor Bill account.move payloads are allowed.")
    if type(payload.get("company_id")) is not int or payload["company_id"] <= 0:
        raise VendorBillWriteValidationError("Vendor Bill company_id is required.")
    if not isinstance(payload.get("partner_id"), int):
        raise VendorBillWriteValidationError("Vendor Bill partner_id is required.")
    if not payload.get("ref"):
        raise VendorBillWriteValidationError("Vendor Bill reference is required.")
    if not payload.get("invoice_line_ids"):
        raise VendorBillWriteValidationError("Vendor Bill invoice lines are required.")
    payload_text = str(payload).lower()
    for forbidden in FORBIDDEN_ACCOUNT_MOVE_FIELDS:
        if forbidden in payload_text:
            raise VendorBillWriteValidationError("Vendor Bill payload contains a forbidden write operation.")


def _validate_customer_invoice_payload(payload: dict[str, Any]) -> None:
    if payload.get("move_type") != CUSTOMER_INVOICE_MOVE_TYPE:
        raise CustomerInvoiceWriteValidationError("Only draft Customer Invoice account.move payloads are allowed.")
    if not isinstance(payload.get("company_id"), int):
        raise CustomerInvoiceWriteValidationError("Customer Invoice company_id is required.")
    if not isinstance(payload.get("partner_id"), int):
        raise CustomerInvoiceWriteValidationError("Customer Invoice partner_id is required.")
    if not payload.get("ref"):
        raise CustomerInvoiceWriteValidationError("Customer Invoice reference is required.")
    if not payload.get("invoice_line_ids"):
        raise CustomerInvoiceWriteValidationError("Customer Invoice invoice lines are required.")
    payload_text = str(payload).lower()
    for forbidden in FORBIDDEN_ACCOUNT_MOVE_FIELDS:
        if forbidden in payload_text:
            raise CustomerInvoiceWriteValidationError("Customer Invoice payload contains a forbidden write operation.")


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
