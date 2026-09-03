"""Integration tests for ``backend.memory.langchain_retriever.HybridRetrieverAdapter``.

Owns: proving the LangChain ``BaseRetriever`` adapter is a genuinely faithful
pass-through over ``HybridRetriever`` -- not just that it satisfies
``BaseRetriever``'s type signature in isolation, per this project's own M5
lesson ("test the composition, not just the units"). The single most
important test in this file is ``TestFaithfulPassThrough``: it proves the
adapter returns the SAME chunks in the SAME order as calling
``HybridRetriever.hybrid_search`` directly, for the same query and top_k --
i.e. that wrapping it in a ``BaseRetriever`` has not perturbed the RRF
ranking M9's own held-out-query-set validation (see
``backend.memory.langchain_retriever``'s module docstring) depends on
staying exactly as configured.

Uses the same real, reachable pgvector Postgres
(``docker-compose.yml``'s ``pgvector`` service) and the same
module-level ``skipif``-when-unreachable pattern as
``tests/integration/test_hybrid_retrieval.py`` -- this file's ``retriever``
fixture is intentionally the same shape (truncate ``code_chunks`` first, so
every test's corpus is small and fully controlled). Every test here uses
``DeterministicFixtureEmbedder`` -- no network, no API key, no billable
call, and no ``live`` marker needed.
"""

from __future__ import annotations

import asyncio
import time

import psycopg
import pytest
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever

from backend.core.settings import get_settings
from backend.memory.context_retriever import HybridRetriever
from backend.memory.embedder import DeterministicFixtureEmbedder
from backend.memory.langchain_retriever import HybridRetrieverAdapter
from backend.memory.tiger_client import apply_migrations, connect

_BASE_SETTINGS = get_settings()
_PGVECTOR_URL = _BASE_SETTINGS.pgvector_url


def _pgvector_reachable(dsn: str) -> bool:
    try:
        with psycopg.connect(dsn, connect_timeout=1) as conn:
            conn.execute("SELECT 1")
        return True
    except (psycopg.Error, OSError):
        return False


pytestmark = pytest.mark.skipif(
    not _pgvector_reachable(_PGVECTOR_URL),
    reason=(
        f"pgvector Postgres not reachable at {_PGVECTOR_URL} -- "
        "run `docker compose up -d pgvector` first"
    ),
)


@pytest.fixture(scope="module", autouse=True)
def _migrated_database() -> None:
    apply_migrations(_PGVECTOR_URL)


@pytest.fixture
def embedder() -> DeterministicFixtureEmbedder:
    return DeterministicFixtureEmbedder(dimension=_BASE_SETTINGS.embedding_dimension)


@pytest.fixture
def retriever(embedder: DeterministicFixtureEmbedder) -> HybridRetriever:
    """A real ``HybridRetriever`` against pgvector, with ``code_chunks`` truncated first.

    Mirrors ``tests/integration/test_hybrid_retrieval.py``'s own ``retriever``
    fixture exactly, for the same reason: several tests below depend on
    exact rank/order among a small, fully-controlled seeded set.
    """
    with connect(_PGVECTOR_URL) as conn:
        conn.execute("TRUNCATE code_chunks")
    return HybridRetriever(_PGVECTOR_URL, embedder, settings=_BASE_SETTINGS)


def _seed_mixed_corpus(retriever: HybridRetriever) -> None:
    """A small, varied corpus exercising both vector and full-text ranking, like M9's own tests."""
    retriever.insert_chunk(
        "backend/auth/login.py",
        "def login(request): return authenticate(request.credentials)",
    )
    retriever.insert_chunk(
        "backend/payments/refund.py",
        "def process_refund(order_id): return issue_refund(order_id)",
    )
    retriever.insert_chunk(
        "backend/reports/export.py",
        "def generate_quarterly_report(fiscal_year): return build_report(fiscal_year)",
    )
    for i in range(6):
        retriever.insert_chunk(f"backend/filler_{i}.py", f"def unrelated_thing_{i}(): pass")


