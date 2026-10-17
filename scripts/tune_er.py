"""Entity-resolution threshold tuning.

  uv run python scripts/tune_er.py candidates   # writes bench/er_candidates.jsonl (pairs to label)
  uv run python scripts/tune_er.py evaluate     # reads bench/er_pairs.jsonl (labeled), writes results/er_tuning.json

Candidate pairs: distinct normalized Company names from the cached extractions whose bge-small
cosine is >= 0.75, plus every pair the alias/acronym rules would merge. Labels (same entity or
not) live in bench/er_pairs.jsonl with the labeler recorded.
"""

from __future__ import annotations

import itertools
import json
import sys

from kgrag.config import REPO_ROOT
from kgrag.corpus import load_manifest
from kgrag.embeddings import get_embedder
from kgrag.extract import ValidationReport
from kgrag.ingest import DATA, all_batches, build_chunks, load_extractions
from kgrag.ontology import EntityType
from kgrag.resolve import EntityResolver, Mention, normalize

THRESHOLDS = [0.80, 0.82, 0.84, 0.86, 0.88, 0.90, 0.92, 0.94, 0.96, 0.98, 1.01]


def company_mentions() -> list[Mention]:
    manifest = load_manifest(DATA / "manifest.json")
    ex = load_extractions(all_batches(manifest, build_chunks(manifest)), ValidationReport())
    return [Mention(e.name, e.type, list(e.aliases)) for _, p in ex for e in p.entities if e.type == EntityType.COMPANY]


def candidates() -> None:
    ms = company_mentions()
    names: dict[str, str] = {}
    for m in ms:
        names.setdefault(normalize(m.name), m.name)
    keys = sorted(k for k in names if k)
    vecs = get_embedder().embed_docs(keys)
    sims = vecs @ vecs.T
    rule = EntityResolver(None).fit(ms)
    rule_pairs = set()
    for ent in rule.values():
        ks = sorted({normalize(a) for a in ent.aliases} & set(keys))
        rule_pairs |= set(itertools.combinations(ks, 2))
    out = []
    for i, j in itertools.combinations(range(len(keys)), 2):
        pair = (keys[i], keys[j])
        if sims[i, j] >= 0.75 or pair in rule_pairs:
            out.append(
                {
                    "a": names[keys[i]],
                    "b": names[keys[j]],
                    "cosine": round(float(sims[i, j]), 4),
                    "rule_merge": pair in rule_pairs,
                }
            )
    out.sort(key=lambda r: -r["cosine"])
    path = REPO_ROOT / "bench" / "er_candidates.jsonl"
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in out), encoding="utf-8")
    print(f"{len(keys)} distinct company keys, {len(out)} candidate pairs -> {path}")


def prf(pred: list[bool], gold: list[bool]) -> dict[str, float]:
    tp = sum(p and g for p, g in zip(pred, gold, strict=True))
    fp = sum(p and not g for p, g in zip(pred, gold, strict=True))
    fn = sum(g and not p for p, g in zip(pred, gold, strict=True))
    p = tp / (tp + fp) if tp + fp else 1.0
    r = tp / (tp + fn) if tp + fn else 1.0
    f = 2 * p * r / (p + r) if p + r else 0.0
    return {"precision": round(p, 4), "recall": round(r, 4), "f1": round(f, 4), "tp": tp, "fp": fp, "fn": fn}


def evaluate() -> None:
    pairs = [
        json.loads(x)
        for x in (REPO_ROOT / "bench" / "er_pairs.jsonl").read_text(encoding="utf-8").splitlines()
        if x.strip()
    ]
    gold = [bool(p["same"]) for p in pairs]
    emb = get_embedder()
    rows = []
    for t in THRESHOLDS:
        emb_only, full = [], []
        for p in pairs:
            r_emb = EntityResolver(emb.embed_docs, threshold=t, use_acronyms=False)
            r_emb.fit([Mention(p["a"], EntityType.COMPANY), Mention(p["b"], EntityType.COMPANY)])
            emb_only.append(r_emb.resolve(p["a"], EntityType.COMPANY) == r_emb.resolve(p["b"], EntityType.COMPANY))
            r_full = EntityResolver(emb.embed_docs, threshold=t, use_acronyms=True)
            r_full.fit([Mention(p["a"], EntityType.COMPANY), Mention(p["b"], EntityType.COMPANY)])
            full.append(r_full.resolve(p["a"], EntityType.COMPANY) == r_full.resolve(p["b"], EntityType.COMPANY))
        rows.append(
            {"threshold": t, "normalize+embedding": prf(emb_only, gold), "normalize+rules+embedding": prf(full, gold)}
        )
    norm_only = [normalize(p["a"]) == normalize(p["b"]) for p in pairs]
    # Max F1, then precision; among ties keep the LOWEST threshold that is still <= 1.0 so the
    # embedding rule stays active (1.01 = embedding disabled, reported for comparison only).
    active = [r for r in rows if r["threshold"] <= 1.0]
    best = max(
        active,
        key=lambda r: (
            r["normalize+rules+embedding"]["f1"],
            r["normalize+rules+embedding"]["precision"],
            -r["threshold"],
        ),
    )
    out = {
        "pairs": len(pairs),
        "positives": sum(gold),
        "normalize_only": prf(norm_only, gold),
        "sweep": rows,
        "chosen_threshold": best["threshold"],
        "note": "Declared-alias merges (text says 'X (\"AMD\")') happen before this and are not scored here.",
    }
    (REPO_ROOT / "results" / "er_tuning.json").write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    {"candidates": candidates, "evaluate": evaluate}[sys.argv[1]]()
