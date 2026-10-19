"""FastAPI service: POST /ask -> answer + validated citations + route.

Endpoints
- GET  /healthz  liveness: the process is up and serving (no dependency calls). Cloud Run liveness probe.
- GET  /readyz   readiness: Neo4j and Postgres both answer within KGRAG_READINESS_TIMEOUT_S, else 503.
                 Cloud Run startup probe and the deploy smoke test.
- GET  /health   legacy local status (store connectivity and model names).
- POST /ask      the question endpoint.

Controls on /ask (in this order)
1. Auth. If KGRAG_API_KEY is set (comma-separated keys allowed, for rotation) every call needs a
   matching `X-API-Key` (constant-time compare). If it is not set, only loopback clients are served.
   Startup refuses to bind a non-loopback host (KGRAG_HOST) without an API key of at least
   MIN_API_KEY_CHARS characters, so a container or Cloud Run revision cannot come up open.
2. Rate limit: token bucket per API key / client (KGRAG_RATE_LIMIT_PER_MIN, KGRAG_RATE_LIMIT_BURST).
   KGRAG_RATE_LIMIT_BACKEND=postgres shares the buckets across instances (required in staging/prod);
   if the shared limiter is unreachable the request fails closed with 503.
3. Kill switch: KGRAG_KILL_SWITCH=1 answers 503 without touching retrieval or any LLM; the LLM
   client refuses calls too (defence in depth for jobs and scripts).
Then body validation (422). Invalid bodies still spend a rate-limit token (abuse costs the caller).

Every response carries security headers; request bodies over MAX_BODY_BYTES get 413 before parsing;
unhandled errors return a generic JSON 500 (the exception type is logged, never the traceback).
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from kgrag.config import (
    DEPLOYED_ENVS,
    MIN_API_KEY_CHARS,
    Settings,
    assert_runtime_config,
    get_settings,
    is_loopback_host,
    kill_switch_on,
)
from kgrag.ratelimit import Limiter, LimiterUnavailable, RateLimiter, client_key, retry_after_header

MAX_QUESTION_CHARS = 500
MAX_BODY_BYTES = 16 * 1024  # a 500-char question in JSON is < 4 KB; anything far larger is refused (413)
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
}
DOCS_PATHS = ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect")
log = logging.getLogger("kgrag.api")


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=3, max_length=MAX_QUESTION_CHARS)
    mode: Literal["hybrid", "vector"] = "hybrid"

    @field_validator("question")
    @classmethod
    def _clean(cls, v: str) -> str:
        v = "".join(ch for ch in v if ch.isprintable() or ch in "\n\t")
        v = v.strip()
        if len(v) < 3:
            raise ValueError("question too short")
        return v


class Citation(BaseModel):
    chunk_id: str
    doc_id: str
    company: str
    snippet: str


class AskResponse(BaseModel):
    answer: str
    refused: bool
    route: str
    mode: str
    citations: list[Citation]
    claims: list[dict[str, Any]]
    answer_entities: list[str]
    graph_facts: list[str]
    template: str | None
    latency_ms: float
    usd: float
    # Every chunk id that was in the answer model's context. Citations are validated against this set.
    retrieved_chunk_ids: list[str] = Field(default_factory=list)
    # Why the router did not decide the route (router_disabled, router_error:..., low_confidence:...).
    fallback_reason: str = ""


class UnsafeBinding(RuntimeError):
    """The API would listen off-host without a usable API key."""


def assert_safe_binding(s: Settings) -> None:
    """Fail closed at startup: a non-loopback bind needs at least one strong API key."""
    if is_loopback_host(s.api_host):
        return
    keys = s.api_keys
    if not keys:
        raise UnsafeBinding(f"KGRAG_API_KEY is required when KGRAG_HOST={s.api_host!r} is not loopback")
    if any(len(k) < MIN_API_KEY_CHARS for k in keys):
        raise UnsafeBinding(f"every KGRAG_API_KEY must be at least {MIN_API_KEY_CHARS} characters")


def _is_loopback(host: str | None) -> bool:
    return is_loopback_host(host)


def _key_matches(given: str, keys: tuple[str, ...]) -> bool:
    ok = False
    for k in keys:  # compare against every key (no early exit) so timing does not reveal which matched
        ok |= hmac.compare_digest(given.encode(), k.encode())
    return ok


def require_access(request: Request, x_api_key: Annotated[str | None, Header()] = None) -> None:
    keys = get_settings().api_keys
    if keys:
        if not x_api_key or not _key_matches(x_api_key, keys):
            raise HTTPException(status_code=401, detail="invalid or missing API key")
        return
    client = request.client.host if request.client else None
    if not _is_loopback(client):
        raise HTTPException(status_code=403, detail="set KGRAG_API_KEY to serve non-local clients")


def build_limiter(s: Settings) -> Limiter:
    if s.rate_limit_backend == "postgres":
        from kgrag.ratelimit import PostgresRateLimiter

        return PostgresRateLimiter(s.pg_dsn, s.rate_limit_per_min, s.rate_limit_burst)
    return RateLimiter(s.rate_limit_per_min, s.rate_limit_burst)


def create_app(services: Any | None = None, limiter: Limiter | None = None) -> FastAPI:
    boot = get_settings()
    state: dict[str, Any] = {"services": services, "draining": False, "limiter": limiter}
    if limiter is None and boot.rate_limit_backend != "postgres":
        state["limiter"] = RateLimiter(boot.rate_limit_per_min, boot.rate_limit_burst)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        s = get_settings()
        assert_safe_binding(s)
        assert_runtime_config(s)  # no per-instance limits in staging/prod, no fake LLM outside dev/ci/test
        if state["limiter"] is None:
            state["limiter"] = await run_in_threadpool(build_limiter, s)
        if state["services"] is None:
            from kgrag.service import build_services

            state["services"] = await run_in_threadpool(build_services)
        least = await _least_privilege(state["services"], s.readiness_timeout_s)
        if least is False:
            # Log-based metric + alert in infra/terraform (monitoring.tf); fix in docs/RUNBOOK.md 2e.
            log.error("db role not least-privilege", extra={"fields": {"env": s.env}})
        log.info("startup complete", extra={"fields": {"kill_switch": kill_switch_on(), "env": s.env}})
        try:
            yield
        finally:
            # SIGTERM (Cloud Run scale-in / new revision): uvicorn stops accepting, drains in-flight
            # requests for up to KGRAG_SHUTDOWN_GRACE_S, then this closes pools and drivers.
            state["draining"] = True
            svc = state["services"]
            if services is None and svc is not None and hasattr(svc, "close"):
                await run_in_threadpool(svc.close)
            lim = state["limiter"]
            if limiter is None and hasattr(lim, "close"):
                await run_in_threadpool(lim.close)
            log.info("shutdown complete")

    expose_docs = boot.env not in DEPLOYED_ENVS
    app = FastAPI(
        title="hopcite",
        version="0.2.0",
        lifespan=lifespan,
        docs_url="/docs" if expose_docs else None,
        redoc_url="/redoc" if expose_docs else None,
        openapi_url="/openapi.json" if expose_docs else None,
    )

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Generic body: no exception text, no traceback (they can carry user text or connection strings).
        log.error("unhandled error", extra={"fields": {"path": request.url.path, "error": type(exc).__name__}})
        return JSONResponse({"detail": "internal error"}, status_code=500, headers=SECURITY_HEADERS)

    @app.middleware("http")
    async def security(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        declared = request.headers.get("content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > MAX_BODY_BYTES):
            response: Response = JSONResponse({"detail": "request body too large"}, status_code=413)
        elif declared is None and request.method in ("POST", "PUT", "PATCH"):
            # Chunked uploads: read at most MAX_BODY_BYTES + 1 before deciding.
            body = b""
            async for chunk in request.stream():
                body += chunk
                if len(body) > MAX_BODY_BYTES:
                    break
            if len(body) > MAX_BODY_BYTES:
                response = JSONResponse({"detail": "request body too large"}, status_code=413)
            else:
                request._body = body  # downstream handlers re-read the buffered body
                response = await call_next(request)
        else:
            response = await call_next(request)
        if request.url.path not in DOCS_PATHS:  # the Swagger UI needs scripts (local only)
            for k, v in SECURITY_HEADERS.items():
                response.headers.setdefault(k, v)
        return response

    @app.middleware("http")
    async def access_log(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        t0 = time.perf_counter()
        response = await call_next(request)
        if request.url.path not in ("/healthz", "/readyz"):
            trace = (request.headers.get("x-cloud-trace-context") or "").split("/")[0]
            log.info(
                "request",
                extra={
                    "fields": {
                        "method": request.method,
                        "path": request.url.path,
                        "status": response.status_code,
                        "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                    },
                    "trace": trace or None,
                },
            )
        return response

    async def _check(fn: Callable[[], Any], timeout_s: float) -> bool:
        try:
            return bool(await asyncio.wait_for(run_in_threadpool(fn), timeout=timeout_s))
        except Exception:
            return False

    def _graph_ping(svc: Any) -> Callable[[], Any]:
        if hasattr(svc.runner, "ping"):
            return lambda: svc.runner.ping()
        return lambda: svc.runner.run("RETURN 1 AS ok", {}) is not None

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        svc = state["services"]
        timeout = get_settings().readiness_timeout_s
        checks = {"neo4j": False, "postgres": False}
        least: bool | None = None
        if svc is not None and not state["draining"]:
            neo, pg = await asyncio.gather(_check(_graph_ping(svc), timeout), _check(svc.vectors.ping, timeout))
            checks = {"neo4j": neo, "postgres": pg}
            if pg:
                least = await _least_privilege(svc, timeout)
        ready = all(checks.values())
        body = {
            "status": "ready" if ready else "not_ready",
            **checks,
            "llm_enabled": not kill_switch_on(),
            # Reported, not gating: false means the runtime DB user kept superuser-like rights
            # (e.g. Cloud SQL cloudsqlsuperuser). None when unknown (no Postgres or a test double).
            "db_least_privilege": least,
        }
        return JSONResponse(body, status_code=200 if ready else 503)

    @app.get("/health")
    def health() -> dict[str, Any]:
        svc = state["services"]
        checks: dict[str, Any] = {"graph": False, "vectors": False}
        if svc is not None:
            try:
                svc.runner.run("RETURN 1 AS ok", {})
                checks["graph"] = True
            except Exception:
                checks["graph"] = False
            try:
                checks["vectors"] = bool(svc.vectors.ping())
            except Exception:
                checks["vectors"] = False
            checks["answer_model"] = f"{svc.answer_provider}/{svc.answer_model}"
            checks["router_model"] = svc.router_model
        status = "ok" if checks["graph"] and checks["vectors"] else "degraded"
        return {"status": status, **checks}

    def rate_limit(request: Request, x_api_key: Annotated[str | None, Header()] = None) -> None:
        lim: Limiter | None = state["limiter"]
        if lim is None:
            raise HTTPException(status_code=503, detail="service not ready")
        try:
            wait = lim.acquire(client_key(x_api_key, request.client.host if request.client else None))
        except LimiterUnavailable:
            log.error("rate limiter unavailable")
            raise HTTPException(
                status_code=503, detail="rate limiter unavailable", headers={"Retry-After": "5"}
            ) from None
        if wait > 0:
            raise HTTPException(
                status_code=429, detail="rate limit exceeded", headers={"Retry-After": retry_after_header(wait)}
            )

    def kill_switch() -> None:
        if kill_switch_on():
            raise HTTPException(
                status_code=503, detail="LLM calls are disabled by the operator", headers={"Retry-After": "300"}
            )

    @app.post(
        "/ask",
        response_model=AskResponse,
        dependencies=[Depends(require_access), Depends(rate_limit), Depends(kill_switch)],
    )
    async def ask(req: AskRequest) -> AskResponse:
        svc = state["services"]
        if svc is None:
            raise HTTPException(status_code=503, detail="service not ready")
        res = await run_in_threadpool(svc.pipeline.ask, req.question, req.mode)
        return AskResponse(
            answer=res.answer,
            refused=res.refused,
            route=res.route,
            mode=res.mode,
            citations=[Citation(**c) for c in res.citations],
            claims=res.claims,
            answer_entities=res.answer_entities,
            graph_facts=res.graph_facts[:30],
            template=res.template,
            latency_ms=res.latency_ms,
            usd=res.usd,
            retrieved_chunk_ids=list(getattr(res, "retrieved_chunk_ids", []) or []),
            fallback_reason=str(getattr(res, "fallback_reason", "") or ""),
        )

    return app


async def _least_privilege(svc: Any, timeout_s: float) -> bool | None:
    fn = getattr(getattr(svc, "vectors", None), "least_privilege", None)
    if fn is None:
        return None
    try:
        return bool(await asyncio.wait_for(run_in_threadpool(fn), timeout=timeout_s))
    except Exception:
        return None


def main() -> None:
    import uvicorn

    from kgrag.logs import configure_logging

    configure_logging()
    s = get_settings()
    assert_safe_binding(s)  # fail before binding the socket, not only at lifespan startup
    assert_runtime_config(s)
    uvicorn.run(
        create_app(),
        host=s.api_host,
        port=s.api_port,
        log_config=None,  # keep the JSON handler from configure_logging
        access_log=False,  # the request middleware logs method/path/status/latency (no client IPs)
        timeout_graceful_shutdown=s.shutdown_grace_s,
        proxy_headers=False,  # rate-limit keys and loopback checks use the socket peer, not X-Forwarded-For
        server_header=False,  # do not advertise the server software
    )


if __name__ == "__main__":
    main()
