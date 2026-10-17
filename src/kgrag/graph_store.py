"""Neo4j store: idempotent MERGE writes and the read-only parameterized Cypher template library.

The model never writes Cypher. Query text lives in TEMPLATES (constants in this file); the router
only picks a template key and supplies parameters, which are validated (see kgrag.router.GraphPlan)
before they are bound as driver parameters. Relationship types used when *writing* come from the
RelationType enum (code-defined), never from model text.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from kgrag.ontology import RelationType

_REL_TYPE_OK = re.compile(r"^[A-Z_]{3,40}$")


class CypherRunner(Protocol):
    def run(self, cypher: str, params: dict[str, Any]) -> list[dict[str, Any]]: ...


# Constraints and indexes are versioned migrations: kgrag/migrations/neo4j (python -m kgrag.migrate up).

MERGE_DOCS = """
UNWIND $rows AS row
MERGE (d:Document {id: row.id})
SET d.company = row.company, d.form = row.form, d.filing_date = row.filing_date, d.url = row.url
"""

MERGE_CHUNKS = """
UNWIND $rows AS row
MERGE (c:Chunk {id: row.id})
SET c.doc_id = row.doc_id, c.section_path = row.section_path, c.ordinal = row.ordinal
WITH c, row
MATCH (d:Document {id: row.doc_id})
MERGE (c)-[:PART_OF]->(d)
"""

MERGE_ENTITIES = """
UNWIND $rows AS row
MERGE (e:Entity {id: row.id})
SET e.name = row.name, e.type = row.type, e.aliases = row.aliases, e.mention_count = row.mention_count
"""

MERGE_MENTIONS = """
UNWIND $rows AS row
MATCH (e:Entity {id: row.entity_id})
MATCH (c:Chunk {id: row.chunk_id})
MERGE (e)-[:MENTIONED_IN]->(c)
"""

# One MERGE statement per relation type; the type comes from the RelationType enum only.
MERGE_RELATION_TMPL = """
UNWIND $rows AS row
MATCH (a:Entity {id: row.src})
MATCH (b:Entity {id: row.dst})
MERGE (a)-[r:%s]->(b)
SET r.chunk_ids = apoc_free_union(r.chunk_ids, row.chunk_ids),
    r.evidence = row.evidence,
    r.confidence = row.confidence
