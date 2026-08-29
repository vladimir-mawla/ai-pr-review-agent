"""Core module - the innermost layer that all other modules depend on.

Per ADR-002 (inward-only dependency rule), this module MUST NOT import from any
sibling package. It contains only pure utilities and base abstractions that are
safe for all layers to depend on without creating cycles.
"""