class TestDocumentMapping:
    """The adapter maps each RetrievedChunk to a Document with the expected content/metadata."""

    def test_page_content_and_metadata_match_the_retrieved_chunk(
        self, retriever: HybridRetriever
    ) -> None:
        chunk_id = retriever.insert_chunk(
            "backend/auth/login.py",
            "def login(request): return authenticate(request.credentials)",
        )
        adapter = HybridRetrieverAdapter(hybrid_retriever=retriever, top_k=5)

        [document] = adapter._get_relevant_documents("authenticate login", run_manager=None)  # type: ignore[arg-type]

        assert isinstance(document, Document)
        assert document.page_content == "def login(request): return authenticate(request.credentials)"
        assert document.metadata["path"] == "backend/auth/login.py"
        assert document.metadata["id"] == chunk_id
        assert isinstance(document.metadata["score"], float)

    def test_no_fields_are_invented_beyond_what_retrievedchunk_has(
        self, retriever: HybridRetriever
    ) -> None:
        retriever.insert_chunk("backend/x.py", "def x(): pass")
        adapter = HybridRetrieverAdapter(hybrid_retriever=retriever, top_k=1)

        [document] = adapter._get_relevant_documents("x", run_manager=None)  # type: ignore[arg-type]

        assert set(document.metadata.keys()) == {"path", "id", "score"}


class TestFaithfulPassThrough:
    """THE key test: identical chunks, identical order, vs calling HybridRetriever directly.

    This is the direct proof that adapting HybridRetriever into a
    BaseRetriever has not perturbed the RRF fusion ranking -- the adapter
    must be a faithful pass-through, not a second implementation that
    happens to agree most of the time.
    """

    def test_same_chunks_same_order_as_calling_hybrid_search_directly(
        self, retriever: HybridRetriever
    ) -> None:
        _seed_mixed_corpus(retriever)
        adapter = HybridRetrieverAdapter(hybrid_retriever=retriever, top_k=5)

        direct_chunks = retriever.hybrid_search("authenticate login refund report", top_k=5)
        adapted_documents = adapter.invoke("authenticate login refund report")

        assert len(adapted_documents) == len(direct_chunks) > 0
        for document, chunk in zip(adapted_documents, direct_chunks, strict=True):
            assert document.page_content == chunk.content
            assert document.metadata["id"] == chunk.id
            assert document.metadata["path"] == chunk.path
            assert document.metadata["score"] == chunk.score

    def test_faithful_across_several_distinct_queries(self, retriever: HybridRetriever) -> None:
        """Not a one-query fluke: the same identity holds for several unrelated queries."""
        _seed_mixed_corpus(retriever)
        adapter = HybridRetrieverAdapter(hybrid_retriever=retriever, top_k=4)

        for query in ("login", "refund order", "quarterly fiscal report", "unrelated thing"):
            direct_ids = [chunk.id for chunk in retriever.hybrid_search(query, top_k=4)]
            adapted_ids = [doc.metadata["id"] for doc in adapter.invoke(query)]
            assert adapted_ids == direct_ids, f"order/identity diverged for query={query!r}"


class TestTopKRespected:
    def test_top_k_limits_the_number_of_documents_returned(self, retriever: HybridRetriever) -> None:
        for i in range(10):
            retriever.insert_chunk(f"backend/mod_{i}.py", f"def handler_{i}(): return {i}")
        adapter = HybridRetrieverAdapter(hybrid_retriever=retriever, top_k=3)

        documents = adapter.invoke("handler")

        assert len(documents) == 3

    def test_default_top_k_defers_to_the_underlying_retriever(
        self, retriever: HybridRetriever
    ) -> None:
        """No top_k override -> HybridRetriever's own default (Settings.hybrid_retrieval_top_k)."""
        for i in range(10):
            retriever.insert_chunk(f"backend/mod_{i}.py", f"def handler_{i}(): return {i}")
        adapter = HybridRetrieverAdapter(hybrid_retriever=retriever)

        documents = adapter.invoke("handler")
        direct = retriever.hybrid_search("handler")

        assert len(documents) == len(direct) == _BASE_SETTINGS.hybrid_retrieval_top_k


class TestEmptyCorpus:
    def test_empty_corpus_returns_empty_list_not_an_error(self, retriever: HybridRetriever) -> None:
        adapter = HybridRetrieverAdapter(hybrid_retriever=retriever, top_k=5)

        documents = adapter.invoke("anything at all")

        assert documents == []


