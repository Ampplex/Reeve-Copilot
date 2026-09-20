from __future__ import annotations

import time
from collections import defaultdict, deque
from contextvars import ContextVar
from typing import Any

import jwt
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from starlette.datastructures import Headers
from starlette.responses import JSONResponse

from threelane_memory.config import (
    DEFAULT_PLAN,
    DEV_AUTH_ENABLED,
    GOOGLE_CLIENT_ID,
    OAUTH_EXPECTED_ISSUER,
    OAUTH_JWT_SECRET,
    OAUTH_TOKEN_EXPIRY_SECONDS,
    PLAN_LIMITS,
    RATE_LIMIT_WINDOW_SECONDS,
)
from threelane_memory.database import (
    get_oauth_client,
    get_user_by_api_key,
    get_user_plan,
    upsert_user,
)

# Context variable to store the current authenticated user ID
current_user_id: ContextVar[str | None] = ContextVar("current_user_id", default=None)
# Google profile claims (email/name/picture) captured when a Google token is
# verified, so downstream handlers (e.g. /auth/verify) can mirror them to
# Supabase without re-decoding the token. None for non-Google auth paths.
current_user_profile: ContextVar[dict[str, Any] | None] = ContextVar(
    "current_user_profile", default=None
)
SESSION_API_AUDIENCE = "reeve_session_api"


def _dev_auth_allowed() -> bool:
    """Dev impersonation tokens (``Bearer dev-user-*``) are a backdoor.

    Only honor them when explicitly enabled *and* the server is not configured
    with a public https issuer, so an accidental ``DEV_AUTH_ENABLED=true`` in a
    production deployment cannot let anyone impersonate any user.
    """
    if not DEV_AUTH_ENABLED:
        return False
    issuer = (OAUTH_EXPECTED_ISSUER or "").lower()
    if issuer.startswith("https://"):
        host = issuer.split("://", 1)[1].split("/", 1)[0].split(":", 1)[0]
        if host not in {"localhost", "127.0.0.1", "::1"}:
            return False
    return True


GOOGLE_UID_PREFIX = "google-oauth2|"


def google_uid(subject: str | None) -> str | None:
    """Namespace a Google account id into Reeve's account identifier.

    Google's ``sub`` is unique per Google account but says nothing about which
    identity provider it came from, so it is namespaced before it becomes an
    account key. Every entry point that learns a *raw provider subject* must go
    through here; identifiers that are already Reeve account ids (internal JWT
    ``sub``, API-key lookups) are passed through untouched by their callers.

    Idempotent, so re-normalizing an already-namespaced id is safe.

    NOTE for adding a second provider (GitHub, Microsoft, …): give it its own
    prefix rather than reusing this one. Two providers can legitimately issue
    the same ``sub`` string, and a shared prefix would collapse two different
    people into one account — the failure mode this namespacing exists to
    prevent.
    """
    if not subject:
        return None
    subject = str(subject)
    if subject.startswith(GOOGLE_UID_PREFIX):
        return subject
    return f"{GOOGLE_UID_PREFIX}{subject}"


def issue_session_api_key(user_id: str, issuer: str) -> tuple[str, int]:
    """Issue a login-session bearer token usable by frontend API calls."""
    now = int(time.time())
    expires_in = OAUTH_TOKEN_EXPIRY_SECONDS
    token = jwt.encode(
        {
            "sub": str(user_id),
            "aud": SESSION_API_AUDIENCE,
            "iss": issuer.rstrip("/"),
            "exp": now + expires_in,
            "iat": now - 60,
        },
        OAUTH_JWT_SECRET,
        algorithm="HS256",
    )
    return token, expires_in


class InMemRateLimiter:
    """Simple in-memory rate limiter using a sliding window of timestamps."""

    def __init__(self, window: int):
        self.window = window
        self.history: dict[str, deque[float]] = defaultdict(deque)

    def is_allowed(self, user_id: str, limit: int) -> bool:
        now = time.time()
        user_history = self.history[user_id]

        # Remove timestamps outside the window (O(1) popleft on a deque)
        cutoff = now - self.window
        while user_history and user_history[0] < cutoff:
            user_history.popleft()

        if len(user_history) < limit:
            user_history.append(now)
            return True
        # Drop the bucket if it drained to empty so idle keys don't accumulate
        # unboundedly (memory-exhaustion vector under high-cardinality abuse).
        if not user_history:
            self.history.pop(user_id, None)
        return False


