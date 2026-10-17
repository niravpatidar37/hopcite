"""Production controls: liveness vs readiness, fail-closed binding, API-key rotation, rate limiting,
the operator kill switch, structured log redaction, the baked embedding model, routing log sinks.

No services or secrets needed. Each control has at least one test that fails if it is removed
(scripts/mutation_check.py breaks them one at a time).
"""

from __future__ import annotations

import io
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from kgrag import config
from kgrag.api import UnsafeBinding, assert_safe_binding, create_app
from kgrag.config import Settings
from kgrag.llm import LLMClient, LLMDisabled
from kgrag.logs import JsonFormatter
from kgrag.ratelimit import RateLimiter, client_key
from kgrag.spend import SpendLedger
from tests.test_api import FakeServices

Q = {"question": "Which foundries does NVIDIA use?"}
STRONG = "k" * 24


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "_loaded", True)  # never read the developer's .env
    for name in (
        "KGRAG_API_KEY",
        "KGRAG_HOST",
        "KGRAG_KILL_SWITCH",
        "KGRAG_RATE_LIMIT_PER_MIN",
        "KGRAG_RATE_LIMIT_BURST",
        "KGRAG_READINESS_TIMEOUT_S",
        "KGRAG_EMBED_MODEL_PATH",
        "PORT",
        "KGRAG_PORT",
    ):
        monkeypatch.delenv(name, raising=False)


def _client(svc: Any = None, host: str = "127.0.0.1") -> TestClient:
    return TestClient(create_app(svc or FakeServices()), client=(host, 50000))


class _Raises:
    def run(self, *a: Any) -> Any:
        raise ConnectionError("neo4j down")

    def ping(self) -> bool:
        raise ConnectionError("down")


class _Slow:
    def ping(self) -> bool:
        time.sleep(1.0)
        return True


# --------------------------------------------------------------------------- liveness / readiness
def test_healthz_ok_even_when_dependencies_are_down() -> None:
    svc = FakeServices()
    svc.runner, svc.vectors = _Raises(), _Raises()
    with _client(svc) as c:
        assert c.get("/healthz").status_code == 200


def test_readyz_ok_when_both_stores_answer() -> None:
    with _client() as c:
        r = c.get("/readyz")
    assert r.status_code == 200
    assert r.json() == {"status": "ready", "neo4j": True, "postgres": True, "llm_enabled": True}


def test_readyz_503_when_neo4j_down() -> None:
    svc = FakeServices()
    svc.runner = _Raises()
    with _client(svc) as c:
        r = c.get("/readyz")
    assert r.status_code == 503 and r.json()["neo4j"] is False and r.json()["postgres"] is True


def test_readyz_503_when_postgres_down() -> None:
    svc = FakeServices()
    svc.vectors = _Raises()
    with _client(svc) as c:
        r = c.get("/readyz")
    assert r.status_code == 503 and r.json()["postgres"] is False


def test_readyz_times_out_on_a_hanging_dependency(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KGRAG_READINESS_TIMEOUT_S", "0.2")
    svc = FakeServices()
    svc.vectors = _Slow()
    with _client(svc) as c:
        t0 = time.perf_counter()
        r = c.get("/readyz")
        assert time.perf_counter() - t0 < 0.9
    assert r.status_code == 503 and r.json()["postgres"] is False


def test_probes_need_no_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KGRAG_API_KEY", STRONG)
    with _client(host="203.0.113.7") as c:
        assert c.get("/healthz").status_code == 200
        assert c.get("/readyz").status_code == 200
        assert c.post("/ask", json=Q).status_code == 401


# --------------------------------------------------------------------------- fail-closed binding
@pytest.mark.parametrize("host", ["0.0.0.0", "::", "10.0.0.5"])
def test_non_loopback_bind_without_key_refused(host: str) -> None:
    with pytest.raises(UnsafeBinding):
        assert_safe_binding(Settings(api_host=host, api_key=""))


def test_non_loopback_bind_with_short_key_refused() -> None:
    with pytest.raises(UnsafeBinding, match="at least"):
        assert_safe_binding(Settings(api_host="0.0.0.0", api_key=f"{STRONG},short"))


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_bind_without_key_allowed(host: str) -> None:
    assert_safe_binding(Settings(api_host=host, api_key=""))


def test_non_loopback_bind_with_strong_key_allowed() -> None:
    assert_safe_binding(Settings(api_host="0.0.0.0", api_key=STRONG))


def test_app_startup_fails_when_bound_off_host_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KGRAG_HOST", "0.0.0.0")
    with pytest.raises(UnsafeBinding), _client():
        pass


def test_main_refuses_before_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    import uvicorn

    from kgrag import api

    called: list[Any] = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: called.append(1))
    monkeypatch.setenv("KGRAG_HOST", "0.0.0.0")
    with pytest.raises(UnsafeBinding):
        api.main()
    assert called == []


