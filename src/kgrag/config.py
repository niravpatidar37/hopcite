"""Runtime configuration, read from environment variables only.

Locally, `load_env()` first copies ./.env (stack creds) and the shared portfolio env file (LLM keys)
into the environment. In containers `KGRAG_LOAD_DOTENV=0` (set in the Dockerfile) turns that off, so
Cloud Run configuration comes from env vars and Secret Manager references only.

Nothing in here prints or logs secret values.
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(os.environ.get("KGRAG_ROOT") or Path(__file__).resolve().parents[2])
DEFAULT_PORTFOLIO_ENV = "F:/.secrets/portfolio.env"
PROJECT = "p1-kgrag"
MIN_API_KEY_CHARS = 16  # keys shorter than this are refused when the API is reachable off-host

_loaded = False
_TRUE = {"1", "true", "yes", "on"}


def load_env() -> None:
    """Load ./.env then the shared portfolio env file. Existing env wins. No-op when
    KGRAG_LOAD_DOTENV=0 (containers: env vars and Secret Manager only)."""
    global _loaded
    if _loaded:
        return
    if os.environ.get("KGRAG_LOAD_DOTENV", "1").strip().lower() in _TRUE:
        load_dotenv(REPO_ROOT / ".env", override=False)
        portfolio = os.environ.get("PORTFOLIO_ENV_FILE", DEFAULT_PORTFOLIO_ENV)
        if Path(portfolio).is_file():
            load_dotenv(portfolio, override=False)
    _loaded = True


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in _TRUE


def kill_switch_on() -> bool:
    """Operator kill switch: KGRAG_KILL_SWITCH=1 disables every LLM call (API, router, jobs).
    Read on every call, so it also covers long-running processes started before the flag."""
    return env_flag("KGRAG_KILL_SWITCH")


def is_loopback_host(host: str | None) -> bool:
    if not host:
        return False
    if host.strip().lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class Settings:
    neo4j_uri: str = "bolt://127.0.0.1:17687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = field(default="", repr=False)
    neo4j_database: str = "neo4j"
    pg_host: str = "127.0.0.1"
    pg_port: int = 15432
    pg_user: str = "kgrag"
    pg_db: str = "kgrag"
    pg_password: str = field(default="", repr=False)
    pg_sslmode: str = "prefer"
    pg_pool_max: int = 4
    api_key: str = field(default="", repr=False)
    api_host: str = "127.0.0.1"
    api_port: int = 8101
    data_dir: Path = REPO_ROOT / "data"
    cache_dir: Path = REPO_ROOT / "data" / "cache"
    log_dir: Path = REPO_ROOT / "logs"
    extraction_model: str = "openai/gpt-oss-120b"
    answer_model: str = "openai/gpt-oss-120b"
    router_model: str = "openai/gpt-oss-20b"
    router_fallback_models: tuple[str, ...] = ("openai/gpt-oss-safeguard-20b",)
    embed_model: str = "BAAI/bge-small-en-v1.5"
    embed_model_path: str = ""  # pre-baked model dir (Docker image); empty = fastembed cache download
    embed_dim: int = 384
    hnsw_ef_search: int = 80
    vector_top_k: int = 8
    er_threshold: float = 0.96
    router_confidence_floor: float = 0.6
    max_question_chars: int = 500
    # Production controls
    rate_limit_per_min: int = 30  # per client (API key or IP), per instance; 0 disables
    rate_limit_burst: int = 10
    routing_log: str = "file"  # file | postgres | stdout | off
    llm_cache: bool = True  # disk cache of LLM responses (off on Cloud Run: in-memory fs, user text at rest)
    log_format: str = "json"  # json | text
    readiness_timeout_s: float = 3.0
    shutdown_grace_s: int = 8  # Cloud Run sends SIGTERM and allows 10 s

    @property
    def api_keys(self) -> tuple[str, ...]:
        """KGRAG_API_KEY may hold several comma-separated keys so a key can be rotated without
        downtime (add the new key, move clients, remove the old one)."""
        return tuple(k.strip() for k in self.api_key.split(",") if k.strip())

    @property
    def pg_dsn(self) -> str:
        from psycopg.conninfo import make_conninfo  # quotes values (passwords may contain spaces)

        return make_conninfo(
            host=self.pg_host,
            port=self.pg_port,
            dbname=self.pg_db,
            user=self.pg_user,
            password=self.pg_password,
            sslmode=self.pg_sslmode,
            connect_timeout=5,
            application_name="kgrag",
        )


def get_settings() -> Settings:
    load_env()
    return Settings(
        neo4j_uri=_env("NEO4J_URI", "bolt://127.0.0.1:17687"),
        neo4j_user=_env("NEO4J_USER", "neo4j"),
        neo4j_password=_env("NEO4J_PASSWORD"),
        neo4j_database=_env("NEO4J_DATABASE", "neo4j"),
        pg_host=_env("PG_HOST", "127.0.0.1"),
        pg_port=int(_env("PG_PORT", "15432")),
        pg_user=_env("PG_USER", "kgrag"),
        pg_db=_env("PG_DB", "kgrag"),
        pg_password=_env("POSTGRES_PASSWORD"),
        pg_sslmode=_env("PG_SSLMODE", "prefer"),
        pg_pool_max=int(_env("PG_POOL_MAX", "4")),
        api_key=_env("KGRAG_API_KEY"),
        api_host=_env("KGRAG_HOST", "127.0.0.1"),
        # Cloud Run injects PORT; KGRAG_PORT wins when both are set.
        api_port=int(_env("KGRAG_PORT") or _env("PORT") or "8101"),
        embed_model_path=_env("KGRAG_EMBED_MODEL_PATH"),
        hnsw_ef_search=int(_env("KGRAG_EF_SEARCH", "80")),
        er_threshold=float(_env("KGRAG_ER_THRESHOLD", "0.96")),
        rate_limit_per_min=int(_env("KGRAG_RATE_LIMIT_PER_MIN", "30")),
        rate_limit_burst=int(_env("KGRAG_RATE_LIMIT_BURST", "10")),
        routing_log=_env("KGRAG_ROUTING_LOG", "file").strip().lower(),
        llm_cache=env_flag("KGRAG_LLM_CACHE", default=True),
        log_format=_env("KGRAG_LOG_FORMAT", "json").strip().lower(),
        readiness_timeout_s=float(_env("KGRAG_READINESS_TIMEOUT_S", "3")),
        shutdown_grace_s=int(_env("KGRAG_SHUTDOWN_GRACE_S", "8")),
    )
