from __future__ import annotations

from kgrag.ontology import EntityType
from kgrag.resolve import EntityResolver, Mention, acronyms, normalize
from tests.conftest import hash_embed

C = EntityType.COMPANY


def test_normalize_strips_legal_suffixes() -> None:
    assert normalize("Acme Corp.") == normalize("Acme Corporation") == normalize("ACME") == "acme"
    assert normalize("Advanced Micro Devices, Inc. (“AMD”)") == "advanced micro devices"


def test_acme_variants_collapse_into_one_node() -> None:
    r = EntityResolver(hash_embed, threshold=0.9)
    ents = r.fit([Mention("Acme Corp", C), Mention("Acme Corporation", C), Mention("ACME", C)])
    assert len(ents) == 1
    node = next(iter(ents.values()))
    assert set(node.aliases) == {"Acme Corp", "Acme Corporation", "ACME"}
    assert r.resolve("acme corp.", C) == node.id


def test_declared_alias_and_acronym_rules() -> None:
    r = EntityResolver(None)
    ents = r.fit(
        [
            Mention("Advanced Micro Devices, Inc.", C, ["AMD"]),
            Mention("AMD", C),
            Mention("Taiwan Semiconductor Manufacturing Company Limited", C),
            Mention("TSMC", C),
        ]
    )
    assert len(ents) == 2
    assert r.resolve("AMD", C) == r.resolve("Advanced Micro Devices", C)
    assert r.resolve("TSMC", C) == r.resolve("Taiwan Semiconductor Manufacturing Company", C)
    assert "tsmc" in acronyms("Taiwan Semiconductor Manufacturing Company Limited")


def test_generic_trailers_merge_but_distinctive_suffixes_do_not() -> None:
    r = EntityResolver(None)
    r.fit([Mention("Cisco", C), Mention("Cisco Systems, Inc.", C), Mention("Global Foundries", C),
           Mention("GLOBALFOUNDRIES Inc.", C), Mention("ASML", C), Mention("ASML Holding N.V.", C),
           Mention("Sony", C), Mention("Sony Semiconductor Solutions Corporation", C),
           Mention("TSMC", C), Mention("TSMC Arizona", C)])  # fmt: skip
    assert r.resolve("Cisco", C) == r.resolve("Cisco Systems, Inc.", C)
    assert r.resolve("Global Foundries", C) == r.resolve("GLOBALFOUNDRIES Inc.", C)
    assert r.resolve("ASML", C) == r.resolve("ASML Holding N.V.", C)
    assert r.resolve("Sony", C) != r.resolve("Sony Semiconductor Solutions Corporation", C)
    assert r.resolve("TSMC", C) != r.resolve("TSMC Arizona", C)


def test_distinct_companies_stay_apart() -> None:
    r = EntityResolver(hash_embed, threshold=0.9)
    ents = r.fit(
        [
            Mention("Intel Corporation", C),
            Mention("AMD", C),
            Mention("Applied Materials, Inc.", C),
            Mention("Analog Devices, Inc.", C),
        ]
    )
    assert len(ents) == 4


def test_numbered_variants_never_merge_by_embedding() -> None:
    def same(texts: list[str]):  # pathological embedder: everything identical
        import numpy as np

        return np.ones((len(texts), 4), dtype="float32") / 2.0

    r = EntityResolver(same, threshold=0.5)
    ents = r.fit([Mention("HBM3", EntityType.PRODUCT), Mention("HBM4", EntityType.PRODUCT)])
    assert len(ents) == 2


def test_types_are_resolved_separately() -> None:
    r = EntityResolver(None)
    ents = r.fit([Mention("Data Center", EntityType.SEGMENT), Mention("Data Center", EntityType.MARKET)])
    assert len(ents) == 2
