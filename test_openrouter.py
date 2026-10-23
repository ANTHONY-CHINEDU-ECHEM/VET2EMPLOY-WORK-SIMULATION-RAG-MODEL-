"""OpenRouter chat and embedding clients against a mocked HTTP transport."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from v2e_rag.embeddings.base import (
    CachedEmbedder,
    EmbeddingError,
    HashingEmbedder,
    OpenRouterEmbedder,
)
from v2e_rag.llm.openrouter import (
    LLMError,
    LLMOutputError,
    OpenRouterClient,
    extract_json,
    to_strict_schema,
)
from v2e_rag.llm.schemas import GeneratedTask
from v2e_rag.store.memory_store import MemoryStore

BASE = "https://openrouter.test/api/v1"


def _completion(content: str, model: str = "anthropic/claude-sonnet-4.5") -> dict:
    return {
        "id": "gen-1",
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19},
    }


def _client(**kwargs) -> OpenRouterClient:
    return OpenRouterClient(
        api_key="sk-test",
        base_url=BASE,
        default_fallbacks=["openai/gpt-4o"],
        max_retries=2,
        **kwargs,
    )


@respx.mock
async def test_structured_request_payload_and_parsing():
    route = respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(200, json=_completion('```json\n{"ok": true}\n```'))
    )
    client = _client(app_title="Vet2Employ RAG", app_url="https://example.org")
    result = await client.complete_json(
        [{"role": "user", "content": "hi"}],
        purpose="task",
        schema_name="s",
        schema={"type": "object", "properties": {"ok": {"type": "boolean"}}},
    )
    assert result.data == {"ok": True} and result.structured_mode == "json_schema"
    assert result.usage["prompt_tokens"] == 12
    request = route.calls.last.request
    body = json.loads(request.content)
    assert body["models"] == ["anthropic/claude-sonnet-4.5", "openai/gpt-4o"]
    assert body["provider"] == {"data_collection": "deny", "require_parameters": True}
    assert body["response_format"]["json_schema"]["strict"] is True
    assert request.headers["Authorization"] == "Bearer sk-test"
    assert request.headers["X-Title"] == "Vet2Employ RAG"
    await client.aclose()


@respx.mock
async def test_falls_back_when_structured_output_is_unsupported():
    route = respx.post(f"{BASE}/chat/completions").mock(
        side_effect=[
            httpx.Response(
                400, json={"error": {"message": "response_format json_schema is not supported"}}
            ),
            httpx.Response(200, json=_completion('{"ok": 1}')),
        ]
    )
    result = await _client().complete_json(
        [{"role": "user", "content": "x"}], purpose="t", schema_name="s", schema={}
    )
    assert result.structured_mode == "json_object"
    assert json.loads(route.calls[1].request.content)["response_format"] == {"type": "json_object"}


@respx.mock
async def test_retries_rate_limits_then_succeeds():
    respx.post(f"{BASE}/chat/completions").mock(
        side_effect=[
            httpx.Response(429, headers={"retry-after": "0"}),
            httpx.Response(200, json=_completion('{"a": 1}')),
        ]
    )
    result = await _client(structured_output_mode="prompt").complete_json(
        [{"role": "user", "content": "x"}], purpose="t", schema_name="s", schema=None
    )
    assert result.data == {"a": 1}


@respx.mock
async def test_error_payload_and_bad_json_raise():
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(200, json={"error": {"code": 402, "message": "credits"}})
    )
    with pytest.raises(LLMError, match="credits"):
        await _client(structured_output_mode="prompt").complete_json(
            [], purpose="t", schema_name="s", schema=None
        )
    with pytest.raises(LLMOutputError):
        extract_json("no json here")
    assert extract_json('Here you go: {"x": [1, 2]} thanks') == {"x": [1, 2]}


def test_strict_schema_closes_objects_and_keeps_property_names():
    strict = to_strict_schema(GeneratedTask.model_json_schema())
    deliverable = strict["$defs"]["TaskDeliverable"]
    assert deliverable["additionalProperties"] is False
    assert "format" in deliverable["properties"]  # a property named like a keyword survives
    assert set(deliverable["required"]) == set(deliverable["properties"])
    assert "maxLength" not in json.dumps(strict)


@respx.mock
async def test_embedder_batches_orders_and_validates_dimensions():
    def reply(request):
        inputs = json.loads(request.content)["input"]
        data = [{"index": i, "embedding": [float(i + 1), 0.0, 0.0]} for i in range(len(inputs))]
        return httpx.Response(200, json={"data": list(reversed(data)), "model": "m"})

    route = respx.post(f"{BASE}/embeddings").mock(side_effect=reply)
    embedder = OpenRouterEmbedder(
        "sk", "openai/text-embedding-3-small", 3, base_url=BASE, batch_size=2
    )
    vectors = await embedder.embed_documents(["a", "b", "c"])
    assert len(route.calls) == 2
    assert vectors[0] == [1.0, 0.0, 0.0] and vectors[1] == [
        1.0,
        0.0,
        0.0,
    ]  # L2 normalised, index ordered
    assert json.loads(route.calls[0].request.content)["provider"] == {"data_collection": "deny"}

    bad = OpenRouterEmbedder("sk", "m", 5, base_url=BASE)
    with pytest.raises(EmbeddingError, match="dimensions"):
        await bad.embed_documents(["a"])


async def test_cached_embedder_reuses_vectors():
    class Counting(HashingEmbedder):
        calls = 0

        async def embed_documents(self, texts):
            Counting.calls += len(texts)
            return await super().embed_documents(texts)

    store = MemoryStore()
    cached = CachedEmbedder(Counting(64), store)
    first = await cached.embed_documents(["alpha", "beta", "alpha"])
    second = await cached.embed_documents(["alpha", "beta"])
    assert Counting.calls == 2 and first[0] == second[0]


async def test_hashing_embedder_is_deterministic():
    a = await HashingEmbedder(128).embed_query("data quality log")
    b = await HashingEmbedder(128).embed_query("data quality log")
    assert a == b and abs(sum(x * x for x in a) - 1.0) < 1e-5
