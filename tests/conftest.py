"""Shared pytest setup.

``backend.api.main`` builds a module-level ``app`` object eagerly at import
time (see that module's docstring: this is deliberate, so a real deployment
fails fast on missing configuration rather than on the first request). That
means merely importing ``backend.api.main`` requires ``GITHUB_WEBHOOK_SECRET``
to be resolvable, even though no test in this suite uses that module-level
``app`` — every webhook test builds its own isolated app via
``create_app(settings=Settings(github_webhook_secret=...))`` with a secret it
controls.

This placeholder value is set before any test module imports
``backend.api.main`` (or anything importing it), purely so that import
doesn't crash in a clean environment with no ``.env`` file. It is never used
to sign or verify anything.
"""

import os

os.environ.setdefault("GITHUB_WEBHOOK_SECRET", "unused-import-time-placeholder")
