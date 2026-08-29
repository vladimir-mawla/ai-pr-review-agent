"""memory module.

Hybrid retrieval layer. Vector search, full-text search, and reciprocal rank fusion.
Per ADR-002, this module follows the inward-only dependency rule: dependencies point
toward backend.core and backend.models, never outward toward api or orchestrator.
"""
