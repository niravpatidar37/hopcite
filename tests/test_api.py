from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi.testclient import TestClient

from kgrag import config
from kgrag.api import create_app


@dataclass
class FakeResult:
    answer: str = "TSMC and Samsung."
    refused: bool = False
    route: str = "graph"
    mode: str = "hybrid"
    citations: list[dict[str, Any]] = field(
        default_factory=lambda: [
            {"chunk_id": "NVDA-10-K-2026#c0012", "doc_id": "NVDA-10-K-2026", "company": "NVIDIA", "snippet": "..."}
        ]
    )
    claims: list[dict[str, Any]] = field(default_factory=list)
    answer_entities: list[str] = field(default_factory=lambda: ["TSMC", "Samsung"])
    graph_facts: list[str] = field(default_factory=list)
    template: str | None = "neighbors_in"
    latency_ms: float = 12.0
    usd: float = 0.0


class FakePipeline:
    def __init__(self) -> None:
        self.questions: list[str] = []

    def ask(self, q: str, mode: str = "hybrid") -> FakeResult:
        self.questions.append(q)
        return FakeResult(mode=mode)


class FakeServices:
    def __init__(self) -> None:
        self.pipeline = FakePipeline()
        self.answer_provider, self.answer_model, self.router_model = "ollama", "m", "r"

        class R:
            def run(self, *a: Any) -> list[Any]:
                return [{"ok": 1}]

        class V:
            def ping(self) -> bool:
                return True

        self.runner, self.vectors = R(), V()


def _client(monkeypatch: pytest.MonkeyPatch, api_key: str = "", client_host: str = "127.0.0.1") -> TestClient:
    monkeypatch.setenv("KGRAG_API_KEY", api_key)
    monkeypatch.setattr(config, "_loaded", True)  # do not read the developer's .env in tests
    return TestClient(create_app(FakeServices()), client=(client_host, 50000))


def test_health(monkeypatch: pytest.MonkeyPatch) -> None:
    with _client(monkeypatch) as c:
        r = c.get("/health")
        assert r.status_code == 200 and r.json()["status"] == "ok"


def test_ask_loopback_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    with _client(monkeypatch) as c:
        r = c.post("/ask", json={"question": "Which foundries does NVIDIA use?"})
        assert r.status_code == 200
        body = r.json()
        assert body["route"] == "graph" and body["citations"][0]["chunk_id"] == "NVDA-10-K-2026#c0012"


def test_non_loopback_refused_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    with _client(monkeypatch, client_host="203.0.113.7") as c:
        assert c.post("/ask", json={"question": "Which foundries does NVIDIA use?"}).status_code == 403


@pytest.mark.parametrize("header,code", [(None, 401), ("wrong", 401), ("s3cret-key", 200)])
def test_api_key_required_when_set(monkeypatch: pytest.MonkeyPatch, header: str | None, code: int) -> None:
    with _client(monkeypatch, api_key="s3cret-key", client_host="203.0.113.7") as c:
        h = {"X-API-Key": header} if header else {}
        assert c.post("/ask", json={"question": "Which foundries does NVIDIA use?"}, headers=h).status_code == code


def test_api_key_also_required_from_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    with _client(monkeypatch, api_key="s3cret-key") as c:
        assert c.post("/ask", json={"question": "Which foundries does NVIDIA use?"}).status_code == 401


@pytest.mark.parametrize(
    "payload",
    [
        {"question": "x" * 501},
        {"question": "  "},
        {"question": "ok question", "mode": "cypher"},
        {"question": "ok question", "cypher": "MATCH (n) DETACH DELETE n"},
    ],
)
def test_input_validation(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> None:
    with _client(monkeypatch) as c:
        assert c.post("/ask", json=payload).status_code == 422


def test_control_characters_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = FakeServices()
    monkeypatch.setenv("KGRAG_API_KEY", "")
    monkeypatch.setattr(config, "_loaded", True)
    with TestClient(create_app(svc), client=("127.0.0.1", 1)) as c:
        assert c.post("/ask", json={"question": "Who\x00 supplies\x1b TSMC?"}).status_code == 200
    assert svc.pipeline.questions == ["Who supplies TSMC?"]
