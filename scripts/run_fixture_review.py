"""M7 demo script: run one fixture review through the real orchestrator graph.

Owns: PLAN.md's M7 demo command's middle step -- applying the events-spine
migrations against the ADMIN connection, then running one real
``LangGraphWorkflowEngine`` review under a caller-supplied ``--review-id``,
so the four specialist nodes' ``span.start``/``span.end`` events and the
aggregator's ``decision`` event actually land in ``agent_events``, which
the demo command's final ``psql`` step then reads back.

Not test code: this produces the demo command's evidence, the same role
``scripts/send_signed_webhook.py`` plays for M2's demo command. Not named
in M7's literal freeze-boundary file list, but required for that
milestone's own demo command (which invokes it by this exact path) to run
at all -- disclosed in this milestone's build report, the same way M2's
``scripts/send_signed_webhook.py`` was.
"""

from __future__ import annotations

import argparse
import sys

from backend.core.settings import get_settings
from backend.database.postgres import apply_migrations
from backend.orchestrator.langgraph_engine import LangGraphWorkflowEngine
from backend.orchestrator.state import GraphState

_FIXTURE_PR_NUMBER = 1
_FIXTURE_OWNER = "demo-org"
_FIXTURE_REPO = "demo-repo"
_FIXTURE_HEAD_SHA = "0" * 40


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--review-id",
        required=True,
        help="review_id to tag every event this run emits with (e.g. 'demo-1').",
    )
    args = parser.parse_args()

    settings = get_settings()
    apply_migrations(settings.database_admin_url)

    engine = LangGraphWorkflowEngine()
    try:
        initial_state: GraphState = {
            "review_id": args.review_id,
            "pr_number": _FIXTURE_PR_NUMBER,
            "repository_owner": _FIXTURE_OWNER,
            "repository_name": _FIXTURE_REPO,
            "head_sha": _FIXTURE_HEAD_SHA,
            "findings": [],
            "node_errors": {},
        }
        result = engine.run(thread_id=args.review_id, initial_state=initial_state)
    finally:
        engine.close()

    review = result.get("review")
    if review is None:
        print(f"no Review produced for review_id={args.review_id!r}", file=sys.stderr)
        return 1
    print(
        f"review_id={args.review_id} status={review.status.value} "
        f"overall_confidence={review.overall_confidence}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
