"""Unit tests for backend.security.rbac.RepositoryAuthorizer."""

from __future__ import annotations

import httpx
import pytest

from backend.security.rbac import RepositoryAuthorizer, RepositoryNotAuthorizedError


def _client(handler) -> httpx.Client:  # type: ignore[no-untyped-def]
    return httpx.Client(transport=httpx.MockTransport(handler))


class TestRepositoryAuthorizer:
    def test_installed_repository_returns_installation_id(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"id": 157972840, "repository_selection": "selected"})

        authorizer = RepositoryAuthorizer(app_id="1", http_client=_client(handler))
        installation_id = authorizer.authorize(owner="vladimir-mawla", repo="pr-review-agent-testbed", app_jwt="x")
        assert installation_id == 157972840

    def test_uninstalled_repository_raises_repository_not_authorized(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"message": "Not Found"})

        authorizer = RepositoryAuthorizer(app_id="1", http_client=_client(handler))
        with pytest.raises(RepositoryNotAuthorizedError) as exc_info:
            authorizer.authorize(owner="someone-else", repo="private-repo", app_jwt="x")
        assert exc_info.value.owner == "someone-else"
        assert exc_info.value.repo == "private-repo"

    def test_repeated_calls_for_the_same_repo_are_cached_not_re_resolved(self) -> None:
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(200, json={"id": 42, "repository_selection": "selected"})

        authorizer = RepositoryAuthorizer(app_id="1", http_client=_client(handler))
        first = authorizer.authorize(owner="acme", repo="widgets", app_jwt="x")
        second = authorizer.authorize(owner="acme", repo="widgets", app_jwt="x")

        assert first == second == 42
        assert call_count == 1

    def test_a_real_github_outage_propagates_as_github_auth_error_not_not_authorized(self) -> None:
        from backend.integrations.github_auth import GitHubAuthError

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"message": "Service Unavailable"})

        authorizer = RepositoryAuthorizer(app_id="1", http_client=_client(handler))
        with pytest.raises(GitHubAuthError):
            authorizer.authorize(owner="acme", repo="widgets", app_jwt="x")
