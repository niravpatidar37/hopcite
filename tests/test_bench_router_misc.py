from __future__ import annotations

import json
from pathlib import Path

import pytest

from kgrag.bench import GoldItem, Question, load_questions, percentile, score
from kgrag.corpus import FetchError, check_url, extract_section
from kgrag.router import EntityCatalog, Route, Router, redact_pii
from tests.test_no_model_cypher import CATALOG_ROWS, FakeLLM

ROOT = Path(__file__).resolve().parents[1]


def _q(atype: str, gold: list[GoldItem] | None = None, num: int | None = None) -> Question:
    return Question(
        id="t",
        category="single_hop",
        question="?",
        answer_type=atype,  # type: ignore[arg-type]
        gold=gold or [],
        gold_number=num,
    )


TSMC = GoldItem(name="TSMC", aliases=["Taiwan Semiconductor Manufacturing Company Limited"])
SAMSUNG = GoldItem(name="Samsung", aliases=["Samsung Electronics"])


def test_set_scoring_alias_aware() -> None:
    preds = ["Taiwan Semiconductor Manufacturing Company Limited", "Samsung Electronics Co."]
    s = score(_q("set", [TSMC, SAMSUNG]), False, "", preds)
    assert s.correct and s.f1 == 1.0


def test_set_scoring_penalizes_extras() -> None:
    s = score(_q("set", [TSMC]), False, "", ["TSMC", "Intel", "UMC", "GlobalFoundries"])
    assert not s.correct and s.recall == 1.0 and s.precision == 0.25


def test_refusal_scoring() -> None:
    assert score(_q("refuse"), True, "", []).correct
    assert not score(_q("refuse"), False, "Canberra", ["Canberra"]).correct
    assert not score(_q("set", [TSMC]), True, "", []).correct


def test_number_and_text_fallback() -> None:
    assert score(_q("number", num=6), False, "", ["6"]).correct
    assert score(_q("number", num=6), False, "Six companies do.", []).correct
    s = score(_q("single", [TSMC]), False, "It is TSMC.", [])
    assert s.correct and s.used_text_fallback


def test_percentile() -> None:
    assert percentile([1, 2, 3, 4, 5], 50) == 3
    assert percentile([10.0] * 20 + [100.0], 95) == pytest.approx(10.0)


def test_committed_benchmark_is_well_formed() -> None:
    qs = load_questions(ROOT / "bench" / "questions.jsonl")
    assert 50 <= len(qs) <= 100
    cats = {q.category for q in qs}
    assert cats == {"single_hop", "two_hop", "three_hop", "aggregation", "out_of_scope"}
    for q in qs:
        if q.category == "out_of_scope":
            assert q.answer_type == "refuse"
        else:
            assert q.evidence and (q.gold or q.gold_number is not None)
            assert q.provenance


def test_router_few_shots_do_not_leak_benchmark_questions() -> None:
    from kgrag.router import FEW_SHOT

    bench = [set(q.question.lower().split()) for q in load_questions(ROOT / "bench" / "questions.jsonl")]
    for shot, _ in FEW_SHOT:
        words = set(shot.lower().split())
        assert max(len(words & b) / len(words | b) for b in bench) < 0.5, shot


def test_low_confidence_falls_back_to_both_paths() -> None:
    out = {
        "route": "graph",
        "confidence": 0.3,
        "query_type": "neighbors",
        "entities": ["NVIDIA"],
        "hop_relations": [["MANUFACTURES_FOR"]],
        "direction": "in",
        "target_type": "Company",
    }
    r = Router(FakeLLM([out]), "m", EntityCatalog.from_rows(CATALOG_ROWS), 0.6)  # type: ignore[arg-type]
    d = r.route("Which foundries does NVIDIA use?")
    assert d.route == Route.HYBRID and d.fallback_reason.startswith("low_confidence")
    assert d.entity_ids == ["company:nvidia"]


def test_router_model_chain_falls_back_to_second_model() -> None:
    from kgrag.llm import LLMError

    class FlakyLLM:
        def __init__(self) -> None:
            self.models: list[str] = []

        def complete_json(self, messages, model, schema_cls, **kw):  # type: ignore[no-untyped-def]
            self.models.append(model)
            if model == "primary":
                raise LLMError("HTTP 429: retry-after 180s exceeds latency budget")
            return schema_cls.model_validate(
                {
                    "route": "graph",
                    "confidence": 0.9,
                    "query_type": "neighbors",
                    "entities": ["AMD"],
                    "hop_relations": [["COMPETES_WITH"]],
                    "direction": "out",
                    "target_type": "Company",
                }
            )

    llm = FlakyLLM()
    r = Router(llm, "primary", EntityCatalog.from_rows(CATALOG_ROWS), 0.6, fallback_models=["backup"])  # type: ignore[arg-type]
    d = r.route("Who competes with AMD?")
    assert llm.models == ["primary", "backup"] and d.model_used == "backup"
    assert d.route == Route.GRAPH and d.fallback_reason.startswith("router_model_fallback")


def test_router_failure_falls_back_to_hybrid() -> None:
    r = Router(None, "m", EntityCatalog.from_rows(CATALOG_ROWS), 0.6)
    d = r.route("Which foundries does AMD use?")
    assert d.route == Route.HYBRID and d.entity_ids == ["company:amd"]


def test_routing_log_redacts_pii(tmp_path: Path) -> None:
    from kgrag.pipeline import Pipeline

    log = tmp_path / "routing.jsonl"
    p = Pipeline(None, None, None, log_path=log)  # type: ignore[arg-type]

    class R:
        question = "Email jane.doe@example.com or call +1 (408) 555-1234 about TSMC"
        mode, route, fallback_reason, template, entity_ids = "vector", "vector", "", None, []
        graph_facts, retrieved_chunk_ids, refused, citations, regenerations = [], [], True, [], 0
        latency_ms, usd, router_model_used = 1.0, 0.0, None

    p._log(R(), None)  # type: ignore[arg-type]
    row = json.loads(log.read_text())
    assert "jane.doe" not in row["question"] and "555-1234" not in row["question"]
    assert redact_pii("ssn 123-45-6789") == "ssn [SSN]"


def test_ssrf_guard() -> None:
    check_url("https://www.sec.gov/Archives/edgar/data/1/x.htm")
    for bad in [
        "http://www.sec.gov/x",
        "https://sec.gov.evil.com/x",
        "https://169.254.169.254/latest",
        "https://www.sec.gov:8443/x",
        "file:///etc/passwd",
    ]:
        with pytest.raises(FetchError):
            check_url(bad)


def test_extract_section_skips_table_of_contents() -> None:
    text = (
        "Item 1. Business 3\nItem 1A. Risk Factors 10\n...\nItem 1. Business\n"
        + "Real content. " * 50
        + "\nItem 1A. Risk Factors\nRisks..."
    )
    sec = extract_section(text, "10-K")
    assert sec.startswith("Item 1. Business") and "Real content" in sec and "Risks" not in sec
