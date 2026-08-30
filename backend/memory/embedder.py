"""Text-to-vector embedding, behind a pluggable ``Embedder`` interface.

Owns: turning a chunk of text into a fixed-length vector of floats for
``backend.memory.context_retriever.HybridRetriever``'s ANN search half.
Two implementations:

- ``OpenAIEmbedder``: the real one. Calls OpenAI's embeddings API for
  ``text-embedding-3-large``, truncated to 256 dimensions via the API's own
  ``dimensions`` parameter -- the spec's pinned config. Wrapped in this
  project's retry/circuit-breaker/timeout composition (see module docstring
  of ``backend.tools.llm_client`` for why a new unprotected outbound call
  would violate this codebase's own production-readiness gate) and safe to
  call from an asyncio event loop (``embed_async`` offloads via
  ``asyncio.to_thread``, mirroring ``AnthropicLLMClient.complete_async`` --
  this project has fixed the "blocking call made directly from a coroutine
  stalls the shared event loop" defect class twice already, see that
  module's docstring point 3 and ``.genesis/checkpoints/CURRENT.md``'s M7
  history).

- ``DeterministicFixtureEmbedder``: no network, no key, fully reproducible.
  Every unit test, every integration test, and this milestone's demo
  command use this one -- there is no OpenAI credential available for this
  build (see this project's current credential situation), and this
  project's own policy (mirroring ``AnthropicLLMClient``'s injectable fake
  client) is that nothing outside a real live-demo path may depend on a
  network credential. See its class docstring for exactly how it makes
  semantically related text land near each other despite being a plain
  deterministic hash, not a trained model.

``get_embedder`` is the one factory call site that reads
``Settings.embedder_backend`` and picks between them -- everything else in
``backend/memory`` depends only on the ``Embedder`` protocol, never on
either concrete class, so a future third implementation (a different
provider, a cached-embeddings file) is a one-function change.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
from typing import Protocol

from openai import (
    APIConnectionError,
    APIStatusError,
    AuthenticationError,
    BadRequestError,
    NotFoundError,
    OpenAI,
    PermissionDeniedError,
)

from backend.core.settings import Settings, get_settings
from backend.reliability.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitOpenError,
    register,
)
from backend.reliability.retry import RetryExhaustedError, RetryPolicy, call_with_retry
from backend.reliability.timeout import TimeoutPolicy, run_with_timeout

_CIRCUIT_BREAKER_NAME = "openai_embedder"

# Mirrors backend.tools.llm_client's _NON_RETRYABLE_EXCEPTIONS: the OpenAI
# SDK's own "this request itself is malformed/unauthorized/forbidden/
# nonexistent, retrying can never help" exception family, extending
# call_with_retry's own default (TypeError/ValueError/KeyError/
# AttributeError) plus CircuitOpenError.
_NON_RETRYABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    TypeError,
    ValueError,
    KeyError,
    AttributeError,
    CircuitOpenError,
    AuthenticationError,
    BadRequestError,
    PermissionDeniedError,
    NotFoundError,
)


class EmbedderConfigurationError(Exception):
    """Raised when a real OpenAI embedding call is attempted with no usable API key.

    Only ever raised from the real (non-injected) client path -- every test
    in this project uses ``DeterministicFixtureEmbedder`` or injects a fake
    OpenAI client, neither of which reaches this check.
    """


class EmbeddingCallFailedError(Exception):
    """Wraps ``RetryExhaustedError``/``CircuitOpenError`` from the reliability layer.

    Mirrors ``backend.tools.llm_client.LLMCallFailedError``'s role: one
    exception type a caller can catch regardless of which underlying
    reliability failure actually happened.
    """


class EmbeddingDimensionError(Exception):
    """Raised when an embedder produced (or was asked to store) the wrong-length vector.

    Defense in depth, not the only guard: ``code_chunks.embedding``'s own
    ``VECTOR(256)`` column type (``migrations/scripts/dev-pgvector-
    init.sql``) rejects a mismatched-dimension INSERT at the database
    level regardless of whether application code validates first -- see
    ``tests/integration/test_hybrid_retrieval.py``'s dimension-mismatch
    test, which exercises the database's own enforcement directly rather
    than only this class's.
    """


class Embedder(Protocol):
    """The shape ``backend.memory.context_retriever.HybridRetriever`` depends on.

    A ``Protocol`` (structural typing), not an ABC -- so a test fake needs
    only to satisfy this shape, not inherit from either concrete class,
    the same reasoning ``backend.tools.llm_client.LLMClientProtocol``
    documents for its own equivalent role.
    """

    @property
    def dimension(self) -> int:
        """The length of every vector this embedder produces."""
        ...

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts, returning one vector of length ``dimension`` per text."""
        ...

    async def embed_async(self, texts: list[str]) -> list[list[float]]:
        """``embed``, safe to await from a coroutine running on a shared event loop."""
        ...


