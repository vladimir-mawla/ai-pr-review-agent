"""Tests for the LangSmith silent-failure guard (``backend.observability.tracing``).

Covers the guard's whole contract, per its own module docstring:

- ``TestAssertTracingHealthyIsANoOpWhenDisabled``: ``langsmith_tracing=False``
  (the default) never touches the injected client at all -- the "must not
  run, and must not be required, when tracing is disabled" half of the
  contract.
- ``TestAssertTracingHealthyDetectsMisconfiguration``: a fake client double
  standing in for "a bad endpoint/key/workspace id" (exactly what a real
  misconfigured ``Client`` does against LangSmith's real API -- see
  ``_ForbiddenClient``/``_SilentlyDroppingClient`` below, and this module's
  own build notes for the real probe run against the real AWS-deployment
  API that these doubles are modeled on) makes ``assert_tracing_healthy``
  raise ``TracingConfigurationError`` -- never silently return.
- ``TestDiagnosticMessagesNeverLeakKeyMaterial``: the one hard requirement
  that must hold even under a hostile/unexpected exception message.
- ``TestAssertTracingHealthyAgainstTheRealApi``: ``@pytest.mark.live`` --
  actually calls the real LangSmith API with a deliberately wrong
  workspace id, proving the guard detects a real misconfiguration against
  the real service, not just a fake double. Deselected by default
  (``pytest``'s own ``-m 'not live'`` in ``pyproject.toml``); skipped
  outright if no real ``LANGSMITH_API_KEY`` is configured.

No test in this file calls the real LangSmith API except the one
explicitly marked ``live`` -- every other test injects a fake ``client``
double, so this whole file (short of ``-m live``) makes zero network
calls, satisfying this project's hard requirement that the free ``pytest``
suite never talks to LangSmith.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from backend.core.settings import Settings, get_settings
from backend.observability.tracing import TracingConfigurationError, assert_tracing_healthy

_SECRET = "tracing-guard-test-secret"


def _settings(**overrides: Any) -> Settings:
    return Settings(github_webhook_secret=_SECRET, **overrides)


class _CallRecordingClient:
    """Base fake: records every method call so a test can assert "never touched"."""

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


class _ForbiddenClient(_CallRecordingClient):
    """Models a bad workspace id / key: every write call 403s, mirroring the real API."""

    def create_run(self, **kwargs: Any) -> None:
        super().create_run(**kwargs)
        raise RuntimeError(
            "Failed to POST https://aws.api.smith.langchain.com/runs in LangSmith "
            "API. HTTPError('403 Client Error: Forbidden for url: "
            "https://aws.api.smith.langchain.com/runs', '{\"error\":\"Forbidden\"}')"
        )


class _SilentlyDroppingClient(_CallRecordingClient):
    """Models THE exact silent-failure class this guard exists to catch.

    create_run/update_run/flush all succeed with no exception (exactly
    what this project observed for real against a misconfigured, real
    AWS-deployment LangSmith account before LANGSMITH_WORKSPACE_ID was
    added -- see backend/observability/tracing.py's module docstring) --
    but the run can never actually be read back, because it never really
    landed.
    """

    def read_run(self, run_id: UUID, *args: Any, **kwargs: Any) -> Any:
        self.calls.append("read_run")
        raise RuntimeError("Resource not found for /runs/... (404 Not Found)")


class _HealthyClient(_CallRecordingClient):
    """Models a correctly configured client: create/update/flush succeed, and the run reads back."""


class TestAssertTracingHealthyIsANoOpWhenDisabled:
    def test_disabled_never_touches_the_client(self) -> None:
        settings = _settings(langsmith_tracing=False)
        client = _ForbiddenClient()  # would raise immediately if ever called

        assert_tracing_healthy(settings, client=client)

        assert client.calls == []

    def test_disabled_is_the_default(self) -> None:
        # Checked against the FIELD's own default, not a bare `Settings()`
        # construction -- this project's own dev `.env` sets
        # LANGSMITH_TRACING=true (a deployment/environment choice, exactly
        # like ANTHROPIC_API_KEY already being present there), and
        # pydantic-settings' normal env-file precedence means a bare
        # `Settings()` in THIS sandbox legitimately picks that up. The
        # "opt-in, off by default" contract is about the Python-level
        # default a checkout with NO .env config at all gets, which is
        # what `model_fields` reports directly.
        assert Settings.model_fields["langsmith_tracing"].default is False


class TestAssertTracingHealthyVerifiesARealReadBack:
    def test_healthy_client_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("backend.observability.tracing.time.sleep", lambda _seconds: None)
        settings = _settings(langsmith_tracing=True, langsmith_api_key="fake-key-not-real")
        client = _HealthyClient()

        assert_tracing_healthy(settings, client=client)

        assert "create_run" in client.calls
        assert "update_run" in client.calls
        assert "flush" in client.calls
        assert "read_run" in client.calls


class TestAssertTracingHealthyDetectsMisconfiguration:
    """Injects a bad endpoint/key/workspace id (via a fake client double standing in for the
    real API's 403/never-lands behavior) and asserts the guard raises loudly -- never
    returns normally, and never merely logs and continues, unlike this project's
    log-and-continue agent_events failure policy (see traced_span/events.py) -- a
    verification check has no safe degraded mode.
    """

    def test_a_403_on_every_call_raises_tracing_configuration_error(self) -> None:
        settings = _settings(
            langsmith_tracing=True,
            langsmith_api_key="fake-key-not-real",
            langsmith_endpoint="https://api.smith.langchain.com",  # the WRONG region, deliberately
            langsmith_workspace_id=None,  # THE gotcha this project hit for real
        )
        client = _ForbiddenClient()

        with pytest.raises(TracingConfigurationError) as exc_info:
            assert_tracing_healthy(settings, client=client)

        message = str(exc_info.value)
        assert "LANGSMITH_WORKSPACE_ID" in message
        assert "403" in message or "Forbidden" in message

    def test_a_run_that_never_lands_raises_after_exhausting_the_readback_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No real sleeping -- this test proves the retry-then-fail control
        # flow, not the real ~1.5-3s LangSmith ingestion latency.
        monkeypatch.setattr("backend.observability.tracing.time.sleep", lambda _seconds: None)
        # Explicit endpoint/workspace id -- NOT left to inherit whatever
        # (if anything) a real .env happens to have -- so this test's
        # assertion below is deterministic across machines/CI, where no
        # ambient LangSmith config exists at all. This test's whole point
        # is proving the "read-back never succeeds" diagnostic path
        # specifically, not the (separately tested, below) "workspace id
        # is unset" one -- a run of this suite on a machine with a real,
        # correctly-configured .env silently masked exactly this gap
        # before this fix (see this project's own build notes).
        settings = _settings(
            langsmith_tracing=True,
            langsmith_api_key="fake-key-not-real",
            langsmith_endpoint="https://aws.api.smith.langchain.com",
            langsmith_workspace_id="ci-test-workspace-id",
        )
        client = _SilentlyDroppingClient()

        with pytest.raises(TracingConfigurationError) as exc_info:
            assert_tracing_healthy(settings, client=client)

        # create/update/flush all "succeeded" (no exception) -- this is
        # exactly the flush()-returns-cleanly-anyway defect class; only the
        # read-back loop is what catches it.
        assert client.calls[:3] == ["create_run", "update_run", "flush"]
        assert client.calls.count("read_run") >= 2
        assert "could not be read back" in str(exc_info.value)

    def test_missing_workspace_id_is_named_even_without_an_exception_hint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The diagnostic names the workspace-id gotcha proactively, not only when the
        underlying exception happens to mention "403"/"Forbidden" -- e.g. a read-back
        that fails for an unrelated reason should still surface this project's
        own known footgun as a likely cause to check.
        """
        monkeypatch.setattr("backend.observability.tracing.time.sleep", lambda _seconds: None)
        settings = _settings(
            langsmith_tracing=True,
            langsmith_api_key="fake-key-not-real",
            langsmith_workspace_id=None,
        )
        client = _SilentlyDroppingClient()

        with pytest.raises(TracingConfigurationError) as exc_info:
            assert_tracing_healthy(settings, client=client)

        assert "LANGSMITH_WORKSPACE_ID is unset" in str(exc_info.value)


class TestDiagnosticMessagesNeverLeakKeyMaterial:
    def test_a_key_shaped_string_in_the_underlying_error_is_redacted(self) -> None:
        class _LeakyClient(_CallRecordingClient):
            def create_run(self, **kwargs: Any) -> None:
                super().create_run(**kwargs)
                raise RuntimeError(
                    "unexpected server response, Authorization: Bearer "
                    "lsv2_sk_abcdefghijklmnopqrstuvwxyz0123456789ABCD"
                )

        settings = _settings(langsmith_tracing=True, langsmith_api_key="fake-key-not-real")

        with pytest.raises(TracingConfigurationError) as exc_info:
            assert_tracing_healthy(settings, client=_LeakyClient())

        assert "lsv2_sk_" not in str(exc_info.value)
        assert "<redacted>" in str(exc_info.value)


@pytest.mark.live
@pytest.mark.skipif(
    not get_settings().langsmith_api_key,
    reason="LANGSMITH_API_KEY is not configured -- skipping the live LangSmith call",
)
class TestAssertTracingHealthyAgainstTheRealApi:
    """Proves the guard detects a real misconfiguration against the real LangSmith API,
    not just a fake double -- exactly what this milestone's own debugging session hit:
    a real, valid API key, wrong workspace id, bare 403 on every call.
    """

    def test_a_wrong_real_workspace_id_is_detected_against_the_real_api(self) -> None:
        real_settings = get_settings()
        broken = _settings(
            langsmith_tracing=True,
            langsmith_api_key=real_settings.langsmith_api_key,
            langsmith_endpoint=real_settings.langsmith_endpoint,
            langsmith_workspace_id="00000000-0000-0000-0000-000000000000",
            langsmith_project=real_settings.langsmith_project,
        )

        with pytest.raises(TracingConfigurationError) as exc_info:
            assert_tracing_healthy(broken)

        message = str(exc_info.value)
        assert "lsv2_sk" not in message
        assert "403" in message or "Forbidden" in message or "could not be read back" in message

    def test_the_real_probe_run_lands_with_correct_configuration(self) -> None:
        """The genuinely-happy path: real credentials, real endpoint, real workspace id --
        proves this guard is not just good at detecting failure, it also confirms
        success for real when configuration is actually correct.
        """
        real_settings = get_settings()

        # Should not raise.
        assert_tracing_healthy(real_settings)


def test_probe_run_id_is_unique_per_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sanity check that each invocation uses a fresh run id (never reuses one across
    calls) -- otherwise a stale prior run could make a broken configuration look
    healthy by accident.
    """
    monkeypatch.setattr("backend.observability.tracing.time.sleep", lambda _seconds: None)
    seen: set[UUID] = set()

    class _IdCapturingClient(_HealthyClient):
        def create_run(self, **kwargs: Any) -> None:
            super().create_run(**kwargs)
            seen.add(kwargs["id"])

    settings = _settings(langsmith_tracing=True, langsmith_api_key="fake-key-not-real")

    for _ in range(3):
        assert_tracing_healthy(settings, client=_IdCapturingClient())

    assert len(seen) == 3
