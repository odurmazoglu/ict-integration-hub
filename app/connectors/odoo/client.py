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
        "account.move.line",
        "purchase.order",
        "sale.order",
        "product.pricelist",
        "res.company",
        "res.partner",
        "product.product",
        "product.template",
        "product.supplierinfo",
        "account.tax",
        "res.currency",
        "account.journal",
        "account.account",
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
        return await self._create_one(path="/json/2/account.move/create", values=payload, label="account.move")

    async def create_sale_order(self, payload: dict[str, Any]) -> int:
        return await self._create_one(path="/json/2/sale.order/create", values=payload, label="sale.order")

    async def create_res_partner(self, payload: dict[str, Any]) -> int:
        """Create exactly one ``res.partner`` and return its positive integer id.

        This is the single sanctioned ``res.partner`` write route. It uses the
        hardened ``{"vals_list": [values]}`` create shape and accepts only a bare
        int, ``{"id": int}``, or a single-element ``[int]`` response; a boolean or
        any other shape fails closed.
        """

        return await self._create_one(path="/json/2/res.partner/create", values=payload, label="res.partner")

    async def create_product_template(self, payload: dict[str, Any]) -> int:
        """Create exactly one ``product.template`` and return its positive integer id.

        This is the single sanctioned ``product.template`` write route, reserved for
        the gated product remediation writer. It uses the hardened
        ``{"vals_list": [values]}`` create shape and accepts only a bare int,
        ``{"id": int}``, or a single-element ``[int]`` response; a boolean or any
        other shape fails closed.
        """

        return await self._create_one(path="/json/2/product.template/create", values=payload, label="product.template")

    async def create_supplierinfo(self, payload: dict[str, Any]) -> int:
        """Create exactly one ``product.supplierinfo`` and return its positive integer id.

        This is the single sanctioned ``product.supplierinfo`` write route, reserved
        for the gated product remediation writer. Same hardened create/response-shape
        handling as ``create_product_template``.
        """

        return await self._create_one(
            path="/json/2/product.supplierinfo/create", values=payload, label="product.supplierinfo"
        )

    async def write_account_move(self, *, record_id: int, values: dict[str, Any]) -> bool:
        if type(record_id) is not int or record_id <= 0:
            raise ConnectorError("Odoo account.move record id is invalid.")
        result = await self._post_json("/json/2/account.move/write", {"ids": [record_id], "vals": values})
        if isinstance(result, bool):
            return result
        raise ConnectorError("Odoo account.move write returned an unexpected response.")

    async def archive_res_partner(self, *, partner_id: int) -> bool:
        """Set exactly one ``res.partner.active = False``. No other field is ever written.

        The single sanctioned ``res.partner`` write route for ONE_OFF_VENDOR retirement
        (P0-PROD-08H) -- hardcoded to ``active: False`` only, never a caller-supplied
        values dict. No unlink/delete capability exists anywhere on this client.
        """

        if type(partner_id) is not int or partner_id <= 0:
            raise ConnectorError("Odoo res.partner record id is invalid.")
        result = await self._post_json("/json/2/res.partner/write", {"ids": [partner_id], "vals": {"active": False}})
        if isinstance(result, bool):
            return result
        raise ConnectorError("Odoo res.partner archive write returned an unexpected response.")

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
        return await self._create_one(path=f"/json/2/{model}/create", values=values, label="Studio")

    async def write_studio_record(self, *, model: str, record_id: int, values: dict[str, Any]) -> bool:
        if not _is_studio_model_allowed(model):
            raise ConnectorError("Odoo Studio write model is not allowed.")
        if type(record_id) is not int or record_id <= 0:
            raise ConnectorError("Odoo Studio record id is invalid.")
        result = await self._post_json(f"/json/2/{model}/write", {"ids": [record_id], "vals": values})
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

    async def read_model_field_metadata(self, *, model: str, field_name: str) -> list[dict[str, Any]]:
        if not _is_read_only_model_allowed(model):
            raise ConnectorError("Odoo metadata target model is not allowed.")
        if not isinstance(field_name, str) or not field_name.strip():
            raise ConnectorError("Odoo metadata field name is required.")
        result = await self._post_json(
            "/json/2/ir.model.fields/search_read",
            {
                "domain": [["model", "=", model], ["name", "=", field_name]],
                "fields": ["name", "ttype", "relation"],
                "limit": 2,
                "offset": 0,
            },
        )
        if not isinstance(result, list):
            raise ConnectorError("Odoo field metadata search_read returned an unexpected response.")
        records: list[dict[str, Any]] = []
        for item in result:
            if not isinstance(item, dict):
                raise ConnectorError("Odoo field metadata search_read returned an unexpected record.")
            records.append(item)
        return records

    async def _create_one(self, *, path: str, values: dict[str, Any], label: str) -> int:
        """Call the fixed JSON-2 ``create(vals_list)`` shape for one record."""

        if not isinstance(values, dict) or not values:
            raise ConnectorError(f"Odoo {label} create values are invalid.")
        result = await self._post_json(path, {"vals_list": [values]})
        if not isinstance(result, bool):
            if type(result) is int and result > 0:
                return result
            if isinstance(result, dict) and type(result.get("id")) is int and result["id"] > 0:
                return int(result["id"])
            if isinstance(result, list) and len(result) == 1 and type(result[0]) is int and result[0] > 0:
                return result[0]
        raise ConnectorError(f"Odoo {label} create returned an unexpected response shape: {_response_shape(result)}.")

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
            detail = _safe_http_error_detail(exc.response)
            if status_code in {400, 422}:
                message = "Odoo rejected the request payload."
                if detail:
                    message += f" {detail}"
                raise ConnectorValidationError(message) from exc
            message = f"Odoo returned HTTP {status_code}."
            if detail:
                message += f" {detail}"
            raise ConnectorError(message) from exc
        except httpx.HTTPError as exc:
            raise ConnectorError("Odoo request failed.") from exc

        return response.json()


def _is_read_only_model_allowed(model: str) -> bool:
    return model in READ_ONLY_MODELS or model.startswith(("x_", "x_studio_"))


def _is_studio_model_allowed(model: str) -> bool:
    return model.startswith(("x_", "x_studio_"))


def _response_shape(value: JsonValue) -> str:
    if value is None:
        return "type=null"
    if isinstance(value, bool):
        return "type=bool"
    if isinstance(value, int):
        return "type=int"
    if isinstance(value, list):
        element_types = sorted({type(element).__name__ for element in value})
        return f"type=list,length={len(value)},element_types={element_types}"
    if isinstance(value, dict):
        return f"type=dict,keys={sorted(value)}"
    if isinstance(value, str):
        return "type=str"
    return f"type={type(value).__name__}"


MAX_SAFE_HTTP_ERROR_DETAIL_LENGTH = 300
UNSAFE_HTTP_ERROR_MARKERS = (
    "api_key",
    "api key",
    "authorization",
    "bearer",
    "password",
    "passwd",
    "token",
    "secret",
    "cookie",
)


def _safe_http_error_detail(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    candidates: list[Any] = [body.get("message")]
    error = body.get("error")
    if isinstance(error, dict):
        candidates.append(error.get("message"))
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        detail = " ".join(candidate.split())
        lowered_detail = detail.lower()
        if (
            not detail
            or "traceback" in lowered_detail
            or "debug" in lowered_detail
            or any(marker in lowered_detail for marker in UNSAFE_HTTP_ERROR_MARKERS)
        ):
            continue
        return detail[:MAX_SAFE_HTTP_ERROR_DETAIL_LENGTH]
    return None
