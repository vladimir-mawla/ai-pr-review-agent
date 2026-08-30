"""Integration tests for M9 hybrid retrieval -- PLAN.md's named demo suite.

Owns: proving vector search, full-text search, and their reciprocal-rank-
fusion merge actually work together against a real pgvector-enabled
Postgres (``docker-compose.yml``'s ``pgvector`` service), not just that the
pieces satisfy their type signatures in isolation -- the M5 lesson this
milestone was explicitly told to apply.

Structure:

- ``TestReciprocalRankFusion``: the fusion arithmetic in complete isolation,
  against known rank inputs with hand-computed expected scores -- no
  database, no embedder, per this milestone's own "test the fusion
  arithmetic directly, not just end-to-end" instruction.
- ``TestVectorSearchFindsWhatKeywordMisses`` /
  ``TestKeywordSearchFindsWhatVectorMisses``: the two directions of "hybrid
  beats either single method" this milestone's success criteria name --
  each a genuine, empirically-verified case (not a contrived one where both
  rankers trivially agree), using ``DeterministicFixtureEmbedder``'s
  documented properties (a small synonym-canonicalization table, and a
  short-token filter -- see ``backend.memory.embedder``'s module docstring
  for exactly why each exists and what real-world limitation each models).
- ``TestHybridSearchComposition``: top-k respected, empty corpus, and the
  full hybrid_search composition against a small mixed corpus.
- ``TestDimensionMismatch``: both the application-level guard
  (``HybridRetriever.insert_embedded_chunk``) and the database's own
  ``VECTOR(256)`` column type reject a wrong-length vector.

These tests need a real reachable pgvector-enabled Postgres (``docker
compose up -d pgvector`` from the repo root). They are skipped -- not
failed -- when it is unreachable, via a module-level ``skipif`` computed
once at collection time, the same pattern
``tests/integration/test_events_spine.py`` and
``tests/integration/test_queue_roundtrip.py`` both already use for their
own real dependencies. ``TestReciprocalRankFusion`` needs no database at
all, but lives in this same file (PLAN.md names this exact path as M9's
demo test suite) and under the same module-level skip for simplicity --
harmless, since PLAN.md's own demo command always brings pgvector up
first.
"""

from __future__ import annotations

import psycopg
import pytest
from pgvector import Vector

from backend.core.settings import get_settings
from backend.memory.context_retriever import HybridRetriever, reciprocal_rank_fusion
from backend.memory.embedder import DeterministicFixtureEmbedder, EmbeddingDimensionError
from backend.memory.tiger_client import apply_migrations, connect

_BASE_SETTINGS = get_settings()
_PGVECTOR_URL = _BASE_SETTINGS.pgvector_url


def _pgvector_reachable(dsn: str) -> bool:
    """Best-effort check: can we actually reach the pgvector Postgres at ``dsn`` right now."""
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
    """Apply migrations once per test-module run, against the real pgvector connection."""
    apply_migrations(_PGVECTOR_URL)


@pytest.fixture
def embedder() -> DeterministicFixtureEmbedder:
    """The no-network, deterministic embedder every test in this file uses."""
    return DeterministicFixtureEmbedder(dimension=_BASE_SETTINGS.embedding_dimension)


@pytest.fixture
def retriever(embedder: DeterministicFixtureEmbedder) -> HybridRetriever:
    """A real ``HybridRetriever`` against the pgvector store, with ``code_chunks`` truncated first.

    Truncating before each test (rather than relying on the module-scoped
    migration fixture alone) keeps every test's corpus isolated -- required
    because several tests below depend on exact rank positions among a
    small, fully-controlled set of seeded chunks, which a leftover row from
    a previous test could silently perturb.
    """
    with connect(_PGVECTOR_URL) as conn:
        conn.execute("TRUNCATE code_chunks")
    return HybridRetriever(_PGVECTOR_URL, embedder, settings=_BASE_SETTINGS)


