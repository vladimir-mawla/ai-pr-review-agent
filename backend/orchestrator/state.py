"""Typed state that flows through the M4 fan-out / M5 fan-in graph.

Owns: the single ``GraphState`` shape every node in ``backend.orchestrator.
graph`` reads and writes. Built as a ``TypedDict`` (not a Pydantic model)
because LangGraph's ``StateGraph`` is schema-driven off plain mapping types —
a ``TypedDict`` lets LangGraph's built-in Pregel channel machinery apply the
``Annotated[..., reducer]`` merge functions below when multiple parallel
branches write to the same key in the same super-step. Individual field
*values* (e.g. each ``Finding``) still reuse the existing Pydantic contracts
from ``backend.models`` rather than re-inventing them — M4 builds on M1's
domain contracts, it does not replace them.

Why the reducers matter (this is what "state merges correctly from parallel
branches" means concretely): the four specialist nodes in ``backend.
orchestrator.nodes`` run inside the *same* super-step, fanned out via
LangGraph's ``Send`` API. Without a reducer, each node's returned partial
update would simply overwrite the previous one — whichever node finished
last would "win" and the other three's findings/errors would be silently
lost. ``operator.add`` (for the findings list) and ``_merge_dicts`` (for the
per-node error map) tell LangGraph to combine every branch's contribution
instead of clobbering it.

M5 addition: ``review`` and ``routing_reason`` give the real ``aggregate_node``
(``backend.orchestrator.nodes``) somewhere to write its output. Both are
``NotRequired`` rather than plain required keys so every existing M4 caller
that builds a ``GraphState`` without them (e.g. ``tests/integration/
test_orchestrator_fanout.py``'s ``_initial_state``) stays valid under
``mypy --strict`` — the initial state legitimately has no ``Review`` yet;
only the join node, running once (not in parallel), ever writes these two
keys, so neither needs a merge reducer.
"""

from __future__ import annotations

import operator
from typing import Annotated, NotRequired, TypedDict

from backend.models import Finding, Review


def _merge_dicts(left: dict[str, str], right: dict[str, str]) -> dict[str, str]:
    """Reducer for ``node_errors``: union two branches' error maps.

    Each specialist node contributes at most one entry (keyed by its own node
    name), so a plain shallow merge is sufficient — no two parallel branches
    ever write the same key, which means there is no ordering ambiguity to
    resolve here.
    """
    return {**left, **right}


class GraphState(TypedDict):
    """State shared by every node in the M4 orchestrator graph.

    Attributes:
        review_id: Correlates this graph run with the PR review it belongs
            to (same shape as ``backend.models.Review.review_id``). M4 does
            not construct a ``Review`` itself — that is M5's aggregator — but
            carries the id through so a future node can.
        pr_number: GitHub PR number, carried through for attribution.
        repository_owner: Repository owner, carried through for attribution.
        repository_name: Repository name, carried through for attribution.
        head_sha: Commit SHA under review, carried through for attribution.
        findings: Every ``Finding`` contributed by every specialist node that
            ran successfully, accumulated via ``operator.add`` so parallel
            branches merge instead of overwrite. Order across branches is not
            guaranteed — M5's aggregator, not M4, owns sorting/deduplication.
        node_errors: Maps a specialist node's name to the error message it
            raised, for any node whose stub work failed but was isolated
            rather than allowed to fail the whole run (see
            ``backend.orchestrator.nodes.AgentExecutionError``). Empty when
            every node succeeded.
        review: The aggregated ``Review`` the M5 join node
            (``aggregate_node``) builds from the merged, deduplicated
            findings — absent until that node runs, since it is the only
            writer. ``NotRequired`` so pre-M5 callers building an initial
            state need not supply it.
        routing_reason: The human-readable explanation
            ``backend.hitl.queue.route_review`` produced for ``review``'s
            status (why it auto-posted or was queued for human review).
            Kept alongside ``review`` rather than folded into one of its
            fields because it is diagnostic output about the *routing
            decision*, not a property of the review itself.
    """

    review_id: str
    pr_number: int
    repository_owner: str
    repository_name: str
    head_sha: str
    findings: Annotated[list[Finding], operator.add]
    node_errors: Annotated[dict[str, str], _merge_dicts]
    review: NotRequired[Review]
    routing_reason: NotRequired[str]
