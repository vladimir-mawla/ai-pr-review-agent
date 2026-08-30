"""Hybrid retrieval over ``code_chunks``: ANN vector search + full-text search, fused by RRF.

Owns: the spec's L4/3.5 "grounding layer" -- given a query, return the
top-k most relevant code chunks by combining two independent rankers:

1. ANN vector search (``search_vector``): cosine distance (pgvector's
   ``<=>`` operator) over ``code_chunks.embedding``, using the HNSW index
   ``migrations/scripts/dev-pgvector-init.sql`` builds.
2. Full-text keyword search (``search_fulltext``): Postgres's own
   ``ts_rank_cd`` over the generated ``content_tsv`` column, using the GIN
   index that same migration builds.

The two rankings are merged by Reciprocal Rank Fusion (``reciprocal_rank_
fusion``, a pure function with no database dependency -- directly unit
tested with known rank inputs in
``tests/integration/test_hybrid_retrieval.py``, per this milestone's own
"test the fusion arithmetic directly, not just end-to-end" instruction):

    score(chunk) = sum over rankers r that surfaced chunk of 1 / (k + rank_r(chunk))

``rank_r`` is 1-indexed (the top result from a ranker has rank 1, never
0) -- the standard formulation from Cormack, Clarke & Buettcher, 2009
("Reciprocal Rank Fusion outperforms Condorcet and individual Rank
Learning Methods"), which this project's ``Settings.rrf_k`` defaults to
``k=60`` per that paper's own commonly-cited default (see
``Settings.rrf_k``'s docstring for why that constant is chosen
deliberately, not left as an arbitrary knob).

Neither ranker alone is sufficient (this milestone's own justification):
full-text search cannot connect a synonym its stemmer does not recognize
as related (e.g. "login" vs. a query for "authenticate"); vector search
alone can rank an exact rare-token match arbitrarily low once that token
is diluted among many other tokens in a longer chunk, precisely the
"vector search misses an exact keyword" case
``DeterministicFixtureEmbedder`` is deliberately built to reproduce (see
that class's docstring). RRF gives each ranker's own top hits real
influence over the fused result without requiring either one to already
agree with the other.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from pgvector import Vector

from backend.core.settings import Settings, get_settings
from backend.memory.embedder import Embedder, EmbeddingDimensionError
from backend.memory.tiger_client import connect

# How many candidates each individual ranker (vector, full-text) retrieves
# before RRF fuses and truncates to the caller's requested top_k. Larger
# than top_k on purpose: RRF needs each ranker's own ranking of a
# reasonably sized candidate pool to fuse over -- if each ranker only ever
# handed back exactly top_k candidates, a chunk that (say) full-text search
# ranks 6th (just outside a top_k=5 window) could never be pulled in by a
# strong vector-side rank even though fusion is precisely what's supposed
# to let it compete. `max(top_k * 4, 20)` is a simple, deliberately
# generous multiplier -- large enough for this milestone's small seeded
# corpus (a few hundred chunks) that "the true best result was outside
# both candidate pools" is not a realistic failure mode, without being
# large enough to make every query scan the whole table.
_CANDIDATE_POOL_MULTIPLIER = 4
_MIN_CANDIDATE_POOL = 20

_INSERT_SQL = """
    INSERT INTO code_chunks (path, content, embedding)
    VALUES (%s, %s, %s)
    RETURNING id
"""

_VECTOR_SEARCH_SQL = """
    SELECT id, path, content
    FROM code_chunks
    ORDER BY embedding <=> %s
    LIMIT %s
"""

# plainto_tsquery (not to_tsquery) because the query text here is plain
# natural-language/identifier text a caller passes in, not already-
# constructed tsquery syntax -- plainto_tsquery ANDs together the stemmed
# lexemes of every word in the input, which is the right default for "find
# chunks that mention these terms" rather than requiring the caller to
# write boolean tsquery expressions themselves. ts_rank_cd (cover density)
# rather than plain ts_rank: it additionally accounts for how close
# together the matching lexemes appear in the document, a better relevance
# signal than a bare presence count for source-code chunks where matching
# terms clustered together (e.g. in one function signature) usually means
# more relevant than the same terms scattered far apart in a long chunk.
_FULLTEXT_SEARCH_SQL = """
    SELECT id, path, content
    FROM code_chunks
    WHERE content_tsv @@ plainto_tsquery('english', %s)
    ORDER BY ts_rank_cd(content_tsv, plainto_tsquery('english', %s)) DESC
    LIMIT %s
