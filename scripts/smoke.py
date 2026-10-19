#!/usr/bin/env python3
"""End-to-end smoke test for a running hopcite API. Stdlib only (runs on any CI runner or laptop).

    python scripts/smoke.py --base-url http://127.0.0.1:8101 --json-out results/smoke.json
    SMOKE_API_KEY=... SMOKE_ID_TOKEN=... python scripts/smoke.py --base-url https://kgrag-staging-api-xxxx.a.run.app

Checks (each prints one PASS / FAIL / SKIP line; exit 1 if any FAIL):
  liveness             GET /healthz is 200 {"status": "ok"} (retried while the service warms up)
  readiness            GET /readyz is 200 with neo4j=true and postgres=true (dependencies really pinged)
  db_least_privilege   /readyz reports the runtime DB user holds no superuser-like role
                       (FAIL only with --require-least-privilege; otherwise reported)
  auth_missing_key     POST /ask without X-API-Key -> 401
  auth_wrong_key       POST /ask with a wrong key -> 401
  auth_valid_key       POST /ask with the key gets past auth (an invalid body then answers 422, not 401)
  input_validation     too-short question, unknown field, bad mode, malformed JSON -> 422
  oversize_question    a 501-char question -> 422
  oversize_body        a 64 KB body -> 413 (refused before parsing)
  security_headers     nosniff / DENY / no-store / CSP on success and error responses, no Server banner
  no_stack_traces      error bodies (401/404/413/422) are JSON with no traceback or file paths
  ask_schema_citations an in-scope question returns a schema-valid answer whose every citation (and every
                       claim citation) is one of the response's retrieved_chunk_ids
  ask_out_of_scope     an out-of-scope question is refused with no citations
  router_fallback      route is a valid enum; with --expect-router-fallback the route must be the
                       deterministic hybrid fallback (no router LLM, e.g. no Groq key)
  kill_switch          /readyz llm_enabled agrees with /ask (503 + Retry-After when the switch is on);
                       with --kill-switch-base-url, a second instance with the switch ON must refuse /ask
                       with 503 while /healthz and /readyz stay 200
  rate_limit           a burst of (cheap, invalid) requests with the key ends in 429 with Retry-After
                       (skip with --skip-rate-limit, e.g. in canary soak loops)

The API key is read from the env var named by --api-key-env (default SMOKE_API_KEY), and a Cloud Run
ID token (Authorization: Bearer) from --id-token-env (default SMOKE_ID_TOKEN). Neither is printed.
Two /ask calls hit the LLM path: a few cents at most on Groq, $0 on local Ollama or the CI fake LLM.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any

# Defaults: a fact stated in NVIDIA's 10-K (in the CI seed fixture and the full index) and a
# question the filings cannot answer. Override with --question / --out-of-scope-question.
IN_SCOPE = "Which foundries does NVIDIA use to produce its semiconductor wafers?"
OUT_OF_SCOPE = "Who painted the Mona Lisa?"
ROUTES = {"graph", "vector", "hybrid", "out_of_scope"}
FALLBACK_PREFIXES = ("router_disabled", "router_error")
REQUIRED_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "cache-control": "no-store",
}
TRACE_MARKERS = ("Traceback", 'File "', "site-packages", "/app/.venv", "psycopg.", "neo4j.exceptions")
ASK_FIELDS: dict[str, type | tuple[type, ...]] = {
    "answer": str,
    "refused": bool,
    "route": str,
    "mode": str,
    "citations": list,
    "claims": list,
    "answer_entities": list,
    "graph_facts": list,
    "template": (str, type(None)),
    "latency_ms": (int, float),
    "usd": (int, float),
    "retrieved_chunk_ids": list,
    "fallback_reason": str,
}
CITATION_FIELDS = ("chunk_id", "doc_id", "company", "snippet")


@dataclass
class Resp:
    status: int
    headers: dict[str, str]
    body: bytes
    ms: float

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


@dataclass
class Check:
    name: str
    status: str  # PASS | FAIL | SKIP
    detail: str
    ms: float = 0.0


@dataclass
class Smoke:
    base: str
    api_key: str
    id_token: str
    timeout: float
    checks: list[Check] = field(default_factory=list)

    # ------------------------------------------------------------------ http
    def call(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        key: str | None = None,
        base: str | None = None,
        content_type: str = "application/json",
        timeout: float | None = None,
    ) -> Resp:
        url = (base or self.base) + path
        if not url.startswith(("http://", "https://")):
            raise ValueError("base URL must be http(s)")
        req = urllib.request.Request(url, data=body, method=method)  # noqa: S310 - scheme checked above
        if body is not None:
            req.add_header("Content-Type", content_type)
        if key:
            req.add_header("X-API-Key", key)
        if self.id_token:
            req.add_header("Authorization", f"Bearer {self.id_token}")
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:  # noqa: S310  # nosec B310
                data, status, headers = r.read(), r.status, r.headers
        except urllib.error.HTTPError as e:
            data, status, headers = e.read(), e.code, e.headers
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            return Resp(0, {}, str(getattr(e, "reason", e)).encode(), (time.perf_counter() - t0) * 1000)
        return Resp(status, {k.lower(): v for k, v in headers.items()}, data, (time.perf_counter() - t0) * 1000)

    def ask(self, payload: Any, key: str | None = None, base: str | None = None, timeout: float | None = None) -> Resp:
        return self.call("POST", "/ask", body=json.dumps(payload).encode(), key=key, base=base, timeout=timeout)

    # ------------------------------------------------------------------ bookkeeping
    def record(self, name: str, ok: bool | None, detail: str, ms: float = 0.0) -> bool:
        status = "SKIP" if ok is None else ("PASS" if ok else "FAIL")
        self.checks.append(Check(name, status, detail, round(ms, 1)))
        print(f"{status:4} {name:22} {detail}", flush=True)
        return bool(ok)


# ---------------------------------------------------------------------- checks
def check_liveness(s: Smoke, wait_s: float) -> bool:
    deadline = time.monotonic() + wait_s
    while True:
        r = s.call("GET", "/healthz", timeout=5)
        if r.status == 200 and (r.json() or {}).get("status") == "ok":
            return s.record("liveness", True, f"/healthz 200 in {r.ms:.0f} ms", r.ms)
        if time.monotonic() > deadline:
            return s.record("liveness", False, f"/healthz {r.status} after {wait_s:.0f} s: {r.text[:120]}")
        time.sleep(2)


def check_readiness(s: Smoke, wait_s: float, require_least: bool) -> dict[str, Any]:
    deadline = time.monotonic() + wait_s
    while True:
        r = s.call("GET", "/readyz", timeout=10)
        body = r.json() or {}
        if r.status == 200 and body.get("neo4j") is True and body.get("postgres") is True:
            s.record("readiness", True, f"/readyz 200 neo4j=true postgres=true in {r.ms:.0f} ms", r.ms)
            break
        if time.monotonic() > deadline:
            s.record("readiness", False, f"/readyz {r.status} {json.dumps(body)[:160]}")
            return body
        time.sleep(2)
    least = body.get("db_least_privilege")
    if least is True:
        s.record("db_least_privilege", True, "runtime DB user holds no superuser-like role")
    elif require_least:
        s.record("db_least_privilege", False, f"db_least_privilege={least!r} (see RUNBOOK 2e)")
    else:
        s.record("db_least_privilege", None, f"db_least_privilege={least!r} (reported only)")
    return body


def check_auth(s: Smoke) -> None:
    payload = {"question": IN_SCOPE}
    if not s.api_key:
        for name in ("auth_missing_key", "auth_wrong_key", "auth_valid_key"):
            s.record(name, None, "no API key given (--api-key-env); auth checks need one")
        return
    r = s.ask(payload)
    s.record("auth_missing_key", r.status == 401, f"no key -> {r.status} (want 401)", r.ms)
    r = s.ask(payload, key="smoke-wrong-key-" + "x" * 16)
    s.record("auth_wrong_key", r.status == 401, f"wrong key -> {r.status} (want 401)", r.ms)


def check_auth_accepted(s: Smoke) -> None:
    if not s.api_key:
        return
    r = s.ask({"question": ""}, key=s.api_key)
    s.record("auth_valid_key", r.status == 422, f"valid key + empty question -> {r.status} (want 422, past auth)", r.ms)


def check_validation(s: Smoke) -> None:
    k = s.api_key or None
    cases = [
        ("too short", s.ask({"question": "hi"}, key=k)),
        ("unknown field", s.ask({"question": "Who supplies TSMC?", "cypher": "MATCH (n) DETACH DELETE n"}, key=k)),
        ("bad mode", s.ask({"question": "Who supplies TSMC?", "mode": "cypher"}, key=k)),
        ("malformed json", s.call("POST", "/ask", body=b'{"question": ', key=k)),
    ]
    bad = [f"{n}={r.status}" for n, r in cases if r.status != 422]
    s.record("input_validation", not bad, "4 bad inputs -> 422" if not bad else "not 422: " + ", ".join(bad))
    r = s.ask({"question": "x" * 501}, key=k)
    s.record("oversize_question", r.status == 422, f"501-char question -> {r.status} (want 422)", r.ms)
    big = json.dumps({"question": "y" * 65536}).encode()
    r = s.call("POST", "/ask", body=big, key=k)
    s.record("oversize_body", r.status == 413, f"64 KB body -> {r.status} (want 413)", r.ms)


def check_headers_and_errors(s: Smoke) -> None:
    k = s.api_key or None
    samples = {
        "healthz": s.call("GET", "/healthz"),
        "404": s.call("GET", "/definitely-not-a-route"),
        "422": s.ask({"question": ""}, key=k),
        "413": s.call("POST", "/ask", body=b"z" * 70000, key=k),
    }
    if s.api_key:
        samples["401"] = s.ask({"question": IN_SCOPE})
    missing = []
    for name, r in samples.items():
        for h, v in REQUIRED_HEADERS.items():
            if r.headers.get(h) != v:
                missing.append(f"{name}:{h}")
        if "content-security-policy" not in r.headers:
            missing.append(f"{name}:content-security-policy")
        if "server" in r.headers and "uvicorn" in r.headers["server"].lower():
            missing.append(f"{name}:server-banner")
    s.record(
        "security_headers",
        not missing,
        f"{len(samples)} responses carry nosniff/DENY/no-store/CSP" if not missing else "missing " + ", ".join(missing),
    )
    leaks = []
    for name, r in samples.items():
        if name == "healthz":
            continue
        if r.json() is None:
            leaks.append(f"{name}:not-json")
        if any(m in r.text for m in TRACE_MARKERS):
            leaks.append(f"{name}:trace")
    s.record(
        "no_stack_traces", not leaks, "error bodies are JSON without tracebacks" if not leaks else ", ".join(leaks)
    )


def _schema_errors(body: Any) -> list[str]:
    if not isinstance(body, dict):
        return ["body is not a JSON object"]
    errs = [f"{f}:{type(body.get(f)).__name__}" for f, t in ASK_FIELDS.items() if not isinstance(body.get(f), t)]
    for i, c in enumerate(body.get("citations") or []):
        if not isinstance(c, dict) or any(not isinstance(c.get(f), str) for f in CITATION_FIELDS):
            errs.append(f"citations[{i}]")
    for i, c in enumerate(body.get("claims") or []):
        if not isinstance(c, dict) or not isinstance(c.get("text"), str) or not isinstance(c.get("citations"), list):
            errs.append(f"claims[{i}]")
    if isinstance(body.get("route"), str) and body["route"] not in ROUTES:
        errs.append(f"route={body['route']}")
    return errs


def check_functional(s: Smoke, expect_fallback: bool, ask_timeout: float, question: str, oos: str) -> None:
    k = s.api_key or None
    r = s.ask({"question": question}, key=k, timeout=ask_timeout)
    body = r.json()
    if r.status != 200:
        s.record("ask_schema_citations", False, f"/ask {r.status}: {r.text[:160]}", r.ms)
        s.record("router_fallback", False, "no /ask response")
    else:
        errs = _schema_errors(body)
        retrieved = set(body.get("retrieved_chunk_ids") or []) if isinstance(body, dict) else set()
        cites = [c.get("chunk_id") for c in body.get("citations") or []] if not errs else []
        claim_cites = [cid for c in body.get("claims") or [] for cid in c.get("citations") or []] if not errs else []
        unresolved = sorted({c for c in [*cites, *claim_cites] if c not in retrieved})
        bad_doc = [
            c["chunk_id"]
            for c in body.get("citations") or []
            if not errs and c["doc_id"] != c["chunk_id"].split("#")[0]
        ]
        ok = not errs and not body["refused"] and bool(cites) and not unresolved and not bad_doc
        detail = (
            f"200 in {r.ms:.0f} ms, {len(cites)} citation(s), all in {len(retrieved)} retrieved chunk ids"
            if ok
            else f"schema={errs} refused={body.get('refused') if isinstance(body, dict) else '?'} "
            f"citations={len(cites)} unresolved={unresolved[:3]} doc_mismatch={bad_doc[:3]}"
        )
        s.record("ask_schema_citations", ok, detail, r.ms)
        route, reason = body.get("route"), body.get("fallback_reason", "")
        if expect_fallback:
            ok_fb = route == "hybrid" and str(reason).startswith(FALLBACK_PREFIXES)
            s.record("router_fallback", ok_fb, f"route={route} fallback_reason={reason!r} (want hybrid + router_*)")
        else:
            s.record("router_fallback", route in ROUTES, f"route={route} fallback_reason={reason!r}")
    r = s.ask({"question": oos}, key=k, timeout=ask_timeout)
    body = r.json() or {}
    ok = r.status == 200 and body.get("refused") is True and body.get("citations") == []
    s.record(
        "ask_out_of_scope",
        ok,
        f"{r.status} refused={body.get('refused')} citations={len(body.get('citations') or [])}",
        r.ms,
    )


def check_kill_switch(s: Smoke, ready: dict[str, Any], ks_base: str | None) -> None:
    k = s.api_key or None
    enabled = ready.get("llm_enabled")
    if enabled is False:
        r = s.ask({"question": IN_SCOPE}, key=k)
        ok = r.status == 503 and "retry-after" in r.headers
        s.record("kill_switch", ok, f"switch ON here: /ask {r.status} Retry-After={r.headers.get('retry-after')}")
        return
    if ks_base is None:
        s.record("kill_switch", enabled is True, f"switch off (llm_enabled={enabled}); /ask served above")
        return
    h = s.call("GET", "/healthz", base=ks_base)
    rd = s.call("GET", "/readyz", base=ks_base)
    rdb = rd.json() or {}
    r = s.ask({"question": IN_SCOPE}, key=k, base=ks_base)
    ok = (
        h.status == 200
        and rd.status == 200
        and rdb.get("llm_enabled") is False
        and r.status == 503
        and "retry-after" in r.headers
        and (r.json() or {}).get("detail", "").startswith("LLM calls are disabled")
    )
    s.record(
        "kill_switch",
        ok,
        f"switch-on instance: healthz {h.status}, readyz {rd.status} llm_enabled={rdb.get('llm_enabled')}, "
        f"/ask {r.status} Retry-After={r.headers.get('retry-after')}",
    )


def check_rate_limit(s: Smoke, max_requests: int) -> None:
    if not s.api_key:
        s.record("rate_limit", None, "needs an API key (the bucket is per key)")
        return
    t0 = time.perf_counter()
    for i in range(1, max_requests + 1):
        r = s.ask({"question": ""}, key=s.api_key)  # 422 after the limiter: no retrieval, no LLM
        if r.status == 429:
            ra = r.headers.get("retry-after", "")
            ok = ra.isdigit() and int(ra) >= 1
            s.record("rate_limit", ok, f"429 after {i} requests, Retry-After={ra}", (time.perf_counter() - t0) * 1000)
            return
        if r.status not in (422, 503):
            s.record("rate_limit", False, f"unexpected {r.status} at request {i}")
            return
    s.record("rate_limit", False, f"no 429 within {max_requests} requests")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--api-key-env", default="SMOKE_API_KEY")
    ap.add_argument("--id-token-env", default="SMOKE_ID_TOKEN")
    ap.add_argument("--json-out")
    ap.add_argument("--wait", type=float, default=60.0, help="seconds to wait for /healthz and /readyz")
    ap.add_argument("--timeout", type=float, default=15.0)
    ap.add_argument("--ask-timeout", type=float, default=60.0)
    ap.add_argument("--expect-router-fallback", action="store_true")
    ap.add_argument("--require-least-privilege", action="store_true")
    ap.add_argument("--kill-switch-base-url")
    ap.add_argument("--skip-rate-limit", action="store_true")
    ap.add_argument("--rate-limit-max-requests", type=int, default=60)
    ap.add_argument("--label", default="", help="free text recorded in the JSON summary (e.g. local-dev)")
    ap.add_argument("--question", default=IN_SCOPE, help="in-scope question that must be answered with citations")
    ap.add_argument("--out-of-scope-question", default=OUT_OF_SCOPE, help="question that must be refused")
    a = ap.parse_args(argv)

    s = Smoke(
        a.base_url.rstrip("/"),
        os.environ.get(a.api_key_env, "").split(",")[0].strip(),
        os.environ.get(a.id_token_env, "").strip(),
        a.timeout,
    )
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    t0 = time.perf_counter()
    print(f"smoke {s.base} (api key: {'yes' if s.api_key else 'no'}, id token: {'yes' if s.id_token else 'no'})")
    if check_liveness(s, a.wait):
        ready = check_readiness(s, a.wait, a.require_least_privilege)
        check_auth(s)
        # Functional /ask calls (and the kill-switch /ask) first: the checks after them spend the
        # key's rate-limit tokens on invalid bodies, and the last check drains the bucket on purpose.
        if ready.get("llm_enabled") is False:
            s.record("ask_schema_citations", None, "kill switch is on")
            s.record("ask_out_of_scope", None, "kill switch is on")
            s.record("router_fallback", None, "kill switch is on")
        else:
            check_functional(s, a.expect_router_fallback, a.ask_timeout, a.question, a.out_of_scope_question)
        check_kill_switch(s, ready, a.kill_switch_base_url.rstrip("/") if a.kill_switch_base_url else None)
        check_auth_accepted(s)
        check_validation(s)
        check_headers_and_errors(s)
        if a.skip_rate_limit:
            s.record("rate_limit", None, "--skip-rate-limit")
        else:
            check_rate_limit(s, a.rate_limit_max_requests)
    duration = time.perf_counter() - t0
    counts = {st: sum(1 for c in s.checks if c.status == st) for st in ("PASS", "FAIL", "SKIP")}
    summary = {
        "base_url": s.base,
        "label": a.label,
        "question": a.question,
        "out_of_scope_question": a.out_of_scope_question,
        "started_at": started,
        "duration_s": round(duration, 2),
        "passed": counts["PASS"],
        "failed": counts["FAIL"],
        "skipped": counts["SKIP"],
        "total": len(s.checks),
        "ok": counts["FAIL"] == 0,
        "checks": [asdict(c) for c in s.checks],
    }
    print(
        f"SMOKE {'PASS' if summary['ok'] else 'FAIL'}: {counts['PASS']} passed, {counts['FAIL']} failed, "
        f"{counts['SKIP']} skipped in {duration:.1f} s"
    )
    if a.json_out:
        os.makedirs(os.path.dirname(os.path.abspath(a.json_out)), exist_ok=True)
        with open(a.json_out, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(summary, fh, indent=2)
            fh.write("\n")
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
