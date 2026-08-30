"""Builds the M4 fan-out/fan-in ``StateGraph``.

Owns: wiring ``backend.orchestrator.state.GraphState`` and the four stub
nodes in ``backend.orchestrator.nodes`` into one compiled LangGraph graph.

Fan-out shape: ``START`` routes through a conditional-edge dispatcher
(``_dispatch``) that returns four ``langgraph.types.Send`` objects — one per
specialist — rather than a plain ``add_edge(START, node)`` per specialist.
Both approaches produce genuinely parallel execution in LangGraph's Pregel
loop (a super-step runs every node it fans out to concurrently, via a thread
pool for a sync graph like this one), but ``Send`` is the API PLAN.md's M4
outcome names explicitly, and it is also the API that generalizes to a
data-dependent fan-out (a variable number of specialists) if a later
milestone ever needs that — a hardcoded ``add_edge`` per node would not.

Fan-in shape: all four specialists edge into ``aggregate`` (the M4 stub join
node), which edges into ``END``. ``GraphState``'s reducers
(``operator.add`` for findings, a dict-merge for node_errors) are what make
this fan-in correct rather than lossy — see ``state.py``'s docstring.

Compiling without a checkpointer is exactly the failure this milestone's
DONE.html gate exists to prevent ("LangGraph checkpoints actually resume
after a simulated worker crash — compiled with a checkpointer, not
without"), so ``build_graph`` requires one as a parameter; there is no
checkpointer-less code path in this module.
"""

from __future__ import annotations

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Send

from backend.orchestrator.nodes import (
    aggregate_node,
    docs_node,
    quality_node,
    security_node,
    tests_node,
)
from backend.orchestrator.state import GraphState

_SPECIALIST_NODES = ["security", "quality", "tests", "docs"]


def _dispatch(state: GraphState) -> list[Send]:
    """Fan out from ``START`` to all four specialists via the Send API.

    Each ``Send`` carries the full current state to its target node — the
    specialists only read ``config`` (for ``thread_id``) at M4, but passing
    the real state (rather than an empty dict) keeps this dispatcher correct
    if a later milestone's specialist needs to read, e.g., the PR diff.
    """
    return [Send(node_name, state) for node_name in _SPECIALIST_NODES]


def build_graph(
    checkpointer: BaseCheckpointSaver[str],
) -> CompiledStateGraph[GraphState, None, GraphState, GraphState]:
    """Build and compile the M4 orchestrator graph, backed by ``checkpointer``.

    ``checkpointer`` is required (not optional / defaulted to ``None``) on
    purpose: compiling a LangGraph graph without one silently disables
    per-node persistence, which would make ``resume`` impossible and is
    exactly the failure mode this milestone's DONE.html gate calls out by
    name. See ``backend.orchestrator.langgraph_engine`` for which concrete
    checkpointer this project actually uses, and why.
    """
    builder = StateGraph(GraphState)

    builder.add_node("security", security_node)
    builder.add_node("quality", quality_node)
    builder.add_node("tests", tests_node)
    builder.add_node("docs", docs_node)
    builder.add_node("aggregate", aggregate_node)

    builder.add_conditional_edges(START, _dispatch, _SPECIALIST_NODES)
    for node_name in _SPECIALIST_NODES:
        builder.add_edge(node_name, "aggregate")
    builder.add_edge("aggregate", END)

    return builder.compile(checkpointer=checkpointer)
