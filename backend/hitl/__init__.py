"""hitl module.

Human-in-the-loop queue and workflows. Routes uncertain findings for manual review.
Per ADR-002, this module follows the inward-only dependency rule: dependencies point
toward backend.core and backend.models, never outward toward api or orchestrator.
"""
