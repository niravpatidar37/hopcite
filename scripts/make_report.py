"""Render the README results table from the committed result files (no numbers typed by hand).

Usage: uv run python scripts/make_report.py > results/report.md
"""

from __future__ import annotations

import json

from kgrag.config import REPO_ROOT
from kgrag.spend import SpendLedger

R = REPO_ROOT / "results"
LABELS = {
    "single_hop": "Single-hop",
    "two_hop": "2-hop",
    "three_hop": "3-hop",
    "aggregation": "Aggregation",
    "out_of_scope": "Out-of-scope (must refuse)",
}


def pct(x: float) -> str:
    return f"{100 * x:.0f}%"


def main() -> None:
    s = json.loads((R / "benchmark_summary.json").read_text(encoding="utf-8"))
    h, v = s["systems"]["hybrid"], s["systems"]["vector"]
    print("| Question type | n | Vector-only RAG | Hybrid graph + vector | Delta |")
    print("|---|---:|---:|---:|---:|")
    for c, label in LABELS.items():
        hc, vc = h["by_category"].get(c), v["by_category"].get(c)
        if not hc or not vc:
            continue
        d = hc["accuracy"] - vc["accuracy"]
        print(f"| {label} | {hc['n']} | {pct(vc['accuracy'])} | {pct(hc['accuracy'])} | {d * 100:+.0f} pts |")
    multi = [c for c in ("two_hop", "three_hop")]
    hn = sum(h["by_category"][c]["n"] for c in multi)
    hm = sum(h["by_category"][c]["accuracy"] * h["by_category"][c]["n"] for c in multi) / hn
    vm = sum(v["by_category"][c]["accuracy"] * v["by_category"][c]["n"] for c in multi) / hn
    print(f"| **Multi-hop (2+3 hop)** | {hn} | **{pct(vm)}** | **{pct(hm)}** | **{(hm - vm) * 100:+.0f} pts** |")
    print(
        f"| **All** | {h['n']} | **{pct(v['accuracy'])}** | **{pct(h['accuracy'])}** | "
        f"**{(h['accuracy'] - v['accuracy']) * 100:+.0f} pts** |"
    )
    print()
    print("| Operational | Vector-only | Hybrid |")
    print("|---|---:|---:|")
    print(f"| Mean set-F1 | {v['mean_f1']:.2f} | {h['mean_f1']:.2f} |")
    cv, ch = pct(v["citation_valid_rate"]), pct(h["citation_valid_rate"])
    print(f"| Answers with >=1 validated citation | {cv} | {ch} |")
    print(f"| p50 latency | {v['p50_ms'] / 1000:.1f} s | {h['p50_ms'] / 1000:.1f} s |")
    print(f"| p95 latency | {v['p95_ms'] / 1000:.1f} s | {h['p95_ms'] / 1000:.1f} s |")
    print(f"| LLM cost per query (paid rates) | ${v['usd_per_query']:.5f} | ${h['usd_per_query']:.5f} |")
    ing = s["ingestion"]
    print(f"| One-time graph ingestion (extraction) | - | ${ing['extraction_usd_ledger']:.4f} |")
    led = SpendLedger()
    print()
    print(f"Project spend from the ledger: ${led.project_spend():.4f} (P1 cap $5.00).")
    print(f"Models: answers {s['answer_model']}; router {s['router_model']}; extraction {s['extraction_model']}.")
    hm_n = hn
    print(f"multi_hop_vector={vm:.4f} multi_hop_hybrid={hm:.4f} n={hm_n}")


if __name__ == "__main__":
    main()
