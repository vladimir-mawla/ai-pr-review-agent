"""Repository-level authorization: is this GitHub App actually installed here?

Owns the one access-control question M11 introduces that M1-M10 never had
to ask: this project's GitHub App has real write permissions
(``pull_requests: write``) that can act on ANY repository someone installs
it on -- before ``RealGitHubClient`` does anything real (fetch a diff, post
a review) for a given ``(owner, repo)``, something must confirm this App is
actually installed there. Without that check, a bug elsewhere that let a
caller pass an arbitrary owner/repo through to the client would have no
structural barrier stopping it from attempting a real GitHub API call
against a repository this project has no business touching.

WHY THIS DOUBLES AS INSTALLATION DISCOVERY, NOT A SEPARATE ALLOWLIST:
GitHub's own ``GET /repos/{owner}/{repo}/installation`` endpoint already IS
the authoritative answer to "is App X installed on this repo, and if so
what is the installation id" -- a 404 means no, a 200 means yes-and-here's-
the-id. Maintaining a second, local allowlist of "repos we're allowed to
touch" alongside that would be exactly the kind of redundant, driftable
second source of truth this project's own history warns against (see
``backend.models.enums.Severity``'s docstring on ``SEVERITY_RANK`` for the
same principle applied elsewhere). ``RepositoryAuthorizer`` is a thin,
named wrapper around that real API call specifically so the *security
intent* ("gate every real action on this check") is visible and testable
as its own unit, independent of ``backend.integrations.github_auth.
discover_installation_id``'s own lower-level HTTP concern.
"""

from __future__ import annotations

import httpx

from backend.integrations.github_auth import GitHubAuthError, discover_installation_id


class RepositoryNotAuthorizedError(Exception):
    """This App is not installed on the requested repository.

    Distinct from ``GitHubAuthError`` (a credential/network problem talking
    to GitHub at all) -- this means the call to GitHub *succeeded* and
    GitHub's own answer was "no installation here". A caller catching this
    specifically should refuse to proceed with any repository action,
    exactly the way an HTTP layer would refuse a request lacking
    permission, rather than retrying (retrying can never turn "not
    installed" into "installed").
    """

    def __init__(self, owner: str, repo: str) -> None:
        super().__init__(
            f"GitHub App is not installed on {owner}/{repo} (or does not have "
            "access to it) -- refusing to act on this repository"
        )
        self.owner = owner
        self.repo = repo


class RepositoryAuthorizer:
    """Resolves and caches the installation id authorized for each ``(owner, repo)``.

    One instance is owned by ``RealGitHubClient`` (see that module) and
    reused across calls within a process -- the installation id for a given
    repository does not change on any timescale this project cares about
    (re-installing/uninstalling the App is an out-of-band admin action, not
    something that happens mid-review), so re-resolving it on every single
    outbound call would be pure, avoidable API load for no correctness
    benefit. Unlike ``InstallationTokenCache`` (which must expire hourly by
    GitHub's own rule), this cache has no TTL -- a stale entry can only
    ever be WRONG in the direction of "this repo used to be authorized and
    no longer is", which the eventual real call itself will surface as a
    401/404 from GitHub once the (by-then-invalid) cached installation id
    is used to request a token.
    """

    def __init__(self, *, app_id: str, http_client: httpx.Client) -> None:
        self._app_id = app_id
        self._http_client = http_client
        self._resolved: dict[tuple[str, str], int] = {}

    def authorize(self, *, owner: str, repo: str, app_jwt: str) -> int:
        """Return the installation id authorized for ``owner/repo``, or raise.

        Raises:
            RepositoryNotAuthorizedError: GitHub confirmed this App has no
                installation covering ``owner/repo`` (a 404 from the
                underlying discovery call).
            GitHubAuthError: any other non-2xx response (a genuinely
                malformed/expired app JWT, a GitHub outage, etc.) --
                propagated unchanged so the caller's own retry/circuit-
                breaker composition can decide whether it's transient.
        """
        cache_key = (owner, repo)
        cached = self._resolved.get(cache_key)
        if cached is not None:
            return cached

        try:
            installation_id = discover_installation_id(
                owner=owner, repo=repo, app_jwt=app_jwt, http_client=self._http_client
            )
        except GitHubAuthError as exc:
            # discover_installation_id raises GitHubAuthError uniformly for
            # every non-2xx status, including 404 -- narrow specifically to
            # "not found" here so a real outage/expired-JWT case (which
            # should propagate as GitHubAuthError, letting the reliability
            # layer classify retryability) is not miscast as "not
            # authorized" and vice versa.
            if "HTTP 404" in str(exc):
                raise RepositoryNotAuthorizedError(owner, repo) from exc
            raise

        self._resolved[cache_key] = installation_id
        return installation_id
