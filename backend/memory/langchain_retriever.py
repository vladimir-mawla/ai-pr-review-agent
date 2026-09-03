"""``langchain_core.retrievers.BaseRetriever`` adapter over ``HybridRetriever``.

WHY THIS EXISTS: ``backend.memory.context_retriever.HybridRetriever`` (M9) is a
plain Python class -- it has no relationship to LangChain's ``Runnable``
protocol, so it cannot be piped into a LangChain chain (``retriever | prompt |
llm``), used as a tool a LangGraph node hands to an agent, or composed with
any other LangChain-native primitive. This project already depends on
``langchain-core`` directly (``backend.orchestrator.langgraph_engine`` uses
``RunnableConfig``; LangGraph itself is built on ``Runnable``), so the gap is
real, not hypothetical: there is a well-typed adapter boundary
(``BaseRetriever``) sitting unused between a fully-built retrieval component
and the rest of the LangChain/LangGraph ecosystem.

``HybridRetrieverAdapter`` closes that gap by wrapping one ``HybridRetriever``
instance and implementing exactly the two methods ``BaseRetriever`` asks a
subclass to implement (see its own docstring): ``_get_relevant_documents``
(required) and ``_aget_relevant_documents`` (optional; implemented here, see
below). Everything else -- ``.invoke()``, ``.ainvoke()``, ``.batch()``,
composing with ``|``, LangSmith run tracing -- comes for free from
``BaseRetriever``/``Runnable`` once those two methods exist.

WHAT THIS ADAPTER DELIBERATELY DOES NOT DO:

- It does NOT reimplement reciprocal rank fusion, does not change ``k=60``
  (``Settings.rrf_k``), and does not change the candidate-pool sizing
  (``_CANDIDATE_POOL_MULTIPLIER``/``_MIN_CANDIDATE_POOL`` in
  ``context_retriever.py``). Every ranking decision is made by
  ``HybridRetriever.hybrid_search`` alone; this module calls that one method
  and reshapes its output, nothing more. This matters beyond "don't
  duplicate code": M9's own PLAN.md history records that the current RRF
  configuration was deliberately chosen and then validated against a
  held-out, blindly-selected 15-query set (``tests/fixtures/
  retrieval_queries_holdout.json``) after 13 tuned alternatives were tried
  and rejected for overfitting the original tuning set. Re-deriving or
  perturbing that logic here -- even accidentally, by re-sorting or
  re-truncating results after the fact -- would silently invalidate a
  measurement that took real, deliberate effort to make honest. See this
  module's own faithfulness test
  (``tests/integration/test_langchain_retriever.py``'s
  ``TestFaithfulPassThrough``) for the direct proof that this adapter's
  output is byte-for-byte the same ranking ``HybridRetriever.hybrid_search``
  itself would have produced for the same query and top_k.
- It does NOT talk to Postgres/pgvector itself, does not construct an
  ``Embedder``, and does not open any connection -- all of that remains
  ``HybridRetriever``'s job, unchanged. This class holds a reference to an
  already-constructed ``HybridRetriever`` and nothing else.
- It does NOT build a parallel review pipeline, prompt chain, or LLM call.
  It is an adapter, not a replacement for anything in
  ``backend.orchestrator``/``backend.agents`` -- see
  ``scripts/demo_langchain_retriever_composition.py`` for the (deliberately
  minimal, LLM-free) proof that it composes with a LangChain
  ``Runnable`` chain.

ASYNC DECISION: ``_aget_relevant_documents`` IS implemented here, delegating
to ``asyncio.to_thread`` -- the same pattern already established three times
in this codebase for exactly this reason (``backend.memory.embedder.
OpenAIEmbedder.embed_async``, ``backend.tools.llm_client.AnthropicLLMClient.
complete_async``, ``backend.observability.events.emit_decision_async``; see
each of those modules' docstrings for the "blocking call made directly from a
coroutine stalls the shared event loop" defect class this project has fixed
more than once). ``HybridRetriever.hybrid_search`` opens a synchronous
``psycopg`` connection and blocks on real network I/O (two SELECTs against
pgvector) with no async variant of its own -- calling it directly from a
coroutine would reproduce that exact defect class a fourth time. Note that
``BaseRetriever`` itself would automatically synthesize an equivalent
thread-offloading async method (via ``langchain_core.runnables.config.
run_in_executor``) if this class did not override
``_aget_relevant_documents`` at all -- so implementing it explicitly here is
not required to avoid blocking, but is done anyway for consistency with this
codebase's own established, explicit, directly-testable
``asyncio.to_thread`` idiom (one pattern for "make a blocking call safe from
a coroutine," not two), and so the non-blocking property is proven the same
way ``TestEmbedAsyncDoesNotBlockTheEventLoop`` (``tests/unit/
test_embedder.py``) proves it for ``embed_async``: a heartbeat coroutine
ticking concurrently with a deliberately slowed call. See
``tests/integration/test_langchain_retriever.py``'s
``TestAsyncDoesNotBlockTheEventLoop`` for that proof.
"""

