from typing import Any

import httpx

from app.connectors.exceptions import (
    ConnectorAuthenticationError,
    ConnectorAuthorizationError,
    ConnectorError,
    ConnectorTimeoutError,
    ConnectorValidationError,
)
from app.core.config import Settings
from app.schemas.odoo import OdooProbeResponse

JsonValue = dict[str, Any] | list[Any] | str | int | float | bool | None

READ_ONLY_MODELS = frozenset(
    {
        "account.move",
        "res.company",
        "res.partner",
        "product.product",
        "account.tax",
        "res.currency",
        "account.journal",
    }
)


class OdooJson2Client:
    def __init__(
        self,
        *,
        base_url: str,
        database: str,
        api_key: str,
        timeout_seconds: float,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._database = database
        self._api_key = api_key
        self._timeout = timeout_seconds
        self._http_client = http_client

    @classmethod
    def from_settings(cls, settings: Settings) -> "OdooJson2Client":
        return cls(
            base_url=str(settings.odoo_base_url),
            database=settings.odoo_database,
            api_key=settings.odoo_api_key.get_secret_value(),
            timeout_seconds=settings.odoo_timeout_seconds,
        )

    async def probe(self) -> OdooProbeResponse:
        payload = {
            "domain": [],
            "fields": ["id", "name"],
            "limit": 1,
        }
        result = await self._post_json("/json/2/res.company/search_read", payload)
        if not isinstance(result, list) or not result:
            raise ConnectorError("Odoo probe did not return company information.")
        company = result[0]
        if not isinstance(company, dict):
            raise ConnectorError("Odoo probe returned an unexpected company payload.")
        return OdooProbeResponse(
            status="ok",
            company_id=int(company["id"]),
            company_name=str(company["name"]),
        )

    async def create_account_move(self, payload: dict[str, Any]) -> int:
        result = await self._post_json("/json/2/account.move/create", payload)
        if isinstance(result, int):
            return result
        if isinstance(result, dict) and isinstance(result.get("id"), int):
            return int(result["id"])
        raise ConnectorError("Odoo account.move create returned an unexpected response.")

    async def write_account_move(self, *, record_id: int, values: dict[str, Any]) -> bool:
        if type(record_id) is not int or record_id <= 0:
            raise ConnectorError("Odoo account.move record id is invalid.")
        result = await self._post_json("/json/2/account.move/write", {"ids": [record_id], "values": values})
        if isinstance(result, bool):
            return result
        raise ConnectorError("Odoo account.move write returned an unexpected response.")

    async def call_model_method(
        self,
        *,
        model: str,
        method: str,
        ids: list[int] | None = None,
        args: list[Any] | None = None,
        kwargs: dict[str, Any] | None = None,
    ) -> Any:
        if model not in {"purchase.order", "account.move"}:
            raise ConnectorError("Odoo method call is not allowed for this model.")
        if not isinstance(method, str) or not method.strip():
            raise ConnectorError("Odoo method name is required.")
        payload = {
            "ids": list(ids) if ids is not None else [],
            "args": list(args) if args is not None else [],
            "kwargs": dict(kwargs) if kwargs is not None else {},
        }
        result = await self._post_json(f"/json/2/{model}/{method}", payload)
        return result

    async def create_studio_record(self, *, model: str, values: dict[str, Any]) -> int:
        if not _is_studio_model_allowed(model):
            raise ConnectorError("Odoo Studio write model is not allowed.")
        result = await self._post_json(f"/json/2/{model}/create", values)
        if isinstance(result, int):
            return result
        if isinstance(result, dict) and isinstance(result.get("id"), int):
            return int(result["id"])
        raise ConnectorError("Odoo Studio create returned an unexpected response.")

    async def write_studio_record(self, *, model: str, record_id: int, values: dict[str, Any]) -> bool:
        if not _is_studio_model_allowed(model):
            raise ConnectorError("Odoo Studio write model is not allowed.")
        if type(record_id) is not int or record_id <= 0:
            raise ConnectorError("Odoo Studio record id is invalid.")
        result = await self._post_json(f"/json/2/{model}/write", {"ids": [record_id], "values": values})
        if isinstance(result, bool):
            return result
        raise ConnectorError("Odoo Studio write returned an unexpected response.")

    async def search_read(
        self,
        *,
        model: str,
        domain: list[Any],
        fields: list[str],
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        if not _is_read_only_model_allowed(model):
            raise ConnectorError("Odoo read-only model is not allowed.")
        result = await self._post_json(
            f"/json/2/{model}/search_read",
            {
                "domain": domain,
                "fields": fields,
                "limit": limit,
                "offset": offset,
            },
        )
        if not isinstance(result, list):
            raise ConnectorError("Odoo search_read returned an unexpected response.")
        records: list[dict[str, Any]] = []
        for item in result:
            if not isinstance(item, dict):
                raise ConnectorError("Odoo search_read returned an unexpected record.")
            records.append(item)
        return records

    async def _post_json(self, path: str, payload: dict[str, Any]) -> JsonValue:
        try:
            headers = {
                "Authorization": f"bearer {self._api_key}",
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": "ict-integration-hub",
            }
            if self._database:
                headers["X-Odoo-Database"] = self._database
            if self._http_client is not None:
                response = await self._http_client.post(path, json=payload, headers=headers)
            else:
                async with httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout) as client:
                    response = await client.post(path, json=payload, headers=headers)
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise ConnectorTimeoutError("Odoo request timed out.") from exc
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            if status_code == 401:
                raise ConnectorAuthenticationError("Odoo authentication failed.") from exc
            if status_code == 403:
                raise ConnectorAuthorizationError("Odoo authorization failed.") from exc
            if status_code in {400, 422}:
                raise ConnectorValidationError("Odoo rejected the request payload.") from exc
            raise ConnectorError(f"Odoo returned HTTP {status_code}.") from exc
        except httpx.HTTPError as exc:
            raise ConnectorError("Odoo request failed.") from exc

        return response.json()


def _is_read_only_model_allowed(model: str) -> bool:
    return model in READ_ONLY_MODELS or model.startswith(("x_", "x_studio_"))


def _is_studio_model_allowed(model: str) -> bool:
    return model.startswith(("x_", "x_studio_"))
