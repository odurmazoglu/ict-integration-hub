import re
from base64 import b64decode
from datetime import datetime
from time import sleep
from typing import Any

from pydantic import SecretStr
from requests import ConnectionError as RequestsConnectionError
from requests import Timeout as RequestsTimeout
from zeep import Client
from zeep import Settings as ZeepSettings
from zeep.exceptions import Error as ZeepError
from zeep.exceptions import TransportError
from zeep.helpers import serialize_object
from zeep.transports import Transport
from zeep.wsse.username import UsernameToken

from app.connectors.exceptions import ConnectorError, ConnectorTimeoutError
from app.connectors.uyumsoft.invoice_mapping import (
    build_invoice_list_query_model,
    is_unsuccessful_response,
    normalize_invoice_list_response,
    response_message,
)
from app.core.config import Settings
from app.schemas.uyumsoft import (
    UyumsoftIdentityResponse,
    UyumsoftOperationsResponse,
    UyumsoftSystemDateResponse,
    UyumsoftTestConnectionResponse,
)
from app.schemas.uyumsoft_invoices import InvoiceDirection, UyumsoftInvoiceListRequest, UyumsoftInvoiceListResponse

READ_ONLY_OPERATIONS = frozenset(
    {
        "TestConnection",
        "WhoAmI",
        "GetSystemDate",
        "GetInboxInvoiceList",
        "GetOutboxInvoiceList",
        "GetInboxInvoiceData",
        "GetOutboxInvoiceData",
    }
)
SENSITIVE_FAULT_PATTERNS = (
    re.compile(r"(Kullanıcı:\s*)[^,]+", re.IGNORECASE),
    re.compile(r"(Ip:\s*)[0-9a-fA-F:.]+", re.IGNORECASE),
)


