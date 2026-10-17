"""Phase 3 router: a small model (gpt-oss-20b) with a few-shot prompt returns an enum route plus
template parameters. The model fills parameters only; it never writes Cypher. Parameters are
validated and resolved to node ids by code before they are bound to a fixed template.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from kgrag.llm import CallStats, LLMClient, LLMError, StructuredOutputError
from kgrag.ontology import EntityType, RelationType
from kgrag.redact import redact_pii
from kgrag.spend import BudgetExceeded


class Route(StrEnum):
    GRAPH = "graph"
    VECTOR = "vector"
    HYBRID = "hybrid"
    OUT_OF_SCOPE = "out_of_scope"


class QueryType(StrEnum):
    NEIGHBORS = "neighbors"  # facts about one entity (one hop)
    TWO_HOP = "two_hop"  # chain through one intermediate entity
    THREE_HOP = "three_hop"  # chain through two intermediate entities
    COMMON_NEIGHBORS = "common_neighbors"  # what two entities share
    PATH_BETWEEN = "path_between"  # how two entities are connected
    RANK = "rank"  # which entity has the most X (aggregation)
    COUNT = "count"  # how many X does an entity have (aggregation)
    LOOKUP = "lookup"  # definitions / descriptions / single facts (vector)


class Direction(StrEnum):
    OUT = "out"
    IN = "in"
    ANY = "any"


class RouterOutput(BaseModel):
    """Exactly what the model may return. Free-text fields are bounded; enums are closed."""

    model_config = ConfigDict(extra="forbid")
    route: Route
    confidence: float = Field(ge=0.0, le=1.0)
    query_type: QueryType
    entities: list[str] = Field(default_factory=list, max_length=3)
    hop_relations: list[list[RelationType]] = Field(default_factory=list, max_length=3)
    direction: Direction = Direction.ANY
    target_type: EntityType | None = None

    @field_validator("entities")
    @classmethod
    def _bounded(cls, v: list[str]) -> list[str]:
        out = []
        for s in v:
            s = " ".join(s.split())[:80]
            if s:
                out.append(s)
        return out

    @field_validator("hop_relations")
    @classmethod
    def _bounded_rels(cls, v: list[list[RelationType]]) -> list[list[RelationType]]:
        return [hop[:4] for hop in v]


# Few-shot examples deliberately use entities/relations that do NOT appear in bench/questions.jsonl,
# so the router is not shown benchmark answers.
def _shot(
    route: str, conf: float, qt: str, ents: list[str], rels: list[list[str]], direction: str, target: str | None
) -> dict[str, Any]:
    return {
        "route": route,
        "confidence": conf,
        "query_type": qt,
        "entities": ents,
        "hop_relations": rels,
        "direction": direction,
        "target_type": target,
    }


FEW_SHOT = [
    ("Which companies supply Supermicro?",
     _shot("graph", 0.9, "neighbors", ["Supermicro"], [["SUPPLIES_TO"]], "in", "Company")),
    ("Where is the company that acquired Novellus headquartered?",
     _shot("graph", 0.85, "two_hop", ["Novellus"], [["ACQUIRED"], ["HEADQUARTERED_IN"]], "any", "Location")),
    ("Which technologies are used by products of the company that acquired Xilinx?",
     _shot("graph", 0.8, "three_hop", ["Xilinx"], [["ACQUIRED"], ["OFFERS"], ["USES_TECHNOLOGY"]], "any",
           "Technology")),
    ("Which markets do Microchip and Texas Instruments both serve?",
     _shot("graph", 0.85, "common_neighbors", ["Microchip", "Texas Instruments"], [["SERVES_MARKET"]], "any",
           "Market")),
    ("Which market is served by the most companies?",
     _shot("graph", 0.85, "rank", [], [["SERVES_MARKET"]], "out", "Market")),
    ("How many products does Synopsys offer?",
     _shot("graph", 0.8, "count", ["Synopsys"], [["OFFERS"]], "out", "Product")),
    ("What does Synopsys say about its approach to research and development?",
     _shot("vector", 0.9, "lookup", ["Synopsys"], [], "any", None)),
    ("Who painted the Mona Lisa?",
     _shot("out_of_scope", 0.95, "lookup", [], [], "any", None)),
]  # fmt: skip


def router_system_prompt() -> str:
    rels = ", ".join(r.value for r in RelationType if r != RelationType.CUSTOMER_OF)
    ents = ", ".join(e.value for e in EntityType)
    shots = "\n".join(f"Q: {q}\nA: {json.dumps(a)}" for q, a in FEW_SHOT)
    return f"""You route questions for a retrieval system over SEC annual reports (10-K / 20-F business
sections) of semiconductor and AI supply-chain companies (NVIDIA, AMD, Intel, Micron, Qualcomm,
Broadcom, TI, Applied Materials, Lam Research, KLA, Marvell, Analog Devices, Supermicro, Synopsys,
Cadence, onsemi, Microchip, TSMC).

