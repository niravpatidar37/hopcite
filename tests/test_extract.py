from __future__ import annotations

from kgrag.chunking import Chunk, chunk_document
from kgrag.extract import Batch, ExtractionPayload, ValidationReport, evidence_supported, validate_payload
from kgrag.ontology import EntityType, ExtractedRelation, RelationType

TEXT = (
    "We utilize foundries, such as Taiwan Semiconductor Manufacturing Company Limited, or TSMC, "
    "to produce our semiconductor wafers. Our competitors include Advanced Micro Devices, Inc."
)


def _batch() -> Batch:
    chunk = Chunk("NVDA-10-K-2026#c0001", "NVDA-10-K-2026", 1, "s", TEXT)
    return Batch("NVDA-10-K-2026", "NVIDIA Corporation", "sha", 0, [chunk])


def _rel(**kw: object) -> ExtractedRelation:
    base: dict[str, object] = dict(
        subject="Taiwan Semiconductor Manufacturing Company Limited",
        subject_type=EntityType.COMPANY,
        relation=RelationType.MANUFACTURES_FOR,
        object="NVIDIA Corporation",
        object_type=EntityType.COMPANY,
        chunk_id="NVDA-10-K-2026#c0001",
        evidence="We utilize foundries, such as Taiwan Semiconductor",
        confidence=0.9,
    )
    base.update(kw)
    return ExtractedRelation.model_validate(base)


def test_valid_relation_kept_and_endpoints_become_mentions() -> None:
    rep = ValidationReport()
    out = validate_payload(ExtractionPayload(relations=[_rel()]), _batch(), rep)
    assert len(out.relations) == 1 and rep.relations_kept == 1
    names = {e.name for e in out.entities}
    assert names == {"Taiwan Semiconductor Manufacturing Company Limited", "NVIDIA Corporation"}


def test_drops_chunk_id_not_in_batch() -> None:
    rep = ValidationReport()
    out = validate_payload(ExtractionPayload(relations=[_rel(chunk_id="OTHER#c0009")]), _batch(), rep)
    assert out.relations == [] and rep.dropped_bad_chunk == 1


def test_drops_type_violations() -> None:
    rep = ValidationReport()
    bad = _rel(relation=RelationType.HEADQUARTERED_IN, object_type=EntityType.COMPANY)
    out = validate_payload(ExtractionPayload(relations=[bad]), _batch(), rep)
    assert out.relations == [] and rep.dropped_bad_types == 1


def test_drops_unsupported_evidence() -> None:
    rep = ValidationReport()
    bad = _rel(evidence="Intel acquired Qualcomm for one trillion dollars")
    out = validate_payload(ExtractionPayload(relations=[bad]), _batch(), rep)
    assert out.relations == [] and rep.dropped_unsupported == 1


def test_customer_of_normalized_to_supplies_to() -> None:
    rep = ValidationReport()
    r = _rel(
        relation=RelationType.CUSTOMER_OF,
        subject="NVIDIA Corporation",
        object="Taiwan Semiconductor Manufacturing Company Limited",
    )
    out = validate_payload(ExtractionPayload(relations=[r]), _batch(), rep)
    assert out.relations[0].relation == RelationType.SUPPLIES_TO
    assert out.relations[0].subject.startswith("Taiwan") and out.relations[0].object == "NVIDIA Corporation"


def test_evidence_supported_tolerates_quotes_and_whitespace() -> None:
    assert evidence_supported("such as  Taiwan Semiconductor\nManufacturing", TEXT)
    assert not evidence_supported("", TEXT)


def test_chunk_ids_stable_and_bounded() -> None:
    text = "Item 1. Business\nOverview\n" + "\n".join(f"Paragraph {i}. " + "word " * 60 for i in range(30))
    a = chunk_document("X-10-K-2025", "Item 1. Business", text)
    b = chunk_document("X-10-K-2025", "Item 1. Business", text)
    assert [c.chunk_id for c in a] == [c.chunk_id for c in b]
    assert all(c.chunk_id.startswith("X-10-K-2025#c") for c in a)
    assert all(len(c.text) <= 2000 for c in a)
    assert a[0].sha256 == b[0].sha256
