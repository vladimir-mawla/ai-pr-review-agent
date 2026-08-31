"""Unit tests for backend.integrations.github_auth.

No network access, no real GitHub App -- every test either signs a JWT with
a locally-generated throwaway RSA key (never a real App's key) or drives
``InstallationTokenCache``/the exchange functions against a fake
``httpx.MockTransport``. This is what lets these tests run in the default,
free ``pytest`` invocation: nothing here is marked ``live``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from backend.integrations.github_auth import (
    CLOCK_SKEW_TOLERANCE_SECONDS,
    GitHubAuthError,
    InstallationTokenCache,
    discover_installation_id,
    exchange_jwt_for_installation_token,
    mint_app_jwt,
)


@pytest.fixture(scope="module")
def throwaway_private_key_pem() -> str:
    """A locally-generated RSA key, never a real GitHub App's key -- module-scoped since key gen is slow."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pem.decode("ascii")


@pytest.fixture(scope="module")
def throwaway_public_key_pem(throwaway_private_key_pem: str) -> bytes:
    private_key = serialization.load_pem_private_key(
        throwaway_private_key_pem.encode("ascii"), password=None
    )
    public_key = private_key.public_key()  # type: ignore[union-attr]
    return public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


class TestMintAppJwt:
    """JWT is well-formed: correct alg, iss, exp bounds; exp is always clamped <= 10 minutes."""

    def test_jwt_uses_rs256_and_carries_the_app_id_as_issuer(
        self, throwaway_private_key_pem: str, throwaway_public_key_pem: bytes
    ) -> None:
        token = mint_app_jwt(app_id="4781442", private_key_pem=throwaway_private_key_pem)

        header = jwt.get_unverified_header(token)
        assert header["alg"] == "RS256"

        payload = jwt.decode(
            token, throwaway_public_key_pem, algorithms=["RS256"], options={"verify_exp": False, "verify_iat": False}
        )
        assert payload["iss"] == "4781442"

    def test_iat_is_backdated_for_clock_skew_tolerance(
        self, throwaway_private_key_pem: str, throwaway_public_key_pem: bytes
    ) -> None:
        now = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)
        token = mint_app_jwt(app_id="1", private_key_pem=throwaway_private_key_pem, now=now)
        payload = jwt.decode(
            token, throwaway_public_key_pem, algorithms=["RS256"], options={"verify_exp": False, "verify_iat": False}
        )

        expected_iat = int((now - timedelta(seconds=CLOCK_SKEW_TOLERANCE_SECONDS)).timestamp())
        assert payload["iat"] == expected_iat
        # iat must be strictly in the past relative to `now` -- GitHub
        # rejects a JWT whose iat is in ITS future, and clock skew between
        # this machine and GitHub's servers is exactly what the backdate
        # exists to absorb.
        assert payload["iat"] < int(now.timestamp())

    def test_exp_is_always_clamped_within_githubs_ten_minute_ceiling(
        self, throwaway_private_key_pem: str, throwaway_public_key_pem: bytes
    ) -> None:
        """GitHub rejects any App JWT whose (exp - iat) exceeds 10 minutes.

        mint_app_jwt exposes no caller-supplied `exp` (by design -- see its
        docstring): every JWT it mints is clamped to a fixed, conservative
        window, so "a >10min exp is rejected/clamped" is proven here by
        showing the function can NEVER produce one, for any `now`, rather
        than by exercising a since-removed override path.
        """
        for now in (
            datetime(2020, 1, 1, tzinfo=UTC),
            datetime(2026, 8, 31, 23, 59, 59, tzinfo=UTC),
            datetime(2099, 12, 31, tzinfo=UTC),
        ):
            token = mint_app_jwt(app_id="1", private_key_pem=throwaway_private_key_pem, now=now)
            payload = jwt.decode(
                token, throwaway_public_key_pem, algorithms=["RS256"], options={"verify_exp": False, "verify_iat": False}
            )
            ttl_seconds = payload["exp"] - payload["iat"]
            assert ttl_seconds <= 600, f"exp-iat={ttl_seconds}s exceeds GitHub's 10-minute ceiling"
            assert ttl_seconds > 0

    def test_malformed_private_key_raises_github_auth_error(self) -> None:
        with pytest.raises(GitHubAuthError):
            mint_app_jwt(app_id="1", private_key_pem="not a real PEM key")


