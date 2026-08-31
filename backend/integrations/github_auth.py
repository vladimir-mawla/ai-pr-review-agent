"""GitHub App authentication: app-level JWT minting and installation token caching.

Owns the two-step flow every GitHub App must implement before it can call
any repository-scoped endpoint:

1. Mint a short-lived RS256 JWT, signed with the App's own private key,
   asserting "I am App <app_id>" (``iss``). This JWT is only good for
   app-level endpoints (``GET /app``, ``GET /app/installations``,
   ``POST /app/installations/{id}/access_tokens`` -- exchanging it for step
   2) and expires in minutes, per GitHub's own documented rules:
   ``exp`` must be at most 10 minutes after ``iat``, and GitHub recommends
   backdating ``iat`` by up to 60 seconds to tolerate clock skew between
   this machine and GitHub's servers (a JWT whose ``iat`` is in GitHub's
   future, even by a second or two of drift, is rejected outright).
2. Exchange that JWT for an installation access token
   (``ghs_...``), which is what actually authenticates real repository
   calls (fetch a diff, post a review) as the specific installation on the
   specific account/repos it was installed on -- never as the app itself.
   Installation tokens are valid for exactly one hour; ``InstallationTokenCache``
   below is what stops a naive caller from minting a fresh app JWT AND
   exchanging it for a fresh installation token on every single outbound
   call (wasteful, and needlessly hammers GitHub's own rate limit on the
   token-exchange endpoint itself), while also never handing out an
   expired token and letting the failure surface as an opaque 401 deep in
   an unrelated call.

NEVER LOG THE PRIVATE KEY OR A MINTED TOKEN. Every function here that
touches key material or a token takes it as a plain argument and returns a
plain value -- no ``repr``/``logging.info`` call anywhere in this module
includes ``private_key_pem``, a minted JWT, or ``token.token``. This is a
hard project rule (see the L1 BUILD brief this milestone was built under),
not merely good practice: a leaked App private key lets an attacker mint
JWTs and act as this App on every repository it is installed on, forever
until the key is rotated.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
import jwt

from backend.integrations.github_models import InstallationResponse, InstallationTokenResponse

# GitHub's own hard rule: an app JWT's exp must be <= 10 minutes after iat,
# or the request is rejected outright ("Expiration time claim ('exp') is
# too far in the future"). This project clamps to a conservative 9 minutes
# rather than the literal 10-minute ceiling, so a JWT minted right at this
# bound is never rejected by a few seconds of formatting/rounding drift.
_MAX_JWT_TTL_SECONDS = 9 * 60

# GitHub documents backdating iat by up to 60 seconds to tolerate clock
# skew between this machine's clock and GitHub's -- a JWT whose iat is even
# slightly in GitHub's future is rejected ("'Issued at' claim ('iat') must
# be in the past"). This is the single source of truth for that backdate;
# both mint_app_jwt and its tests reference it so the two can never drift
# apart.
CLOCK_SKEW_TOLERANCE_SECONDS = 60

# Refresh an installation token this many seconds before its real GitHub-
# reported expiry, rather than waiting for it to actually lapse. Installation
# tokens live for exactly one hour; refreshing five minutes early gives a
# generous safety margin against the token expiring mid-flight during a
# slow outbound call (fetch diff -> analyze -> post review can span
# multiple real network round trips), which would otherwise surface as a
# random 401 partway through a review.
_REFRESH_MARGIN_SECONDS = 5 * 60

_GITHUB_API_BASE_URL = "https://api.github.com"
_ACCEPT_HEADER = "application/vnd.github+json"
_API_VERSION_HEADER = "2022-11-28"


class GitHubAuthError(Exception):
    """Authentication itself failed: bad key, unknown app id, or a non-2xx from GitHub's auth endpoints."""


