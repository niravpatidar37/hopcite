"""Phase 2 check: recall@k of the pgvector index on a labeled set, and HNSW ef_search tuning.

Labeled set: each in-scope benchmark question's evidence quotes are mapped to the chunk(s) that
contain them (mechanically, by normalized substring match). recall@k = share of a question's
evidence chunks found in the top-k. HNSW quality = overlap of the ANN top-10 with exact search.

Usage: uv run python scripts/eval_recall.py   (stack running + `python -m kgrag.ingest build` done)
"""

from __future__ import annotations

import json
import re
import statistics
import time

from kgrag.bench import load_questions
from kgrag.config import REPO_ROOT, get_settings
from kgrag.corpus import load_manifest
from kgrag.embeddings import get_embedder
from kgrag.ingest import DATA, build_chunks
from kgrag.vector_store import VectorStore

KS = [1, 3, 5, 8, 10, 20]
EF = [10, 20, 40, 80, 200]


def norm(s: str) -> str:
    s = s.replace("“", '"').replace("”", '"').replace("’", "'")
    return re.sub(r"\s+", " ", s).strip().lower()


def main() -> None:
    s = get_settings()
    chunks = build_chunks(load_manifest(DATA / "manifest.json"))
    ntext = {c.chunk_id: norm(c.text) for c in chunks}
    qs = [q for q in load_questions(REPO_ROOT / "bench" / "questions.jsonl") if q.category != "out_of_scope"]
    labeled: list[tuple[str, str, str, set[str]]] = []
    unmapped = 0
    for q in qs:
        rel: set[str] = set()
        for ev in q.evidence:
            # Quotes can span a chunk boundary; fall back to the quote's first 60 characters.
            nq = norm(ev.quote)
            hits = {cid for cid, t in ntext.items() if cid.startswith(ev.doc_id) and nq in t}
            if not hits:
                hits = {cid for cid, t in ntext.items() if cid.startswith(ev.doc_id) and nq[:60] in t}
            if not hits:
                unmapped += 1
            rel |= hits
        if rel:
            labeled.append((q.id, q.category, q.question, rel))

    emb = get_embedder()
    vs = VectorStore(s.pg_dsn, s.embed_dim, s.hnsw_ef_search)
    qvecs = {qid: emb.embed_query(text) for qid, _, text, _ in labeled}

    per_cat: dict[str, dict[str, list[float]]] = {}
    overall: dict[str, list[float]] = {f"recall@{k}": [] for k in KS}
    for qid, cat, _, rel in labeled:
        ranked = [p.chunk_id for p in vs.search(qvecs[qid], k=max(KS), ef_search=200)]
        d = per_cat.setdefault(cat, {f"recall@{k}": [] for k in KS})
        for k in KS:
            r = len(rel & set(ranked[:k])) / len(rel)
            overall[f"recall@{k}"].append(r)
            d[f"recall@{k}"].append(r)

    hnsw: list[dict[str, float]] = []
    exact = {qid: [p.chunk_id for p in vs.search(v, k=10, exact=True)] for qid, v in qvecs.items()}
    for ef in EF:
        overlaps, lat = [], []
        for qid, v in qvecs.items():
            t0 = time.perf_counter()
            ann = [p.chunk_id for p in vs.search(v, k=10, ef_search=ef)]
            lat.append((time.perf_counter() - t0) * 1000)
            overlaps.append(len(set(ann) & set(exact[qid])) / 10)
        hnsw.append(
            {
                "ef_search": ef,
                "ann_recall@10_vs_exact": round(statistics.mean(overlaps), 4),
                "p50_ms": round(statistics.median(lat), 2),
            }
        )
    vs.close()

    out = {
        "labeled_questions": len(labeled),
        "unmapped_quotes": unmapped,
        "chunks_indexed": len(chunks),
        "embed_model": s.embed_model,
        "index": "hnsw (m=16, ef_construction=64, cosine)",
        "overall": {k: round(statistics.mean(v), 4) for k, v in overall.items()},
        "by_category": {
            c: {k: round(statistics.mean(v), 4) for k, v in d.items()} | {"n": len(d["recall@1"])}
            for c, d in per_cat.items()
        },
        "hnsw_ef_search": hnsw,
        "chosen_ef_search": s.hnsw_ef_search,
    }
    (REPO_ROOT / "results" / "recall.json").write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
