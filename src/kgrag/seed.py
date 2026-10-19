"""Small committed seed dataset for the smoke-test stack (CI has no EDGAR access and no LLM keys).

    python -m kgrag.seed export smoke/fixtures/seed.json --docs NVDA-10-K-2026,SMCI-10-K-2026,TSM-20-F-2025
    python -m kgrag.seed load /fixtures/seed.json

`export` reads a subset of the real local index (documents, chunks with their filing text, the
entities mentioned in them, and the relations whose evidence chunks are all inside the subset) from
Neo4j and Postgres. `load` writes it back with the same idempotent MERGE / upsert code as ingestion and
re-embeds the chunk text with the baked embedding model, so the fixture holds no vectors and stays
small. SEC filings are public documents; the fixture holds excerpts of three Business sections.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from kgrag.config import get_settings
from kgrag.ontology import RelationType

FORMAT_VERSION = 1


def export(out: Path, doc_ids: list[str]) -> dict[str, int]:
    import psycopg

    from kgrag.graph_store import Neo4jRunner

    s = get_settings()
    with psycopg.connect(s.pg_dsn) as conn:
        rows = conn.execute(
            "SELECT chunk_id, doc_id, company, form, filing_date::text, section_path, ordinal, text, sha256, "
            "entity_ids FROM chunks WHERE doc_id = ANY(%s) ORDER BY doc_id, ordinal",
            (doc_ids,),
        ).fetchall()
    chunks = [
        {
            "chunk_id": r[0],
            "doc_id": r[1],
            "company": r[2],
            "form": r[3],
            "filing_date": r[4],
            "section_path": r[5],
            "ordinal": r[6],
            "text": r[7],
            "sha256": r[8],
            "entity_ids": list(r[9]),
        }
        for r in rows
    ]
    chunk_set = {c["chunk_id"] for c in chunks}
    runner = Neo4jRunner(s.neo4j_uri, s.neo4j_user, s.neo4j_password, s.neo4j_database)
    try:
        docs = runner.run(
            "MATCH (d:Document) WHERE d.id IN $ids RETURN d.id AS id, d.company AS company, d.form AS form, "
            "d.filing_date AS filing_date, d.url AS url ORDER BY id",
            {"ids": doc_ids},
        )
        mentions = runner.run(
            "MATCH (e:Entity)-[:MENTIONED_IN]->(c:Chunk) WHERE c.id IN $ids "
            "RETURN e.id AS entity_id, c.id AS chunk_id ORDER BY entity_id, chunk_id",
            {"ids": sorted(chunk_set)},
        )
        rels = runner.run(
            "MATCH (a:Entity)-[r]->(b:Entity) WHERE any(c IN r.chunk_ids WHERE c IN $ids) "
            "RETURN a.id AS src, b.id AS dst, type(r) AS rel, r.chunk_ids AS chunk_ids, r.evidence AS evidence, "
            "r.confidence AS confidence ORDER BY rel, src, dst",
            {"ids": sorted(chunk_set)},
        )
        relations = []
        for r in rels:
            kept = [c for c in r["chunk_ids"] if c in chunk_set]
            if kept:
                relations.append({**r, "chunk_ids": kept, "evidence": list(r.get("evidence") or [])[: len(kept)]})
        entity_ids = sorted(
            {m["entity_id"] for m in mentions} | {r["src"] for r in relations} | {r["dst"] for r in relations}
        )
        entities = runner.run(
            "MATCH (e:Entity) WHERE e.id IN $ids RETURN e.id AS id, e.name AS name, e.type AS type, "
            "e.aliases AS aliases, e.mention_count AS mention_count ORDER BY id",
            {"ids": entity_ids},
        )
    finally:
        runner.close()
    known = {e["id"] for e in entities}
    for c in chunks:
        c["entity_ids"] = [e for e in c["entity_ids"] if e in known]
    data = {
        "format_version": FORMAT_VERSION,
        "source": "subset of the local hopcite index (SEC 10-K / 20-F Business sections)",
        "documents": docs,
        "chunks": chunks,
        "entities": entities,
        "mentions": mentions,
        "relations": relations,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return {k: len(v) for k, v in data.items() if isinstance(v, list)}


def load(path: Path) -> dict[str, int]:
    from kgrag.embeddings import get_embedder
    from kgrag.graph_store import GraphStore, Neo4jRunner
    from kgrag.vector_store import VectorStore

    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    if data.get("format_version") != FORMAT_VERSION:
        raise ValueError(f"unsupported seed format {data.get('format_version')!r}")
    s = get_settings()
    runner = Neo4jRunner(s.neo4j_uri, s.neo4j_user, s.neo4j_password, s.neo4j_database)
    try:
        g = GraphStore(runner)
        g.write_documents(data["documents"])
        g.write_chunks(
            [
                {"id": c["chunk_id"], "doc_id": c["doc_id"], "section_path": c["section_path"], "ordinal": c["ordinal"]}
                for c in data["chunks"]
            ]
        )
        g.write_entities(data["entities"])
        g.write_mentions(data["mentions"])
        by_rel: dict[str, list[dict[str, Any]]] = {}
        for r in data["relations"]:
            by_rel.setdefault(r["rel"], []).append(r)
        for rel, rows in by_rel.items():
            g.write_relations(RelationType(rel), rows)  # enum-checked relation types only
    finally:
        runner.close()
    emb = get_embedder()
    vecs = emb.embed_docs([c["text"] for c in data["chunks"]])
    vs = VectorStore(s.pg_dsn, s.embed_dim, s.hnsw_ef_search)
    try:
        vs.upsert([{**c, "embedding": v} for c, v in zip(data["chunks"], vecs, strict=True)])
        n = vs.count()
    finally:
        vs.close()
    return {"documents": len(data["documents"]), "chunks": len(data["chunks"]), "pg_rows": n}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m kgrag.seed")
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export")
    e.add_argument("out", type=Path)
    e.add_argument("--docs", required=True, help="comma-separated doc ids")
    ld = sub.add_parser("load")
    ld.add_argument("path", type=Path)
    args = ap.parse_args(argv)
    if args.cmd == "export":
        print(json.dumps(export(args.out, [d.strip() for d in args.docs.split(",") if d.strip()])))
    else:
        print(json.dumps(load(args.path)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