def _mock_transport(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


class TestExchangeJwtForInstallationToken:
    def test_successful_exchange_returns_token_and_parsed_expiry(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/app/installations/157972840/access_tokens"
            assert request.headers["Authorization"] == "Bearer fake-jwt"
            return httpx.Response(
                201,
                json={"token": "ghs_abc123", "expires_at": "2026-08-31T15:00:00Z"},
            )

        client = _mock_transport(handler)
        result = exchange_jwt_for_installation_token(
            app_jwt="fake-jwt", installation_id=157972840, http_client=client
        )
        assert result.token == "ghs_abc123"
        assert result.expires_at == datetime(2026, 8, 31, 15, 0, 0, tzinfo=UTC)

    def test_non_2xx_raises_github_auth_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"message": "Not Found"})

        client = _mock_transport(handler)
        with pytest.raises(GitHubAuthError):
            exchange_jwt_for_installation_token(
                app_jwt="fake-jwt", installation_id=1, http_client=client
            )


class TestDiscoverInstallationId:
    def test_resolves_installation_id_for_repo(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/repos/acme/widgets/installation"
            return httpx.Response(200, json={"id": 999, "repository_selection": "selected"})

        client = _mock_transport(handler)
        installation_id = discover_installation_id(
            owner="acme", repo="widgets", app_jwt="fake-jwt", http_client=client
        )
        assert installation_id == 999

    def test_404_raises_github_auth_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"message": "Not Found"})

        client = _mock_transport(handler)
        with pytest.raises(GitHubAuthError, match="HTTP 404"):
            discover_installation_id(owner="acme", repo="widgets", app_jwt="x", http_client=client)


class TestInstallationTokenCache:
    """Installation token is cached and refreshed, not re-minted per call."""

    def test_repeated_calls_for_the_same_installation_reuse_the_cached_token(
        self, throwaway_private_key_pem: str
    ) -> None:
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(
                201, json={"token": f"ghs_{call_count}", "expires_at": "2099-01-01T00:00:00Z"}
            )

        client = _mock_transport(handler)
        cache = InstallationTokenCache(
            app_id="1", private_key_pem=throwaway_private_key_pem, http_client=client
        )

        first = cache.get_token(157972840)
        second = cache.get_token(157972840)
        third = cache.get_token(157972840)

        assert first == second == third == "ghs_1"
        assert cache.exchange_count == 1
        assert call_count == 1

    def test_a_token_close_to_expiry_is_refreshed_not_reused(
        self, throwaway_private_key_pem: str
    ) -> None:
        """Proves the cache actually refreshes -- not merely that it caches forever."""
        responses = iter(
            [
                {"token": "ghs_first", "expires_at": "2026-01-01T00:04:00Z"},
                {"token": "ghs_second", "expires_at": "2099-01-01T00:00:00Z"},
            ]
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(201, json=next(responses))

        client = _mock_transport(handler)
        # A fixed, injectable "now" just under the first token's expires_at
        # minus the refresh margin, so the cache should treat it as stale
        # on the second call without needing to actually wait an hour.
        fixed_now = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)

        class _FixedClock(datetime):
            @classmethod
            def now(cls, tz=None):  # type: ignore[override]
                return fixed_now if tz is None else fixed_now.astimezone(tz)

        cache = InstallationTokenCache(
            app_id="1",
            private_key_pem=throwaway_private_key_pem,
            http_client=client,
            clock=_FixedClock,
        )
        first = cache.get_token(1)
        second = cache.get_token(1)

        assert first == "ghs_first"
        # Only 4 minutes of validity remained (< the 5-minute refresh
        # margin), so the second call must have minted a fresh token
        # rather than returning the stale one.
        assert second == "ghs_second"
        assert cache.exchange_count == 2

    def test_different_installations_are_cached_independently(
        self, throwaway_private_key_pem: str
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            installation_id = request.url.path.split("/")[3]
            return httpx.Response(
                201,
                json={"token": f"ghs_{installation_id}", "expires_at": "2099-01-01T00:00:00Z"},
            )

        client = _mock_transport(handler)
        cache = InstallationTokenCache(
            app_id="1", private_key_pem=throwaway_private_key_pem, http_client=client
        )

        token_a = cache.get_token(111)
        token_b = cache.get_token(222)
        token_a_again = cache.get_token(111)

        assert token_a == "ghs_111"
        assert token_b == "ghs_222"
        assert token_a_again == "ghs_111"
        assert cache.exchange_count == 2

    def test_invalidate_forces_a_fresh_mint_on_next_use(self, throwaway_private_key_pem: str) -> None:
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(
                201, json={"token": f"ghs_{call_count}", "expires_at": "2099-01-01T00:00:00Z"}
            )

        client = _mock_transport(handler)
        cache = InstallationTokenCache(
            app_id="1", private_key_pem=throwaway_private_key_pem, http_client=client
        )

        first = cache.get_token(1)
        cache.invalidate(1)
        second = cache.get_token(1)

        assert first == "ghs_1"
        assert second == "ghs_2"
        assert cache.exchange_count == 2
