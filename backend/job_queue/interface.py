"""Abstract enqueue interface between webhook ingress and job processing.

Owns: the contract the ingress handler (``backend.webhook_receiver``) programs
against, so the request path never depends on a concrete queue technology.
At M2 there is no real queue — that is M3's job (Redis + ARQ). Coding the
ingress against ``JobQueue`` rather than a concrete class means M3 can swap
in a Redis-backed implementation without changing the router, its tests, or
this interface at all.

Idempotency is part of this contract, not bolted on separately: an
implementation must guarantee that calling ``enqueue`` twice with the same
``WebhookEvent.delivery_id`` results in exactly one job, because that is what
"a retried GitHub delivery is not processed twice" actually means at the
point where work is handed off.

``QueueUnavailableError`` (added at M6) is likewise part of this contract
rather than an implementation-specific detail: any ``JobQueue`` may
legitimately be temporarily unable to accept work (its backing store is
down, or -- for ``RedisJobQueue`` specifically -- its circuit breaker has
opened after repeated failures), and the router needs one exception type it
can catch regardless of which concrete implementation raised it, to answer
with a 503 rather than an unhandled 500.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from backend.models import WebhookEvent


@dataclass(frozen=True)
class EnqueueResult:
    """Outcome of a single ``enqueue`` call.

    Attributes:
        enqueued: True if this call actually added a new job. False if
            ``delivery_id`` had already been seen and the call was a
            no-op (the idempotency case).
        delivery_id: The idempotency key this result refers to, echoed back
            for logging/response purposes.
    """

    enqueued: bool
    delivery_id: str


class QueueUnavailableError(Exception):
    """Raised by ``enqueue`` when the queue cannot currently accept work.

    Distinct from "this delivery was already seen" (that's the ordinary,
    successful ``EnqueueResult(enqueued=False, ...)`` path) — this means the
    call could not even determine that, because the backing dependency is
    unreachable or (for ``RedisJobQueue``) its circuit breaker has opened.
    The webhook route catches this and answers 503, not 500: an unavailable
    dependency is an expected, recoverable condition (GitHub will retry the
    delivery later), not a bug to surface as an unhandled server error.
    """


class JobQueue(Protocol):
    """Abstract job queue. Every implementation must be idempotent per delivery_id."""

    def enqueue(self, event: WebhookEvent) -> EnqueueResult:
        """Enqueue a verified webhook event exactly once per ``delivery_id``.

        Must return quickly and must never run the actual job processing
        inline — the whole point of this interface is that the request
        path only ever does this and returns. "Quickly" does not mean
        "with zero I/O": an implementation may perform bounded blocking
        I/O of its own (e.g. a synchronous idempotency check against a
        real store, or a synchronous hand-off to whatever moves the job
        onto a real queue) as long as it is a fixed, short round trip and
        not the job's own work. Callers should treat this call as
        potentially blocking for a bounded amount of time, not as
        guaranteed non-blocking.
        """
        ...

    def size(self) -> int:
        """Number of distinct jobs currently enqueued (for tests/introspection)."""
        ...
