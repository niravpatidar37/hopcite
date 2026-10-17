"""Wire stores, models and pipelines from settings."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any

from kgrag.answer import Answerer
from kgrag.config import Settings, get_settings
from kgrag.embeddings import get_embedder
from kgrag.graph_store import GraphStore, Neo4jRunner
from kgrag.llm import LLMClient
from kgrag.pipeline import Pipeline, RoutingSink
from kgrag.router import EntityCatalog, Router
from kgrag.vector_store import VectorStore

_routing_logger = logging.getLogger("kgrag.routing")


@dataclass
class Services:
    pipeline: Pipeline
    runner: Neo4jRunner
    vectors: VectorStore
    answer_provider: str
    answer_model: str
    router_model: str

    def close(self) -> None:
        """Graceful shutdown: return pooled DB connections and close the Neo4j driver."""
        for closer in (self.vectors.close, self.runner.close):
            try:
                closer()
            except Exception as exc:
                logging.getLogger("kgrag.service").warning(
                    "close failed", extra={"fields": {"error": type(exc).__name__}}
                )


def answer_backend() -> tuple[str, str]:
    """Answer model is configurable. Default: local Ollama llama3.1:8b ($0), because the Groq free
    tier (8K tokens/min, ~200K tokens/day per model, shared across projects) cannot carry a
    100-question x 2-system benchmark on gpt-oss-120b in one day. Cloud Run has no Ollama, so
    Terraform sets KGRAG_ANSWER_PROVIDER=groq."""
    provider = os.environ.get("KGRAG_ANSWER_PROVIDER", "ollama")
    default_model = "llama3.1:8b" if provider == "ollama" else get_settings().answer_model
    return provider, os.environ.get("KGRAG_ANSWER_MODEL", default_model)


def routing_sink(s: Settings, vectors: VectorStore) -> tuple[RoutingSink | None, bool]:
    """(sink, use_file). KGRAG_ROUTING_LOG: file (JSONL under logs/, local default), postgres
    (routing_log table; Cloud Run), stdout (structured log line), off."""
    mode = s.routing_log
    if mode == "postgres":
        return vectors.insert_routing_log, False
    if mode == "stdout":

        def _stdout(row: dict[str, Any]) -> None:
            _routing_logger.info("routing", extra={"fields": json.loads(json.dumps(row, default=str))})

        return _stdout, False
    if mode == "off":
        return None, False
    return None, True


def build_services(use_router_llm: bool = True, use_cache: bool | None = None) -> Services:
    s = get_settings()
    cache = s.llm_cache if use_cache is None else use_cache
    runner = Neo4jRunner(s.neo4j_uri, s.neo4j_user, s.neo4j_password, s.neo4j_database)
    graph = GraphStore(runner)
    vectors = VectorStore(s.pg_dsn, s.embed_dim, s.hnsw_ef_search, pool_max=s.pg_pool_max)
    catalog = EntityCatalog.from_rows(graph.entity_catalog())
    router_llm = (
        LLMClient("groq", use_cache=cache, timeout_s=15, max_retries=1, max_wait_s=8)
        if use_router_llm and os.environ.get("GROQ_API_KEY")
        else None
    )
    # Router has a tight latency budget: on rate limits/timeouts it fails fast and the pipeline
    # falls back to running both retrieval paths (logged as router_error).
    router = Router(
        router_llm, s.router_model, catalog, s.router_confidence_floor, fallback_models=list(s.router_fallback_models)
    )
    provider, model = answer_backend()
    answerer = Answerer(LLMClient(provider, timeout_s=180, use_cache=cache), model)
    sink, use_file = routing_sink(s, vectors)
    pipe = Pipeline(
        vectors,
        get_embedder(),
        answerer,
        graph,
        router,
        top_k=s.vector_top_k,
        log_path=s.log_dir / "routing.jsonl" if use_file else None,
        log_sink=sink,
    )
    return Services(pipe, runner, vectors, provider, model, s.router_model)
