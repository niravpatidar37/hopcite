"""Wire stores, models and pipelines from settings."""

from __future__ import annotations

import os
from dataclasses import dataclass

from kgrag.answer import Answerer
from kgrag.config import get_settings
from kgrag.embeddings import get_embedder
from kgrag.graph_store import GraphStore, Neo4jRunner
from kgrag.llm import LLMClient
from kgrag.pipeline import Pipeline
from kgrag.router import EntityCatalog, Router
from kgrag.vector_store import VectorStore


@dataclass
class Services:
    pipeline: Pipeline
    runner: Neo4jRunner
    vectors: VectorStore
    answer_provider: str
    answer_model: str
    router_model: str


def answer_backend() -> tuple[str, str]:
    """Answer model is configurable. Default: local Ollama llama3.1:8b ($0), because the Groq free
    tier (8K tokens/min, ~200K tokens/day per model, shared across projects) cannot carry a
    100-question x 2-system benchmark on gpt-oss-120b in one day."""
    provider = os.environ.get("KGRAG_ANSWER_PROVIDER", "ollama")
    default_model = "llama3.1:8b" if provider == "ollama" else get_settings().answer_model
    return provider, os.environ.get("KGRAG_ANSWER_MODEL", default_model)


def build_services(use_router_llm: bool = True, use_cache: bool = True) -> Services:
    s = get_settings()
    runner = Neo4jRunner(s.neo4j_uri, s.neo4j_user, s.neo4j_password)
    graph = GraphStore(runner)
    vectors = VectorStore(s.pg_dsn, s.embed_dim, s.hnsw_ef_search)
    catalog = EntityCatalog.from_rows(graph.entity_catalog())
    router_llm = (
        LLMClient("groq", use_cache=use_cache, timeout_s=15, max_retries=1, max_wait_s=8)
        if use_router_llm and os.environ.get("GROQ_API_KEY")
        else None
    )
    # Router has a tight latency budget: on rate limits/timeouts it fails fast and the pipeline
    # falls back to running both retrieval paths (logged as router_error).
    router = Router(router_llm, s.router_model, catalog, s.router_confidence_floor)
    provider, model = answer_backend()
    answerer = Answerer(LLMClient(provider, timeout_s=180, use_cache=use_cache), model)
    pipe = Pipeline(
        vectors, get_embedder(), answerer, graph, router, top_k=s.vector_top_k, log_path=s.log_dir / "routing.jsonl"
    )
    return Services(pipe, runner, vectors, provider, model, s.router_model)