# Global rate limiter instance (keyed by authenticated user id)
rate_limiter = InMemRateLimiter(RATE_LIMIT_WINDOW_SECONDS)

# Per-IP limiter for UNauthenticated OAuth endpoints (DCR/authorize/token). These
# are public by spec, so throttle them to prevent unbounded OAuthClient node
# creation and brute-force of auth codes.
UNAUTH_OAUTH_PATHS = ("/register", "/authorize", "/token")
UNAUTH_OAUTH_RATE_LIMIT = 30
unauth_rate_limiter = InMemRateLimiter(RATE_LIMIT_WINDOW_SECONDS)


def _client_ip(scope: dict) -> str:
    client = scope.get("client")
    if client and client[0]:
        return str(client[0])
    return "unknown"


class AuthMiddleware:
    """ASGI middleware for Google OAuth 2.0 verification, API keys, and rate limiting."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")

        # Throttle unauthenticated OAuth endpoints per client IP before doing any
        # work, so they can't be used to flood the graph with client/auth-code
        # nodes or brute-force authorization codes.
        if path in UNAUTH_OAUTH_PATHS:
            if not unauth_rate_limiter.is_allowed(_client_ip(scope), UNAUTH_OAUTH_RATE_LIMIT):
                response = JSONResponse(
                    {"error": "rate_limited", "error_description": "Too many requests"},
                    status_code=429,
                )
                await response(scope, receive, send)
                return

        if not _is_protected_path(path):
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        base_url = _build_base_url(scope, headers)
        auth_header = headers.get("authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            metadata_url = _protected_resource_metadata_url(base_url, path)
            response = JSONResponse(
                {"error": "Missing or invalid Authorization header"},
                status_code=401,
                headers={
                    "WWW-Authenticate": (
                        f'Bearer realm="mcp", resource_metadata="{metadata_url}", scope="mcp:all"'
                    )
                },
            )
            await response(scope, receive, send)
            return

        token = auth_header[7:]

        user_id = None
        plan = DEFAULT_PLAN
        profile: dict[str, Any] | None = None

        # 1. Try as an internal OAuth JWT first
        try:
            expected_issuer = OAUTH_EXPECTED_ISSUER or base_url
            unverified_payload = jwt.decode(token, options={"verify_signature": False})
            audience = unverified_payload.get("aud")
            if not _is_registered_oauth_audience(audience):
                raise jwt.InvalidAudienceError("Unregistered OAuth audience")
            payload = jwt.decode(
                token,
                OAUTH_JWT_SECRET,
                algorithms=["HS256"],
                audience=audience,
                issuer=expected_issuer,
                options={
                    "require": ["sub", "aud", "iss", "exp", "iat"],
                },
            )
            user_id = payload.get("sub")
            if user_id:
                plan = get_user_plan(user_id)
            else:
                user_id = None
        except jwt.ExpiredSignatureError:
            response = JSONResponse(
                {"error": "token_expired", "error_description": "The access token has expired"},
                status_code=401,
            )
            await response(scope, receive, send)
            return
        except jwt.InvalidTokenError:
            pass
        except Exception:
            pass

        if not user_id:
            # 2. Try as a permanent API Key
            user_info = get_user_by_api_key(token)
            if user_info:
                user_id = user_info["uid"]
                plan = user_info["plan"]
            else:
                # 3. Try as a Google ID token or Access token or Dev token
                try:
                    if _dev_auth_allowed() and token.startswith("dev-user-"):
                        user_id = token
                        email = None
                    else:
                        if not GOOGLE_CLIENT_ID:
                            raise ValueError("Google OAuth is not configured")
                        try:
                            # a) Try as ID Token first (JWT)
                            id_info = id_token.verify_oauth2_token(
                                token, google_requests.Request(), GOOGLE_CLIENT_ID
                            )
                            user_id = google_uid(id_info["sub"])
                            email = id_info.get("email")
                            profile = {
                                "email": email,
                                "name": id_info.get("name"),
                                "picture": id_info.get("picture"),
                            }
                        except Exception:
                            # b) Opaque access token.
                            #
                            # Checked with tokeninfo, NOT userinfo. userinfo
                            # answers for ANY valid Google access token
                            # whichever application it was minted for, so it
                            # tells us the caller is somebody's real Google
                            # account and nothing about whether that token was
                            # ever meant for us. Any third-party app the user
                            # has signed into holds such a token, and its
                            # operator could replay it here for a full session
                            # as that user.
                            #
                            # tokeninfo returns the audience — the client the
                            # token was issued to — which is the missing check.
                            # The ID-token path above has always had it; this
                            # path is the one that did not.
                            import requests

                            resp = requests.get(
                                "https://oauth2.googleapis.com/tokeninfo",
                                params={"access_token": token},
                                timeout=5,
                            )
                            if resp.status_code != 200:
                                raise Exception(
                                    "Token is neither a valid ID token nor access token"
                                )

                            token_info = resp.json()
                            # `aud` for an access token is the client id it was
                            # issued to. Compared exactly, and against the same
                            # single configured client the ID-token path uses.
                            if token_info.get("aud") != GOOGLE_CLIENT_ID:
                                raise Exception(
                                    "Access token was issued for a different application"
                                )

                            subject = token_info.get("sub")
                            if not subject:
                                raise Exception("Access token identifies no account")

                            user_id = google_uid(subject)
                            email = token_info.get("email")

                            # Display fields only, and only now that the token
                            # has been established as ours. A failure here must
                            # not fail the sign-in: a missing avatar is not an
                            # authentication problem.
                            profile = {"email": email, "name": None, "picture": None}
                            try:
                                info_resp = requests.get(
                                    "https://www.googleapis.com/oauth2/v3/userinfo",
                                    headers={"Authorization": f"Bearer {token}"},
                                    timeout=5,
                                )
                                if info_resp.status_code == 200:
                                    user_data = info_resp.json()
                                    profile = {
                                        "email": email or user_data.get("email"),
                                        "name": user_data.get("name"),
                                        "picture": user_data.get("picture"),
                                    }
                            except Exception:
                                pass

                    # Sync user and get plan
                    upsert_user(user_id, email=email)
                    plan = get_user_plan(user_id)
                except Exception:
                    response = JSONResponse({"error": "Invalid token or key"}, status_code=401)
                    await response(scope, receive, send)
                    return

        # 4. Enforce rate limits (skip SSE handshake to avoid reconnect storms)
        if not _is_sse_path(path):
            limit = PLAN_LIMITS.get(plan, PLAN_LIMITS[DEFAULT_PLAN])
            if not rate_limiter.is_allowed(user_id, limit):
                response = JSONResponse(
                    {
                        "error": (
                            f"Rate limit exceeded for {plan} plan "
                            f"({limit} req/{RATE_LIMIT_WINDOW_SECONDS}s)"
                        )
                    },
                    status_code=429,
                )
                await response(scope, receive, send)
                return

        ctx_token = current_user_id.set(user_id)
        profile_ctx_token = current_user_profile.set(profile)
        try:
            await self.app(scope, receive, send)
        finally:
            current_user_id.reset(ctx_token)
            current_user_profile.reset(profile_ctx_token)


def _is_protected_path(path: str) -> bool:
    if path in ("/sse", "/sse/"):
        return True
    if path.startswith("/messages"):
        return True
    if path.startswith("/dashboard/"):
        return True
    # /account/ carries the erasure endpoint. Unauthenticated it would let anyone
    # delete anyone's memories, so this must be added BEFORE any /account/ route
    # is registered — hence the test in tests/test_security_hardening.py.
    if path.startswith("/account/"):
        return True
    if path.startswith("/billing/") and path != "/billing/webhook":
        return True
    return path in ("/auth/verify", "/auth/revoke-api-key", "/oauth/consent")


def _is_sse_path(path: str) -> bool:
    return path in ("/sse", "/sse/")


def _protected_resource_metadata_url(base_url: str, path: str) -> str:
    if path in ("/sse", "/sse/") or path.startswith("/messages"):
        return f"{base_url}/.well-known/oauth-protected-resource/sse"
    return f"{base_url}/.well-known/oauth-protected-resource"


def _is_registered_oauth_audience(audience: Any) -> bool:
    """Validate that an internal OAuth token audience maps to a registered client."""
    audiences = audience if isinstance(audience, list) else [audience]
    for value in audiences:
        if value == SESSION_API_AUDIENCE:
            return True
        if isinstance(value, str) and get_oauth_client(value):
            return True
    return False


def _build_base_url(scope: dict, headers: Headers) -> str:
    scheme = scope.get("scheme") or "http"
    host = headers.get("host")
    if not host:
        server = scope.get("server")
        if server:
            host = server[0]
            port = server[1]
            if port and port not in (80, 443):
                host = f"{host}:{port}"
    if not host:
        host = "localhost"
    return f"{scheme}://{host}"