class TestReciprocalRankFusion:
    """The RRF arithmetic itself, against known rank inputs -- no database, no embedder.

    Formula under test: ``score(d) = sum over rankers r that surfaced d of
    1 / (k + rank_r(d))``, ``rank_r`` 1-indexed.
    """

    def test_single_ranker_scores_match_the_formula_exactly(self) -> None:
        """One ranker, three documents: score(d) is exactly 1/(k+rank) for that one ranker."""
        fused = reciprocal_rank_fusion([[10, 20, 30]], k=60)
        assert fused == [
            (10, pytest.approx(1 / 61)),
            (20, pytest.approx(1 / 62)),
            (30, pytest.approx(1 / 63)),
        ]

    def test_two_rankers_agreeing_sums_both_contributions(self) -> None:
        """A document ranked #1 by both rankers gets 1/(k+1) + 1/(k+1), not just one term."""
        fused = reciprocal_rank_fusion([[1, 2, 3], [1, 2, 3]], k=60)
        scores = dict(fused)
        assert scores[1] == pytest.approx(2 / 61)
        assert scores[2] == pytest.approx(2 / 62)
        assert scores[3] == pytest.approx(2 / 63)

    def test_disagreeing_rankers_can_lift_a_document_above_either_ones_top_pick(self) -> None:
        """A document ranked 2nd by BOTH rankers can outscore a document ranked 1st by only one.

        Ranker A: [X, Y]  (X first, Y second)
        Ranker B: [Y, X]  (Y first, X second)
        Neither ranker's own #1 pick agrees with the other's -- but Y and X
        both appear in both rankings, always at rank 1 or 2, so with k=60
        their fused scores are IDENTICAL (1/61 + 1/62 each) -- this is RRF's
        core behavior: a document that shows up near the top of every
        ranker beats one that is #1 in only one ranking and absent from the
        other entirely.
        """
        fused = reciprocal_rank_fusion([["X", "Y"], ["Y", "X"]], k=60)
        scores = dict(fused)
        assert scores["X"] == pytest.approx(1 / 61 + 1 / 62)
        assert scores["Y"] == pytest.approx(1 / 62 + 1 / 61)
        assert scores["X"] == scores["Y"]
        # And both beat a hypothetical document ranked #1 in only one list
        # and absent from the other -- 1/61 alone is less than 1/61+1/62.
        solo_first_place_score = 1 / 61
        assert scores["X"] > solo_first_place_score

    def test_document_present_in_only_one_ranking_still_scores(self) -> None:
        """A document that only one ranker ever saw still gets a nonzero score from that ranker alone."""
        fused = reciprocal_rank_fusion([[1, 2], [3, 4]], k=60)
        scores = dict(fused)
        assert scores[1] == pytest.approx(1 / 61)
        assert scores[3] == pytest.approx(1 / 61)
        assert len(scores) == 4

    def test_ordering_is_by_descending_fused_score(self) -> None:
        """The returned list is sorted best-first, not in input/insertion order."""
        fused = reciprocal_rank_fusion([[100], [200, 100]], k=60)
        # 100: rank 1 in ranker A (1/61) + rank 2 in ranker B (1/62) = 2 contributions.
        # 200: rank 1 in ranker B only (1/61) = 1 contribution.
        assert [doc_id for doc_id, _score in fused] == [100, 200]

    def test_empty_rankings_produce_empty_result(self) -> None:
        assert reciprocal_rank_fusion([], k=60) == []
        assert reciprocal_rank_fusion([[], []], k=60) == []

    def test_nonpositive_k_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="k must be positive"):
            reciprocal_rank_fusion([[1, 2]], k=0)
        with pytest.raises(ValueError, match="k must be positive"):
            reciprocal_rank_fusion([[1, 2]], k=-5)


