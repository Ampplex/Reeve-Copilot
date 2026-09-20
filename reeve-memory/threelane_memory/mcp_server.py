"""MCP server wrapper for threelane-memory.

This module exposes the core long-term memory operations as MCP tools so any
MCP-compatible client can store and query memory through stdio or network
transports.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import logging
from typing import Any

logger = logging.getLogger(__name__)


def _server_class() -> tuple[Any, bool]:
    """Return (server class, is_v2). Supports mcp 1.x and 2.x.

    mcp 2.0 renamed FastMCP to MCPServer and moved it to mcp.server.mcpserver.
    Both are supported rather than cutting over, so the dependency ceiling can be
    relaxed without the deployed container having to change in lockstep.
    """
    try:
        return importlib.import_module("mcp.server.mcpserver").MCPServer, True
    except (ImportError, AttributeError):
        pass
    try:
        return importlib.import_module("mcp.server.fastmcp").FastMCP, False
    except ImportError as exc:
        raise ImportError(
            "MCP dependencies are not installed. Install with: pip install 'threelane-memory[mcp]'"
        ) from exc


def transport_security_settings():
    """Explicit DNS-rebinding protection, or None when disabled.

    Must be set explicitly. Deriving it from the bind host — which is what
    passing host/port to FastMCP did — produces None for the 0.0.0.0 bind
    production uses, leaving the protection off while looking configured.
    """
    from threelane_memory.config import (
        MCP_ALLOWED_HOSTS,
        MCP_ALLOWED_ORIGINS,
        MCP_DNS_REBINDING_PROTECTION,
    )

    if not MCP_DNS_REBINDING_PROTECTION:
        return None
    try:
        from mcp.server.transport_security import TransportSecuritySettings
    except ImportError:  # pragma: no cover - mcp extra not installed
        return None
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(MCP_ALLOWED_HOSTS),
        allowed_origins=list(MCP_ALLOWED_ORIGINS),
    )


def create_server(*, host: str = "127.0.0.1", port: int = 8000):
    """Build and return an MCP server with threelane-memory tools."""
    server_cls, is_v2 = _server_class()

    from threelane_memory import tools

    # 2.x dropped host/port from the constructor. Nothing is lost: the network
    # bind is done by uvicorn in run_server, and these arguments only ever fed
    # FastMCP's transport-security derivation — which is now set explicitly
    # instead, because that derivation silently produced no protection at all.
    security = transport_security_settings()
    if is_v2:
        # 2.x takes it at sse_app() time; stash it for run_server to pass.
        server = server_cls("threelane-memory")
        server._reeve_transport_security = security
    else:
        server = server_cls(
            "threelane-memory", host=host, port=port, transport_security=security
        )

    async def store_memory(
        text: str,
        speaker: str = "default",
        image_base64: str | None = None,
        image_media_type: str = "image/jpeg",
        image_url: str | None = None,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            tools.store_memory,
            text,
            speaker=speaker,
            image_base64=image_base64,
            image_media_type=image_media_type,
            image_url=image_url,
        )

    async def search_image_memories(
        query: str = "",
        speaker: str = "default",
        image_url: str | None = None,
        image_base64: str | None = None,
    ) -> str:
        return await asyncio.to_thread(
            tools.search_image_memories,
            query,
            speaker=speaker,
            image_url=image_url,
            image_base64=image_base64,
        )

    async def query_memory(
        question: str,
        speaker: str = "default",
        image_url: str | None = None,
        image_base64: str | None = None,
        image_media_type: str = "image/jpeg",
    ) -> str:
        return await tools.query_memory(
            question,
            speaker=speaker,
            image_url=image_url,
            image_base64=image_base64,
            image_media_type=image_media_type,
        )

    async def retrieve_memory_context(
        question: str,
        speaker: str = "default",
        image_url: str | None = None,
        image_base64: str | None = None,
    ) -> str:
        return await asyncio.to_thread(
            tools.retrieve_memory_context,
            question,
            speaker=speaker,
            image_url=image_url,
            image_base64=image_base64,
        )

    async def memory_config() -> dict[str, Any]:
        return await asyncio.to_thread(tools.memory_config)

    async def backup_memory(
        speaker: str | None = None,
        since: str | None = None,
    ) -> dict[str, str]:
        return await asyncio.to_thread(tools.backup_memory, speaker=speaker, since=since)

    async def deduplicate_memory_entities(
        dry_run: bool = False, speaker: str = "default"
    ) -> dict[str, int | bool]:
        return await asyncio.to_thread(
            tools.deduplicate_memory_entities, dry_run=dry_run, speaker=speaker
        )

    async def consolidate_memory(speaker: str = "default") -> dict[str, Any]:
        return await asyncio.to_thread(tools.consolidate_memory, speaker=speaker)

    async def clear_memory(speaker: str = "default", dry_run: bool = False) -> dict[str, Any]:
        return await asyncio.to_thread(tools.clear_memory, speaker=speaker, dry_run=dry_run)

    # Register tools from the central tools module. FastMCP executes sync tools
    # inline, so these async wrappers keep long LLM/DB calls off the event loop.
    server.tool(
        description=(
            "Store a memory entry into the 3-lane long-term memory graph. "
            "Optionally attach a photo — image_url (a public image link; the "
            "server fetches it) or image_base64. A multimodal model turns it "
            "into a text memory, EXIF GPS is resolved to the place it was "
            "taken, and an image vector is stored for visual search."
        )
    )(store_memory)
    server.tool(
        description=(
            "Find photo memories by how they look, not just their text — "
            "'beach photos' (text→image), or 'photos like this one' via "
            "image_url / image_base64 (image→image)."
        )
    )(search_image_memories)
    server.tool(description="Query long-term memory and get a natural-language answer.")(
        query_memory
    )
    server.tool(description="Retrieve ranked raw memory context (pre-answer) for inspection.")(
        retrieve_memory_context
    )
    server.tool(description="Show provider and vector-index health details.")(memory_config)
    server.tool(
        description="Export memories to JSON backup. Optionally filter by speaker and date."
    )(backup_memory)
    server.tool(description="Deduplicate entities in the memory graph.")(
        deduplicate_memory_entities
    )
    server.tool(description="Consolidate old low-importance episodes for a speaker.")(
        consolidate_memory
    )
    server.tool(
        description=(
            "Permanently delete all memory for a namespace (hard reset). Destructive "
            "and irreversible; scoped to the caller's own account. Pass dry_run=true "
            "to preview the counts without deleting."
        )
    )(clear_memory)

    return server


def run_server(
    *,
    transport: str = "stdio",
    host: str = "127.0.0.1",
    port: int = 8000,
) -> None:
    """Run the MCP server with the selected transport."""
    server = create_server(host=host, port=port)
    if transport == "stdio":
        server.run(transport="stdio")
        return

    if transport == "sse":
        import os

        import uvicorn
        from starlette.middleware.cors import CORSMiddleware
        from starlette.responses import JSONResponse
        from starlette.staticfiles import StaticFiles
        from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

        from threelane_memory.auth import AuthMiddleware, current_user_id
        from threelane_memory.config import (
            API_KEY_HASH_SECRET_IS_EPHEMERAL,
            OAUTH_JWT_SECRET_IS_EPHEMERAL,
        )

        # A per-process random signing key means every worker/restart rejects
        # previously-issued tokens and cannot verify stored API-key HMACs. Fail
        # fast instead of silently breaking authentication in production.
        if OAUTH_JWT_SECRET_IS_EPHEMERAL:
            raise RuntimeError(
                "OAUTH_JWT_SECRET must be set to a persistent value before running "
                "the network (sse) server. Refusing to start with an ephemeral, "
                "per-process signing key."
            )
        if API_KEY_HASH_SECRET_IS_EPHEMERAL:
            raise RuntimeError(
                "API_KEY_HASH_SECRET must be set to a persistent value before running "
                "the network (sse) server so stored API-key hashes remain verifiable."
            )

        # 2.x takes the security settings here rather than in the constructor.
        _security = getattr(server, "_reeve_transport_security", None)
        app = server.sse_app(transport_security=_security) if _security else server.sse_app()

        from threelane_memory.config import ASYNC_WRITE_ENABLED
        from threelane_memory.database import migrate_plaintext_api_keys

        try:
            migrated_keys = migrate_plaintext_api_keys()
            if migrated_keys:
                logger.info("Migrated %s legacy plaintext API keys", migrated_keys)
        except Exception as exc:
            logger.error("Legacy API key migration failed: %s", exc)
            raise

        # Ensure the entity vector index exists and apply pending graph schema
        # migrations (per-speaker entity isolation) before the async worker
        # drains any writes or traffic is served. Idempotent and resumable;
        # the operator should pre-run `reeve migrate` in production so this is a
        # no-op safety net rather than the primary path.
        from threelane_memory.database import ensure_entity_vector_index
        from threelane_memory.migrations import run_pending_migrations

        ensure_entity_vector_index()
        try:
            run_pending_migrations()
        except Exception as exc:
            logger.error("Graph schema migration failed: %s", exc)
            raise

        stop_worker = None
        if ASYNC_WRITE_ENABLED:
            from threelane_memory.background_worker import start_worker
            from threelane_memory.background_worker import stop_worker as _stop_worker

            start_worker()
            stop_worker = _stop_worker


        # OAuth 2.1 Routes
        from threelane_memory.oauth2 import (
            authorize,
            oauth_consent,
            register_client,
            token,
            well_known_mcp_resource,
            well_known_oauth_server,
        )

        app.add_route(
            "/.well-known/oauth-protected-resource", well_known_mcp_resource, methods=["GET"]
        )
        app.add_route(
            "/.well-known/oauth-protected-resource/sse", well_known_mcp_resource, methods=["GET"]
        )
        app.add_route("/.well-known/mcp-resource", well_known_mcp_resource, methods=["GET"])
        app.add_route(
            "/.well-known/oauth-authorization-server", well_known_oauth_server, methods=["GET"]
        )

        app.add_route("/register", register_client, methods=["POST"])
        app.add_route("/authorize", authorize, methods=["GET"])
        app.add_route("/oauth/consent", oauth_consent, methods=["POST"])
        app.add_route("/token", token, methods=["POST"])

        # Add a verification endpoint for the frontend
        async def verify_auth(request):
            from threelane_memory.auth import issue_session_api_key
            from threelane_memory.config import OAUTH_EXPECTED_ISSUER
            from threelane_memory.database import (
                get_user_plan,
                issue_user_api_key,
                upsert_user,
                user_has_api_key,
            )

            user_id = current_user_id.get()
            if not user_id:
                return JSONResponse({"error": "Unauthorized"}, status_code=401)

            upsert_user(user_id)
            plan = get_user_plan(user_id)

            # Mirror the Google profile into Supabase server-side (service role),
            # replacing the frontend anon-key write now blocked by RLS. Best-
            # effort and off the event loop; never fails auth. plan/api_key stay
            # sourced from the graph (billing + key hashes live there).
            from threelane_memory import supabase_sync
            from threelane_memory.auth import current_user_profile

            google_profile = current_user_profile.get() or {}
            await asyncio.to_thread(
                supabase_sync.sync_user_profile,
                user_id,
                email=google_profile.get("email"),
                name=google_profile.get("name"),
                picture=google_profile.get("picture"),
            )

            issue_api_key = request.query_params.get("issue_api_key", "").lower() in {
                "1",
                "true",
                "yes",
            }
            api_key = issue_user_api_key(user_id) if issue_api_key else None
            response = {"uid": user_id, "plan": plan, "has_api_key": user_has_api_key(user_id)}
            if api_key:
                response["api_key"] = api_key
                response["api_key_kind"] = "permanent"
            else:
                issuer = OAUTH_EXPECTED_ISSUER or str(request.base_url).rstrip("/")
                session_api_key, expires_in = issue_session_api_key(user_id, issuer)
                response["api_key"] = session_api_key
                response["session_api_key"] = session_api_key
                response["api_key_kind"] = "session"
                response["expires_in"] = expires_in
            return JSONResponse(response)

        app.add_route("/auth/verify", verify_auth, methods=["POST"])

        async def revoke_api_key(request):
            from threelane_memory.database import revoke_user_api_key

            user_id = current_user_id.get()
            if not user_id:
                return JSONResponse({"error": "Unauthorized"}, status_code=401)

            revoke_user_api_key(user_id)
            return JSONResponse({"uid": user_id, "has_api_key": False})

        app.add_route("/auth/revoke-api-key", revoke_api_key, methods=["POST"])

        # Serve the login page
        static_path = os.path.join(os.path.dirname(__file__), "static")
        if os.path.exists(static_path):
            app.mount("/static_assets", StaticFiles(directory=static_path), name="static_assets")

            async def login_page(request):
                from starlette.responses import HTMLResponse

                from threelane_memory.config import GOOGLE_CLIENT_ID

                with open(os.path.join(static_path, "index.html")) as f:
                    content = f.read()
                # Inject the real client ID from config
                content = content.replace("REPLACE_WITH_YOUR_GOOGLE_CLIENT_ID", GOOGLE_CLIENT_ID)
                return HTMLResponse(content)

            async def index_redirect(request):
                return await login_page(request)

            app.add_route("/login", login_page)
            app.add_route("/", index_redirect)

        # Dashboard API endpoints
        async def account_data(request):
            """GET /account/data — what this account holds, per namespace.

            Backs the erasure control in the dashboard. Until this existed the
            only way to delete anything was calling clear_memory through the SDK,
            which is a developer action — not a mechanism a Data Principal can
            reasonably be expected to use to exercise a statutory right.
            """
            from threelane_memory.auth import current_user_id
            from threelane_memory.database import run_query

            uid = current_user_id.get()
            if not uid:
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                rows = run_query(
                    "MATCH (ep:Episode) WHERE ep.speaker STARTS WITH $prefix "
                    "RETURN ep.speaker AS speaker, count(ep) AS episodes, "
                    "count(ep.image_key) AS photos ORDER BY episodes DESC",
                    {"prefix": f"{uid}:"},
                )
                items = [
                    {
                        # Show the namespace, not the composed key: the uid half
                        # is an internal identity detail.
                        "namespace": r["speaker"].split(":", 1)[1] if ":" in r["speaker"]
                        else r["speaker"],
                        "episodes": r["episodes"],
                        "photos": r["photos"],
                    }
                    for r in rows
                ]
                return JSONResponse({
                    "namespaces": items,
                    "total_episodes": sum(i["episodes"] for i in items),
                    "total_photos": sum(i["photos"] for i in items),
                })
            except Exception as exc:
                logger.error("account_data error for uid=%s: %s", uid, exc)
                return JSONResponse({"error": "Internal server error"}, status_code=500)

        async def account_delete_data(request):
            """POST /account/delete-data — erase this account's memories.

            Body: {"namespace": "work"} for one, or {"all": true} for everything.
            Deletes the graph AND any retained photos, via the same clear_speaker
            path the MCP tool uses, so erasure behaves identically however it is
            invoked. Irreversible by design — that is the point of the right.
            """
            from threelane_memory.auth import current_user_id
            from threelane_memory.database import run_query
            from threelane_memory.reconciler import clear_speaker

            uid = current_user_id.get()
            if not uid:
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                body = await request.json()
            except Exception:
                body = {}

            namespace = str((body or {}).get("namespace") or "").strip()
            delete_all = bool((body or {}).get("all"))
            if not namespace and not delete_all:
                return JSONResponse(
                    {"error": "Specify a namespace, or all: true"}, status_code=400
                )

            try:
                if delete_all:
                    rows = run_query(
                        "MATCH (ep:Episode) WHERE ep.speaker STARTS WITH $prefix "
                        "RETURN DISTINCT ep.speaker AS speaker",
                        {"prefix": f"{uid}:"},
                    )
                    speakers = [r["speaker"] for r in rows]
                else:
                    # Always compose from the AUTHENTICATED uid, never from the
                    # request: a caller must not be able to name another
                    # account's speaker key and have it erased.
                    speakers = [f"{uid}:{namespace}"]

                summary = {"episodes": 0, "photos_deleted": 0, "namespaces": 0}
                for speaker in speakers:
                    result = clear_speaker(speaker, dry_run=False)
                    summary["episodes"] += result.get("episodes", 0)
                    summary["photos_deleted"] += result.get("images_deleted", 0)
                    summary["namespaces"] += 1
                logger.info(
                    "Erasure: uid=%s removed %s episodes and %s photos across %s namespace(s)",
                    uid, summary["episodes"], summary["photos_deleted"],
                    summary["namespaces"],
                )
                return JSONResponse({"deleted": True, **summary})
            except Exception as exc:
                logger.error("account_delete_data error for uid=%s: %s", uid, exc)
                return JSONResponse({"error": "Deletion failed"}, status_code=500)

        async def dashboard_stats(request):
            from threelane_memory.auth import current_user_id
            from threelane_memory.database import get_dashboard_stats
            uid = current_user_id.get()
            if not uid:
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                return JSONResponse(get_dashboard_stats(uid))
            except Exception as exc:
                logger.error("Dashboard stats error for uid=%s: %s", uid, exc)
                return JSONResponse({"error": "Internal server error"}, status_code=500)

        async def dashboard_logs(request):
            from threelane_memory.auth import current_user_id
            from threelane_memory.database import get_dashboard_logs
            uid = current_user_id.get()
            if not uid:
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                try:
                    limit = int(request.query_params.get("limit", 50))
                except (TypeError, ValueError):
                    limit = 50
                limit = max(1, min(limit, 500))
                return JSONResponse({"logs": get_dashboard_logs(uid, limit=limit)})
            except Exception as exc:
                logger.error("Dashboard logs error for uid=%s: %s", uid, exc)
                return JSONResponse({"error": "Internal server error"}, status_code=500)

        async def dashboard_graph(request):
            from threelane_memory.auth import current_user_id
            from threelane_memory.database import get_dashboard_graph
            uid = current_user_id.get()
            if not uid:
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                return JSONResponse(get_dashboard_graph(uid))
            except Exception as exc:
                logger.error("Dashboard graph error for uid=%s: %s", uid, exc)
                return JSONResponse({"error": "Internal server error"}, status_code=500)

        async def dashboard_usage(request):
            from threelane_memory.auth import current_user_id
            from threelane_memory.database import get_dashboard_usage
            uid = current_user_id.get()
            if not uid:
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                try:
                    days = int(request.query_params.get("days", 30))
                except (TypeError, ValueError):
                    days = 30
                days = max(1, min(days, 365))
                return JSONResponse(get_dashboard_usage(uid, days=days))
            except Exception as exc:
                logger.error("Dashboard usage error for uid=%s: %s", uid, exc)
                return JSONResponse({"error": "Internal server error"}, status_code=500)

        app.add_route("/account/data", account_data, methods=["GET"])
        app.add_route("/account/delete-data", account_delete_data, methods=["POST"])
        app.add_route("/dashboard/stats", dashboard_stats, methods=["GET"])
        app.add_route("/dashboard/logs", dashboard_logs, methods=["GET"])
        app.add_route("/dashboard/graph", dashboard_graph, methods=["GET"])
        app.add_route("/dashboard/usage", dashboard_usage, methods=["GET"])

        from threelane_memory.billing import (
            billing_status,
            billing_webhook,
            cancel_subscription,
            confirm_subscription_payment,
            create_subscription,
            list_invoices,
            update_spend_cap,
            usage_summary,
        )

        app.add_route("/billing/create-subscription", create_subscription, methods=["POST"])
        app.add_route("/billing/confirm", confirm_subscription_payment, methods=["POST"])
        app.add_route("/billing/webhook", billing_webhook, methods=["POST"])
        app.add_route("/billing/cancel", cancel_subscription, methods=["POST"])
        app.add_route("/billing/status", billing_status, methods=["GET"])
        app.add_route("/billing/invoices", list_invoices, methods=["GET"])
        app.add_route("/billing/usage", usage_summary, methods=["GET"])
        app.add_route("/billing/spend-cap", update_spend_cap, methods=["POST"])

        from threelane_memory.config import TRUSTED_PROXY_HOSTS

        app.add_middleware(AuthMiddleware)
        app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=TRUSTED_PROXY_HOSTS)
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[
                "https://reeve.co.in",
                "https://www.reeve.co.in",
                "https://mcp.reeve.co.in",
                "http://localhost:3000",
            ],
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type"],
            allow_credentials=True,
        )
        try:
            uvicorn.run(app, host=host, port=port)
        finally:
            if stop_worker is not None:
                stop_worker()
        return

    # Some FastMCP versions read host/port from settings even when run() accepts
    # explicit arguments, so set both for widest compatibility.
    settings = getattr(server, "settings", None)
    if settings is not None:
        if hasattr(settings, "host"):
            settings.host = host
        if hasattr(settings, "port"):
            settings.port = port

    # Keep compatibility across FastMCP versions that may accept different args.
    try:
        server.run(transport=transport, host=host, port=port)
    except TypeError:
        # Older FastMCP versions may reject host/port in run().
        server.run(transport=transport)


def main(argv: list[str] | None = None) -> None:
    """CLI entrypoint for running the MCP server standalone."""
    parser = argparse.ArgumentParser(prog="threelane-memory-mcp")
    parser.add_argument("--transport", choices=["stdio", "sse"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    run_server(transport=args.transport, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