Return JSON only. Fields:
- route: "graph" for relationships between entities, multi-hop chains, comparisons across entities,
  and aggregations over relationships; "vector" for definitions, descriptions, policies and single
  facts stated in prose; "out_of_scope" if the corpus cannot answer it (unrelated topic, future
  events, stock prices, personal data); "hybrid" if unsure.
- confidence: 0..1, your confidence in the route.
- query_type: one of neighbors, two_hop, three_hop, common_neighbors, path_between, rank, count, lookup.
- entities: the named entities in the question, starting with the anchor of the chain (max 3).
- hop_relations: for each hop starting at the anchor, the allowed relation types from: {rels}.
  SUPPLIES_TO points supplier -> customer; MANUFACTURES_FOR points foundry -> customer.
- direction: for one-hop questions, "out" if the anchor is the subject, "in" if it is the object.
- target_type: the entity type of the answer, one of {ents}, or null.

Examples:
{shots}"""


@dataclass
class EntityCatalog:
    """Resolves question entity strings to graph node ids (normalize + alias table + token match)."""

    by_alias: dict[str, list[tuple[str, int]]]
    names: dict[str, str]
    types: dict[str, str]

    @classmethod
    def from_rows(cls, rows: list[dict[str, Any]]) -> EntityCatalog:
        from kgrag.resolve import normalize

        by_alias: dict[str, list[tuple[str, int]]] = {}
        names: dict[str, str] = {}
        types: dict[str, str] = {}
        for r in rows:
            names[r["id"]] = r["name"]
            types[r["id"]] = r["type"]
            for a in {r["name"], *(r.get("aliases") or [])}:
                k = normalize(a)
                if k:
                    by_alias.setdefault(k, []).append((r["id"], int(r.get("degree") or 0)))
        return cls(by_alias, names, types)

    def resolve(self, name: str) -> str | None:
        from kgrag.resolve import normalize

        k = normalize(name)
        if not k:
            return None
        cands = self.by_alias.get(k)
        if not cands:
            # Prefix match on whole tokens ("Lam Research" -> "lam research")
            pref = [
                (a, c) for a, cs in self.by_alias.items() for c in cs if a.startswith(k + " ") or k.startswith(a + " ")
            ]
            if not pref:
                return None
            cands = [c for _, c in pref]
        # Prefer companies, then the most connected node.
        best = sorted(cands, key=lambda c: (self.types.get(c[0]) != "Company", -c[1]))[0]
        return best[0]

    def scan(self, question: str, max_n: int = 3) -> list[str]:
        """Deterministic linking: aliases that occur as whole phrases in the question."""
        from kgrag.resolve import normalize

        q = f" {normalize(question)} "
        hits: list[tuple[int, int, str]] = []
        for alias, cands in self.by_alias.items():
            if len(alias) < 3 or alias in _STOP_ALIASES:
                continue
            pos = q.find(f" {alias} ")
            if pos >= 0:
                eid = sorted(cands, key=lambda c: (self.types.get(c[0]) != "Company", -c[1]))[0][0]
                hits.append((pos, -len(alias), eid))
        out: list[str] = []
        for _, _, eid in sorted(hits):
            if eid not in out:
                out.append(eid)
        return out[:max_n]


_STOP_ALIASES = {
    "company",
    "the company",
    "data center",
    "ai",
    "products",
    "foundry",
    "customers",
    "semiconductor",
    "technology",
    "software",
    "hardware",
}


@dataclass
class GraphPlan:
    template: str
    params: dict[str, Any]
    entity_ids: list[str] = field(default_factory=list)


MAX_LIMIT = 50

_TEMPLATE_FOR: dict[QueryType, str] = {
    QueryType.TWO_HOP: "two_hop",
    QueryType.THREE_HOP: "three_hop",
    QueryType.COMMON_NEIGHBORS: "common_neighbors",
    QueryType.PATH_BETWEEN: "path_between",
    QueryType.RANK: "rank_by_relation",
    QueryType.COUNT: "count_neighbors",
}


def build_plan(out: RouterOutput, entity_ids: list[str]) -> GraphPlan | None:
    """Map a validated router output to (template key, bound params). Pure code; no model text
    reaches the query except as values of typed parameters."""
    rels = [[r.value for r in hop if r != RelationType.CUSTOMER_OF] for hop in out.hop_relations]
    while len(rels) < 3:
        rels.append([])
    target = out.target_type.value if out.target_type else None
    base: dict[str, Any] = {
        "rels": rels[0],
        "rels2": rels[1],
        "rels3": rels[2],
        "target_type": target,
        "limit": MAX_LIMIT,
        "max_degree": 400,
        "per_target": 8,
    }
    qt = out.query_type
    if qt == QueryType.RANK:
        return GraphPlan("rank_by_relation", {**base, "limit": 5}, [])
    if not entity_ids:
        return None
    base["entity_id"] = entity_ids[0]
    if qt in (QueryType.COMMON_NEIGHBORS, QueryType.PATH_BETWEEN):
        if len(entity_ids) < 2:
            qt = QueryType.NEIGHBORS
        else:
            base["entity_id2"] = entity_ids[1]
            return GraphPlan(_TEMPLATE_FOR[qt], base, entity_ids[:2])
    if qt in (QueryType.NEIGHBORS, QueryType.LOOKUP):
        key = {Direction.OUT: "neighbors_out", Direction.IN: "neighbors_in"}.get(out.direction, "neighbors_any")
        return GraphPlan(key, base, entity_ids[:1])
    return GraphPlan(_TEMPLATE_FOR[qt], base, entity_ids[:1])


@dataclass
class RoutingDecision:
    question: str
    output: RouterOutput | None
    route: Route
    fallback_reason: str
    plan: GraphPlan | None
    entity_ids: list[str]
    latency_ms: float
    model_used: str | None = None


class Router:
    def __init__(
        self,
        llm: LLMClient | None,
        model: str,
        catalog: EntityCatalog,
        confidence_floor: float = 0.6,
        log_path: Path | None = None,
        fallback_models: list[str] | None = None,
    ) -> None:
        self.llm = llm
        self.model = model
        self.fallback_models = fallback_models or []
        self.catalog = catalog
        self.floor = confidence_floor
        self.log_path = log_path
        self._system = router_system_prompt()

    def route(self, question: str, stats: CallStats | None = None) -> RoutingDecision:
        t0 = time.perf_counter()
        out: RouterOutput | None = None
        reason = ""
        model_used = None
        if self.llm is not None:
            errors = []
            # Model chain: primary router model, then same-size/same-price fallbacks (each fails fast
            # under rate limits), then the deterministic both-paths fallback below.
            for model in [self.model, *self.fallback_models]:
                try:
                    out = self.llm.complete_json(
                        [{"role": "system", "content": self._system}, {"role": "user", "content": "Q: " + question}],
                        model,
                        RouterOutput,
                        max_tokens=600,
                        attempts=2,
                        reasoning_effort="low",
                        stats=stats,
                        stage="router",
                    )
                    model_used = model
                    break
                except (StructuredOutputError, LLMError, BudgetExceeded) as exc:
                    errors.append(f"{model.split('/')[-1]}:{type(exc).__name__}")
            if out is None:
                reason = "router_error:" + ",".join(errors)
            elif errors:
                reason = "router_model_fallback:" + ",".join(errors)
        else:
            reason = "router_disabled"
        # Resolve entities: model-named entities first, then deterministic scan of the question.
        ids: list[str] = []
        for name in out.entities if out else []:
            eid = self.catalog.resolve(name)
            if eid and eid not in ids:
                ids.append(eid)
        for eid in self.catalog.scan(question):
            if eid not in ids:
                ids.append(eid)
        ids = ids[:3]
        if out is None:
            route = Route.HYBRID
        elif out.confidence < self.floor:
            route, reason = Route.HYBRID, f"low_confidence:{out.confidence:.2f}"
        else:
            route = out.route
        plan = build_plan(out, ids) if out is not None else None
        if route in (Route.GRAPH, Route.HYBRID) and plan is None and out is not None and route == Route.GRAPH:
            route, reason = Route.HYBRID, "no_graph_plan"
        dec = RoutingDecision(question, out, route, reason, plan, ids, (time.perf_counter() - t0) * 1000, model_used)
        self._log(dec)
        return dec

    def _log(self, d: RoutingDecision) -> None:
        if self.log_path is None:
            return
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "question": redact_pii(d.question),
            "model_route": d.output.route.value if d.output else None,
            "confidence": d.output.confidence if d.output else None,
            "query_type": d.output.query_type.value if d.output else None,
            "final_route": d.route.value,
            "fallback_reason": d.fallback_reason,
            "template": d.plan.template if d.plan else None,
            "entity_ids": d.entity_ids,
            "latency_ms": round(d.latency_ms, 1),
        }
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
