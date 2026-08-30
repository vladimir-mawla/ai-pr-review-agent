"""Trace reconstruction: the query this milestone's outcome text names.

Owns: ``reconstruct_review_trace``, the one function that answers "what
happened during review X, in order" from ``review_id`` alone -- exactly
PLAN.md's M7 outcome text: "a trace-viewer query reconstructs one review
end-to-end from ``review_id`` alone."
"""

from __future__ import annotations

from backend.database.models import AgentEvent
from backend.database.repository import EventRepository


def reconstruct_review_trace(repository: EventRepository, review_id: str) -> list[AgentEvent]:
    """Every event recorded for ``review_id``, in the order it happened.

    A thin, named wrapper around
    ``EventRepository.fetch_events_for_review`` rather than having callers
    reach into the repository directly -- this is the one function a
    future trace-viewer (dashboard, CLI, notebook) should import, so "how
    do I reconstruct a review" has exactly one answer instead of every
    caller re-deriving the same query independently.
    """
    return repository.fetch_events_for_review(review_id)
