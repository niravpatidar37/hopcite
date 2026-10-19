"""Migrations against real Postgres (pgvector) and Neo4j. Skipped unless KGRAG_INTEGRATION=1.

CI runs this in the `migrations` job against service containers; locally:
KGRAG_INTEGRATION=1 uv run pytest tests/test_migrations_integration.py
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from kgrag.migrate import MigrationDrift, migrate_neo4j, migrate_postgres

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("KGRAG_INTEGRATION") != "1", reason="needs postgres + neo4j"),
]


@pytest.fixture
def pg() -> Any:
    import psycopg

    from kgrag.config import get_settings

    with psycopg.connect(get_settings().pg_dsn, autocommit=True) as conn:
        yield conn


def test_postgres_up_is_idempotent(pg: Any) -> None:
    migrate_postgres()
    second = migrate_postgres()
    assert second == ["R__grants.sql"]  # only the repeatable step runs again
    rows = pg.execute("SELECT version, name FROM schema_migrations ORDER BY version").fetchall()
    assert rows == [(1, "chunks_pgvector"), (2, "routing_log"), (3, "shared_limits")]
    idx = {r[0] for r in pg.execute("SELECT indexname FROM pg_indexes WHERE tablename = 'chunks'")}
    assert {"chunks_hnsw_idx", "chunks_doc_idx", "chunks_entities_idx"} <= idx
    ext = pg.execute("SELECT extname FROM pg_extension WHERE extname = 'vector'").fetchone()
    assert ext == ("vector",)


def test_postgres_drift_detected(pg: Any) -> None:
    migrate_postgres()
    orig = pg.execute("SELECT checksum FROM schema_migrations WHERE version = 2").fetchone()[0]
    pg.execute("UPDATE schema_migrations SET checksum = %s WHERE version = 2", ("0" * 64,))
    try:
        with pytest.raises(MigrationDrift):
            migrate_postgres()
    finally:
        pg.execute("UPDATE schema_migrations SET checksum = %s WHERE version = 2", (orig,))


def test_routing_log_sink_writes_a_row(pg: Any) -> None:
    from kgrag.config import get_settings
    from kgrag.vector_store import VectorStore

    migrate_postgres()
    s = get_settings()
    vs = VectorStore(s.pg_dsn, s.embed_dim, s.hnsw_ef_search)
    try:
        assert vs.ping() is True
        vs.insert_routing_log(
            {
                "ts": "2026-10-17T00:00:00Z",
                "mode": "hybrid",
                "question": "itest: who supplies [EMAIL]?",
                "model_route": "graph",
                "model_confidence": 0.9,
                "query_type": "lookup",
                "hop_relations": [["SUPPLIES_TO"]],
                "final_route": "graph",
                "fallback_reason": "",
                "router_model_used": "openai/gpt-oss-20b",
                "template": "neighbors_in",
                "entity_ids": ["company:nvidia"],
                "n_graph_facts": 2,
                "n_passages": 8,
                "outcome": "answered",
                "n_citations": 1,
                "regenerations": 0,
                "latency_ms": 12.5,
                "usd": 0.00008,
            }
        )
        n = pg.execute("SELECT count(*) FROM routing_log WHERE question_redacted LIKE 'itest:%'").fetchone()[0]
        assert n >= 1
    finally:
        pg.execute("DELETE FROM routing_log WHERE question_redacted LIKE 'itest:%'")
        vs.close()


def test_neo4j_up_is_idempotent() -> None:
    from kgrag.config import get_settings
    from kgrag.graph_store import Neo4jRunner

    migrate_neo4j()
    assert migrate_neo4j() == []
    s = get_settings()
    r = Neo4jRunner(s.neo4j_uri, s.neo4j_user, s.neo4j_password, s.neo4j_database)
    try:
        names = {row["name"] for row in r.run("SHOW CONSTRAINTS YIELD name RETURN name", {})}
        assert {"entity_id", "chunk_id", "doc_id", "schema_migration_version"} <= names
        assert r.ping() is True
    finally:
        r.close()


def test_runtime_role_gets_only_its_grants(pg: Any) -> None:
    """R__grants.sql: kgrag_app may SELECT chunks and INSERT routing_log, nothing else, and loses a
    cloudsqlsuperuser membership (simulated here with a stand-in role)."""
    exists = pg.execute("SELECT 1 FROM pg_roles WHERE rolname IN ('kgrag_app', 'cloudsqlsuperuser')").fetchall()
    if exists:
        pytest.skip("refusing to modify pre-existing kgrag_app / cloudsqlsuperuser roles")
    pg.execute("CREATE ROLE kgrag_app NOLOGIN")
    pg.execute("CREATE ROLE cloudsqlsuperuser NOLOGIN")
    pg.execute("GRANT cloudsqlsuperuser TO kgrag_app")
    try:
        migrate_postgres()

        def priv(table: str, p: str) -> bool:
            return bool(pg.execute("SELECT has_table_privilege('kgrag_app', %s, %s)", (table, p)).fetchone()[0])

        assert priv("chunks", "SELECT") and not priv("chunks", "INSERT") and not priv("chunks", "DELETE")
        assert priv("routing_log", "INSERT") and not priv("routing_log", "SELECT")
        assert not priv("schema_migrations", "SELECT")
        member = pg.execute("SELECT pg_has_role('kgrag_app', 'cloudsqlsuperuser', 'MEMBER')").fetchone()[0]
        assert member is False
    finally:
        pg.execute("DROP OWNED BY kgrag_app")
        pg.execute("DROP ROLE kgrag_app")
        pg.execute("DROP ROLE cloudsqlsuperuser")


def test_role_check_reports_cloudsqlsuperuser_membership(pg: Any) -> None:
    """`python -m kgrag.migrate roles` / check_app_role: a runtime role that is a member of
    cloudsqlsuperuser (simulated with a stand-in role) is reported as not least-privilege."""
    from kgrag.migrate import check_app_role

    if pg.execute("SELECT 1 FROM pg_roles WHERE rolname IN ('cloudsqlsuperuser', 'kgrag_itest_app')").fetchall():
        pytest.skip("refusing to modify pre-existing cloudsqlsuperuser / kgrag_itest_app roles")
    pg.execute("CREATE ROLE cloudsqlsuperuser NOLOGIN")
    pg.execute("CREATE ROLE kgrag_itest_app NOLOGIN")
    try:
        assert check_app_role(role="kgrag_itest_app")["least_privilege"] is True
        pg.execute("GRANT cloudsqlsuperuser TO kgrag_itest_app")
        rep = check_app_role(role="kgrag_itest_app")
        assert rep["cloudsqlsuperuser"] is True and rep["least_privilege"] is False
        pg.execute("REVOKE cloudsqlsuperuser FROM kgrag_itest_app")  # the RUNBOOK 2e fix
        assert check_app_role(role="kgrag_itest_app")["least_privilege"] is True
        assert check_app_role(role="kgrag_no_such_role") == {
            "role": "kgrag_no_such_role",
            "exists": False,
            "least_privilege": None,
        }
    finally:
        pg.execute("DROP ROLE IF EXISTS kgrag_itest_app")
        pg.execute("DROP ROLE IF EXISTS cloudsqlsuperuser")
