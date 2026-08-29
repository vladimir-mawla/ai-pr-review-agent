"""api module.

FastAPI application and HTTP routes. Public entrypoints for webhook and admin APIs.
Per ADR-002, this module follows the inward-only dependency rule: dependencies point
toward backend.core and backend.models, never outward toward api or orchestrator.
"""
