-- 0001: pgvector extension, the chunk table (chunk_id shared with the Neo4j :Chunk nodes) and its indexes.
-- IF NOT EXISTS everywhere so this baselines cleanly on databases built by the pre-migration
-- ensure_schema() code (identical DDL). The embedding dimension (384) must match KGRAG embed_dim;
-- kgrag.migrate refuses to run if they differ.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS chunks (
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
    embedding    vector(384) NOT NULL
);

CREATE INDEX IF NOT EXISTS chunks_doc_idx ON chunks (doc_id);
CREATE INDEX IF NOT EXISTS chunks_entities_idx ON chunks USING gin (entity_ids);
-- HNSW (cosine). m/ef_construction as used for the published recall numbers (results/recall.json).
CREATE INDEX IF NOT EXISTS chunks_hnsw_idx ON chunks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
