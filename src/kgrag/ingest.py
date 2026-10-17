"""Ingestion CLI: chunk -> (estimate) -> extract -> resolve -> write Neo4j + pgvector.

uv run python -m kgrag.ingest estimate     # cost estimate (+ pilot of 2 batches with --pilot)
uv run python -m kgrag.ingest extract      # full extraction pass (cached by document hash)
uv run python -m kgrag.ingest build        # entity resolution + MERGE into Neo4j + pgvector
"""

from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path
from typing import Any

from kgrag.chunking import Chunk, chunk_document, relation_score
from kgrag.config import REPO_ROOT, get_settings
from kgrag.corpus import FILER_NAMES, ManifestEntry, load_manifest, load_section
from kgrag.extract import (
    MAX_OUTPUT_TOKENS,
    Batch,
    ExtractionPayload,
    Extractor,
    ValidationReport,
    estimate_batch_tokens,
    make_batches,
    validate_payload,
)
from kgrag.llm import LLMClient, LLMError
from kgrag.ontology import EntityType, RelationType
from kgrag.resolve import EntityResolver, Mention
from kgrag.spend import BudgetExceeded, price_for

DATA = REPO_ROOT / "data"
EXTRACTED = DATA / "extracted"
RESULTS = REPO_ROOT / "results"
MIN_RELATION_SCORE = 2
LEAD_CHUNKS_PER_DOC = 2


def build_chunks(manifest: list[ManifestEntry]) -> list[Chunk]:
    chunks: list[Chunk] = []
    for e in manifest:
        chunks.extend(chunk_document(e.doc_id, e.section, load_section(DATA, e)))
    return chunks


def select_for_extraction(chunks: list[Chunk]) -> list[Chunk]:
    """Paid extraction only runs on relation-bearing chunks (plus each document's opening chunks,
    which carry headquarters / segment facts). All chunks still go into the vector index."""
    by_doc: dict[str, list[Chunk]] = defaultdict(list)
    for c in chunks:
        by_doc[c.doc_id].append(c)
    keep: list[Chunk] = []
    for cs in by_doc.values():
        for c in cs:
            if c.ordinal < LEAD_CHUNKS_PER_DOC or relation_score(c.text) >= MIN_RELATION_SCORE:
                keep.append(c)
    return keep


def all_batches(manifest: list[ManifestEntry], chunks: list[Chunk]) -> list[Batch]:
    sel = select_for_extraction(chunks)
    by_doc: dict[str, list[Chunk]] = defaultdict(list)
    for c in sel:
        by_doc[c.doc_id].append(c)
    batches: list[Batch] = []
    for e in manifest:
        batches.extend(
            make_batches(e.doc_id, FILER_NAMES.get(e.ticker, e.company), e.section_sha256, by_doc.get(e.doc_id, []))
        )
    return batches


def cmd_estimate(pilot: int) -> None:
    s = get_settings()
    manifest = load_manifest(DATA / "manifest.json")
    chunks = build_chunks(manifest)
    batches = all_batches(manifest, chunks)
    price = price_for("groq", s.extraction_model)
    est_in = sum(estimate_batch_tokens(b) for b in batches)
    worst = price.cost(est_in, MAX_OUTPUT_TOKENS * len(batches))
    out: dict[str, Any] = {
        "model": s.extraction_model,
        "documents": len(manifest),
        "chunks_total": len(chunks),
        "chunks_selected_for_extraction": sum(len(b.chunks) for b in batches),
        "batches": len(batches),
        "estimated_input_tokens": est_in,
        "worst_case_usd_if_every_call_hits_max_tokens": round(worst, 4),
        "price_per_m": {"input": price.input_per_m, "output": price.output_per_m},
    }
    if pilot:
        llm = LLMClient("groq")
        ex = Extractor(llm, s.extraction_model, EXTRACTED)
        sample = batches[:: max(1, len(batches) // pilot)][:pilot]
        for b in sample:
            ex.extract(b)
        paid_in, paid_out = ex.stats.input_tokens, ex.stats.output_tokens
        if ex.stats.calls and paid_in:
            ratio_in = paid_in / sum(estimate_batch_tokens(b) for b in sample)
            out_per_call = paid_out / ex.stats.calls
            proj = price.cost(int(est_in * ratio_in), int(out_per_call * len(batches)))
            out["pilot"] = {
                "batches": len(sample),
                "calls": ex.stats.calls,
                "input_tokens": paid_in,
                "output_tokens": paid_out,
                "usd": round(ex.stats.usd, 5),
                "projected_full_run_usd": round(proj, 4),
            }
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "cost_estimate.json").write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(out, indent=2))


