"""security module.

RBAC, request signing, secret masking. Security controls and policies.
Per ADR-002, this module follows the inward-only dependency rule: dependencies point
toward backend.core and backend.models, never outward toward api or orchestrator.
"""
