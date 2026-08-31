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
run), non-zero only if no ``Review`` could be produced at all.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from uuid import uuid4

from backend.core.workflow_engine import WorkflowEngine
from backend.integrations.github_client import GitHubClient, MockGitHubClient, post_or_queue
from backend.models import Review
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
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
