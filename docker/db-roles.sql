-- Local/CI equivalent of Terraform's google_sql_user.app: the API's runtime login role.
-- Idempotent. The password comes from the environment (psql \getenv), never from this file.
-- Privileges are granted by the migrations' R__grants.sql, not here.
\getenv app_pw PG_APP_PASSWORD
SELECT 'CREATE ROLE kgrag_app LOGIN' WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kgrag_app')
\gexec
SELECT format('ALTER ROLE kgrag_app WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD %L', :'app_pw')
\gexec
