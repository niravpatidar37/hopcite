from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel

from kgrag import llm as llm_mod
from kgrag.llm import LLMClient, StructuredOutputError
from kgrag.spend import BudgetExceeded, SpendLedger, price_for
from tests.conftest import write_ledger


def test_global_cap_blocks(tmp_path: Path) -> None:
    p = tmp_path / "l.jsonl"
    write_ledger(p, [{"project": "p2-research", "usd": 24.99}])
    led = SpendLedger(p, project="p1-kgrag", global_cap=25.0, project_cap=5.0)
    with pytest.raises(BudgetExceeded, match="global"):
        led.check(0.02)
    led.check(0.005)  # still fits


def test_project_cap_blocks(tmp_path: Path) -> None:
    p = tmp_path / "l.jsonl"
    write_ledger(p, [{"project": "p1-kgrag", "usd": 4.99}, {"project": "p2-research", "usd": 1.0}])
    led = SpendLedger(p, project="p1-kgrag", global_cap=25.0, project_cap=5.0)
    with pytest.raises(BudgetExceeded, match="project cap"):
        led.check(0.02)


def test_corrupt_ledger_fails_closed(tmp_path: Path) -> None:
    p = tmp_path / "l.jsonl"
    p.write_text("{not json\n", encoding="utf-8")
    led = SpendLedger(p, project="p1-kgrag", global_cap=25.0, project_cap=5.0)
    with pytest.raises(BudgetExceeded):
        led.check(0.01)


def test_record_appends(ledger: SpendLedger) -> None:
    ledger.record("groq", "openai/gpt-oss-20b", 100, 50, 0.001)
    ledger.record("groq", "openai/gpt-oss-20b", 100, 50, 0.002)
    rows = [json.loads(x) for x in ledger.path.read_text().splitlines()]
    assert [r["usd"] for r in rows] == [0.001, 0.002]
    assert set(rows[0]) == {"ts", "project", "provider", "model", "input_tokens", "output_tokens", "usd"}
    assert ledger.project_spend() == pytest.approx(0.003)


def test_unknown_paid_model_refused() -> None:
    with pytest.raises(BudgetExceeded):
        price_for("groq", "some/unpriced-model")
    assert price_for("ollama", "anything").cost(10**6, 10**6) == 0.0


def _groq_ok(text: str, pin: int = 10, pout: int = 5) -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": text}}], "usage": {"prompt_tokens": pin, "completion_tokens": pout}},
    )


def test_budget_checked_before_any_http(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "test")
    p = tmp_path / "l.jsonl"
    write_ledger(p, [{"project": "p1-kgrag", "usd": 5.0}])
    sent: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        sent.append(req)
        return _groq_ok("hi")

    c = LLMClient(
        "groq", ledger=SpendLedger(p, "p1-kgrag", 25.0, 5.0), cache_dir=tmp_path, transport=httpx.MockTransport(handler)
    )
    with pytest.raises(BudgetExceeded):
        c.complete([{"role": "user", "content": "x"}], "openai/gpt-oss-20b", max_tokens=100)
    assert sent == []  # refused before the network


def test_cache_hit_is_free_and_unlogged(tmp_path: Path, ledger: SpendLedger, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "test")
    n = {"calls": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        n["calls"] += 1
        return _groq_ok("hello", 1000, 1000)

    c = LLMClient("groq", ledger=ledger, cache_dir=tmp_path, transport=httpx.MockTransport(handler))
    msgs = [{"role": "user", "content": "x"}]
    a = c.complete(msgs, "openai/gpt-oss-20b")
    b = c.complete(msgs, "openai/gpt-oss-20b")
    assert n["calls"] == 1 and b.cached and b.usd == 0.0 and a.text == b.text
    assert len(ledger.path.read_text().splitlines()) == 1
    assert a.usd == pytest.approx(price_for("groq", "openai/gpt-oss-20b").cost(1000, 1000))


def test_429_retry_after_honored(tmp_path: Path, ledger: SpendLedger, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "test")
    sleeps: list[float] = []
    monkeypatch.setattr(llm_mod.time, "sleep", lambda s: sleeps.append(s))
    seq = [httpx.Response(429, headers={"retry-after": "7"}), _groq_ok("ok")]

    def handler(req: httpx.Request) -> httpx.Response:
        return seq.pop(0)

    c = LLMClient("groq", ledger=ledger, cache_dir=tmp_path, transport=httpx.MockTransport(handler))
    assert c.complete([{"role": "user", "content": "x"}], "openai/gpt-oss-20b").text == "ok"
    assert sleeps and sleeps[0] >= 7


class Out(BaseModel):
    value: int


def test_schema_retry_then_success(tmp_path: Path, ledger: SpendLedger, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "test")
    seq = [_groq_ok('{"value": "nope"}'), _groq_ok('{"value": 3}')]
    c = LLMClient("groq", ledger=ledger, cache_dir=tmp_path, transport=httpx.MockTransport(lambda r: seq.pop(0)))
    assert c.complete_json([{"role": "user", "content": "x"}], "openai/gpt-oss-20b", Out).value == 3


def test_schema_failure_raises(tmp_path: Path, ledger: SpendLedger, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "test")
    c = LLMClient(
        "groq", ledger=ledger, cache_dir=tmp_path, transport=httpx.MockTransport(lambda r: _groq_ok("garbage"))
    )
    with pytest.raises(StructuredOutputError):
        c.complete_json([{"role": "user", "content": "x"}], "openai/gpt-oss-20b", Out, attempts=2)


def test_claude_adapter_off_without_key(tmp_path: Path, ledger: SpendLedger, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    c = LLMClient("anthropic", ledger=ledger, cache_dir=tmp_path)
    with pytest.raises(Exception, match=r"ANTHROPIC_API_KEY|placeholder"):
        c.complete([{"role": "user", "content": "x"}], "CLAUDE_MODEL_ID_PLACEHOLDER")
