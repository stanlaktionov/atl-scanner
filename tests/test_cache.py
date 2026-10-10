"""Tests for sources/cache.py: layout, TTL, immutability."""

from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from atl_scanner.sources.cache import RawCache

T0 = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
URL = "https://api.example.com/api/v3/coins/markets"


class FakeNow:
    def __init__(self) -> None:
        self.value = T0

    def __call__(self) -> datetime:
        return self.value


def save(cache: RawCache, key: str, body: object) -> Path:
    return cache.save(
        "coingecko", "/coins/markets", key, url=URL, params={"page": 1}, status=200, body=body
    )


def test_layout_and_record(tmp_path: Path) -> None:
    cache = RawCache(tmp_path, now=FakeNow())
    key = RawCache.request_key(URL, {"page": 1})
    path = save(cache, key, [{"id": "abc"}])
    assert path.relative_to(tmp_path).parts[:3] == ("coingecko", "coins_markets", "2026-10-10")
    assert path.name.startswith(key) and path.name.endswith(".json.gz")
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        record = json.load(fh)
    assert record["url"] == URL
    assert record["params"] == {"page": "1"}
    assert record["body"] == [{"id": "abc"}]


def test_request_key_depends_on_params_not_order() -> None:
    assert RawCache.request_key(URL, {"a": 1, "b": 2}) == RawCache.request_key(
        URL, {"b": 2, "a": 1}
    )
    assert RawCache.request_key(URL, {"page": 1}) != RawCache.request_key(URL, {"page": 2})


def test_ttl(tmp_path: Path) -> None:
    now = FakeNow()
    cache = RawCache(tmp_path, now=now)
    save(cache, "k", {"v": 1})
    now.value = T0 + timedelta(minutes=59)
    hit = cache.load_fresh("coingecko", "/coins/markets", "k", ttl_hours=1)
    assert hit is not None and hit.body == {"v": 1}
    now.value = T0 + timedelta(minutes=61)
    assert cache.load_fresh("coingecko", "/coins/markets", "k", ttl_hours=1) is None


def test_miss_for_unknown_key(tmp_path: Path) -> None:
    cache = RawCache(tmp_path, now=FakeNow())
    save(cache, "k", {"v": 1})
    assert cache.load_fresh("coingecko", "/coins/markets", "other", ttl_hours=1) is None


def test_same_timestamp_does_not_overwrite(tmp_path: Path) -> None:
    cache = RawCache(tmp_path, now=FakeNow())  # frozen clock
    first = save(cache, "k", {"v": 1})
    second = save(cache, "k", {"v": 2})
    assert first != second and first.exists()
    hit = cache.load_fresh("coingecko", "/coins/markets", "k", ttl_hours=1)
    assert hit is not None and hit.body == {"v": 2}


def test_files_are_never_overwritten_and_newest_wins(tmp_path: Path) -> None:
    now = FakeNow()
    cache = RawCache(tmp_path, now=now)
    first = save(cache, "k", {"v": 1})
    now.value = T0 + timedelta(days=1)  # also crosses into a new date directory
    second = save(cache, "k", {"v": 2})
    assert first != second and first.exists() and second.exists()
    hit = cache.load_fresh("coingecko", "/coins/markets", "k", ttl_hours=1)
    assert hit is not None and hit.body == {"v": 2}


def test_corrupt_file_is_a_miss(tmp_path: Path) -> None:
    cache = RawCache(tmp_path, now=FakeNow())
    path = save(cache, "k", {"v": 1})
    path.write_bytes(b"not gzip")
    assert cache.load_fresh("coingecko", "/coins/markets", "k", ttl_hours=1) is None
