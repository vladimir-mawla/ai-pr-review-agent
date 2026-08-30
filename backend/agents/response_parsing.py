"""Tolerant parsing of an LLM's raw text response into schema-valid Findings.

Owns: turning whatever text a real model actually returns into a
``list[backend.models.findings.Finding]`` -- the point in the pipeline
DONE.html's "All LLM/structured outputs validated against a schema" gate is
about, and the exact place PLAN.md's M8 outcome text names ("its raw output
is parsed through the Finding Pydantic schema before anything downstream
sees it").

WHY THIS MODULE EXISTS AT ALL: a model instructed to "respond with only a
JSON object of this shape" does not reliably do exactly that. Real,
observed drift this parser is built to survive:

1. Well-formed: ``{"findings": [...]}`` -- the happy path.
2. Key drift: the model uses a different top-level key, e.g.
   ``{"issues": [...]}`` instead of ``{"findings": [...]}``.
3. Markdown-fenced JSON: the model wraps its JSON in a ` ```json ... ``` `
   (or bare ` ``` ... ``` `) code fence, despite being told not to.
4. Prose-then-JSON: the model prefixes the JSON with a sentence or two of
   commentary ("Here is my review:\\n\\n{...}").
5. A bare list: the model skips the wrapping object entirely and returns
   ``[{...}, {...}]`` directly.
6. Total garbage: no JSON structure can be found/parsed at all, or every
   extracted item fails ``Finding`` validation.

Cases 1-5 are handled by ``parse_findings_from_llm_response`` returning a
real ``list[Finding]``. Case 6 is NOT handled by this module papering over
it with an empty list (that would be "silently dropping the finding", which
this milestone's instructions explicitly forbid) -- it raises
``ResponseParseError``, and it is ``backend.agents.security_agent.
SecurityAgent.analyze`` (the caller) that decides what a total parse
failure means for the review as a whole (a synthetic, forced-HITL fallback
Finding -- see that module).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from pydantic import ValidationError

from backend.models import AgentType, Finding

logger = logging.getLogger(__name__)

# Matches a fenced code block, with or without a language tag (```json ...
# ``` or plain ``` ... ```). Non-greedy + DOTALL so it captures exactly the
# first fenced block's contents, including embedded newlines, and does not
# swallow everything up to the LAST closing fence in a response that
# happens to contain more than one.
_FENCE_RE = re.compile(r"```(?:[a-zA-Z0-9_-]*\n)?(.*?)```", re.DOTALL)

# The alternate top-level keys a drifted response might use instead of the
# documented "findings" key. Checked in order; the first one present wins.
_ALTERNATE_LIST_KEYS: tuple[str, ...] = ("findings", "issues")


class ResponseParseError(Exception):
    """Raised when no valid Finding could be extracted from an LLM response at all.

    Distinct from "the model reported zero findings" (a valid, empty
    ``findings: []`` list is not an error -- it means the specialist found
    nothing to flag). This is raised only when the response could not be
    turned into any schema-valid Finding, which is a very different
    situation: the model's output itself is untrustworthy, not merely
    reporting a clean diff.
    """


def _try_json_loads(text: str) -> Any | None:
    """``json.loads``, returning ``None`` instead of raising on failure."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _extract_embedded_json(text: str) -> Any | None:
    """Find the first balanced JSON object/array embedded anywhere in ``text``.

    Handles "prose-then-JSON": scans for each ``{``/``[`` in turn and asks
    the stdlib decoder to parse starting from there via ``raw_decode``,
    which stops at the first structurally complete value and tells us where
    it ended -- exactly what's needed to ignore trailing prose too, without
    this module hand-rolling a JSON brace-matcher.
    """
    decoder = json.JSONDecoder()
    for match in re.finditer(r"[{\[]", text):
        start = match.start()
        try:
            value, _end_index = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            continue
        return value
    return None


def _extract_json_payload(raw_text: str) -> Any | None:
    """Best-effort extraction of a JSON value from a real model's raw text.

    Tries, in order: the whole text as-is; the first markdown-fenced
    block's contents as-is; then, for each of those two candidates, a
    scan for embedded JSON to tolerate leading/trailing prose. Returns
    ``None`` if nothing parseable is found anywhere.
    """
    stripped = raw_text.strip()
    candidates = [stripped]
    fence_match = _FENCE_RE.search(stripped)
    if fence_match:
        candidates.append(fence_match.group(1).strip())

    for candidate in candidates:
        direct = _try_json_loads(candidate)
        if direct is not None:
            return direct
        embedded = _extract_embedded_json(candidate)
        if embedded is not None:
            return embedded
    return None


def _extract_finding_dicts(payload: Any) -> list[Any] | None:
    """Pull the list of raw finding dicts out of a parsed JSON payload.

    Handles the "bare list" drift case directly (the payload itself is
    already a list) and the "key drift" case by trying each of
    ``_ALTERNATE_LIST_KEYS`` in turn. Returns ``None`` if ``payload`` is
    neither shape, or is a dict with no recognizable list under any known
    key.
    """
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in _ALTERNATE_LIST_KEYS:
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return None


def parse_findings_from_llm_response(
    raw_text: str,
    *,
    default_agent_type: AgentType,
) -> list[Finding]:
    """Parse a real model's raw text response into a list of validated Findings.

    ``default_agent_type`` is injected onto every item that doesn't already
    specify its own ``agent_type`` (the prompt explicitly asks the model
    NOT to include one -- see ``backend/prompts/templates/security/v1.md``
    -- since the caller already knows which specialist is asking; trusting
    the model's own claim about its identity would be an unnecessary and
    unenforceable extra trust boundary).

    An individual item that fails ``Finding`` validation (e.g. a
    hallucinated severity string, a missing required field) is logged and
    skipped rather than failing the whole batch -- one malformed entry in
    an otherwise-good list of findings should not discard every other,
    valid finding the model reported.

    Raises ``ResponseParseError`` if no JSON structure could be located at
    all, no recognizable findings list could be extracted from it, or the
    list was non-empty yet every item in it failed validation -- see this
    module's docstring for why that case is the caller's responsibility,
    not this function's. A genuinely EMPTY findings list (``items == []``,
    e.g. ``{"findings": []}`` for a clean diff) is deliberately NOT an
    error -- it is not distinguishable from, and must not be conflated
    with, "every item failed validation": conflating the two would turn a
    clean-diff report into a spurious forced-HITL fallback every single
    time a specialist finds nothing to flag.
    """
    payload = _extract_json_payload(raw_text)
    if payload is None:
        raise ResponseParseError("no JSON object or array found in the LLM response")

    items = _extract_finding_dicts(payload)
    if items is None:
        raise ResponseParseError(
            "parsed JSON has no recognizable findings list "
            f"(looked for a bare list or one of {_ALTERNATE_LIST_KEYS!r})"
        )

    findings: list[Finding] = []
    for item in items:
        if not isinstance(item, dict):
            logger.warning("dropping non-object item in findings list: %r", item)
            continue
        candidate = dict(item)
        candidate.setdefault("agent_type", default_agent_type.value)
        try:
            findings.append(Finding.model_validate(candidate))
        except ValidationError as exc:
            logger.warning("dropping unparseable finding item %r: %s", item, exc)

    # `items` non-empty but nothing survived validation: THIS is the real
    # "the model's output is untrustworthy" case (see the module docstring's
    # case 6). An empty `items` list to begin with is a valid, clean report
    # and must return an empty list here, not raise.
    if not findings and items:
        raise ResponseParseError(
            "no valid Finding objects could be extracted from the LLM response"
        )
    return findings