class OpenAIEmbedder:
    """Real embedder: OpenAI ``text-embedding-3-large``, truncated to ``dimension`` dims.

    Construction never requires ``OPENAI_API_KEY`` to be set -- the real
    SDK client is built lazily, only inside ``_client()``, mirroring
    ``AnthropicLLMClient``'s identical lazy-construction reasoning (a
    key-less instance can exist, e.g. as a default-constructed fallback,
    without failing merely by existing; only an actual call with no key
    and no injected client raises).
    """

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        openai_client: OpenAI | None = None,
    ) -> None:
        self._settings = settings if settings is not None else get_settings()
        self._injected_client = openai_client
        self._cached_real_client: OpenAI | None = None

        self._retry_policy = RetryPolicy(
            max_attempts=self._settings.embedding_retry_max_attempts,
            base_delay_seconds=self._settings.embedding_retry_base_delay_seconds,
            max_delay_seconds=self._settings.embedding_retry_max_delay_seconds,
        )
        self._timeout_policy = TimeoutPolicy(seconds=self._settings.embedding_timeout_seconds)
        self._breaker = register(
            CircuitBreaker(
                CircuitBreakerConfig(
                    failure_threshold=self._settings.embedding_circuit_breaker_failure_threshold,
                    reset_timeout_seconds=(
                        self._settings.embedding_circuit_breaker_reset_timeout_seconds
                    ),
                ),
                name=_CIRCUIT_BREAKER_NAME,
            )
        )

    @property
    def dimension(self) -> int:
        return self._settings.embedding_dimension

    def _client(self) -> OpenAI:
        if self._injected_client is not None:
            return self._injected_client
        if self._cached_real_client is None:
            if not self._settings.openai_api_key:
                raise EmbedderConfigurationError(
                    "OPENAI_API_KEY is not configured -- cannot make a real "
                    "OpenAI embeddings call. Set it in .env and set "
                    "EMBEDDER_BACKEND=openai for a real embedding run, or "
                    "leave EMBEDDER_BACKEND=fixture (the default) to use "
                    "DeterministicFixtureEmbedder instead."
                )
            # max_retries=0: this project's own retry/breaker/timeout
            # composition (below) is the single source of retry behavior,
            # exactly as AnthropicLLMClient._client() documents for the
            # identical reason -- leaving the SDK's own default retries on
            # would silently double-retry underneath this layer.
            self._cached_real_client = OpenAI(
                api_key=self._settings.openai_api_key, max_retries=0
            )
        return self._cached_real_client

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts via one real, reliability-wrapped OpenAI API call.

        Raises:
            ``EmbedderConfigurationError``: no API key configured and no
                fake client injected.
            ``EmbeddingCallFailedError``: every retry attempt failed, or
                the circuit breaker was already open.
        """
        if not texts:
            return []
        client = self._client()

        def _make_request() -> list[list[float]]:
            response = client.embeddings.create(
                model=self._settings.openai_embedding_model,
                input=texts,
                dimensions=self._settings.embedding_dimension,
            )
            return [item.embedding for item in response.data]

        def _bounded_request() -> list[list[float]]:
            return run_with_timeout(_make_request, policy=self._timeout_policy)

        try:
            return call_with_retry(
                lambda: self._breaker.call(_bounded_request),
                policy=self._retry_policy,
                non_retryable_exceptions=_NON_RETRYABLE_EXCEPTIONS,
            )
        except (RetryExhaustedError, CircuitOpenError) as exc:
            raise EmbeddingCallFailedError(
                f"Embedding call failed (retries exhausted or circuit breaker open): {exc}"
            ) from exc

    async def embed_async(self, texts: list[str]) -> list[list[float]]:
        """``embed``, off the calling coroutine's own thread.

        Offloads via ``asyncio.to_thread`` (asyncio's own default bounded
        executor), the same pattern ``AnthropicLLMClient.complete_async``
        and ``backend.observability.events.emit_decision_async`` already
        established in this codebase -- see this module's docstring for
        why that pattern exists at all.
        """
        return await asyncio.to_thread(self.embed, texts)


# ---------------------------------------------------------------------
# DeterministicFixtureEmbedder
#
# Design: a seeded, hashing-trick bag-of-tokens projection, NOT a trained
# model -- but engineered so that texts sharing vocabulary (including a
# small hand-picked set of near-synonyms, see _CANONICAL_TOKENS below) get
# genuinely closer vectors (higher cosine similarity) than texts that share
# nothing, which is the one property this milestone's retrieval tests
# actually need from it. Fully reproducible: every step is a function of
# the input text and fixed constants, with no dependency on Python's
# randomized-per-process string hashing (hashlib, not `hash()`).
#
# Algorithm, per text:
#   1. Tokenize: extract [a-zA-Z0-9_]+ runs, lowercased.
#   2. Canonicalize: map a token through _CANONICAL_TOKENS if present (a
#      small synonym table -- e.g. "login" and "signin" both canonicalize
#      to "authenticate"). This is the one deliberate piece of injected
#      "semantic" knowledge: a real OpenAI embedding would learn that
#      "login" and "authenticate" are related from its training data; this
#      fixture cannot learn anything, so it is told a handful of such
#      relationships explicitly, just enough to make the hybrid-retrieval
#      tests exercise a genuine "vector finds a semantically related term
#      FTS's stemmer does not connect" case rather than a vacuous one where
#      both rankers always agree because they both key off literal token
#      overlap.
#   3. Drop tokens shorter than 3 characters. A real subword/BPE-based
#      embedding vocabulary often under-represents very short, rare
#      identifiers and codes (they get split into low-information
#      sub-pieces); this fixture models that same class of limitation
#      directly, by design, which is what makes it possible to also
#      demonstrate the reverse case: full-text search finds an exact
#      short-token match this embedder cannot represent at all.
#   4. For each surviving (canonicalized) token, deterministically derive
#      a pseudo-random unit vector: seed a private ``random.Random`` from
#      the first 8 bytes of ``sha256(token)`` (never Python's own salted
#      `hash()`, which is randomized per process unless
#      ``PYTHONHASHSEED`` is fixed), draw `dimension` standard-normal
#      samples, and L2-normalize. This is the standard "feature hashing"
#      trick: independent tokens land in effectively random, near-
#      orthogonal directions in a 256-dim space, while the SAME token
#      (or its canonicalization target) always maps to the exact same
#      direction.
#   5. Sum every surviving token's vector, then L2-normalize the sum. Two
#      texts that share more (canonicalized) tokens end up with a higher
#      cosine similarity than two texts that share fewer -- exactly the
#      "semantically similar texts land near each other" property this
#      milestone's tests require, without needing a real trained model.
#   6. If no tokens survive steps 1-3 (e.g. the text is empty, or is a
#      single short/numeric token), fall back to one fixed sentinel token,
#      "__empty__", so ``embed`` never raises and always returns a valid
#      unit vector -- but one that shares no vocabulary with any real
#      content, so it does not spuriously resemble anything.
# ---------------------------------------------------------------------

_TOKEN_PATTERN = re.compile(r"[a-zA-Z0-9_]+")
_MIN_TOKEN_LENGTH = 3
_EMPTY_TEXT_SENTINEL = "__empty__"

# The synonym-canonicalization table described in step 2 above. Small and
# hand-picked, scoped to exactly what this milestone's own tests need to
# demonstrate a genuine "vector similarity captures a relationship keyword
# search does not" case -- not an attempt at a general thesaurus.
_CANONICAL_TOKENS: dict[str, str] = {
    "login": "authenticate",
    "signin": "authenticate",
    "authentication": "authenticate",
    "auth": "authenticate",
    "authenticated": "authenticate",
    "authenticates": "authenticate",
}


def _token_unit_vector(token: str, dimension: int) -> list[float]:
    """A fixed, deterministic pseudo-random unit vector for one token.

    Seeded from ``sha256(token)`` (not the built-in, per-process-salted
    ``hash()``) so the same token always produces the same vector, in this
    process or any other -- required for the fixture embedder's output to
    be reproducible across test runs and machines, per this milestone's
    "deterministic" requirement.
    """
    import random as _random  # local import: this module's own PRNG use is confined here

    digest = hashlib.sha256(token.encode("utf-8")).digest()
    seed = int.from_bytes(digest[:8], byteorder="big")
    rng = _random.Random(seed)
    raw = [rng.gauss(0.0, 1.0) for _ in range(dimension)]
    norm = math.sqrt(sum(component * component for component in raw))
    if norm == 0.0:
        # Astronomically unlikely for a 256-dim Gaussian draw, but handled
        # so this function is total rather than able to divide by zero.
        return raw
    return [component / norm for component in raw]


def _tokenize_and_canonicalize(text: str) -> list[str]:
    """Steps 1-3 of the algorithm above: tokenize, canonicalize, filter short tokens."""
    tokens = _TOKEN_PATTERN.findall(text.lower())
    canonical = [_CANONICAL_TOKENS.get(token, token) for token in tokens]
    return [token for token in canonical if len(token) >= _MIN_TOKEN_LENGTH]


class DeterministicFixtureEmbedder:
    """No-network, seeded embedder for tests and this milestone's demo command.

    See the module-level design comment above for the full algorithm. This
    class holds no state beyond the target ``dimension`` -- every vector is
    a pure function of its input text.
    """

    def __init__(self, *, dimension: int = 256) -> None:
        if dimension <= 0:
            raise ValueError(f"dimension must be positive, got {dimension}")
        self._dimension = dimension

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        tokens = _tokenize_and_canonicalize(text)
        if not tokens:
            tokens = [_EMPTY_TEXT_SENTINEL]
        accumulator = [0.0] * self._dimension
        for token in tokens:
            vector = _token_unit_vector(token, self._dimension)
            for i in range(self._dimension):
                accumulator[i] += vector[i]
        norm = math.sqrt(sum(component * component for component in accumulator))
        if norm == 0.0:
            return accumulator
        return [component / norm for component in accumulator]

    async def embed_async(self, texts: list[str]) -> list[list[float]]:
        """``embed``, awaitable for interface symmetry with ``OpenAIEmbedder``.

        No thread offload here -- unlike the real embedder, this class
        does no I/O and no meaningfully-blocking CPU work (a few hundred
        dot products at most for this milestone's corpus size), so there
        is nothing to protect a shared event loop from.
        """
        return self.embed(texts)


def get_embedder(settings: Settings | None = None) -> Embedder:
    """Construct the configured ``Embedder`` -- the one place ``Settings.embedder_backend`` is read.

    Everything else in ``backend.memory`` depends on the ``Embedder``
    protocol, never on ``OpenAIEmbedder``/``DeterministicFixtureEmbedder``
    directly, so adding a third backend later is a change confined to this
    function and ``Settings.embedder_backend``'s ``Literal``.
    """
    settings = settings if settings is not None else get_settings()
    if settings.embedder_backend == "openai":
        return OpenAIEmbedder(settings=settings)
    return DeterministicFixtureEmbedder(dimension=settings.embedding_dimension)


# Re-exported so a caller catching "any OpenAI transport-level failure"
# does not need to import the OpenAI SDK directly just to name the
# exceptions this module's retry policy already treats as transient
# (anything NOT in _NON_RETRYABLE_EXCEPTIONS, e.g. these, is retried).
__all__ = [
    "APIConnectionError",
    "APIStatusError",
    "Embedder",
    "EmbedderConfigurationError",
    "EmbeddingCallFailedError",
    "EmbeddingDimensionError",
    "DeterministicFixtureEmbedder",
    "OpenAIEmbedder",
    "get_embedder",
]
