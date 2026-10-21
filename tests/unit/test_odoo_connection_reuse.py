"""P0: Odoo connection reuse, Studio selection-metadata cache and read-only retry backoff.

Covers the pooled keep-alive transport (reuse, cleanup, bounds, auth/cookie isolation,
event-loop safety), the TTL cache (hit/miss, expiry, bounds, credential scoping,
Studio-only, failures never cached, concurrency), connect-vs-read timeout
classification, the adapter's retry policy (transient only, exponential backoff with
jitter, bounded) and that writes are never retried. Ends with the request count of
ten Workbench projections before/after, through the real publisher -> adapter ->
client stack against a mock Odoo.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections import Counter
from typing import Any

import httpx
import pytest

from app.application.workbench.dto import ReviewReasonsRole
from app.connectors.exceptions import (
    ConnectorAuthenticationError,
    ConnectorConnectionError,
    ConnectorConnectTimeoutError,
    ConnectorError,
    ConnectorTimeoutError,
    ConnectorValidationError,
)
from app.connectors.odoo import transport as transport_module
from app.connectors.odoo.client import OdooJson2Client
from app.connectors.odoo.transport import (
    SHARED_HTTP_CLIENTS,
    SHARED_SELECTION_CACHE,
    SelectionMetadataCache,
    SharedOdooHttpClients,
    odoo_timeout,
)
from app.erp.exceptions import ErpRepositoryError, ErpRepositoryTimeoutError
from app.erp.odoo.adapter import OdooReadOnlyAdapter
from app.erp.odoo.workbench_projection_publisher import (
    ODOO_BUSINESS_CONTEXT_REQUIRED_BY_CANONICAL,
    ODOO_EXECUTION_STATUS_BY_CANONICAL,
    ODOO_REVIEW_REQUIRED_BY_CANONICAL,
    ODOO_REVIEW_STATUS_BY_CANONICAL,
    ODOO_WORKFLOW_BY_CANONICAL,
    OdooWorkbenchJson2ProjectionAdapter,
    OdooWorkbenchProjectionPublisher,
)
from tests.unit.test_odoo_workbench_projection_publisher import _mapping, _projection

BASE_URL = "https://example.odoo.com"


class _Recorder:
    """Mock Odoo: records every request; ``responses`` maps a JSON-2 path to a reply."""

    def __init__(self, responses: dict[str, Any] | None = None, *, error: Exception | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.responses = responses or {}
        self.error = error

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return httpx.Response(200, json=self.responses.get(request.url.path, []))

    @property
    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]


def _pool(recorder: _Recorder, created: list[httpx.Client] | None = None, **kwargs: Any) -> SharedOdooHttpClients:
    def factory(base_url: str, timeout: httpx.Timeout) -> httpx.Client:
        client = transport_module._new_client(base_url, timeout, transport=httpx.MockTransport(recorder))
        if created is not None:
            created.append(client)
        return client

    return SharedOdooHttpClients(factory=factory, **kwargs)


def _client(
    pool: SharedOdooHttpClients | None,
    *,
    api_key: str = "key-a",
    cache: SelectionMetadataCache | None = None,
) -> OdooJson2Client:
    return OdooJson2Client(
        base_url=BASE_URL,
        database="db",
        api_key=api_key,
        timeout_seconds=10,
        shared_clients=pool,
        selection_cache=cache,
    )


# =========================================================================== connection reuse


def test_production_clients_share_the_process_pool_and_cache() -> None:
    from app.core.config import Settings

    client = OdooJson2Client.from_settings(Settings())
    assert client._shared_clients is SHARED_HTTP_CLIENTS
    assert client._selection_cache is SHARED_SELECTION_CACHE
    assert client._http_client is None


def test_requests_reuse_one_pooled_client() -> None:
    recorder, created = _Recorder(), []
    pool = _pool(recorder, created)
    odoo = _client(pool)

    for _ in range(5):
        asyncio.run(odoo.search_read(model="res.company", domain=[], fields=["id"]))

    assert len(recorder.requests) == 5
    assert len(created) == 1
    assert len(pool) == 1


def test_pooled_client_is_bounded_and_keepalive_limited() -> None:
    client = transport_module._new_client(BASE_URL, odoo_timeout(10))
    try:
        pool = client._transport._pool  # httpcore connection pool
        assert pool._max_connections == transport_module.MAX_CONNECTIONS
        assert pool._max_keepalive_connections == transport_module.MAX_KEEPALIVE_CONNECTIONS
        assert pool._keepalive_expiry == transport_module.KEEPALIVE_EXPIRY_SECONDS
        assert client.follow_redirects is False
    finally:
        client.close()


def test_pool_survives_separate_event_loops_and_an_active_loop() -> None:
    """The adapter runs each call in its own asyncio.run loop (or a worker thread)."""

    recorder, created = _Recorder(), []
    adapter = OdooReadOnlyAdapter(client=_client(_pool(recorder, created)), retry_backoff_seconds=0)

    adapter.search_read(model="res.company", domain=[], fields=["id"])

    async def inside_active_loop() -> None:
        adapter.search_read(model="res.company", domain=[], fields=["id"])

    asyncio.run(inside_active_loop())
    assert len(recorder.requests) == 2
    assert len(created) == 1


def test_close_all_closes_clients_and_a_later_request_reopens() -> None:
    recorder, created = _Recorder(), []
    pool = _pool(recorder, created)
    odoo = _client(pool)
    asyncio.run(odoo.search_read(model="res.company", domain=[], fields=["id"]))

    pool.close_all()

    assert created[0].is_closed
    assert len(pool) == 0
    asyncio.run(odoo.search_read(model="res.company", domain=[], fields=["id"]))
    assert len(created) == 2 and not created[1].is_closed


def test_least_recently_used_client_is_closed_beyond_the_bound() -> None:
    created: list[httpx.Client] = []
    pool = _pool(_Recorder(), created, max_clients=2)
    for index in range(3):
        pool.get(base_url=f"https://odoo-{index}.example", timeout_seconds=10)

    assert len(pool) == 2
    assert created[0].is_closed
    assert not created[1].is_closed and not created[2].is_closed


def test_concurrent_threads_get_one_client() -> None:
    created: list[httpx.Client] = []
    pool = _pool(_Recorder(), created)
    results: list[httpx.Client] = []
    barrier = threading.Barrier(16)

    def worker() -> None:
        barrier.wait()
        results.append(pool.get(base_url=BASE_URL, timeout_seconds=10))

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(created) == 1
    assert {id(client) for client in results} == {id(created[0])}


def test_credentials_are_per_request_and_cookies_are_never_shared() -> None:
    seen: list[tuple[str | None, str | None, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            (
                request.headers.get("authorization"),
                request.headers.get("x-odoo-database"),
                request.headers.get("cookie"),
            )
        )
        return httpx.Response(200, json=[], headers={"set-cookie": "session_id=leak; Path=/"})

    pool = SharedOdooHttpClients(
        factory=lambda base_url, timeout: transport_module._new_client(
            base_url, timeout, transport=httpx.MockTransport(handler)
        )
    )
    asyncio.run(_client(pool, api_key="key-a").search_read(model="res.company", domain=[], fields=["id"]))
    asyncio.run(_client(pool, api_key="key-b").search_read(model="res.company", domain=[], fields=["id"]))

    assert seen == [("bearer key-a", "db", None), ("bearer key-b", "db", None)]
    assert len(pool) == 1


def test_connect_and_read_timeouts_are_separate_and_bounded() -> None:
    timeout = odoo_timeout(10)
    assert (timeout.connect, timeout.read, timeout.write, timeout.pool) == (5.0, 10, 10, 10)
    assert odoo_timeout(3).connect == 3


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (httpx.ConnectTimeout("connect"), ConnectorConnectTimeoutError),
        (httpx.ReadTimeout("read"), ConnectorTimeoutError),
        (httpx.ConnectError("refused"), ConnectorConnectionError),
        (httpx.RemoteProtocolError("reset"), ConnectorError),
    ],
)
def test_transport_failures_are_classified(error: Exception, expected: type[ConnectorError]) -> None:
    odoo = _client(_pool(_Recorder(error=error)))
    with pytest.raises(expected) as exc_info:
        asyncio.run(odoo.search_read(model="res.company", domain=[], fields=["id"]))
    assert type(exc_info.value) is expected
    assert exc_info.value.safe_message in {"Odoo request timed out.", "Odoo request failed."}


def test_injected_async_test_client_path_is_unchanged() -> None:
    recorder = _Recorder()
    odoo = OdooJson2Client(
        base_url=BASE_URL,
        database="db",
        api_key="k",
        timeout_seconds=10,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(recorder), base_url=BASE_URL),
        shared_clients=_pool(_Recorder()),
    )
    asyncio.run(odoo.search_read(model="res.company", domain=[], fields=["id"]))
    assert len(recorder.requests) == 1


# =========================================================================== writes: never retried


@pytest.mark.parametrize(
    "error", [httpx.ConnectError("refused"), httpx.ReadTimeout("read")], ids=["connection", "uncertain"]
)
def test_vendor_bill_create_is_sent_once_and_never_retried(error: Exception) -> None:
    recorder = _Recorder(error=error)
    odoo = _client(_pool(recorder))

    with pytest.raises(ConnectorError):
        asyncio.run(odoo.create_account_move({"move_type": "in_invoice"}))

    assert recorder.paths == ["/json/2/account.move/create"]


def test_vendor_bill_create_through_the_pool_returns_the_id_once() -> None:
    recorder = _Recorder({"/json/2/account.move/create": 901})
    odoo = _client(_pool(recorder))

    assert asyncio.run(odoo.create_account_move({"move_type": "in_invoice"})) == 901
    assert recorder.paths == ["/json/2/account.move/create"]
    assert json.loads(recorder.requests[0].content) == {"vals_list": [{"move_type": "in_invoice"}]}


# =========================================================================== selection cache

SELECTION_PATH = "/json/2/ir.model.fields.selection/search_read"


def _selection_odoo(values: list[str], **kwargs: Any) -> tuple[_Recorder, OdooJson2Client]:
    recorder = _Recorder({SELECTION_PATH: [{"value": value} for value in values]})
    return recorder, _client(_pool(recorder), **kwargs)


def test_studio_selection_cache_hit_and_miss() -> None:
    cache = SelectionMetadataCache()
    recorder, odoo = _selection_odoo(["a", "b"], cache=cache)

    first = asyncio.run(odoo.read_field_selection_values(model="x_ipp_import_workbench", field_name="x_status"))
    second = asyncio.run(odoo.read_field_selection_values(model="x_ipp_import_workbench", field_name="x_status"))
    asyncio.run(odoo.read_field_selection_values(model="x_ipp_import_workbench", field_name="x_other"))

    assert first == second == ("a", "b")
    assert recorder.paths.count(SELECTION_PATH) == 2  # x_status once, x_other once


def test_selection_cache_expires_after_ttl() -> None:
    now = [0.0]
    cache = SelectionMetadataCache(ttl_seconds=600, clock=lambda: now[0])
    recorder, odoo = _selection_odoo(["a"], cache=cache)

    def read() -> tuple[str, ...]:
        return asyncio.run(odoo.read_field_selection_values(model="x_ipp_import_workbench", field_name="x_status"))

    read()
    now[0] = 599.9
    read()
    now[0] = 600.0
    read()
    assert recorder.paths.count(SELECTION_PATH) == 2


def test_selection_cache_is_size_bounded() -> None:
    cache = SelectionMetadataCache(max_entries=2)
    for field in ("f1", "f2", "f3"):
        cache.put(
            SelectionMetadataCache.key(base_url="u", database="d", api_key="k", model="x_m", field_name=field), ("v",)
        )
    assert len(cache) == 2
    assert (
        cache.get(SelectionMetadataCache.key(base_url="u", database="d", api_key="k", model="x_m", field_name="f1"))
        is None
    )


def test_selection_cache_is_scoped_to_database_and_credential() -> None:
    cache = SelectionMetadataCache()
    recorder = _Recorder({SELECTION_PATH: [{"value": "a"}]})
    pool = _pool(recorder)
    for api_key in ("key-a", "key-b", "key-a"):
        asyncio.run(
            _client(pool, api_key=api_key, cache=cache).read_field_selection_values(
                model="x_ipp_import_workbench", field_name="x_status"
            )
        )
    assert recorder.paths.count(SELECTION_PATH) == 2
    key = SelectionMetadataCache.key(base_url=BASE_URL, database="db", api_key="key-a", model="x", field_name="f")
    assert "key-a" not in "".join(key)  # only a fingerprint is kept


def test_non_studio_selection_metadata_is_never_cached() -> None:
    """The res.partner classification check guards a partner create: always a live read."""

    cache = SelectionMetadataCache()
    recorder, odoo = _selection_odoo(["vendor"], cache=cache)
    for _ in range(2):
        asyncio.run(odoo.read_field_selection_values(model="res.partner", field_name="x_studio_partner_class"))
    assert recorder.paths.count(SELECTION_PATH) == 2
    assert len(cache) == 0


def test_selection_read_failures_are_never_cached() -> None:
    cache = SelectionMetadataCache()
    recorder = _Recorder(error=httpx.ReadTimeout("slow"))
    odoo = _client(_pool(recorder), cache=cache)

    with pytest.raises(ConnectorTimeoutError):
        asyncio.run(odoo.read_field_selection_values(model="x_ipp_import_workbench", field_name="x_status"))
    assert len(cache) == 0

    recorder.error = None
    recorder.responses[SELECTION_PATH] = [{"value": "a"}]
    assert asyncio.run(odoo.read_field_selection_values(model="x_ipp_import_workbench", field_name="x_status")) == (
        "a",
    )


def test_selection_cache_concurrent_access() -> None:
    cache = SelectionMetadataCache(max_entries=50)
    errors: list[Exception] = []

    def worker(index: int) -> None:
        try:
            for step in range(200):
                key = SelectionMetadataCache.key(
                    base_url="u", database="d", api_key="k", model="x_m", field_name=f"f{(index + step) % 80}"
                )
                cache.put(key, (str(step),))
                cache.get(key)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len(cache) <= 50


# =========================================================================== read retry policy


class _FlakyClient:
    def __init__(self, *errors: Exception) -> None:
        self.errors = list(errors)
        self.calls = 0

    async def search_read(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return [{"id": 1}]


@pytest.fixture()
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    recorded: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr("app.erp.odoo.adapter.asyncio.sleep", fake_sleep)
    return recorded


def test_transient_read_failures_retry_with_exponential_backoff(sleeps: list[float]) -> None:
    client = _FlakyClient(ConnectorConnectTimeoutError("t"), ConnectorConnectionError("c"))
    adapter = OdooReadOnlyAdapter(client=client, retry_backoff_seconds=0.5, jitter=lambda: 1.0)

    assert adapter.search_read(model="res.company", domain=[], fields=["id"]) == ({"id": 1},)
    assert client.calls == 3
    assert sleeps == [0.5, 1.0]


def test_backoff_jitter_and_cap(sleeps: list[float]) -> None:
    client = _FlakyClient(*[ConnectorTimeoutError("t")] * 5)
    adapter = OdooReadOnlyAdapter(client=client, retry_attempts=6, retry_backoff_seconds=0.5, jitter=lambda: 0.0)

    adapter.search_read(model="res.company", domain=[], fields=["id"])

    assert sleeps == [0.25, 0.5, 1.0, 1.0, 1.0]  # half-jitter floor; 2 s cap * 0.5


def test_retries_are_bounded_and_end_in_a_timeout_error(sleeps: list[float]) -> None:
    client = _FlakyClient(*[ConnectorTimeoutError("Odoo request timed out.")] * 5)
    adapter = OdooReadOnlyAdapter(client=client, retry_backoff_seconds=0.5, jitter=lambda: 1.0)

    with pytest.raises(ErpRepositoryTimeoutError):
        adapter.search_read(model="res.company", domain=[], fields=["id"])
    assert client.calls == 3
    assert sum(sleeps) == 1.5


@pytest.mark.parametrize(
    "error",
    [
        ConnectorAuthenticationError("Odoo authentication failed."),
        ConnectorValidationError("Odoo rejected the request payload."),
    ],
)
def test_permanent_failures_are_not_retried(sleeps: list[float], error: ConnectorError) -> None:
    client = _FlakyClient(error)
    adapter = OdooReadOnlyAdapter(client=client)

    with pytest.raises(ErpRepositoryError):
        adapter.search_read(model="res.company", domain=[], fields=["id"])
    assert client.calls == 1
    assert sleeps == []


# =========================================================================== request count before/after


def _all_selection_values() -> list[dict[str, str]]:
    values: set[str] = set()
    for mapping in (
        ODOO_REVIEW_STATUS_BY_CANONICAL,
        ODOO_WORKFLOW_BY_CANONICAL,
        ODOO_REVIEW_REQUIRED_BY_CANONICAL,
        ODOO_BUSINESS_CONTEXT_REQUIRED_BY_CANONICAL,
        ODOO_EXECUTION_STATUS_BY_CANONICAL,
    ):
        values.update(mapping.values())
    return [{"value": value} for value in sorted(values)]


def _ten_projections(*, cache: SelectionMetadataCache | None) -> tuple[Counter[str], int]:
    """Plan (dry-run) ten reviews, each through a fresh publisher like production does."""

    recorder = _Recorder({SELECTION_PATH: _all_selection_values()})
    created: list[httpx.Client] = []
    client = _client(_pool(recorder, created), cache=cache)
    for index in range(10):
        publisher = OdooWorkbenchProjectionPublisher(
            adapter=OdooWorkbenchJson2ProjectionAdapter(client=client), mapping=_mapping()
        )
        publisher.sync_projection(
            _projection(review_id=f"review-{index}", review_reasons_role=ReviewReasonsRole.CURRENT_BLOCKERS),
            apply=False,
        )
    return Counter(path.split("/")[3] for path in recorder.paths), len(created)


def test_request_count_for_ten_projections_before_and_after() -> None:
    before, _ = _ten_projections(cache=None)
    after, pooled_clients = _ten_projections(cache=SelectionMetadataCache())

    per_review_selection_reads = before["ir.model.fields.selection"] // 10
    assert per_review_selection_reads >= 2
    # Selection metadata is read once per field for all ten reviews instead of per review.
    assert after["ir.model.fields.selection"] == per_review_selection_reads
    assert after["x_ipp_import_workbench"] == before["x_ipp_import_workbench"] == 10  # row lookups stay live
    assert sum(after.values()) < sum(before.values())
    # Before: one TCP+TLS connection per request; after: one pooled client for all of them.
    assert pooled_clients == 1
