"""tools module.

LLM client, model routing, and tool definitions for agent use.
Per ADR-002, this module follows the inward-only dependency rule: dependencies point
toward backend.core and backend.models, never outward toward api or orchestrator.
"""
