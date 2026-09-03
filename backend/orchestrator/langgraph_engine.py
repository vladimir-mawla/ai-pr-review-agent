"""Concrete ``WorkflowEngine`` (ADR-001) backed by LangGraph.

Owns: the only place ``backend.core.workflow_engine.WorkflowEngine``'s three
methods (``run`` / ``resume`` / ``get_state``) are actually implemented for
M4, by wrapping a compiled LangGraph graph (``backend.orchestrator.graph.
build_graph``) plus a checkpointer.

Checkpointer choice — SQLite, not Redis, and why
-------------------------------------------------
PLAN.md's M4 outcome describes checkpointing "to Redis". This module uses
``langgraph_checkpoint_sqlite.SqliteSaver`` instead, and that deviation is
deliberate, not a silent substitution:

- ``langgraph-checkpoint-redis`` (the official LangGraph Redis checkpointer
  package) was installed and smoke-tested directly against this project's
  own ``docker-compose.yml`` Redis (plain ``redis:7-alpine``, from M3). It
  does not work there: on ``.setup()`` it issues ``FT.INFO`` /
  ``FT.CREATE`` — RediSearch module commands — to build a search index for
  checkpoint lookups, and plain Redis does not implement RediSearch. The
  concrete failure observed: ``redis.exceptions.ResponseError: unknown
  command 'FT.INFO'``. Making that package work would mean switching the
  M3-established Redis image to Redis Stack, which is outside M4's freeze
  boundary (``docker-compose.yml`` is not listed) and not a decision this
  milestone should make unilaterally.
- Per this milestone's explicit instructions: when a working LangGraph
  Redis checkpointer is not available, use whatever durable checkpointer
  LangGraph actually ships that works here. ``SqliteSaver`` is exactly
  that: it is a real, first-party LangGraph checkpointer (not a mock), it
  persists to a file (durable across process restarts, not just in-process
  memory), and it was verified empirically (see
  ``tests/integration/test_orchestrator_fanout.py``) to support the exact
  behavior this milestone's gate requires: a node's output committed before
  a crash is not re-executed on resume.
- This is the same kind of documented, justified infra deviation M3 made
  for the Redis host port (6380 instead of 6379) — a real constraint
  discovered on the build machine, recorded rather than hidden.

Why a real file, not ``:memory:``: an in-memory SQLite connection does not
survive the actual process it lives in exiting, which would make "resume
after a crash" untestable in any way that resembles the real failure mode
(a worker process dying and a *new* process resuming its work). Tests use a
temp-file path (via ``tmp_path``) so runs don't collide across test cases;
the default here is a real path under the project so a real local demo run
persists across restarts too.

LANGSMITH TRACE QUALITY (added alongside the LangSmith integration, no
graph restructuring): ``_config`` now attaches ``metadata``/``tags``/a
deterministic ``run_id`` to every ``graph.invoke``/``graph.get_state`` call
via ``RunnableConfig`` -- fields LangChain's own Runnable machinery already
understands natively, so this only enriches whatever trace LangChain's
tracer produces IF one happens to be attached; it makes no direct network
call itself and costs nothing when no tracer is attached (the ordinary
case for every existing test in this project, none of which export
LANGSMITH_TRACING/LANGCHAIN_TRACING_V2 into the real process environment --
see ``backend.observability.tracing``'s module docstring for why that
distinction, not ``Settings.langsmith_tracing``, is what actually gates
whether a tracer activates here).

Two things this project verified empirically (against its own real,
AWS-deployment LangSmith account) needed NO further code change at all:

- Each specialist node already appears as its own clearly-named span
  ("security"/"quality"/"tests"/"docs", matching ``add_node``'s own
  registered names 1:1) with near-identical start/end timestamps
  (genuinely concurrent, not sequential) -- LangGraph names each node's
  span after its own graph node name automatically.
- The "aggregate" node's span already carries the full routing decision
  and ``overall_confidence`` as its own recorded OUTPUT (LangChain's
  tracer records a chain run's actual return value verbatim) -- since
  ``aggregate_node`` (``backend.orchestrator.nodes``) returns
  ``{"review": <the Review, including .status/.overall_confidence>,
  "routing_reason": <str>}``, that is already exactly what shows up on
  that span with zero extra wiring.

What THIS module adds, since it did not already exist: ``review_id``,
``pr_number``, ``repository`` (owner/name), ``head_sha``, and the driver
``model`` as ROOT-run metadata (inherited by every child span, including
all four specialists and aggregate) plus tags, so a trace is identifiable
and correlatable with the matching ``agent_events`` rows and GitHub PR
without having to open the aggregate span specifically.
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path
from typing import Any, cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver

from backend.core.settings import get_settings
from backend.orchestrator.graph import build_graph
from backend.orchestrator.state import GraphState

DEFAULT_CHECKPOINT_DB_PATH = Path("var/orchestrator_checkpoints.sqlite3")

# Fixed, arbitrary namespace UUID (generated once, never changed) for
# deriving a stable root-run id from a given thread_id via `uuid.uuid5` --
# see `_trace_run_id`'s docstring for why this needs to be deterministic
# rather than random.
_TRACE_RUN_ID_NAMESPACE = uuid.UUID("a15c8f2e-2b4d-4c66-9d3a-6a1f2e0c9b71")


def _trace_run_id(thread_id: str) -> uuid.UUID:
    """Derive a stable LangSmith root-run id from ``thread_id`` (the review id).

    Deterministic (not ``uuid.uuid4()``) so a caller who already knows a
    review's ``thread_id`` -- an operator, this milestone's own real-trace
    verification step, a future dashboard feature -- can independently
    compute the exact same LangSmith run id and look it up directly
    (``Client.read_run``), without ``WorkflowEngine.run`` needing to return
    anything beyond the ``GraphState`` its Protocol already promises (see
    ``backend.core.workflow_engine.WorkflowEngine`` -- its three method
    signatures are a fixed contract this module does not change).
    """
    return uuid.uuid5(_TRACE_RUN_ID_NAMESPACE, thread_id)


def _trace_metadata(state: GraphState) -> dict[str, Any]:
    """Root-run metadata identifying which review this trace belongs to.

    Inherited by every child span (LangChain's callback machinery
    propagates a parent run's metadata down to its children by default),
    so the four specialist spans and the aggregate span all carry this
    too, not only the root "LangGraph" span.
    """
    return {
        "review_id": state["review_id"],
        "pr_number": state["pr_number"],
        "repository": f"{state['repository_owner']}/{state['repository_name']}",
        "head_sha": state["head_sha"],
        "model": get_settings().anthropic_model,
    }


def _trace_tags(review_id: str, *, extra: list[str] | None = None) -> list[str]:
    return ["pr-review-agent", f"review_id:{review_id}", *(extra or [])]


class LangGraphWorkflowEngine:
    """``WorkflowEngine[GraphState]`` implementation backed by LangGraph + SQLite.

    Not registered as a subclass of ``backend.core.workflow_engine.
    WorkflowEngine`` via inheritance — ``WorkflowEngine`` is a ``Protocol``,
    so structural conformance (having matching ``run``/``resume``/
    ``get_state`` methods) is what satisfies it, exactly as ADR-001 intends:
    a caller programmed against the abstract interface never needs to know
    this class exists.
    """

    def __init__(self, checkpoint_db_path: str | Path = DEFAULT_CHECKPOINT_DB_PATH) -> None:
        db_path = Path(checkpoint_db_path)
        if str(db_path) != ":memory:":
            db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: LangGraph's Pregel loop runs a super-step's
        # parallel branches on a thread pool even for a sync graph like this
        # one, and SqliteSaver's connection is shared across those threads.
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        # KNOWN RISK (observed by L4 VERIFY on M4, see checkpoints/CURRENT.md):
        # on resume, LangGraph logs "Deserializing unregistered type
        # backend.models.enums.AgentType / Severity / backend.models.findings.
        # Finding from checkpoint. This will be blocked in a future version."
        # Our custom Pydantic/enum types aren't registered with LangGraph's
        # (de)serializer, so a future LangGraph major could hard-block resume
        # for these types. Mitigation if/when that happens: register them via
        # LangGraph's `allowed_msgpack_modules` (or equivalent) config. Not
        # blocking today -- resume works -- but don't upgrade LangGraph
        # without checking this first.
        self._saver = SqliteSaver(self._conn)
        self._graph = build_graph(self._saver)

    @staticmethod
    def _config(
        thread_id: str,
        *,
        metadata: dict[str, Any] | None = None,
        tags: list[str] | None = None,
        run_id: uuid.UUID | None = None,
    ) -> RunnableConfig:
        config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
        # `metadata`/`tags`/`run_id` are plain `RunnableConfig` fields
        # LangChain's own Runnable machinery already understands -- see
        # this module's docstring for why passing them here makes no
        # network call and costs nothing when no tracer happens to be
        # attached to the process (the ordinary case for every existing
        # test in this project).
        if metadata is not None:
            config["metadata"] = metadata
        if tags is not None:
            config["tags"] = tags
        if run_id is not None:
            config["run_id"] = run_id
        return config

    def run(self, thread_id: str, initial_state: GraphState) -> GraphState:
        """Start a new run under ``thread_id`` and run it to completion.

        If a node raises, LangGraph's checkpointer still durably records
        whatever sibling branches in that same super-step *did* complete
        (as "pending writes" against the last good checkpoint) before this
        call re-raises — that persisted partial progress is what ``resume``
        later reads back instead of re-running everything from scratch.

        Also attaches this run's LangSmith trace metadata/tags/root-run-id
        (see ``_trace_metadata``/``_trace_tags``/``_trace_run_id``) so, when
        a tracer happens to be attached, the resulting trace is
        identifiable and correlatable with this same ``review_id`` --- see
        this module's docstring for the full trace-quality reasoning.
        """
        config = self._config(
            thread_id,
            metadata=_trace_metadata(initial_state),
            tags=_trace_tags(initial_state["review_id"]),
            run_id=_trace_run_id(thread_id),
        )
        result = self._graph.invoke(initial_state, config=config)
        # `invoke` is typed to return `Any` (LangGraph supports several output
        # shapes depending on `stream_mode`/`version`); at the default
        # settings used here it always returns a plain dict shaped exactly
        # like `GraphState`, which this cast documents rather than silently
        # trusting via `Any`.
        return cast(GraphState, result)

    def resume(self, thread_id: str) -> GraphState:
        """Continue ``thread_id`` from its last persisted checkpoint.

        Passing ``None`` as the input (rather than a fresh initial state)
        is what tells LangGraph "continue the existing run for this
        ``thread_id``" instead of starting a new one — it resumes the
        Pregel loop from the last checkpoint, re-running only whichever
        tasks did not have a recorded successful result.

        Deliberately does NOT reuse ``run``'s deterministic
        ``_trace_run_id`` here -- a resume is a genuinely separate
        ``graph.invoke`` call (LangChain gives it its own, freshly
        generated root run id when none is passed), and forcing the same
        id as the original run would mean asking LangSmith to create a
        second run under an id it already has one for, an untested and
        unnecessary risk this milestone does not need to take. Metadata/
        tags (best-effort, from whatever state was actually persisted) are
        still attached, so a resumed run's trace remains identifiable and
        correlatable even though its root run id itself is a new one.
        """
        snapshot = self._graph.get_state(self._config(thread_id))
        config = self._config(thread_id)
        if snapshot.values:
            persisted_state = cast(GraphState, snapshot.values)
            config = self._config(
                thread_id,
                metadata=_trace_metadata(persisted_state),
                tags=_trace_tags(persisted_state["review_id"], extra=["resume"]),
            )
        result = self._graph.invoke(None, config=config)
        return cast(GraphState, result)

    def get_state(self, thread_id: str) -> GraphState | None:
        """Read back the current persisted state for ``thread_id`` without mutating it."""
        snapshot = self._graph.get_state(self._config(thread_id))
        if not snapshot.values:
            return None
        return cast(GraphState, snapshot.values)

    def close(self) -> None:
        """Release the underlying SQLite connection. Safe to call once at shutdown."""
        self._conn.close()
