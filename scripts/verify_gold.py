"""Verify that every benchmark evidence quote occurs verbatim in the fetched source text, and that
each in-scope question's gold items are named in its evidence (traceability check).

Usage: uv run python scripts/verify_gold.py   (needs data/raw from scripts/fetch_corpus.py)
Writes results/gold_verification.json. Exit code 1 on any failure.
"""

from __future__ import annotations

import json
import re
import sys

from kgrag.bench import load_questions
from kgrag.config import REPO_ROOT
from kgrag.corpus import FILER_NAMES, load_manifest, load_section
from kgrag.resolve import normalize


def norm(s: str) -> str:
    s = s.replace("“", '"').replace("”", '"').replace("’", "'").replace("‘", "'")
    return re.sub(r"\s+", " ", s).strip()


def main() -> int:
    data = REPO_ROOT / "data"
    manifest = {e.doc_id: e for e in load_manifest(data / "manifest.json")}
    texts = {d: norm(load_section(data, e)) for d, e in manifest.items()}
    questions = load_questions(REPO_ROOT / "bench" / "questions.jsonl")
    failures: list[str] = []
    checked_quotes = 0
    for q in questions:
        if q.category != "out_of_scope" and not q.evidence:
            failures.append(f"{q.id}: no evidence")
        ev_text = ""
        for ev in q.evidence:
            checked_quotes += 1
            if ev.doc_id not in texts:
                failures.append(f"{q.id}: unknown doc {ev.doc_id}")
                continue
            if norm(ev.quote) not in texts[ev.doc_id]:
                failures.append(f"{q.id}: quote not found in {ev.doc_id}: {ev.quote[:80]!r}")
            # The filer itself is named implicitly ("we", "our") by its own document.
            ticker = ev.doc_id.split("-")[0]
            ev_text += " " + normalize(ev.quote) + " " + normalize(FILER_NAMES.get(ticker, "")) + " " + ticker.lower()
        for g in q.gold:
            forms = [normalize(x) for x in [g.name, *g.aliases]]
            if not any(f and f" {f} " in f" {ev_text} " for f in forms):
                failures.append(f"{q.id}: gold item {g.name!r} not named in its evidence")
    report = {"questions": len(questions), "quotes_checked": checked_quotes, "failures": failures, "ok": not failures}
    (REPO_ROOT / "results").mkdir(exist_ok=True)
    (REPO_ROOT / "results" / "gold_verification.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
