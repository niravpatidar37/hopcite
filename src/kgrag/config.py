"""Runtime configuration. Secrets are read from the environment / the shared portfolio env file.

Nothing in here prints or logs secret values.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(os.environ.get("KGRAG_ROOT") or Path(__file__).resolve().parents[2])
DEFAULT_PORTFOLIO_ENV = "F:/.secrets/portfolio.env"
PROJECT = "p1-kgrag"

_loaded = False


def load_env() -> None:
    """Load ./.env (stack creds) then the shared portfolio env file (LLM keys). Existing env wins."""
    global _loaded
    if _loaded:
        return
    load_dotenv(REPO_ROOT / ".env", override=False)
    portfolio = os.environ.get("PORTFOLIO_ENV_FILE", DEFAULT_PORTFOLIO_ENV)
    if Path(portfolio).is_file():
        load_dotenv(portfolio, override=False)
    _loaded = True


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    neo4j_uri: str = "bolt://127.0.0.1:17687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = field(default="", repr=False)
    pg_host: str = "127.0.0.1"
    pg_port: int = 15432
    pg_user: str = "kgrag"
    pg_db: str = "kgrag"
    pg_password: str = field(default="", repr=False)
    api_key: str = field(default="", repr=False)
    api_host: str = "127.0.0.1"
    api_port: int = 8101
    data_dir: Path = REPO_ROOT / "data"
    cache_dir: Path = REPO_ROOT / "data" / "cache"
    log_dir: Path = REPO_ROOT / "logs"
    extraction_model: str = "openai/gpt-oss-120b"
    answer_model: str = "openai/gpt-oss-120b"
    router_model: str = "openai/gpt-oss-20b"
    embed_model: str = "BAAI/bge-small-en-v1.5"
    embed_dim: int = 384
    hnsw_ef_search: int = 80
    vector_top_k: int = 8
    er_threshold: float = 0.96
    router_confidence_floor: float = 0.6
    max_question_chars: int = 500

    @property
    def pg_dsn(self) -> str:
        return (
            f"host={self.pg_host} port={self.pg_port} dbname={self.pg_db} "
            f"user={self.pg_user} password={self.pg_password}"
        )


def get_settings() -> Settings:
    load_env()
    return Settings(
        neo4j_uri=_env("NEO4J_URI", "bolt://127.0.0.1:17687"),
        neo4j_password=_env("NEO4J_PASSWORD"),
        pg_host=_env("PG_HOST", "127.0.0.1"),
        pg_port=int(_env("PG_PORT", "15432")),
        pg_password=_env("POSTGRES_PASSWORD"),
        api_key=_env("KGRAG_API_KEY"),
        api_host=_env("KGRAG_HOST", "127.0.0.1"),
        api_port=int(_env("KGRAG_PORT", "8101")),
        hnsw_ef_search=int(_env("KGRAG_EF_SEARCH", "80")),
        er_threshold=float(_env("KGRAG_ER_THRESHOLD", "0.96")),
    )
