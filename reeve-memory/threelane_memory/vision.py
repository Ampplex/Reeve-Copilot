"""Image ingestion — turn a photo into a text memory record.

Dual-model design: a separate multimodal Bedrock model (``BEDROCK_VISION_MODEL_ID``)
describes the image once at write time; every other stage — extraction, place
cards, retrieval, answers — stays on the main chat model. The image itself is
not persisted (no blob store yet); what enters the graph is the derived text.

Photos often carry EXIF GPS coordinates. When present they are reverse-geocoded
(Nominatim) and the resolved place is appended to the memory text, so the
operator extracts it as the episode's location and the geo enrichment pipeline
(place card, vibe embedding, ``location_point``) kicks in automatically —
a photo of a beach taken in Goa becomes a Goa memory without anyone typing
"Goa".

Vision tokens are metered through the request-scoped usage accumulator like
every other internal call.
"""

from __future__ import annotations

import base64
import io
import logging
from typing import Any

from threelane_memory.config import (
    AWS_REGION,
    BEDROCK_API_KEY,
    BEDROCK_CHAT_TIMEOUT_SECONDS,
    BEDROCK_VISION_MODEL_ID,
    VISION_DESCRIBE_MAX_TOKENS,
    VISION_MAX_IMAGE_BYTES,
    VISION_MAX_TOKENS,
)

logger = logging.getLogger(__name__)

# "any visible ... readable text" used to sit inside the 2-4 sentence budget,
# which quietly made a whole class of photo unanswerable. A whiteboard, a
# timetable or a receipt IS its text — four sentences summarise it as "a
# whiteboard with blue and red writing listing a seminar series" and the names,
# dates and room numbers, the only reason anyone photographs such a thing, are
# gone. Asked "who is speaking on 13 November?", the system then answers "I
# don't remember" while holding a photo that says so in plain handwriting.
#
# The retrieval gate makes this worse than a soft miss. The ambient image lane
# needs IMAGE_LANE_MIN_SAMPLE photos to find a break in the ranking, so an
# account with one or two photos never re-reads them at all, and the gate's
# justification — "a miss costs nothing, the text lanes still retrieve that
# photo through its description" — only holds if the description carries the
# detail. Transcription is what makes that true.
IMAGE_MEMORY_PROMPT = """\
You convert a photo into a compact memory record.

Describe this photo in 2-4 sentences as a memory: the main scene and activity,
the dominant colours of the main subject, people and notable objects, any
visible place or landmark, and the mood. Be concrete and specific; no preamble,
no speculation beyond what is visible.

If the photo contains readable text — a whiteboard, slide, receipt, label,
sign, screen, timetable or printed page — then after the description add a line
reading exactly TEXT: and transcribe the text verbatim below it, one line per
line as written, in the order it appears. Copy names, numbers, dates, times,
room and item codes exactly; those are usually why the photo was taken. Stop
after 40 lines. If there is no readable text, omit the TEXT section entirely.
"""

_CONVERSE_FORMATS = {
    "image/jpeg": "jpeg",
    "image/jpg": "jpeg",
    "jpeg": "jpeg",
    "jpg": "jpeg",
    "image/png": "png",
    "png": "png",
    "image/gif": "gif",
    "gif": "gif",
    "image/webp": "webp",
    "webp": "webp",
}

_GPS_IFD_TAG = 0x8825  # EXIF pointer to the GPS IFD


def is_configured() -> bool:
    return bool(BEDROCK_VISION_MODEL_ID and BEDROCK_API_KEY)


def _converse_format(media_type: str) -> str:
    fmt = _CONVERSE_FORMATS.get((media_type or "").lower().strip())
    if not fmt:
        raise ValueError(
            f"Unsupported image media type {media_type!r}; "
            "use jpeg, png, gif, or webp."
        )
    return fmt


def _converse_vision(
    images: list[tuple[str, str]],
    prompt: str,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    """One Converse call to the vision model with N images. Raw response JSON.

    Takes a list so the same call serves both jobs: describing a single photo at
    ingest, and showing the model several retrieved photos when answering a
    question about them.
    """
    from threelane_memory.bedrock_llm import _post_with_backoff

    url = (
        f"https://bedrock-runtime.{AWS_REGION}.amazonaws.com"
        f"/model/{BEDROCK_VISION_MODEL_ID}/converse"
    )
    content: list[dict[str, Any]] = [
        {"image": {"format": fmt, "source": {"bytes": b64}}} for b64, fmt in images
    ]
    content.append({"text": prompt})
    body = {
        "messages": [{"role": "user", "content": content}],
        "inferenceConfig": {
            "maxTokens": max_tokens or VISION_MAX_TOKENS,
            "temperature": 0.2,
        },
    }
    response = _post_with_backoff(
        url,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {BEDROCK_API_KEY}",
        },
        json=body,
        timeout=BEDROCK_CHAT_TIMEOUT_SECONDS,
    )
    return response.json()


def describe_image(image_base64: str, media_type: str = "image/jpeg") -> str:
    """Run the multimodal model over an image and return the memory description."""
    if not is_configured():
        raise ValueError(
            "Image support is not configured: set BEDROCK_VISION_MODEL_ID "
            "(a multimodal Bedrock model) to enable it."
        )
    fmt = _converse_format(media_type)
    # Its own budget, because this prompt can now be asked to transcribe up to
    # 40 lines. On the shared 512 the tail of a timetable would be cut mid-row,
    # which is worse than not transcribing: the description would look complete
    # while silently missing the last entries. The answering path keeps the
    # smaller budget — it writes a sentence or two, not a transcript.
    data = _converse_vision(
        [(image_base64, fmt)], IMAGE_MEMORY_PROMPT, max_tokens=VISION_DESCRIBE_MAX_TOKENS
    )

    blocks = data.get("output", {}).get("message", {}).get("content", [])
    text = "".join(
        block.get("text", "") if isinstance(block, dict) else str(block) for block in blocks
    ).strip()

    try:
        from threelane_memory.usage import add_usage

        usage = data.get("usage", {}) or {}
        add_usage(
            tokens_in=int(usage.get("inputTokens", 0) or 0),
            tokens_out=int(usage.get("outputTokens", 0) or 0),
        )
    except Exception:
        pass

    if not text:
        raise RuntimeError(f"Vision model returned no text: {str(data)[:200]}")
    return text


