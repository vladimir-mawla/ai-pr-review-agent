"""job_queue module.

Async job queue backed by Redis/ARQ. Work distribution to agents.
Per ADR-002, this module follows the inward-only dependency rule: dependencies point
toward backend.core and backend.models, never outward toward api or orchestrator.
"""
