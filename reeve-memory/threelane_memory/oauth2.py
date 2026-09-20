"""OAuth 2.1 implementation for MCP discovery and authorization."""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import time
from urllib.parse import urlencode, urlparse

import jwt
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

from threelane_memory.config import (
    OAUTH_ALLOWED_REDIRECT_HOSTS,
    OAUTH_EXPECTED_ISSUER,
    OAUTH_JWT_SECRET,
)
from threelane_memory.database import (
    consume_auth_code,
    create_auth_code,
    create_oauth_client,
    get_oauth_client,
)

LOOPBACK_REDIRECT_HOSTS = {"localhost", "127.0.0.1", "::1"}
CLAUDE_HOSTED_REDIRECT_URI = "https://claude.ai/api/mcp/auth_callback"
DEFAULT_MCP_RESOURCE_PATH = "/sse"

logger = logging.getLogger(__name__)


def _oauth_issuer(request: Request) -> str:
    return OAUTH_EXPECTED_ISSUER or str(request.base_url).rstrip("/")


def _is_allowed_redirect_uri(redirect_uri: str) -> bool:
    try:
        parsed = urlparse(redirect_uri)
    except Exception:
        return False

    if parsed.fragment or parsed.username or parsed.password:
        return False

    hostname = (parsed.hostname or "").lower()
    if not parsed.scheme or not hostname:
        return False

    if parsed.scheme == "http":
        return hostname in LOOPBACK_REDIRECT_HOSTS

    if parsed.scheme != "https":
        return False

    claude_callback = urlparse(CLAUDE_HOSTED_REDIRECT_URI)
    if (
        hostname == claude_callback.hostname
        and parsed.path.rstrip("/") == claude_callback.path
        and not parsed.query
    ):
        return True

    return hostname in OAUTH_ALLOWED_REDIRECT_HOSTS


def _redirect_uri_matches(registered_uri: str, requested_uri: str) -> bool:
    if registered_uri.rstrip("/") == requested_uri.rstrip("/"):
        return True

    try:
        registered = urlparse(registered_uri)
        requested = urlparse(requested_uri)
    except Exception:
        return False

    registered_host = (registered.hostname or "").lower()
    requested_host = (requested.hostname or "").lower()
    if registered.scheme != "http" or requested.scheme != "http":
        return False
    if (
        registered_host not in LOOPBACK_REDIRECT_HOSTS
        or requested_host not in LOOPBACK_REDIRECT_HOSTS
    ):
        return False
    if registered_host != requested_host:
        return False

    registered_path = (registered.path or "/").rstrip("/")
    requested_path = (requested.path or "/").rstrip("/")
    return registered_path == requested_path and registered.query == requested.query


def _metadata_resource_url(request: Request) -> str:
    path = request.url.path
    prefix = "/.well-known/oauth-protected-resource"
    if path.startswith(f"{prefix}/"):
        resource_path = path[len(prefix) :]
    else:
        resource_path = DEFAULT_MCP_RESOURCE_PATH
    return f"{_oauth_issuer(request)}{resource_path}"


async def well_known_mcp_resource(request: Request) -> JSONResponse:
    """RFC 9728 discovery endpoint pointing to this authorization server."""
    base_url = _oauth_issuer(request)
    return JSONResponse(
        {
            "resource": _metadata_resource_url(request),
            "authorization_servers": [base_url],
            "scopes_supported": ["mcp:all"],
            "scopes": ["mcp:all"],
        }
    )


async def well_known_oauth_server(request: Request) -> JSONResponse:
    """Standard OAuth 2.1 metadata discovery endpoint."""
    base_url = _oauth_issuer(request)
    return JSONResponse(
        {
            "issuer": base_url,
            "authorization_endpoint": f"{base_url}/authorize",
            "token_endpoint": f"{base_url}/token",
            "registration_endpoint": f"{base_url}/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code"],
            "token_endpoint_auth_methods_supported": ["none"],
            "code_challenge_methods_supported": ["S256"],
            "scopes_supported": ["mcp:all"],
        }
    )


