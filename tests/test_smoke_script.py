"""scripts/smoke.py against a real HTTP server (uvicorn in a thread, test-double services).

Proves the smoke script passes on a correct service AND fails when the product invariant breaks
(a citation that was not retrieved), so a green smoke run means something.
"""

from __future__ import annotations

import importlib.util
import json
import socket
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from kgrag import config
from kgrag.api import create_app
from kgrag.ratelimit import RateLimiter
from tests.test_api import FakeServices

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("smoke", ROOT / "scripts" / "smoke.py")
assert _spec and _spec.loader
smoke = importlib.util.module_from_spec(_spec)
sys.modules["smoke"] = smoke
_spec.loader.exec_module(smoke)

KEY = "smoke-test-key-" + "a" * 16
RETRIEVED = ["SMCI-10-K-2026#c0010", "SMCI-10-K-2026#c0011"]


@dataclass
class Result:
    question: str
    cite: str = RETRIEVED[0]
    answer: str = "Dell, HPE and Lenovo."
    refused: bool = False
    route: str = "hybrid"
    mode: str = "hybrid"
    claims: list[dict[str, Any]] = field(default_factory=list)
    answer_entities: list[str] = field(default_factory=list)
    graph_facts: list[str] = field(default_factory=list)
    template: str | None = None
    latency_ms: float = 5.0
    usd: float = 0.0
    retrieved_chunk_ids: list[str] = field(default_factory=lambda: list(RETRIEVED))
    fallback_reason: str = "router_disabled"

    @property
    def citations(self) -> list[dict[str, Any]]:
        if self.refused:
            return []
        return [{"chunk_id": self.cite, "doc_id": self.cite.split("#")[0], "company": "Super Micro", "snippet": "."}]


class Pipe:
    def __init__(self, cite: str) -> None:
        self.cite = cite

    def ask(self, q: str, mode: str = "hybrid") -> Result:
        if "Mona Lisa" in q:
            return Result(q, refused=True, answer="I can't answer that from the indexed filings.")
        r = Result(q, cite=self.cite)
        r.claims = [{"text": "x", "citations": [self.cite]}]
        return r


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def serve(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    import uvicorn

    monkeypatch.setattr(config, "_loaded", True)
    for name in ("KGRAG_ENV", "KGRAG_LLM_MODE", "KGRAG_RATE_LIMIT_BACKEND", "KGRAG_SPEND_BACKEND", "K_SERVICE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KGRAG_API_KEY", KEY)
    servers: list[Any] = []

    def start(cite: str = RETRIEVED[0], kill: bool = False) -> str:
        svc = FakeServices()
        svc.pipeline = Pipe(cite)  # type: ignore[assignment]
        app = create_app(svc, limiter=RateLimiter(30, 10))
        if kill:
            # The kill switch is read per request from the env; emulate a second instance with it on.
            @app.middleware("http")
            async def _ks(request: Any, call_next: Any) -> Any:
                import os

                os.environ["KGRAG_KILL_SWITCH"] = "1"
                try:
                    return await call_next(request)
                finally:
                    os.environ.pop("KGRAG_KILL_SWITCH", None)

        port = _free_port()
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", server_header=False)
        )
        threading.Thread(target=server.run, daemon=True).start()
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        servers.append(server)
        return f"http://127.0.0.1:{port}"

    yield start
    for s in servers:
        s.should_exit = True
    time.sleep(0.2)


def _run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *args: str) -> tuple[int, dict[str, Any]]:
    monkeypatch.setenv("SMOKE_API_KEY", KEY)
    out = tmp_path / "smoke.json"
    rc = smoke.main([*args, "--json-out", str(out), "--wait", "5"])
    return rc, json.loads(out.read_text(encoding="utf-8"))


def test_smoke_passes_on_a_correct_service(serve: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base, ks = serve(), serve(kill=True)
    rc, summary = _run(
        tmp_path, monkeypatch, "--base-url", base, "--kill-switch-base-url", ks, "--expect-router-fallback"
    )
    by = {c["name"]: c["status"] for c in summary["checks"]}
    assert rc == 0, summary
    for name in (
        "liveness",
        "readiness",
        "auth_missing_key",
        "auth_wrong_key",
        "auth_valid_key",
        "input_validation",
        "oversize_question",
        "oversize_body",
        "security_headers",
        "no_stack_traces",
        "ask_schema_citations",
        "ask_out_of_scope",
        "router_fallback",
        "kill_switch",
        "rate_limit",
    ):
        assert by[name] == "PASS", (name, summary)
    assert by["db_least_privilege"] == "SKIP"  # test double cannot report it
    assert summary["failed"] == 0 and summary["passed"] == 15


def test_smoke_fails_when_a_citation_was_not_retrieved(
    serve: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = serve(cite="NVDA-10-K-2026#c9999")
    rc, summary = _run(tmp_path, monkeypatch, "--base-url", base, "--skip-rate-limit")
    by = {c["name"]: c["status"] for c in summary["checks"]}
    assert rc == 1 and by["ask_schema_citations"] == "FAIL"


def test_smoke_fails_when_auth_is_off(serve: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = serve()
    monkeypatch.setenv("KGRAG_API_KEY", "")  # read per request: the service now accepts anyone (loopback)
    rc, summary = _run(tmp_path, monkeypatch, "--base-url", base, "--skip-rate-limit")
    by = {c["name"]: c["status"] for c in summary["checks"]}
    assert rc == 1 and by["auth_missing_key"] == "FAIL"


def test_smoke_requires_least_privilege_when_asked(serve: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = serve()
    rc, summary = _run(tmp_path, monkeypatch, "--base-url", base, "--skip-rate-limit", "--require-least-privilege")
    by = {c["name"]: c["status"] for c in summary["checks"]}
    assert rc == 1 and by["db_least_privilege"] == "FAIL"


def test_smoke_reports_unreachable_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rc, summary = _run(tmp_path, monkeypatch, "--base-url", f"http://127.0.0.1:{_free_port()}", "--wait", "1")
    assert rc == 1 and summary["checks"][0] == {**summary["checks"][0], "name": "liveness", "status": "FAIL"}
