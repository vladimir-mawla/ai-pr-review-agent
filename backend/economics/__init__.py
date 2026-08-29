"""economics module.

Cost tracking and budget guard. Daily spend caps and token accounting.
Per ADR-002, this module follows the inward-only dependency rule: dependencies point
toward backend.core and backend.models, never outward toward api or orchestrator.
"""
