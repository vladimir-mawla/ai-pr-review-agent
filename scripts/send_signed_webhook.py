#!/usr/bin/env python3
"""CLI: sign a sample GitHub webhook payload and POST it to a running server.

Owns: proving the M2 demo end-to-end without a real GitHub App. It reads a
payload fixture as raw bytes (never re-serializes it — see
``backend.webhook_receiver.validator`` for why byte-for-byte fidelity
matters), computes the same HMAC-SHA256 signature GitHub would send, and
POSTs it with the headers the ingress route expects.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import sys
import uuid
from pathlib import Path

import httpx

DEFAULT_PAYLOAD_PATH = Path(__file__).resolve().parent.parent / "tests/fixtures/sample_pr_payload.json"


def sign_payload(raw_body: bytes, secret: str) -> str:
    """Compute the ``sha256=<hex>`` signature GitHub sends in X-Hub-Signature-256."""
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        description="Sign and POST a sample GitHub pull_request webhook payload."
    )
    parser.add_argument(
        "--secret",
        required=True,
        help="Shared webhook secret to sign with (must match the server's GITHUB_WEBHOOK_SECRET).",
    )
    parser.add_argument(
        "--url",
        required=True,
        help="Full URL of the webhook endpoint, e.g. http://localhost:8000/webhook",
    )
    parser.add_argument(
        "--payload",
        type=Path,
        default=DEFAULT_PAYLOAD_PATH,
        help="Path to the JSON payload fixture to send (sent as raw bytes, unmodified).",
    )
    parser.add_argument(
        "--event",
        default="pull_request",
        help="Value for the X-GitHub-Event header (default: pull_request).",
    )
    parser.add_argument(
        "--delivery-id",
        default=None,
        help="Value for the X-GitHub-Delivery header. Defaults to a freshly generated UUID.",
    )
    parser.add_argument(
        "--tamper",
        action="store_true",
        help="Flip one byte of the body after signing, to demonstrate rejection of a tampered body.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Sign the payload, POST it, print the result, and return an exit code."""
    args = build_arg_parser().parse_args(argv)

    raw_body = args.payload.read_bytes()
    signature = sign_payload(raw_body, args.secret)

    body_to_send = raw_body
    if args.tamper:
        # Deliberately corrupt the body *after* signing, so the signature no
        # longer matches -- used to demonstrate the tampered-body rejection
        # path, not part of the normal demo run.
        body_to_send = raw_body.replace(b'"opened"', b'"closed"', 1)

    delivery_id = args.delivery_id or str(uuid.uuid4())

    headers = {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": signature,
        "X-GitHub-Event": args.event,
        "X-GitHub-Delivery": delivery_id,
    }

    response = httpx.post(args.url, content=body_to_send, headers=headers, timeout=10.0)
    print(f"POST {args.url} -> {response.status_code}")
    print(response.text)

    if response.status_code >= 400:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
