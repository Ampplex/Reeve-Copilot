"""Retained photo bytes in S3 — the only way to answer *new* questions about an
old picture.

The image embedding stored on the Episode is a one-way fingerprint: excellent
for "find the photo that looks like this", incapable of "how many people were
in it?". Answering the latter means showing the original to a vision model, and
that means keeping the original.

Keeping it is a privacy decision, not just a storage one, so this module is
built to be switched off (the default) and to delete reliably when asked:

  • keys are prefixed by a HASH of the speaker, so listing the bucket reveals no
    account ids or namespace names (the raw speaker embeds a Google uid)
  • ``get_image`` refuses a key that does not belong to the requesting speaker,
    so a crafted or stale key cannot read another tenant's photo
  • ``delete_speaker`` paginates, so erasure is complete rather than first-page
  • deletion failures are logged at ERROR and surfaced in return values; a photo
    that survives an erasure request is the one failure mode that matters here
  • writes are best-effort: a store outage degrades to "no stored image" rather
    than failing the user's memory write
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import uuid

from threelane_memory.config import (
    IMAGE_STORE_BUCKET,
    IMAGE_STORE_ENABLED,
    IMAGE_STORE_KMS_KEY_ID,
    IMAGE_STORE_PREFIX,
    IMAGE_STORE_REGION,
    IMAGE_STORE_RETENTION_DAYS,
    IMAGE_STORE_TIMEOUT_SECONDS,
)

logger = logging.getLogger(__name__)

_EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
}
_MEDIA_TYPES = {v: k for k, v in _EXTENSIONS.items() if k != "image/jpg"}

_client = None
_client_failed = False


def is_configured() -> bool:
    """True when photo retention is switched on AND a bucket is set."""
    return bool(IMAGE_STORE_ENABLED and IMAGE_STORE_BUCKET)


def _get_client():
    """Lazily build the S3 client; cache the failure so we retry-storm nothing."""
    global _client, _client_failed
    if _client is not None or _client_failed:
        return _client
    try:
        import boto3
        from botocore.config import Config

        _client = boto3.client(
            "s3",
            region_name=IMAGE_STORE_REGION,
            config=Config(
                connect_timeout=IMAGE_STORE_TIMEOUT_SECONDS,
                read_timeout=IMAGE_STORE_TIMEOUT_SECONDS,
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )
    except Exception as exc:  # missing boto3, no credentials, bad region
        _client_failed = True
        logger.warning("Image store unavailable (S3 client init failed): %s", exc)
    return _client


def _speaker_prefix(speaker: str) -> str:
    """Hash the speaker so the bucket never discloses account ids or namespaces."""
    digest = hashlib.sha256(speaker.encode("utf-8")).hexdigest()[:32]
    return f"{IMAGE_STORE_PREFIX}/{digest}"


def object_key(speaker: str, media_type: str) -> str:
    """Fresh key: <prefix>/<sha256(speaker)>/<uuid>.<ext>.

    Deliberately NOT keyed by episode id. Writes are asynchronous, so at upload
    time the episode does not exist yet — the key is minted here and travels
    down to the episode as a short string, which also keeps 4 MB payloads out of
    the in-memory write buffer.
    """
    ext = _EXTENSIONS.get((media_type or "").lower(), "bin")
    return f"{_speaker_prefix(speaker)}/{uuid.uuid4().hex}.{ext}"


def media_type_for_key(key: str) -> str:
    """Recover the media type from a stored key's extension."""
    ext = key.rsplit(".", 1)[-1].lower() if "." in key else ""
    return _MEDIA_TYPES.get(ext, "image/jpeg")


