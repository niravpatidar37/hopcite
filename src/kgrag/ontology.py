"""Constrained ontology (defined before any extraction code) and the strict extraction schema."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class EntityType(StrEnum):
    COMPANY = "Company"
    PRODUCT = "Product"  # a named product or product family (e.g. "Blackwell", "Snapdragon")
    TECHNOLOGY = "Technology"  # a technology, process node, architecture or standard (e.g. "EUV", "x86")
    SEGMENT = "Segment"  # a reporting/business segment of a company (e.g. "Data Center")
    MARKET = "Market"  # an end market / application area (e.g. "automotive", "smartphones")
    LOCATION = "Location"  # a country, region or city
    PERSON = "Person"  # a named executive


class RelationType(StrEnum):
    COMPETES_WITH = "COMPETES_WITH"  # Company -> Company (symmetric in meaning)
    SUPPLIES_TO = "SUPPLIES_TO"  # supplier Company -> customer Company (parts, equipment, IP, software)
    MANUFACTURES_FOR = "MANUFACTURES_FOR"  # foundry / assembly / test Company -> customer Company
    CUSTOMER_OF = "CUSTOMER_OF"  # normalized away to SUPPLIES_TO at write time (see below)
    PARTNERS_WITH = "PARTNERS_WITH"  # Company -> Company (alliance, collaboration, JV)
    ACQUIRED = "ACQUIRED"  # acquirer Company -> acquired Company
    SUBSIDIARY_OF = "SUBSIDIARY_OF"  # subsidiary Company -> parent Company
    OFFERS = "OFFERS"  # Company -> Product
    HAS_SEGMENT = "HAS_SEGMENT"  # Company -> Segment
    USES_TECHNOLOGY = "USES_TECHNOLOGY"  # Company|Product -> Technology
    SERVES_MARKET = "SERVES_MARKET"  # Company|Product|Segment -> Market
    HEADQUARTERED_IN = "HEADQUARTERED_IN"  # Company -> Location
    HAS_FACILITY_IN = "HAS_FACILITY_IN"  # Company -> Location (fab, R&D, manufacturing site)
    EXECUTIVE_OF = "EXECUTIVE_OF"  # Person -> Company


# Relation types whose subject/object are always companies (used by templates and validation).
COMPANY_RELATIONS = {
    RelationType.COMPETES_WITH,
    RelationType.SUPPLIES_TO,
    RelationType.MANUFACTURES_FOR,
    RelationType.PARTNERS_WITH,
    RelationType.ACQUIRED,
    RelationType.SUBSIDIARY_OF,
}

# Allowed (subject type, object type) pairs per relation. Extracted triples outside these are dropped.
ALLOWED_TYPES: dict[RelationType, tuple[set[EntityType], set[EntityType]]] = {
    RelationType.COMPETES_WITH: ({EntityType.COMPANY}, {EntityType.COMPANY}),
    RelationType.SUPPLIES_TO: ({EntityType.COMPANY}, {EntityType.COMPANY}),
    RelationType.MANUFACTURES_FOR: ({EntityType.COMPANY}, {EntityType.COMPANY}),
    RelationType.CUSTOMER_OF: ({EntityType.COMPANY}, {EntityType.COMPANY}),
    RelationType.PARTNERS_WITH: ({EntityType.COMPANY}, {EntityType.COMPANY}),
    RelationType.ACQUIRED: ({EntityType.COMPANY}, {EntityType.COMPANY}),
    RelationType.SUBSIDIARY_OF: ({EntityType.COMPANY}, {EntityType.COMPANY}),
    RelationType.OFFERS: ({EntityType.COMPANY}, {EntityType.PRODUCT}),
    RelationType.HAS_SEGMENT: ({EntityType.COMPANY}, {EntityType.SEGMENT}),
    RelationType.USES_TECHNOLOGY: ({EntityType.COMPANY, EntityType.PRODUCT}, {EntityType.TECHNOLOGY}),
    RelationType.SERVES_MARKET: ({EntityType.COMPANY, EntityType.PRODUCT, EntityType.SEGMENT}, {EntityType.MARKET}),
    RelationType.HEADQUARTERED_IN: ({EntityType.COMPANY}, {EntityType.LOCATION}),
    RelationType.HAS_FACILITY_IN: ({EntityType.COMPANY}, {EntityType.LOCATION}),
    RelationType.EXECUTIVE_OF: ({EntityType.PERSON}, {EntityType.COMPANY}),
}

# Human-readable verbs used when graph paths are turned into sentences.
RELATION_VERBS: dict[RelationType, str] = {
    RelationType.COMPETES_WITH: "competes with",
    RelationType.SUPPLIES_TO: "supplies",
    RelationType.MANUFACTURES_FOR: "manufactures products for",
    RelationType.CUSTOMER_OF: "is a customer of",
    RelationType.PARTNERS_WITH: "partners with",
    RelationType.ACQUIRED: "acquired",
    RelationType.SUBSIDIARY_OF: "is a subsidiary of",
    RelationType.OFFERS: "offers",
    RelationType.HAS_SEGMENT: "has the business segment",
    RelationType.USES_TECHNOLOGY: "uses the technology",
    RelationType.SERVES_MARKET: "serves the market",
    RelationType.HEADQUARTERED_IN: "is headquartered in",
    RelationType.HAS_FACILITY_IN: "has facilities in",
    RelationType.EXECUTIVE_OF: "is an executive of",
}


class ExtractedEntity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120, description="canonical name as written in the text")
    type: EntityType
    chunk_id: str = Field(description="id of the chunk where the entity is mentioned")
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("name")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = " ".join(v.split())
        if not any(ch.isalnum() for ch in v):
            raise ValueError("entity name has no alphanumeric characters")
        return v


class ExtractedRelation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject: str = Field(min_length=1, max_length=120)
    subject_type: EntityType
    relation: RelationType
    object: str = Field(min_length=1, max_length=120)
    object_type: EntityType
    chunk_id: str = Field(description="id of the chunk whose text states this relation")
    evidence: str = Field(max_length=400, description="short verbatim quote from the chunk")
    confidence: float = Field(ge=0.0, le=1.0)


class ExtractionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entities: list[ExtractedEntity] = Field(default_factory=list, max_length=200)
    relations: list[ExtractedRelation] = Field(default_factory=list, max_length=200)


def ontology_prompt() -> str:
    ents = "\n".join(f"- {e.value}" for e in EntityType)
    rels = "\n".join(
        f"- {r.value}: {'/'.join(sorted(t.value for t in ALLOWED_TYPES[r][0]))} -> "
        f"{'/'.join(sorted(t.value for t in ALLOWED_TYPES[r][1]))} ({RELATION_VERBS[r]})"
        for r in RelationType
    )
    return f"Entity types:\n{ents}\n\nRelation types (subject -> object):\n{rels}"
