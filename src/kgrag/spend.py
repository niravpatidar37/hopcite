"""Hard LLM spend cap.

Protocol (both backends): before a paid call the caller `reserve()`s the worst-case cost; the call is
refused with BudgetExceeded if the global cap or this project's cap would be crossed. After the call
it `settle()`s the actual cost, or `release()`s the reservation when the provider answered with an
error (nothing was billed). A reservation that is never settled keeps counting at its worst case
(fail safe: a crash mid-call cannot hide spend).

- SpendLedger (KGRAG_SPEND_BACKEND=file, local default): the append-only JSONL ledger shared by all
  portfolio projects on this machine, guarded by a file lock. Check and record are separate lock
  holds, so two processes can both pass the check before either records (acceptable on one laptop).
- PostgresSpendLedger (KGRAG_SPEND_BACKEND=postgres, required in staging/prod): the
  `llm_spend_ledger` table (migration 0003). reserve() takes a transaction-scoped advisory lock,
  sums the ledger, checks both caps and inserts the reservation row in ONE transaction, so concurrent
  callers on any number of Cloud Run instances serialize and the cap holds globally. The ledger is
  durable: replacing an instance does not reset spend (the old /tmp JSONL did).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import portalocker

from kgrag.config import PROJECT, load_env

PRICING_PATH = Path(__file__).with_name("pricing.json")
PROJECT_CAPS_USD = {
    "p1-kgrag": 5.0,
    "p2-research": 3.0,
    "p3-gateway": 1.0,
    "p4-judge": 5.0,
    "p5-distill": 7.0,
    "p6-redteam": 3.0,
}
DEFAULT_GLOBAL_CAP = 25.0
_LOCK_FLAGS = portalocker.LOCK_EX | portalocker.LOCK_NB  # non-blocking + timeout => retried acquire
CHARS_PER_TOKEN = 3.5  # conservative (over-estimates tokens for English prose)


class BudgetExceeded(RuntimeError):
    """Raised before a paid call when the estimate would cross a cap."""


def raise_if_over(
    total: float, project_total: float, estimate_usd: float, global_cap: float, project_cap: float, project: str
) -> None:
    """The cap rule shared by both ledgers: refuse if either the global or the project cap would be crossed."""
    if total + estimate_usd > global_cap:
        raise BudgetExceeded(f"global spend {total:.4f} + estimate {estimate_usd:.4f} > cap {global_cap:.2f}")
    if project_total + estimate_usd > project_cap:
        raise BudgetExceeded(
            f"{project} spend {project_total:.4f} + estimate {estimate_usd:.4f} > project cap {project_cap:.2f}"
        )


@dataclass
class Reservation:
    """Handle returned by reserve(); `id` is the ledger row (Postgres) or None (file backend)."""

    estimate_usd: float
    id: int | None = None


@dataclass(frozen=True)
class Price:
    input_per_m: float
    output_per_m: float

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return (input_tokens * self.input_per_m + output_tokens * self.output_per_m) / 1_000_000


def load_prices() -> dict[str, dict[str, Price]]:
    raw = json.loads(PRICING_PATH.read_text(encoding="utf-8"))
    out: dict[str, dict[str, Price]] = {}
    for provider, models in raw.items():
        if provider.startswith("_") or not isinstance(models, dict):
            continue
        out[provider] = {
            m: Price(float(p["input"]), float(p["output"])) for m, p in models.items() if not m.startswith("_")
        }
    return out


def price_for(provider: str, model: str) -> Price:
    prices = load_prices()
    if provider in ("ollama", "fake"):  # local model / deterministic CI stand-in: $0
        return Price(0.0, 0.0)
    try:
        return prices[provider][model]
    except KeyError as exc:  # unknown paid model: refuse rather than guess $0
        raise BudgetExceeded(f"no price configured for {provider}/{model}; refusing paid call") from exc


def estimate_tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN) + 16


class SpendLedger:
    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        project: str = PROJECT,
        global_cap: float | None = None,
        project_cap: float | None = None,
    ) -> None:
        load_env()
        self.path = Path(path or os.environ.get("LLM_SPEND_LEDGER", "F:/.llm-spend/ledger.jsonl"))
        self.project = project
        self.global_cap = (
            global_cap if global_cap is not None else float(os.environ.get("LLM_SPEND_CAP_USD", DEFAULT_GLOBAL_CAP))
        )
        self.project_cap = project_cap if project_cap is not None else PROJECT_CAPS_USD.get(project, 0.0)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)

    def _read_totals(self, fh: Any) -> tuple[float, float]:
        fh.seek(0)
        total = 0.0
        project_total = 0.0
        for line in fh.read().splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                usd = float(row.get("usd", 0.0))
            except (ValueError, TypeError, AttributeError):
                # Fail safe: an unreadable line cannot be shown to be cheap. Count it at the
                # project cap so a corrupted ledger blocks spending instead of allowing it.
                usd = self.project_cap
                row = {"project": self.project}
            total += usd
            if row.get("project") == self.project:
                project_total += usd
        return total, project_total

    def totals(self) -> tuple[float, float]:
        # Exclusive lock for reads too: msvcrt has no true shared lock on Windows.
        with portalocker.Lock(str(self.path), mode="a+", flags=_LOCK_FLAGS, timeout=30) as fh:
            return self._read_totals(fh)

    def check(self, estimate_usd: float) -> None:
        total, project_total = self.totals()
        self._raise_if_over(total, project_total, estimate_usd)

    def reserve(self, estimate_usd: float) -> Reservation:
        self.check(estimate_usd)
        return Reservation(estimate_usd)

    def settle(
        self, res: Reservation, provider: str, model: str, input_tokens: int, output_tokens: int, usd: float
    ) -> None:
        if usd > 0:
            self.record(provider, model, input_tokens, output_tokens, usd)

    def release(self, res: Reservation) -> None:
        """Nothing was written at reserve() time in the file backend."""

    def _raise_if_over(self, total: float, project_total: float, estimate_usd: float) -> None:
        raise_if_over(total, project_total, estimate_usd, self.global_cap, self.project_cap, self.project)

    def record(self, provider: str, model: str, input_tokens: int, output_tokens: int, usd: float) -> None:
        row = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "project": self.project,
            "provider": provider,
            "model": model,
            "input_tokens": int(input_tokens),
            "output_tokens": int(output_tokens),
            "usd": round(float(usd), 8),
        }
        with portalocker.Lock(str(self.path), mode="a", flags=_LOCK_FLAGS, timeout=30) as fh:
            fh.write(json.dumps(row) + "\n")
            fh.flush()

    def project_spend(self) -> float:
        return self.totals()[1]


# --------------------------------------------------------------------------- shared Postgres ledger
SPEND_LOCK_KEY = 0x6B677370  # "kgsp": pg_advisory_xact_lock key that serializes reservations

PG_TOTALS = """
SELECT COALESCE(SUM(usd), 0)::float8,
       COALESCE(SUM(usd) FILTER (WHERE project = %(project)s), 0)::float8
