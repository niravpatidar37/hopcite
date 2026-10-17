"""Benchmark questions, automatic scoring, and aggregation.

Scoring is deterministic (no LLM judge):
- refuse questions: correct iff the system refused.
- number questions: correct iff the predicted number equals the gold number.
- set / single questions: set F1 between predicted answer_entities and gold items (alias-aware,
  whole-token containment); correct iff F1 >= 0.6. If the system returned no answer_entities but
  did answer, gold aliases are searched in the answer text instead (deterministic fallback).
An LLM judge is deliberately not used here; Project 4 (LLM-as-judge) consumes results/*.jsonl.
"""

from __future__ import annotations

import json
import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from kgrag.resolve import normalize

Category = Literal["single_hop", "two_hop", "three_hop", "aggregation", "out_of_scope"]
CATEGORIES: list[str] = ["single_hop", "two_hop", "three_hop", "aggregation", "out_of_scope"]
F1_CORRECT = 0.6


class GoldItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    aliases: list[str] = Field(default_factory=list)


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    doc_id: str
    quote: str


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    category: Category
    question: str
    answer_type: Literal["set", "single", "number", "refuse"]
    gold: list[GoldItem] = Field(default_factory=list)
    gold_number: int | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    reasoning: str = ""
    provenance: str = ""


def load_questions(path: Path) -> list[Question]:
    return [
        Question.model_validate_json(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def _forms(item: GoldItem) -> list[str]:
    return [f for f in {normalize(item.name), *(normalize(a) for a in item.aliases)} if f]


def _contains(hay: str, needle: str) -> bool:
    return f" {needle} " in f" {hay} "


def item_matches(pred: str, item: GoldItem) -> bool:
    p = normalize(pred)
    if not p:
        return False
    return any(p == f or _contains(p, f) or (len(p) >= 3 and _contains(f, p)) for f in _forms(item))


_NUM = re.compile(r"\b\d+\b")
_WORDS = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}


def _first_number(texts: list[str]) -> int | None:
    for t in texts:
        m = _NUM.search(t)
        if m:
            return int(m.group(0))
        for w, n in _WORDS.items():
            if re.search(rf"\b{w}\b", t.lower()):
                return n
    return None


@dataclass
class Score:
    correct: bool
    f1: float
    precision: float
    recall: float
    used_text_fallback: bool = False
    notes: str = ""


def score(q: Question, refused: bool, answer: str, entities: list[str]) -> Score:
    if q.answer_type == "refuse":
        return Score(refused, float(refused), float(refused), float(refused))
    if refused:
        return Score(False, 0.0, 0.0, 0.0, notes="refused in-scope question")
    if q.answer_type == "number":
        n = _first_number(entities) if entities else None
        fallback = False
        if n is None:
            n, fallback = _first_number([answer]), True
        ok = n is not None and n == q.gold_number
        return Score(ok, float(ok), float(ok), float(ok), fallback, notes=f"pred={n}")
    preds = [e for e in entities if normalize(e)]
    fallback = False
    if not preds:
        fallback = True
        text = normalize(answer)
        preds = [g.name for g in q.gold if any(_contains(text, f) for f in _forms(g))]
        if not preds:
            return Score(False, 0.0, 0.0, 0.0, True, notes="no entities")
    matched_gold = sum(any(item_matches(p, g) for p in preds) for g in q.gold)
    matched_pred = sum(any(item_matches(p, g) for g in q.gold) for p in preds)
    recall = matched_gold / len(q.gold) if q.gold else 0.0
    precision = matched_pred / len(preds) if preds else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return Score(f1 >= F1_CORRECT, round(f1, 4), round(precision, 4), round(recall, 4), fallback)


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    v = sorted(values)
    k = (len(v) - 1) * pct / 100
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


@dataclass
class SystemSummary:
    system: str
    n: int
    accuracy: float
    by_category: dict[str, dict[str, float]] = field(default_factory=dict)
    p50_ms: float = 0.0
    p95_ms: float = 0.0
    usd_total: float = 0.0
    usd_per_query: float = 0.0
    mean_f1: float = 0.0
    citation_valid_rate: float = 0.0


def summarize(system: str, rows: list[dict[str, Any]]) -> SystemSummary:
    lat = [r["latency_ms"] for r in rows]
    s = SystemSummary(
        system=system,
        n=len(rows),
        accuracy=round(sum(r["score"]["correct"] for r in rows) / max(1, len(rows)), 4),
        p50_ms=round(percentile(lat, 50), 1),
        p95_ms=round(percentile(lat, 95), 1),
        usd_total=round(sum(r["usd"] for r in rows), 6),
        mean_f1=round(statistics.mean(r["score"]["f1"] for r in rows) if rows else 0.0, 4),
    )
    s.usd_per_query = round(s.usd_total / max(1, len(rows)), 7)
    answered = [r for r in rows if not r["refused"]]
    s.citation_valid_rate = round(
        sum(1 for r in answered if not r["citation_errors"] and r["citations"]) / max(1, len(answered)), 4
    )
    for c in CATEGORIES:
        rs = [r for r in rows if r["category"] == c]
        if rs:
            s.by_category[c] = {
                "n": len(rs),
                "accuracy": round(sum(r["score"]["correct"] for r in rs) / len(rs), 4),
                "mean_f1": round(statistics.mean(r["score"]["f1"] for r in rs), 4),
                "p50_ms": round(percentile([r["latency_ms"] for r in rs], 50), 1),
            }
    return s


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