# --------------------------------------------------------------------------- API keys
def test_multiple_api_keys_for_rotation(monkeypatch: pytest.MonkeyPatch) -> None:
    old, new = "old-" + "a" * 20, "new-" + "b" * 20
    monkeypatch.setenv("KGRAG_API_KEY", f"{old}, {new}")
    with _client(host="203.0.113.7") as c:
        assert c.post("/ask", json=Q, headers={"X-API-Key": old}).status_code == 200
        assert c.post("/ask", json=Q, headers={"X-API-Key": new}).status_code == 200
        assert c.post("/ask", json=Q, headers={"X-API-Key": "c" * 24}).status_code == 401
        assert c.post("/ask", json=Q, headers={"X-API-Key": f"{old}, {new}"}).status_code == 401


# --------------------------------------------------------------------------- rate limiting
def test_rate_limit_returns_429_with_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KGRAG_RATE_LIMIT_PER_MIN", "6")
    monkeypatch.setenv("KGRAG_RATE_LIMIT_BURST", "3")
    svc = FakeServices()
    with _client(svc) as c:
        codes = [c.post("/ask", json=Q).status_code for _ in range(5)]
        r = c.post("/ask", json=Q)
    assert codes == [200, 200, 200, 429, 429]
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1
    assert len(svc.pipeline.questions) == 3  # limited calls never reach the pipeline


def test_rate_limit_buckets_are_per_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    a, b = "a" * 24, "b" * 24
    monkeypatch.setenv("KGRAG_API_KEY", f"{a},{b}")
    monkeypatch.setenv("KGRAG_RATE_LIMIT_PER_MIN", "6")
    monkeypatch.setenv("KGRAG_RATE_LIMIT_BURST", "1")
    with _client(host="203.0.113.7") as c:
        assert c.post("/ask", json=Q, headers={"X-API-Key": a}).status_code == 200
        assert c.post("/ask", json=Q, headers={"X-API-Key": a}).status_code == 429
        assert c.post("/ask", json=Q, headers={"X-API-Key": b}).status_code == 200


def test_rate_limit_zero_disables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KGRAG_RATE_LIMIT_PER_MIN", "0")
    monkeypatch.setenv("KGRAG_RATE_LIMIT_BURST", "1")
    with _client() as c:
        assert all(c.post("/ask", json=Q).status_code == 200 for _ in range(5))


def test_token_bucket_refills_and_bounds_memory() -> None:
    now = [0.0]
    rl = RateLimiter(per_minute=60, burst=2, max_keys=3, clock=lambda: now[0])
    assert rl.acquire("x") == 0.0 and rl.acquire("x") == 0.0
    wait = rl.acquire("x")
    assert 0.9 < wait <= 1.0
    now[0] += 1.0
    assert rl.acquire("x") == 0.0
    for k in ("a", "b", "c", "d"):
        rl.acquire(k)
    assert len(rl._buckets) == 3


def test_client_key_hashes_api_keys() -> None:
    k = client_key("super-secret-key-value", "1.2.3.4")
    assert "super-secret" not in k and k.startswith("key:")
    assert client_key(None, "1.2.3.4") == "ip:1.2.3.4"


# --------------------------------------------------------------------------- kill switch
def test_kill_switch_blocks_ask_before_the_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KGRAG_KILL_SWITCH", "1")
    svc = FakeServices()
    with _client(svc) as c:
        r = c.post("/ask", json=Q)
        assert r.status_code == 503 and "disabled" in r.json()["detail"]
        assert c.get("/healthz").status_code == 200
        ready = c.get("/readyz")
        assert ready.status_code == 200 and ready.json()["llm_enabled"] is False
    assert svc.pipeline.questions == []


