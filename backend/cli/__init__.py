"""cli module.

Command-line interface for local testing and admin tasks.

M10 correction to this stub's original text: ``backend.cli`` is a top-level
entry point (like ``backend.api``), not an inner layer -- ``import-linter``'s
``.importlinter`` contract only forbids ``backend.core``/``backend.models``
from importing outward, and does not (and should not) restrict this
package. ``backend.cli.review_local`` (M10) legitimately imports
``backend.orchestrator`` and ``backend.integrations`` -- it exists
specifically to drive the orchestrator end-to-end from the command line,
the same "outermost layer wires everything else together" role
``backend.api.main`` plays for the HTTP path.
"""
