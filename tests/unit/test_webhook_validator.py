"""Tests for the M2 webhook ingress: signature verification + idempotency.

Covers both the narrow HMAC math (backend.webhook_receiver.validator) and the
route-level behavior (backend.webhook_receiver.router via FastAPI's
TestClient), since the milestone's success criteria are about end-to-end HTTP
behavior (status codes, enqueue counts), not just the crypto in isolation.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from backend.api.main import create_app
from backend.core.settings import Settings
from backend.job_queue.in_memory import InMemoryJobQueue
from backend.webhook_receiver.validator import (
    InvalidSignatureError,
    MalformedSignatureError,
    MissingSignatureError,
    verify_signature,
)

SECRET = "unit-test-secret"


def _sign(body: bytes, secret: str = SECRET) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _sample_payload(action: str = "opened") -> dict[str, object]:
    return {
        "action": action,
        "pull_request": {
            "number": 42,
            "head": {"sha": "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"},
        },
        "repository": {
            "name": "pr-review-agent",
            "owner": {"login": "myorg"},
        },
    }


@pytest.fixture
def queue() -> InMemoryJobQueue:
    return InMemoryJobQueue()


@pytest.fixture
def client(queue: InMemoryJobQueue) -> Iterator[TestClient]:
    app = create_app(settings=Settings(github_webhook_secret=SECRET), job_queue=queue)
    with TestClient(app) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# Unit-level tests: backend.webhook_receiver.validator.verify_signature
# ---------------------------------------------------------------------------


class TestVerifySignatureUnit:
    def test_correctly_signed_payload_verifies(self) -> None:
        body = b'{"hello": "world"}'
        header = _sign(body)
        verify_signature(body, header, SECRET)  # must not raise

    def test_wrong_secret_is_rejected(self) -> None:
        body = b'{"hello": "world"}'
        header = _sign(body, secret="a-different-secret")
        with pytest.raises(InvalidSignatureError):
            verify_signature(body, header, SECRET)

    def test_missing_header_is_rejected(self) -> None:
        with pytest.raises(MissingSignatureError):
            verify_signature(b'{"hello": "world"}', None, SECRET)

    @pytest.mark.parametrize(
        "bad_header",
        [
            "deadbeef" * 8,  # 64 hex chars but no "sha256=" prefix
            "sha1=" + "a" * 40,  # wrong algorithm prefix
            "sha256=not-hex-at-all-" + "z" * 41,  # non-hex characters
            "sha256=" + "a" * 63,  # truncated (63 instead of 64 hex chars)
            "sha256=",  # empty digest
        ],
    )
    def test_malformed_header_is_rejected(self, bad_header: str) -> None:
        with pytest.raises(MalformedSignatureError):
            verify_signature(b'{"hello": "world"}', bad_header, SECRET)

    def test_underscore_in_digest_is_rejected_as_malformed_not_invalid(self) -> None:
        # int(value, 16) happily parses underscore-separated hex ("dead_beef"),
        # so a naive `_is_hex` built on it would let this 64-character string
        # through the shape check and only fail later as a signature
        # mismatch (InvalidSignatureError / 401) rather than being caught as
        # malformed input (MalformedSignatureError / 400). The charset-only
        # check must reject the underscore itself.
        digest_with_underscore = "a" * 31 + "_" + "b" * 32  # 64 chars, one underscore
        assert len(digest_with_underscore) == 64
        bad_header = f"sha256={digest_with_underscore}"
        with pytest.raises(MalformedSignatureError):
            verify_signature(b'{"hello": "world"}', bad_header, SECRET)

    def test_tampered_body_with_valid_signature_shape_is_rejected(self) -> None:
        original_body = b'{"hello": "world"}'
        header = _sign(original_body)
        tampered_body = b'{"hello": "warld"}'
        with pytest.raises(InvalidSignatureError):
            verify_signature(tampered_body, header, SECRET)

    def test_uses_constant_time_comparison(self) -> None:
        # hmac.compare_digest is what makes this timing-safe; this test just
        # pins that a correct digest with different case still verifies
        # (case-insensitive hex compare), proving we compare digest bytes
        # semantically rather than doing a brittle exact-string check.
        body = b'{"hello": "world"}'
        header = _sign(body).upper().replace("SHA256", "sha256")
        verify_signature(body, header, SECRET)  # must not raise


# ---------------------------------------------------------------------------
# Route-level tests: POST /webhook via FastAPI TestClient
# ---------------------------------------------------------------------------


class TestWebhookRoute:
    def test_correctly_signed_pull_request_opened_is_accepted(
        self, client: TestClient, queue: InMemoryJobQueue
    ) -> None:
        body = json.dumps(_sample_payload("opened")).encode("utf-8")
        response = client.post(
            "/webhook",
            content=body,
            headers={
                "X-Hub-Signature-256": _sign(body),
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "11111111-1111-1111-1111-111111111111",
                "Content-Type": "application/json",
            },
        )
        assert response.status_code == 200
        assert response.json()["status"] == "accepted"
        assert queue.size() == 1

    def test_wrong_signature_is_rejected_with_401(
        self, client: TestClient, queue: InMemoryJobQueue
    ) -> None:
        body = json.dumps(_sample_payload("opened")).encode("utf-8")
        response = client.post(
            "/webhook",
            content=body,
            headers={
                "X-Hub-Signature-256": _sign(body, secret="wrong-secret"),
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "22222222-2222-2222-2222-222222222222",
            },
        )
        assert response.status_code == 401
        assert queue.size() == 0

    def test_missing_signature_header_is_rejected_with_401(
        self, client: TestClient, queue: InMemoryJobQueue
    ) -> None:
        body = json.dumps(_sample_payload("opened")).encode("utf-8")
        response = client.post(
            "/webhook",
            content=body,
            headers={
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "33333333-3333-3333-3333-333333333333",
            },
        )
        assert response.status_code == 401
        assert queue.size() == 0

    def test_malformed_signature_header_is_rejected_with_400(
        self, client: TestClient, queue: InMemoryJobQueue
    ) -> None:
        body = json.dumps(_sample_payload("opened")).encode("utf-8")
        response = client.post(
            "/webhook",
            content=body,
            headers={
                "X-Hub-Signature-256": "not-even-close-to-valid",
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "44444444-4444-4444-4444-444444444444",
            },
        )
        assert response.status_code == 400
        assert queue.size() == 0

    def test_tampered_body_with_otherwise_valid_signature_is_rejected(
        self, client: TestClient, queue: InMemoryJobQueue
    ) -> None:
        original_body = json.dumps(_sample_payload("opened")).encode("utf-8")
        header = _sign(original_body)
        tampered_body = json.dumps(_sample_payload("closed")).encode("utf-8")
        response = client.post(
            "/webhook",
            content=tampered_body,
            headers={
                "X-Hub-Signature-256": header,
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "55555555-5555-5555-5555-555555555555",
            },
        )
        assert response.status_code == 401
        assert queue.size() == 0

    def test_replayed_delivery_id_is_enqueued_exactly_once(
        self, client: TestClient, queue: InMemoryJobQueue
    ) -> None:
        body = json.dumps(_sample_payload("opened")).encode("utf-8")
        headers = {
            "X-Hub-Signature-256": _sign(body),
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "66666666-6666-6666-6666-666666666666",
        }

        first = client.post("/webhook", content=body, headers=headers)
        second = client.post("/webhook", content=body, headers=headers)

        assert first.status_code == 200
        assert first.json()["status"] == "accepted"
        assert second.status_code == 200
        assert second.json()["status"] == "duplicate"
        assert queue.size() == 1

    def test_non_pull_request_event_is_ignored_gracefully(
        self, client: TestClient, queue: InMemoryJobQueue
    ) -> None:
        body = json.dumps({"zen": "Responsive is better than fast."}).encode("utf-8")
        response = client.post(
            "/webhook",
            content=body,
            headers={
                "X-Hub-Signature-256": _sign(body),
                "X-GitHub-Event": "ping",
                "X-GitHub-Delivery": "77777777-7777-7777-7777-777777777777",
            },
        )
        assert response.status_code == 200
        assert response.json()["status"] == "ignored"
        assert queue.size() == 0

    @pytest.mark.parametrize("action", ["closed", "labeled", "review_requested"])
    def test_unsupported_action_is_ignored_gracefully(
        self, client: TestClient, queue: InMemoryJobQueue, action: str
    ) -> None:
        body = json.dumps(_sample_payload(action)).encode("utf-8")
        response = client.post(
            "/webhook",
            content=body,
            headers={
                "X-Hub-Signature-256": _sign(body),
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "88888888-8888-8888-8888-888888888888",
            },
        )
        assert response.status_code == 200
        assert response.json()["status"] == "ignored"
        assert queue.size() == 0

    def test_missing_delivery_header_is_rejected_with_400(
        self, client: TestClient, queue: InMemoryJobQueue
    ) -> None:
        body = json.dumps(_sample_payload("opened")).encode("utf-8")
        response = client.post(
            "/webhook",
            content=body,
            headers={
                "X-Hub-Signature-256": _sign(body),
                "X-GitHub-Event": "pull_request",
            },
        )
        assert response.status_code == 400
        assert queue.size() == 0

    def test_malformed_json_body_is_rejected_with_400(
        self, client: TestClient, queue: InMemoryJobQueue
    ) -> None:
        body = b"{not valid json"
        response = client.post(
            "/webhook",
            content=body,
            headers={
                "X-Hub-Signature-256": _sign(body),
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "99999999-9999-9999-9999-999999999999",
            },
        )
        assert response.status_code == 400
        assert queue.size() == 0

    def test_malformed_pull_request_shape_is_rejected_with_400(
        self, client: TestClient, queue: InMemoryJobQueue
    ) -> None:
        # Valid JSON, valid signature, correct event/action -- but missing
        # the nested fields the parser requires.
        body = json.dumps({"action": "opened"}).encode("utf-8")
        response = client.post(
            "/webhook",
            content=body,
            headers={
                "X-Hub-Signature-256": _sign(body),
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            },
        )
        assert response.status_code == 400
        assert queue.size() == 0
