"""agents module.

Specialist agent implementations (security, quality, tests, docs). Single responsibility per agent.
Per ADR-002, this module follows the inward-only dependency rule: dependencies point
toward backend.core and backend.models, never outward toward api or orchestrator.
"""
