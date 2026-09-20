"""Standard library functions that mirror the MCP tools for reeve.

These functions act as an API client, communicating with a remote reeve/MCP server
over SSE/HTTP to perform memory operations. This ensures the core logic remains
secure on the backend.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
import uuid
from typing import Any

import requests

logger = logging.getLogger(__name__)

# The public SDK only supports the hosted Reeve MCP service. Localhost/self-hosted
# MCP transports are intentionally kept out of this package surface.
HOSTED_REEVE_BASE_URL = "https://mcp.reeve.co.in"
REEVE_BASE_URL = HOSTED_REEVE_BASE_URL
REEVE_API_KEY = os.getenv("REEVE_API_KEY", "")


def _resolve_hosted_base_url(base_url: str | None = None) -> str:
    requested = (base_url or os.getenv("REEVE_BASE_URL") or HOSTED_REEVE_BASE_URL).rstrip("/")
    if requested != HOSTED_REEVE_BASE_URL:
        raise ValueError(
            "The Reeve SDK only supports the hosted MCP endpoint "
            f"{HOSTED_REEVE_BASE_URL}. Remove REEVE_BASE_URL or set it to "
            f"{HOSTED_REEVE_BASE_URL}."
        )
    return HOSTED_REEVE_BASE_URL


def _is_stale_session_error(exc: BaseException) -> bool:
    """Whether *exc* means "the server no longer knows this SSE session".

    Deliberately narrow: only a 404 on the message endpoint (the session id is
    gone) and transport-level drops of the kept-alive connection. Anything else
    — 401, 429, 5xx, a tool error — is a real answer from the server and must
    surface to the caller rather than be silently retried.
    """
    import requests

    if isinstance(exc, requests.HTTPError):
        response = getattr(exc, "response", None)
        return response is not None and response.status_code == 404
    return isinstance(exc, (requests.ConnectionError, requests.exceptions.ChunkedEncodingError))


class ReeveClient:
    """A minimal client for interacting with a remote reeve/MCP server over SSE."""

    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        self.base_url = _resolve_hosted_base_url(base_url)
        self.api_key = api_key or REEVE_API_KEY
        self.session = requests.Session()
        if self.api_key:
            self.session.headers.update({"Authorization": f"Bearer {self.api_key}"})

        self._sse_url: str | None = None
        self._message_url: str | None = None
        self._responses: dict[str | int, queue.Queue] = {}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._listener_thread: threading.Thread | None = None
        self._initialized = False

    def _ensure_connected(self):
        """Establish SSE connection and perform MCP initialization if needed."""
        if self._initialized:
            return

        if not self.base_url:
            raise ValueError(
                "REEVE_BASE_URL is not set. Please provide it or set "
                "the REEVE_BASE_URL environment variable."
            )

        # 1. Connect to SSE
        sse_endpoint = f"{self.base_url}/sse"
        logger.debug(f"DEBUG: Connecting to SSE at {sse_endpoint}...")
        max_retries = 3
        last_exception = None

        for attempt in range(max_retries):
            try:
                # We use a combined connect/read timeout and retries to handle
                # intermittent server delays and slow startups.
                response = self.session.get(
                    sse_endpoint,
                    stream=True,
                    headers={"Accept": "text/event-stream"},
                    timeout=120,
                )
                response.raise_for_status()
                last_exception = None
                logger.debug(f"DEBUG: SSE connection established (attempt {attempt+1}).")
                break
            except Exception as e:
                logger.debug(f"DEBUG: SSE connection attempt {attempt+1} failed: {e}")
                last_exception = e
                if attempt < max_retries - 1:
                    time.sleep(2**attempt)
                continue

        if last_exception:
            raise ConnectionError(
                f"Failed to connect to Reeve server at {sse_endpoint}: {last_exception}"
            )

        try:
            import sseclient
        except ImportError as exc:
            raise ImportError(
                "sseclient-py is required to connect to the Reeve MCP server. "
                "Install it with: pip install sseclient-py"
            ) from exc

        logger.debug("DEBUG: Initializing SSEClient...")
        client = sseclient.SSEClient(response)

        # 2. Start listener thread IMMEDIATELY so we don't miss the endpoint event
        logger.debug("DEBUG: Starting listener thread...")
        self._listener_thread = threading.Thread(target=self._listen, args=(client,), daemon=True)
        self._listener_thread.start()

        # 3. Wait for the endpoint event to tell us where to send messages
        logger.debug("DEBUG: Waiting for message endpoint...")
        start_time = time.time()
        while not self._message_url and (time.time() - start_time < 30):
            time.sleep(0.1)

        if not self._message_url:
            logger.debug("DEBUG: Timed out waiting for message endpoint.")
            self.close()
            raise ConnectionError("Timed out waiting for message endpoint from SSE stream.")

        logger.debug(f"DEBUG: Message endpoint received: {self._message_url}")

        # 4. Initialize MCP session
        logger.debug("DEBUG: Sending MCP initialize...")
        init_id = str(uuid.uuid4())
        self._send_rpc(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "reeve-python-sdk", "version": "0.1.41"},
            },
            msg_id=init_id,
        )

        # Wait for init result (increased timeout)
        logger.debug("DEBUG: Waiting for initialize response...")
        self._wait_for_response(init_id, timeout=20)

        # 5. Notify server we are ready
        logger.debug("DEBUG: Sending initialized notification...")
        self._send_rpc("notifications/initialized", {}, is_notification=True)
        self._initialized = True
        logger.debug("DEBUG: ReeveClient initialization complete.")

    def _listen(self, client: Any):
        """Listen for messages on the SSE stream."""
        try:
            for event in client.events():
                if self._stop_event.is_set():
                    break

                # Handle endpoint event
                if event.event == "endpoint":
                    self._message_url = f"{self.base_url}{event.data}"
                    continue

                # Handle message event
                if event.event == "message":
                    try:
                        data = json.loads(event.data)
                        msg_id = data.get("id")
                        if msg_id is not None:
                            with self._lock:
                                if msg_id in self._responses:
                                    self._responses[msg_id].put(data)
                    except json.JSONDecodeError:
                        continue
        except Exception:
            pass

    def _send_rpc(
        self,
        method: str,
        params: dict,
        msg_id: str | int | None = None,
        is_notification: bool = False,
    ):
        """Send a JSON-RPC message to the server."""
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params}
        if not is_notification:
            if msg_id is None:
                msg_id = str(uuid.uuid4())
            payload["id"] = msg_id
            with self._lock:
                self._responses[msg_id] = queue.Queue()

        resp = self.session.post(self._message_url, json=payload, timeout=60)
        resp.raise_for_status()
        return msg_id

    def _wait_for_response(self, msg_id: str | int, timeout: float = 180.0) -> dict:
        """Wait for a specific JSON-RPC response."""
        with self._lock:
            q = self._responses.get(msg_id)

        if not q:
            raise RuntimeError(f"No response queue for message {msg_id}")

        try:
            response = q.get(timeout=timeout)
            if "error" in response:
                raise RuntimeError(f"RPC Error: {response['error']}")
            return response.get("result", {})
        except queue.Empty:
            raise TimeoutError(f"Timed out waiting for response to {msg_id} after {timeout}s")
        finally:
            with self._lock:
                if msg_id in self._responses:
                    del self._responses[msg_id]

    def _reconnect(self) -> None:
        """Tear down a dead session and establish a fresh one."""
        try:
            self.close()
        except Exception:
            pass
        with self._lock:
            self._responses.clear()
        self._initialized = False
        self._ensure_connected()

    def call_tool(self, name: str, arguments: dict) -> Any:
        """Call an MCP tool on the remote server.

        The SSE session is server-side state that expires while a client sits
        idle (and is dropped whenever the server restarts). A long-lived client
        would then fail its next call with a raw 404/connection error, so a
        session that has gone away is re-established once and the call retried —
        callers should not have to know the transport reconnected.
        """
        self._ensure_connected()
        try:
            msg_id = self._send_rpc("tools/call", {"name": name, "arguments": arguments})
        except Exception as exc:
            if not _is_stale_session_error(exc):
                raise
            logger.debug("Reeve session expired (%s); reconnecting and retrying", exc)
            self._reconnect()
            msg_id = self._send_rpc("tools/call", {"name": name, "arguments": arguments})
        result = self._wait_for_response(msg_id)

        # MCP tool results are usually list of content objects
        content = result.get("content", [])
        if not content:
            return None

        # If it's a simple text response, return it as a string
        if len(content) == 1 and content[0].get("type") == "text":
            text = content[0].get("text", "")
            try:
                # Try to parse as JSON if it looks like one
                if text.strip().startswith(("{", "[")):
                    return json.loads(text)
                return text
            except json.JSONDecodeError:
                return text
        return content

    def close(self):
        """Close the client and stop the listener."""
        self._stop_event.set()
        self._initialized = False
        self.session.close()


# --- Helper Functions (Singleton Client) ---

_client_cache: dict[tuple[str, str], ReeveClient] = {}


def _client_cache_key(base_url: str | None, api_key: str | None) -> tuple[str, str]:
    resolved_base_url = _resolve_hosted_base_url(base_url)
    resolved_api_key = api_key or os.getenv("REEVE_API_KEY", REEVE_API_KEY) or ""
    return resolved_base_url, resolved_api_key


def _get_client(base_url: str | None = None, api_key: str | None = None):
    """Return a cached client for the current base URL and API key."""
    cache_key = _client_cache_key(base_url, api_key)
    client = _client_cache.get(cache_key)
    if client is None:
        client = ReeveClient(base_url=cache_key[0], api_key=cache_key[1] or None)
        _client_cache[cache_key] = client
    return client


def reset_client_cache() -> None:
    """Close and clear cached clients, mainly for tests and identity switches."""
    for client in _client_cache.values():
        client.close()
    _client_cache.clear()


_IMAGE_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def store_memory(
    text: str,
    speaker: str = "default",
    image_path: str | None = None,
    image_base64: str | None = None,
    image_media_type: str | None = None,
    image_url: str | None = None,
) -> dict[str, Any]:
    """Store a memory entry via the remote reeve server.

    Attach a photo as a local file (``image_path``), pre-encoded bytes
    (``image_base64`` + ``image_media_type``), or a public link (``image_url``,
    the server fetches it). The server describes the image, resolves EXIF GPS to
    the place it was taken, and stores an image vector for visual search.
    """
    args: dict[str, Any] = {"text": text, "speaker": speaker}
    provided = [x for x in (image_path, image_base64, image_url) if x]
    if len(provided) > 1:
        raise ValueError("Pass only one of image_path, image_base64, image_url")
    if image_path:
        import base64
        from pathlib import Path

        path = Path(image_path)
        suffix = path.suffix.lower()
        media_type = image_media_type or _IMAGE_MEDIA_TYPES.get(suffix)
        if not media_type:
            raise ValueError(
                f"Cannot infer media type from {suffix!r}; pass image_media_type"
            )
        args["image_base64"] = base64.b64encode(path.read_bytes()).decode("ascii")
        args["image_media_type"] = media_type
    elif image_base64:
        args["image_base64"] = image_base64
        args["image_media_type"] = image_media_type or "image/jpeg"
    elif image_url:
        args["image_url"] = image_url
    return _get_client().call_tool("store_memory", args)


def search_image_memories(
    query: str = "",
    speaker: str = "default",
    image_path: str | None = None,
    image_url: str | None = None,
) -> str:
    """Find photo memories by visual similarity via the remote reeve server.

    Search by words ("beach photos") or by an example image ("photos like
    this") given as a local file (``image_path``) or a public ``image_url``.
    """
    args: dict[str, Any] = {"query": query, "speaker": speaker}
    if image_path and image_url:
        raise ValueError("Pass image_path or image_url, not both")
    if image_path:
        import base64
        from pathlib import Path

        args["image_base64"] = base64.b64encode(Path(image_path).read_bytes()).decode("ascii")
    elif image_url:
        args["image_url"] = image_url
    return _get_client().call_tool("search_image_memories", args)


def _image_args(image_path: str | None, image_url: str | None) -> dict[str, Any]:
    """Encode an attached photo for a query. One source only."""
    if image_path and image_url:
        raise ValueError("Pass image_path or image_url, not both")
    if image_path:
        import base64
        from pathlib import Path

        path = Path(image_path)
        media_type = _IMAGE_MEDIA_TYPES.get(path.suffix.lower())
        if not media_type:
            raise ValueError(
                f"Cannot infer media type from {path.suffix!r}; use a jpg, png, gif or webp"
            )
        return {
            "image_base64": base64.b64encode(path.read_bytes()).decode("ascii"),
            "image_media_type": media_type,
        }
    if image_url:
        return {"image_url": image_url}
    return {}


def query_memory(
    question: str,
    speaker: str = "default",
    image_path: str | None = None,
    image_url: str | None = None,
) -> str:
    """Query long-term memory via the remote reeve server.

    Attach a photo to ask about it directly — ``query_memory("what did I cook
    here?", image_path="dish.jpg")``. The photo steers retrieval toward memories
    that LOOK like it, and when the server has retained the originals the answer
    is written with those photos in view rather than from their captions alone.
    """
    args: dict[str, Any] = {"question": question, "speaker": speaker}
    args.update(_image_args(image_path, image_url))
    return _get_client().call_tool("query_memory", args)


def retrieve_memory_context(
    question: str,
    speaker: str = "default",
    image_path: str | None = None,
    image_url: str | None = None,
) -> str:
    """Retrieve raw memory context via the remote reeve server.

    Accepts an attached photo like :func:`query_memory`, for callers running
    their own model over Reeve's context.
    """
    args: dict[str, Any] = {"question": question, "speaker": speaker}
    args.update(_image_args(image_path, image_url))
    return _get_client().call_tool("retrieve_memory_context", args)


def memory_config() -> dict[str, Any]:
    """Show remote provider and index health details."""
    return _get_client().call_tool("memory_config", {})


def backup_memory(speaker: str | None = None, since: str | None = None) -> dict[str, str]:
    """Export memories to JSON backup via the remote reeve server."""
    args = {}
    if speaker:
        args["speaker"] = speaker
    if since:
        args["since"] = since
    return _get_client().call_tool("backup_memory", args)


def deduplicate_memory_entities(dry_run: bool = False) -> dict[str, int | bool]:
    """Deduplicate entities in the remote memory graph."""
    return _get_client().call_tool("deduplicate_memory_entities", {"dry_run": dry_run})


def consolidate_memory(speaker: str = "default") -> dict[str, Any]:
    """Consolidate memories via the remote reeve server."""
    return _get_client().call_tool("consolidate_memory", {"speaker": speaker})
