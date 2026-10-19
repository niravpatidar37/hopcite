"""Two API instances share one rate limit and one spend cap through Postgres.

Each "instance" below is a separate PostgresRateLimiter / PostgresSpendLedger with its OWN connection
pool, exactly as two Cloud Run instances would have. Skipped unless KGRAG_INTEGRATION=1 (CI runs it in
the `migrations` job against a pgvector service container; locally after `python -m kgrag.migrate up`).
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from kgrag.migrate import migrate_postgres
from kgrag.ratelimit import PostgresRateLimiter
from kgrag.spend import SPEND_LOCK_KEY, BudgetExceeded, PostgresSpendLedger

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("KGRAG_INTEGRATION") != "1", reason="needs postgres"),
]


@pytest.fixture(scope="module")
def dsn() -> str:
    from kgrag.config import get_settings

    migrate_postgres()
    return get_settings().pg_dsn


@pytest.fixture
def pg(dsn: str) -> Iterator[Any]:
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        yield conn
        conn.execute("DELETE FROM rate_limit_buckets WHERE key LIKE 'itest-%'")
        conn.execute("DELETE FROM llm_spend_ledger WHERE project LIKE 'itest-%'")


def _key() -> str:
    return f"itest-{uuid.uuid4().hex[:12]}"


def _limiters(dsn: str, per_minute: int, burst: int, n: int = 2) -> list[PostgresRateLimiter]:
    return [PostgresRateLimiter(dsn, per_minute, burst) for _ in range(n)]


# --------------------------------------------------------------------------- rate limiter
def test_two_instances_share_one_bucket(dsn: str, pg: Any) -> None:
    a, b = _limiters(dsn, per_minute=1, burst=3)
    key = _key()
    try:
        waits = [lim.acquire(key) for lim in (a, b, a, b, a)]
    finally:
        a.close()
        b.close()
    allowed = [w == 0.0 for w in waits]
    assert allowed == [True, True, True, False, False]  # per-instance buckets would allow all five
    assert waits[3] > 1.0  # 1/min refill: the next token is ~a minute away


def test_concurrent_requests_on_two_instances_never_exceed_burst(dsn: str, pg: Any) -> None:
    limiters = _limiters(dsn, per_minute=1, burst=5)
    key = _key()
    results: list[float] = []
    lock = threading.Lock()

    def hit(lim: PostgresRateLimiter) -> None:
        w = lim.acquire(key)
        with lock:
            results.append(w)

    threads = [threading.Thread(target=hit, args=(limiters[i % 2],)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    for lim in limiters:
        lim.close()
    assert len(results) == 20
    assert sum(1 for w in results if w == 0.0) == 5


def test_two_apps_share_the_limit(dsn: str, pg: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from kgrag import config
    from kgrag.api import create_app
    from tests.test_api import FakeServices

    monkeypatch.setattr(config, "_loaded", True)
    monkeypatch.setenv("KGRAG_API_KEY", "k" * 24 + _key())  # unique key => unique bucket per test run
    key = os.environ["KGRAG_API_KEY"]
    a, b = _limiters(dsn, per_minute=1, burst=4)
    try:
        with (
            TestClient(create_app(FakeServices(), limiter=a), client=("203.0.113.7", 1)) as ca,
            TestClient(create_app(FakeServices(), limiter=b), client=("203.0.113.8", 1)) as cb,
        ):
            codes = []
            for i in range(8):
                c = ca if i % 2 == 0 else cb
                r = c.post("/ask", json={"question": "Which foundries does NVIDIA use?"}, headers={"X-API-Key": key})
                codes.append(r.status_code)
                if r.status_code == 429:
                    assert int(r.headers["Retry-After"]) >= 1
    finally:
        a.close()
        b.close()
    assert codes == [200, 200, 200, 200, 429, 429, 429, 429]


def test_replacing_an_instance_does_not_reset_the_bucket(dsn: str, pg: Any) -> None:
    key = _key()
    (a,) = _limiters(dsn, per_minute=1, burst=2, n=1)
    assert a.acquire(key) == 0.0 and a.acquire(key) == 0.0
    a.close()  # instance scaled in / replaced by a new revision
    (fresh,) = _limiters(dsn, per_minute=1, burst=2, n=1)
    try:
        assert fresh.acquire(key) > 0.0  # an in-memory limiter would start full here
    finally:
        fresh.close()


def test_bucket_refills_over_time(dsn: str, pg: Any) -> None:
    key = _key()
    (a,) = _limiters(dsn, per_minute=600, burst=1, n=1)  # 10 tokens/s
    try:
        assert a.acquire(key) == 0.0
        wait = a.acquire(key)
        assert 0.0 < wait <= 0.11
        time.sleep(0.25)
        assert a.acquire(key) == 0.0
    finally:
        a.close()


# --------------------------------------------------------------------------- spend ledger
def _ledgers(dsn: str, project: str, cap: float, n: int = 2) -> list[PostgresSpendLedger]:
    return [PostgresSpendLedger(dsn, project=project, global_cap=1e9, project_cap=cap) for _ in range(n)]


def test_concurrent_reservations_on_two_instances_respect_the_cap(dsn: str, pg: Any) -> None:
    project = _key()
    ledgers = _ledgers(dsn, project, cap=1.0)
    ok: list[int] = []
    refused: list[int] = []
    lock = threading.Lock()

    def reserve(led: PostgresSpendLedger) -> None:
        try:
            led.reserve(0.3)
            with lock:
                ok.append(1)
        except BudgetExceeded:
            with lock:
                refused.append(1)

    threads = [threading.Thread(target=reserve, args=(ledgers[i % 2],)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    for led in ledgers:
        led.close()
    assert (len(ok), len(refused)) == (3, 9)  # 3 x 0.3 <= 1.0 < 4 x 0.3, across both instances
    total = pg.execute("SELECT sum(usd) FROM llm_spend_ledger WHERE project = %s", (project,)).fetchone()[0]
    assert float(total) == pytest.approx(0.9)


def test_spend_survives_instance_replacement_and_settles(dsn: str, pg: Any) -> None:
    project = _key()
    (a,) = _ledgers(dsn, project, cap=1.0, n=1)
    r1 = a.reserve(0.6)
    a.settle(r1, "groq", "openai/gpt-oss-20b", 1000, 100, 0.05)  # actual far below the worst case
    r2 = a.reserve(0.5)
    a.close()  # the old per-instance /tmp ledger would be gone with the instance
    (b,) = _ledgers(dsn, project, cap=1.0, n=1)
    try:
        assert b.totals()[1] == pytest.approx(0.55)  # settled 0.05 + open reservation 0.5
        with pytest.raises(BudgetExceeded):
            b.reserve(0.5)
        b.release(r2)  # provider error: nothing billed
        assert b.totals()[1] == pytest.approx(0.05)
        b.reserve(0.5)
    finally:
        b.close()
    statuses = dict(pg.execute("SELECT id, status FROM llm_spend_ledger WHERE project = %s", (project,)).fetchall())
    assert statuses[r1.id] == "settled" and statuses[r2.id] == "released"


def test_reservation_waits_for_the_cluster_wide_lock(dsn: str, pg: Any) -> None:
    """reserve() must serialize on the advisory lock; without it the check-then-insert races."""
    import psycopg

    project = _key()
    (led,) = _ledgers(dsn, project, cap=1.0, n=1)
    done = threading.Event()
    with psycopg.connect(dsn, autocommit=True) as holder:
        holder.execute("SELECT pg_advisory_lock(%s)", (SPEND_LOCK_KEY,))
        t = threading.Thread(target=lambda: (led.reserve(0.1), done.set()))
        t.start()
        time.sleep(1.0)
        blocked = not done.is_set()
        holder.execute("SELECT pg_advisory_unlock(%s)", (SPEND_LOCK_KEY,))
        t.join(timeout=10)
    led.close()
    assert blocked, "reserve() did not wait for the advisory lock"
    assert done.is_set()


def test_runtime_role_can_use_shared_tables_but_not_delete_spend(dsn: str, pg: Any) -> None:
    exists = pg.execute("SELECT 1 FROM pg_roles WHERE rolname = 'kgrag_app'").fetchone()
    if exists:
        pytest.skip("kgrag_app exists already (compose stack); checked by the smoke test instead")
    pg.execute("CREATE ROLE kgrag_app NOLOGIN")
    try:
        migrate_postgres()

        def priv(table: str, p: str) -> bool:
            return bool(pg.execute("SELECT has_table_privilege('kgrag_app', %s, %s)", (table, p)).fetchone()[0])

        assert priv("rate_limit_buckets", "INSERT") and priv("rate_limit_buckets", "UPDATE")
        assert priv("llm_spend_ledger", "INSERT") and priv("llm_spend_ledger", "UPDATE")
        assert not priv("llm_spend_ledger", "DELETE") and not priv("llm_spend_ledger", "TRUNCATE")
    finally:
        pg.execute("DROP OWNED BY kgrag_app")
        pg.execute("DROP ROLE kgrag_app")
