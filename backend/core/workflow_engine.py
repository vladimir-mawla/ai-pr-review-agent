"""Abstract workflow-engine interface (ADR-001).

Owns: the contract every orchestration engine must satisfy, so the rest of the
codebase (and future callers such as the webhook worker) can drive a review
workflow without depending on LangGraph directly. M4 introduces exactly one
implementation of this interface — ``backend.orchestrator.langgraph_engine.
LangGraphWorkflowEngine`` — but the interface itself lives in ``backend.core``
so it stays swappable: a later milestone could implement it with a different
engine entirely without touching any caller written against ``WorkflowEngine``.

Why this lives in ``backend.core`` and not ``backend.orchestrator``: per
ADR-002's inward-only dependency rule (enforced by ``.importlinter``),
``backend.core`` may not import ``backend.orchestrator`` or ``backend.models``
— it is the innermost layer every other layer depends on, never the reverse.
That constraint shapes this file: it cannot reference ``GraphState`` or
``Finding`` by name, so the interface is generic over an opaque state type
(``StateT``) instead. The concrete LangGraph implementation binds that type
parameter to the real ``GraphState`` TypedDict where inward-to-outward
dependencies are allowed (``backend.orchestrator`` -> ``backend.models``).

Three operations, matching the architecture note this module was scoped
against: ``run`` (start a new workflow run under a given thread/run id),
``resume`` (continue a previously started run from its last persisted
checkpoint, without re-executing whatever already completed), and
``get_state`` (read back the current persisted state without mutating it).
"""

from __future__ import annotations

from typing import Protocol, TypeVar

# Bound to no particular shape on purpose: LangGraph's own state schemas are
# typically TypedDicts, but nothing in this interface should require that —
# a future, non-LangGraph implementation might use a dataclass or a Pydantic
# model instead. Keeping StateT unbound keeps this module honest about not
# knowing (or caring) what the concrete state type actually looks like.
StateT = TypeVar("StateT")


class WorkflowEngine(Protocol[StateT]):
    """Abstract interface for running and resuming a checkpointed workflow.

    Every method takes a ``thread_id``: the identifier a checkpointer uses to
    correlate all state for one workflow run (e.g. one PR review). Two calls
    with the same ``thread_id`` operate on the same persisted run.
    """

    def run(self, thread_id: str, initial_state: StateT) -> StateT:
        """Start a new workflow run under ``thread_id`` and run it to completion.

        Implementations must persist state as the run progresses (not only at
        the end) so that a crash partway through can later be resumed via
        ``resume`` without re-doing whatever already completed. If the run
        raises partway through, implementations should let that exception
        propagate rather than swallow it — the caller (or a supervising
        process) decides whether and when to call ``resume``.
        """
        ...

    def resume(self, thread_id: str) -> StateT:
        """Continue a previously started run from its last persisted checkpoint.

        Must not re-execute work that was already durably recorded as
        complete for ``thread_id`` — that is the entire point of persisting
        checkpoints per node rather than only persisting a final result.
        Raises if no checkpoint exists for ``thread_id``.
        """
        ...

    def get_state(self, thread_id: str) -> StateT | None:
        """Return the current persisted state for ``thread_id``, or ``None``.

        Read-only: never advances or mutates the run. Returns ``None`` if no
        checkpoint has ever been written for this ``thread_id``.
        """
        ...