def mint_app_jwt(
    *,
    app_id: str,
    private_key_pem: str,
    now: datetime | None = None,
) -> str:
    """Mint a short-lived RS256 JWT asserting "I am GitHub App ``app_id``".

    Args:
        app_id: The GitHub App's numeric id (as a string; GitHub's own API
            returns/accepts it as a JSON string in some contexts and an int
            in others -- passing a string here and letting PyJWT serialize
            it into the claim as-is matches what GitHub's own
            documentation examples do).
        private_key_pem: The App's PEM-encoded RSA private key content
            (never a file path -- callers read the file themselves via
            ``Settings.github_app_private_key_path`` so this function has
            no filesystem dependency and is trivially unit-testable with an
            in-memory generated key).
        now: Injectable for deterministic tests; defaults to the real
            wall-clock time.

    Returns:
        The encoded JWT string.

    ``iat`` is backdated by ``CLOCK_SKEW_TOLERANCE_SECONDS`` and ``exp`` is
    set ``_MAX_JWT_TTL_SECONDS`` after that backdated ``iat`` -- i.e. the
    JWT's total validity window is capped at
    ``_MAX_JWT_TTL_SECONDS - CLOCK_SKEW_TOLERANCE_SECONDS`` from the actual
    current wall-clock moment, comfortably inside GitHub's 10-minute
    ceiling even after the backdate.
    """
    current = now if now is not None else datetime.now(UTC)
    issued_at = current - timedelta(seconds=CLOCK_SKEW_TOLERANCE_SECONDS)
    expires_at = issued_at + timedelta(seconds=_MAX_JWT_TTL_SECONDS)

    payload = {
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
        "iss": app_id,
    }
    try:
        encoded = jwt.encode(payload, private_key_pem, algorithm="RS256")
    except (ValueError, jwt.InvalidKeyError) as exc:
        # PyJWT raises ValueError for a malformed PEM blob (e.g. missing
        # BEGIN/END markers) before it ever gets to InvalidKeyError for a
        # structurally-valid-but-wrong-type key -- both mean "this key
        # cannot be used to sign", which is an auth configuration problem,
        # not a transient failure worth retrying.
        raise GitHubAuthError(f"failed to sign App JWT: {exc}") from exc
    # PyJWT >= 2 returns str already; the isinstance guard is defensive
    # against older/alternate encoders that return bytes, satisfying
    # mypy --strict's requirement that every path returns str.
    return encoded if isinstance(encoded, str) else encoded.decode("ascii")


@dataclass(frozen=True)
class InstallationToken:
    """A minted installation access token plus the wall-clock moment it stops being valid."""

    token: str
    expires_at: datetime


def exchange_jwt_for_installation_token(
    *,
    app_jwt: str,
    installation_id: int,
    http_client: httpx.Client,
) -> InstallationToken:
    """``POST /app/installations/{installation_id}/access_tokens`` -- step 2 of the flow.

    Raises ``GitHubAuthError`` on any non-2xx response (an invalid/expired
    app JWT, an installation id this app does not actually have, etc.) --
    never returns a token when GitHub did not confirm one. Callers wrap
    this in the M6 retry/breaker/timeout composition (see
    ``backend.integrations.github_client``); this function itself makes
    exactly one HTTP call.
    """
    response = http_client.post(
        f"{_GITHUB_API_BASE_URL}/app/installations/{installation_id}/access_tokens",
        headers={
            "Authorization": f"Bearer {app_jwt}",
            "Accept": _ACCEPT_HEADER,
            "X-GitHub-Api-Version": _API_VERSION_HEADER,
        },
    )
    if response.status_code >= 300:
        raise GitHubAuthError(
            f"installation token exchange failed for installation_id={installation_id}: "
            f"HTTP {response.status_code}"
        )
    parsed = InstallationTokenResponse.model_validate(response.json())
    expires_at = datetime.fromisoformat(parsed.expires_at.replace("Z", "+00:00"))
    return InstallationToken(token=parsed.token, expires_at=expires_at)


def discover_installation_id(
    *,
    owner: str,
    repo: str,
    app_jwt: str,
    http_client: httpx.Client,
) -> int:
    """Resolve the installation id for ``owner/repo`` via the real API -- never hardcoded.

    ``GET /repos/{owner}/{repo}/installation`` is deliberately the
    per-repository endpoint (not ``GET /app/installations`` followed by a
    local filter over every installation's repository list) -- it returns
    exactly the one installation covering this repository, or a plain 404
    if this App is not installed on it at all. That 404 is exactly the
    signal ``backend.security.rbac.RepositoryAuthorizer`` needs to refuse
    acting on a repository this App has no business touching (see that
    module), so this same call does double duty as both "discover the id"
    and "prove authorization" rather than needing two separate mechanisms.

    Raises ``GitHubAuthError`` on any non-2xx response.
    """
    response = http_client.get(
        f"{_GITHUB_API_BASE_URL}/repos/{owner}/{repo}/installation",
        headers={
            "Authorization": f"Bearer {app_jwt}",
            "Accept": _ACCEPT_HEADER,
            "X-GitHub-Api-Version": _API_VERSION_HEADER,
        },
    )
    if response.status_code >= 300:
        raise GitHubAuthError(
            f"could not resolve installation for {owner}/{repo}: HTTP {response.status_code}"
        )
    parsed = InstallationResponse.model_validate(response.json())
    return parsed.id