from __future__ import annotations

import asyncio

from langchain_core.callbacks import (
    AsyncCallbackManagerForRetrieverRun,
    CallbackManagerForRetrieverRun,
)
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from pydantic import ConfigDict

from backend.memory.context_retriever import HybridRetriever, RetrievedChunk


def _chunk_to_document(chunk: RetrievedChunk) -> Document:
    """Map one ``RetrievedChunk`` to a LangChain ``Document``, inventing nothing.

    ``page_content`` is the chunk's source text, unchanged. ``metadata``
    carries exactly the fields ``RetrievedChunk`` actually has beyond its
    content: ``path`` (the source path the diff-grounding prompt already
    labels retrieved context with -- see ``backend.agents.base_agent.
    format_retrieved_context``'s ``f"--- {chunk.path} ---"`` header), ``id``
    (the ``code_chunks.id`` row this came from), and ``score`` (the fused
    RRF score -- see ``HybridRetriever.hybrid_search``'s docstring for what
    it does and does not mean: higher is more relevant, but it is not a
    probability and is only meaningful relative to other scores from the
    same call). There is no separate per-ranker rank to surface here --
    ``hybrid_search`` returns only the final fused score, not each
    ranker's own intermediate rank -- so ``score`` is the one relevance
    signal genuinely available to carry through.
    """
    return Document(
        page_content=chunk.content,
        metadata={"path": chunk.path, "id": chunk.id, "score": chunk.score},
    )


class HybridRetrieverAdapter(BaseRetriever):
    """A LangChain ``Runnable`` retriever backed by one ``HybridRetriever`` instance.

    See this module's docstring for the full rationale. In short: this class
    holds a ``HybridRetriever`` and an optional ``top_k`` override, and does
    nothing but call ``HybridRetriever.hybrid_search`` and reshape its
    output into ``Document`` objects -- every ranking decision (vector
    search, full-text search, reciprocal rank fusion, the candidate pool
    size) is made entirely inside ``HybridRetriever``, unchanged.

    Attributes:
        hybrid_retriever: The already-constructed ``HybridRetriever`` to
            delegate every search to. This class never constructs one
            itself (no DSN, no ``Embedder`` config lives here) -- the
            caller wires that up exactly as it already does for any other
            ``HybridRetriever`` consumer (e.g.
            ``backend.agents.base_agent.RetrieverProtocol``).
        top_k: How many fused results to request per query. ``None`` (the
            default) defers to ``HybridRetriever``'s own default
            (``Settings.hybrid_retrieval_top_k``), exactly mirroring
            ``HybridRetriever.hybrid_search``'s own ``top_k: int | None``
            parameter -- this class does not invent a second, different
            default.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    hybrid_retriever: HybridRetriever
    top_k: int | None = None

    def _fetch(self, query: str) -> list[Document]:
        """Delegate to ``HybridRetriever.hybrid_search`` and map the result. Shared by sync and async."""
        chunks = self.hybrid_retriever.hybrid_search(query, top_k=self.top_k)
        return [_chunk_to_document(chunk) for chunk in chunks]

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun
    ) -> list[Document]:
        """``BaseRetriever``'s required sync hook. ``run_manager`` is unused: this adapter
        raises nothing of its own to report and has no intermediate steps worth a
        custom callback event -- ``BaseRetriever.invoke`` already emits the
        standard retriever-start/retriever-end (or retriever-error) events
        around this call for LangSmith/callback consumers.
        """
        return self._fetch(query)

    async def _aget_relevant_documents(
        self, query: str, *, run_manager: AsyncCallbackManagerForRetrieverRun
    ) -> list[Document]:
        """``asyncio.to_thread``-offloaded async hook. See module docstring's ASYNC DECISION."""
        return await asyncio.to_thread(self._fetch, query)


__all__ = ["HybridRetrieverAdapter"]
