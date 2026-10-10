"""Shared httpx client: timeouts, retries with exponential backoff, Retry-After
handling for 429/418/5xx, integration with rate limiters and the raw disk cache.

ApiClient is safe to share between worker threads (httpx.Client is thread-safe; counters
and the call budget are guarded by a lock).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Mapping
from types import TracebackType
from typing import Any, Self

import httpx

from atl_scanner.config import HttpConfig
from atl_scanner.sources.cache import Params, RawCache
from atl_scanner.sources.ratelimit import Sleep, SlidingWindowLimiter

logger = logging.getLogger(__name__)

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
STATUS_HINTS = {
    401: "check the API key in .env",
    403: "check the API key in .env",
    418: "IP is banned for ignoring rate limits; wait for Retry-After before any new request",
    451: "region is blocked; set another base_url in config",
}


class ApiError(Exception):
    """Request failed permanently (non-retryable status or retries exhausted)."""

    def __init__(self, source: str, url: str, status: int | None, detail: str) -> None:
        self.source = source
        self.url = url
        self.status = status
        hint = STATUS_HINTS.get(status or 0)
        message = f"{source}: {status or 'network error'} for {url}: {detail}"
        super().__init__(f"{message} ({hint})" if hint else message)


class RateLimitBanError(ApiError):
    """Server banned the client (HTTP 418); never retried automatically."""


class CallBudgetExceeded(Exception):
    """The per-run cap on network calls for a source was reached."""


class ApiClient:
    """GET-JSON client for one source: cache first, then rate-limited, retried requests."""

    def __init__(
        self,
        *,
        source: str,
        base_url: str,
        http_config: HttpConfig,
        limiter: SlidingWindowLimiter,
        cache: RawCache,
        headers: Mapping[str, str] | None = None,
        max_calls: int | None = None,
        used_weight_header: str | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Sleep = time.sleep,
    ) -> None:
        self.source = source
        self._config = http_config
        self._limiter = limiter
        self._cache = cache
        self._max_calls = max_calls
        self._used_weight_header = used_weight_header
        self._sleep = sleep
        self._client = httpx.Client(
            base_url=base_url,
            headers=dict(headers or {}),
            timeout=http_config.timeout_s,
            transport=transport,
        )
        self.network_calls = 0
        self.cache_hits = 0
        self.cost_used = 0
        self._lock = threading.Lock()

    def get_json(
        self,
        endpoint: str,
        params: Params | None = None,
        *,
        cost: int = 1,
        ttl_hours: float | None = None,
    ) -> Any:
        """GET `endpoint` and return parsed JSON; the raw response is always stored.

        `ttl_hours=None` skips the cache lookup (e.g. the newest, still-changing candles).
        """
        query = dict(params or {})
        url = f"{str(self._client.base_url).rstrip('/')}{endpoint}"
        key = RawCache.request_key(url, query)
        if ttl_hours is not None:
            cached = self._cache.load_fresh(self.source, endpoint, key, ttl_hours)
            if cached is not None:
                with self._lock:
                    self.cache_hits += 1
                return cached.body
        response = self._request_with_retries(endpoint, query, url, cost)
        try:
            body = response.json()
        except ValueError as exc:
            raise ApiError(self.source, url, response.status_code, "invalid JSON") from exc
        self._cache.save(
            self.source,
            endpoint,
            key,
            url=url,
            params=query,
            status=response.status_code,
            body=body,
        )
        return body

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def _request_with_retries(
        self, endpoint: str, params: Params, url: str, cost: int
    ) -> httpx.Response:
        max_retries = self._config.max_retries
        for attempt in range(max_retries + 1):
            response = self._send(endpoint, params, url, cost, is_last=attempt == max_retries)
            if response is None:
                self._wait(self._backoff(attempt), url, "network error")
                continue
            if response.is_success:
                return response
            if response.status_code == 418:
                raise RateLimitBanError(self.source, url, 418, _snippet(response))
            if response.status_code in RETRYABLE_STATUS and attempt < max_retries:
                delay = _retry_after(response)
                self._wait(self._backoff(attempt) if delay is None else delay, url, response)
                continue
            raise ApiError(self.source, url, response.status_code, _snippet(response))
        raise AssertionError("unreachable: last attempt always returns or raises")

    def _send(
        self, endpoint: str, params: Params, url: str, cost: int, *, is_last: bool
    ) -> httpx.Response | None:
        """One network attempt; None means a transport error worth retrying."""
        with self._lock:
            if self._max_calls is not None and self.network_calls >= self._max_calls:
                raise CallBudgetExceeded(
                    f"{self.source}: max {self._max_calls} calls per run reached"
                )
            self.network_calls += 1
            self.cost_used += cost
        self._limiter.acquire(cost)
        try:
            response = self._client.get(endpoint, params=params)
        except httpx.TransportError as exc:
            if is_last:
                raise ApiError(self.source, url, None, str(exc)) from exc
            return None
        self._sync_server_usage(response)
        return response

    def _sync_server_usage(self, response: httpx.Response) -> None:
        if self._used_weight_header is None:
            return
        value = response.headers.get(self._used_weight_header)
        if value is not None and value.isdigit():
            self._limiter.sync_server_usage(int(value))

    def _backoff(self, attempt: int) -> float:
        cfg = self._config
        return float(min(cfg.backoff_max_s, cfg.backoff_base_s * 2**attempt))

    def _wait(self, delay: float, url: str, reason: httpx.Response | str) -> None:
        status = reason if isinstance(reason, str) else reason.status_code
        logger.warning("%s: %s for %s, retrying in %.1fs", self.source, status, url, delay)
        self._sleep(delay)


def _retry_after(response: httpx.Response) -> float | None:
    """Retry-After in seconds if the server sent a numeric value."""
    value = response.headers.get("Retry-After", "")
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def _snippet(response: httpx.Response, limit: int = 300) -> str:
    return response.text[:limit]
