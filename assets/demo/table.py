"""Print the committed hybrid-vs-vector benchmark table for the terminal demo.

    python assets/demo/table.py

Reads results/benchmark_summary.json (the same file scripts/make_report.py renders into the README).
"""

from __future__ import annotations

import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
B, DIM, TEAL, R = "\033[1m", "\033[2m", "\033[36m", "\033[0m"
LABELS = {
    "single_hop": "Single-hop",
    "two_hop": "2-hop",
    "three_hop": "3-hop",
    "aggregation": "Aggregation",
    "out_of_scope": "Out-of-scope (refuse)",
}


def main() -> None:
    s = json.loads((ROOT / "results" / "benchmark_summary.json").read_text(encoding="utf-8"))
    h, v = s["systems"]["hybrid"], s["systems"]["vector"]
    print(f"{DIM}results/benchmark_summary.json, {s['questions']} questions, same answer model{R}")
    print(f"{B}{'question type':<24}{'n':>4}{'vector-only':>14}{'hybrid':>10}{'delta':>10}{R}")

    def row(label: str, n: int, va: float, ha: float, bold: bool = False) -> None:
        line = f"{label:<24}{n:>4}{va * 100:>13.0f}%{ha * 100:>9.0f}%{(ha - va) * 100:>+6.0f} pts"
        print(f"{TEAL}{B}{line}{R}" if bold else line)

    for c, label in LABELS.items():
        hc, vc = h["by_category"][c], v["by_category"][c]
        row(label, hc["n"], vc["accuracy"], hc["accuracy"])
    multi = ("two_hop", "three_hop")
    n = sum(h["by_category"][c]["n"] for c in multi)
    hm = sum(h["by_category"][c]["accuracy"] * h["by_category"][c]["n"] for c in multi) / n
    vm = sum(v["by_category"][c]["accuracy"] * v["by_category"][c]["n"] for c in multi) / n
    row("Multi-hop (2+3 hop)", n, vm, hm, bold=True)
    row("All", h["n"], v["accuracy"], h["accuracy"])
    cv, ch = v["citation_valid_rate"] * 100, h["citation_valid_rate"] * 100
    print(f"{DIM}answers with >=1 validated citation: vector {cv:.0f}%, hybrid {ch:.0f}%{R}")


if __name__ == "__main__":
    main()
