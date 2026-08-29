"""GitHub ``pull_request`` webhook payload parsing.

Owns: turning an already-signature-verified, already-JSON-decoded GitHub
webhook body into the ``WebhookEvent`` domain contract from ``backend.models``.
This module only extracts and validates shape; deciding *whether* an event
should be parsed at all (right event type, supported action) is the router's
job, done before this is called, so this module can assume it is looking at
a ``pull_request`` payload.

Why a narrow extraction instead of keeping the whole raw payload: GitHub's
webhook payloads are large and their shape can grow over time. Modeling only
the fields this system actually depends on makes it explicit what our
contract with GitHub really is, and insulates the rest of the system from
upstream schema changes we don't care about.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from backend.models import WebhookEvent

# Per M2's scope: only these pull_request actions trigger a review job.
# Other actions (closed, labeled, review_requested, ...) are acknowledged by
# the router but never reach this parser.
SUPPORTED_ACTIONS: frozenset[str] = frozenset({"opened", "synchronize", "reopened"})


def parse_pull_request_payload(
    payload: dict[str, Any],
    *,
    delivery_id: str,
    received_at: str | None = None,
) -> WebhookEvent:
    """Extract a ``WebhookEvent`` from a raw GitHub ``pull_request`` webhook payload.

    Args:
        payload: The parsed JSON body of a GitHub ``pull_request`` webhook
            event (already verified and JSON-decoded by the caller).
        delivery_id: The ``X-GitHub-Delivery`` header value — the idempotency
            key for this specific delivery attempt.
        received_at: ISO 8601 timestamp to stamp on the event; defaults to
            "now" in UTC when not supplied (tests may pass a fixed value).

    Returns:
        A validated ``WebhookEvent``.

    Raises:
        KeyError: a required field is absent from the payload (malformed
            input from the caller's point of view).
        pydantic.ValidationError: a field is present but has a shape
            ``WebhookEvent`` rejects (e.g. ``head.sha`` is not 40 hex chars).
    """
    action = payload["action"]
    pull_request = payload["pull_request"]
    repository = payload["repository"]

    return WebhookEvent(
        action=action,
        pr_number=pull_request["number"],
        repository_owner=repository["owner"]["login"],
        repository_name=repository["name"],
        head_sha=pull_request["head"]["sha"],
        delivery_id=delivery_id,
        received_at=received_at or datetime.now(UTC).isoformat(),
    )
