"""Integration tests against the live docker stack. Skipped unless KGRAG_INTEGRATION=1.

KGRAG_INTEGRATION=1 uv run pytest tests/test_integration.py
"""

from __future__ import annotations

import os

import pytest

from kgrag.ontology import RelationType

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("KGRAG_INTEGRATION") != "1", reason="needs the p1-kgrag stack"),
]


@pytest.fixture
def graph():  # type: ignore[no-untyped-def]
    from kgrag.config import get_settings
    from kgrag.graph_store import GraphStore, Neo4jRunner

    s = get_settings()
    runner = Neo4jRunner(s.neo4j_uri, s.neo4j_user, s.neo4j_password)
    g = GraphStore(runner)
    yield g
    runner.run("MATCH (n) WHERE n.id STARTS WITH 'itest:' DETACH DELETE n", {})
    runner.close()


def test_merge_is_idempotent(graph) -> None:  # type: ignore[no-untyped-def]
    ents = [
        {"id": "itest:a", "name": "A", "type": "Company", "aliases": ["A"], "mention_count": 1},
        {"id": "itest:b", "name": "B", "type": "Company", "aliases": ["B"], "mention_count": 1},
    ]
    rel = [{"src": "itest:a", "dst": "itest:b", "chunk_ids": ["itest:c1"], "evidence": ["e"], "confidence": 0.9}]
    for _ in range(3):
        graph.write_entities(ents)
        graph.write_relations(RelationType.SUPPLIES_TO, rel)
    graph.write_relations(RelationType.SUPPLIES_TO, [{**rel[0], "chunk_ids": ["itest:c1", "itest:c2"]}])
    rows = graph.runner.run(
        "MATCH (a:Entity {id:'itest:a'})-[r:SUPPLIES_TO]->(b:Entity {id:'itest:b'}) RETURN count(r) AS n, "
        "collect(r.chunk_ids)[0] AS c",
        {},
    )
    assert rows[0]["n"] == 1
    assert sorted(rows[0]["c"]) == ["itest:c1", "itest:c2"]
    n = graph.runner.run("MATCH (e:Entity) WHERE e.id STARTS WITH 'itest:' RETURN count(e) AS n", {})
    assert n[0]["n"] == 2


def test_templates_run_read_only_on_real_db(graph) -> None:  # type: ignore[no-untyped-def]
    rows = graph.run_template(
        "neighbors_in", {"entity_id": "company:nvidia", "rels": ["MANUFACTURES_FOR"], "target_type": None, "limit": 10}
    )
    assert isinstance(rows, list)
    from neo4j.exceptions import ClientError

    with pytest.raises(ClientError):  # a write through the read path is rejected by the server
        graph.runner.run_read("CREATE (:Entity {id:'itest:x'})", {})