"""
# Neo4j community has no APOC by default: emulate set-union in plain Cypher.
_UNION = (
    "CASE WHEN r.chunk_ids IS NULL THEN row.chunk_ids "
    "ELSE r.chunk_ids + [x IN row.chunk_ids WHERE NOT x IN r.chunk_ids] END"
)


def merge_relation_cypher(rel: RelationType) -> str:
    rel = RelationType(rel)  # raises on anything that is not an ontology member
    if not _REL_TYPE_OK.match(rel.value):
        raise ValueError("bad relation type")
    return (MERGE_RELATION_TMPL % rel.value).replace("apoc_free_union(r.chunk_ids, row.chunk_ids)", _UNION)


# --------------------------------------------------------------------------- read templates
# Each template returns rows of hops: [{src, src_type, rel, dst, dst_type, chunk_ids}] in `path`.
# Relationship direction is read from startNode/endNode, so undirected matches print correctly.
_HOP = (
    "{src: startNode(%(r)s).name, rel: type(%(r)s), dst: endNode(%(r)s).name, "
    "src_id: startNode(%(r)s).id, dst_id: endNode(%(r)s).id, chunk_ids: %(r)s.chunk_ids}"
)


def _hop(r: str) -> str:
    return _HOP % {"r": r}


TEMPLATES: dict[str, str] = {
    # Facts directly attached to one entity, optionally filtered by relation type and direction.
    "neighbors_out": (
        "MATCH (a:Entity {id: $entity_id})-[r]->(b:Entity) "
        "WHERE (size($rels) = 0 OR type(r) IN $rels) AND ($target_type IS NULL OR b.type = $target_type) "
        f"RETURN [{_hop('r')}] AS path LIMIT $limit"
    ),
    "neighbors_in": (
        "MATCH (a:Entity {id: $entity_id})<-[r]-(b:Entity) "
        "WHERE (size($rels) = 0 OR type(r) IN $rels) AND ($target_type IS NULL OR b.type = $target_type) "
        f"RETURN [{_hop('r')}] AS path LIMIT $limit"
    ),
    "neighbors_any": (
        "MATCH (a:Entity {id: $entity_id})-[r]-(b:Entity) "
        "WHERE (size($rels) = 0 OR type(r) IN $rels) AND ($target_type IS NULL OR b.type = $target_type) "
        f"RETURN [{_hop('r')}] AS path LIMIT $limit"
    ),
    # Two-hop chain a -[rels1]- x -[rels2]- b (direction-agnostic; direction is reported per hop).
    "two_hop": (
        "MATCH (a:Entity {id: $entity_id})-[r1]-(x:Entity)-[r2]-(b:Entity) "
        "WHERE (size($rels) = 0 OR type(r1) IN $rels) AND (size($rels2) = 0 OR type(r2) IN $rels2) "
        "AND b <> a AND ($target_type IS NULL OR b.type = $target_type) "
        "AND COUNT { (x)--() } <= $max_degree "
        f"RETURN [{_hop('r1')}, {_hop('r2')}] AS path LIMIT $limit"
    ),
    "three_hop": (
        "MATCH (a:Entity {id: $entity_id})-[r1]-(x:Entity)-[r2]-(y:Entity)-[r3]-(b:Entity) "
        "WHERE (size($rels) = 0 OR type(r1) IN $rels) AND (size($rels2) = 0 OR type(r2) IN $rels2) "
        "AND (size($rels3) = 0 OR type(r3) IN $rels3) AND y <> a AND b <> x AND b <> a "
        "AND ($target_type IS NULL OR b.type = $target_type) "
        "AND COUNT { (x)--() } <= $max_degree AND COUNT { (y)--() } <= $max_degree "
        f"RETURN [{_hop('r1')}, {_hop('r2')}, {_hop('r3')}] AS path LIMIT $limit"
    ),
    # Entities connected to BOTH a and b by the given relation types (comparisons / shared neighbors).
    "common_neighbors": (
        "MATCH (a:Entity {id: $entity_id})-[r1]-(x:Entity)-[r2]-(b:Entity {id: $entity_id2}) "
        "WHERE (size($rels) = 0 OR type(r1) IN $rels) AND (size($rels) = 0 OR type(r2) IN $rels) "
        "AND ($target_type IS NULL OR x.type = $target_type) "
        f"RETURN [{_hop('r1')}, {_hop('r2')}] AS path LIMIT $limit"
    ),
    # Shortest connection between two entities, up to 3 hops.
    "path_between": (
        "MATCH (a:Entity {id: $entity_id}), (b:Entity {id: $entity_id2}) "
        "MATCH p = shortestPath((a)-[*..3]-(b)) "
        "WHERE all(r IN relationships(p) WHERE type(r) <> 'MENTIONED_IN' AND type(r) <> 'PART_OF') "
        "RETURN [r IN relationships(p) | {src: startNode(r).name, rel: type(r), dst: endNode(r).name, "
        "src_id: startNode(r).id, dst_id: endNode(r).id, chunk_ids: r.chunk_ids}] AS path LIMIT 1"
    ),
    # Aggregation: which entities have the most incoming/any edges of the given types.
    "rank_by_relation": (
        "MATCH (s:Entity)-[r]->(t:Entity) "
        "WHERE (size($rels) = 0 OR type(r) IN $rels) AND ($target_type IS NULL OR t.type = $target_type) "
        "WITH t, count(DISTINCT s) AS n, collect(r)[..$per_target] AS rs "
        "ORDER BY n DESC, t.name LIMIT $limit "
        "RETURN [r IN rs | {src: startNode(r).name, rel: type(r), dst: endNode(r).name, "
        "src_id: startNode(r).id, dst_id: endNode(r).id, chunk_ids: r.chunk_ids}] AS path, t.name AS target, n"
    ),
    # Graph -> text: chunks that mention any of the linked entities.
    "chunks_mentioning": (
        "UNWIND $entity_ids AS eid MATCH (e:Entity {id: eid})-[:MENTIONED_IN]->(c:Chunk) "
        "WITH c, count(*) AS n ORDER BY n DESC LIMIT 200 RETURN c.id AS id"
    ),
    # Aggregation: count distinct entities linked to one entity by the given relation types.
    "count_neighbors": (
        "MATCH (a:Entity {id: $entity_id})-[r]-(b:Entity) "
        "WHERE (size($rels) = 0 OR type(r) IN $rels) AND ($target_type IS NULL OR b.type = $target_type) "
        "WITH b, collect(r)[0] AS r "
        f"RETURN [{_hop('r')}] AS path LIMIT $limit"
    ),
}

READ_ONLY_FORBIDDEN = re.compile(
    r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|LOAD\s+CSV|CALL\s+dbms)\b", re.IGNORECASE
)


@dataclass
class Hop:
    src: str
    rel: str
    dst: str
    src_id: str
    dst_id: str
    chunk_ids: list[str]


class Neo4jRunner:
    def __init__(self, uri: str, user: str, password: str, database: str = "neo4j") -> None:
        from neo4j import GraphDatabase

        # Bounded connect time, and a liveness check on pooled connections that sat idle: managed
        # Neo4j (AuraDB) and NAT paths drop idle connections, which would otherwise surface as a
        # failed request after an idle period.
        self._driver = GraphDatabase.driver(
            uri,
            auth=(user, password),
            connection_timeout=10.0,
            connection_acquisition_timeout=15.0,
            liveness_check_timeout=30.0,
            max_connection_lifetime=1800,
        )
        self._db = database

    def run(self, cypher: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        with self._driver.session(database=self._db) as s:
            return [r.data() for r in s.run(cypher, params)]

    def run_read(self, cypher: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        with self._driver.session(database=self._db, default_access_mode="READ") as s:
            return list(s.execute_read(lambda tx: [r.data() for r in tx.run(cypher, params)]))

    def ping(self, timeout_s: float = 3.0) -> bool:
        from neo4j import Query

        with self._driver.session(database=self._db, default_access_mode="READ") as s:
            rec = s.run(Query("RETURN 1 AS ok", timeout=timeout_s)).single()
            return rec is not None and rec["ok"] == 1

    def close(self) -> None:
        self._driver.close()


class GraphStore:
    def __init__(self, runner: CypherRunner) -> None:
        self.runner = runner

    # ---------------------------------------------------------------- writes (ingestion only)
    def ensure_schema(self) -> None:
        """Apply the versioned Neo4j migrations (same code path as the migration job)."""
        from kgrag.migrate import migrate_neo4j

        migrate_neo4j(self.runner)

    def write_documents(self, rows: list[dict[str, Any]]) -> None:
        self.runner.run(MERGE_DOCS, {"rows": rows})

    def write_chunks(self, rows: list[dict[str, Any]]) -> None:
        for batch in _batches(rows, 500):
            self.runner.run(MERGE_CHUNKS, {"rows": batch})

    def write_entities(self, rows: list[dict[str, Any]]) -> None:
        for batch in _batches(rows, 500):
            self.runner.run(MERGE_ENTITIES, {"rows": batch})

    def write_mentions(self, rows: list[dict[str, Any]]) -> None:
        for batch in _batches(rows, 1000):
            self.runner.run(MERGE_MENTIONS, {"rows": batch})

    def write_relations(self, rel: RelationType, rows: list[dict[str, Any]]) -> None:
        cypher = merge_relation_cypher(rel)
        for batch in _batches(rows, 500):
            self.runner.run(cypher, {"rows": batch})

    # ---------------------------------------------------------------- reads (query time)
    def run_template(self, key: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if key not in TEMPLATES:
            raise KeyError(f"unknown template {key}")
        cypher = TEMPLATES[key]
        if READ_ONLY_FORBIDDEN.search(cypher):  # defence in depth; templates are constants
            raise ValueError("templates must be read-only")
        runner = self.runner
        if isinstance(runner, Neo4jRunner):
            return runner.run_read(cypher, params)
        return runner.run(cypher, params)

    def entity_catalog(self) -> list[dict[str, Any]]:
        return self.runner.run(
            "MATCH (e:Entity) RETURN e.id AS id, e.name AS name, e.type AS type, e.aliases AS aliases, "
            "COUNT { (e)--() } AS degree",
            {},
        )

    def entities_for_chunks(self, chunk_ids: Sequence[str]) -> dict[str, list[str]]:
        rows = self.runner.run(
            "UNWIND $ids AS cid MATCH (e:Entity)-[:MENTIONED_IN]->(c:Chunk {id: cid}) "
            "RETURN cid, collect(e.id) AS ents",
            {"ids": list(chunk_ids)},
        )
        return {r["cid"]: r["ents"] for r in rows}

    def stats(self) -> dict[str, Any]:
        n = self.runner.run("MATCH (e:Entity) RETURN e.type AS type, count(*) AS n ORDER BY n DESC", {})
        r = self.runner.run("MATCH (:Entity)-[r]->(:Entity) RETURN type(r) AS type, count(*) AS n ORDER BY n DESC", {})
        return {"entities": {x["type"]: x["n"] for x in n}, "relations": {x["type"]: x["n"] for x in r}}


def _batches(rows: list[dict[str, Any]], n: int) -> Iterable[list[dict[str, Any]]]:
    for i in range(0, len(rows), n):
        yield rows[i : i + n]


def rows_to_hops(rows: list[dict[str, Any]]) -> list[list[Hop]]:
    out: list[list[Hop]] = []
    for row in rows:
        path = row.get("path") or []
        hops = [
            Hop(h["src"], h["rel"], h["dst"], h["src_id"], h["dst_id"], list(h.get("chunk_ids") or []))
            for h in path
            if h and h.get("src") is not None
        ]
        if hops:
            out.append(hops)
    return out
