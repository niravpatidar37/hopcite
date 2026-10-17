"""Query pipelines: hybrid (router -> graph templates and/or vectors -> grounded answer) and the
vector-only baseline. Both share the same answerer, vector index and citation validation so the
benchmark compares retrieval, not prompts."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from kgrag.answer import Answerer, AnswerResult, Fact, dedupe_passages, paths_to_facts
from kgrag.embeddings import Embedder
from kgrag.graph_store import GraphStore, Hop, rows_to_hops
from kgrag.llm import CallStats
from kgrag.redact import redact_pii
from kgrag.router import Route, Router, RoutingDecision
from kgrag.vector_store import Passage, VectorStore

RoutingSink = Callable[[dict[str, Any]], None]
_log = logging.getLogger("kgrag.pipeline")

GRAPH_PASSAGES = 2  # passages from chunks that mention the linked entities
FACT_CHUNK_PASSAGES = 2  # passages backing the graph facts (so citations resolve to real text)


@dataclass
class AskResult:
    question: str
    mode: str
    route: str
    answer: str
    refused: bool
    answer_entities: list[str]
    citations: list[dict[str, Any]]
    claims: list[dict[str, Any]]
    graph_facts: list[str]
    retrieved_chunk_ids: list[str]
    template: str | None
    entity_ids: list[str]
    fallback_reason: str
    latency_ms: float
    router_model_used: str | None
    usd: float
    llm_calls: int
    cached_llm_calls: int
    input_tokens: int
    output_tokens: int
    regenerations: int
    citation_errors: list[str] = field(default_factory=list)
    timings_ms: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Pipeline:
    def __init__(
        self,
        vectors: VectorStore,
        embedder: Embedder,
        answerer: Answerer,
        graph: GraphStore | None = None,
        router: Router | None = None,
        top_k: int = 8,
        log_path: Path | None = None,
        log_sink: RoutingSink | None = None,
    ) -> None:
        self.vectors = vectors
        self.embedder = embedder
        self.answerer = answerer
        self.graph = graph
        self.router = router
        self.top_k = top_k
        self.log_path = log_path
        self.log_sink = log_sink  # e.g. the Postgres routing_log table on Cloud Run; else JSONL at log_path

    # ------------------------------------------------------------------ graph retrieval
    def graph_retrieve(self, decision: RoutingDecision) -> tuple[list[list[Hop]], str | None]:
        if self.graph is None:
            raise RuntimeError("graph store not configured")
        plan = decision.plan
        paths: list[list[Hop]] = []
        template = None
        if plan is not None:
            template = plan.template
            paths = rows_to_hops(self.graph.run_template(plan.template, plan.params))
        if not paths and decision.entity_ids:
            # Fallback: one-hop neighbourhood of each linked entity, relation filter from the plan.
            rels = plan.params.get("rels", []) if plan else []
            for eid in decision.entity_ids[:2]:
                params = {"entity_id": eid, "rels": rels, "target_type": None, "limit": 40}
                paths += rows_to_hops(self.graph.run_template("neighbors_any", params))
                if not paths and rels:
                    params["rels"] = []
                    paths += rows_to_hops(self.graph.run_template("neighbors_any", params))
            template = (template + "+" if template else "") + "neighbors_any(fallback)"
        return paths, template

    # ------------------------------------------------------------------ public entrypoints
    def ask(self, question: str, mode: str = "hybrid") -> AskResult:
        t0 = time.perf_counter()
        stats = CallStats()
        timings: dict[str, float] = {}
        if mode == "vector" or self.router is None or self.graph is None:
            decision = None
            route = Route.VECTOR
        else:
            ts = time.perf_counter()
            decision = self.router.route(question, stats)
            timings["route_ms"] = (time.perf_counter() - ts) * 1000
            route = decision.route

        ts = time.perf_counter()
        qvec = self.embedder.embed_query(question)
        timings["embed_ms"] = (time.perf_counter() - ts) * 1000

        facts: list[Fact] = []
        passages: list[Passage] = []
        template = None
        if route == Route.OUT_OF_SCOPE:
            pass  # refuse without retrieval; the answerer returns the refusal for empty context
        else:
            if route in (Route.GRAPH, Route.HYBRID) and decision is not None:
                ts = time.perf_counter()
                paths, template = self.graph_retrieve(decision)
                facts = paths_to_facts(paths)
                fact_chunks = []
                for f in facts:
                    for c in f.chunk_ids:
                        if c not in fact_chunks:
                            fact_chunks.append(c)
                # Passages that back the facts, ranked by similarity to the question.
                passages += self.vectors.search_within(qvec, fact_chunks, k=FACT_CHUNK_PASSAGES)
                # Graph -> text: chunks mentioning the linked entities.
                if decision.entity_ids and self.graph is not None:
                    mention_chunks = self._chunks_mentioning(decision.entity_ids)
                    passages += self.vectors.search_within(qvec, mention_chunks, k=GRAPH_PASSAGES)
                timings["graph_ms"] = (time.perf_counter() - ts) * 1000
            # Vector passages are always merged in: graph-linked passages first, then the vector
            # top-k fill the shared passage budget. The router decides whether the graph runs.
            ts = time.perf_counter()
            passages += self.vectors.search(qvec, k=self.top_k)
            timings["vector_ms"] = (time.perf_counter() - ts) * 1000
        # Same passage budget for both systems; the hybrid's extra context is the graph facts.
        passages = dedupe_passages(passages, limit=self.top_k)

        ts = time.perf_counter()
        if route == Route.OUT_OF_SCOPE:
            ans = self.answerer.answer(question, [], [], stats)
        else:
            ans = self.answerer.answer(question, facts, passages, stats)
        timings["answer_ms"] = (time.perf_counter() - ts) * 1000
        res = self._result(question, mode, route, decision, ans, facts, passages, template, stats, t0, timings)
        self._log(res, decision)
        return res

    def _chunks_mentioning(self, entity_ids: list[str]) -> list[str]:
        if self.graph is None:
            raise RuntimeError("graph store not configured")
        rows = self.graph.run_template("chunks_mentioning", {"entity_ids": entity_ids[:3]})
        return [r["id"] for r in rows]

    def _result(
        self,
        question: str,
        mode: str,
        route: Route,
        decision: RoutingDecision | None,
        ans: AnswerResult,
        facts: list[Fact],
        passages: list[Passage],
        template: str | None,
        stats: CallStats,
        t0: float,
        timings: dict[str, float],
    ) -> AskResult:
        by_id = {p.chunk_id: p for p in passages}
        missing = [c for c in ans.citations if c not in by_id]
        if missing:
            by_id.update(self.vectors.get(missing))
        citations = [
            {
                "chunk_id": c,
                "doc_id": by_id[c].doc_id if c in by_id else c.split("#")[0],
                "company": by_id[c].company if c in by_id else "",
                "snippet": (by_id[c].text[:240] if c in by_id else ""),
            }
            for c in ans.citations
        ]
        return AskResult(
            question=question,
            mode=mode,
            route=route.value,
            answer=ans.answer,
            refused=ans.refused,
            answer_entities=ans.answer_entities,
            citations=citations,
            claims=[c.model_dump() for c in ans.claims],
            graph_facts=[f.sentence for f in facts],
            retrieved_chunk_ids=ans.context_chunk_ids,
            template=template,
            entity_ids=decision.entity_ids if decision else [],
            fallback_reason=decision.fallback_reason if decision else "",
            router_model_used=decision.model_used if decision else None,
            latency_ms=round((time.perf_counter() - t0) * 1000, 1),
            usd=round(stats.usd, 6),
            llm_calls=stats.calls,
            cached_llm_calls=stats.cached_calls,
            input_tokens=stats.input_tokens,
            output_tokens=stats.output_tokens,
            regenerations=max(0, ans.attempts - 1),
            citation_errors=ans.citation_errors,
            timings_ms={k: round(v, 1) for k, v in timings.items()},
        )

    def _log(self, res: AskResult, decision: RoutingDecision | None) -> None:
        """Every routing decision with the question and its outcome (PII-redacted question).
        Goes to `log_sink` when set (a failing sink never fails the request), else JSONL at log_path."""
        if self.log_path is None and self.log_sink is None:
            return
        out = decision.output if decision else None
        row = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "mode": res.mode,
            "question": redact_pii(res.question),
            "model_route": out.route.value if out else None,
            "model_confidence": out.confidence if out else None,
            "query_type": out.query_type.value if out else None,
            "hop_relations": [[r.value for r in hop] for hop in out.hop_relations] if out else None,
            "final_route": res.route,
            "fallback_reason": res.fallback_reason,
            "router_model_used": res.router_model_used,
            "template": res.template,
            "entity_ids": res.entity_ids,
            "n_graph_facts": len(res.graph_facts),
            "n_passages": len(res.retrieved_chunk_ids),
            "outcome": "refused" if res.refused else "answered",
            "n_citations": len(res.citations),
            "regenerations": res.regenerations,
            "latency_ms": res.latency_ms,
            "usd": res.usd,
        }
        if self.log_sink is not None:
            try:
                self.log_sink(row)
            except Exception as exc:  # observability must not take the answer path down
                _log.warning("routing log sink failed", extra={"fields": {"error": type(exc).__name__}})
            return
        if self.log_path is None:
            return
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