class TestVectorSearchFindsWhatKeywordMisses:
    """HYBRID_WINS, direction 1: vector similarity surfaces a chunk full-text search cannot.

    ``DeterministicFixtureEmbedder`` canonicalizes a small set of synonyms
    (e.g. "login" -> "authenticate") before hashing, so a chunk that only
    ever says "login" embeds close to a query for "authenticate" -- a
    relationship Postgres's English stemmer has no way to connect (it
    stems "login" and "authenticate" to unrelated lexemes). This models
    exactly what a real trained embedding does that keyword search
    structurally cannot: connect semantically related but lexically
    different terms.
    """

    def test_synonym_chunk_found_by_vector_even_though_fulltext_matches_nothing(
        self, retriever: HybridRetriever
    ) -> None:
        auth_chunk_id = retriever.insert_chunk(
            "backend/auth/login.py",
            "def login(request):\n"
            "    user = get_user(request)\n"
            "    return start_session(user)",
        )
        # Decoys share generic tokens/filler with the query but not the
        # login/authenticate relationship.
        retriever.insert_chunk(
            "backend/notify/welcome.py",
            "def send_welcome_email(user):\n    return render_template(user)",
        )
        retriever.insert_chunk(
            "backend/stats/average.py",
            "def compute_average(values):\n    return sum(values) / len(values)",
        )
        retriever.insert_chunk(
            "backend/db/pool.py",
            "def close_connection(pool):\n    pool.release()\n    return None",
        )

        query = "authenticate user"

        # Full-text genuinely finds nothing: plainto_tsquery ANDs the
        # stemmed lexemes of "authenticate" and "user" together, and no
        # seeded chunk contains the literal word "authenticate" (only
        # "login", which the embedder -- not Postgres's stemmer --
        # recognizes as related).
        fts_hits = retriever.search_fulltext(query, top_k=5)
        assert auth_chunk_id not in [chunk_id for chunk_id, _p, _c in fts_hits]

        # Vector search finds it, ranked first, purely from the
        # login->authenticate canonicalization.
        vector_hits = retriever.search_vector(query, top_k=5)
        assert vector_hits[0][0] == auth_chunk_id

        # And the fused hybrid result still surfaces it in the top-3, even
        # though full-text contributed nothing for this chunk at all --
        # this is the milestone's own success criterion in miniature: a
        # relevant chunk retrievable by vector even where keyword search
        # would have missed it outright.
        fused = retriever.hybrid_search(query, top_k=3)
        assert auth_chunk_id in [chunk.id for chunk in fused]


class TestKeywordSearchFindsWhatVectorMisses:
    """HYBRID_WINS, direction 2: full-text search surfaces a chunk vector similarity ranks last.

    ``DeterministicFixtureEmbedder`` deliberately drops tokens shorter than
    3 characters before hashing (modeling how a real subword-tokenizer
    vocabulary often under-represents very short, rare identifiers/codes).
    A query consisting solely of such a short token therefore embeds as the
    embedder's fixed "no real tokens survived" sentinel vector, which bears
    no genuine relationship to any chunk's real content -- vector search
    degrades to near-random ordering for that query. Full-text search has
    no such blind spot: an exact short lexeme like "s3" is indexed and
    matched like any other word.
    """

    def test_short_exact_token_found_by_fulltext_even_though_vector_ranks_it_last(
        self, retriever: HybridRetriever
    ) -> None:
        target_id = retriever.insert_chunk(
            "backend/storage/uploader.py",
            "Uploads the given file object to s3 for durable storage and "
            "returns the resulting object key",
        )
        decoy_ids = [
            retriever.insert_chunk(
                "backend/config/loader.py",
                "Parses the incoming configuration file and validates every "
                "required field before startup",
            ),
            retriever.insert_chunk(
                "backend/batch/pager.py",
                "Splits a large batch of records into smaller pages for "
                "downstream processing pipelines",
            ),
            retriever.insert_chunk(
                "backend/format/currency.py",
                "Formats a currency amount into a human readable string "
                "using the active locale settings",
            ),
            retriever.insert_chunk(
                "backend/sensors/rolling.py",
                "Computes a rolling average over the most recent window of "
                "sensor readings collected",
            ),
        ]

        query = "s3"

        # Full-text finds exactly the target chunk -- "s3" is a literal,
        # unique lexeme none of the decoys contain.
        fts_hits = retriever.search_fulltext(query, top_k=5)
        assert [chunk_id for chunk_id, _p, _c in fts_hits] == [target_id]

        # Vector search ranks the target chunk dead last among all five --
        # "s3" is filtered out of the embedder's own tokenization (length
        # < 3), so the query embeds as the fixed empty-text sentinel, which
        # has no real relationship to any chunk; empirically (this is a
        # fully deterministic computation, verified directly here) every
        # decoy's incidental cosine similarity with that sentinel beats the
        # target's.
        vector_hits = retriever.search_vector(query, top_k=5)
        vector_ranking = [chunk_id for chunk_id, _p, _c in vector_hits]
        assert vector_ranking[-1] == target_id
        assert target_id not in vector_ranking[:3]

        # The fused hybrid result still surfaces it in the top-3, carried
        # entirely by full-text's contribution -- exactly the milestone's
        # named success criterion: "a query for a known function name
        # returns it in the top-3 fused results via FTS even when the
        # embedding model ranks it lower."
        fused = retriever.hybrid_search(query, top_k=3)
        fused_ids = [chunk.id for chunk in fused]
        assert target_id in fused_ids
        # Sanity: the decoys are indeed still in the corpus and could have
        # crowded it out of a naive vector-only top-3 (they do, above).
        assert set(decoy_ids) | {target_id} == set(vector_ranking)


