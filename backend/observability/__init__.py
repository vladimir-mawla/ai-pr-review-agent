"""observability module.

The events spine (M7): instrumentation and audit logging over the
append-only ``agent_events`` table (``backend.database``). Owns the
emission API (``events.py`` -- one ``emit_*`` function per event type, with
the log-and-continue failure policy documented there), span timing
(``tracing.py``), and trace reconstruction (``audit.py`` -- "what happened
during review X, in order"), plus the shared run-id/repository glue
(``workflow_context.py``) both live call sites
(``backend.webhook_receiver.router`` and ``backend.orchestrator.nodes``)
use.

Per ADR-002, this module follows the inward-only dependency rule:
dependencies point toward backend.core, backend.models, and
backend.database, never outward toward api or orchestrator.
"""

from backend.observability.audit import reconstruct_review_trace
from backend.observability.events import (
    emit_decision,
    emit_decision_async,
    emit_llm_call,
    emit_span_end,
    emit_span_start,
    emit_tool_call,
)
from backend.observability.tracing import (
    TracingConfigurationError,
    assert_tracing_healthy,
    traced_span,
)
from backend.observability.workflow_context import get_event_repository, run_id_for_delivery

__all__ = [
    "emit_span_start",
    "emit_span_end",
    "emit_decision",
    "emit_decision_async",
    "emit_llm_call",
    "emit_tool_call",
    "traced_span",
    "assert_tracing_healthy",
    "TracingConfigurationError",
    "reconstruct_review_trace",
    "get_event_repository",
    "run_id_for_delivery",
]
