"""Unit tests for backend.memory.embedder.

No test in this file requires OPENAI_API_KEY -- every OpenAIEmbedder
constructed here is given an injected fake ``openai_client`` (a bare object
exposing ``.embeddings.create(...)``), so the real OpenAI SDK client
(``OpenAIEmbedder._client``'s lazy real-client path) is never reached at
all. Mirrors ``tests/unit/test_llm_client.py``'s identical policy for
``AnthropicLLMClient``.

Covers, one class per concern:
- ``TestReliabilityComposition``: the M6 retry/circuit-breaker layer,
  reused around this outbound call, proven by fault injection -- a
  transient failure is retried to success, and a persistently failing
  client trips the breaker and then fails fast without invoking the client
  again. This is the "grep-verifiable live call site" gate's actual
  behavioral proof, not just an import.
- ``TestConfigurationError``: no key configured and no fake client
  injected raises a clear ``EmbedderConfigurationError`` at call time, not
  at construction.
- ``TestEmbedAsyncDoesNotBlockTheEventLoop``: ``embed_async`` actually
  offloads the blocking call via ``asyncio.to_thread`` rather than merely
  being declared ``async def`` and blocking anyway.
- ``TestDeterministicFixtureEmbedder``: the no-network fixture embedder's
  own documented properties -- determinism/reproducibility across
  instances, correct output dimension, the synonym-canonicalization
  property (semantically related text lands closer than unrelated text),
  and the short-token-filtering property (a lone short token falls back to
  the empty-text sentinel). The retrieval-level consequences of these
  properties (a genuine "vector wins"/"keyword wins" case) are proven
  end-to-end against a real database in
  ``tests/integration/test_hybrid_retrieval.py``; this file proves the
  embedder-level properties those tests depend on, in isolation.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import pytest

from backend.core.settings import Settings
from backend.memory.embedder import (
    DeterministicFixtureEmbedder,
    EmbedderConfigurationError,
    EmbeddingCallFailedError,
)


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "github_webhook_secret": "unused-in-this-test",
        "embedding_timeout_seconds": 5.0,
        "embedding_retry_max_attempts": 3,
        "embedding_retry_base_delay_seconds": 0.01,
        "embedding_retry_max_delay_seconds": 0.02,
        "embedding_circuit_breaker_failure_threshold": 2,
        "embedding_circuit_breaker_reset_timeout_seconds": 0.2,
        "embedding_dimension": 8,
    }
    values.update(overrides)
    return Settings(**values)


@dataclass
class _FakeEmbeddingItem:
    embedding: list[float]


@dataclass
class _FakeEmbeddingResponse:
    data: list[_FakeEmbeddingItem]


class _FakeEmbeddingsEndpoint:
    """Stands in for ``openai.OpenAI().embeddings`` -- just the one method used."""

    def __init__(self, responder: Any) -> None:
        self._responder = responder
        self.call_count = 0

    def create(self, **kwargs: Any) -> _FakeEmbeddingResponse:
        self.call_count += 1
        return self._responder(**kwargs)


class _FakeOpenAIClient:
    """Stands in for ``openai.OpenAI`` -- injected via ``OpenAIEmbedder(openai_client=...)``."""

    def __init__(self, responder: Any) -> None:
        self.embeddings = _FakeEmbeddingsEndpoint(responder)

    @property
    def call_count(self) -> int:
        return self.embeddings.call_count


def _fake_response(texts: list[str], *, dimension: int = 8) -> _FakeEmbeddingResponse:
    """One deterministic, arbitrary vector per input text -- content doesn't matter for these tests."""
    return _FakeEmbeddingResponse(
        data=[_FakeEmbeddingItem(embedding=[float(i)] * dimension) for i, _ in enumerate(texts)]
    )