class TestRunnableInterface:
    """The whole point of the exercise: this is a real langchain_core Runnable."""

    def test_is_a_base_retriever_instance(self, retriever: HybridRetriever) -> None:
        adapter = HybridRetrieverAdapter(hybrid_retriever=retriever)
        assert isinstance(adapter, BaseRetriever)

    def test_invoke_works(self, retriever: HybridRetriever) -> None:
        retriever.insert_chunk("backend/auth/login.py", "def login(): return authenticate()")
        adapter = HybridRetrieverAdapter(hybrid_retriever=retriever, top_k=2)

        result = adapter.invoke("login authenticate")

        assert isinstance(result, list)
        assert all(isinstance(document, Document) for document in result)

    async def test_ainvoke_works(self, retriever: HybridRetriever) -> None:
        retriever.insert_chunk("backend/auth/login.py", "def login(): return authenticate()")
        adapter = HybridRetrieverAdapter(hybrid_retriever=retriever, top_k=2)

        result = await adapter.ainvoke("login authenticate")

        assert isinstance(result, list)
        assert all(isinstance(document, Document) for document in result)

    def test_composes_with_a_runnable_pipe(self, retriever: HybridRetriever) -> None:
        """Minimal, honest proof it composes: `retriever | format_fn` as a real Runnable chain.

        No LLM call, no parallel review pipeline -- exactly what
        `backend.memory.langchain_retriever`'s module docstring says this
        adapter is for and is not for. See also
        `scripts/demo_langchain_retriever_composition.py` for a
        stand-alone, runnable version of the same demonstration.
        """
        retriever.insert_chunk(
            "backend/payments/refund.py",
            "def process_refund(order_id): return issue_refund(order_id)",
        )
        adapter = HybridRetrieverAdapter(hybrid_retriever=retriever, top_k=1)

        def join_sources(documents: list[Document]) -> str:
            return ", ".join(document.metadata["path"] for document in documents)

        chain = adapter | join_sources
        result = chain.invoke("process refund order")

        assert result == "backend/payments/refund.py"


class TestAsyncDoesNotBlockTheEventLoop:
    """_aget_relevant_documents must offload, not just declare itself async and block anyway.

    Reuses the exact heartbeat-coroutine pattern
    ``tests/unit/test_embedder.py``'s
    ``TestEmbedAsyncDoesNotBlockTheEventLoop`` established for
    ``OpenAIEmbedder.embed_async`` -- a slow, genuinely-blocking synchronous
    call is injected in place of ``HybridRetriever.hybrid_search``, and a
    concurrent heartbeat coroutine's tick count proves whether the event
    loop was free to run it while the "slow call" was in flight.
    """

    async def test_a_slow_call_does_not_stall_a_concurrent_coroutine(
        self, retriever: HybridRetriever, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        retriever.insert_chunk("backend/auth/login.py", "def login(): return authenticate()")

        slow_seconds = 0.3
        original_hybrid_search = retriever.hybrid_search

        def slow_hybrid_search(query_text: str, top_k: int | None = None) -> object:
            time.sleep(slow_seconds)  # a genuinely blocking, synchronous call
            return original_hybrid_search(query_text, top_k=top_k)

        monkeypatch.setattr(retriever, "hybrid_search", slow_hybrid_search)
        adapter = HybridRetrieverAdapter(hybrid_retriever=retriever, top_k=2)

        heartbeat_ticks = 0

        async def heartbeat() -> None:
            nonlocal heartbeat_ticks
            deadline = asyncio.get_event_loop().time() + slow_seconds
            while asyncio.get_event_loop().time() < deadline:
                heartbeat_ticks += 1
                await asyncio.sleep(0.01)

        result, _ = await asyncio.gather(adapter.ainvoke("login authenticate"), heartbeat())

        assert len(result) == 1
        # If _aget_relevant_documents blocked the event loop (e.g. by
        # calling self._fetch directly instead of via asyncio.to_thread),
        # the heartbeat coroutine would never get scheduled while the slow
        # call ran, and this would be 0 or close to it instead of the ~30
        # ticks 0.3s / 0.01s implies.
        assert heartbeat_ticks > 5, (
            f"only {heartbeat_ticks} heartbeat ticks during the slow call -- "
            "_aget_relevant_documents appears to be blocking the event loop"
        )
