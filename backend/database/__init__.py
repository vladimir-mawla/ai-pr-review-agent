"""database module.

Persistent storage layer. Postgres connections, schema, and repository interfaces.
Per ADR-002, this module follows the inward-only dependency rule: dependencies point
toward backend.core and backend.models, never outward toward api or orchestrator.
"""