class TestReliabilityComposition:
    """M6's retry/circuit-breaker layer, reused around this outbound call, proven by fault injection."""

    def test_a_transient_failure_is_retried_to_success(self) -> None:
        from backend.memory.embedder import OpenAIEmbedder

        attempts = {"count": 0}

        def flaky(**kwargs: Any) -> _FakeEmbeddingResponse:
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise RuntimeError("transient upstream failure")
            return _fake_response(kwargs["input"])

        fake_client = _FakeOpenAIClient(responder=flaky)
        embedder = OpenAIEmbedder(
            settings=_settings(
                embedding_retry_max_attempts=3, embedding_circuit_breaker_failure_threshold=5
            ),
            openai_client=fake_client,
        )

        result = embedder.embed(["some code"])
        assert result == [[0.0] * 8]
        assert attempts["count"] == 3  # 2 failures + 1 success, all counted

    def test_persistent_failure_trips_the_breaker_then_fails_fast(self) -> None:
        """A 100%-failing client trips the breaker within N attempts; then it fails fast (no further calls)."""
        from backend.memory.embedder import OpenAIEmbedder

        def always_fails(**_: Any) -> _FakeEmbeddingResponse:
            raise RuntimeError("upstream is down")

        fake_client = _FakeOpenAIClient(responder=always_fails)
        embedder = OpenAIEmbedder(
            # 1 attempt per embed() call, so each embed() maps to exactly
            # one breaker.call() -- makes the "N attempts to trip"
            # arithmetic exact and easy to assert on.
            settings=_settings(
                embedding_retry_max_attempts=1, embedding_circuit_breaker_failure_threshold=2
            ),
            openai_client=fake_client,
        )

        for _ in range(2):
            with pytest.raises(EmbeddingCallFailedError):
                embedder.embed(["x"])
        assert fake_client.call_count == 2

        # 3rd call: the breaker is OPEN -- fails fast, the fake client is
        # NOT invoked a 3rd time. This is the actual proof the breaker (not
        # just the retry loop) is wired in, not merely imported.
        with pytest.raises(EmbeddingCallFailedError):
            embedder.embed(["x"])
        assert fake_client.call_count == 2, "breaker should fail fast without calling the client again"


class TestConfigurationError:
    """No key configured and no fake client injected raises a clear error, only when actually called."""

    def test_no_key_raises_only_on_a_real_call_attempt(self) -> None:
        from backend.memory.embedder import OpenAIEmbedder

        embedder = OpenAIEmbedder(settings=_settings(openai_api_key=None))
        # Construction alone must not raise -- only an actual attempt to
        # reach the real, non-injected client does.
        with pytest.raises(EmbedderConfigurationError, match="OPENAI_API_KEY"):
            embedder.embed(["some code"])


class TestEmbedAsyncDoesNotBlockTheEventLoop:
    """embed_async must offload, not just declare itself async and block anyway."""

    async def test_a_slow_call_does_not_stall_a_concurrent_coroutine(self) -> None:
        from backend.memory.embedder import OpenAIEmbedder

        slow_seconds = 0.3

        def slow_responder(**kwargs: Any) -> _FakeEmbeddingResponse:
            time.sleep(slow_seconds)  # a genuinely blocking, synchronous call
            return _fake_response(kwargs["input"])

        fake_client = _FakeOpenAIClient(responder=slow_responder)
        embedder = OpenAIEmbedder(settings=_settings(), openai_client=fake_client)

        heartbeat_ticks = 0

        async def heartbeat() -> None:
            nonlocal heartbeat_ticks
            deadline = asyncio.get_event_loop().time() + slow_seconds
            while asyncio.get_event_loop().time() < deadline:
                heartbeat_ticks += 1
                await asyncio.sleep(0.01)

        result, _ = await asyncio.gather(embedder.embed_async(["x"]), heartbeat())

        assert result == [[0.0] * 8]
        # If embed_async blocked the event loop (e.g. by calling self.embed
        # directly instead of via asyncio.to_thread), the heartbeat
        # coroutine would never get scheduled while the slow call ran, and
        # this would be 0 or close to it instead of the ~30 ticks 0.3s /
        # 0.01s implies.
        assert heartbeat_ticks > 5, (
            f"only {heartbeat_ticks} heartbeat ticks during the slow call -- "
            "embed_async appears to be blocking the event loop"
        )


