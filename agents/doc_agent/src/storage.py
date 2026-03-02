"""
Document Agent — Supabase Storage Client.

Handles downloading source files (PDFs/images) from Supabase Storage
and uploading parsed artifacts back.
"""

import logging
from typing import Optional

import httpx

from src.config import SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, STORAGE_BUCKET

logger = logging.getLogger(__name__)


def _headers() -> dict:
    """Return auth headers for Supabase Storage REST API."""
    return {
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
    }


async def download_file(file_path: str, bucket: str = STORAGE_BUCKET) -> bytes:
    """
    Download a file from Supabase Storage.

    ``file_path`` is the object key inside the bucket,
    e.g. ``"uploads/abc-123/resume.pdf"``.
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set "
            "for Supabase Storage access."
        )

    url = f"{SUPABASE_URL}/storage/v1/object/{bucket}/{file_path}"
    logger.info("Downloading file from %s", url)

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.get(url, headers=_headers())
        resp.raise_for_status()
        logger.info("Downloaded %d bytes", len(resp.content))
        return resp.content


async def upload_artifact(
    user_id: str,
    task_id: str,
    data: bytes,
    filename: str = "artifact.json",
    content_type: str = "application/json",
    bucket: str = STORAGE_BUCKET,
) -> str:
    """
    Upload a parsed artifact to Supabase Storage.

    Returns the storage path of the uploaded object.
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError("Supabase credentials not configured.")

    object_path = f"artifacts/{user_id}/{task_id}/{filename}"
    url = f"{SUPABASE_URL}/storage/v1/object/{bucket}/{object_path}"

    headers = {
        **_headers(),
        "Content-Type": content_type,
        "x-upsert": "true",
    }

    logger.info("Uploading artifact to %s", url)
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(url, headers=headers, content=data)
        resp.raise_for_status()

    logger.info("Artifact uploaded: %s", object_path)
    return object_path


async def get_public_url(file_path: str, bucket: str = STORAGE_BUCKET) -> str:
    """Return the public/signed URL for a stored object."""
    return f"{SUPABASE_URL}/storage/v1/object/public/{bucket}/{file_path}"


def detect_file_type(data: bytes) -> str:
    """Detect file type from magic bytes."""
    if data[:5] == b"%PDF-":
        return "pdf"
    # PNG
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image"
    # JPEG
    if data[:2] == b"\xff\xd8":
        return "image"
    # TIFF
    if data[:2] in (b"II", b"MM"):
        return "image"
    # WebP
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image"
    # Default: treat as image (let downstream handle errors)
    return "image"