class UyumsoftSoapClient:
    def __init__(
        self,
        *,
        wsdl_url: str,
        username: str,
        password: SecretStr,
        timeout_seconds: float,
        retry_attempts: int = 3,
        retry_backoff_seconds: float = 0.2,
        zeep_client: Client | None = None,
    ) -> None:
        self._wsdl_url = wsdl_url
        self._username = username
        self._password = password
        self._timeout_seconds = timeout_seconds
        self._retry_attempts = retry_attempts
        self._retry_backoff_seconds = retry_backoff_seconds
        self._client = zeep_client

    @classmethod
    def from_settings(cls, settings: Settings) -> "UyumsoftSoapClient":
        return cls(
            wsdl_url=settings.uyumsoft_wsdl_url,
            username=settings.uyumsoft_username,
            password=settings.uyumsoft_password,
            timeout_seconds=settings.uyumsoft_timeout_seconds,
            retry_attempts=settings.uyumsoft_retry_attempts,
            retry_backoff_seconds=settings.uyumsoft_retry_backoff_seconds,
        )

    def test_connection(self) -> UyumsoftTestConnectionResponse:
        result = self._call("TestConnection")
        return UyumsoftTestConnectionResponse(status="ok", result=str(result))

    def who_am_i(self) -> UyumsoftIdentityResponse:
        result = self._call("WhoAmI")
        return UyumsoftIdentityResponse(status="ok", identity=self._safe_mapping(result))

    def get_system_date(self) -> UyumsoftSystemDateResponse:
        result = self._call("GetSystemDate")
        if isinstance(result, datetime):
            system_date = result
        else:
            system_date = datetime.fromisoformat(str(result))
        return UyumsoftSystemDateResponse(status="ok", system_date=system_date)

    def inspect_wsdl(self) -> UyumsoftOperationsResponse:
        client = self._get_client()
        operations: list[str] = []
        for service in client.wsdl.services.values():
            for port in service.ports.values():
                operations.extend(sorted(port.binding._operations.keys()))
        return UyumsoftOperationsResponse(
            status="ok",
            wsdl_url=self._wsdl_url,
            operations=sorted(set(operations)),
            read_only_operations=sorted(READ_ONLY_OPERATIONS),
        )

    def list_inbox_invoices(self, request: UyumsoftInvoiceListRequest) -> UyumsoftInvoiceListResponse:
        return self._list_invoices("GetInboxInvoiceList", "Inbox", request)

    def list_outbox_invoices(self, request: UyumsoftInvoiceListRequest) -> UyumsoftInvoiceListResponse:
        return self._list_invoices("GetOutboxInvoiceList", "Outbox", request)

    def download_invoice_ubl_xml(self, *, direction: InvoiceDirection, invoice_id: str) -> bytes:
        if not invoice_id.strip():
            raise ConnectorError("Uyumsoft invoice id is required for document download.")
        operation = "GetInboxInvoiceData" if direction == "Inbox" else "GetOutboxInvoiceData"
        raw_response = self._call_invoice_data(operation, invoice_id)
        if is_unsuccessful_response(raw_response):
            detail = response_message(raw_response) or "Uyumsoft invoice document request was not successful."
            raise ConnectorError(detail)
        return _invoice_data_bytes(raw_response)

    def _call(self, operation: str) -> Any:
        if operation not in READ_ONLY_OPERATIONS:
            raise ValueError(f"Operation {operation} is not allowed.")
        try:
            service = self._get_client().service
            return getattr(service, operation)()
        except ZeepError as exc:
            raise ConnectorError(f"Uyumsoft SOAP error: {_sanitize_fault_message(str(exc))}") from exc
        except Exception as exc:
            raise ConnectorError("Uyumsoft request failed.") from exc

    def _list_invoices(
        self,
        operation: str,
        direction: InvoiceDirection,
        request: UyumsoftInvoiceListRequest,
    ) -> UyumsoftInvoiceListResponse:
        zeep_client = self._get_client()
        query = build_invoice_list_query_model(zeep_client, request, direction=direction)
        raw_response = self._call_invoice_list(operation, query)
        if is_unsuccessful_response(raw_response):
            detail = response_message(raw_response) or "Uyumsoft invoice list request was not successful."
            raise ConnectorError(detail)
        return normalize_invoice_list_response(raw_response, direction=direction, request=request)

    def _call_invoice_list(self, operation: str, query: Any) -> Any:
        if operation not in READ_ONLY_OPERATIONS:
            raise ValueError(f"Operation {operation} is not allowed.")
        attempts = 0
        while True:
            attempts += 1
            try:
                service = self._get_client().service
                return getattr(service, operation)(query)
            except (RequestsConnectionError, RequestsTimeout) as exc:
                if attempts >= self._retry_attempts:
                    self._raise_transport_error(exc)
                self._sleep_before_retry()
            except TransportError as exc:
                if not _is_transient_transport_error(exc) or attempts >= self._retry_attempts:
                    raise ConnectorError("Uyumsoft transport request failed.") from exc
                self._sleep_before_retry()
            except ZeepError as exc:
                raise ConnectorError(f"Uyumsoft SOAP error: {_sanitize_fault_message(str(exc))}") from exc
            except Exception as exc:
                raise ConnectorError("Uyumsoft request failed.") from exc

    def _call_invoice_data(self, operation: str, invoice_id: str) -> Any:
        if operation not in READ_ONLY_OPERATIONS:
            raise ValueError(f"Operation {operation} is not allowed.")
        attempts = 0
        while True:
            attempts += 1
            try:
                service = self._get_client().service
                return getattr(service, operation)(invoice_id)
            except (RequestsConnectionError, RequestsTimeout) as exc:
                if attempts >= self._retry_attempts:
                    self._raise_transport_error(exc)
                self._sleep_before_retry()
            except TransportError as exc:
                if not _is_transient_transport_error(exc) or attempts >= self._retry_attempts:
                    raise ConnectorError("Uyumsoft transport request failed.") from exc
                self._sleep_before_retry()
            except ZeepError as exc:
                raise ConnectorError(f"Uyumsoft SOAP error: {_sanitize_fault_message(str(exc))}") from exc
            except Exception as exc:
                raise ConnectorError("Uyumsoft request failed.") from exc

    def _sleep_before_retry(self) -> None:
        if self._retry_backoff_seconds > 0:
            sleep(self._retry_backoff_seconds)

    def _raise_transport_error(self, exc: RequestsConnectionError | RequestsTimeout) -> None:
        if isinstance(exc, RequestsTimeout):
            raise ConnectorTimeoutError("Uyumsoft request timed out.") from exc
        raise ConnectorError("Uyumsoft transport request failed.") from exc

    def _get_client(self) -> Client:
        if self._client is None:
            self._client = Client(
                wsdl=self._wsdl_url,
                transport=Transport(timeout=self._timeout_seconds, operation_timeout=self._timeout_seconds),
                settings=ZeepSettings(strict=False),
                wsse=UsernameToken(self._username, self._password.get_secret_value(), use_digest=False),
            )
        return self._client

    @staticmethod
    def _safe_mapping(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if hasattr(value, "__dict__"):
            return {key: item for key, item in vars(value).items() if not key.startswith("_")}
        return {"value": str(value)}


def _is_transient_transport_error(exc: TransportError) -> bool:
    status_code = getattr(exc, "status_code", None)
    return isinstance(status_code, int) and status_code >= 500


def _sanitize_fault_message(message: str) -> str:
    sanitized = message
    for pattern in SENSITIVE_FAULT_PATTERNS:
        sanitized = pattern.sub(r"\1<redacted>", sanitized)
    return sanitized


def _invoice_data_bytes(raw_response: Any) -> bytes:
    response = _to_mapping(raw_response)
    value = _to_mapping(response.get("Value"))
    data = value.get("Data")
    if data is None:
        return b""
    if isinstance(data, bytes):
        return data
    if isinstance(data, bytearray):
        return bytes(data)
    if isinstance(data, str):
        try:
            return b64decode(data, validate=True)
        except ValueError as exc:
            raise ConnectorError("Uyumsoft invoice document data is not valid base64.") from exc
    raise ConnectorError("Uyumsoft invoice document data has an unsupported type.")


def _to_mapping(value: Any) -> dict[str, Any]:
    serialized = serialize_object(value)
    if isinstance(serialized, dict):
        return serialized
    if hasattr(serialized, "__dict__"):
        return {key: item for key, item in vars(serialized).items() if not key.startswith("_")}
    return {}
