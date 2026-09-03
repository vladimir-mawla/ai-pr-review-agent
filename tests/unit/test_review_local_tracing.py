"""Tests for ``backend.cli.review_local``'s opt-in ``--verify-tracing`` contract.

The gap this closes: ``review_local`` is the CLI used for demos, and it is
where LangSmith's silent-failure mode actually bites (see
``backend/observability/tracing.py``'s module docstring, and
``backend/cli/review_local.py``'s own "CLOSING THE ACTUAL DEMO-DAY GAP"
section) -- a misconfigured ``LANGSMITH_ENDPOINT``/``LANGSMITH_WORKSPACE_ID``
lets LangSmith swallow the ingestion error while ``review_local`` still
prints a fully successful review and exits 0, with zero traces and zero
indication anything went wrong.

``verify_tracing_before_review`` is the whole fix, tested here entirely
independently of the orchestrator/LLM pipeline (no diff, no engine, no
Anthropic call, ever) via injected ``Settings``/fake LangSmith ``Client``
doubles -- exactly the same style ``tests/unit/test_tracing_guard.py`` uses
for ``assert_tracing_healthy`` itself, since this function is a thin,
non-duplicating wrapper around that one.

No test in this file makes a real LangSmith (or any other) network call --
the whole point of ``--verify-tracing`` defaulting to ``False`` is that the
free `pytest` suite never has to.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from backend.cli.review_local import _UNVERIFIED_TRACING_WARNING, verify_tracing_before_review
from backend.core.settings import Settings
from backend.observability.tracing import TracingConfigurationError

_SECRET = "review-local-tracing-test-secret"


def _settings(**overrides: Any) -> Settings:
    return Settings(github_webhook_secret=_SECRET, **overrides)


class _NeverTouchMeClient:
    """A client double that fails the test the instant ANY method is called on it.

    Used to prove "no LangSmith call at all" -- not by inspecting a call
    log after the fact, but by making a stray call impossible to miss.
    """

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(
            f"verify_tracing_before_review touched the LangSmith client "
            f"(attempted .{name}(...)) when it was not supposed to"
        )


class _CallRecordingClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def create_run(self, **kwargs: Any) -> None:
        self.calls.append("create_run")

    def update_run(self, run_id: UUID, **kwargs: Any) -> None:
        self.calls.append("update_run")

    def flush(self, timeout: float | None = None) -> None:
        self.calls.append("flush")

    def read_run(self, run_id: UUID, *args: Any, **kwargs: Any) -> Any:
        self.calls.append("read_run")
        return object()

    def read_project(self, *, project_name: str) -> Any:
        self.calls.append("read_project")

        class _FakeProject:
            url = "https://smith.langchain.com/o/fake-org/projects/p/fake-project-id"

        return _FakeProject()


class _ForbiddenClient(_CallRecordingClient):
    """Models a bad workspace id/key: every write call 403s, mirroring the real API."""

    def create_run(self, **kwargs: Any) -> None:
        super().create_run(**kwargs)
        raise RuntimeError(
            "Failed to POST https://aws.api.smith.langchain.com/runs in LangSmith "
            "API. HTTPError('403 Client Error: Forbidden for url: "
            "https://aws.api.smith.langchain.com/runs', '{\"error\":\"Forbidden\"}')"
        )


class TestVerifyTracingOffMakesNoLangSmithCallAtAll:
    """``--verify-tracing`` is off by default -- this is the free-suite safety property."""

    def test_off_with_tracing_enabled_never_touches_the_client(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        settings = _settings(langsmith_tracing=True, langsmith_api_key="fake-key-not-real")

        # Would raise immediately (failing this test) if verify_tracing_before_review
        # ever called anything on it.
        verify_tracing_before_review(verify_tracing=False, settings=settings, client=_NeverTouchMeClient())

        # The one thing that DOES happen: the unmissable stdout warning.
        assert _UNVERIFIED_TRACING_WARNING in capsys.readouterr().out

    def test_off_with_tracing_disabled_is_a_silent_no_op(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        settings = _settings(langsmith_tracing=False)

        verify_tracing_before_review(verify_tracing=False, settings=settings, client=_NeverTouchMeClient())

        assert capsys.readouterr().out == ""

    def test_verify_tracing_flag_defaults_to_false_on_the_cli(self) -> None:
        from backend.cli.review_local import _build_arg_parser

        args = _build_arg_parser().parse_args(
            ["--diff", "unused.patch", "--out", "unused.json"]
        )
        assert args.verify_tracing is False


class TestVerifyTracingOnWithTracingDisabledIsACleanNoOp:
    def test_no_op_no_error_no_config_required(self, capsys: pytest.CaptureFixture[str]) -> None:
        settings = _settings(langsmith_tracing=False)  # no LangSmith config at all

        # Should not raise, and should never touch the client either --
        # assert_tracing_healthy's own no-op-when-disabled short-circuit.
        verify_tracing_before_review(verify_tracing=True, settings=settings, client=_NeverTouchMeClient())

        assert capsys.readouterr().out == ""


class TestVerifyTracingOnWithBrokenConfigurationFailsLoudly:
    def test_raises_tracing_configuration_error_with_a_diagnosis(self) -> None:
        settings = _settings(
            langsmith_tracing=True,
            langsmith_api_key="fake-key-not-real",
            langsmith_endpoint="https://api.smith.langchain.com",  # the WRONG region
            langsmith_workspace_id=None,  # the known gotcha
        )
        client = _ForbiddenClient()

        with pytest.raises(TracingConfigurationError) as exc_info:
            verify_tracing_before_review(verify_tracing=True, settings=settings, client=client)

        message = str(exc_info.value)
        assert "LANGSMITH_WORKSPACE_ID" in message
        assert "403" in message or "Forbidden" in message
        # Never reached read_project -- the health check failed first.
        assert "read_project" not in client.calls


class TestVerifyTracingOnWithHealthyConfigurationPassesAndPrintsTheProjectUrl:
    def test_prints_verified_and_the_project_url(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr("backend.observability.tracing.time.sleep", lambda _seconds: None)
        settings = _settings(langsmith_tracing=True, langsmith_api_key="fake-key-not-real")
        client = _CallRecordingClient()

        verify_tracing_before_review(verify_tracing=True, settings=settings, client=client)

        out = capsys.readouterr().out
        assert "verified healthy" in out
        assert "https://smith.langchain.com/o/fake-org/projects/p/fake-project-id" in out
        assert client.calls[:4] == ["create_run", "update_run", "flush", "read_run"]
        assert "read_project" in client.calls
