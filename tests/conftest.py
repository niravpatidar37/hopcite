from __future__ import annotations

import json
import zlib
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from kgrag.spend import SpendLedger


@pytest.fixture
def ledger(tmp_path: Path) -> SpendLedger:
    return SpendLedger(tmp_path / "ledger.jsonl", project="p1-kgrag", global_cap=25.0, project_cap=5.0)


class FakeRunner:
    """Records every Cypher statement and its params; returns canned rows."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.rows = rows or []

    def run(self, cypher: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        self.calls.append((cypher, params))
        return self.rows


def hash_embed(texts: list[str]) -> np.ndarray:
    """Deterministic toy embedding: character-trigram hashing, L2-normalized."""
    out = np.zeros((len(texts), 256), dtype=np.float32)
    for i, t in enumerate(texts):
        t = f"  {t.lower()}  "
        for j in range(len(t) - 2):
            out[i, zlib.crc32(t[j : j + 3].encode()) % 256] += 1.0
    n = np.linalg.norm(out, axis=1, keepdims=True)
    n[n == 0] = 1
    return out / n


def write_ledger(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
