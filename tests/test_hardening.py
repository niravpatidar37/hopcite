"""Production hardening: fake-LLM gate, deployed-env config guard, security headers, body size limit,
generic 500s, fail-closed shared limiter, spend reservations, and the deterministic fake answerer.

No services or secrets needed. The two-instance proofs for the Postgres limiter and ledger are in
tests/test_shared_limits_integration.py (KGRAG_INTEGRATION=1).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from kgrag import config
from kgrag.answer import Answerer, check_citations
from kgrag.api import MAX_BODY_BYTES, SECURITY_HEADERS, create_app
from kgrag.config import UnsafeConfig, assert_runtime_config, get_settings
from kgrag.llm import FAKE_PREFIX, LLMClient, LLMError, fake_complete
from kgrag.ratelimit import LimiterUnavailable, RateLimiter
from kgrag.spend import BudgetExceeded, Reservation, SpendLedger
from kgrag.vector_store import Passage
from tests.test_api import FakeResult, FakeServices

Q = {"question": "Which foundries does NVIDIA use?"}
ENV_VARS = (
    "KGRAG_API_KEY",
    "KGRAG_HOST",
    "KGRAG_KILL_SWITCH",
    "KGRAG_RATE_LIMIT_PER_MIN",
    "KGRAG_RATE_LIMIT_BURST",
    "KGRAG_RATE_LIMIT_BACKEND",
    "KGRAG_SPEND_BACKEND",
    "KGRAG_ENV",
    "KGRAG_LLM_MODE",
    "K_SERVICE",
    "CLOUD_RUN_JOB",
)


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "_loaded", True)
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _client(svc: Any = None, limiter: Any = None, **kw: Any) -> TestClient:
    return TestClient(create_app(svc or FakeServices(), limiter=limiter), client=("127.0.0.1", 50000), **kw)


# --------------------------------------------------------------------------- fake LLM gate
@pytest.mark.parametrize("env", ["prod", "staging", "production", "PROD"])
def test_fake_llm_refused_outside_dev_ci_test(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env: str) -> None:
    monkeypatch.setenv("KGRAG_ENV", env)  # unknown values ("production") count as prod
    monkeypatch.setenv("KGRAG_LLM_MODE", "fake")
    with pytest.raises(UnsafeConfig):
        LLMClient("fake", ledger=SpendLedger(tmp_path / "l.jsonl", "p1-kgrag", 25, 5), cache_dir=tmp_path)
    with pytest.raises(UnsafeConfig):
        assert_runtime_config(get_settings())
    from kgrag.service import answer_backend

    with pytest.raises(UnsafeConfig):
        answer_backend()


def test_fake_llm_refused_on_cloud_run_even_if_env_says_ci(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KGRAG_ENV", "ci")
    monkeypatch.setenv("K_SERVICE", "kgrag-prod-api")  # set by Cloud Run on every service instance
    with pytest.raises(UnsafeConfig, match="Cloud Run"):
        LLMClient("fake", ledger=SpendLedger(tmp_path / "l.jsonl", "p1-kgrag", 25, 5), cache_dir=tmp_path)


def test_app_refuses_to_start_with_fake_llm_in_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KGRAG_ENV", "prod")
    monkeypatch.setenv("KGRAG_LLM_MODE", "fake")
    monkeypatch.setenv("KGRAG_RATE_LIMIT_BACKEND", "postgres")
    monkeypatch.setenv("KGRAG_SPEND_BACKEND", "postgres")
    with pytest.raises(UnsafeConfig), _client(limiter=RateLimiter(30, 10)):
        pass


@pytest.mark.parametrize("env", ["ci", "dev", "test"])
def test_fake_llm_allowed_in_ci(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env: str) -> None:
    monkeypatch.setenv("KGRAG_ENV", env)
    monkeypatch.setenv("KGRAG_LLM_MODE", "fake")
    c = LLMClient("fake", ledger=SpendLedger(tmp_path / "l.jsonl", "p1-kgrag", 25, 5), cache_dir=tmp_path)
    assert c.provider == "fake"
    from kgrag.service import answer_backend

    assert answer_backend() == ("fake", "fake-extractive")


# --------------------------------------------------------------------------- deployed-env config guard
@pytest.mark.parametrize(
    "rl,sp,ok",
    [
        ("memory", "file", False),
        ("postgres", "file", False),
        ("memory", "postgres", False),
        ("postgres", "postgres", True),
    ],
)
def test_deployed_envs_require_shared_limits(monkeypatch: pytest.MonkeyPatch, rl: str, sp: str, ok: bool) -> None:
    monkeypatch.setenv("KGRAG_RATE_LIMIT_BACKEND", rl)
    monkeypatch.setenv("KGRAG_SPEND_BACKEND", sp)
    for env in ("staging", "prod"):
        monkeypatch.setenv("KGRAG_ENV", env)
        if ok:
            assert_runtime_config(get_settings())
        else:
            with pytest.raises(UnsafeConfig, match="BACKEND must be postgres"):
                assert_runtime_config(get_settings())
    monkeypatch.setenv("KGRAG_ENV", "dev")
    assert_runtime_config(get_settings())  # local dev may use the in-process limiter and the JSONL ledger


def test_cloud_run_requires_shared_limits_even_without_kgrag_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("K_SERVICE", "kgrag-staging-api")
    with pytest.raises(UnsafeConfig):
        assert_runtime_config(get_settings())


def test_unknown_backend_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KGRAG_RATE_LIMIT_BACKEND", "redis")
    with pytest.raises(UnsafeConfig):
        assert_runtime_config(get_settings())


# --------------------------------------------------------------------------- headers, docs, errors
def test_security_headers_on_success_and_error_responses() -> None:
    with _client() as c:
        for r in (c.get("/healthz"), c.post("/ask", json={"question": ""}), c.get("/nope")):
            for k, v in SECURITY_HEADERS.items():
                assert r.headers.get(k) == v, (r.request.url, k)


def test_docs_disabled_in_deployed_envs(monkeypatch: pytest.MonkeyPatch) -> None:
    with _client() as c:
        assert c.get("/openapi.json").status_code == 200  # local dev keeps the docs
    monkeypatch.setenv("KGRAG_ENV", "prod")
    monkeypatch.setenv("KGRAG_RATE_LIMIT_BACKEND", "postgres")
    monkeypatch.setenv("KGRAG_SPEND_BACKEND", "postgres")
    with _client(limiter=RateLimiter(30, 10)) as c:
        assert c.get("/openapi.json").status_code == 404
        assert c.get("/docs").status_code == 404


class _ExplodingPipeline:
    def ask(self, q: str, mode: str = "hybrid") -> Any:
        raise RuntimeError("password=hunter2 at postgresql://kgrag@10.0.0.3")


def test_unhandled_errors_are_generic_500s() -> None:
    svc = FakeServices()
    svc.pipeline = _ExplodingPipeline()  # type: ignore[assignment]
    with _client(svc, raise_server_exceptions=False) as c:
        r = c.post("/ask", json=Q)
    assert r.status_code == 500 and r.json() == {"detail": "internal error"}
    assert "hunter2" not in r.text and "Traceback" not in r.text and "postgresql://" not in r.text
    assert r.headers.get("X-Content-Type-Options") == "nosniff"


def test_validation_errors_do_not_echo_tracebacks() -> None:
    with _client() as c:
        r = c.post("/ask", content=b'{"question": ', headers={"content-type": "application/json"})
    assert r.status_code == 422 and "Traceback" not in r.text


# --------------------------------------------------------------------------- body size
def test_oversize_body_rejected_with_413_before_the_pipeline() -> None:
    svc = FakeServices()
    big = {"question": "x" * (MAX_BODY_BYTES + 10)}
    with _client(svc) as c:
        r = c.post("/ask", json=big)
    assert r.status_code == 413 and svc.pipeline.questions == []


def test_chunked_oversize_body_rejected() -> None:
    svc = FakeServices()

    def gen() -> Any:
        for _ in range(40):
            yield b"x" * 1024

    with _client(svc) as c:
        r = c.post("/ask", content=gen(), headers={"content-type": "application/json"})
    assert r.status_code == 413 and svc.pipeline.questions == []


def test_chunked_small_body_still_works() -> None:
    svc = FakeServices()

    def gen() -> Any:
        yield b'{"question": '
        yield b'"Which foundries does NVIDIA use?"}'

    with _client(svc) as c:
        r = c.post("/ask", content=gen(), headers={"content-type": "application/json"})
    assert r.status_code == 200 and svc.pipeline.questions == ["Which foundries does NVIDIA use?"]


def test_question_over_500_chars_is_422() -> None:
    with _client() as c:
        assert c.post("/ask", json={"question": "x" * 501}).status_code == 422


# --------------------------------------------------------------------------- rate limiting
def test_invalid_bodies_spend_rate_limit_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """The smoke test trips the limiter with invalid bodies (no LLM cost); this pins that behaviour."""
    with _client(limiter=RateLimiter(6, 2)) as c:
        codes = [c.post("/ask", json={"question": ""}).status_code for _ in range(3)]
        r = c.post("/ask", json={"question": ""})
    assert codes == [422, 422, 429] and int(r.headers["Retry-After"]) >= 1


class _DownLimiter:
    enabled = True

    def acquire(self, key: str) -> float:
        raise LimiterUnavailable("OperationalError")


def test_unreachable_shared_limiter_fails_closed() -> None:
    svc = FakeServices()
    with _client(svc, limiter=_DownLimiter()) as c:
        r = c.post("/ask", json=Q)
    assert r.status_code == 503 and svc.pipeline.questions == []


def test_postgres_limiter_wraps_db_errors() -> None:
    from kgrag.ratelimit import PostgresRateLimiter

    class Pool:
        def connection(self) -> Any:
            raise ConnectionError("db down")

    lim = PostgresRateLimiter(None, 30, 10, pool=Pool())
    with pytest.raises(LimiterUnavailable):
        lim.acquire("k")


def test_response_exposes_retrieved_ids_and_fallback_reason() -> None:
    class P:
        def ask(self, q: str, mode: str = "hybrid") -> Any:
            r = FakeResult()
            r.retrieved_chunk_ids = ["NVDA-10-K-2026#c0012", "NVDA-10-K-2026#c0013"]  # type: ignore[attr-defined]
            r.fallback_reason = "router_disabled"  # type: ignore[attr-defined]
            return r

    svc = FakeServices()
    svc.pipeline = P()  # type: ignore[assignment]
    with _client(svc) as c:
        body = c.post("/ask", json=Q).json()
    assert body["fallback_reason"] == "router_disabled"
    assert {c["chunk_id"] for c in body["citations"]} <= set(body["retrieved_chunk_ids"])


def test_main_disables_server_header(monkeypatch: pytest.MonkeyPatch) -> None:
    import uvicorn

    from kgrag import api

    seen: dict[str, Any] = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(kw))
    api.main()
    assert seen["server_header"] is False and seen["proxy_headers"] is False


# --------------------------------------------------------------------------- spend reservations
class _RecordingLedger:
    def __init__(self, fail: bool = False) -> None:
        self.events: list[tuple[str, Any]] = []
        self.fail = fail

    def reserve(self, estimate_usd: float) -> Reservation:
        if self.fail:
            raise BudgetExceeded("over")
        self.events.append(("reserve", estimate_usd))
        return Reservation(estimate_usd, 7)

    def settle(self, res: Reservation, provider: str, model: str, i: int, o: int, usd: float) -> None:
        self.events.append(("settle", usd))

    def release(self, res: Reservation) -> None:
        self.events.append(("release", res.id))


def _groq(tmp_path: Path, ledger: Any, handler: Any) -> LLMClient:
    return LLMClient(
        "groq",
        ledger=ledger,
        cache_dir=tmp_path,
        use_cache=False,
        max_retries=0,
        transport=httpx.MockTransport(handler),
    )


def test_paid_call_reserves_then_settles_actual_cost(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "test")
    ledger = _RecordingLedger()
    body = {"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 100, "completion_tokens": 10}}
    c = _groq(tmp_path, ledger, lambda req: httpx.Response(200, json=body))
    c.complete([{"role": "user", "content": "hi"}], "openai/gpt-oss-20b", max_tokens=50)
    kinds = [k for k, _ in ledger.events]
    assert kinds == ["reserve", "settle"]
    assert ledger.events[1][1] < ledger.events[0][1]  # actual cost below the worst-case reservation


def test_budget_refusal_happens_before_any_http(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "test")
    hits: list[Any] = []
    c = _groq(tmp_path, _RecordingLedger(fail=True), lambda req: hits.append(req) or httpx.Response(200, json={}))
    with pytest.raises(BudgetExceeded):
        c.complete([{"role": "user", "content": "hi"}], "openai/gpt-oss-20b", max_tokens=50)
    assert hits == []


def test_provider_error_releases_reservation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "test")
    ledger = _RecordingLedger()
    c = _groq(tmp_path, ledger, lambda req: httpx.Response(400, json={"error": "bad"}))
    with pytest.raises(LLMError):
        c.complete([{"role": "user", "content": "hi"}], "openai/gpt-oss-20b", max_tokens=50)
    assert [k for k, _ in ledger.events] == ["reserve", "release"]


def test_timeout_keeps_worst_case_reservation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "test")
    ledger = _RecordingLedger()

    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=req)

    c = _groq(tmp_path, ledger, handler)
    with pytest.raises(LLMError):
        c.complete([{"role": "user", "content": "hi"}], "openai/gpt-oss-20b", max_tokens=50)
    assert [k for k, _ in ledger.events] == ["reserve"]  # may have been billed: stays counted


def test_file_ledger_reserve_enforces_caps(tmp_path: Path) -> None:
    ledger = SpendLedger(tmp_path / "l.jsonl", "p1-kgrag", global_cap=25.0, project_cap=1.0)
    res = ledger.reserve(0.4)
    ledger.settle(res, "groq", "m", 1, 1, 0.9)
    with pytest.raises(BudgetExceeded):
        ledger.reserve(0.2)


# --------------------------------------------------------------------------- fake answerer
def _passage(cid: str, text: str) -> Passage:
    # Real section paths contain ">" (e.g. "Item 1. Business > Competition"); the fake must parse them.
    return Passage(cid, cid.split("#")[0], "Super Micro Computer", "Item 1. Business > Competition", text, 0.9, [])


PASSAGES = [
    _passage("SMCI-10-K-2026#c0010", "We compete with Dell, HPE and Lenovo in server and storage systems. More text."),
    _passage("SMCI-10-K-2026#c0011", "Our manufacturing facilities are located in San Jose and Taiwan."),
]


def _fake_answerer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Answerer:
    monkeypatch.setenv("KGRAG_ENV", "ci")
    llm = LLMClient("fake", ledger=SpendLedger(tmp_path / "l.jsonl", "p1-kgrag", 25, 5), cache_dir=tmp_path)
    return Answerer(llm, "fake-extractive")


def test_fake_answer_cites_a_retrieved_passage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    res = _fake_answerer(tmp_path, monkeypatch).answer(
        "Which companies does Supermicro compete with in servers?", [], PASSAGES
    )
    assert res.refused is False and res.answer.startswith(FAKE_PREFIX)
    assert res.citations == ["SMCI-10-K-2026#c0010"]
    assert set(res.citations) <= set(res.context_chunk_ids)


def test_fake_answer_refuses_out_of_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    res = _fake_answerer(tmp_path, monkeypatch).answer("Who painted the Mona Lisa?", [], PASSAGES)
    assert res.refused is True and res.citations == []


def test_fake_complete_output_passes_citation_check() -> None:
    from kgrag.answer import AnswerPayload, build_context

    ctx = build_context([], PASSAGES)
    msgs = [
        {"role": "user", "content": f"CONTEXT:\n{ctx}\n\nQUESTION: Where are the manufacturing facilities located?"}
    ]
    text, _, _ = fake_complete(msgs, AnswerPayload.model_json_schema())
    payload = AnswerPayload.model_validate_json(text)
    assert check_citations(payload, {p.chunk_id for p in PASSAGES}) == []
    assert payload.claims[0].citations == ["SMCI-10-K-2026#c0011"]


def test_fake_complete_rejects_other_schemas() -> None:
    from kgrag.router import RouterOutput

    with pytest.raises(LLMError):
        fake_complete([{"role": "user", "content": "Q: x"}], RouterOutput.model_json_schema())
