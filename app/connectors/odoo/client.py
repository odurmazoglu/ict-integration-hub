from typing import Any

import httpx
from fastapi import HTTPException, status

from app.core.config import Settings
from app.schemas.odoo import OdooProbeResponse


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
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Odoo probe did not return company information.",
            )
        company = result[0]
        return OdooProbeResponse(
            status="ok",
            company_id=int(company["id"]),
            company_name=str(company["name"]),
        )

    async def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
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
            raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail="Odoo request timed out.") from exc
        except httpx.HTTPStatusError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Odoo returned HTTP {exc.response.status_code}.",
            ) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Odoo request failed.") from exc

        return response.json()
