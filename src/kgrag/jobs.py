"""Batch entrypoints for Cloud Run Jobs (and local use).

    python -m kgrag.jobs ingest                      # migrate -> fetch corpus -> build (default steps)
    python -m kgrag.jobs ingest --steps fetch,extract,build

Steps
- migrate: versioned schema migrations for Postgres and Neo4j (idempotent).
- fetch:   SEC EDGAR -> data/raw + data/manifest.json (SSRF-guarded, <= 8 req/s; needs SEC_USER_AGENT).
- extract: paid gpt-oss-120b extraction for batches missing from the committed cache in
           data/extracted (spend-capped; refuses when the kill switch is on). Opt-in.
- build:   entity resolution + idempotent MERGE into Neo4j + upsert into pgvector.

With the committed extraction cache, `migrate,fetch,build` rebuilds the published graph and index for
$0 of LLM spend as long as EDGAR still serves the same filings (section hashes are checked against
the manifest by the cache key).
"""

from __future__ import annotations

import argparse
import sys

from kgrag.logs import configure_logging, get_logger

STEPS = ("migrate", "fetch", "extract", "build")
DEFAULT_STEPS = "migrate,fetch,build"


def run_ingest(steps: list[str]) -> int:
    log = get_logger("kgrag.jobs")
    unknown = [s for s in steps if s not in STEPS]
    if unknown:
        log.error("unknown steps", extra={"fields": {"unknown": unknown}})
        return 2
    for step in STEPS:  # fixed order regardless of how --steps was written
        if step not in steps:
            continue
        log.info("step start", extra={"fields": {"step": step}})
        if step == "migrate":
            from kgrag.migrate import main as migrate_main

            rc = migrate_main(["up"])
            if rc:
                return rc
        elif step == "fetch":
            from kgrag.config import REPO_ROOT
            from kgrag.corpus import fetch_corpus, write_manifest

            data = REPO_ROOT / "data"
            entries = fetch_corpus(data)
            write_manifest(entries, data / "manifest.json")
            log.info("fetched", extra={"fields": {"documents": len(entries)}})
        elif step == "extract":
            from kgrag.ingest import cmd_extract

            cmd_extract(None)
        elif step == "build":
            from kgrag.ingest import cmd_build

            cmd_build()
        log.info("step done", extra={"fields": {"step": step}})
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m kgrag.jobs")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ing = sub.add_parser("ingest")
    ing.add_argument("--steps", default=DEFAULT_STEPS, help=f"comma-separated subset of {','.join(STEPS)}")
    args = ap.parse_args(argv)
    configure_logging()
    return run_ingest([s.strip() for s in args.steps.split(",") if s.strip()])


if __name__ == "__main__":
    sys.exit(main())