FROM llm_spend_ledger
"""
PG_RESERVE = """
INSERT INTO llm_spend_ledger (project, status, usd, estimate_usd)
VALUES (%(project)s, 'reserved', %(usd)s, %(usd)s)
RETURNING id
"""
PG_SETTLE = """
UPDATE llm_spend_ledger
SET status = 'settled', provider = %(provider)s, model = %(model)s, input_tokens = %(in)s,
    output_tokens = %(out)s, usd = %(usd)s, settled_at = now()
WHERE id = %(id)s AND status = 'reserved'
"""
PG_RELEASE = """
UPDATE llm_spend_ledger SET status = 'released', usd = 0, settled_at = now()
WHERE id = %(id)s AND status = 'reserved'
"""


class PostgresSpendLedger:
    """Spend ledger shared by every instance and job through Postgres (see module docstring)."""

    def __init__(
        self,
        dsn: str | None = None,
        project: str = PROJECT,
        global_cap: float | None = None,
        project_cap: float | None = None,
        pool: Any | None = None,
    ) -> None:
        load_env()
        self.project = project
        self.global_cap = (
            global_cap if global_cap is not None else float(os.environ.get("LLM_SPEND_CAP_USD", DEFAULT_GLOBAL_CAP))
        )
        self.project_cap = project_cap if project_cap is not None else PROJECT_CAPS_USD.get(project, 0.0)
        self._own_pool = pool is None
        if pool is None:
            from psycopg_pool import ConnectionPool

            from kgrag.config import get_settings

            pool = ConnectionPool(
                dsn or get_settings().pg_dsn,
                min_size=1,
                max_size=2,
                kwargs={"autocommit": True},
                check=ConnectionPool.check_connection,
                timeout=10.0,
                open=True,
                name="kgrag-spend",
            )
        self._pool = pool

    def totals(self) -> tuple[float, float]:
        with self._pool.connection() as conn:
            row = conn.execute(PG_TOTALS, {"project": self.project}).fetchone()
        return float(row[0]), float(row[1])

    def check(self, estimate_usd: float) -> None:
        total, project_total = self.totals()
        self._raise_if_over(total, project_total, estimate_usd)

    def _raise_if_over(self, total: float, project_total: float, estimate_usd: float) -> None:
        raise_if_over(total, project_total, estimate_usd, self.global_cap, self.project_cap, self.project)

    def reserve(self, estimate_usd: float) -> Reservation:
        """Cap check and reservation in one transaction, serialized across instances by an advisory lock."""
        with self._pool.connection() as conn, conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (SPEND_LOCK_KEY,))
            row = conn.execute(PG_TOTALS, {"project": self.project}).fetchone()
            self._raise_if_over(float(row[0]), float(row[1]), estimate_usd)  # raising rolls back
            rid = conn.execute(PG_RESERVE, {"project": self.project, "usd": estimate_usd}).fetchone()
        return Reservation(estimate_usd, int(rid[0]))

    def settle(
        self, res: Reservation, provider: str, model: str, input_tokens: int, output_tokens: int, usd: float
    ) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                PG_SETTLE,
                {
                    "id": res.id,
                    "provider": provider,
                    "model": model,
                    "in": int(input_tokens),
                    "out": int(output_tokens),
                    "usd": round(float(usd), 8),
                },
            )

    def release(self, res: Reservation) -> None:
        with self._pool.connection() as conn:
            conn.execute(PG_RELEASE, {"id": res.id})

    def record(self, provider: str, model: str, input_tokens: int, output_tokens: int, usd: float) -> None:
        """Record spend that already happened (no cap check: the money is spent either way)."""
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO llm_spend_ledger (project, status, provider, model, input_tokens, output_tokens, usd, "
                "estimate_usd, settled_at) VALUES (%s, 'settled', %s, %s, %s, %s, %s, 0, now())",
                (self.project, provider, model, int(input_tokens), int(output_tokens), round(float(usd), 8)),
            )

    def project_spend(self) -> float:
        return self.totals()[1]

    def close(self) -> None:
        if self._own_pool:
            self._pool.close()


def default_ledger() -> SpendLedger | PostgresSpendLedger:
    """KGRAG_SPEND_BACKEND=postgres -> shared table; anything else -> the local JSONL ledger."""
    load_env()
    if os.environ.get("KGRAG_SPEND_BACKEND", "file").strip().lower() == "postgres":
        return PostgresSpendLedger()
    return SpendLedger()
