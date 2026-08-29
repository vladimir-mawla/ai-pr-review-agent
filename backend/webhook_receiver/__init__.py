"""webhook_receiver module.

GitHub webhook ingress. HMAC validation, idempotency checking, request parsing.
Per ADR-002, this module follows the inward-only dependency rule: dependencies point
toward backend.core and backend.models, never outward toward api or orchestrator.
"""
