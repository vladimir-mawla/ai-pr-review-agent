"""Span-timing helper built on ``backend.observability.events``, plus the LangSmith tracing guard.

Owns two independent things:

1. ``traced_span`` (pre-existing): a context manager that emits
   ``span.start`` on entry and ``span.end`` (with measured ``latency_ms``
   and an "ok"/"error" ``outcome``) on exit -- the shape every orchestrator
   specialist node wraps its work in (see ``backend.orchestrator.nodes``).
   This is this project's OWN append-only events spine (``agent_events``),
   unrelated to LangSmith.

2. ``assert_tracing_healthy`` (new): the LangSmith silent-failure guard.
   LangSmith's ingestion is designed to swallow tracing errors -- a
   misconfigured endpoint/key/workspace id produces ZERO traces and ZERO
   application errors, because the SDK's default ``auto_batch_tracing``
   path ships run data to ``POST /runs/multipart`` on a background
   thread/queue, and a failed POST there does not propagate back to
   whatever code called ``client.flush()`` (verified empirically against
   this project's own real, AWS-deployment LangSmith account: a request
   signed with the wrong workspace id 403s on every call, yet
   ``flush()`` still returns cleanly and no exception reaches application
   code). A team that "wires up tracing" and then trusts an empty
   dashboard's silence would have no way to tell "no findings to report"
   apart from "traces never arrived at all" -- exactly the class of
   silent failure this project's own review process has repeatedly
   rejected (see ``backend/observability/events.py``'s own
   log-and-continue-vs-fail-loudly policy for the same principle applied
   to this project's Postgres events spine).

   ``assert_tracing_healthy`` does not trust ``flush()``'s silence: it
   writes one real, uniquely-named probe run, flushes, and then actively
   reads that exact run back BY ID with a short retry budget (LangSmith's
   ingestion is asynchronous even on success -- a run is not always
   immediately readable the instant ``flush()`` returns; see this
   project's own build notes for the observed ~1.5-3s landing latency
   against the real API). Only a successful read-back counts as "tracing
   is healthy" -- anything else (an exception during create/update/flush,
   or every read-back attempt failing) raises ``TracingConfigurationError``
   with a diagnostic message naming the likely cause, never merely logs
   and continues, because there is no safe "degraded" mode for a
   verification check whose entire job is to fail loudly.

   WHERE THIS RUNS, AND WHERE IT DELIBERATELY DOES NOT
   -----------------------------------------------------
   This project has three candidate "at startup" hooks: the webhook
   FastAPI app (``backend.api.main``), the ARQ worker process
   (``backend.job_queue.arq_worker``), and the M10 CLI
   (``backend.cli.review_local``). Only ONE of those actually executes
   the LangGraph orchestrator graph that LangSmith would trace, and it is
   not the FastAPI app -- ``backend.webhook_receiver.router`` only
   verifies + enqueues; it never calls ``graph.invoke``. Wiring this guard
   into the FastAPI app's lifespan would therefore check a process that
   never needs tracing, while creating a real hazard: ``backend.core.
   settings.Settings`` reads ``.env`` by default, several existing tests
   (e.g. ``tests/unit/test_webhook_validator.py``) build a ``Settings(...)``
   overriding only ``github_webhook_secret`` and then drive it through
   ``with TestClient(app) as client:`` -- which DOES execute FastAPI
   lifespan events -- so a lifespan-wired guard would run for real,
   unrelated unit tests any time ``.env`` has ``LANGSMITH_TRACING=true``
   (which this project's own ``.env`` does, once configured), violating
   this project's hard requirement that the free ``pytest`` suite make
   ZERO LangSmith network calls. The ARQ worker and the CLI do not have
   that hazard (no test drives either's real startup path with
   unmodified ``Settings``; see ``tests/integration/test_queue_to_orchestrator.py``
   /``test_queue_roundtrip.py``, which construct a bare ``arq.worker.Worker``
   directly rather than going through ``backend.job_queue.arq_worker.
   WorkerSettings``, and ``tests/e2e/test_full_local_review.py``, which
   drives ``backend.cli.review_local.main`` but never invokes this
   module's CLI entry point). So this guard is wired at TWO places instead:

   - ``backend.job_queue.arq_worker.WorkerSettings.on_startup`` -- the real
     production path that actually runs the graph unattended, gated on
     ``settings.langsmith_tracing`` exactly like every other opt-in
     backend flag in this project.
   - A standalone CLI check, ``python -m backend.observability.tracing``
     (see ``main`` below) -- for an operator (or this milestone's own
     Task 4 real-trace verification) to run explicitly before/alongside
     the M10 demo command, without coupling it to ``review_local.main``'s
     own already-tested startup path.
   - An OPT-IN ``--verify-tracing`` flag on ``backend.cli.review_local``
     itself (``review_local.verify_tracing_before_review``, called from
     ``main`` before ``run_review_locally`` -- i.e. before any of the
     four real LLM calls). This closes the actual demo-day gap: an
     operator can run ``review_local`` with ``LANGSMITH_TRACING=true`` and
     a broken endpoint/workspace id, get a perfectly successful-looking
     review (findings, exit 0), and see zero traces with zero indication
     anything was wrong -- LangSmith's ingestion swallows that class of
     failure by design (see above). ``--verify-tracing`` is UNCONDITIONALLY
     off by default (bare ``review_local`` never imports/constructs a
     ``Client`` for this at all unless the flag is passed), which is
     exactly why this is safe to add: it does not touch
     ``TestCliMainWritesTheDemoCommandsOutputFile``'s existing, currently-
     free assertion that plain ``main()`` makes no network call, because
     that test never passes ``--verify-tracing``.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import cast
from uuid import uuid4

from langsmith import Client

from backend.core.settings import Settings
from backend.database.repository import EventRepository
from backend.observability.events import emit_span_end, emit_span_start

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 1. traced_span -- this project's own agent_events span timing. Unchanged.
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# 2. assert_tracing_healthy -- the LangSmith silent-failure guard. See
# module docstring for the full defect this closes and where it does/does
# not run.
# ---------------------------------------------------------------------------

_PROBE_RUN_NAME = "pr-review-agent-tracing-startup-probe"

# LangSmith's ingestion is asynchronous even on a genuinely successful
# write: empirically (against this project's own real, AWS-deployment
# LangSmith account), a probe run was NOT yet readable by id ~1.5s after a
# clean flush(), but WAS readable ~3s after. This budget (6 attempts *
# 1.5s = up to 9s of extra startup latency, worst case) is generous enough
# to absorb that observed latency with real margin, while still bounded --
# a startup check that could hang indefinitely would itself become an
# availability problem.
_PROBE_READBACK_ATTEMPTS = 6
_PROBE_READBACK_DELAY_SECONDS = 1.5

# Defensively strips anything that looks like a LangSmith key from a
# diagnostic string before it is ever raised/logged. Belt-and-braces: no
# LangSmith SDK exception observed during this integration's own testing
# has ever included key material in its message (HTTP error bodies/status
# lines do not echo the Authorization header back), but this guard exists
# so a future SDK version doing something different can never turn this
# check into the thing that leaks a credential into a log line.
_KEY_PATTERN = re.compile(r"lsv2_(?:sk|pt)_[A-Za-z0-9_]+")


class TracingConfigurationError(RuntimeError):
    """Raised by ``assert_tracing_healthy`` when LangSmith tracing is enabled but unverifiable.

    Deliberately a distinct, named exception type (not a bare
    ``RuntimeError`` or a re-raise of whatever the LangSmith SDK itself
    raised) so a caller -- or an operator reading a startup crash -- can
    tell at a glance "tracing is misconfigured" apart from any other
    startup failure, and so ``pytest`` can assert on it precisely (see
    ``tests/unit/test_tracing_guard.py``).
    """


def _redact(text: str) -> str:
    return _KEY_PATTERN.sub("<redacted>", text)


def _build_client(settings: Settings) -> Client:
    """Construct a ``langsmith.Client`` from ``settings`` explicitly.

    Deliberately does NOT rely on the LangSmith SDK's own default
    behavior of reading ``LANGSMITH_API_KEY``/``LANGSMITH_ENDPOINT``/
    ``LANGSMITH_WORKSPACE_ID`` from the ambient process environment --
    passing every value through from ``settings`` explicitly is what
    makes this function (and therefore ``assert_tracing_healthy``)
    deterministic and unit-testable: a test can construct a ``Settings``
    with a deliberately wrong ``langsmith_endpoint``/``langsmith_workspace_id``
    and prove the guard detects it, with no global environment mutation
    required.
    """
    return Client(
        api_url=settings.langsmith_endpoint,
        api_key=settings.langsmith_api_key,
        workspace_id=settings.langsmith_workspace_id,
    )


def _diagnose(exc: Exception | None, settings: Settings, *, readback_failed: bool) -> str:
    """Build a clear, credential-free diagnostic naming the likely cause.

    This is the "fail loudly, name the likely cause" half of the guard's
    contract -- an operator who mistypes the workspace id must see
    something actionable, not a bare stack trace from deep inside the
    LangSmith SDK.
    """
    detail = _redact(str(exc)) if exc is not None else "no exception was raised"
    hints: list[str] = []

    forbidden = exc is not None and ("403" in str(exc) or "Forbidden" in str(exc))
    if forbidden:
        hints.append(
            "a 403/Forbidden response on every call is this org's known "
            "signature for EITHER a missing/incorrect LANGSMITH_WORKSPACE_ID "
            "(this project's LangSmith account is on the AWS deployment, "
            "where a service-account key 403s on every endpoint without an "
            "explicit workspace id -- see .env.example) OR an invalid/"
            "revoked API key."
        )
    if settings.langsmith_endpoint and "aws.api.smith.langchain.com" not in settings.langsmith_endpoint:
        hints.append(
            f"LANGSMITH_ENDPOINT is currently {settings.langsmith_endpoint!r} -- "
            "if this org's LangSmith account lives on the AWS deployment, it "
            "must be https://aws.api.smith.langchain.com, not the SDK's "
            "default api.smith.langchain.com; using the wrong region 403s "
            "identically to a bad key."
        )
    if not settings.langsmith_workspace_id:
        hints.append(
            "LANGSMITH_WORKSPACE_ID is unset. On this project's AWS-deployment "
            "LangSmith org, a service-account key 403s on every endpoint "
            "without it, even though the key itself is valid -- LangSmith's "
            "own quickstart snippet omits this variable."
        )
    if readback_failed and not hints:
        hints.append(
            f"the probe run could not be read back by id after "
            f"{_PROBE_READBACK_ATTEMPTS} attempts over "
            f"{_PROBE_READBACK_ATTEMPTS * _PROBE_READBACK_DELAY_SECONDS:.1f}s. "
            "LangSmith's ingestion swallows this class of failure by design "
            "(a failed POST /runs/multipart still lets client.flush() return "
            "cleanly), which is exactly why this check reads the run back "
            "instead of trusting flush()."
        )

    hint_text = " ".join(hints) if hints else "no further diagnostic signal is available."
    return (
        "LangSmith tracing is enabled (LANGSMITH_TRACING=true) but the "
        f"startup probe run did not verifiably land in LangSmith. {hint_text} "
        f"Underlying error: {detail}"
    )


def assert_tracing_healthy(settings: Settings, *, client: Client | None = None) -> None:
    """Actively verify LangSmith tracing is landing, or raise loudly.

    A no-op when ``settings.langsmith_tracing`` is ``False`` -- this is
    the "must not run, and must not be required, when tracing is
    disabled" half of this guard's contract. When tracing IS enabled,
    this writes one uniquely-named probe run, updates it, flushes, and
    then reads it back by id with a short retry budget (see
    ``_PROBE_READBACK_ATTEMPTS``/``_PROBE_READBACK_DELAY_SECONDS`` for
    why a retry budget is necessary even on success). Only a successful
    read-back is treated as "healthy" -- every other outcome (an
    exception during create/update/flush, or every read-back attempt
    failing) raises ``TracingConfigurationError`` with a diagnostic
    message. Never logs or raises anything containing key material (see
    ``_redact``).

    Args:
        settings: Supplies ``langsmith_tracing`` (the opt-in gate) and the
            LangSmith connection details (``langsmith_endpoint``/
            ``langsmith_api_key``/``langsmith_workspace_id``/
            ``langsmith_project``) the probe run is written through.
        client: Test-only injection point -- a caller (this project's own
            ``tests/unit/test_tracing_guard.py``) can pass a fake/broken
            ``Client`` double directly, without needing a real network
            call, to prove this function's control flow (raise on
            create/flush failure, raise on read-back failure, no-op when
            disabled) without depending on LangSmith's actual API being
            reachable. Production call sites never pass this -- a real
            ``Client`` is built from ``settings`` via ``_build_client``.

    Raises:
        TracingConfigurationError: tracing is enabled but the probe run
            could not be verified to have landed.
    """
    if not settings.langsmith_tracing:
        return

    resolved_client = client if client is not None else _build_client(settings)
    run_id = uuid4()

    try:
        resolved_client.create_run(
            name=_PROBE_RUN_NAME,
            inputs={"probe": True},
            run_type="chain",
            id=run_id,
            project_name=settings.langsmith_project,
            tags=["tracing-startup-probe"],
        )
        resolved_client.update_run(run_id, outputs={"probe": "ok"})
        resolved_client.flush()
    except Exception as exc:  # noqa: BLE001 -- deliberately broad, see module docstring
        message = _diagnose(exc, settings, readback_failed=False)
        logger.error(message)
        raise TracingConfigurationError(message) from exc

    last_exc: Exception | None = None
    for _ in range(_PROBE_READBACK_ATTEMPTS):
        time.sleep(_PROBE_READBACK_DELAY_SECONDS)
        try:
            # `read_run` is deprecated in favor of `client.runs.retrieve()`
            # (langsmith 0.11.2 warns but keeps it working until Jan 2027) --
            # kept deliberately for this single call site's simplicity; it
            # is not in this project's hot path, so the migration can wait
            # for a dedicated pass rather than riding along with this one.
            resolved_client.read_run(run_id)
            logger.info(
                "LangSmith tracing verified healthy (probe run %s read back successfully).",
                run_id,
            )
            return
        except Exception as exc:  # noqa: BLE001 -- see module docstring
            last_exc = exc

    message = _diagnose(last_exc, settings, readback_failed=True)
    logger.error(message)
    raise TracingConfigurationError(message)


def resolve_project_url(settings: Settings, *, client: Client | None = None) -> str | None:
    """Best-effort: the LangSmith project URL an operator can click straight through to.

    Used by ``backend.cli.review_local``'s ``--verify-tracing`` flag right
    after a successful ``assert_tracing_healthy`` call, so a demo run that
    just proved tracing is landing also hands the operator the exact
    project to look at -- "genuinely the thing they want at demo time"
    (this project's own M10-follow-up notes), rather than making them go
    guess the project name/workspace in the LangSmith UI.

    Deliberately returns ``None`` instead of raising when the project
    cannot be resolved (e.g. the SDK's ``read_project`` call itself fails
    for an unrelated reason) -- this is a convenience lookup that runs
    strictly AFTER ``assert_tracing_healthy`` has already confirmed
    tracing is healthy; a caller whose run already succeeded should not
    have that success turned into a failure merely because a follow-up
    "resolve the pretty URL" call hit its own, unrelated hiccup. Never
    raises, and (per this module's redaction policy) never includes key
    material -- the LangSmith SDK's project/session objects carry no
    credential fields at all, so there is nothing to redact here.

    Args:
        settings: Supplies the LangSmith connection details and
            ``langsmith_project`` (the project name to resolve).
        client: Test-only injection point, mirroring
            ``assert_tracing_healthy``'s own ``client`` parameter -- a
            caller that already built/was given a ``Client`` for the
            health check can reuse it here instead of constructing a
            second one. Production call sites pass the same client the
            preceding ``assert_tracing_healthy`` call used.

    Returns:
        The project's web URL, or ``None`` if it could not be resolved.
    """
    resolved_client = client if client is not None else _build_client(settings)
    try:
        project = resolved_client.read_project(project_name=settings.langsmith_project)
    except Exception:  # noqa: BLE001 -- best-effort lookup, see docstring
        return None
    # The LangSmith SDK ships no inline type information for
    # `read_project`'s return value from mypy's point of view, so `.url`
    # resolves to `Any` -- this cast states the actual, documented type
    # (`TracerSessionResult.url: Optional[str]`) rather than letting `Any`
    # silently propagate out of this function's own declared signature.
    return cast("str | None", project.url)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: ``python -m backend.observability.tracing``.

    Prints (never raises past this function -- a CLI check reports via
    exit code, it does not crash with a traceback) a one-line result and
    returns 0 if tracing is disabled (a deliberate no-op) or verified
    healthy, 1 if tracing is enabled but unverifiable. See this module's
    docstring for why this CLI entry point exists as an explicit,
    separately-invoked check rather than being wired into ``backend.cli.
    review_local.main``'s own startup.
    """
    del argv  # no arguments today; accepted for a consistent CLI signature
    settings = Settings()  # type: ignore[call-arg]  # see backend.core.settings.get_settings
    if not settings.langsmith_tracing:
        print("LANGSMITH_TRACING is not enabled -- nothing to check (this is a no-op, not a failure).")
        return 0
    try:
        assert_tracing_healthy(settings)
    except TracingConfigurationError as exc:
        print(f"LangSmith tracing check FAILED: {exc}")
        return 1
    print(
        f"LangSmith tracing check OK -- probe run landed in project "
        f"{settings.langsmith_project!r} at {settings.langsmith_endpoint or '(SDK default endpoint)'}."
    )
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
