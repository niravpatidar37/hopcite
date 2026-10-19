-- 0003: state that must be shared by every API instance (Cloud Run runs several and replaces them).
-- Additive only (new tables and indexes), so the previous revision keeps working after this runs.
--
-- rate_limit_buckets: one token bucket per client key, refilled and debited by a single
--   INSERT ... ON CONFLICT DO UPDATE (kgrag.ratelimit.PostgresRateLimiter). Rows of idle clients are
--   pruned; a missing row means a full bucket.
-- llm_spend_ledger: the LLM spend cap ledger (kgrag.spend.PostgresSpendLedger). A row is inserted as
--   'reserved' at the worst-case estimate inside the same transaction as the cap check, then 'settled'
--   at the actual cost or 'released' (usd = 0) when the provider returned an error. Durable, unlike the
--   per-instance /tmp JSONL ledger it replaces on Cloud Run.
CREATE TABLE IF NOT EXISTS rate_limit_buckets (
    key        text PRIMARY KEY,
    tokens     double precision NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    allowed    boolean NOT NULL DEFAULT true
);

CREATE INDEX IF NOT EXISTS rate_limit_buckets_updated_idx ON rate_limit_buckets (updated_at);

CREATE TABLE IF NOT EXISTS llm_spend_ledger (
    id            bigserial PRIMARY KEY,
    ts            timestamptz NOT NULL DEFAULT now(),
    project       text NOT NULL,
    status        text NOT NULL CHECK (status IN ('reserved', 'settled', 'released')),
    provider      text,
    model         text,
    input_tokens  int,
    output_tokens int,
    usd           numeric(14, 8) NOT NULL CHECK (usd >= 0),
    estimate_usd  numeric(14, 8) NOT NULL DEFAULT 0,
    settled_at    timestamptz
);

CREATE INDEX IF NOT EXISTS llm_spend_ledger_project_idx ON llm_spend_ledger (project);
CREATE INDEX IF NOT EXISTS llm_spend_ledger_ts_idx ON llm_spend_ledger (ts);
