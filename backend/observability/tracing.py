"""Span-timing helper built on ``backend.observability.events``.

Owns: ``traced_span``, a context manager that emits ``span.start`` on entry
and ``span.end`` (with measured ``latency_ms`` and an "ok"/"error"
``outcome``) on exit -- the shape every orchestrator specialist node wraps
its work in (see ``backend.orchestrator.nodes``).
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager

from backend.database.repository import EventRepository
from backend.observability.events import emit_span_end, emit_span_start


@contextmanager
def traced_span(repository: EventRepository, review_id: str, agent: str) -> Iterator[None]:
    """Emit ``span.start`` now, then ``span.end`` (latency + outcome) when the block exits.

    ``outcome`` is ``"error"`` if an exception propagates out of the
    wrapped block, ``"ok"`` otherwise. Any exception is re-raised unchanged
    after the ``span.end`` event is recorded -- this context manager only
    *observes* failures, it never suppresses them. Suppressing one here
    would silently turn a real specialist failure into a "successful" run
    from the orchestrator's point of view, which would directly undermine
    ``backend.orchestrator.nodes.SimulatedNodeCrashError``'s existing
    contract (it must propagate all the way out of the node, uncaught, for
    M4's checkpoint-resume behavior to still work) -- this wrapper must not
    interfere with that.
    """
    emit_span_start(repository, review_id, agent)
    start = time.monotonic()
    outcome = "ok"
    try:
        yield
    except BaseException:
        outcome = "error"
        raise
    finally:
        latency_ms = int((time.monotonic() - start) * 1000)
        emit_span_end(repository, review_id, agent, latency_ms=latency_ms, outcome=outcome)
