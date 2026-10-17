"""Versioned schema migrations for Postgres (pgvector) and Neo4j.

    python -m kgrag.migrate up        # apply pending migrations to both stores (deploy step / Cloud Run Job)
    python -m kgrag.migrate status    # show applied / pending, exit 1 if anything is pending or drifted
    python -m kgrag.migrate up --target postgres

Files live in kgrag/migrations/{postgres,neo4j}/NNNN_name.{sql,cypher}; versions start at 1 and must
be contiguous. Postgres: each pending file runs in its own transaction together with its
`schema_migrations` row, under a session advisory lock so two concurrent runners cannot interleave;
`R__*.sql` files are repeatable (idempotent grants) and run after every `up`. Neo4j: statements must be
idempotent (IF NOT EXISTS); each runs in auto-commit (schema commands cannot share a transaction with
writes) and the version is recorded on a `(:SchemaMigration)` node. Applied files whose checksum
changed are reported as drift and stop the run: edit-in-place is not allowed, add a new version.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kgrag.config import get_settings

MIGRATIONS_DIR = Path(__file__).with_name("migrations")
_NAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.(sql|cypher)$")
EXPECTED_EMBED_DIM = 384  # vector(384) in 0001_chunks_pgvector.sql
ADVISORY_LOCK_KEY = 0x6B677261  # "kgra"

PG_BOOKKEEPING = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    int PRIMARY KEY,
    name       text NOT NULL,
    checksum   text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
)
"""


class MigrationError(RuntimeError):
    pass


class MigrationDrift(MigrationError):
    """An already-applied migration file was edited (checksum mismatch)."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    body: str
    checksum: str


def _checksum(body: str) -> str:
    return hashlib.sha256(body.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def load_migrations(kind: str, root: Path = MIGRATIONS_DIR) -> list[Migration]:
    ext = {"postgres": "sql", "neo4j": "cypher"}[kind]
    out: list[Migration] = []
    for f in sorted((root / kind).iterdir()):
        if f.name.startswith("R__") or not f.is_file():
            continue
        m = _NAME.match(f.name)
        if not m or m.group(3) != ext:
            raise MigrationError(f"bad migration file name: {kind}/{f.name}")
        body = f.read_text(encoding="utf-8")
        out.append(Migration(int(m.group(1)), m.group(2), body, _checksum(body)))
    versions = [m.version for m in out]
    if versions != list(range(1, len(out) + 1)):
        raise MigrationError(f"{kind} migration versions must be contiguous from 0001, got {versions}")
    return out


def load_repeatables(root: Path = MIGRATIONS_DIR) -> list[tuple[str, str]]:
    return [(f.name, f.read_text(encoding="utf-8")) for f in sorted((root / "postgres").glob("R__*.sql"))]


def plan(migrations: list[Migration], applied: dict[int, str]) -> list[Migration]:
    """Pending migrations, after checking that every applied one still matches its file."""
    by_version = {m.version: m for m in migrations}
    for version, checksum in sorted(applied.items()):
        m = by_version.get(version)
        if m is None:
            raise MigrationDrift(f"version {version} is applied but its file is missing")
        if m.checksum != checksum:
            raise MigrationDrift(f"version {version} ({m.name}) was edited after it was applied")
    return [m for m in migrations if m.version not in applied]


def split_cypher(body: str) -> list[str]:
    lines = [ln for ln in body.splitlines() if not ln.strip().startswith("//")]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]


# --------------------------------------------------------------------------- postgres
def migrate_postgres(dsn: str | None = None, dry_run: bool = False) -> list[str]:
    import psycopg

    s = get_settings()
    if s.embed_dim != EXPECTED_EMBED_DIM:
        raise MigrationError(f"embed_dim {s.embed_dim} != {EXPECTED_EMBED_DIM} baked into 0001; add a migration")
    migrations = load_migrations("postgres")
    done: list[str] = []
    with psycopg.connect(dsn or s.pg_dsn, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))
        try:
            conn.execute(PG_BOOKKEEPING)
            applied = {int(v): str(c) for v, c in conn.execute("SELECT version, checksum FROM schema_migrations")}
            pending = plan(migrations, applied)
            if dry_run:
                return [f"{m.version:04d}_{m.name}" for m in pending]
            for m in pending:
                with conn.transaction():
                    conn.execute(m.body.encode("utf-8"))  # no parameters: multi-statement file is allowed
                    conn.execute(
                        "INSERT INTO schema_migrations (version, name, checksum) VALUES (%s, %s, %s)",
                        (m.version, m.name, m.checksum),
                    )
                done.append(f"{m.version:04d}_{m.name}")
            for name, body in load_repeatables():
                with conn.transaction():
                    conn.execute(body.encode("utf-8"))
                done.append(name)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))
    return done


# --------------------------------------------------------------------------- neo4j
def migrate_neo4j(runner: Any | None = None, dry_run: bool = False) -> list[str]:
    from kgrag.graph_store import Neo4jRunner

    s = get_settings()
    own = runner is None
    r = runner or Neo4jRunner(s.neo4j_uri, s.neo4j_user, s.neo4j_password, s.neo4j_database)
    try:
        r.run(
            "CREATE CONSTRAINT schema_migration_version IF NOT EXISTS "
            "FOR (m:SchemaMigration) REQUIRE m.version IS UNIQUE",
            {},
        )
        rows = r.run("MATCH (m:SchemaMigration) RETURN m.version AS version, m.checksum AS checksum", {})
        applied = {int(x["version"]): str(x["checksum"]) for x in rows}
        pending = plan(load_migrations("neo4j"), applied)
        if dry_run:
            return [f"{m.version:04d}_{m.name}" for m in pending]
        done = []
        for m in pending:
            for stmt in split_cypher(m.body):
                r.run(stmt, {})
            r.run(
                "MERGE (m:SchemaMigration {version: $v}) SET m.name = $n, m.checksum = $c, m.applied_at = datetime()",
                {"v": m.version, "n": m.name, "c": m.checksum},
            )
            done.append(f"{m.version:04d}_{m.name}")
        return done
    finally:
        if own:
            r.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m kgrag.migrate")
    ap.add_argument("cmd", choices=["up", "status"])
    ap.add_argument("--target", choices=["all", "postgres", "neo4j"], default="all")
    args = ap.parse_args(argv)
    from kgrag.logs import configure_logging, get_logger

    configure_logging()
    log = get_logger("kgrag.migrate")
    dry = args.cmd == "status"
    rc = 0
    for target, fn in (("postgres", migrate_postgres), ("neo4j", migrate_neo4j)):
        if args.target not in ("all", target):
            continue
        try:
            names = fn(dry_run=dry)
        except MigrationError as exc:
            log.error("migration failed", extra={"fields": {"target": target, "error": str(exc)}})
            return 2
        key = "pending" if dry else "applied"
        log.info(f"{target} migrations {key}", extra={"fields": {"target": target, key: names}})
        if dry and names:
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
