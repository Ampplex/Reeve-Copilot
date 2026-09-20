"""Multimodal embeddings — images and text in one shared vector space.

Uses a Bedrock multimodal embedding model (``BEDROCK_MULTIMODAL_EMBED_MODEL``,
e.g. Titan Multimodal ``amazon.titan-embed-image-v1``) that maps both images and
text into the SAME 1024-dim space. This is what enables genuine image-to-image
("photos like this one") and text-to-image ("beach photos") search — capabilities
the text-only embedding cannot provide.

It sits ALONGSIDE the text embedding: a photo memory gets both a text vector
(from its Nova Lite description, via the normal pipeline) and an image vector
(here). The image bytes themselves are not stored — only the vector — so search
and reasoning work without holding users' photos.

Runs remotely on Bedrock (no local model weights), reusing the shared retry /
throttle machinery in bedrock_llm.
"""

from __future__ import annotations

import base64
import ipaddress
import logging
import socket
from urllib.parse import urlparse

import requests

from threelane_memory.config import (
    AWS_REGION,
    BEDROCK_API_KEY,
    BEDROCK_EMBED_TIMEOUT_SECONDS,
    BEDROCK_MULTIMODAL_EMBED_MODEL,
    IMAGE_URL_FETCH_TIMEOUT_SECONDS,
    MULTIMODAL_EMBED_DIM,
    VISION_MAX_IMAGE_BYTES,
)

logger = logging.getLogger(__name__)


def is_configured() -> bool:
    return bool(BEDROCK_MULTIMODAL_EMBED_MODEL and BEDROCK_API_KEY)


def _endpoint() -> str:
    return (
        f"https://bedrock-runtime.{AWS_REGION}.amazonaws.com"
        f"/model/{BEDROCK_MULTIMODAL_EMBED_MODEL}/invoke"
    )


def _invoke(body: dict) -> list[float]:
    from threelane_memory.bedrock_llm import _post_with_backoff

    response = _post_with_backoff(
        _endpoint(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {BEDROCK_API_KEY}",
        },
        json=body,
        timeout=BEDROCK_EMBED_TIMEOUT_SECONDS,
    )
    data = response.json()

    # Meter the provider's real input tokens (text side) into the usage scope.
    try:
        from threelane_memory.usage import add_usage

        tokens = data.get("inputTextTokenCount")
        if tokens:
            add_usage(tokens_in=int(tokens))
    except Exception:
        pass

    vec = data.get("embedding")
    if not vec:
        raise RuntimeError(f"Multimodal model returned no embedding: {str(data)[:200]}")
    return [float(x) for x in vec]


def embed_image_b64(image_base64: str) -> list[float]:
    """Embed an image (base64) into the shared multimodal space."""
    if not is_configured():
        raise ValueError(
            "Multimodal embeddings are not configured: set "
            "BEDROCK_MULTIMODAL_EMBED_MODEL to enable image vectors."
        )
    return _invoke(
        {
            "inputImage": image_base64,
            "embeddingConfig": {"outputEmbeddingLength": MULTIMODAL_EMBED_DIM},
        }
    )


def embed_text(text: str) -> list[float]:
    """Embed text into the SAME space as images (for text→image search)."""
    if not is_configured():
        raise ValueError("Multimodal embeddings are not configured.")
    if not text or not text.strip():
        raise ValueError("embed_text requires non-empty text")
    return _invoke(
        {
            "inputText": text,
            "embeddingConfig": {"outputEmbeddingLength": MULTIMODAL_EMBED_DIM},
        }
    )


# ── image_url fetch (MCP path) — SSRF-guarded ─────────────────────────────────


def _is_public_host(hostname: str) -> bool:
    """Reject loopback/private/link-local targets so image_url can't hit internal
    services (SSRF). Resolves the host and checks every returned address."""
    try:
        infos = socket.getaddrinfo(hostname, None)
    except Exception:
        return False
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr.split("%")[0])
        except ValueError:
            return False
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return False
    return True


def fetch_image_as_base64(image_url: str) -> tuple[str, str]:
    """Download a public image URL and return (base64, media_type).

    Guards: https/http only, no private/loopback hosts, capped size and time.
    Raises ValueError on anything disallowed.
    """
    parsed = urlparse(image_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("image_url must be an http(s) URL")
    if not _is_public_host(parsed.hostname):
        raise ValueError("image_url host is not allowed")

    resp = requests.get(
        image_url,
        timeout=IMAGE_URL_FETCH_TIMEOUT_SECONDS,
        stream=True,
        headers={"User-Agent": "reeve-memory/image-fetch"},
    )
    resp.raise_for_status()
    media_type = (resp.headers.get("content-type") or "image/jpeg").split(";")[0].strip()
    if not media_type.startswith("image/"):
        raise ValueError(f"image_url did not return an image (got {media_type!r})")

    chunks = bytearray()
    for chunk in resp.iter_content(8192):
        chunks.extend(chunk)
        if len(chunks) > VISION_MAX_IMAGE_BYTES:
            raise ValueError(
                f"image_url exceeds the {VISION_MAX_IMAGE_BYTES:,}-byte limit"
            )
    return base64.b64encode(bytes(chunks)).decode("ascii"), media_type
