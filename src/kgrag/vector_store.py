"""pgvector store. Same chunk_id as the Neo4j :Chunk nodes. HNSW index (cosine), tunable ef_search.

The schema (table, HNSW index, routing_log) is owned by the versioned migrations in
kgrag/migrations/postgres, applied by `python -m kgrag.migrate up` (a deploy step / Cloud Run Job),
never at import or connect time. Connections come from a small pool that checks each connection on
checkout, so a dropped Cloud SQL connection is replaced instead of failing every later request.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import numpy.typing as npt
from psycopg.types.json import Jsonb

UPSERT = """
INSERT INTO chunks (chunk_id, doc_id, company, form, filing_date, section_path, ordinal, text, sha256,
                    entity_ids, embedding)
VALUES (%(chunk_id)s, %(doc_id)s, %(company)s, %(form)s, %(filing_date)s, %(section_path)s, %(ordinal)s,
        %(text)s, %(sha256)s, %(entity_ids)s, %(embedding)s)
ON CONFLICT (chunk_id) DO UPDATE SET
    text = EXCLUDED.text, sha256 = EXCLUDED.sha256, entity_ids = EXCLUDED.entity_ids,
    embedding = EXCLUDED.embedding, section_path = EXCLUDED.section_path
"""


INSERT_ROUTING_LOG = """
INSERT INTO routing_log (ts, mode, question_redacted, model_route, model_confidence, query_type, hop_relations,
                         final_route, fallback_reason, router_model_used, template, entity_ids, n_graph_facts,
                         n_passages, outcome, n_citations, regenerations, latency_ms, usd)
VALUES (%(ts)s, %(mode)s, %(question)s, %(model_route)s, %(model_confidence)s, %(query_type)s, %(hop_relations)s,
        %(final_route)s, %(fallback_reason)s, %(router_model_used)s, %(template)s, %(entity_ids)s, %(n_graph_facts)s,
        %(n_passages)s, %(outcome)s, %(n_citations)s, %(regenerations)s, %(latency_ms)s, %(usd)s)
"""


# Privilege report for a database role. Cloud SQL makes every API-created user a member of
# cloudsqlsuperuser; the runtime role must not keep that (or any role-level superpower). The LEFT JOIN
# keeps the query valid on plain Postgres, where cloudsqlsuperuser does not exist.
ROLE_REPORT_SQL = """
SELECT r.rolname, r.rolsuper, r.rolcreaterole, r.rolcreatedb, r.rolbypassrls,
       COALESCE(pg_has_role(r.oid, c.oid, 'MEMBER'), false) AS cloudsqlsuperuser
FROM pg_roles r LEFT JOIN pg_roles c ON c.rolname = 'cloudsqlsuperuser'
WHERE r.rolname = COALESCE(%(role)s::text, current_user::text)
"""
ROLE_FLAGS = ("superuser", "createrole", "createdb", "bypassrls", "cloudsqlsuperuser")


def role_report(conn: Any, role: str | None = None) -> dict[str, Any] | None:
    """Privilege flags of `role` (default: the connected user). None when the role does not exist."""
    row = conn.execute(ROLE_REPORT_SQL, {"role": role}).fetchone()
    if row is None:
        return None
    flags = dict(zip(ROLE_FLAGS, (bool(v) for v in row[1:]), strict=True))
    return {"role": row[0], **flags, "least_privilege": not any(flags.values())}


@dataclass
class Passage:
    chunk_id: str
    doc_id: str
    company: str
    section_path: str
    text: str
    score: float
    entity_ids: list[str]


class VectorStore:
    def __init__(self, dsn: str, dim: int = 384, ef_search: int = 40, pool_max: int = 4) -> None:
        from pgvector.psycopg import register_vector
        from psycopg_pool import ConnectionPool

        self.dim = dim
        self.ef_search = ef_search
        self._pool: Any = ConnectionPool(
            dsn,
            min_size=1,
            max_size=max(1, pool_max),
            kwargs={"autocommit": True},
            configure=register_vector,
            check=ConnectionPool.check_connection,
            timeout=10.0,
            open=True,
            name="kgrag-pg",
        )

    def ensure_schema(self) -> None:
        """Apply the versioned Postgres migrations (same code path as the migration job)."""
        from kgrag.migrate import migrate_postgres

        migrate_postgres()

    def upsert(self, rows: list[dict[str, Any]]) -> None:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.executemany(UPSERT, rows)

    def count(self) -> int:
        with self._pool.connection() as conn:
            row = conn.execute("SELECT count(*) FROM chunks").fetchone()
        return int(row[0]) if row else 0

    def search(
        self, qvec: npt.NDArray[np.float32], k: int = 8, ef_search: int | None = None, exact: bool = False
    ) -> list[Passage]:
        ef = int(ef_search or self.ef_search)
        with self._pool.connection() as conn, conn.transaction():
            if exact:  # brute force (ground truth for HNSW recall)
                conn.execute("SET LOCAL enable_indexscan = off")
            else:
                conn.execute("SELECT set_config('hnsw.ef_search', %s, true)", (str(max(1, min(ef, 1000))),))
            rows = conn.execute(
                "SELECT chunk_id, doc_id, company, section_path, text, 1 - (embedding <=> %s) AS score, "
                "entity_ids FROM chunks ORDER BY embedding <=> %s LIMIT %s",
                (qvec, qvec, int(k)),
            ).fetchall()
        return [Passage(r[0], r[1], r[2], r[3], r[4], float(r[5]), list(r[6])) for r in rows]

    def search_within(self, qvec: npt.NDArray[np.float32], chunk_ids: list[str], k: int = 8) -> list[Passage]:
        if not chunk_ids:
            return []
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT chunk_id, doc_id, company, section_path, text, 1 - (embedding <=> %s) AS score, "
                "entity_ids FROM chunks WHERE chunk_id = ANY(%s) ORDER BY embedding <=> %s LIMIT %s",
                (qvec, chunk_ids, qvec, int(k)),
            ).fetchall()
        return [Passage(r[0], r[1], r[2], r[3], r[4], float(r[5]), list(r[6])) for r in rows]

    def get(self, chunk_ids: list[str]) -> dict[str, Passage]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT chunk_id, doc_id, company, section_path, text, 0.0, entity_ids FROM chunks "
                "WHERE chunk_id = ANY(%s)",
                (chunk_ids,),
            ).fetchall()
        return {r[0]: Passage(r[0], r[1], r[2], r[3], r[4], 0.0, list(r[6])) for r in rows}

    def insert_routing_log(self, row: dict[str, Any]) -> None:
        with self._pool.connection() as conn:
            conn.execute(INSERT_ROUTING_LOG, {**row, "hop_relations": Jsonb(row.get("hop_relations"))})

    def ping(self) -> bool:
        with self._pool.connection(timeout=3.0) as conn:
            row = conn.execute("SELECT 1").fetchone()
        return bool(row == (1,))

    def least_privilege(self) -> bool:
        """True when the connected (runtime) user holds no superuser-like role attribute or membership."""
        with self._pool.connection(timeout=3.0) as conn:
            rep = role_report(conn)
        return bool(rep and rep["least_privilege"])

    def close(self) -> None:
        self._pool.close()
