"""FastAPI service: POST /ask -> answer + validated citations + route.

Auth: if KGRAG_API_KEY is set, every /ask call needs header `X-API-Key` (constant-time compare).
If it is not set, only loopback clients are served (fail closed for non-local use).
"""

from __future__ import annotations

import hmac
import ipaddress
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field, field_validator

from kgrag.config import get_settings

MAX_QUESTION_CHARS = 500


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


def _is_loopback(host: str | None) -> bool:
    if not host:
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def require_access(request: Request, x_api_key: Annotated[str | None, Header()] = None) -> None:
    key = get_settings().api_key
    if key:
        if not x_api_key or not hmac.compare_digest(x_api_key.encode(), key.encode()):
            raise HTTPException(status_code=401, detail="invalid or missing API key")
        return
    client = request.client.host if request.client else None
    if not _is_loopback(client):
        raise HTTPException(status_code=403, detail="set KGRAG_API_KEY to serve non-local clients")


def create_app(services: Any | None = None) -> FastAPI:
    state: dict[str, Any] = {"services": services}

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
        if state["services"] is None:
            from kgrag.service import build_services

            state["services"] = await run_in_threadpool(build_services)
        yield

    app = FastAPI(title="kg-rag-enterprise", version="0.1.0", lifespan=lifespan)

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

    @app.post("/ask", response_model=AskResponse, dependencies=[Depends(require_access)])
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

    s = get_settings()
    uvicorn.run(create_app(), host=s.api_host, port=s.api_port, log_level="info")


if __name__ == "__main__":
    main()
