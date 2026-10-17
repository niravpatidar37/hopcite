"""Hard spend cap shared by all portfolio projects.

Append-only JSONL ledger (one line per paid call) guarded by a file lock because several projects
write to it concurrently. Before every call the caller reserves a worst-case estimate; the call is
refused with BudgetExceeded if either the global cap or this project's cap would be crossed.
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
    if provider == "ollama":
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

    def _raise_if_over(self, total: float, project_total: float, estimate_usd: float) -> None:
        if total + estimate_usd > self.global_cap:
            raise BudgetExceeded(f"global spend {total:.4f} + estimate {estimate_usd:.4f} > cap {self.global_cap:.2f}")
        if project_total + estimate_usd > self.project_cap:
            raise BudgetExceeded(
                f"{self.project} spend {project_total:.4f} + estimate {estimate_usd:.4f} "
                f"> project cap {self.project_cap:.2f}"
            )

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
