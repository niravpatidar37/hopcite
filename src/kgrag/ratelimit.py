"""Token-bucket rate limiters for POST /ask. One bucket per client key (a hash of the API key, else
the client address).

- RateLimiter (KGRAG_RATE_LIMIT_BACKEND=memory, local default): in-process. Limits are per process,
  so N instances would allow N x the rate, and a replaced instance starts with a full bucket.
- PostgresRateLimiter (KGRAG_RATE_LIMIT_BACKEND=postgres, required when KGRAG_ENV is staging/prod):
  the bucket lives in the `rate_limit_buckets` table (migration 0003) and is refilled and debited by
  ONE `INSERT ... ON CONFLICT DO UPDATE` statement. Postgres takes the row lock for the upsert, so
  concurrent requests on any number of instances serialize on the bucket row and the limit is global.
  Refill uses the database clock (`statement_timestamp()`), so instance clock skew does not matter.
"""

from __future__ import annotations

import hashlib
import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any, Protocol


class Limiter(Protocol):
    enabled: bool

    def acquire(self, key: str) -> float: ...


class LimiterUnavailable(RuntimeError):
    """The shared limiter could not be reached; the API fails closed (503) rather than unlimited."""


class RateLimiter:
    def __init__(
        self,
        per_minute: int,
        burst: int,
        max_keys: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.rate = per_minute / 60.0
        self.capacity = float(max(1, burst))
        self.enabled = per_minute > 0
        self.max_keys = max_keys
        self._clock = clock
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()  # key -> (tokens, last)
        self._lock = threading.Lock()

    def acquire(self, key: str) -> float:
        """Take one token. Returns 0.0 when allowed, else the seconds until a token is available."""
        if not self.enabled:
            return 0.0
        now = self._clock()
        with self._lock:
            tokens, last = self._buckets.pop(key, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.rate)
            if tokens >= 1.0:
                self._buckets[key] = (tokens - 1.0, now)
                wait = 0.0
            else:
                self._buckets[key] = (tokens, now)
                wait = (1.0 - tokens) / self.rate
            while len(self._buckets) > self.max_keys:
                self._buckets.popitem(last=False)
        return wait


# One statement: refill by elapsed time (capped at capacity), then debit one token if available.
# `allowed` records whether this call got a token; `tokens` is the balance after the call.
# statement_timestamp() is fixed for the statement, so every use below sees the same "now"; a statement
# that queued on the row lock behind a later one never gets negative elapsed time (GREATEST 0).
PG_ACQUIRE = """
INSERT INTO rate_limit_buckets AS b (key, tokens, updated_at, allowed)
VALUES (%(key)s, %(capacity)s - 1, statement_timestamp(), true)
ON CONFLICT (key) DO UPDATE SET
    tokens = CASE
        WHEN LEAST(%(capacity)s,
                   b.tokens + GREATEST(0, EXTRACT(EPOCH FROM (statement_timestamp() - b.updated_at))) * %(rate)s) >= 1
        THEN LEAST(%(capacity)s,
                   b.tokens + GREATEST(0, EXTRACT(EPOCH FROM (statement_timestamp() - b.updated_at))) * %(rate)s) - 1
        ELSE LEAST(%(capacity)s,
                   b.tokens + GREATEST(0, EXTRACT(EPOCH FROM (statement_timestamp() - b.updated_at))) * %(rate)s)
    END,
    allowed = LEAST(%(capacity)s,
                    b.tokens + GREATEST(0, EXTRACT(EPOCH FROM (statement_timestamp() - b.updated_at))) * %(rate)s) >= 1,
    updated_at = GREATEST(b.updated_at, statement_timestamp())
RETURNING tokens, allowed
"""
PG_PRUNE = "DELETE FROM rate_limit_buckets WHERE updated_at < statement_timestamp() - interval '1 hour'"
PRUNE_EVERY = 500  # calls per instance between deletes of idle buckets (a full bucket needs no row)


class PostgresRateLimiter:
    """Token bucket shared by every API instance through Postgres (see module docstring)."""

    def __init__(self, dsn: str | None, per_minute: int, burst: int, pool: Any | None = None) -> None:
        self.rate = per_minute / 60.0
        self.capacity = float(max(1, burst))
        self.enabled = per_minute > 0
        self._calls = 0
        self._own_pool = pool is None
        if pool is None:
            from psycopg_pool import ConnectionPool

            pool = ConnectionPool(
                dsn or "",
                min_size=1,
                max_size=2,
                kwargs={"autocommit": True},
                check=ConnectionPool.check_connection,
                timeout=5.0,
                open=True,
                name="kgrag-ratelimit",
            )
        self._pool = pool

    def acquire(self, key: str) -> float:
        if not self.enabled:
            return 0.0
        params = {"key": key, "capacity": self.capacity, "rate": self.rate}
        try:
            with self._pool.connection() as conn:
                row = conn.execute(PG_ACQUIRE, params).fetchone()
                self._calls += 1
                if self._calls % PRUNE_EVERY == 0:
                    conn.execute(PG_PRUNE)
        except Exception as exc:  # fail closed: an unreachable limiter must not mean "no limit"
            raise LimiterUnavailable(type(exc).__name__) from exc
        tokens, allowed = float(row[0]), bool(row[1])
        if allowed:
            return 0.0
        return max(1e-3, (1.0 - tokens) / self.rate)

    def close(self) -> None:
        if self._own_pool:
            self._pool.close()


def client_key(api_key: str | None, client_host: str | None) -> str:
    if api_key:
        return "key:" + hashlib.sha256(api_key.encode()).hexdigest()[:16]
    return "ip:" + (client_host or "unknown")


def retry_after_header(wait_s: float) -> str:
    return str(max(1, math.ceil(wait_s)))
