"""Ask the running hopcite API one question and print the response for a terminal demo.

    python assets/demo/ask.py "Which company is ...?" [--mode hybrid|vector] [--url http://127.0.0.1:8101]

Stdlib only. Everything printed comes from the `POST /ask` response; this script only picks
fields and colours them. Graph facts are filtered to the ones that mention an answer entity (the
count of all facts is printed next to them).
"""

from __future__ import annotations

import argparse
import json
import os
import textwrap
import urllib.request

B, DIM, TEAL, AMB, RED, GRN, R = "\033[1m", "\033[2m", "\033[36m", "\033[33m", "\033[31m", "\033[32m", "\033[0m"
WIDTH = 98


def short(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("--mode", default="hybrid", choices=["hybrid", "vector"])
    ap.add_argument("--url", default="http://127.0.0.1:8101")
    ap.add_argument("--facts", type=int, default=4)
    a = ap.parse_args()
    if not a.url.startswith(("http://127.0.0.1", "http://localhost")):
        raise SystemExit("demo client only talks to a loopback API")
    body = json.dumps({"question": a.question, "mode": a.mode}).encode()
    headers = {"content-type": "application/json"}
    if os.environ.get("KGRAG_API_KEY"):
        headers["X-API-Key"] = os.environ["KGRAG_API_KEY"].split(",")[0]
    req = urllib.request.Request(a.url + "/ask", data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=300) as r:  # loopback only, checked above
        resp = json.loads(r.read())

    print(
        f"{DIM}mode{R} {B}{resp['mode']}{R}   {DIM}route{R} {B}{resp['route']}{R}   "
        f"{DIM}template{R} {resp['template'] or '-'}   {DIM}server latency{R} {resp['latency_ms'] / 1000:.2f} s"
    )
    facts = resp["graph_facts"]
    if facts:
        ents = [e.lower() for e in resp["answer_entities"]]
        hits = [f for f in facts if any(e in f.lower() for e in ents)] or facts
        print(
            f"{TEAL}graph facts{R} {DIM}({min(len(hits), a.facts)} shown of {len(facts)}; those naming the answer){R}"
        )
        for f in hits[: a.facts]:
            print(f"  {TEAL}•{R} {short(f, WIDTH - 4)}")
    colour = RED if resp["refused"] else GRN
    print(f"{colour}{B}answer{R}")
    for line in textwrap.wrap(resp["answer"], WIDTH - 2):
        print(f"  {line}")
    if resp["claims"]:
        print(f"{AMB}claims -> citations{R}")
        for c in resp["claims"]:
            print(f"  {short(c['text'], WIDTH - 2)}")
            print(f"    {AMB}cites {', '.join(c['citations'])}{R}")
    if resp["citations"]:
        print(f"{AMB}citations{R} {DIM}(each id checked by the server against the retrieved chunks){R}")
        for c in resp["citations"]:
            print(f"  {AMB}{c['chunk_id']}{R}  {DIM}{short(c['snippet'], WIDTH - len(c['chunk_id']) - 4)}{R}")


if __name__ == "__main__":
    main()
