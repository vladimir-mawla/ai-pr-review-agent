"""observability module.

Observability and instrumentation. Events table, tracing, audit logging.
Per ADR-002, this module follows the inward-only dependency rule: dependencies point
toward backend.core and backend.models, never outward toward api or orchestrator.
"""
