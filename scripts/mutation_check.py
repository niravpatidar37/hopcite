"""Mutation check for the security controls: break each control one way at a time, run the unit
tests, and require at least one failure (KILLED). Writes results/mutation_check.json.

Usage: uv run python scripts/mutation_check.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

MUTANTS: list[tuple[str, str, str, str]] = [
    (
        "src/kgrag/router.py",
        "bind raw model entity string as node id",
        'base["entity_id"] = entity_ids[0]',
        'base["entity_id"] = out.entities[0] if out.entities else entity_ids[0]',
    ),
    (
        "src/kgrag/router.py",
        "skip entity resolution (model string used as id)",
        "eid = self.catalog.resolve(name)",
        "eid = name",
    ),
    (
        "src/kgrag/graph_store.py",
        "run arbitrary text when the template key is unknown",
        "        if key not in TEMPLATES:\n"
        '            raise KeyError(f"unknown template {key}")\n'
        "        cypher = TEMPLATES[key]\n",
        "        cypher = TEMPLATES.get(key, key)\n",
    ),
    (
        "src/kgrag/graph_store.py",
        "relation type for writes not validated against the enum",
        "rel = RelationType(rel)  # raises on anything that is not an ontology member",
        "rel = rel",
    ),
    (
        "src/kgrag/answer.py",
        "accept citations that were never retrieved",
        "            if cid not in allowed:",
        "            if False:",
    ),
    (
        "src/kgrag/answer.py",
        "do not neutralize fence-breaking tags in passages",
        'return _FENCE_BREAK.sub("[tag removed]", text)[:MAX_PASSAGE_CHARS]',
        "return text[:MAX_PASSAGE_CHARS]",
    ),
    (
        "src/kgrag/api.py",
        "API key not enforced",
        "if not x_api_key or not hmac.compare_digest(x_api_key.encode(), key.encode()):",
        "if False:",
    ),
    (
        "src/kgrag/api.py",
        "non-loopback clients served without a key",
        "    if not _is_loopback(client):",
        "    if False:",
    ),
    ("src/kgrag/spend.py", "global cap not enforced", "if total + estimate_usd > self.global_cap:", "if False:"),
    (
        "src/kgrag/spend.py",
        "corrupt ledger fails open",
        "                usd = self.project_cap",
        "                usd = 0.0",
    ),
    (
        "src/kgrag/llm.py",
        "budget not checked before a paid call",
        "            self.ledger.check(worst_case)  # raises BudgetExceeded",
        "            pass",
    ),
    (
        "src/kgrag/extract.py",
        "unsupported (invented) evidence accepted",
        "        if not evidence_supported(r.evidence, texts[r.chunk_id]):",
        "        if False:",
    ),
]


def main() -> int:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("VIRTUAL_ENV", "PYTHON", "UV_"))}
    cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-x", "tests"]
    results = []
    for rel, label, old, new in MUTANTS:
        p = ROOT / rel
        orig = p.read_text(encoding="utf-8")
        assert orig.count(old) == 1, f"{label}: anchor found {orig.count(old)} times"
        p.write_text(orig.replace(old, new), encoding="utf-8", newline="\n")
        try:
            r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, env=env, timeout=600)
            out = r.stdout + r.stderr
            failed = [ln for ln in out.splitlines() if ln.startswith("FAILED")]
            status = "KILLED" if r.returncode and failed else ("ERROR" if r.returncode else "SURVIVED")
            results.append({"mutant": label, "file": rel, "status": status, "failing_tests": failed[:3]})
            print(status, label, failed[:1])
        finally:
            p.write_text(orig, encoding="utf-8", newline="\n")
    (ROOT / "results" / "mutation_check.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    survived = [r for r in results if r["status"] != "KILLED"]
    print(f"{len(results) - len(survived)}/{len(results)} mutants killed")
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main())