def cmd_extract(limit: int | None) -> None:
    s = get_settings()
    manifest = load_manifest(DATA / "manifest.json")
    chunks = build_chunks(manifest)
    batches = all_batches(manifest, chunks)[:limit]
    llm = LLMClient("groq")
    ex = Extractor(llm, s.extraction_model, EXTRACTED)
    t0 = time.time()
    aborted = ""
    for i, b in enumerate(batches):
        try:
            ex.extract(b)
        except (LLMError, BudgetExceeded) as exc:  # rate-limit exhaustion or spend cap: stop cleanly
            aborted = f"{b.doc_id}#{b.index}: {type(exc).__name__}: {str(exc)[:200]}"
            print("ABORT", aborted, flush=True)
            break
        if i % 5 == 0:
            print(f"[{i + 1}/{len(batches)}] {b.doc_id} usd={ex.stats.usd:.4f} t={time.time() - t0:.0f}s", flush=True)
    rep = {
        "batches": len(batches),
        "paid_calls": ex.stats.calls,
        "cached_calls": ex.stats.cached_calls,
        "input_tokens": ex.stats.input_tokens,
        "output_tokens": ex.stats.output_tokens,
        "usd_this_run": round(ex.stats.usd, 5),
        "failures": ex.failures,
        "validation": asdict(ex.report),
        "wall_s": round(time.time() - t0, 1),
        "aborted": aborted,
    }
    print(json.dumps(rep, indent=2))
    (RESULTS / "extraction_run.json").write_text(json.dumps(rep, indent=2) + "\n", encoding="utf-8")


def load_extractions(batches: list[Batch], report: ValidationReport) -> list[tuple[Batch, ExtractionPayload]]:
    out = []
    ex = Extractor.__new__(Extractor)
    ex.cache_dir = EXTRACTED
    for b in batches:
        raw = Extractor.cached(ex, b)
        if raw is not None:
            out.append((b, validate_payload(raw, b, report)))
    return out


