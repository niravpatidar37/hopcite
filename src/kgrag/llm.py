"""Provider-agnostic LLM client: Groq (OpenAI-compatible), local Ollama, optional Anthropic.

Every paid call goes through the spend ledger (worst-case estimate checked before, actual usage
recorded after). Responses are cached on disk by a content hash of (provider, model, messages,
params) so reruns are free. 429/5xx are retried honoring Retry-After with exponential backoff and
jitter. Structured output is validated with Pydantic and retried on validation failure.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from kgrag.config import get_settings, load_env
from kgrag.spend import BudgetExceeded, SpendLedger, estimate_tokens, price_for

T = TypeVar("T", bound=BaseModel)

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434") + "/api/chat"
# 4096 matches the server default, so concurrent users of the shared Ollama server do not force
# model reloads; the answer prompt is budgeted to fit (see kgrag.answer limits).
OLLAMA_NUM_CTX = int(os.environ.get("OLLAMA_NUM_CTX", "4096"))
# Placeholder: verify a real model id via GET https://api.anthropic.com/v1/models before use.
CLAUDE_MODEL_ID = os.environ.get("CLAUDE_MODEL_ID", "CLAUDE_MODEL_ID_PLACEHOLDER")


class LLMError(RuntimeError):
    pass


class StructuredOutputError(LLMError):
    pass


@dataclass
class LLMResult:
    text: str
    input_tokens: int
    output_tokens: int
    usd: float
    cached: bool
    latency_ms: float
    model: str
    provider: str


@dataclass
class CallStats:
    """Per-request accumulator so the API/benchmark can report cost and latency per query."""

    usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0
    cached_calls: int = 0
    llm_ms: float = 0.0
    by_stage: dict[str, float] = field(default_factory=dict)

    def add(self, stage: str, r: LLMResult) -> None:
        self.usd += r.usd
        self.input_tokens += r.input_tokens
        self.output_tokens += r.output_tokens
        self.calls += 1
        self.cached_calls += int(r.cached)
        self.llm_ms += r.latency_ms
        self.by_stage[stage] = self.by_stage.get(stage, 0.0) + r.usd


def _cache_key(payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


class LLMClient:
    def __init__(
        self,
        provider: str = "groq",
        ledger: SpendLedger | None = None,
        cache_dir: Path | None = None,
        use_cache: bool = True,
        timeout_s: float = 90.0,
        max_retries: int = 6,
        max_wait_s: float = 120.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        load_env()
        self.provider = provider
        self.ledger = ledger or SpendLedger()
        self.cache_dir = (cache_dir or get_settings().cache_dir) / "llm"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.use_cache = use_cache
        self.max_retries = max_retries
        self.max_wait_s = max_wait_s  # latency budget for one backoff sleep (router uses a small one)
        self._http = httpx.Client(timeout=timeout_s, transport=transport)

    # ------------------------------------------------------------------ public API
    def complete(
        self,
        messages: list[dict[str, str]],
        model: str,
        max_tokens: int = 2048,
        temperature: float = 0.0,
        response_schema: dict[str, Any] | None = None,
        reasoning_effort: str | None = "low",
        cache_salt: str = "",
    ) -> LLMResult:
        payload: dict[str, Any] = {
            "provider": self.provider,
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "schema": response_schema,
            "reasoning_effort": reasoning_effort,
            "salt": cache_salt,
        }
        key = _cache_key(payload)
        cache_file = self.cache_dir / f"{key}.json"
        if self.use_cache and cache_file.is_file():
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            return LLMResult(
                text=data["text"],
                input_tokens=data["input_tokens"],
                output_tokens=data["output_tokens"],
                usd=0.0,
                cached=True,
                latency_ms=0.0,
                model=model,
                provider=self.provider,
            )

        price = price_for(self.provider, model)
        prompt_tokens = sum(estimate_tokens(m["content"]) + 8 for m in messages)
        worst_case = price.cost(prompt_tokens, max_tokens)
        if price.input_per_m or price.output_per_m:
            self.ledger.check(worst_case)  # raises BudgetExceeded

        t0 = time.perf_counter()
        if self.provider == "groq":
            text, in_tok, out_tok = self._groq(
                messages, model, max_tokens, temperature, response_schema, reasoning_effort
            )
        elif self.provider == "ollama":
            text, in_tok, out_tok = self._ollama(messages, model, max_tokens, temperature, response_schema)
        elif self.provider == "anthropic":
            text, in_tok, out_tok = self._anthropic(messages, model, max_tokens, temperature)
        else:
            raise LLMError(f"unknown provider {self.provider}")
        latency_ms = (time.perf_counter() - t0) * 1000
        usd = price.cost(in_tok, out_tok)
        if usd > 0:
            self.ledger.record(self.provider, model, in_tok, out_tok, usd)
        if self.use_cache:
            cache_file.write_text(
                json.dumps({"text": text, "input_tokens": in_tok, "output_tokens": out_tok, "usd": usd}),
                encoding="utf-8",
            )
        return LLMResult(text, in_tok, out_tok, usd, False, latency_ms, model, self.provider)

    def complete_json(
        self,
        messages: list[dict[str, str]],
        model: str,
        schema_cls: type[T],
        max_tokens: int = 2048,
        attempts: int = 3,
        reasoning_effort: str | None = "low",
        stats: CallStats | None = None,
        stage: str = "llm",
    ) -> T:
        """Call with a JSON schema, validate with Pydantic, retry (with the error) on failure."""
        schema = schema_cls.model_json_schema()
        msgs = list(messages)
        last_err = ""
        for attempt in range(attempts):
            res = self.complete(
                msgs,
                model,
                max_tokens=max_tokens,
                response_schema=schema,
                reasoning_effort=reasoning_effort,
                cache_salt=f"attempt{attempt}",
            )
            if stats is not None:
                stats.add(stage, res)
            try:
                return schema_cls.model_validate_json(_strip_fences(res.text))
            except ValidationError as exc:
                last_err = str(exc)[:800]
                msgs = [
                    *messages,
                    {"role": "assistant", "content": res.text[:4000]},
                    {
                        "role": "user",
                        "content": "That output failed schema validation:\n"
                        + last_err
                        + "\nReturn ONLY corrected JSON that matches the schema.",
                    },
                ]
        raise StructuredOutputError(f"schema validation failed after {attempts} attempts: {last_err}")

    # ------------------------------------------------------------------ providers
    def _post_with_retry(self, url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        delay = 2.0
        for attempt in range(self.max_retries + 1):
            try:
                r = self._http.post(url, headers=headers, json=body)
            except httpx.TransportError as exc:
                if attempt == self.max_retries:
                    raise LLMError(f"transport error: {type(exc).__name__}") from exc
                time.sleep(delay + random.uniform(0, 1))  # noqa: S311  # nosec B311 - jitter, not crypto
                delay = min(delay * 2, 60)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                if attempt == self.max_retries:
                    raise LLMError(f"HTTP {r.status_code} after {attempt} retries")
                retry_after = _parse_retry_after(r.headers.get("retry-after"))
                if retry_after is not None and retry_after > self.max_wait_s:
                    raise LLMError(f"HTTP {r.status_code}: retry-after {retry_after:.0f}s exceeds latency budget")
                wait = max(retry_after or 0.0, delay) + random.uniform(0, 1)  # noqa: S311  # nosec B311
                time.sleep(min(wait, self.max_wait_s))
                delay = min(delay * 2, 60)
                continue
            if r.status_code >= 400:
                # Do not echo request bodies (they may contain user text); keep the provider message.
                raise LLMError(f"HTTP {r.status_code}: {r.text[:300]}")
            result: dict[str, Any] = r.json()
            return result
        raise LLMError("unreachable")

    def _groq(
        self,
        messages: list[dict[str, str]],
        model: str,
        max_tokens: int,
        temperature: float,
        schema: dict[str, Any] | None,
        reasoning_effort: str | None,
    ) -> tuple[str, int, int]:
        key = os.environ.get("GROQ_API_KEY", "")
        if not key:
            raise LLMError("GROQ_API_KEY not set")
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_completion_tokens": max_tokens,
            "temperature": temperature,
        }
        if reasoning_effort and model.startswith("openai/gpt-oss"):
            body["reasoning_effort"] = reasoning_effort
            body["include_reasoning"] = False
        if schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": schema.get("title", "output"), "schema": schema, "strict": False},
            }
        data = self._post_with_retry(GROQ_URL, {"Authorization": f"Bearer {key}"}, body)
        text = data["choices"][0]["message"].get("content") or ""
        usage = data.get("usage", {})
        return text, int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))

    def _ollama(
        self,
        messages: list[dict[str, str]],
        model: str,
        max_tokens: int,
        temperature: float,
        schema: dict[str, Any] | None,
    ) -> tuple[str, int, int]:
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            # Ollama's default context (2-4K) would silently truncate RAG prompts.
            "options": {"temperature": temperature, "num_predict": max_tokens, "num_ctx": OLLAMA_NUM_CTX},
        }
        if schema is not None:
            body["format"] = schema
        data = self._post_with_retry(OLLAMA_URL, {}, body)
        return (
            data.get("message", {}).get("content", ""),
            int(data.get("prompt_eval_count", 0)),
            int(data.get("eval_count", 0)),
        )

    def _anthropic(
        self, messages: list[dict[str, str]], model: str, max_tokens: int, temperature: float
    ) -> tuple[str, int, int]:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise LLMError("ANTHROPIC_API_KEY not set; Claude adapter disabled")
        if model == "CLAUDE_MODEL_ID_PLACEHOLDER":
            raise BudgetExceeded("Claude model id is a placeholder; verify via GET /v1/models first")
        import anthropic  # optional dependency

        client: Any = anthropic.Anthropic()
        system = "\n".join(m["content"] for m in messages if m["role"] == "system")
        rest = [m for m in messages if m["role"] != "system"]
        resp = client.messages.create(
            model=model,
            system=system,
            messages=rest,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        text = "".join(getattr(b, "text", "") for b in resp.content)
        return text, resp.usage.input_tokens, resp.usage.output_tokens


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        if t.endswith("```"):
            t = t[:-3]
    return t.strip()
