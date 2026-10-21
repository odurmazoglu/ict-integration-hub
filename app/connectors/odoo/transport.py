"""Process-wide Odoo HTTP connection reuse and Studio selection-metadata cache.

Why: every Odoo JSON-2 request used to open its own ``httpx.AsyncClient`` -- a fresh
DNS + TCP + TLS handshake per call. On a lossy path to Odoo SaaS that handshake is
where requests stall (SYN/TLS retransmissions), so a single projection paid it 8-10
times. A keep-alive connection answers the same request in ~0.18 s instead of 0.6-6 s.

Connection reuse
----------------
* One synchronous ``httpx.Client`` per (base URL, timeout) -- thread-safe, so it is
  shared by the API and every worker thread. Callers drive it through
  ``asyncio.to_thread``; it is never bound to an event loop, so the per-call
  ``asyncio.run`` loops of the ERP adapter cannot break it.
* Bounded: at most ``MAX_CONNECTIONS`` sockets per client, idle sockets expire after
  ``KEEPALIVE_EXPIRY_SECONDS``, and at most ``MAX_POOLED_CLIENTS`` clients exist (the
  least recently used one is closed when a new one is needed).
* Authentication isolation: credentials are never stored on the client -- the bearer
  key and database are sent as per-request headers -- and the cookie jar refuses every
  cookie, so no server session state can leak from one credential to another.
* Closed on interpreter exit (``atexit``) and via ``close_shared_odoo_http_clients``.

Selection-metadata cache
------------------------
Only the allowed values of *Odoo Studio* (``x_*``) selection fields are cached: stable
Workbench schema metadata the projection publisher re-validated for every review.
Nothing financial, authorization- or mapping-related is ever cached here, and the
``res.partner`` classification check stays a live read (it guards a partner create).
Entries expire after ``SELECTION_CACHE_TTL_SECONDS``; the cache is size-bounded and
thread-safe; failures are never cached.
"""

from __future__ import annotations

import atexit
import hashlib
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from http.cookiejar import CookieJar, DefaultCookiePolicy

import httpx

#: Connect phase gets its own, shorter bound: a handshake stuck this long on a lossy
#: path is better retried (reads only) than waited out; the total stays the setting.
CONNECT_TIMEOUT_SECONDS = 5.0
MAX_CONNECTIONS = 10
MAX_KEEPALIVE_CONNECTIONS = 5
KEEPALIVE_EXPIRY_SECONDS = 30.0
MAX_POOLED_CLIENTS = 8

SELECTION_CACHE_TTL_SECONDS = 600.0
SELECTION_CACHE_MAX_ENTRIES = 256


def odoo_timeout(timeout_seconds: float) -> httpx.Timeout:
    """Per-request bound: connect <= ``CONNECT_TIMEOUT_SECONDS``; read/write/pool = the setting."""

    return httpx.Timeout(timeout_seconds, connect=min(CONNECT_TIMEOUT_SECONDS, timeout_seconds))


def _no_cookies() -> CookieJar:
    return CookieJar(policy=DefaultCookiePolicy(allowed_domains=[]))


class SharedOdooHttpClients:
    """Bounded, thread-safe registry of pooled synchronous Odoo HTTP clients."""

    def __init__(
        self,
        *,
        max_clients: int = MAX_POOLED_CLIENTS,
        factory: Callable[[str, httpx.Timeout], httpx.Client] | None = None,
    ) -> None:
        self._max_clients = max_clients
        self._factory = factory or _new_client
        self._clients: OrderedDict[tuple[str, float | None, float | None], httpx.Client] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, *, base_url: str, timeout_seconds: float) -> httpx.Client:
        timeout = odoo_timeout(timeout_seconds)
        key = (base_url, timeout.connect, timeout.read)
        evicted: httpx.Client | None = None
        with self._lock:
            client = self._clients.get(key)
            if client is not None and not client.is_closed:
                self._clients.move_to_end(key)
                return client
            client = self._factory(base_url, timeout)
            self._clients[key] = client
            if len(self._clients) > self._max_clients:
                _, evicted = self._clients.popitem(last=False)
        if evicted is not None:
            evicted.close()
        return client

    def close_all(self) -> None:
        with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()
        for client in clients:
            client.close()

    def __len__(self) -> int:
        with self._lock:
            return len(self._clients)


def _new_client(base_url: str, timeout: httpx.Timeout, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(
        base_url=base_url,
        transport=transport,
        timeout=timeout,
        limits=httpx.Limits(
            max_connections=MAX_CONNECTIONS,
            max_keepalive_connections=MAX_KEEPALIVE_CONNECTIONS,
            keepalive_expiry=KEEPALIVE_EXPIRY_SECONDS,
        ),
        cookies=_no_cookies(),
        follow_redirects=False,
    )


SelectionKey = tuple[str, str, str, str, str]


class SelectionMetadataCache:
    """TTL- and size-bounded, thread-safe cache of Studio selection values."""

    def __init__(
        self,
        *,
        ttl_seconds: float = SELECTION_CACHE_TTL_SECONDS,
        max_entries: int = SELECTION_CACHE_MAX_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._clock = clock
        self._entries: OrderedDict[SelectionKey, tuple[float, tuple[str, ...]]] = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def key(*, base_url: str, database: str, api_key: str, model: str, field_name: str) -> SelectionKey:
        """Database, model and field identify the metadata; a credential fingerprint (never
        the key itself) keeps what one credential can see apart from another's."""

        fingerprint = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]
        return (base_url, database, fingerprint, model, field_name)

    def get(self, key: SelectionKey) -> tuple[str, ...] | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            expires_at, values = entry
            if self._clock() >= expires_at:
                del self._entries[key]
                return None
            self._entries.move_to_end(key)
            return values

    def put(self, key: SelectionKey, values: tuple[str, ...]) -> None:
        with self._lock:
            self._entries[key] = (self._clock() + self._ttl, tuple(values))
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


SHARED_HTTP_CLIENTS = SharedOdooHttpClients()
SHARED_SELECTION_CACHE = SelectionMetadataCache()


def close_shared_odoo_http_clients() -> None:
    SHARED_HTTP_CLIENTS.close_all()


atexit.register(close_shared_odoo_http_clients)


__all__ = [
    "CONNECT_TIMEOUT_SECONDS",
    "SHARED_HTTP_CLIENTS",
    "SHARED_SELECTION_CACHE",
    "SelectionMetadataCache",
    "SharedOdooHttpClients",
    "close_shared_odoo_http_clients",
    "odoo_timeout",
]