class TestDeterministicFixtureEmbedder:
    """The no-network fixture embedder's own documented properties, in isolation."""

    def test_output_has_the_configured_dimension(self) -> None:
        embedder = DeterministicFixtureEmbedder(dimension=256)
        [vector] = embedder.embed(["def foo(): pass"])
        assert len(vector) == 256

    def test_same_text_produces_the_same_vector_across_instances(self) -> None:
        """Reproducible across processes: seeded from sha256, never Python's salted hash()."""
        first = DeterministicFixtureEmbedder(dimension=32).embed(["def handle_login(): pass"])
        second = DeterministicFixtureEmbedder(dimension=32).embed(["def handle_login(): pass"])
        assert first == second

    def test_output_vectors_are_unit_normalized(self) -> None:
        [vector] = DeterministicFixtureEmbedder(dimension=64).embed(["some realistic content here"])
        norm_squared = sum(component * component for component in vector)
        assert norm_squared == pytest.approx(1.0, abs=1e-9)

    def test_empty_text_falls_back_to_a_fixed_sentinel_not_an_error(self) -> None:
        embedder = DeterministicFixtureEmbedder(dimension=16)
        first = embedder.embed([""])
        second = embedder.embed(["   "])  # whitespace-only tokenizes to nothing too
        assert first == second  # both hit the same "__empty__" sentinel fallback

    def test_short_token_alone_also_falls_back_to_the_empty_sentinel(self) -> None:
        """A single token under 3 chars is filtered out entirely -- see module docstring step 3."""
        embedder = DeterministicFixtureEmbedder(dimension=16)
        [short_token_vector] = embedder.embed(["s3"])
        [empty_vector] = embedder.embed([""])
        assert short_token_vector == empty_vector

    def test_synonym_canonicalization_makes_related_text_closer_than_unrelated_text(self) -> None:
        """'login' and 'authenticate' are canonicalized to the same token -- see _CANONICAL_TOKENS."""
        embedder = DeterministicFixtureEmbedder(dimension=256)
        [query_vec] = embedder.embed(["authenticate user"])
        [related_vec] = embedder.embed(["def login(request): return get_user(request)"])
        [unrelated_vec] = embedder.embed(["def compute_average(values): return sum(values)"])

        def cosine(a: list[float], b: list[float]) -> float:
            return sum(x * y for x, y in zip(a, b, strict=True))

        related_similarity = cosine(query_vec, related_vec)
        unrelated_similarity = cosine(query_vec, unrelated_vec)
        assert related_similarity > unrelated_similarity

    def test_shared_vocabulary_increases_similarity_monotonically(self) -> None:
        """More shared (canonicalized) tokens -> strictly higher cosine similarity, not just nonzero."""
        embedder = DeterministicFixtureEmbedder(dimension=256)
        [query_vec] = embedder.embed(["process order payment"])
        [one_shared] = embedder.embed(["def process(): return None"])
        [two_shared] = embedder.embed(["def process(order): return None"])

        def cosine(a: list[float], b: list[float]) -> float:
            return sum(x * y for x, y in zip(a, b, strict=True))

        assert cosine(query_vec, two_shared) > cosine(query_vec, one_shared)

    def test_nonpositive_dimension_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="dimension must be positive"):
            DeterministicFixtureEmbedder(dimension=0)

    async def test_embed_async_matches_embed(self) -> None:
        embedder = DeterministicFixtureEmbedder(dimension=16)
        assert await embedder.embed_async(["hello world"]) == embedder.embed(["hello world"])