def cmd_build() -> None:
    from kgrag.embeddings import get_embedder
    from kgrag.graph_store import GraphStore, Neo4jRunner
    from kgrag.vector_store import VectorStore

    s = get_settings()
    manifest = load_manifest(DATA / "manifest.json")
    meta = {e.doc_id: e for e in manifest}
    chunks = build_chunks(manifest)
    batches = all_batches(manifest, chunks)
    report = ValidationReport()
    extractions = load_extractions(batches, report)
    print(f"{len(extractions)}/{len(batches)} batches have cached extractions")

    emb = get_embedder()
    # Every filer is a known Company even if no batch mentioned it by name.
    mentions = [Mention(FILER_NAMES.get(e.ticker, e.company), EntityType.COMPANY, [e.ticker]) for e in manifest]
    for _, p in extractions:
        for ent in p.entities:
            mentions.append(Mention(ent.name, ent.type, list(ent.aliases)))
    resolver = EntityResolver(emb.embed_docs, threshold=s.er_threshold)
    entities = resolver.fit(mentions)

    chunk_entities: dict[str, set[str]] = defaultdict(set)
    rel_rows: dict[RelationType, dict[tuple[str, str], dict[str, Any]]] = defaultdict(dict)
    for _, p in extractions:
        for ent in p.entities:
            eid = resolver.resolve(ent.name, ent.type)
            if eid:
                chunk_entities[ent.chunk_id].add(eid)
        for r in p.relations:
            a, b = resolver.resolve(r.subject, r.subject_type), resolver.resolve(r.object, r.object_type)
            if not a or not b or a == b:
                continue
            row = rel_rows[r.relation].setdefault(
                (a, b), {"src": a, "dst": b, "chunk_ids": [], "evidence": [], "confidence": 0.0}
            )
            if r.chunk_id not in row["chunk_ids"]:
                row["chunk_ids"].append(r.chunk_id)
                row["evidence"].append(r.evidence[:200])
            row["confidence"] = max(row["confidence"], r.confidence)

    runner = Neo4jRunner(s.neo4j_uri, s.neo4j_user, s.neo4j_password, s.neo4j_database)
    g = GraphStore(runner)
    g.ensure_schema()  # versioned migrations (idempotent)
    g.write_documents(
        [
            {
                "id": e.doc_id,
                "company": FILER_NAMES.get(e.ticker, e.company),
                "form": e.form,
                "filing_date": e.filing_date,
                "url": e.url,
            }
            for e in manifest
        ]
    )
    g.write_chunks(
        [{"id": c.chunk_id, "doc_id": c.doc_id, "section_path": c.section_path, "ordinal": c.ordinal} for c in chunks]
    )
    g.write_entities(
        [
            {"id": e.id, "name": e.name, "type": e.type.value, "aliases": e.aliases, "mention_count": e.mention_count}
            for e in entities.values()
        ]
    )
    g.write_mentions([{"entity_id": eid, "chunk_id": cid} for cid, eids in chunk_entities.items() for eid in eids])
    for rel, by_pair in rel_rows.items():
        g.write_relations(rel, list(by_pair.values()))
    stats = g.stats()
    runner.close()

    vs = VectorStore(s.pg_dsn, s.embed_dim, s.hnsw_ef_search)
    vs.ensure_schema()  # versioned migrations (idempotent)
    t0 = time.time()
    vecs = emb.embed_docs([c.text for c in chunks])
    embed_s = time.time() - t0
    rows = []
    for c, v in zip(chunks, vecs, strict=True):
        m = meta[c.doc_id]
        rows.append(
            {
                "chunk_id": c.chunk_id,
                "doc_id": c.doc_id,
                "company": FILER_NAMES.get(m.ticker, m.company),
                "form": m.form,
                "filing_date": m.filing_date,
                "section_path": c.section_path,
                "ordinal": c.ordinal,
                "text": c.text,
                "sha256": c.sha256,
                "entity_ids": sorted(chunk_entities.get(c.chunk_id, set())),
                "embedding": v,
            }
        )
    vs.upsert(rows)
    n = vs.count()
    vs.close()

    (DATA / "entities.json").write_text(
        json.dumps(
            [
                {"id": e.id, "name": e.name, "type": e.type.value, "aliases": e.aliases, "mentions": e.mention_count}
                for e in sorted(entities.values(), key=lambda x: x.id)
            ],
            indent=1,
        )
        + "\n",
        encoding="utf-8",
    )
    summary = {
        "documents": len(manifest),
        "chunks": len(chunks),
        "pg_rows": n,
        "batches_with_extractions": len(extractions),
        "mentions": len(mentions),
        "resolved_entities": len(entities),
        "graph": stats,
        "validation": asdict(report),
        "embed_seconds": round(embed_s, 1),
        "er_threshold": s.er_threshold,
    }
    (RESULTS / "ingest_summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("estimate")
    e.add_argument("--pilot", type=int, default=0)
    x = sub.add_parser("extract")
    x.add_argument("--limit", type=int, default=None)
    sub.add_parser("build")
    args = ap.parse_args()
    if args.cmd == "estimate":
        cmd_estimate(args.pilot)
    elif args.cmd == "extract":
        cmd_extract(args.limit)
    else:
        cmd_build()


if __name__ == "__main__":
    main()


__all__ = ["Path", "all_batches", "build_chunks", "select_for_extraction"]