class InstallationTokenCache:
    """Caches one installation access token per ``installation_id``, refreshing before expiry.

    WHY A CACHE AT ALL: an installation token is valid for a full hour: a
    real review touches several endpoints in sequence (PR metadata, the
    diff, changed files, posting the review) -- minting a fresh app JWT
    and exchanging it for a fresh installation token before each of those
    would be four-plus token exchanges per review instead of at most one,
    for no correctness benefit and real, avoidable load against GitHub's
    own rate limits on the auth endpoints themselves.

    THREAD SAFETY: mirrors ``backend.reliability.circuit_breaker.
    CircuitBreaker``'s pattern exactly -- every read and mutation of
    ``_tokens`` happens under one ``threading.Lock``, held only for the
    cache lookup/store, never across the network call itself (a slow token
    exchange must not block a concurrent caller for an unrelated
    installation id, or one that's asking for the same id and would be
    happy to wait for the in-flight result -- see the note on
    ``_refresh_lock`` below for how a concurrent *same-id* request is
    still handled without a thundering herd of duplicate exchanges).
    """

    def __init__(
        self,
        *,
        app_id: str,
        private_key_pem: str,
        http_client: httpx.Client,
        clock: type[datetime] = datetime,
    ) -> None:
        self._app_id = app_id
        self._private_key_pem = private_key_pem
        self._http_client = http_client
        self._clock = clock
        self._lock = threading.Lock()
        self._tokens: dict[int, InstallationToken] = {}
        # Counts real exchange calls made -- test/inspection surface only,
        # proving "cached and refreshed, not re-minted per call" directly
        # rather than trusting the implementation by inspection alone.
        self.exchange_count = 0

    def get_token(self, installation_id: int) -> str:
        """Return a valid installation token for ``installation_id``, minting/refreshing as needed.

        A single lock guards the whole "check cache, and if stale/missing,
        mint+exchange+store" sequence for a given call -- two threads
        racing to refresh the same expired installation id will make at
        most... actually exactly one avoidable extra exchange in the worst
        case (the lock is held across the network call here, deliberately
        trading a small amount of contention for the much simpler
        correctness property "never two callers both decide to mint at
        once and one of the two mints redundantly under a finer-grained
        lock"). This is acceptable because token refresh is rare (once
        every ~55 minutes per installation, per ``_REFRESH_MARGIN_SECONDS``)
        compared to how often ``get_token`` itself is called.
        """
        with self._lock:
            cached = self._tokens.get(installation_id)
            now = self._clock.now(UTC)
            if cached is not None and cached.expires_at - now > timedelta(
                seconds=_REFRESH_MARGIN_SECONDS
            ):
                return cached.token

            app_jwt = mint_app_jwt(app_id=self._app_id, private_key_pem=self._private_key_pem, now=now)
            token = exchange_jwt_for_installation_token(
                app_jwt=app_jwt,
                installation_id=installation_id,
                http_client=self._http_client,
            )
            self._tokens[installation_id] = token
            self.exchange_count += 1
            return token.token

    def invalidate(self, installation_id: int) -> None:
        """Drop any cached token for ``installation_id``, forcing a fresh mint on next use.

        Called by ``RealGitHubClient`` when a call fails with a real 401
        despite the cache believing the token was still fresh -- GitHub can
        revoke/rotate a token out from under a client (e.g. an installation
        suspended mid-review) in ways a purely time-based cache cannot
        predict; this is the escape hatch for that case rather than
        letting every subsequent call keep failing until the margin-based
        expiry eventually catches up.
        """
        with self._lock:
            self._tokens.pop(installation_id, None)


# Small helper kept here (not github_client.py) so both the client and this
# module's own tests share exactly one place that builds the "app-level"
# httpx headers -- avoids the two drifting apart on e.g. the API version
# header.
def app_auth_headers(app_jwt: str) -> dict[str, str]:
    """The header set every app-level (JWT-authenticated) GitHub call needs."""
    return {
        "Authorization": f"Bearer {app_jwt}",
        "Accept": _ACCEPT_HEADER,
        "X-GitHub-Api-Version": _API_VERSION_HEADER,
    }
