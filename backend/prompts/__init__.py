"""prompts module.

Prompt registry and versioning. Templates for each specialist agent.
Per ADR-002, this module follows the inward-only dependency rule: dependencies point
toward backend.core and backend.models, never outward toward api or orchestrator.
"""