def test_kill_switch_off_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KGRAG_KILL_SWITCH", "0")
    with _client() as c:
        assert c.post("/ask", json=Q).status_code == 200


@pytest.mark.parametrize("provider", ["groq", "ollama"])
def test_kill_switch_blocks_llm_client_before_any_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "test")
    monkeypatch.setenv("KGRAG_KILL_SWITCH", "true")
    hits: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        hits.append(req)
        return httpx.Response(200, json={})

    ledger = SpendLedger(tmp_path / "l.jsonl", "p1-kgrag", 25.0, 5.0)
    c = LLMClient(provider, ledger=ledger, cache_dir=tmp_path, transport=httpx.MockTransport(handler))
    with pytest.raises(LLMDisabled):
        c.complete([{"role": "user", "content": "hi"}], "openai/gpt-oss-20b", max_tokens=10)
    assert hits == []
    assert (tmp_path / "l.jsonl").read_text(encoding="utf-8") == ""


# --------------------------------------------------------------------------- structured logs
def _format(record: logging.LogRecord) -> dict[str, Any]:
    out: dict[str, Any] = json.loads(JsonFormatter().format(record))
    return out


def test_json_logs_redact_pii_in_message_and_fields() -> None:
    rec = logging.LogRecord("kgrag.api", logging.INFO, __file__, 1, "from jane.doe@example.com", None, None)
    rec.fields = {"question": "call +1 (415) 555-0100", "nested": {"ssn": "123-45-6789"}, "n": 3}
    out = _format(rec)
    assert out["severity"] == "INFO" and out["logger"] == "kgrag.api"
    blob = json.dumps(out)
    for leaked in ("jane.doe@example.com", "555-0100", "123-45-6789"):
        assert leaked not in blob
    assert out["n"] == 3 and "[EMAIL]" in out["message"]


def test_json_logs_omit_exception_text() -> None:
    try:
        raise ValueError("password=hunter2 for bob@example.com")
    except ValueError:
        import sys

        rec = logging.LogRecord("x", logging.ERROR, __file__, 1, "failed", None, sys.exc_info())
    out = _format(rec)
    assert out["exception"] == "ValueError" and "hunter2" not in json.dumps(out)


def test_configure_logging_emits_json(monkeypatch: pytest.MonkeyPatch) -> None:
    from kgrag import logs

    buf = io.StringIO()
    monkeypatch.setattr(logs, "_configured", False)
    monkeypatch.setattr(logs.sys, "stdout", buf)
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    try:
        logs.configure_logging("json")
        logging.getLogger("kgrag.t").info("hello a@b.co")
        line = json.loads(buf.getvalue().strip().splitlines()[-1])
        assert line["message"] == "hello [EMAIL]"
    finally:
        root.handlers[:] = saved[0]
        root.setLevel(saved[1])
        monkeypatch.setattr(logs, "_configured", False)


# --------------------------------------------------------------------------- baked embedding model
def test_embedder_loads_baked_model_offline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import fastembed

    from kgrag import embeddings

    seen: dict[str, Any] = {}

    class FakeTE:
        def __init__(self, **kw: Any) -> None:
            seen.update(kw)

    monkeypatch.setattr(fastembed, "TextEmbedding", FakeTE)
    monkeypatch.setenv("KGRAG_EMBED_MODEL_PATH", str(tmp_path))
    embeddings.FastEmbedder()
    assert seen["specific_model_path"] == str(tmp_path) and seen["local_files_only"] is True


def test_image_pins_model_revision_and_hash() -> None:
    root = Path(__file__).resolve().parents[1]
    script = (root / "scripts" / "fetch_embed_model.py").read_text(encoding="utf-8")
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    assert 'REVISION = "aa8f8b060edb00e03bfdd08813a2949946c8ba55"' in script
    assert "ONNX_SHA256 = " in script
    assert "fetch_embed_model.py" in dockerfile and "HF_HUB_OFFLINE=1" in dockerfile


