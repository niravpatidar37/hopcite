-- 0002: routing decisions and outcomes per /ask call (the JSONL routing log, as a table for Cloud Run,
-- whose filesystem is ephemeral). The question is stored PII-redacted (kgrag.router.redact_pii).
CREATE TABLE IF NOT EXISTS routing_log (
    id                bigserial PRIMARY KEY,
    ts                timestamptz NOT NULL DEFAULT now(),
    mode              text NOT NULL,
    question_redacted text NOT NULL,
    model_route       text,
    model_confidence  real,
    query_type        text,
    hop_relations     jsonb,
    final_route       text NOT NULL,
    fallback_reason   text NOT NULL DEFAULT '',
    router_model_used text,
    template          text,
    entity_ids        text[] NOT NULL DEFAULT '{}',
    n_graph_facts     int,
    n_passages        int,
    outcome           text NOT NULL CHECK (outcome IN ('answered', 'refused')),
    n_citations       int,
    regenerations     int,
    latency_ms        real,
    usd               numeric(14, 8)
);

CREATE INDEX IF NOT EXISTS routing_log_ts_idx ON routing_log (ts);
CREATE INDEX IF NOT EXISTS routing_log_final_route_idx ON routing_log (final_route, ts);