"""


@dataclass(frozen=True)
class RetrievedChunk:
    """One code chunk plus its fused RRF score, as returned by ``hybrid_search``.

    Attributes:
        id: The chunk's ``code_chunks.id``.
        path: Repo-relative file path the chunk was extracted from.
        content: The chunk's source text.
        score: Its fused reciprocal-rank-fusion score (higher is more
            relevant). Not a probability or a distance -- only meaningful
            relative to other scores from the same ``hybrid_search`` call.
    """

    id: int
    path: str
    content: str
    score: float


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[int]], *, k: int = 60
) -> list[tuple[int, float]]:
    """Merge multiple rankers' results by Reciprocal Rank Fusion.

    Args:
        rankings: One sequence per ranker, each a list of document ids in
            that ranker's own rank order (best first, index 0 = rank 1).
            A ranker that did not surface a given document simply omits it
            -- there is no requirement that every ranking contain the same
            ids or be the same length.
        k: The RRF constant. See ``Settings.rrf_k``'s docstring for why 60
            is the deliberate default; passed explicitly here (rather than
            defaulting to ``get_settings().rrf_k``) so this pure function
            has no dependency on process-wide settings and can be unit
            tested with an arbitrary k with no fixture setup at all.

    Returns:
        ``(document_id, fused_score)`` pairs, sorted by score descending
        (ties broken by insertion/dict order, i.e. arbitrarily -- callers
        needing a fully deterministic tiebreak should sort further by
        their own secondary key). A document's score is the sum, over
        every ranking it appears in, of ``1 / (k + rank)`` where ``rank``
        is that document's 1-indexed position in that particular ranking.

    Raises:
        ValueError: if ``k`` is not positive -- a non-positive k would let
            a rank-1 result divide by a non-positive number, which is
            undefined for this formula's intended monotonic-decay
            behavior.
    """
    if k <= 0:
        raise ValueError(f"k must be positive, got {k}")
    scores: dict[int, float] = {}
    for ranking in rankings:
        for zero_indexed_rank, document_id in enumerate(ranking):
            rank = zero_indexed_rank + 1  # RRF's rank is 1-indexed, never 0
            scores[document_id] = scores.get(document_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda pair: pair[1], reverse=True)


class HybridRetriever:
    """Vector + full-text search over ``code_chunks``, fused by RRF.

    Each public method opens and closes its own short-lived connection,
    mirroring ``backend.database.repository.EventRepository``'s pattern
    (see that class's docstring) rather than holding a long-lived pool --
    the right trade-off for this milestone's low-frequency, non-hot-path
    usage (a seeding script and a handful of retrieval calls per review,
    not a per-request webhook path).
    """

    def __init__(
        self,
        dsn: str,
        embedder: Embedder,
        *,
        top_k: int | None = None,
        rrf_k: int | None = None,
        settings: Settings | None = None,
    ) -> None:
        settings = settings if settings is not None else get_settings()
        self._dsn = dsn
        self._embedder = embedder
        self._default_top_k = top_k if top_k is not None else settings.hybrid_retrieval_top_k
        self._rrf_k = rrf_k if rrf_k is not None else settings.rrf_k

    def insert_chunk(self, path: str, content: str) -> int:
        """Embed ``content`` via the configured embedder and insert one row.

        Returns the new row's ``id``.
        """
        embedding = self._embedder.embed([content])[0]
        return self.insert_embedded_chunk(path, content, embedding)

    def insert_embedded_chunk(self, path: str, content: str, embedding: list[float]) -> int:
        """Insert one chunk with an already-computed embedding.

        Separate from ``insert_chunk`` so a caller that already has
        embeddings (e.g. ``scripts/seed_code_chunks.py``, which embeds a
        whole batch of chunks in one call for efficiency, then inserts
        them one row at a time) does not need to re-embed per row.

        Raises:
            ``EmbeddingDimensionError``: ``embedding``'s length does not
                match ``self._embedder.dimension`` -- an application-level
                check, raised BEFORE any SQL is issued. This is defense in
                depth, not the only guard: ``code_chunks.embedding``'s own
                ``VECTOR(256)`` column type (see the migration) rejects a
                mismatched-length vector at the database level regardless
                of whether this check ran first -- proven directly (by a
                raw insert that deliberately bypasses this class) in
                ``tests/integration/test_hybrid_retrieval.py``, so the
                schema itself is never trusting this application check
                alone.
        """
        if len(embedding) != self._embedder.dimension:
            raise EmbeddingDimensionError(
                f"embedding has {len(embedding)} dimension(s), expected "
                f"{self._embedder.dimension} (the configured embedder's "
                "dimension) -- code_chunks.embedding cannot store a vector "
                "of the wrong length."
            )
        with connect(self._dsn) as conn:
            row = conn.execute(_INSERT_SQL, (path, content, Vector(embedding))).fetchone()
        assert row is not None  # INSERT ... RETURNING always returns exactly one row on success
        chunk_id: int = row[0]
        return chunk_id

    def search_vector(self, query_text: str, top_k: int) -> list[tuple[int, str, str]]:
        """ANN search: the ``top_k`` chunks nearest ``query_text``'s embedding by cosine distance.

        Returns ``(id, path, content)`` tuples in ranked order (nearest
        first). An empty ``code_chunks`` table returns an empty list, not
        an error -- ``ORDER BY ... LIMIT`` over zero rows is simply zero
        rows.
        """
        query_embedding = self._embedder.embed([query_text])[0]
        with connect(self._dsn) as conn:
            rows = conn.execute(
                _VECTOR_SEARCH_SQL, (Vector(query_embedding), top_k)
            ).fetchall()
        return [(row[0], row[1], row[2]) for row in rows]

    def search_fulltext(self, query_text: str, top_k: int) -> list[tuple[int, str, str]]:
        """Full-text search: the ``top_k`` chunks best matching ``query_text`` by ``ts_rank_cd``.

        Returns ``(id, path, content)`` tuples in ranked order (best match
        first). A query whose stemmed lexemes match no chunk (or an empty
        ``code_chunks`` table) returns an empty list, not an error --
        ``plainto_tsquery`` matching zero rows is a normal, valid result of
        the ``WHERE`` clause, not a failure.
        """
        with connect(self._dsn) as conn:
            rows = conn.execute(
                _FULLTEXT_SEARCH_SQL, (query_text, query_text, top_k)
            ).fetchall()
        return [(row[0], row[1], row[2]) for row in rows]

    def hybrid_search(self, query_text: str, top_k: int | None = None) -> list[RetrievedChunk]:
        """The milestone's headline operation: vector + full-text, fused by RRF.

        Args:
            query_text: The natural-language or identifier query.
            top_k: How many fused results to return. Defaults to
                ``Settings.hybrid_retrieval_top_k`` (set at construction)
                when omitted.

        Returns:
            Up to ``top_k`` ``RetrievedChunk``s, ordered by fused RRF score
            descending. An empty corpus (or a query neither ranker matches
            anything for) returns an empty list, not an error.
        """
        k = top_k if top_k is not None else self._default_top_k
        candidate_pool = max(k * _CANDIDATE_POOL_MULTIPLIER, _MIN_CANDIDATE_POOL)

        vector_hits = self.search_vector(query_text, candidate_pool)
        fulltext_hits = self.search_fulltext(query_text, candidate_pool)

        vector_ranking = [chunk_id for chunk_id, _path, _content in vector_hits]
        fulltext_ranking = [chunk_id for chunk_id, _path, _content in fulltext_hits]
        fused = reciprocal_rank_fusion([vector_ranking, fulltext_ranking], k=self._rrf_k)

        # Both hit lists together always contain every chunk id RRF fused
        # (a chunk cannot have a fused score without appearing in at least
        # one of the two rankings that produced it), so this lookup never
        # misses.
        rows_by_id = {chunk_id: (path, content) for chunk_id, path, content in vector_hits}
        rows_by_id.update(
            {chunk_id: (path, content) for chunk_id, path, content in fulltext_hits}
        )

        results: list[RetrievedChunk] = []
        for chunk_id, score in fused[:k]:
            path, content = rows_by_id[chunk_id]
            results.append(RetrievedChunk(id=chunk_id, path=path, content=content, score=score))
        return results
