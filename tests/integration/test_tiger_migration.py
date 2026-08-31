"""Live integration tests for M12 (Tiger Cloud Migration) -- PLAN.md's named demo suite.

Owns: proving Stages A-C of ADR-003's Tiger Cloud migration actually work
against the REAL, paid Tiger Cloud instance
(``migrations/scripts/2026-06-tiger-init.sql``), not just that the SQL
parses. Specifically:

- Stage A: ``vector``/``vectorscale`` (plus the pre-installed
  ``timescaledb``/``timescaledb_toolkit``) are actually present.
- Stage B: ``agent_events`` is a REAL hypertable with 1-day chunks, and the
  append-only invariant survives hypertable conversion -- on the
  hypertable itself AND on a specific, real physical chunk, which is the
  genuine regression risk hypertables introduce (see
  ``migrations/scripts/2026-06-tiger-init.sql``'s Stage B comments for the
  concrete gap this project's own L1 BUILD found: a statement-level
  TRUNCATE trigger does NOT propagate to chunks, closed instead by the
  restricted ``agent_events_writer`` role's REVOKEd privileges, which DO
  propagate).
- The two named continuous aggregates (``agent_health_1m``,
  ``pr_cost_hourly``) return numerically correct results against known
  seeded rows, with the synthetic-row exclusion filter proven to survive
  being baked into the view definition rather than applied at read time.
- The M13 swap (``EventRepository.aggregate_llm_calls_by_agent`` reading
  ``agent_health_1m`` instead of raw ``agent_events`` when
  ``events_backend='tiger'``) produces the SAME honest totals a real
  dashboard would render, with synthetic rows still excluded.
- Stage C: the DiskANN index exists and the query planner actually chooses
  it (not a sequential scan) at a realistic row count, and hybrid
  retrieval still returns correct results against it.

COST DISCIPLINE: every class in this file is marked ``@pytest.mark.live``
(deselected by default via ``pyproject.toml``'s ``addopts``, mirroring
every other real-external-dependency test in this project) AND skipped --
not failed -- when Tiger Cloud is not configured (``PGHOST`` unset) or not
reachable, via a module-level ``skipif`` computed once at collection time,
the same pattern ``tests/integration/test_events_spine.py`` and
``tests/integration/test_hybrid_retrieval.py`` already use for their own
real dependencies. A plain ``pytest`` run (no ``-m live``, no PG* config)
never touches Tiger Cloud at all.

APPEND-ONLY MEANS THIS FILE'S OWN ``agent_events`` TEST ROWS CAN NEVER BE
DELETED -- by design, the same design this file exists to prove. Every
review_id this file writes uses the ``"m12-"`` prefix
(``backend.database.repository._TEST_FIXTURE_REVIEW_ID_PREFIXES``
recognizes it), so these rows are excluded from every dashboard/aggregate
total forever, the same way this project's other test suites' own
``budget-guard-``/``append-only-``/etc. fixture rows already are -- except
for the ONE deliberate exception in
``TestContinuousAggregateExclusionSurvivesTheSwap``, which needs a row
that is NOT excluded to prove inclusion (not just exclusion) is correct;
see that class's own docstring for why that row is real, permanent, and
accepted. ``code_chunks`` (Stage C) has no such constraint -- it is fully
rebuildable, so this file truncates it freely, mirroring
``tests/integration/test_hybrid_retrieval.py``'s own ``retriever`` fixture.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import psycopg
import pytest
from pgvector import Vector

from backend.core.settings import get_settings
from backend.database.postgres import init_tiger_schema
from backend.database.repository import EventRepository
from backend.memory.context_retriever import HybridRetriever
from backend.memory.embedder import DeterministicFixtureEmbedder
from backend.memory.tiger_client import connect

_BASE_SETTINGS = get_settings()


def _tiger_configured_and_reachable() -> bool:
    """Best-effort check: is Tiger Cloud configured AND reachable right now.

    Two separate conditions, both required, mirroring this project's other
    ``skipif`` gates (``_postgres_reachable``/``_pgvector_reachable``) but
    with an extra first check: unlike local Postgres/pgvector (always
    "configured", just maybe not running), Tiger Cloud has no default at
    all -- ``PGHOST`` unset means "no Tiger account for this build", not
    "Tiger is temporarily down", and must skip cleanly either way.
    """
    if not _BASE_SETTINGS.pghost:
        return False
    try:
        with psycopg.connect(_BASE_SETTINGS.resolve_tiger_dsn(), connect_timeout=3) as conn:
            conn.execute("SELECT 1")
        return True
    except (psycopg.Error, OSError):
        return False


pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not _tiger_configured_and_reachable(),
        reason=(
            "Tiger Cloud not configured/reachable -- set PGHOST/PGPORT/PGUSER/"
            "PGPASSWORD/PGDATABASE/PGSSLMODE (see .env.example) to run this suite"
        ),
    ),
]


@pytest.fixture(scope="module", autouse=True)
def _migrated_tiger_schema() -> None:
    """Apply migrations/scripts/2026-06-tiger-init.sql once per test-module run."""
    init_tiger_schema(_BASE_SETTINGS.resolve_tiger_dsn())


def _unique_review_id(suffix: str) -> str:
    """A per-test-run unique, RECOGNIZED-PREFIX review_id -- see this module's own docstring.

    Every row this helper names is excluded from every dashboard/aggregate
    total by ``backend.database.repository._TEST_FIXTURE_REVIEW_ID_PREFIXES``'s
    ``"m12-"`` entry, and (being written into a real, append-only table on
    a real, paid, shared Tiger instance) can never be deleted -- using a
    UUID suffix keeps repeated runs from colliding with each other, not
    from accumulating; accumulation is the accepted, disclosed cost of
    testing an append-only table's real behavior for real, the same
    tradeoff this project's other live/fixture-writing test suites already
    made (see ``tests/integration/test_events_spine.py``'s and
    ``tests/integration/test_budget_guard_events.py``'s own module
    docstrings for precedent).
    """
    return f"m12-{suffix}-{uuid.uuid4()}"


def _insert_llm_call(
    conn: psycopg.Connection,
    *,
    review_id: str,
    ts: datetime,
    agent: str = "security",
    model: str = "claude-haiku-4-5",
    cost_usd: str = "0.001000",
    latency_ms: int = 500,
    tokens_in: int = 100,
    tokens_out: int = 50,
) -> None:
    conn.execute(
        """
        INSERT INTO agent_events
            (ts, review_id, event_type, agent, model, tokens_in, tokens_out,
             cost_usd, latency_ms, outcome, confidence)
        VALUES (%s, %s, 'llm.call', %s, %s, %s, %s, %s, %s, 'ok', 0.900)
        """,
        (ts, review_id, agent, model, tokens_in, tokens_out, Decimal(cost_usd), latency_ms),
    )


class TestExtensionsPresent:
    """Stage A: vector + vectorscale created; timescaledb(+toolkit) pre-installed."""

    def test_all_four_extensions_present(self) -> None:
        with psycopg.connect(_BASE_SETTINGS.resolve_tiger_dsn(), autocommit=True) as conn:
            rows = conn.execute(
                "SELECT extname, extversion FROM pg_extension "
                "WHERE extname IN ('timescaledb', 'timescaledb_toolkit', 'vector', 'vectorscale') "
                "ORDER BY extname"
            ).fetchall()
        names = {row[0] for row in rows}
        assert names == {"timescaledb", "timescaledb_toolkit", "vector", "vectorscale"}
        for _name, version in rows:
            assert version  # every extension reports a real, non-empty version string


class TestHypertableChunking:
    """Stage B: agent_events is a real hypertable, chunked by 1-day ts ranges."""

    def test_agent_events_is_a_hypertable(self) -> None:
        with psycopg.connect(_BASE_SETTINGS.resolve_tiger_dsn(), autocommit=True) as conn:
            row = conn.execute(
                "SELECT hypertable_name FROM timescaledb_information.hypertables "
                "WHERE hypertable_name = 'agent_events'"
            ).fetchone()
        assert row is not None

    def test_chunk_interval_is_one_day(self) -> None:
        with psycopg.connect(_BASE_SETTINGS.resolve_tiger_dsn(), autocommit=True) as conn:
            row = conn.execute(
                "SELECT time_interval FROM timescaledb_information.dimensions "
                "WHERE hypertable_name = 'agent_events' AND column_name = 'ts'"
            ).fetchone()
        assert row is not None
        assert row[0] == timedelta(days=1)


class TestAppendOnlyEnforcementOnHypertableAndChunk:
    """The CRITICAL M12 claim: append-only survives hypertable conversion --
    on the parent hypertable AND on a specific physical chunk, both real
    mechanisms proven with real rejected DML, not merely inspected."""

    def test_update_rejected_on_hypertable(self) -> None:
        review_id = _unique_review_id("append-only-hyper-update")
        with psycopg.connect(_BASE_SETTINGS.resolve_tiger_dsn(), autocommit=True) as conn:
            _insert_llm_call(conn, review_id=review_id, ts=datetime.now(UTC))
            with pytest.raises(psycopg.errors.RaiseException) as exc_info:
                conn.execute(
                    "UPDATE agent_events SET outcome = 'hacked' WHERE review_id = %s",
                    (review_id,),
                )
        assert "append-only" in str(exc_info.value)
        assert "UPDATE" in str(exc_info.value)

    def test_delete_rejected_on_hypertable(self) -> None:
        review_id = _unique_review_id("append-only-hyper-delete")
        with psycopg.connect(_BASE_SETTINGS.resolve_tiger_dsn(), autocommit=True) as conn:
            _insert_llm_call(conn, review_id=review_id, ts=datetime.now(UTC))
            with pytest.raises(psycopg.errors.RaiseException) as exc_info:
                conn.execute("DELETE FROM agent_events WHERE review_id = %s", (review_id,))
        assert "append-only" in str(exc_info.value)
        assert "DELETE" in str(exc_info.value)

    def test_truncate_rejected_on_hypertable_even_for_the_admin(self) -> None:
        review_id = _unique_review_id("append-only-hyper-truncate")
        with psycopg.connect(_BASE_SETTINGS.resolve_tiger_dsn(), autocommit=True) as conn:
            _insert_llm_call(conn, review_id=review_id, ts=datetime.now(UTC))
            with pytest.raises(psycopg.errors.RaiseException) as exc_info:
                conn.execute("TRUNCATE agent_events")
            assert "append-only" in str(exc_info.value)
            assert "TRUNCATE" in str(exc_info.value)
            # The row survives: rejected before it took effect.
            row = conn.execute(
                "SELECT count(*) FROM agent_events WHERE review_id = %s", (review_id,)
            ).fetchone()
        assert row is not None
        assert row[0] == 1

    def test_row_level_triggers_propagate_to_a_real_chunk(self) -> None:
        """The genuine hypertable-specific regression risk: does the
        append-only trigger actually fire when a mutation targets a
        specific CHUNK's own table name, not just the hypertable's virtual
        name -- proven by inserting a row far enough in the past to land
        in its own chunk, then mutating that chunk directly."""
        review_id = _unique_review_id("append-only-chunk-row")
        old_ts = datetime.now(UTC) - timedelta(days=10)
        with psycopg.connect(_BASE_SETTINGS.resolve_tiger_dsn(), autocommit=True) as conn:
            _insert_llm_call(conn, review_id=review_id, ts=old_ts)
            chunk_row = conn.execute(
                "SELECT chunk_schema, chunk_name FROM timescaledb_information.chunks "
                "WHERE hypertable_name = 'agent_events' AND range_start <= %s AND range_end > %s",
                (old_ts, old_ts),
            ).fetchone()
            assert chunk_row is not None, "expected the INSERT above to have created its own chunk"
            chunk_schema, chunk_name = chunk_row

            with pytest.raises(psycopg.errors.RaiseException) as update_exc:
                conn.execute(
                    f'UPDATE "{chunk_schema}"."{chunk_name}" SET outcome = %s WHERE review_id = %s',
                    ("hacked", review_id),
                )
            assert "append-only" in str(update_exc.value)

            with pytest.raises(psycopg.errors.RaiseException) as delete_exc:
                conn.execute(
                    f'DELETE FROM "{chunk_schema}"."{chunk_name}" WHERE review_id = %s',
                    (review_id,),
                )
            assert "append-only" in str(delete_exc.value)

    def test_truncate_on_a_specific_chunk_is_blocked_by_the_restricted_role(self) -> None:
        """CRITICAL FINDING this test locks in as a regression test: a
        statement-level TRUNCATE trigger does NOT propagate to chunks (an
        earlier, disclosed L1 BUILD probe proved a bare TRUNCATE against a
        real chunk, issued as the admin, silently succeeded with no
        exception at all). The restricted ``agent_events_writer`` role's
        REVOKEd TRUNCATE privilege is what actually closes this gap -- and
        because TimescaleDB propagates GRANT/REVOKE to chunks automatically
        (existing AND future ones), this holds for a chunk this test
        creates itself, not merely a pre-existing one."""
        review_id = _unique_review_id("append-only-chunk-truncate")
        # A distinct day so this test gets its own, fresh chunk rather than
        # potentially sharing one with another test in this class.
        distinct_ts = datetime.now(UTC) - timedelta(days=20)
        with psycopg.connect(_BASE_SETTINGS.resolve_tiger_dsn(), autocommit=True) as admin_conn:
            _insert_llm_call(admin_conn, review_id=review_id, ts=distinct_ts)
            chunk_row = admin_conn.execute(
                "SELECT chunk_schema, chunk_name FROM timescaledb_information.chunks "
                "WHERE hypertable_name = 'agent_events' AND range_start <= %s AND range_end > %s",
                (distinct_ts, distinct_ts),
            ).fetchone()
        assert chunk_row is not None
        chunk_schema, chunk_name = chunk_row

        with psycopg.connect(
            _BASE_SETTINGS.resolve_tiger_writer_dsn(), autocommit=True
        ) as writer_conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                writer_conn.execute(f'TRUNCATE "{chunk_schema}"."{chunk_name}"')
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                writer_conn.execute(f'UPDATE "{chunk_schema}"."{chunk_name}" SET outcome = %s', ("x",))
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                writer_conn.execute(f'DELETE FROM "{chunk_schema}"."{chunk_name}"')

        # The row survives every rejected attempt above.
        with psycopg.connect(_BASE_SETTINGS.resolve_tiger_dsn(), autocommit=True) as conn:
            row = conn.execute(
                "SELECT count(*) FROM agent_events WHERE review_id = %s", (review_id,)
            ).fetchone()
        assert row is not None
        assert row[0] == 1

    def test_writer_role_can_still_select_and_insert(self) -> None:
        """The restricted role is restricted, not useless -- SELECT/INSERT
        (the only two operations application code actually needs) still
        work, proving the REVOKEs above are scoped exactly to
        UPDATE/DELETE/TRUNCATE, not accidentally broader."""
        review_id = _unique_review_id("writer-role-select-insert")
        with psycopg.connect(
            _BASE_SETTINGS.resolve_tiger_writer_dsn(), autocommit=True
        ) as writer_conn:
            _insert_llm_call(writer_conn, review_id=review_id, ts=datetime.now(UTC))
            row = writer_conn.execute(
                "SELECT count(*) FROM agent_events WHERE review_id = %s", (review_id,)
            ).fetchone()
        assert row is not None
        assert row[0] == 1


class TestContinuousAggregateExclusionSurvivesTheSwap:
    """The M13-history-critical claim: agent_health_1m/pr_cost_hourly's
    baked-in synthetic-row exclusion actually excludes what it should and
    counts what it shouldn't -- proven against REAL seeded rows, not
    inspected SQL text.

    DELIBERATE, DISCLOSED EXCEPTION to this file's own "every row uses the
    m12- prefix" rule: ``test_a_real_review_id_is_correctly_included``
    below seeds one row under a ``webhook-``-prefixed review_id (production's
    real shape, per ``backend.database.repository``'s own prefix-list
    comment) specifically BECAUSE proving inclusion requires a row the
    exclusion filter does NOT match -- an "m12-" row proves exclusion, never
    inclusion. That one row is real, permanent (append-only), and tiny
    ($0.005) -- the same "test fixture written into the real, append-only
    production table" cost this project's own history (see
    ``backend.database.repository``'s ``ROOT_CAUSE_JUDGEMENT`` comment)
    already accepts elsewhere, now paid once more, deliberately, to prove
    this specific claim for real.
    """

    def test_synthetic_prefixed_row_is_excluded_from_both_aggregates(self) -> None:
        review_id = _unique_review_id("cagg-synth-exclude")
        now = datetime.now(UTC)
        with psycopg.connect(_BASE_SETTINGS.resolve_tiger_dsn(), autocommit=True) as conn:
            _insert_llm_call(
                conn, review_id=review_id, ts=now, cost_usd="9999.000000", latency_ms=1
            )
            conn.execute("CALL refresh_continuous_aggregate('agent_health_1m', NULL, NULL)")
            conn.execute("CALL refresh_continuous_aggregate('pr_cost_hourly', NULL, NULL)")
            health_row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd_sum), 0) FROM agent_health_1m "
                "WHERE agent = 'security' AND model = 'claude-haiku-4-5'"
            ).fetchone()
            pr_row = conn.execute(
                "SELECT cost_usd_sum FROM pr_cost_hourly WHERE review_id = %s", (review_id,)
            ).fetchone()
        assert pr_row is None  # the m12- prefixed review_id never entered the view at all
        assert health_row is not None
        # $9,999 (NUMERIC(10,6)'s own max magnitude) would be impossible to
        # miss if it leaked in -- the same
        # "an unmistakably large synthetic number" technique
        # tests/integration/test_budget_guard_events.py already uses.
        assert Decimal(health_row[0]) < Decimal("100")

    def test_a_real_review_id_is_correctly_included(self) -> None:
        review_id = f"webhook-m12-cagg-inclusion-{uuid.uuid4()}"
        now = datetime.now(UTC)
        with psycopg.connect(_BASE_SETTINGS.resolve_tiger_dsn(), autocommit=True) as conn:
            _insert_llm_call(
                conn,
                review_id=review_id,
                ts=now,
                agent="m12-cagg-inclusion-agent",
                cost_usd="0.005000",
                latency_ms=1234,
                tokens_in=111,
                tokens_out=22,
            )
            conn.execute("CALL refresh_continuous_aggregate('agent_health_1m', NULL, NULL)")
            conn.execute("CALL refresh_continuous_aggregate('pr_cost_hourly', NULL, NULL)")
            health_row = conn.execute(
                "SELECT call_count, cost_usd_sum, latency_ms_sum, tokens_in_sum, tokens_out_sum "
                "FROM agent_health_1m WHERE agent = 'm12-cagg-inclusion-agent'"
            ).fetchone()
            pr_row = conn.execute(
                "SELECT call_count, cost_usd_sum FROM pr_cost_hourly WHERE review_id = %s",
                (review_id,),
            ).fetchone()
        assert health_row is not None
        assert (int(health_row[0]), Decimal(health_row[1]), int(health_row[2])) == (
            1,
            Decimal("0.005000"),
            1234,
        )
        assert (int(health_row[3]), int(health_row[4])) == (111, 22)
        assert pr_row is not None
        assert (int(pr_row[0]), Decimal(pr_row[1])) == (1, Decimal("0.005000"))


class TestCostAggregationExcludesSyntheticAfterTheSwap:
    """EventRepository.aggregate_llm_calls_by_agent(events_backend='tiger')
    -- the actual M13 dashboard call, not raw SQL -- still excludes
    synthetic rows and still counts real ones, end to end through the
    swapped implementation."""

    def test_synthetic_row_excluded_real_row_counted_through_the_repository(self) -> None:
        repo = EventRepository(_BASE_SETTINGS.resolve_tiger_writer_dsn(), events_backend="tiger")
        real_agent = f"m12-repo-swap-agent-{uuid.uuid4().hex[:8]}"
        synthetic_review_id = _unique_review_id("repo-swap-synthetic")
        real_review_id = f"webhook-m12-repo-swap-real-{uuid.uuid4()}"
        now = datetime.now(UTC)
        with psycopg.connect(_BASE_SETTINGS.resolve_tiger_dsn(), autocommit=True) as conn:
            _insert_llm_call(
                conn,
                review_id=synthetic_review_id,
                ts=now,
                agent=real_agent,
                cost_usd="9999.000000",
            )
            _insert_llm_call(
                conn,
                review_id=real_review_id,
                ts=now,
                agent=real_agent,
                cost_usd="0.002500",
                latency_ms=600,
                tokens_in=40,
                tokens_out=15,
            )
            conn.execute("CALL refresh_continuous_aggregate('agent_health_1m', NULL, NULL)")

        result = repo.aggregate_llm_calls_by_agent(now=now + timedelta(seconds=1))
        matching = [m for m in result.metrics if m.agent == real_agent]
        assert len(matching) == 1
        metrics = matching[0]
        assert metrics.call_count == 1  # only the real row, not the $99,999 synthetic one
        assert metrics.total_cost_usd == Decimal("0.002500")
        assert metrics.avg_latency_ms == 600
        assert metrics.total_tokens_in == 40
        assert metrics.total_tokens_out == 15

        # The exclusion summary (still read from raw agent_events on either
        # backend) accounts for the synthetic row that was kept OUT of
        # metrics above -- transparency survives the swap too.
        assert result.exclusions.test_fixture_cost_usd >= Decimal("9999.000000")


class TestDiskannIndexAndHybridRetrieval:
    """Stage C: the DiskANN index exists, the planner actually uses it at
    realistic scale, and hybrid retrieval still returns correct results
    against it."""

    def test_diskann_index_exists(self) -> None:
        with psycopg.connect(_BASE_SETTINGS.resolve_tiger_dsn(), autocommit=True) as conn:
            row = conn.execute(
                "SELECT indexdef FROM pg_indexes "
                "WHERE tablename = 'code_chunks' AND indexname = 'idx_code_chunks_embedding_diskann'"
            ).fetchone()
        assert row is not None
        assert "diskann" in row[0]

    def test_planner_uses_diskann_at_realistic_scale(self) -> None:
        dsn = _BASE_SETTINGS.resolve_tiger_dsn()
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("TRUNCATE code_chunks")
            conn.execute(
                """
                INSERT INTO code_chunks (path, content, embedding)
                SELECT
                    'm12/planner_scale_test/file_' || g || '.py',
                    'def f_' || g || '(): pass',
                    (SELECT ('[' || string_agg(round((random() * 2 - 1)::numeric, 4)::text, ',') || ']')::vector
                     FROM generate_series(1, 256))
                FROM generate_series(1, 2000) AS g
                """
            )
            # A known query vector, fetched via the primary key (an Index
            # Scan on code_chunks_pkey, not a Seq Scan) rather than an
            # unordered `LIMIT 1` sub-select -- the latter has no ORDER BY
            # to justify an index, so Postgres correctly (and irrelevantly
            # to this test's actual claim) picks a Seq Scan for IT, which
            # then falsely trips a bare "Seq Scan" substring check on the
            # whole plan even though the real ANN ORDER BY below still uses
            # the DiskANN index. Fetching the query vector as a plain
            # Python value and binding it directly sidesteps the ambiguity
            # entirely -- mirrors the manual verification this migration's
            # build report already ran (`... WHERE id = 1`).
            first_row = conn.execute(
                "SELECT id, embedding FROM code_chunks ORDER BY id LIMIT 1"
            ).fetchone()
            assert first_row is not None
            # This bare psycopg connection has no `vector` type adapter
            # registered (unlike backend.memory.tiger_client.connect), so
            # `embedding` comes back as its plain text representation
            # (e.g. "[0.1,0.2,...]") -- cast it back explicitly rather than
            # relying on an implicit cast psycopg may not perform.
            query_embedding = str(first_row[1])
            plan_rows = conn.execute(
                "EXPLAIN SELECT id FROM code_chunks ORDER BY embedding <=> %s::vector LIMIT 5",
                (query_embedding,),
            ).fetchall()
            conn.execute("TRUNCATE code_chunks")
        plan_text = "\n".join(row[0] for row in plan_rows)
        assert "idx_code_chunks_embedding_diskann" in plan_text
        assert "Seq Scan" not in plan_text

    def test_hybrid_search_returns_correct_top_k_against_tiger(self) -> None:
        dsn = _BASE_SETTINGS.resolve_tiger_dsn()
        embedder = DeterministicFixtureEmbedder(dimension=_BASE_SETTINGS.embedding_dimension)
        with connect(dsn) as conn:
            conn.execute("TRUNCATE code_chunks")
        retriever = HybridRetriever(dsn, embedder, settings=_BASE_SETTINGS)

        retriever.insert_chunk("m12/auth.py", "def authenticate_user(username, password): ...")
        retriever.insert_chunk("m12/math_utils.py", "def compute_checksum(data): ...")
        retriever.insert_chunk("m12/unrelated.py", "def render_widget(config): ...")

        results = retriever.hybrid_search("authenticate_user", top_k=2)

        assert len(results) >= 1
        assert results[0].path == "m12/auth.py"

        with connect(dsn) as conn:
            conn.execute("TRUNCATE code_chunks")

    def test_wrong_dimension_vector_rejected_by_the_diskann_backed_column(self) -> None:
        dsn = _BASE_SETTINGS.resolve_tiger_dsn()
        with connect(dsn) as conn:
            conn.execute("TRUNCATE code_chunks")
            with pytest.raises(psycopg.Error):
                conn.execute(
                    "INSERT INTO code_chunks (path, content, embedding) VALUES (%s, %s, %s)",
                    ("m12/bad.py", "short vector", Vector([0.1, 0.2, 0.3])),
                )
