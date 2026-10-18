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
3. Kill switch: KGRAG_KILL_SWITCH=1 answers 503 without touching retrieval or any LLM; the LLM
   client refuses calls too (defence in depth for jobs and scripts).
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

from kgrag.config import MIN_API_KEY_CHARS, Settings, get_settings, is_loopback_host, kill_switch_on
from kgrag.ratelimit import RateLimiter, client_key, retry_after_header

MAX_QUESTION_CHARS = 500
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


def create_app(services: Any | None = None) -> FastAPI:
    boot = get_settings()
    state: dict[str, Any] = {"services": services, "draining": False}
    limiter = RateLimiter(boot.rate_limit_per_min, boot.rate_limit_burst)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        assert_safe_binding(get_settings())
        if state["services"] is None:
            from kgrag.service import build_services

            state["services"] = await run_in_threadpool(build_services)
        log.info("startup complete", extra={"fields": {"kill_switch": kill_switch_on()}})
        try:
            yield
        finally:
            # SIGTERM (Cloud Run scale-in / new revision): uvicorn stops accepting, drains in-flight
            # requests for up to KGRAG_SHUTDOWN_GRACE_S, then this closes pools and drivers.
            state["draining"] = True
            svc = state["services"]
            if services is None and svc is not None and hasattr(svc, "close"):
                await run_in_threadpool(svc.close)
            log.info("shutdown complete")

    app = FastAPI(title="hopcite", version="0.2.0", lifespan=lifespan)

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
        if svc is not None and not state["draining"]:
            neo, pg = await asyncio.gather(_check(_graph_ping(svc), timeout), _check(svc.vectors.ping, timeout))
            checks = {"neo4j": neo, "postgres": pg}
        ready = all(checks.values())
        body = {"status": "ready" if ready else "not_ready", **checks, "llm_enabled": not kill_switch_on()}
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
        wait = limiter.acquire(client_key(x_api_key, request.client.host if request.client else None))
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
        )

    return app


def main() -> None:
    import uvicorn

    from kgrag.logs import configure_logging

    configure_logging()
    s = get_settings()
    assert_safe_binding(s)  # fail before binding the socket, not only at lifespan startup
    uvicorn.run(
        create_app(),
        host=s.api_host,
        port=s.api_port,
        log_config=None,  # keep the JSON handler from configure_logging
        access_log=False,  # the request middleware logs method/path/status/latency (no client IPs)
        timeout_graceful_shutdown=s.shutdown_grace_s,
        proxy_headers=False,  # rate-limit keys and loopback checks use the socket peer, not X-Forwarded-For
    )


if __name__ == "__main__":
    main()
