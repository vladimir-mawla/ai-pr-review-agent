"""M10's outcome, made runnable: PLAN.md's named M10 demo command.

Owns: ``python -m backend.cli.review_local --diff <patch> --out out/review.json``
-- the first genuine end-to-end run of the whole cognitive pipeline this
project has built across M1-M10: a real fixture diff flows through all four
real, LLM-backed specialists (SECURITY, QUALITY, TESTS, DOCS -- fanned out
in parallel by the same compiled LangGraph graph M4 built and M5/M8
extended), each one grounded with retrieved repository context (M9's
``HybridRetriever``, wired in at M10 -- see
``backend.agents.base_agent``'s module docstring), aggregated and deduped
(M5), routed through the HITL confidence gate (M5), with GitHub posting
mocked out (``backend.integrations.github_client.MockGitHubClient`` --
this milestone's own explicit scope: prove the pipeline before touching a
real repository, which is M11's job) -- and writes one structured
``Review`` as JSON to ``--out``.

PREREQUISITES this script does NOT set up for you (operational, not code):
``ANTHROPIC_API_KEY`` must be configured (four real LLM calls happen here --
this is PLAN.md's own M10 credentials line) and, for retrieval grounding to
actually surface anything, the ``code_chunks`` corpus should already be
seeded (``scripts/seed_code_chunks.py`` -- see that script's own docstring
and this project's documented "code_chunks is empty after any pgvector
container recreate" trap). Neither is a hard requirement to RUN this script
without crashing: a missing API key surfaces as each specialist's own
forced-HITL infrastructure-failure fallback (see
``backend.orchestrator.nodes``'s module docstring) rather than a crash, and
an empty/unreachable retrieval corpus degrades to "no retrieved context",
not a failure (see ``backend.agents.base_agent.build_user_message``'s
docstring) -- but a real, useful demo run needs both.

EXIT CODE: 0 if a ``Review`` was produced and written (regardless of
whether it was POSTED or QUEUED_FOR_HITL -- both are a successful pipeline
run), non-zero only if no ``Review`` could be produced at all, or if
``--verify-tracing`` was passed and tracing could not be verified (see
below -- that check runs BEFORE any LLM call, so this exit path never
follows a real, billed review).

``--verify-tracing`` -- CLOSING THE ACTUAL DEMO-DAY GAP
--------------------------------------------------------
This CLI is THE path used for demos, and it is where
``backend.observability.tracing``'s silent-failure guard actually matters
in practice: a misconfigured ``LANGSMITH_ENDPOINT``/``LANGSMITH_WORKSPACE_ID``
makes LangSmith's ingestion swallow the error by design (see that module's
docstring), so this script prints a perfectly successful review -- findings,
exit 0 -- while producing ZERO traces, with nothing in this script's own
output hinting anything is wrong. An operator would demo it, watch it
"work," and only discover the problem later staring at an empty LangSmith
project with no explanation.

``assert_tracing_healthy`` (``backend.observability.tracing``) is exactly
the check that catches this -- but it is NOT unconditionally wired into
``run_review_locally``/``main`` here, for the same reason it is not wired
into the FastAPI app's lifespan (see that module's docstring): this
script's own existing tests
(``tests/e2e/test_full_local_review.py::TestCliMainWritesTheDemoCommandsOutputFile``)
drive the real ``main()`` with unmodified, ``.env``-backed ``Settings``,
so unconditionally running a LangSmith network call there would make that
currently-free test hit the network the moment ``.env`` has
``LANGSMITH_TRACING=true`` -- exactly the hazard that kept this guard out
of ``review_local`` in the first place.

The fix is an explicit, OPT-IN flag: ``--verify-tracing``. Off by default,
so a plain ``review_local`` invocation (and every existing test that
drives it) behaves exactly as before -- zero LangSmith calls, regardless
of what ``.env`` has ``LANGSMITH_TRACING`` set to. When passed,
``verify_tracing_before_review`` runs ``assert_tracing_healthy`` -- and
nothing else, no second implementation -- BEFORE ``run_review_locally`` is
ever called, i.e. before any of the four real Anthropic calls: failing
loudly on a broken LangSmith endpoint before spending money on a review
that would have looked successful anyway is strictly better than failing
after. On success, it also prints the LangSmith project URL (via
``backend.observability.tracing.resolve_project_url``) so an operator can
click straight through to confirm the trace landed -- the thing they
actually want at demo time.

Separately: when ``LANGSMITH_TRACING=true`` but ``--verify-tracing`` was
NOT passed, this script prints an explicit, hard-to-miss warning to stdout
naming the flag. This is judged worth the noise (rather than silent, which
is the exact failure mode this whole fix exists to close) because the
alternative -- a clean exit-0 review with no indication tracing was never
checked -- is precisely how the real incident that prompted this fix went
undetected. The warning never makes a network call itself; it only reads
``Settings.langsmith_tracing``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from uuid import uuid4

from langsmith import Client

from backend.core.settings import Settings, get_settings
from backend.core.workflow_engine import WorkflowEngine
from backend.integrations.github_client import GitHubClient, MockGitHubClient, post_or_queue
from backend.models import Review
from backend.observability.tracing import (
    TracingConfigurationError,
    assert_tracing_healthy,
    resolve_project_url,
)
from backend.orchestrator.langgraph_engine import LangGraphWorkflowEngine
from backend.orchestrator.state import GraphState

# Synthetic PR identity for a local, no-real-repository dry run -- there is
# no actual GitHub PR behind this invocation (that is M11's scope), so
# these are fixed, documented placeholders rather than values a caller
# needs to supply for a simple demo run. Overridable via CLI flags below
# for a caller that wants a specific identity in the output JSON.
_DEFAULT_PR_NUMBER = 1
_DEFAULT_REPOSITORY_OWNER = "local"
_DEFAULT_REPOSITORY_NAME = "review"
_DEFAULT_HEAD_SHA = "0" * 40


class NoReviewProducedError(Exception):
    """Raised when the orchestrator run completed but wrote no ``Review``.

    There is no currently-known way for this to happen in production (the
    aggregator node always writes ``GraphState['review']``), but
    ``run_review_locally`` does not assume that holds forever -- surfacing
    a clear, named error here is better than a caller hitting a bare
    ``KeyError``/``AttributeError`` deep in JSON-writing code.
    """


def _read_diff(path: str | Path) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


_UNVERIFIED_TRACING_WARNING = (
    "WARNING: LANGSMITH_TRACING is enabled but tracing was NOT verified this "
    "run (pass --verify-tracing to confirm traces are actually landing in "
    "LangSmith). LangSmith's ingestion can swallow a misconfigured "
    "LANGSMITH_ENDPOINT/LANGSMITH_WORKSPACE_ID with ZERO application errors "
    "-- a successful-looking review below is not proof any trace exists."
)


def verify_tracing_before_review(
    *,
    verify_tracing: bool,
    settings: Settings | None = None,
    client: Client | None = None,
) -> None:
    """The whole ``--verify-tracing`` contract, called from ``main`` before ``run_review_locally``.

    Deliberately a separate, standalone call -- not folded into
    ``run_review_locally`` -- so it can run, and be unit-tested, entirely
    independently of the orchestrator/LLM pipeline: this function alone
    never runs the graph, never needs a diff, and (when ``verify_tracing``
    is ``False``, the default) never even imports/constructs a LangSmith
    ``Client``. That last property is exactly what makes the default,
    free `pytest` suite safe -- see
    ``tests/unit/test_review_local_tracing.py``.

    Four cases, matching this module's own docstring:

    1. ``verify_tracing=False`` (the CLI default) and tracing disabled:
       complete no-op, nothing printed, nothing called.
    2. ``verify_tracing=False`` and tracing enabled: no LangSmith call at
       all (``client`` is never touched), but prints
       ``_UNVERIFIED_TRACING_WARNING`` to stdout -- the "operator scanning
       stdout would notice" signal called for by this fix's own design
       requirements.
    3. ``verify_tracing=True`` and tracing disabled: delegates to
       ``assert_tracing_healthy``, which itself no-ops when
       ``settings.langsmith_tracing`` is ``False`` -- clean no-op, no
       error, no LangSmith config required.
    4. ``verify_tracing=True`` and tracing enabled: runs
       ``assert_tracing_healthy`` for real (the SAME implementation
       ``backend.job_queue.arq_worker`` and the standalone
       ``python -m backend.observability.tracing`` CLI use -- no second
       implementation here). Raises ``TracingConfigurationError`` on
       failure; on success, also prints the LangSmith project URL (via
       ``resolve_project_url``) so an operator can click straight through.

    Args:
        verify_tracing: The CLI's ``--verify-tracing`` flag, passed
            through verbatim.
        settings: Defaults to ``get_settings()`` (the process-wide,
            ``.env``-backed singleton) when omitted -- a test passes its
            own ``Settings(...)`` instead, so this function's behavior is
            deterministic regardless of what a real ``.env`` happens to
            contain.
        client: Test-only injection point, passed straight through to
            ``assert_tracing_healthy``/``resolve_project_url`` -- a test
            can inject a fake/broken ``Client`` double (see
            ``tests/unit/test_tracing_guard.py``'s own doubles) to prove
            this function's control flow without a real network call.
            Production call sites (``main`` below) never pass this.

    Raises:
        TracingConfigurationError: ``verify_tracing`` is ``True``, tracing
            is enabled, and the probe run could not be verified to have
            landed.
    """
    resolved_settings = settings if settings is not None else get_settings()

    if not verify_tracing:
        if resolved_settings.langsmith_tracing:
            print(_UNVERIFIED_TRACING_WARNING)
        return

    if not resolved_settings.langsmith_tracing:
        # assert_tracing_healthy would no-op here anyway; returning early
        # ourselves keeps this path from ever touching `client`, matching
        # "must not require any LangSmith config" when tracing is off.
        return

    print("Verifying LangSmith tracing is landing (writing + reading back a probe run)...")
    assert_tracing_healthy(resolved_settings, client=client)
    print("LangSmith tracing verified healthy.")

    project_url = resolve_project_url(resolved_settings, client=client)
    if project_url:
        print(f"LangSmith project: {project_url}")


def run_review_locally(
    diff: str,
    *,
    review_id: str | None = None,
    pr_number: int = _DEFAULT_PR_NUMBER,
    repository_owner: str = _DEFAULT_REPOSITORY_OWNER,
    repository_name: str = _DEFAULT_REPOSITORY_NAME,
    head_sha: str = _DEFAULT_HEAD_SHA,
    github_client: GitHubClient | None = None,
    engine: WorkflowEngine[GraphState] | None = None,
) -> Review:
    """Run one diff through the full pipeline and return the resulting ``Review``.

    The testable core of this module: ``main`` below is a thin CLI wrapper
    around this function (parse args, read the diff file, write the result
    to disk). Exposed here, separately, so a test can inject its own
    ``github_client`` (to inspect exactly which of ``post_review_comment``/
    ``queue_for_hitl`` was called -- PLAN.md's own M10 success criterion)
    and its own ``engine`` (an isolated, ``tmp_path``-backed
    ``LangGraphWorkflowEngine``, or a fake, rather than this process's
    shared default checkpoint database) without needing to shell out to a
    subprocess or monkeypatch module globals.

    Args:
        diff: The unified diff text to review (already read from disk, or
            constructed directly by a caller/test).
        review_id: Defaults to a fresh ``"local-<uuid4>"`` when omitted.
        github_client: Defaults to a fresh ``MockGitHubClient()`` -- GitHub
            posting is mocked out at this milestone regardless of what is
            injected here (a real implementation is M11's scope); this
            parameter exists for test inspection, not to allow a real post.
        engine: Defaults to a fresh ``LangGraphWorkflowEngine()`` (this
            process's shared on-disk checkpoint database), closed before
            this function returns. A caller-supplied ``engine`` is NOT
            closed here -- the caller owns its lifecycle in that case,
            mirroring how ``scripts/run_fixture_review.py`` only closes an
            engine it constructed itself.

    Raises:
        ``NoReviewProducedError``: the orchestrator run completed but wrote
            no ``Review`` into the final state.
    """
    resolved_review_id = review_id or f"local-{uuid4()}"
    resolved_github_client: GitHubClient = (
        github_client if github_client is not None else MockGitHubClient()
    )

    owns_engine = engine is None
    resolved_engine: WorkflowEngine[GraphState] = engine if engine is not None else LangGraphWorkflowEngine()
    try:
        initial_state: GraphState = {
            "review_id": resolved_review_id,
            "pr_number": pr_number,
            "repository_owner": repository_owner,
            "repository_name": repository_name,
            "head_sha": head_sha,
            "findings": [],
            "node_errors": {},
            "diff": diff,
        }
        result = resolved_engine.run(thread_id=resolved_review_id, initial_state=initial_state)
    finally:
        if owns_engine:
            close = getattr(resolved_engine, "close", None)
            if callable(close):
                close()

    review = result.get("review")
    if review is None:
        raise NoReviewProducedError(f"no Review produced for review_id={resolved_review_id!r}")

    # Exactly one of post_review_comment/queue_for_hitl -- see
    # backend.integrations.github_client.post_or_queue's docstring for why
    # this is a shared dispatcher, not an inline if/else here.
    post_or_queue(resolved_github_client, review)
    return review


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--diff",
        required=True,
        help="Path to a unified diff (patch) file to review.",
    )
    parser.add_argument(
        "--out",
        required=True,
        help="Path to write the resulting Review JSON to (parent directories are created).",
    )
    parser.add_argument(
        "--review-id",
        default=None,
        help="review_id to tag this run with. Defaults to a fresh 'local-<uuid4>'.",
    )
    parser.add_argument("--pr-number", type=int, default=_DEFAULT_PR_NUMBER)
    parser.add_argument("--repository-owner", default=_DEFAULT_REPOSITORY_OWNER)
    parser.add_argument("--repository-name", default=_DEFAULT_REPOSITORY_NAME)
    parser.add_argument("--head-sha", default=_DEFAULT_HEAD_SHA)
    parser.add_argument(
        "--verify-tracing",
        action="store_true",
        default=False,
        help=(
            "Before running the review, actively verify LangSmith tracing is "
            "landing (writes a probe run, flushes, reads it back by id) and "
            "fail loudly -- before any LLM call -- if it did not. Off by "
            "default, so a plain review_local run makes zero LangSmith calls "
            "regardless of LANGSMITH_TRACING. A no-op if LANGSMITH_TRACING is "
            "not enabled."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)

    try:
        verify_tracing_before_review(verify_tracing=args.verify_tracing)
    except TracingConfigurationError as exc:
        print(f"tracing verification FAILED: {exc}", file=sys.stderr)
        return 1

    diff = _read_diff(args.diff)

    try:
        review = run_review_locally(
            diff,
            review_id=args.review_id,
            pr_number=args.pr_number,
            repository_owner=args.repository_owner,
            repository_name=args.repository_name,
            head_sha=args.head_sha,
        )
    except NoReviewProducedError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(review.model_dump_json(indent=2), encoding="utf-8")

    print(
        f"review_id={review.review_id} status={review.status.value} "
        f"overall_confidence={review.overall_confidence} "
        f"findings={len(review.findings)} wrote={out_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
