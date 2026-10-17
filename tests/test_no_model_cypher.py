"""Security control: the model never writes Cypher.

The router model's output can only choose a template key and fill typed parameters. These tests
fail if (a) any executed query text is not a constant from the template library, (b) model-supplied
strings ever reach query text, (c) unresolved model strings are bound as node ids, or (d) the
router schema accepts free-form query fields or unknown relation types.
"""

from __future__ import annotations

import json
import random
import string
from typing import Any

import pytest
from pydantic import ValidationError

from kgrag.graph_store import READ_ONLY_FORBIDDEN, TEMPLATES, GraphStore, merge_relation_cypher
from kgrag.ontology import RelationType
from kgrag.pipeline import Pipeline
from kgrag.router import EntityCatalog, Router, RouterOutput
from tests.conftest import FakeRunner

CATALOG_ROWS = [
    {"id": "company:nvidia", "name": "NVIDIA Corporation", "type": "Company", "aliases": ["NVIDIA"], "degree": 50},
    {
        "id": "company:tsmc",
        "name": "TSMC",
        "type": "Company",
        "aliases": ["Taiwan Semiconductor Manufacturing Company Limited"],
        "degree": 40,
    },
    {"id": "company:amd", "name": "Advanced Micro Devices, Inc.", "type": "Company", "aliases": ["AMD"], "degree": 30},
]
INJECTIONS = [
    "NVIDIA'}) DETACH DELETE n //",
    "x\") CALL dbms.security.createUser('a','b') //",
    "MATCH (n) DETACH DELETE n",
    "}) RETURN 1 UNION MATCH (n) SET n.pwned = true //",
    "LOAD CSV FROM 'http://evil.example/x' AS row",
]


class FakeLLM:
    def __init__(self, outputs: list[dict[str, Any]]) -> None:
        self.outputs = outputs

    def complete_json(self, messages: Any, model: str, schema_cls: Any, **kw: Any) -> Any:
        return schema_cls.model_validate_json(json.dumps(self.outputs.pop(0)))


class NoVectors:
    def search_within(self, *a: Any, **k: Any) -> list[Any]:
        return []


def _router(outputs: list[dict[str, Any]]) -> Router:
    cat = EntityCatalog.from_rows(CATALOG_ROWS)
    return Router(FakeLLM(outputs), "openai/gpt-oss-20b", cat, 0.6)  # type: ignore[arg-type]


def _executed_queries_are_templates(runner: FakeRunner) -> None:
    allowed = set(TEMPLATES.values())
    for cypher, params in runner.calls:
        assert cypher in allowed, f"non-template query executed: {cypher[:80]}"
        for inj in INJECTIONS:
            assert inj not in cypher


def _router_output(entities: list[str], qt: str = "two_hop") -> dict[str, Any]:
    return {
        "route": "graph",
        "confidence": 0.95,
        "query_type": qt,
        "entities": entities,
        "hop_relations": [["MANUFACTURES_FOR"], ["HEADQUARTERED_IN"]],
        "direction": "any",
        "target_type": "Location",
    }


@pytest.mark.parametrize("inj", INJECTIONS)
def test_injected_entity_strings_never_reach_query_text(inj: str) -> None:
    router = _router([_router_output([inj, "NVIDIA"])])
    runner = FakeRunner()
    pipe = Pipeline(NoVectors(), None, None, GraphStore(runner), router)  # type: ignore[arg-type]
    decision = router.route(f"Where is the foundry of {inj} headquartered?")
    pipe.graph_retrieve(decision)
    assert runner.calls, "graph path should run a template"
    _executed_queries_are_templates(runner)
    # Only catalog ids are bound as node ids: the injected string resolved to nothing.
    for _, params in runner.calls:
        for key in ("entity_id", "entity_id2"):
            if key in params:
                assert params[key] in {r["id"] for r in CATALOG_ROWS}


def test_unresolvable_entities_run_no_entity_query() -> None:
    router = _router([_router_output(["Totally Unknown Co"])])
    runner = FakeRunner()
    pipe = Pipeline(NoVectors(), None, None, GraphStore(runner), router)  # type: ignore[arg-type]
    decision = router.route("Where is the foundry of Totally Unknown Co headquartered?")
    assert decision.plan is None and decision.entity_ids == []
    pipe.graph_retrieve(decision)
    assert runner.calls == []


def test_fuzzed_router_outputs_only_ever_execute_templates() -> None:
    rng = random.Random(1234)
    qtypes = ["neighbors", "two_hop", "three_hop", "common_neighbors", "path_between", "rank", "count", "lookup"]
    rels = [r.value for r in RelationType]
    names = [*INJECTIONS, "NVIDIA", "AMD", "TSMC", ""]
    outputs = []
    for _ in range(150):
        junk = "".join(rng.choice(string.printable) for _ in range(rng.randint(0, 40)))
        outputs.append(
            {
                "route": rng.choice(["graph", "hybrid", "vector"]),
                "confidence": rng.random(),
                "query_type": rng.choice(qtypes),
                "entities": rng.sample([*names, junk], k=rng.randint(0, 3)),
                "hop_relations": [rng.sample(rels, k=rng.randint(0, 3)) for _ in range(rng.randint(0, 3))],
                "direction": rng.choice(["in", "out", "any"]),
                "target_type": rng.choice([None, "Company", "Location"]),
            }
        )
    router = _router(list(outputs))
    runner = FakeRunner()
    pipe = Pipeline(NoVectors(), None, None, GraphStore(runner), router)  # type: ignore[arg-type]
    for _ in outputs:
        pipe.graph_retrieve(router.route("question"))
    _executed_queries_are_templates(runner)
    for _, params in runner.calls:
        for v in params.get("rels", []) + params.get("rels2", []) + params.get("rels3", []):
            assert v in rels


def test_router_schema_rejects_free_form_cypher_and_unknown_relations() -> None:
    base = _router_output(["NVIDIA"])
    with pytest.raises(ValidationError):
        RouterOutput.model_validate({**base, "cypher": "MATCH (n) DETACH DELETE n"})
    with pytest.raises(ValidationError):
        RouterOutput.model_validate({**base, "hop_relations": [["DELETE_EVERYTHING"]]})
    with pytest.raises(ValidationError):
        RouterOutput.model_validate({**base, "query_type": "raw_cypher"})


def test_templates_are_read_only_and_unknown_keys_refused() -> None:
    for key, cypher in TEMPLATES.items():
        assert not READ_ONLY_FORBIDDEN.search(cypher), key
        assert "%" not in cypher and "{0" not in cypher, key
    with pytest.raises(KeyError):
        GraphStore(FakeRunner()).run_template("MATCH (n) DETACH DELETE n", {})


def test_write_relation_type_comes_from_enum_only() -> None:
    assert ":COMPETES_WITH]" in merge_relation_cypher(RelationType.COMPETES_WITH)
    with pytest.raises(ValueError):
        merge_relation_cypher("COMPETES_WITH]->(b) DETACH DELETE a //")  # type: ignore[arg-type]
