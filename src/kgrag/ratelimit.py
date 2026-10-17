"""In-process token-bucket rate limiter for POST /ask.

One bucket per client key (a hash of the API key, else the client address). Limits are per
instance: the effective ceiling is `rate x max_instances`, which is why Terraform caps
max_instance_count. Memory is bounded: least-recently-seen buckets are evicted past `max_keys`.
"""

from __future__ import annotations

import hashlib
import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable


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


def client_key(api_key: str | None, client_host: str | None) -> str:
    if api_key:
        return "key:" + hashlib.sha256(api_key.encode()).hexdigest()[:16]
    return "ip:" + (client_host or "unknown")


def retry_after_header(wait_s: float) -> str:
    return str(max(1, math.ceil(wait_s)))
