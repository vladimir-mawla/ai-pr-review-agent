"""The GitHub webhook HTTP route.

Owns: wiring the three things M2 exists to do, in order, and nothing else:

    1. Verify the HMAC-SHA256 signature over the RAW request body — reject
       forgeries before any other work happens.
    2. Check the ``X-GitHub-Delivery`` idempotency key so a retried delivery
       is not processed twice.
    3. Enqueue the job (behind the ``JobQueue`` interface) and return 200
       immediately. No heavy work happens in this request path — at M2
       "heavy work" doesn't exist yet, but the shape must already be right
       for M3 to plug a real queue in without touching this file.

Status code choices (documented here since the spec asks for deliberateness):
    - 401: signature missing, or well-formed but wrong (an auth failure).
    - 400: malformed input — a garbled signature header, invalid JSON body,
      a missing required header, or a ``pull_request`` payload that doesn't
      match the expected shape.
    - 200: request handled to completion, whether that means "accepted and
      enqueued", "already seen, not re-enqueued", or "not a pull_request /
      unsupported action, intentionally ignored". All three are successful
      outcomes from GitHub's point of view — GitHub should not retry any of
      them — which is why none of them is a 4xx or 202.
    - 503: the queue itself could not accept the job (M6; previously this
      surfaced as an unhandled 500 -- see the M3-deferred item this closes,
      tracked in ``.genesis/checkpoints/CURRENT.md``). ``RedisJobQueue``
      raises ``JobQueue.enqueue``'s ``QueueUnavailableError`` when Redis is
      unreachable after retries, or when its circuit breaker has opened.
      Both are the same kind of failure from a caller's point of view: "the
      dependency this needs is down right now" -- a condition GitHub's own
      redelivery mechanism is designed to handle, not a bug in this
      service. 503 (not 500) tells GitHub, and any other caller, that this
      is a transient, expected condition worth retrying, not a defect.

Freeze-boundary note (M6): this file is outside M6's literal freeze
boundary (``backend/reliability/{retry,circuit_breaker,idempotency,timeout}.py``,
``tests/unit/test_reliability.py``), but the M6 L1 BUILD driver's own
instructions explicitly called for this specific, narrow change (closing
the tracked "Redis-down enqueue returns 500 not 503" deferred item now that
a circuit breaker makes "the dependency is down" a distinguishable state) —
disclosed here and in that session's final report, not silently expanded
scope.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from backend.core.settings import Settings
from backend.job_queue.interface import JobQueue, QueueUnavailableError
from backend.webhook_receiver.parser import SUPPORTED_ACTIONS, parse_pull_request_payload
from backend.webhook_receiver.validator import (
    InvalidSignatureError,
    MalformedSignatureError,
    MissingSignatureError,
    verify_signature,
)

router = APIRouter()

_SIGNATURE_HEADER = "X-Hub-Signature-256"
_EVENT_HEADER = "X-GitHub-Event"
_DELIVERY_HEADER = "X-GitHub-Delivery"
_PULL_REQUEST_EVENT = "pull_request"


def get_job_queue(request: Request) -> JobQueue:
    """Dependency: fetch the app-level JobQueue instance.

    Reading it off ``request.app.state`` (set once in ``create_app``) rather
    than a module-level global keeps each FastAPI app instance's queue
    isolated — required for tests to construct independent apps with
    independent queues without sharing state.
    """
    queue: JobQueue = request.app.state.job_queue
    return queue


def get_settings_dependency(request: Request) -> Settings:
    """Dependency: fetch the app-level Settings instance.

    Like ``get_job_queue``, this reads from ``request.app.state`` (set in
    ``create_app``) instead of calling ``get_settings()`` directly, so tests
    can construct an app with an arbitrary secret without touching the real
    process environment or a ``.env`` file.
    """
    settings: Settings = request.app.state.settings
    return settings


@router.post("/webhook")
async def receive_webhook(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings_dependency)],
    queue: Annotated[JobQueue, Depends(get_job_queue)],
) -> JSONResponse:
    """Receive, verify, and enqueue a GitHub ``pull_request`` webhook delivery.

    Order of operations is the point of this milestone: signature
    verification happens first, over the untouched raw body, before the
    body is parsed as JSON or inspected in any way.
    """
    raw_body = await request.body()

    # Step 1: verify the signature over the RAW bytes, before anything else.
    signature_header = request.headers.get(_SIGNATURE_HEADER)
    try:
        verify_signature(raw_body, signature_header, settings.github_webhook_secret)
    except MissingSignatureError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except MalformedSignatureError as exc:
        raise HTTPException(status_code=400, detail=f"malformed signature header: {exc}") from exc
    except InvalidSignatureError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    # Only now, with a verified signature, do we parse the body at all.
    try:
        payload: Any = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"malformed JSON body: {exc}") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="webhook payload must be a JSON object")

    # Only pull_request events are handled at all; everything else is
    # acknowledged (200) but otherwise ignored, per M2 scope.
    event_type = request.headers.get(_EVENT_HEADER)
    if event_type != _PULL_REQUEST_EVENT:
        return JSONResponse(
            status_code=200,
            content={"status": "ignored", "reason": f"unsupported event type: {event_type!r}"},
        )

    action = payload.get("action")
    if action not in SUPPORTED_ACTIONS:
        return JSONResponse(
            status_code=200,
            content={"status": "ignored", "reason": f"unsupported action: {action!r}"},
        )

    # Step 2 (idempotency key): the X-GitHub-Delivery header is required for
    # any event we're actually going to act on.
    delivery_id = request.headers.get(_DELIVERY_HEADER)
    if not delivery_id:
        raise HTTPException(status_code=400, detail=f"missing {_DELIVERY_HEADER} header")

    try:
        event = parse_pull_request_payload(
            payload,
            delivery_id=delivery_id,
            received_at=datetime.now(UTC).isoformat(),
        )
    except (KeyError, ValidationError) as exc:
        raise HTTPException(
            status_code=400, detail=f"malformed pull_request payload: {exc}"
        ) from exc

    # Step 2 (dedup) + Step 3 (enqueue): JobQueue.enqueue is itself the
    # idempotency check — calling it twice with the same delivery_id is a
    # no-op the second time. No heavy work happens here or after.
    try:
        result = queue.enqueue(event)
    except QueueUnavailableError as exc:
        # M6: the queue could not accept this job right now (Redis
        # unreachable after retries, or its circuit breaker has opened).
        # 503, not an unhandled 500 -- GitHub's own redelivery will retry
        # this exact webhook later, once the dependency recovers.
        raise HTTPException(
            status_code=503, detail=f"job queue temporarily unavailable: {exc}"
        ) from exc
    return JSONResponse(
        status_code=200,
        content={
            "status": "accepted" if result.enqueued else "duplicate",
            "delivery_id": result.delivery_id,
        },
    )