ANSWER_WITH_IMAGES_PROMPT = """\
You are a personal memory assistant. The user is asking about their own photos,
which are attached above, along with the text memories already retrieved.

RULES:
1. The attached photo(s) ARE the user's memories. Anything visible in them is
   something the user recorded, so answer questions about colour, count,
   arrangement, garnish, setting or text directly from what you can see. Never
   refuse a question the photo plainly answers.
2. The text context adds names, places and dates the photo cannot show —
   combine both. If the context names a place or restaurant belonging to a
   photo, use that name.
3. Answer in one or two sentences. No preamble, no describing the photo unless
   that is what was asked.
4. The memory context may be empty. That is normal, and it does not stop you
   answering from the photo alone.
5. Only if the photo shows nothing relevant AND the context says nothing
   relevant, respond with exactly: 'I don't remember.'
6. Do not invent facts that neither the photo nor the context supports.

Memory Context:
{context}

Question: {question}
"""


def answer_with_images(
    question: str,
    context: str,
    images: list[tuple[str, str]],
    max_tokens: int = 512,
) -> str:
    """Answer *question* with the actual photos in front of the model.

    This is the one thing the image embedding cannot do. The embedding matches
    pictures; it cannot be read. Questions nobody anticipated at upload time
    ("how many people were there?", "what colour was it?") need the original,
    which is why the image store exists.

    Routed to the VISION model deliberately: the main chat model (Mistral) is
    text-only, so it could not see these at all.
    """
    if not is_configured():
        raise ValueError(
            "Image support is not configured: set BEDROCK_VISION_MODEL_ID "
            "(a multimodal Bedrock model) to enable it."
        )
    if not images:
        raise ValueError("answer_with_images requires at least one image")

    prompt = ANSWER_WITH_IMAGES_PROMPT.format(context=context or "(none)", question=question)
    data = _converse_vision(images, prompt, max_tokens=max_tokens)

    blocks = data.get("output", {}).get("message", {}).get("content", [])
    text = "".join(
        block.get("text", "") if isinstance(block, dict) else str(block) for block in blocks
    ).strip()

    try:
        from threelane_memory.usage import add_usage

        usage = data.get("usage", {}) or {}
        add_usage(
            tokens_in=int(usage.get("inputTokens", 0) or 0),
            tokens_out=int(usage.get("outputTokens", 0) or 0),
        )
    except Exception:
        pass

    if not text:
        raise RuntimeError(f"Vision model returned no text: {str(data)[:200]}")
    return text


# ── EXIF GPS ──────────────────────────────────────────────────────────────────


def _rational(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        num, den = value
        return float(num) / float(den or 1)


def gps_from_ifd(gps: dict[Any, Any]) -> tuple[float, float] | None:
    """Convert a raw EXIF GPS IFD (tags 1-4) to signed decimal degrees."""
    try:
        lat_ref, lat = gps[1], gps[2]
        lon_ref, lon = gps[3], gps[4]
        latitude = _rational(lat[0]) + _rational(lat[1]) / 60 + _rational(lat[2]) / 3600
        longitude = _rational(lon[0]) + _rational(lon[1]) / 60 + _rational(lon[2]) / 3600
        if str(lat_ref).upper().startswith("S"):
            latitude = -latitude
        if str(lon_ref).upper().startswith("W"):
            longitude = -longitude
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            return None
        return latitude, longitude
    except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError):
        return None


def extract_exif_gps(image_bytes: bytes) -> tuple[float, float] | None:
    """Best-effort EXIF GPS extraction; returns None on anything unexpected."""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(image_bytes)) as img:
            gps = img.getexif().get_ifd(_GPS_IFD_TAG)
        if not gps:
            return None
        return gps_from_ifd(dict(gps))
    except Exception:
        return None


# ── Public entry point ────────────────────────────────────────────────────────


def image_to_memory_text(image_base64: str, media_type: str = "image/jpeg") -> str:
    """Full image-ingestion pass: description + EXIF place, as memory text."""
    try:
        raw = base64.b64decode(image_base64, validate=True)
    except Exception as exc:
        raise ValueError(f"image_base64 is not valid base64: {exc}") from exc
    if len(raw) > VISION_MAX_IMAGE_BYTES:
        raise ValueError(
            f"Image is {len(raw):,} bytes; the limit is {VISION_MAX_IMAGE_BYTES:,}."
        )

    description = describe_image(image_base64, media_type)

    place_line = ""
    coords = extract_exif_gps(raw)
    if coords:
        try:
            from threelane_memory.geo import reverse_geocode

            place = reverse_geocode(coords[0], coords[1])
        except Exception:
            place = None
        if place and place.get("name"):
            where = place["name"]
            if place.get("country"):
                where = f"{where}, {place['country']}"
            place_line = f" The photo was taken in {where}."
        else:
            place_line = (
                f" The photo was taken at coordinates {coords[0]:.4f}, {coords[1]:.4f}."
            )

    return f"[Photo] {description}{place_line}"
