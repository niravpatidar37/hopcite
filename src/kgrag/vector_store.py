"""pgvector store. Same chunk_id as the Neo4j :Chunk nodes. HNSW index (cosine), tunable ef_search."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import numpy.typing as npt

DDL = [
    "CREATE EXTENSION IF NOT EXISTS vector",
    """CREATE TABLE IF NOT EXISTS chunks (
        chunk_id     text PRIMARY KEY,
        doc_id       text NOT NULL,
        company      text NOT NULL,
        form         text NOT NULL,
        filing_date  date NOT NULL,
        section_path text NOT NULL,
        ordinal      int  NOT NULL,
        text         text NOT NULL,
        sha256       text NOT NULL,
        entity_ids   text[] NOT NULL DEFAULT '{}',
        embedding    vector(%d) NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS chunks_doc_idx ON chunks (doc_id)",
    "CREATE INDEX IF NOT EXISTS chunks_entities_idx ON chunks USING gin (entity_ids)",
    "CREATE INDEX IF NOT EXISTS chunks_hnsw_idx ON chunks USING hnsw (embedding vector_cosine_ops) "
    "WITH (m = 16, ef_construction = 64)",
]

UPSERT = """
INSERT INTO chunks (chunk_id, doc_id, company, form, filing_date, section_path, ordinal, text, sha256,
                    entity_ids, embedding)
VALUES (%(chunk_id)s, %(doc_id)s, %(company)s, %(form)s, %(filing_date)s, %(section_path)s, %(ordinal)s,
        %(text)s, %(sha256)s, %(entity_ids)s, %(embedding)s)
ON CONFLICT (chunk_id) DO UPDATE SET
    text = EXCLUDED.text, sha256 = EXCLUDED.sha256, entity_ids = EXCLUDED.entity_ids,
    embedding = EXCLUDED.embedding, section_path = EXCLUDED.section_path
"""


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
    def __init__(self, dsn: str, dim: int = 384, ef_search: int = 40) -> None:
        import psycopg
        from pgvector.psycopg import register_vector

        self.dim = dim
        self.ef_search = ef_search
        self._conn = psycopg.connect(dsn, autocommit=True)
        self._conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        register_vector(self._conn)

    def ensure_schema(self) -> None:
        for stmt in DDL:
            self._conn.execute(stmt % self.dim if "%d" in stmt else stmt)  # dim is an int from config

    def upsert(self, rows: list[dict[str, Any]]) -> None:
        with self._conn.cursor() as cur:
            cur.executemany(UPSERT, rows)

    def count(self) -> int:
        row = self._conn.execute("SELECT count(*) FROM chunks").fetchone()
        return int(row[0]) if row else 0

    def search(
        self, qvec: npt.NDArray[np.float32], k: int = 8, ef_search: int | None = None, exact: bool = False
    ) -> list[Passage]:
        ef = int(ef_search or self.ef_search)
        with self._conn.transaction():
            if exact:  # brute force (ground truth for HNSW recall)
                self._conn.execute("SET LOCAL enable_indexscan = off")
            else:
                self._conn.execute("SELECT set_config('hnsw.ef_search', %s, true)", (str(max(1, min(ef, 1000))),))
            rows = self._conn.execute(
                "SELECT chunk_id, doc_id, company, section_path, text, 1 - (embedding <=> %s) AS score, "
                "entity_ids FROM chunks ORDER BY embedding <=> %s LIMIT %s",
                (qvec, qvec, int(k)),
            ).fetchall()
        return [Passage(r[0], r[1], r[2], r[3], r[4], float(r[5]), list(r[6])) for r in rows]

    def search_within(self, qvec: npt.NDArray[np.float32], chunk_ids: list[str], k: int = 8) -> list[Passage]:
        if not chunk_ids:
            return []
        rows = self._conn.execute(
            "SELECT chunk_id, doc_id, company, section_path, text, 1 - (embedding <=> %s) AS score, "
            "entity_ids FROM chunks WHERE chunk_id = ANY(%s) ORDER BY embedding <=> %s LIMIT %s",
            (qvec, chunk_ids, qvec, int(k)),
        ).fetchall()
        return [Passage(r[0], r[1], r[2], r[3], r[4], float(r[5]), list(r[6])) for r in rows]

    def get(self, chunk_ids: list[str]) -> dict[str, Passage]:
        rows = self._conn.execute(
            "SELECT chunk_id, doc_id, company, section_path, text, 0.0, entity_ids FROM chunks "
            "WHERE chunk_id = ANY(%s)",
            (chunk_ids,),
        ).fetchall()
        return {r[0]: Passage(r[0], r[1], r[2], r[3], r[4], 0.0, list(r[6])) for r in rows}

    def ping(self) -> bool:
        return self._conn.execute("SELECT 1").fetchone() == (1,)

    def close(self) -> None:
        self._conn.close()
