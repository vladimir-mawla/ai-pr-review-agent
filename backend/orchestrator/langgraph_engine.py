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
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver

from backend.orchestrator.graph import build_graph
from backend.orchestrator.state import GraphState

DEFAULT_CHECKPOINT_DB_PATH = Path("var/orchestrator_checkpoints.sqlite3")


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
        self._saver = SqliteSaver(self._conn)
        self._graph = build_graph(self._saver)

    @staticmethod
    def _config(thread_id: str) -> RunnableConfig:
        return {"configurable": {"thread_id": thread_id}}

    def run(self, thread_id: str, initial_state: GraphState) -> GraphState:
        """Start a new run under ``thread_id`` and run it to completion.

        If a node raises, LangGraph's checkpointer still durably records
        whatever sibling branches in that same super-step *did* complete
        (as "pending writes" against the last good checkpoint) before this
        call re-raises — that persisted partial progress is what ``resume``
        later reads back instead of re-running everything from scratch.
        """
        result = self._graph.invoke(initial_state, config=self._config(thread_id))
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
        """
        result = self._graph.invoke(None, config=self._config(thread_id))
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
