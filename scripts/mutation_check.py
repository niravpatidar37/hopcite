"""Mutation check for the security controls: break each control one way at a time, run the unit
tests, and require at least one failure (KILLED). Writes results/mutation_check.json.

Usage: uv run python scripts/mutation_check.py              # unit-testable mutants, all unit tests
       KGRAG_INTEGRATION=1 uv run python scripts/mutation_check.py --only-pg --out <file>
                                     # the shared-limit mutants that only a real Postgres can kill,
                                     # against tests/test_shared_limits_integration.py (CI runs this
                                     # in the `migrations` job)
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
        "if not x_api_key or not _key_matches(x_api_key, keys):",
        "if False:",
    ),
    (
        "src/kgrag/api.py",
        "non-loopback clients served without a key",
        "    if not _is_loopback(client):",
        "    if False:",
    ),
    ("src/kgrag/spend.py", "global cap not enforced", "if total + estimate_usd > global_cap:", "if False:"),
    (
        "src/kgrag/spend.py",
        "corrupt ledger fails open",
        "                usd = self.project_cap",
        "                usd = 0.0",
    ),
    (
        "src/kgrag/llm.py",
        "budget not reserved before a paid call",
        "            reservation = self.ledger.reserve(worst_case) if paid else None",
        "            reservation = None",
    ),
    (
        "src/kgrag/api.py",
        "non-loopback bind allowed without an API key",
        "    if is_loopback_host(s.api_host):\n        return\n",
        "    return\n",
    ),
    (
        "src/kgrag/api.py",
        "short API keys accepted for an off-host bind",
        "    if any(len(k) < MIN_API_KEY_CHARS for k in keys):",
        "    if False:",
    ),
    (
        "src/kgrag/api.py",
        "rate limit not enforced",
        "        if wait > 0:",
        "        if False:",
    ),
    (
        "src/kgrag/api.py",
        "kill switch ignored by the API",
        "        if kill_switch_on():\n            raise HTTPException(",
        "        if False:\n            raise HTTPException(",
    ),
    (
        "src/kgrag/llm.py",
        "kill switch ignored by the LLM client",
        "        if kill_switch_on():  # operator kill switch",
        "        if False:  # operator kill switch",
    ),
    (
        "src/kgrag/api.py",
        "readiness reports ready without checking dependencies",
        "        ready = all(checks.values())",
        "        ready = True",
    ),
    (
        "src/kgrag/logs.py",
        "PII not redacted from structured log fields",
        "        return redact_pii(value)\n    if isinstance(value, dict):",
        "        return value\n    if isinstance(value, dict):",
    ),
    (
        "src/kgrag/migrate.py",
        "edited (drifted) migration accepted",
        "        if m.checksum != checksum:",
        "        if False:",
    ),
    (
        "src/kgrag/extract.py",
        "unsupported (invented) evidence accepted",
        "        if not evidence_supported(r.evidence, texts[r.chunk_id]):",
        "        if False:",
    ),
    # ---- production hardening (shared limits, fake-LLM gate, request hygiene) ----
    (
        "src/kgrag/config.py",
        "fake LLM allowed in staging/prod",
        "    if env not in FAKE_LLM_ENVS:",
        "    if False:",
    ),
    (
        "src/kgrag/config.py",
        "fake LLM allowed on Cloud Run",
        '    if on_cloud_run():\n        raise UnsafeConfig("KGRAG_LLM_MODE=fake',
        '    if False:\n        raise UnsafeConfig("KGRAG_LLM_MODE=fake',
    ),
    (
        "src/kgrag/config.py",
        "per-instance rate limiter accepted in staging/prod",
        '        if s.rate_limit_backend != "postgres":',
        "        if False:",
    ),
    (
        "src/kgrag/config.py",
        "per-instance spend ledger accepted in staging/prod",
        '        if s.spend_backend != "postgres":',
        "        if False:",
    ),
    (
        "src/kgrag/ratelimit.py",
        "shared limiter fails open when Postgres is unreachable",
        "            raise LimiterUnavailable(type(exc).__name__) from exc",
        "            return 0.0",
    ),
    (
        "src/kgrag/api.py",
        "API serves /ask when the shared limiter is down",
        '                status_code=503, detail="rate limiter unavailable", headers={"Retry-After": "5"}\n',
        '                status_code=200, detail="rate limiter unavailable", headers={"Retry-After": "5"}\n',
    ),
    (
        "src/kgrag/ratelimit.py",
        "Postgres bucket never debited (no shared limit)",
        " * %(rate)s) - 1\n        ELSE",
        " * %(rate)s)\n        ELSE",
    ),
    (
        "src/kgrag/spend.py",
        "spend reservation not serialized across instances (no advisory lock)",
        '            conn.execute("SELECT pg_advisory_xact_lock(%s)", (SPEND_LOCK_KEY,))',
        "            pass",
    ),
    (
        "src/kgrag/llm.py",
        "possibly-billed call (timeout) releases its reservation",
        "            if reservation is not None and not exc.billed_unknown:",
        "            if reservation is not None:",
    ),
    (
        "src/kgrag/api.py",
        "oversize bodies accepted",
        "        if declared is not None and (not declared.isdigit() or int(declared) > MAX_BODY_BYTES):",
        "        if False:",
    ),
    (
        "src/kgrag/api.py",
        "security headers not set",
        "                response.headers.setdefault(k, v)",
        "                pass",
    ),
    (
        "src/kgrag/api.py",
        "unhandled errors leak the exception text",
        '        return JSONResponse({"detail": "internal error"}, status_code=500, headers=SECURITY_HEADERS)',
        '        return JSONResponse({"detail": str(exc)}, status_code=500, headers=SECURITY_HEADERS)',
    ),
    (
        "src/kgrag/migrate.py",
        "status gate ignores an over-privileged runtime DB role",
        "        rc = rc or (roles_rc if dry else 0)",
        "        rc = rc or 0",
    ),
    (
        "scripts/check_migrations.py",
        "migration guard ignores DROP",
        '    ("DROP", re.compile(r"\\bDROP\\b", re.IGNORECASE)),',
        '    ("DROP", re.compile(r"\\bNEVERMATCHES\\b", re.IGNORECASE)),',
    ),
]


# Mutants of SQL that only a real Postgres executes; unit tests cannot kill them.
PG_ONLY = {
    "Postgres bucket never debited (no shared limit)",
    "spend reservation not serialized across instances (no advisory lock)",
}
PG_TESTS = "tests/test_shared_limits_integration.py"


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--only-pg", action="store_true", help="run the Postgres-only mutants (KGRAG_INTEGRATION=1)")
    ap.add_argument("--out", default=str(ROOT / "results" / "mutation_check.json"))
    args = ap.parse_args()
    if args.only_pg and os.environ.get("KGRAG_INTEGRATION") != "1":
        print("--only-pg needs KGRAG_INTEGRATION=1 and a migrated Postgres", file=sys.stderr)
        return 2
    env = {k: v for k, v in os.environ.items() if not k.startswith(("VIRTUAL_ENV", "PYTHON", "UV_"))}
    target = PG_TESTS if args.only_pg else "tests"
    if not args.only_pg:
        env.pop("KGRAG_INTEGRATION", None)  # the unit run must not depend on a local stack
    cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-x", target]
    results = []
    for rel, label, old, new in MUTANTS:
        if (label in PG_ONLY) != args.only_pg:
            if label in PG_ONLY:
                results.append({"mutant": label, "file": rel, "status": "PG_ONLY", "failing_tests": [PG_TESTS]})
            continue
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
    Path(args.out).write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    ran = [r for r in results if r["status"] != "PG_ONLY"]
    survived = [r for r in ran if r["status"] != "KILLED"]
    print(f"{len(ran) - len(survived)}/{len(ran)} mutants killed" + (" (Postgres-only set)" if args.only_pg else ""))
    pg = len(results) - len(ran)
    if pg:
        print(f"{pg} Postgres-only mutants not run here: KGRAG_INTEGRATION=1 ... --only-pg (CI migrations job)")
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main())