async def register_client(request: Request) -> JSONResponse:
    """Dynamic Client Registration (RFC 7591)."""
    try:
        body = await request.json()
    except Exception:
        logger.info("OAuth DCR rejected malformed JSON from %s", request.client)
        return JSONResponse(
            {"error": "invalid_request", "error_description": "Request body must be JSON"},
            status_code=400,
        )
    if not isinstance(body, dict):
        return JSONResponse(
            {"error": "invalid_request", "error_description": "Request body must be a JSON object"},
            status_code=400,
        )

    redirect_uris_value = body.get("redirect_uris", [])
    redirect_uris = (
        [redirect_uris_value] if isinstance(redirect_uris_value, str) else redirect_uris_value
    )
    client_name = body.get("client_name") or "Unknown MCP Client"
    if not isinstance(client_name, str) or len(client_name) > 255:
        return JSONResponse(
            {
                "error": "invalid_client_metadata",
                "error_description": "client_name must be a string up to 255 characters",
            },
            status_code=400,
        )

    if not isinstance(redirect_uris, list) or not redirect_uris:
        return JSONResponse(
            {
                "error": "invalid_client_metadata",
                "error_description": "redirect_uris must be a non-empty array",
            },
            status_code=400,
        )
    if len(redirect_uris) > 10 or not all(isinstance(uri, str) for uri in redirect_uris):
        return JSONResponse(
            {
                "error": "invalid_client_metadata",
                "error_description": "redirect_uris must contain between 1 and 10 strings",
            },
            status_code=400,
        )
    if not all(_is_allowed_redirect_uri(uri) for uri in redirect_uris):
        return JSONResponse(
            {
                "error": "invalid_redirect_uri",
                "error_description": "One or more redirect URIs are not allowed",
            },
            status_code=400,
        )

    token_auth_method = body.get("token_endpoint_auth_method") or "none"
    if token_auth_method != "none":
        return JSONResponse(
            {
                "error": "invalid_client_metadata",
                "error_description": "Only public OAuth clients are supported",
            },
            status_code=400,
        )

    grant_types = body.get("grant_types") or ["authorization_code"]
    if not isinstance(grant_types, list) or "authorization_code" not in grant_types:
        return JSONResponse(
            {
                "error": "invalid_client_metadata",
                "error_description": "authorization_code grant_type is required",
            },
            status_code=400,
        )

    response_types = body.get("response_types") or ["code"]
    if not isinstance(response_types, list) or "code" not in response_types:
        return JSONResponse(
            {
                "error": "invalid_client_metadata",
                "error_description": "code response_type is required",
            },
            status_code=400,
        )

    client_id = create_oauth_client(client_name, redirect_uris)
    return JSONResponse(
        {
            "client_id": client_id,
            "client_name": client_name,
            "redirect_uris": redirect_uris,
            "grant_types": ["authorization_code"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "scope": "mcp:all",
        },
        status_code=201,
    )


async def authorize(request: Request) -> RedirectResponse | HTMLResponse:
    """Initiate the authorization flow and redirect to the login page."""
    client_id = request.query_params.get("client_id")
    redirect_uri = request.query_params.get("redirect_uri")
    code_challenge = request.query_params.get("code_challenge")
    state = request.query_params.get("state")

    if client_id is None or redirect_uri is None or code_challenge is None:
        message = "Missing required parameters (client_id, redirect_uri, code_challenge)"
        return HTMLResponse(message, status_code=400)

    client = get_oauth_client(client_id)
    if not client:
        return HTMLResponse("Invalid client", status_code=400)

    if not _is_allowed_redirect_uri(redirect_uri):
        return HTMLResponse("Invalid redirect URI", status_code=400)
    registered_redirect_uris = client.get("redirect_uris", [])
    if not isinstance(registered_redirect_uris, list) or not any(
        isinstance(uri, str) and _redirect_uri_matches(uri, redirect_uri)
        for uri in registered_redirect_uris
    ):
        return HTMLResponse("Invalid redirect URI", status_code=400)

    # Encode the authorization request into a temporary signed token
    auth_request_data = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "state": state,
        "iss": _oauth_issuer(request),
        "aud": "oauth_consent",
        "iat": int(time.time()),
        "exp": int(time.time()) + 600,  # 10 minute expiration
    }

    auth_req_token = jwt.encode(auth_request_data, OAUTH_JWT_SECRET, algorithm="HS256")

    # Redirect to the existing login page with the auth request context
    return RedirectResponse(f"/login?auth_req={auth_req_token}")


