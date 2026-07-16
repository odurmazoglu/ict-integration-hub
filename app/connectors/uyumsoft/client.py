from datetime import datetime
from typing import Any

from fastapi import HTTPException, status
from pydantic import SecretStr
from zeep import Client
from zeep.exceptions import Error as ZeepError
from zeep.transports import Transport

from app.core.config import Settings
from app.schemas.uyumsoft import (
    UyumsoftIdentityResponse,
    UyumsoftOperationsResponse,
    UyumsoftSystemDateResponse,
    UyumsoftTestConnectionResponse,
)

READ_ONLY_OPERATIONS = frozenset({"TestConnection", "WhoAmI", "GetSystemDate"})


class UyumsoftSoapClient:
    def __init__(
        self,
        *,
        wsdl_url: str,
        username: str,
        password: SecretStr,
        timeout_seconds: float,
        zeep_client: Client | None = None,
    ) -> None:
        self._wsdl_url = wsdl_url
        self._username = username
        self._password = password
        self._timeout_seconds = timeout_seconds
        self._client = zeep_client

    @classmethod
    def from_settings(cls, settings: Settings) -> "UyumsoftSoapClient":
        return cls(
            wsdl_url=settings.uyumsoft_wsdl_url,
            username=settings.uyumsoft_username,
            password=settings.uyumsoft_password,
            timeout_seconds=settings.uyumsoft_timeout_seconds,
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

    def _call(self, operation: str) -> Any:
        if operation not in READ_ONLY_OPERATIONS:
            raise ValueError(f"Operation {operation} is not allowed.")
        try:
            service = self._get_client().service
            return getattr(service, operation)(self._username, self._password.get_secret_value())
        except ZeepError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Uyumsoft SOAP error: {exc}") from exc
        except Exception as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Uyumsoft request failed.") from exc

    def _get_client(self) -> Client:
        if self._client is None:
            self._client = Client(
                wsdl=self._wsdl_url,
                transport=Transport(timeout=self._timeout_seconds, operation_timeout=self._timeout_seconds),
            )
        return self._client

    @staticmethod
    def _safe_mapping(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if hasattr(value, "__dict__"):
            return {key: item for key, item in vars(value).items() if not key.startswith("_")}
        return {"value": str(value)}
