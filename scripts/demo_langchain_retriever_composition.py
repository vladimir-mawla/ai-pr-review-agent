"""Demo: HybridRetrieverAdapter composed into a real LangChain Runnable chain.

Owns: a small, honest, free (no LLM call, no OpenAI/Anthropic key needed)
demonstration that ``backend.memory.langchain_retriever.HybridRetrieverAdapter``
genuinely composes with LangChain -- the entire reason it exists (see that
module's docstring). This is NOT a parallel review pipeline and does not
call an LLM: it pipes the adapter (a ``Runnable``) into a plain formatting
function via ``|``, then into a ``ChatPromptTemplate``, and prints the
resulting prompt -- proving the adapter's output slots directly into
LangChain's composition primitives without any glue code beyond ``|``.

Not named in any milestone's freeze-boundary file list -- same disclosed,
not-strictly-required-but-directly-useful category as
``scripts/run_fixture_review.py`` (M7) and ``scripts/send_signed_webhook.py``
(M2): required to make this session's own demonstration runnable and
re-checkable, not part of the production review pipeline.

Usage:
    docker compose up -d pgvector
    python scripts/seed_code_chunks.py --repo .   # EMBEDDER_BACKEND=fixture by default
    python scripts/demo_langchain_retriever_composition.py "reciprocal rank fusion"
"""

from __future__ import annotations

import sys

from langchain_core.prompts import ChatPromptTemplate

from backend.core.settings import get_settings
from backend.memory.context_retriever import HybridRetriever
from backend.memory.embedder import DeterministicFixtureEmbedder
from backend.memory.langchain_retriever import HybridRetrieverAdapter
from backend.memory.tiger_client import apply_migrations


def _format_documents(documents: list[object]) -> str:
    """Render retrieved Documents as a labeled block -- the `| ` pipe target."""
    if not documents:
        return "(no relevant code found)"
    blocks = []
    for document in documents:
        path = document.metadata["path"]  # type: ignore[attr-defined]
        score = document.metadata["score"]  # type: ignore[attr-defined]
        blocks.append(f"--- {path} (score={score:.6f}) ---\n{document.page_content}")
    return "\n\n".join(blocks)


def main() -> int:
    query = sys.argv[1] if len(sys.argv) > 1 else "reciprocal rank fusion"

    settings = get_settings()
    apply_migrations(settings.pgvector_url)
    embedder = DeterministicFixtureEmbedder(dimension=settings.embedding_dimension)
    hybrid_retriever = HybridRetriever(settings.pgvector_url, embedder, settings=settings)

    adapter = HybridRetrieverAdapter(hybrid_retriever=hybrid_retriever, top_k=3)

    # A real LangChain Runnable chain: retriever -> formatter -> prompt.
    # No LLM call -- this only proves composition, per this script's own
    # module docstring.
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", "You are reviewing a pull request. Relevant existing code:\n{context}"),
            ("human", "{question}"),
        ]
    )
    chain = {"context": adapter | _format_documents, "question": lambda _: query} | prompt

    result = chain.invoke(query)
    print(f"Query: {query!r}\n")
    print("Composed chain output (a real langchain_core.prompts.ChatPromptTemplate result):\n")
    for message in result.to_messages():
        print(f"[{message.type}] {message.content}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
