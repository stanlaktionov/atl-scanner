"""Disk cache of raw API responses.

Layout: <raw_dir>/<source>/<endpoint>/<YYYY-MM-DD>/<request-key>_<HHMMSSffffff>.json.gz
Every successful response is stored; files are never overwritten. A lookup returns the newest
file for the request key if it is younger than the TTL given by the caller (from config).
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

Params = Mapping[str, str | int | float]


@dataclass(frozen=True)
class CachedResponse:
    fetched_at: datetime
    body: Any
    path: Path


def _utc_now() -> datetime:
    return datetime.now(UTC)


class RawCache:
    """Immutable store of raw JSON responses, doubling as a TTL cache."""

    def __init__(self, raw_dir: Path, *, now: Callable[[], datetime] = _utc_now) -> None:
        self.raw_dir = raw_dir
        self._now = now

    @staticmethod
    def request_key(url: str, params: Params) -> str:
        """Stable key of a request: URL plus sorted params (secrets go in headers, not here)."""
        payload = json.dumps(
            {"url": url, "params": {k: str(v) for k, v in params.items()}}, sort_keys=True
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def load_fresh(
        self, source: str, endpoint: str, key: str, ttl_hours: float
    ) -> CachedResponse | None:
        """Newest stored response for `key` if younger than `ttl_hours`, else None."""
        candidates = sorted(self._endpoint_dir(source, endpoint).glob(f"*/{key}_*.json.gz"))
        if not candidates:
            return None
        path = candidates[-1]
        try:
            record = self._read(path)
        except (OSError, EOFError, ValueError) as exc:
            logger.warning("unreadable cache file %s: %s", path, exc)
            return None
        fetched_at = datetime.fromisoformat(record["fetched_at"])
        if self._now() - fetched_at > timedelta(hours=ttl_hours):
            return None
        return CachedResponse(fetched_at=fetched_at, body=record["body"], path=path)

    def save(
        self,
        source: str,
        endpoint: str,
        key: str,
        *,
        url: str,
        params: Params,
        status: int,
        body: Any,
    ) -> Path:
        """Store a response as a new immutable file and return its path."""
        fetched_at = self._now()
        path = self._file_path(source, endpoint, key, fetched_at)
        while path.exists():  # coarse clocks: keep files unique and name-sorted by time
            fetched_at += timedelta(microseconds=1)
            path = self._file_path(source, endpoint, key, fetched_at)
        record = {
            "fetched_at": fetched_at.isoformat(),
            "source": source,
            "endpoint": endpoint,
            "url": url,
            "params": {k: str(v) for k, v in params.items()},
            "status": status,
            "body": body,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            json.dump(record, fh)
        tmp.replace(path)
        return path

    def _file_path(self, source: str, endpoint: str, key: str, fetched_at: datetime) -> Path:
        return (
            self._endpoint_dir(source, endpoint)
            / f"{fetched_at:%Y-%m-%d}"
            / f"{key}_{fetched_at:%H%M%S%f}.json.gz"
        )

    def _endpoint_dir(self, source: str, endpoint: str) -> Path:
        return self.raw_dir / source / endpoint.strip("/").replace("/", "_")

    @staticmethod
    def _read(path: Path) -> dict[str, Any]:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            record: dict[str, Any] = json.load(fh)
        return record
