"""OpenRouter chat completions client.

Features:

* OpenAI compatible ``/chat/completions`` with the OpenRouter ``models`` fallback list, so a
  provider outage routes to the next model without application code changes.
* Structured output: ``json_schema`` (strict) by default, falling back automatically to
  ``json_object`` and then to prompt only JSON when a routed model rejects the parameter.
* Provider preferences: ``data_collection: deny`` by default and ``require_parameters`` when
  structured output is requested, so requests only reach providers that honour it.
* Retries with exponential backoff and jitter for 408, 429 and 5xx responses and transport
  errors, honouring ``Retry-After``.
* Robust JSON extraction (code fences, leading prose) and token usage capture.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential_jitter

from v2e_rag.observability.logging import get_logger
from v2e_rag.observability.metrics import LLM_REQUESTS, LLM_TOKENS

log = get_logger(__name__)


class LLMError(RuntimeError):
    pass


class LLMOutputError(LLMError):
    """The model answered but the content was not valid JSON for the requested schema."""


@dataclass
class LLMResult:
    content: str
    data: dict[str, Any] | None
    model: str
    usage: dict[str, int] = field(default_factory=dict)
    latency_ms: int = 0
    structured_mode: str = "prompt"


class ChatModel(Protocol):
    async def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        purpose: str,
        schema_name: str,
        schema: dict[str, Any] | None,
        model: str | None = None,
        fallbacks: list[str] | None = None,
        temperature: float = 0.2,
        max_tokens: int = 2000,
    ) -> LLMResult: ...

    async def aclose(self) -> None: ...


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def extract_json(content: str) -> dict[str, Any]:
    """Parse a JSON object from model output, tolerating code fences and stray prose."""
    text = _FENCE_RE.sub("", content.strip()).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise LLMOutputError("No JSON object found in model output") from None
        try:
            value = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMOutputError(f"Invalid JSON in model output: {exc}") from exc
    if not isinstance(value, dict):
        raise LLMOutputError("Model output JSON is not an object")
    return value


def to_strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Adapt a Pydantic JSON schema to the strict subset used by structured outputs:
    every object closes ``additionalProperties`` and lists all properties as required."""

    # Validation keywords are enforced by Pydantic after parsing; several providers reject
    # them inside strict schemas, so they are removed from the transmitted schema.
    dropped = {
        "default",
        "title",
        "examples",
        "description",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "pattern",
        "format",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "uniqueItems",
    }

    def visit(node: Any) -> Any:
        if isinstance(node, dict):
            node = {
                k: (
                    {name: visit(sub) for name, sub in v.items()}
                    if k in ("properties", "$defs")
                    else visit(v)
                )
                for k, v in node.items()
                if k not in dropped
            }
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"].keys())
            return node
        if isinstance(node, list):
            return [visit(v) for v in node]
        return node

    return visit(schema)


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError | httpx.TimeoutException):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (408, 409, 425, 429, 500, 502, 503, 504)
    return False


def _is_unsupported_parameter(exc: httpx.HTTPStatusError) -> bool:
    if exc.response.status_code not in (400, 404, 422):
        return False
    text = exc.response.text.lower()
    return any(
        k in text
        for k in (
            "response_format",
            "json_schema",
            "structured",
            "require_parameters",
            "no endpoints",
        )
    )


class OpenRouterClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://openrouter.ai/api/v1",
        default_model: str = "anthropic/claude-sonnet-4.5",
        default_fallbacks: list[str] | None = None,
        structured_output_mode: str = "json_schema",
        timeout: float = 90.0,
        max_retries: int = 3,
        app_url: str = "",
        app_title: str = "",
        data_collection: str = "deny",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.default_model = default_model
        self.default_fallbacks = default_fallbacks or []
        self.structured_output_mode = structured_output_mode
        self.max_retries = max_retries
        self.data_collection = data_collection
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        if app_url:
            headers["HTTP-Referer"] = app_url
        if app_title:
            headers["X-Title"] = app_title
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"), headers=headers, timeout=timeout
        )
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _payload(
        self,
        messages: list[dict[str, str]],
        model: str,
        fallbacks: list[str],
        temperature: float,
        max_tokens: int,
        mode: str,
        schema_name: str,
        schema: dict[str, Any] | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "provider": {"data_collection": self.data_collection},
        }
        if fallbacks:
            payload["models"] = [model, *[m for m in fallbacks if m != model]]
        if mode == "json_schema" and schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "strict": True,
                    "schema": to_strict_schema(schema),
                },
            }
            payload["provider"]["require_parameters"] = True
        elif mode == "json_object":
            payload["response_format"] = {"type": "json_object"}
        return payload

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        response: httpx.Response | None = None
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self.max_retries + 1),
            wait=wait_exponential_jitter(initial=1, max=30),
            retry=retry_if_exception(_retryable),
            reraise=True,
        ):
            with attempt:
                response = await self._client.post("/chat/completions", json=payload)
                if response.status_code == 429 and "retry-after" in response.headers:
                    log.warning(
                        "openrouter_rate_limited", retry_after=response.headers["retry-after"]
                    )
                response.raise_for_status()
        assert response is not None
        body = response.json()
        if isinstance(body, dict) and body.get("error"):
            error = body["error"]
            raise LLMError(
                f"OpenRouter error {error.get('code', '')}: {error.get('message', error)}"
            )
        return body

    async def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        purpose: str,
        schema_name: str,
        schema: dict[str, Any] | None,
        model: str | None = None,
        fallbacks: list[str] | None = None,
        temperature: float = 0.2,
        max_tokens: int = 2000,
    ) -> LLMResult:
        chosen = model or self.default_model
        fallback_models = self.default_fallbacks if fallbacks is None else fallbacks
        modes = {
            "json_schema": ["json_schema", "json_object", "prompt"],
            "json_object": ["json_object", "prompt"],
        }.get(self.structured_output_mode, ["prompt"])
        last_error: Exception | None = None
        for mode in modes:
            payload = self._payload(
                messages,
                chosen,
                fallback_models,
                temperature,
                max_tokens,
                mode,
                schema_name,
                schema,
            )
            started = time.perf_counter()
            try:
                body = await self._post(payload)
            except httpx.HTTPStatusError as exc:
                if _is_unsupported_parameter(exc):
                    log.warning("structured_output_unsupported", mode=mode, model=chosen)
                    last_error = exc
                    continue
                LLM_REQUESTS.labels(purpose=purpose, model=chosen, outcome="http_error").inc()
                raise LLMError(
                    f"OpenRouter HTTP {exc.response.status_code}: {exc.response.text[:500]}"
                ) from exc
            except httpx.HTTPError as exc:
                LLM_REQUESTS.labels(purpose=purpose, model=chosen, outcome="transport_error").inc()
                raise LLMError(f"OpenRouter transport error: {exc}") from exc

            latency = int((time.perf_counter() - started) * 1000)
            choices = body.get("choices") or []
            if not choices:
                raise LLMError("OpenRouter returned no choices")
            message = choices[0].get("message") or {}
            content = message.get("content") or ""
            if isinstance(content, list):
                content = "".join(
                    part.get("text", "") for part in content if isinstance(part, dict)
                )
            usage = {
                k: int(v)
                for k, v in (body.get("usage") or {}).items()
                if isinstance(v, int | float)
            }
            used_model = body.get("model", chosen)
            LLM_REQUESTS.labels(purpose=purpose, model=used_model, outcome="ok").inc()
            LLM_TOKENS.labels(purpose=purpose, kind="prompt").inc(usage.get("prompt_tokens", 0))
            LLM_TOKENS.labels(purpose=purpose, kind="completion").inc(
                usage.get("completion_tokens", 0)
            )
            data = extract_json(content)
            return LLMResult(
                content=content,
                data=data,
                model=used_model,
                usage=usage,
                latency_ms=latency,
                structured_mode=mode,
            )
        raise LLMError(f"No structured output mode was accepted: {last_error}")
