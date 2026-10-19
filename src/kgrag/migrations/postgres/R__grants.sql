-- Repeatable (runs after the versioned migrations on every `migrate up`; must stay idempotent).
-- Least privilege for the runtime role used by the API service (Cloud Run): read chunks, append
-- routing logs, read/write its rate-limit buckets and spend-ledger rows, nothing else (no DELETE on the
-- ledger, no DDL). The role is created out of band (Terraform google_sql_user
-- `kgrag_app`); locally it usually does not exist and this is a no-op.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kgrag_app') THEN
        REVOKE ALL ON ALL TABLES IN SCHEMA public FROM kgrag_app;
        GRANT USAGE ON SCHEMA public TO kgrag_app;
        GRANT SELECT ON chunks TO kgrag_app;
        GRANT INSERT ON routing_log TO kgrag_app;
        GRANT USAGE ON SEQUENCE routing_log_id_seq TO kgrag_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON rate_limit_buckets TO kgrag_app;
        GRANT SELECT, INSERT, UPDATE ON llm_spend_ledger TO kgrag_app;
        GRANT USAGE ON SEQUENCE llm_spend_ledger_id_seq TO kgrag_app;

        -- Cloud SQL makes every API-created user a member of cloudsqlsuperuser. Drop that membership
        -- so the grants above are the runtime role's only privileges. If the migrating user lacks the
        -- right to revoke it, warn instead of failing; `python -m kgrag.migrate roles` reports it (exit 3),
        -- /readyz shows db_least_privilege=false, and docs/RUNBOOK.md has the manual fix.
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cloudsqlsuperuser')
           AND pg_has_role('kgrag_app', 'cloudsqlsuperuser', 'MEMBER') THEN
            BEGIN
                EXECUTE 'REVOKE cloudsqlsuperuser FROM kgrag_app';
            EXCEPTION WHEN insufficient_privilege THEN
                RAISE WARNING 'could not revoke cloudsqlsuperuser from kgrag_app; revoke it as the postgres user';
            END;
        END IF;
    END IF;
END
$$;