# --------------------------------------------------------------------------- config
def test_pg_dsn_quotes_awkward_passwords() -> None:
    from psycopg.conninfo import conninfo_to_dict

    pw = "a b'c\\d=e"
    d = conninfo_to_dict(Settings(pg_password=pw, pg_sslmode="require").pg_dsn)
    assert d["password"] == pw and d["sslmode"] == "require"


def test_port_env_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORT", "8080")
    assert config.get_settings().api_port == 8080
    monkeypatch.setenv("KGRAG_PORT", "9000")
    assert config.get_settings().api_port == 9000


def test_dotenv_loading_can_be_disabled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[Any] = []
    monkeypatch.setattr(config, "_loaded", False)
    monkeypatch.setattr(config, "load_dotenv", lambda *a, **k: calls.append(a))
    monkeypatch.setenv("KGRAG_LOAD_DOTENV", "0")
    config.load_env()
    assert calls == []


# --------------------------------------------------------------------------- routing log sinks
def _ask_result() -> Any:
    from kgrag.pipeline import AskResult

    return AskResult(
        question="email me at a@b.co",
        mode="vector",
        route="vector",
        answer="x",
        refused=False,
        answer_entities=[],
        citations=[],
        claims=[],
        graph_facts=[],
        retrieved_chunk_ids=[],
        template=None,
        entity_ids=[],
        fallback_reason="",
        latency_ms=1.0,
        router_model_used=None,
        usd=0.0,
        llm_calls=0,
        cached_llm_calls=0,
        input_tokens=0,
        output_tokens=0,
        regenerations=0,
    )


def test_failing_routing_sink_does_not_fail_the_request(tmp_path: Path) -> None:
    from kgrag.pipeline import Pipeline

    def boom(row: dict[str, Any]) -> None:
        raise ConnectionError("db down")

    pipe = Pipeline(None, None, None, log_path=tmp_path / "r.jsonl", log_sink=boom)  # type: ignore[arg-type]
    pipe._log(_ask_result(), None)  # must not raise
    assert not (tmp_path / "r.jsonl").exists()  # the sink, not the file, is the destination


def test_routing_sink_receives_redacted_question() -> None:
    from kgrag.pipeline import Pipeline

    rows: list[dict[str, Any]] = []
    pipe = Pipeline(None, None, None, log_sink=rows.append)  # type: ignore[arg-type]
    pipe._log(_ask_result(), None)
    assert rows[0]["question"] == "email me at [EMAIL]" and rows[0]["outcome"] == "answered"


@pytest.mark.parametrize("mode,expect_file", [("file", True), ("postgres", False), ("stdout", False), ("off", False)])
def test_routing_sink_selection(mode: str, expect_file: bool) -> None:
    from kgrag.service import routing_sink

    class V:
        def insert_routing_log(self, row: dict[str, Any]) -> None:
            pass

    v = V()
    sink, use_file = routing_sink(Settings(routing_log=mode), v)  # type: ignore[arg-type]
    assert use_file is expect_file
    if mode == "postgres":
        assert sink == v.insert_routing_log
    if mode in ("file", "off"):
        assert sink is None


# --------------------------------------------------------------------------- jobs
def test_ingest_job_runs_steps_in_fixed_order(monkeypatch: pytest.MonkeyPatch) -> None:
    from kgrag import ingest, jobs, migrate

    order: list[str] = []
    monkeypatch.setattr(migrate, "main", lambda argv=None: order.append("migrate") or 0)
    monkeypatch.setattr(ingest, "cmd_build", lambda: order.append("build"))
    monkeypatch.setattr(ingest, "cmd_extract", lambda limit: order.append("extract"))
    assert jobs.run_ingest(["build", "migrate", "extract"]) == 0
    assert order == ["migrate", "extract", "build"]


def test_ingest_job_stops_when_migration_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    from kgrag import ingest, jobs, migrate

    built: list[int] = []
    monkeypatch.setattr(migrate, "main", lambda argv=None: 2)
    monkeypatch.setattr(ingest, "cmd_build", lambda: built.append(1))
    assert jobs.run_ingest(["migrate", "build"]) == 2 and built == []


def test_ingest_job_rejects_unknown_steps() -> None:
    from kgrag import jobs

    assert jobs.run_ingest(["drop_everything"]) == 2
