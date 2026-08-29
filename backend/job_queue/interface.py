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


class JobQueue(Protocol):
    """Abstract job queue. Every implementation must be idempotent per delivery_id."""

    def enqueue(self, event: WebhookEvent) -> EnqueueResult:
        """Enqueue a verified webhook event exactly once per ``delivery_id``.

        Must return immediately (no heavy work, no blocking I/O on the
        actual job processing) — the whole point of this interface is that
        the request path only ever does this and returns.
        """
        ...

    def size(self) -> int:
        """Number of distinct jobs currently enqueued (for tests/introspection)."""
        ...
