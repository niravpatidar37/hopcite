"""Entity resolution: normalization + alias/acronym rules + embedding similarity above a tuned threshold.

Clusters mentions of the same type with union-find. Each resolved node gets a stable id, a canonical
name (most frequent surface form, longest on ties) and the full alias list.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

import numpy as np
import numpy.typing as npt

from kgrag.ontology import EntityType

LEGAL_SUFFIXES = {
    "inc",
    "incorporated",
    "corporation",
    "corp",
    "co",
    "company",
    "ltd",
    "limited",
    "plc",
    "nv",
    "llc",
    "holdings",
    "holding",
    "group",
    "sa",
    "ag",
    "se",
    "lp",
    "bv",
    "gmbh",
    "kk",
    "the",
}


def normalize(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = s.lower().replace("&", " and ")
    s = re.sub(r"\(.*?\)", " ", s)  # drop parentheticals like ("AMD")
    s = re.sub(r"\b([a-z])\.(?=[a-z]\b)", r"\1", s)  # dotted abbreviations: "n.v." -> "nv."
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    toks = s.split()
    while toks and toks[-1] in LEGAL_SUFFIXES:
        toks.pop()
    while toks and toks[0] == "the":
        toks.pop(0)
    return " ".join(toks)


# Words that do not distinguish one company from another when they trail a distinctive name
# ("Cisco Systems" = "Cisco", "Infineon Technologies AG" = "Infineon").
GENERIC_TRAILERS = {
    "technologies", "technology", "systems", "group", "electronics", "international",
    "semiconductor", "semiconductors", "devices", "industries", "enterprises",
} | LEGAL_SUFFIXES  # fmt: skip


def core_key(key: str) -> str:
    """Normalized key without trailing generic corporate words and spaces; '' if nothing
    distinctive is left."""
    toks = key.split()
    while len(toks) > 1 and toks[-1] in GENERIC_TRAILERS:
        toks.pop()
    core = "".join(toks)
    return core if len(core) >= 3 and toks[0] not in GENERIC_TRAILERS else ""


def acronyms(name: str) -> set[str]:
    """Initialisms of the name with and without trailing legal suffixes ("Taiwan Semiconductor
    Manufacturing Company Limited" -> {"tsmcl", "tsmc", "tsm"})."""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\(.*?\)", " ", s)
    toks = [t for t in re.sub(r"[^a-z0-9 ]+", " ", s).split() if t not in {"and", "of", "the"}]
    out: set[str] = set()
    while len(toks) >= 2:
        out.add("".join(t[0] for t in toks))
        if toks[-1] in LEGAL_SUFFIXES:
            toks = toks[:-1]
        else:
            break
    return {a for a in out if len(a) >= 3}


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:80] or "x"


@dataclass
class Mention:
    name: str
    type: EntityType
    aliases: list[str] = field(default_factory=list)


@dataclass
class ResolvedEntity:
    id: str
    name: str
    type: EntityType
    aliases: list[str]
    mention_count: int


class UnionFind:
    def __init__(self, n: int) -> None:
        self.p = list(range(n))

    def find(self, x: int) -> int:
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)


EmbedFn = Callable[[list[str]], npt.NDArray[np.float32]]


class EntityResolver:
    """Resolve surface names to node ids. `embed` maps names to L2-normalized vectors."""

    def __init__(self, embed: EmbedFn | None, threshold: float = 0.90, use_acronyms: bool = True) -> None:
        self.embed = embed
        self.threshold = threshold
        self.use_acronyms = use_acronyms
        self.entities: dict[str, ResolvedEntity] = {}
        self._lookup: dict[tuple[EntityType, str], str] = {}

    def fit(self, mentions: Iterable[Mention]) -> dict[str, ResolvedEntity]:
        by_type: dict[EntityType, list[Mention]] = defaultdict(list)
        for m in mentions:
            if normalize(m.name):
                by_type[m.type].append(m)
        for typ, ms in by_type.items():
            self._fit_type(typ, ms)
        return self.entities

    def _fit_type(self, typ: EntityType, ms: list[Mention]) -> None:
        # Distinct normalized keys are the units we cluster.
        keys: list[str] = []
        idx: dict[str, int] = {}
        surfaces: dict[int, Counter[str]] = defaultdict(Counter)
        alias_sets: dict[int, set[str]] = defaultdict(set)
        for m in ms:
            k = normalize(m.name)
            if k not in idx:
                idx[k] = len(keys)
                keys.append(k)
            surfaces[idx[k]][m.name] += 1
            for a in m.aliases:
                if normalize(a):
                    alias_sets[idx[k]].add(a)
        uf = UnionFind(len(keys))
        # Rule 1: alias declared in text ("Advanced Micro Devices, Inc. ("AMD")").
        for i, aliases in alias_sets.items():
            for a in aliases:
                j = idx.get(normalize(a))
                if j is not None:
                    uf.union(i, j)
        # Rule 2 (companies): same distinctive core once generic trailers and spaces are removed
        # ("Cisco Systems, Inc." = "Cisco", "Global Foundries" = "GLOBALFOUNDRIES").
        if self.use_acronyms and typ == EntityType.COMPANY:
            by_core: dict[str, int] = {}
            for i, k in enumerate(keys):
                c = core_key(k)
                if c and c in by_core:
                    uf.union(i, by_core[c])
                elif c:
                    by_core[c] = i
        # Rule 3: acronym of a multi-word name equals another key (companies only: "AI" etc. are risky).
        if self.use_acronyms and typ == EntityType.COMPANY:
            for i in range(len(keys)):
                for name in surfaces[i]:
                    for acr in acronyms(name):
                        j = idx.get(acr)
                        if j is not None and j != i:
                            uf.union(i, j)
        # Rule 4: embedding similarity above the tuned threshold.
        if self.embed is not None and len(keys) > 1:
            vecs = self.embed(keys)
            sims = vecs @ vecs.T
            n = len(keys)
            for i in range(n):
                for j in range(i + 1, n):
                    if sims[i, j] >= self.threshold and _compatible(keys[i], keys[j]):
                        uf.union(i, j)
        clusters: dict[int, list[int]] = defaultdict(list)
        for i in range(len(keys)):
            clusters[uf.find(i)].append(i)
        for members in clusters.values():
            counts: Counter[str] = Counter()
            declared: set[str] = set()
            for i in members:
                counts.update(surfaces[i])
                declared |= alias_sets[i]
            canonical = sorted(counts.items(), key=lambda kv: (-kv[1], -len(kv[0]), kv[0]))[0][0]
            ent_id = f"{typ.value.lower()}:{slug(normalize(canonical))}"
            names = sorted(set(counts) | declared)
            ent = ResolvedEntity(ent_id, canonical, typ, names, sum(counts.values()))
            if ent_id in self.entities:  # same canonical key from a separate cluster: merge
                prev = self.entities[ent_id]
                prev.aliases = sorted(set(prev.aliases) | set(names))
                prev.mention_count += ent.mention_count
                ent = prev
            self.entities[ent_id] = ent
            for nm in names:
                self._lookup[(typ, normalize(nm))] = ent_id

    def resolve(self, name: str, typ: EntityType) -> str | None:
        return self._lookup.get((typ, normalize(name)))

    def lookup_table(self) -> dict[tuple[EntityType, str], str]:
        return dict(self._lookup)


_NUM = re.compile(r"\d+")


def _compatible(a: str, b: str) -> bool:
    """Block merges the embedding cannot be trusted with: names that differ only by a number
    ("hbm3" vs "hbm4", "3nm" vs "5nm") are different things."""
    return _NUM.findall(a) == _NUM.findall(b)
