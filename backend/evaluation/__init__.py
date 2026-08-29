"""evaluation module.

Model evaluation and regression testing. Golden dataset and LLM-as-judge.
Per ADR-002, this module follows the inward-only dependency rule: dependencies point
toward backend.core and backend.models, never outward toward api or orchestrator.
"""
