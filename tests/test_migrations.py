"""Versioned migrations: file layout, checksums and drift detection, idempotent Neo4j steps.
Runs without services; tests/test_migrations_integration.py applies them to real databases."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from kgrag import migrate
from kgrag.config import Settings
from kgrag.migrate import (
    EXPECTED_EMBED_DIM,
    MIGRATIONS_DIR,
    MigrationDrift,
    MigrationError,
    load_migrations,
    load_repeatables,
    plan,
    split_cypher,
)


def test_postgres_migrations_are_contiguous_and_packaged() -> None:
    ms = load_migrations("postgres")
    assert [m.version for m in ms] == [1, 2]
    assert [m.name for m in ms] == ["chunks_pgvector", "routing_log"]
    assert MIGRATIONS_DIR.parent.name == "kgrag"  # shipped inside the package (non-editable installs)
    assert all(len(m.checksum) == 64 for m in ms)


def test_schema_covers_hnsw_and_routing_log() -> None:
    body = "\n".join(m.body for m in load_migrations("postgres"))
    assert "CREATE EXTENSION IF NOT EXISTS vector" in body
    assert re.search(r"USING hnsw \(embedding vector_cosine_ops\)", body)
    assert "CREATE TABLE IF NOT EXISTS routing_log" in body


def test_vector_dimension_matches_settings() -> None:
    body = load_migrations("postgres")[0].body
    assert f"vector({EXPECTED_EMBED_DIM})" in body
    assert Settings().embed_dim == EXPECTED_EMBED_DIM


def test_runtime_role_is_least_privilege() -> None:
    ((name, body),) = load_repeatables()
    assert name == "R__grants.sql"
    assert "GRANT SELECT ON chunks TO kgrag_app" in body
    assert "GRANT INSERT ON routing_log TO kgrag_app" in body
    assert not re.search(r"GRANT\s+(ALL|INSERT|UPDATE|DELETE)[^;]*ON chunks", body)


def test_plan_returns_pending_in_order() -> None:
    ms = load_migrations("postgres")
    assert plan(ms, {}) == ms
    assert plan(ms, {1: ms[0].checksum}) == ms[1:]
    assert plan(ms, {1: ms[0].checksum, 2: ms[1].checksum}) == []


def test_edited_migration_is_drift() -> None:
    ms = load_migrations("postgres")
    with pytest.raises(MigrationDrift, match="edited"):
        plan(ms, {1: "0" * 64})


def test_applied_migration_with_missing_file_is_drift() -> None:
    ms = load_migrations("postgres")
    with pytest.raises(MigrationDrift, match="missing"):
        plan(ms, {1: ms[0].checksum, 2: ms[1].checksum, 3: "x"})


def test_checksum_ignores_line_endings() -> None:
    assert migrate._checksum("a;\r\nb;\r\n") == migrate._checksum("a;\nb;\n")


@pytest.mark.parametrize(
    "files",
    [
        {"0001_a.sql": "", "0003_c.sql": ""},  # gap
        {"0002_b.sql": ""},  # does not start at 1
        {"0001_A.sql": ""},  # bad name
        {"1_a.sql": ""},  # bad name
        {"0001_a.cypher": ""},  # wrong extension for postgres
    ],
)
def test_bad_layouts_rejected(tmp_path: Path, files: dict[str, str]) -> None:
    (tmp_path / "postgres").mkdir()
    for n, b in files.items():
        (tmp_path / "postgres" / n).write_text(b, encoding="utf-8")
    with pytest.raises(MigrationError):
        load_migrations("postgres", tmp_path)


def test_neo4j_statements_are_idempotent() -> None:
    (m,) = load_migrations("neo4j")
    stmts = split_cypher(m.body)
    assert len(stmts) == 5
    assert all("IF NOT EXISTS" in s for s in stmts)
    assert not any(s.startswith("//") for s in stmts)


class _GraphFake:
    """Minimal Neo4j stand-in: remembers SchemaMigration nodes, records every statement."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.applied: dict[int, str] = {}

    def run(self, cypher: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        self.calls.append(cypher)
        if cypher.startswith("MATCH (m:SchemaMigration)"):
            return [{"version": v, "checksum": c} for v, c in self.applied.items()]
        if cypher.startswith("MERGE (m:SchemaMigration"):
            self.applied[params["v"]] = params["c"]
        return []

    def close(self) -> None:  # pragma: no cover - runner is caller-owned in these tests
        pass


def test_neo4j_migration_records_version_and_is_rerunnable() -> None:
    g = _GraphFake()
    assert migrate.migrate_neo4j(g) == ["0001_constraints"]
    n_first = len(g.calls)
    assert any("REQUIRE e.id IS UNIQUE" in c for c in g.calls)
    assert migrate.migrate_neo4j(g) == []
    assert not any("REQUIRE e.id IS UNIQUE" in c for c in g.calls[n_first:])


def test_neo4j_drift_stops_the_run() -> None:
    g = _GraphFake()
    g.applied[1] = "f" * 64
    with pytest.raises(MigrationDrift):
        migrate.migrate_neo4j(g)


def test_runtime_never_creates_schema() -> None:
    """Schema changes live in migrations only: no DDL in the request path."""
    src = Path(migrate.__file__).parent
    for mod in ("vector_store.py", "api.py", "service.py", "pipeline.py"):
        text = (src / mod).read_text(encoding="utf-8")
        assert "CREATE EXTENSION" not in text and "CREATE TABLE" not in text, mod


def test_cli_status_reports_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(migrate, "migrate_postgres", lambda dry_run=False: ["0002_routing_log"])
    monkeypatch.setattr(migrate, "migrate_neo4j", lambda dry_run=False: [])
    assert migrate.main(["status"]) == 1
    monkeypatch.setattr(migrate, "migrate_postgres", lambda dry_run=False: [])
    assert migrate.main(["status"]) == 0


def test_cli_drift_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(dry_run: bool = False) -> list[str]:
        raise MigrationDrift("version 1 edited")

    monkeypatch.setattr(migrate, "migrate_postgres", boom)
    assert migrate.main(["up", "--target", "postgres"]) == 2
