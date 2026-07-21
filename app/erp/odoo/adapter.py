from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any, Protocol

from app.connectors.exceptions import ConnectorError, ConnectorTimeoutError
from app.connectors.odoo.client import OdooJson2Client
from app.core.config import Settings
from app.erp.exceptions import ErpReadonlyViolationError, ErpRepositoryError, ErpRepositoryTimeoutError

JsonRecord = dict[str, Any]

READONLY_METHOD = "search_read"
FORBIDDEN_METHODS = frozenset(
    {
        "create",
        "write",
        "unlink",
        "action_post",
        "button_validate",
        "send",
        "cancel",
    }
)


class _SearchReadClient(Protocol):
    async def search_read(
        self,
        *,
        model: str,
        domain: list[Any],
        fields: list[str],
        limit: int = 20,
        offset: int = 0,
    ) -> list[JsonRecord]:
        pass


class OdooReadOnlyAdapter:
    def __init__(
        self,
        *,
        client: _SearchReadClient,
        page_size: int = 80,
        retry_attempts: int = 3,
        retry_backoff_seconds: float = 0.1,
    ) -> None:
        self._client = client
        self._page_size = page_size
        self._retry_attempts = max(1, retry_attempts)
        self._retry_backoff_seconds = max(0, retry_backoff_seconds)

    @classmethod
    def from_settings(cls, settings: Settings) -> OdooReadOnlyAdapter:
        return cls(client=OdooJson2Client.from_settings(settings))

    def search_read(
        self,
        *,
        model: str,
        domain: list[Any],
        fields: list[str],
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[JsonRecord, ...]:
        self._ensure_readonly(method=READONLY_METHOD)
        request_limit = self._page_size if limit is None else limit
        return tuple(
            _run_sync(
                self._search_read_async(
                    model=model,
                    domain=domain,
                    fields=fields,
                    limit=request_limit,
                    offset=offset,
                )
            )
        )

    def search_read_all(
        self,
        *,
        model: str,
        domain: list[Any],
        fields: list[str],
        page_size: int | None = None,
        max_records: int | None = None,
    ) -> tuple[JsonRecord, ...]:
        self._ensure_readonly(method=READONLY_METHOD)
        size = self._page_size if page_size is None else page_size
        records: list[JsonRecord] = []
        offset = 0
        while True:
            remaining = None if max_records is None else max_records - len(records)
            if remaining is not None and remaining <= 0:
                break
            limit = size if remaining is None else min(size, remaining)
            page = self.search_read(model=model, domain=domain, fields=fields, limit=limit, offset=offset)
            records.extend(page)
            if len(page) < limit:
                break
            offset += limit
        return tuple(records)

    async def _search_read_async(
        self,
        *,
        model: str,
        domain: list[Any],
        fields: list[str],
        limit: int,
        offset: int,
    ) -> list[JsonRecord]:
        last_error: ConnectorError | None = None
        for attempt in range(self._retry_attempts):
            try:
                return await self._client.search_read(
                    model=model,
                    domain=domain,
                    fields=fields,
                    limit=limit,
                    offset=offset,
                )
            except ConnectorTimeoutError as exc:
                last_error = exc
                if attempt + 1 >= self._retry_attempts:
                    raise ErpRepositoryTimeoutError(exc.safe_message) from exc
            except ConnectorError as exc:
                last_error = exc
                if attempt + 1 >= self._retry_attempts:
                    raise ErpRepositoryError(exc.safe_message) from exc
            if self._retry_backoff_seconds:
                await asyncio.sleep(self._retry_backoff_seconds)
        raise ErpRepositoryError(last_error.safe_message if last_error else "ERP repository request failed.")

    @staticmethod
    def _ensure_readonly(*, method: str) -> None:
        if method != READONLY_METHOD or method in FORBIDDEN_METHODS:
            raise ErpReadonlyViolationError("ERP adapter only permits read-only search_read operations.")


def _run_sync(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    coro.close()
    raise ErpRepositoryError("ERP adapter cannot run a synchronous request inside an active event loop.")


def many2one_id(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, Sequence) and not isinstance(value, str) and value:
        first = value[0]
        if isinstance(first, int) and not isinstance(first, bool):
            return first
    return None
