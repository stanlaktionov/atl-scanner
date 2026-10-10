"""Tests for sources/http.py using httpx.MockTransport (no network)."""

from __future__ import annotations

import gzip
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from atl_scanner.config import HttpConfig
from atl_scanner.sources.cache import RawCache
from atl_scanner.sources.http import (
    ApiClient,
    ApiError,
    CallBudgetExceeded,
    RateLimitBanError,
)
from atl_scanner.sources.ratelimit import SlidingWindowLimiter
from tests.conftest import FakeClock

HTTP = HttpConfig(timeout_s=5, max_retries=3, backoff_base_s=1, backoff_max_s=8)
BASE = "https://api.example.com/api/v3"


def ok(body: object = None) -> httpx.Response:
    return httpx.Response(200, json={"ok": True} if body is None else body)


class Server:
    """Replays queued responses (or exceptions) and records requests."""

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self._queue: Iterator[httpx.Response | Exception] = iter(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = next(self._queue)
        if isinstance(item, Exception):
            raise item
        return item


def make_client(
    server: Server,
    tmp_path: Path,
    clock: FakeClock,
    *,
    capacity: int = 100,
    http_config: HttpConfig = HTTP,
    max_calls: int | None = None,
    used_weight_header: str | None = None,
) -> ApiClient:
    return ApiClient(
        source="test",
        base_url=BASE,
        http_config=http_config,
        limiter=SlidingWindowLimiter(capacity, clock=clock.now, sleep=clock.sleep),
        cache=RawCache(tmp_path),
        headers={"x-api-key": "SECRET"},
        max_calls=max_calls,
        used_weight_header=used_weight_header,
        transport=httpx.MockTransport(server),
        sleep=clock.sleep,
    )


def test_success_sends_params_and_headers(tmp_path: Path, clock: FakeClock) -> None:
    server = Server(ok([1, 2]))
    client = make_client(server, tmp_path, clock)
    assert client.get_json("/coins/markets", {"page": 2}) == [1, 2]
    request = server.requests[0]
    assert str(request.url) == f"{BASE}/coins/markets?page=2"
    assert request.headers["x-api-key"] == "SECRET"


def test_retries_429_honoring_retry_after_then_backoff(tmp_path: Path, clock: FakeClock) -> None:
    server = Server(
        httpx.Response(429, headers={"Retry-After": "5"}),
        httpx.Response(429),
        ok(),
    )
    client = make_client(server, tmp_path, clock)
    assert client.get_json("/x") == {"ok": True}
    assert client.network_calls == 3
    assert clock.sleeps == [5.0, 2.0]  # Retry-After, then backoff_base * 2**1


def test_5xx_exhausts_retries(tmp_path: Path, clock: FakeClock) -> None:
    server = Server(*[httpx.Response(503) for _ in range(4)])
    client = make_client(server, tmp_path, clock)
    with pytest.raises(ApiError) as exc_info:
        client.get_json("/x")
    assert exc_info.value.status == 503
    assert client.network_calls == HTTP.max_retries + 1
    assert clock.sleeps == [1.0, 2.0, 4.0]


def test_backoff_is_capped(tmp_path: Path, clock: FakeClock) -> None:
    config = HttpConfig(timeout_s=5, max_retries=4, backoff_base_s=3, backoff_max_s=8)
    server = Server(*[httpx.Response(500) for _ in range(4)], ok())
    make_client(server, tmp_path, clock, http_config=config).get_json("/x")
    assert clock.sleeps == [3.0, 6.0, 8.0, 8.0]


def test_client_error_is_not_retried(tmp_path: Path, clock: FakeClock) -> None:
    server = Server(httpx.Response(401, text="bad key"))
    client = make_client(server, tmp_path, clock)
    with pytest.raises(ApiError, match="check the API key"):
        client.get_json("/x")
    assert client.network_calls == 1


def test_418_ban_is_not_retried(tmp_path: Path, clock: FakeClock) -> None:
    server = Server(httpx.Response(418, headers={"Retry-After": "120"}))
    client = make_client(server, tmp_path, clock)
    with pytest.raises(RateLimitBanError):
        client.get_json("/x")
    assert client.network_calls == 1 and clock.sleeps == []


def test_transport_error_is_retried(tmp_path: Path, clock: FakeClock) -> None:
    server = Server(httpx.ConnectTimeout("timeout"), ok())
    client = make_client(server, tmp_path, clock)
    assert client.get_json("/x") == {"ok": True}
    assert client.network_calls == 2


def test_transport_error_on_last_attempt_raises_api_error(tmp_path: Path, clock: FakeClock) -> None:
    server = Server(*[httpx.ConnectError("down") for _ in range(4)])
    client = make_client(server, tmp_path, clock)
    with pytest.raises(ApiError, match="network error"):
        client.get_json("/x")


def test_invalid_json(tmp_path: Path, clock: FakeClock) -> None:
    server = Server(httpx.Response(200, text="<html>"))
    with pytest.raises(ApiError, match="invalid JSON"):
        make_client(server, tmp_path, clock).get_json("/x")


def test_cache_hit_skips_network(tmp_path: Path, clock: FakeClock) -> None:
    server = Server(ok({"v": 1}))
    client = make_client(server, tmp_path, clock)
    assert client.get_json("/x", {"a": 1}, ttl_hours=1) == {"v": 1}
    assert client.get_json("/x", {"a": 1}, ttl_hours=1) == {"v": 1}
    assert client.network_calls == 1 and client.cache_hits == 1


def test_no_ttl_always_fetches_and_stores_raw(tmp_path: Path, clock: FakeClock) -> None:
    server = Server(ok({"v": 1}), ok({"v": 2}))
    client = make_client(server, tmp_path, clock)
    assert client.get_json("/x") == {"v": 1}
    assert client.get_json("/x") == {"v": 2}
    raw_files = list(tmp_path.rglob("*.json.gz"))
    assert len(raw_files) == 2
    for path in raw_files:
        assert b"SECRET" not in gzip.decompress(path.read_bytes())


def test_call_budget(tmp_path: Path, clock: FakeClock) -> None:
    server = Server(ok(), ok(), ok())
    client = make_client(server, tmp_path, clock, max_calls=2)
    client.get_json("/x", {"p": 1})
    client.get_json("/x", {"p": 2})
    with pytest.raises(CallBudgetExceeded):
        client.get_json("/x", {"p": 3})
    assert len(server.requests) == 2


def test_cost_and_server_weight_header(tmp_path: Path, clock: FakeClock) -> None:
    clock.t = 10.0
    server = Server(
        httpx.Response(200, json=[], headers={"X-MBX-USED-WEIGHT-1M": "100"}),
        ok(),
    )
    client = make_client(
        server, tmp_path, clock, capacity=100, used_weight_header="X-MBX-USED-WEIGHT-1M"
    )
    client.get_json("/klines", {"p": 1}, cost=2)
    client.get_json("/klines", {"p": 2}, cost=2)  # server says the minute is full
    assert clock.sleeps == [50.0]
    assert client.cost_used == 4