async def oauth_consent(request: Request) -> JSONResponse:
    """Finalize authorization after the user has logged in via Google."""
    from threelane_memory.auth import current_user_id

    user_id = current_user_id.get()

    if not user_id:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_request"}, status_code=400)

    auth_req_token = body.get("auth_req")
    if not auth_req_token:
        return JSONResponse({"error": "missing auth_req"}, status_code=400)

    try:
        auth_req = jwt.decode(
            auth_req_token,
            OAUTH_JWT_SECRET,
            algorithms=["HS256"],
            audience="oauth_consent",
            issuer=_oauth_issuer(request),
            options={
                "require": [
                    "client_id",
                    "redirect_uri",
                    "code_challenge",
                    "iss",
                    "aud",
                    "iat",
                    "exp",
                ]
            },
        )
    except jwt.ExpiredSignatureError:
        return JSONResponse({"error": "authorization request expired"}, status_code=400)
    except jwt.InvalidTokenError:
        return JSONResponse({"error": "invalid authorization request"}, status_code=400)

    # Generate authorization code
    code = secrets.token_urlsafe(32)
    create_auth_code(
        code=code,
        client_id=auth_req["client_id"],
        user_id=user_id,
        redirect_uri=auth_req["redirect_uri"],
        code_challenge=auth_req["code_challenge"],
    )

    # Build redirect URL with code and original state
    redirect_uri = auth_req["redirect_uri"]
    state = auth_req.get("state")

    params = {"code": code}
    if state:
        params["state"] = state

    delimiter = "&" if "?" in redirect_uri else "?"
    final_redirect = f"{redirect_uri}{delimiter}{urlencode(params)}"

    return JSONResponse({"redirect_url": final_redirect})


async def token(request: Request) -> JSONResponse:
    """Exchange authorization code and PKCE verifier for an access token."""
    # OAuth 2.1 token endpoint typically expects application/x-www-form-urlencoded
    content_type = request.headers.get("content-type", "")
    if "application/x-www-form-urlencoded" in content_type:
        body = await request.form()
    else:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid_request"}, status_code=400)

    grant_type = body.get("grant_type")
    if grant_type != "authorization_code":
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    code = body.get("code")
    client_id = body.get("client_id")
    redirect_uri = body.get("redirect_uri")
    code_verifier = body.get("code_verifier")

    if not all([code, client_id, redirect_uri, code_verifier]):
        return JSONResponse({"error": "invalid_request"}, status_code=400)

    if not isinstance(code, str):
        return JSONResponse({"error": "invalid_request"}, status_code=400)
    if not isinstance(client_id, str):
        return JSONResponse({"error": "invalid_request"}, status_code=400)
    if not isinstance(redirect_uri, str):
        return JSONResponse({"error": "invalid_request"}, status_code=400)
    if not isinstance(code_verifier, str):
        return JSONResponse({"error": "invalid_request"}, status_code=400)

    auth_code_data = consume_auth_code(code)
    if not auth_code_data:
        return JSONResponse({"error": "invalid_grant"}, status_code=400)

    stored_client_id = auth_code_data.get("client_id")
    stored_redirect_uri = auth_code_data.get("redirect_uri")
    if not isinstance(stored_client_id, str) or not isinstance(stored_redirect_uri, str):
        return JSONResponse({"error": "invalid_grant"}, status_code=400)

    if stored_client_id != client_id or stored_redirect_uri.rstrip("/") != redirect_uri.rstrip("/"):
        return JSONResponse(
            {"error": "invalid_grant", "error_description": "Redirect URI mismatch"},
            status_code=400,
        )

    # Verify PKCE (S256)
    # The code_challenge was: base64url(sha256(code_verifier))
    hashed_verifier = hashlib.sha256(code_verifier.encode("ascii")).digest()
    expected_challenge = base64.urlsafe_b64encode(hashed_verifier).decode("ascii").rstrip("=")

    # Normalize stored challenge (remove padding if present)
    stored_challenge = auth_code_data.get("code_challenge")
    if not isinstance(stored_challenge, str):
        return JSONResponse({"error": "invalid_grant"}, status_code=400)
    stored_challenge = stored_challenge.rstrip("=")

    if stored_challenge != expected_challenge:
        return JSONResponse(
            {"error": "invalid_grant", "error_description": "PKCE verification failed"},
            status_code=400,
        )

    # Issue internal JWT Access Token
    from threelane_memory.config import OAUTH_TOKEN_EXPIRY_SECONDS

    user_id = auth_code_data["user_id"]
    base_url = _oauth_issuer(request)
    now = int(time.time())
    access_token = jwt.encode(
        {
            "sub": str(user_id),
            "aud": client_id,
            "iss": base_url,
            "exp": now + OAUTH_TOKEN_EXPIRY_SECONDS,
            "iat": now - 60,  # 60s leeway for clock drift
        },
        OAUTH_JWT_SECRET,
        algorithm="HS256",
    )

    return JSONResponse(
        {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": OAUTH_TOKEN_EXPIRY_SECONDS,
            "scope": "mcp:all",
        }
    )