class TestHybridSearchComposition:
    """top-k respected, empty corpus, and a straightforward mixed-corpus recall check."""

    def test_top_k_is_respected(self, retriever: HybridRetriever) -> None:
        for i in range(10):
            retriever.insert_chunk(f"backend/mod_{i}.py", f"def handler_{i}(): return {i}")
        results = retriever.hybrid_search("handler", top_k=3)
        assert len(results) == 3

    def test_empty_corpus_returns_empty_not_an_error(self, retriever: HybridRetriever) -> None:
        assert retriever.hybrid_search("anything at all", top_k=5) == []
        assert retriever.search_vector("anything at all", top_k=5) == []
        assert retriever.search_fulltext("anything at all", top_k=5) == []

    def test_seeded_chunk_retrievable_by_vector_similarity(
        self, retriever: HybridRetriever
    ) -> None:
        chunk_id = retriever.insert_chunk(
            "backend/payments/refund.py",
            "def process_refund(order_id): return issue_refund(order_id)",
        )
        results = retriever.search_vector("process refund order", top_k=3)
        assert results[0][0] == chunk_id

    def test_seeded_chunk_retrievable_by_keyword(self, retriever: HybridRetriever) -> None:
        chunk_id = retriever.insert_chunk(
            "backend/reports/export.py",
            "def generate_quarterly_report(fiscal_year): return build_report(fiscal_year)",
        )
        retriever.insert_chunk(
            "backend/unrelated/thing.py", "def unrelated_helper(): return None"
        )
        results = retriever.search_fulltext("quarterly report", top_k=3)
        assert results[0][0] == chunk_id

    def test_known_function_name_recall_at_five(self, retriever: HybridRetriever) -> None:
        """PLAN.md's own success criterion, at small scale: a known function name query recalls it."""
        target_id = retriever.insert_chunk(
            "backend/memory/context_retriever.py",
            "def reciprocal_rank_fusion(rankings): return fuse(rankings)",
        )
        for i in range(8):
            retriever.insert_chunk(f"backend/filler_{i}.py", f"def unrelated_thing_{i}(): pass")
        results = retriever.hybrid_search("reciprocal_rank_fusion", top_k=5)
        assert target_id in [chunk.id for chunk in results]


class TestDimensionMismatch:
    """A wrong-dimension vector is rejected clearly, at two independent layers."""

    def test_application_level_check_rejects_wrong_dimension(
        self, retriever: HybridRetriever
    ) -> None:
        """``HybridRetriever.insert_embedded_chunk`` validates before issuing any SQL."""
        with pytest.raises(EmbeddingDimensionError, match="expected 256"):
            retriever.insert_embedded_chunk("backend/bad.py", "content", [0.1] * 10)

    def test_database_column_type_rejects_wrong_dimension_directly(self) -> None:
        """The VECTOR(256) column itself enforces the dimension, bypassing application code entirely.

        Defense in depth: this inserts directly via a raw connection,
        completely bypassing ``HybridRetriever``'s own guard (proven
        separately above), to confirm the schema is not merely trusted to
        be protected by application code that happens to check first.
        """
        with connect(_PGVECTOR_URL) as conn, pytest.raises(psycopg.Error, match="expected 256"):
            conn.execute(
                "INSERT INTO code_chunks (path, content, embedding) VALUES (%s, %s, %s)",
                ("backend/bad.py", "raw bypass insert", Vector([0.1] * 10)),
            )
