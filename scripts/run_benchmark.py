"""Phase 5: run the hybrid system and the vector-only baseline over the same question set.

Writes (all committed):
  results/benchmark_results.jsonl  one row per (system, question): answer, citations, route, score,
                                   latency, cost. This is the file Project 4 (LLM-as-judge) consumes.
  results/benchmark_summary.json   accuracy by category, p50/p95 latency, cost per query, ingestion cost.

Usage: uv run python scripts/run_benchmark.py [--systems hybrid,vector] [--limit N]
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict
from typing import Any

from kgrag.bench import load_questions, score, summarize, write_jsonl
from kgrag.config import REPO_ROOT
from kgrag.service import build_services
from kgrag.spend import SpendLedger

RESULTS = REPO_ROOT / "results"


def ledger_usd_by_model(ledger: SpendLedger) -> dict[str, float]:
    out: dict[str, float] = {}
    for line in ledger.path.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("project") == ledger.project:
            out[r["model"]] = out.get(r["model"], 0.0) + float(r["usd"])
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--systems", default="hybrid,vector")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--cache", action="store_true", help="reuse cached LLM responses (latency then meaningless)")
    args = ap.parse_args()
    systems = args.systems.split(",")
    questions = load_questions(REPO_ROOT / "bench" / "questions.jsonl")[: args.limit]
    svc = build_services(use_cache=args.cache)
    ledger = SpendLedger()
    spend_before = ledger.project_spend()
    rows: list[dict[str, Any]] = []
    t_start = time.time()
    # Interleave systems per question so drift (rate limits, GPU contention) hits both equally.
    for i, q in enumerate(questions, 1):
        for system in systems:
            res = svc.pipeline.ask(q.question, mode=system)
            sc = score(q, res.refused, res.answer, res.answer_entities)
            row = {
                "system": system,
                "question_id": q.id,
                "category": q.category,
                **res.to_dict(),
                "gold": [g.model_dump() for g in q.gold],
                "gold_number": q.gold_number,
                "answer_type": q.answer_type,
                "score": asdict(sc),
                "answer_model": f"{svc.answer_provider}/{svc.answer_model}",
                "router_model": svc.router_model if system == "hybrid" else None,
            }
            rows.append(row)
            mark = "OK " if sc.correct else "-- "
            print(
                f"[{i}/{len(questions)}] {system:6s} {mark}{q.id} {q.category:12s} route={res.route:12s} "
                f"f1={sc.f1:.2f} {res.latency_ms:7.0f}ms  {res.answer_entities[:4]}",
                flush=True,
            )
        write_jsonl(RESULTS / "benchmark_results.jsonl", rows)  # checkpoint every question

    summaries = {s: asdict(summarize(s, [r for r in rows if r["system"] == s])) for s in systems}
    extraction = json.loads((RESULTS / "extraction_run.json").read_text(encoding="utf-8"))
    ingest = json.loads((RESULTS / "ingest_summary.json").read_text(encoding="utf-8"))
    summary = {
        "questions": len(questions),
        "systems": summaries,
        "answer_model": f"{svc.answer_provider}/{svc.answer_model}",
        "router_model": f"groq/{svc.router_model}",
        "extraction_model": "groq/openai/gpt-oss-120b",
        "ingestion": {
            "documents": ingest["documents"],
            "chunks": ingest["chunks"],
            "resolved_entities": ingest["resolved_entities"],
            "graph": ingest["graph"],
            # Extraction is the only gpt-oss-120b use in this project (answers: local Ollama; router: 20b),
            # so the one-time ingestion cost is the ledger total for that model (includes the pilot).
            "extraction_usd_ledger": round(ledger_usd_by_model(ledger).get("openai/gpt-oss-120b", 0.0), 6),
            "extraction_paid_calls": extraction.get("paid_calls"),
            "extraction_input_tokens": extraction.get("input_tokens"),
            "extraction_output_tokens": extraction.get("output_tokens"),
        },
        "benchmark_spend_usd_ledger_delta": round(ledger.project_spend() - spend_before, 6),
        "wall_seconds": round(time.time() - t_start, 1),
    }
    (RESULTS / "benchmark_summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