def put_image(speaker: str, image_base64: str, media_type: str) -> str | None:
    """Store the photo; return its key, or None if retention is off/unavailable.

    Best-effort by design: the memory itself is already written and useful
    without the original, so a store outage must not fail the user's write.
    """
    if not is_configured() or not speaker or not image_base64:
        return None
    client = _get_client()
    if client is None:
        return None
    try:
        body = base64.b64decode(image_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        logger.warning("Image store skipped: payload is not valid base64: %s", exc)
        return None

    key = object_key(speaker, media_type)
    extra = {
        "Bucket": IMAGE_STORE_BUCKET,
        "Key": key,
        "Body": body,
        "ContentType": media_type or "image/jpeg",
    }
    if IMAGE_STORE_KMS_KEY_ID:
        extra["ServerSideEncryption"] = "aws:kms"
        extra["SSEKMSKeyId"] = IMAGE_STORE_KMS_KEY_ID
    else:
        extra["ServerSideEncryption"] = "AES256"

    try:
        client.put_object(**extra)
    except Exception as exc:
        logger.warning("Image store write failed: %s", exc)
        return None
    logger.info("image_store put key=%s bytes=%d", key, len(body))
    return key


def get_image(key: str, speaker: str) -> tuple[str, str] | None:
    """Fetch a stored photo as (base64, media_type), or None.

    The key is re-derived against *speaker* before use: episode rows are already
    speaker-filtered, but a stale or crafted key must never be able to read
    another tenant's photo just because it reached this function.
    """
    if not is_configured() or not key or not speaker:
        return None
    if not key.startswith(_speaker_prefix(speaker) + "/"):
        logger.error("image_store REFUSED cross-tenant key read: key=%s", key)
        return None
    client = _get_client()
    if client is None:
        return None
    try:
        obj = client.get_object(Bucket=IMAGE_STORE_BUCKET, Key=key)
        body = obj["Body"].read()
    except Exception as exc:
        # Expected once the retention TTL has expired the object.
        logger.info("image_store read miss key=%s: %s", key, exc)
        return None
    logger.info("image_store get key=%s bytes=%d", key, len(body))
    return base64.b64encode(body).decode("ascii"), media_type_for_key(key)


def delete_keys(keys: list[str]) -> int:
    """Delete specific objects; return how many were confirmed deleted.

    Unlike writes, failures here are logged at ERROR: an object that outlives an
    erasure request is a retained photo the user asked us to destroy.
    """
    keys = [k for k in keys if k]
    if not is_configured() or not keys:
        return 0
    client = _get_client()
    if client is None:
        logger.error("image_store CANNOT DELETE %d object(s): client unavailable", len(keys))
        return 0

    deleted = 0
    for start in range(0, len(keys), 1000):  # S3 delete_objects caps at 1000
        batch = keys[start : start + 1000]
        try:
            resp = client.delete_objects(
                Bucket=IMAGE_STORE_BUCKET,
                Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True},
            )
        except Exception as exc:
            logger.error("image_store DELETE FAILED for %d object(s): %s", len(batch), exc)
            continue
        errors = resp.get("Errors") or []
        for err in errors:
            logger.error(
                "image_store DELETE FAILED key=%s code=%s", err.get("Key"), err.get("Code")
            )
        deleted += len(batch) - len(errors)
    logger.info("image_store deleted %d/%d object(s)", deleted, len(keys))
    return deleted


def delete_speaker(speaker: str) -> int:
    """Delete every stored photo for *speaker*; return the count.

    Paginated: a partial sweep would leave photos behind after an erasure
    request, which is precisely the outcome this exists to prevent.
    """
    if not is_configured() or not speaker:
        return 0
    client = _get_client()
    if client is None:
        logger.error("image_store CANNOT PURGE speaker: client unavailable")
        return 0

    prefix = _speaker_prefix(speaker) + "/"
    keys: list[str] = []
    try:
        token = None
        while True:
            kwargs = {"Bucket": IMAGE_STORE_BUCKET, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            page = client.list_objects_v2(**kwargs)
            keys.extend(o["Key"] for o in page.get("Contents", []))
            if not page.get("IsTruncated"):
                break
            token = page.get("NextContinuationToken")
    except Exception as exc:
        logger.error("image_store PURGE LISTING FAILED for prefix=%s: %s", prefix, exc)
        return 0
    return delete_keys(keys)


def ensure_lifecycle() -> bool:
    """Apply the retention rule so photos age out without anyone remembering to.

    Storage limitation, enforced by the bucket rather than by our code paths:
    even if a delete is ever missed, objects still expire. Descriptions and
    embeddings are unaffected, so recall keeps working after the photo is gone.
    """
    if not is_configured() or IMAGE_STORE_RETENTION_DAYS <= 0:
        return False
    client = _get_client()
    if client is None:
        return False
    try:
        client.put_bucket_lifecycle_configuration(
            Bucket=IMAGE_STORE_BUCKET,
            LifecycleConfiguration={
                "Rules": [
                    {
                        "ID": "reeve-image-retention",
                        "Status": "Enabled",
                        "Filter": {"Prefix": f"{IMAGE_STORE_PREFIX}/"},
                        "Expiration": {"Days": IMAGE_STORE_RETENTION_DAYS},
                        "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1},
                    }
                ]
            },
        )
    except Exception as exc:
        logger.warning("Image retention lifecycle could not be applied: %s", exc)
        return False
    logger.info("image_store retention set to %d day(s)", IMAGE_STORE_RETENTION_DAYS)
    return True
