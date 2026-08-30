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

``enqueue_async`` (L2 DEBUG, HIGH PRIORITY item found by M7's L4 VERIFY,
fixed post-M7 in dedicated M6-scope-code fix loop): ``enqueue`` above is
*synchronous*, and this docstring has always said calling it "may perform
bounded blocking I/O of its own" -- for ``RedisJobQueue`` specifically,
that blocking I/O runs through ``backend.reliability.timeout``'s
``run_with_timeout``/``await_future``, which call a plain
``future.result(timeout=...)``. Before this fix,
``backend.webhook_receiver.router``'s ``async def receive_webhook`` called
``queue.enqueue(event)`` directly -- unawaited, not offloaded -- from
inside a coroutine running on uvicorn's single event-loop thread. That is
the exact defect class ``backend.observability.events``'s
``emit_decision_async`` docstring describes M7's own L4 VERIFY catching on
the events-write call site: a synchronous blocking call made directly from
a coroutine blocks the *entire* event loop for as long as the call takes,
serialising every other concurrent, unrelated request behind it -- here,
one request's slow-but-not-down Redis would stall every other in-flight
webhook. ``enqueue_async`` below is the fix, following that same
established pattern: it runs ``queue.enqueue`` on a worker thread via
``asyncio.to_thread`` (asyncio's own default executor -- a bounded thread
pool, not one thread per call) and is meant to be ``await``-ed by a
coroutine caller, so only *that* caller's own task blocks, never the event
loop every other in-flight request also depends on. It is a free function
here (not a second ``JobQueue`` Protocol method) so every existing
``JobQueue`` implementation -- ``InMemoryJobQueue``, ``RedisJobQueue``, and
any future one -- gets it for free with no interface change, and every
purely-synchronous caller (tests, the ARQ worker, anything not running on
an event loop) keeps calling ``enqueue`` directly, unaffected.
"""

from __future__ import annotations

import asyncio
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


async def enqueue_async(queue: JobQueue, event: WebhookEvent) -> EnqueueResult:
    """``queue.enqueue(event)``, off the calling coroutine's own thread.

    The intended call site is ``backend.webhook_receiver.router.receive_webhook``
    -- an ``async def`` route on uvicorn's single event loop -- exactly
    mirroring why ``backend.observability.events.emit_decision_async``
    exists (see that function's docstring, and this module's docstring's
    "L2 DEBUG" section, for the full defect this fixes). ``asyncio.to_thread``
    submits ``queue.enqueue`` to asyncio's own default executor (a bounded
    thread pool, not one new thread per call -- see the stdlib's
    ``loop.run_in_executor`` docs) and returns an awaitable, so only this
    call's own caller blocks while it waits, not the whole event loop.

    Still *awaits* completion before returning (not "fire and forget"): the
    caller sees ``enqueue``'s real result (or its real exception) before
    proceeding, preserving the exact same observable behavior ``enqueue``
    itself has -- idempotency, the ``QueueUnavailableError`` contract, and
    every M6 reliability guarantee -- unchanged. Only *where* the blocking
    happens has changed, not *whether*, *what*, or *how long* the caller
    waits for it.

    A cleaner-looking alternative would be to give ``RedisJobQueue`` a
    genuinely async ``enqueue`` that awaits ARQ's coroutine directly instead
    of bouncing through a thread (twice, in fact: once here, and once again
    inside ``RedisJobQueue`` itself, which already bridges onto its own
    private event-loop thread via ``asyncio.run_coroutine_threadsafe`` --
    see ``backend/job_queue/redis_arq.py``'s module docstring). That would
    mean reworking ``JobQueue`` into an async Protocol, which every
    implementation (``InMemoryJobQueue``, every test double, the ARQ worker's
    own call sites) and every M6 reliability call site inside
    ``RedisJobQueue`` would have to follow -- a much larger, higher-risk
    change for a HIGH PRIORITY fix that needs to preserve M2/M3/M6's already-
    verified behavior exactly. This function keeps ``JobQueue.enqueue``
    itself untouched (still the synchronous contract M2 established and
    every implementation/test already relies on) and only changes how the
    router calls it -- the same scope discipline M7's own
    ``emit_decision_async`` fix used for the identical class of bug.
    """
    return await asyncio.to_thread(queue.enqueue, event)
