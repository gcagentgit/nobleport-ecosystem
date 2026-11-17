"""Authenticated, size-capped, MIME-allowlisted uploads (POST /agent/upload).

An upload attaches evidence to a gateway run (`source_refs`). The endpoint
that exposes this module requires a bearer token with the `gateway:upload`
scope — see app.py — so nothing here is reachable anonymously. The file is
streamed to disk under a random name, its SHA-256 is computed on the way
through, and the record is anchored in the audit chain by the caller.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException, UploadFile, status

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_CHUNK = 64 * 1024
MAX_FILENAME_CHARS = 128


def safe_filename(name: str | None) -> str:
    base = Path(name or "upload").name  # strip any directory components
    base = _SAFE_NAME.sub("_", base).strip("._") or "upload"
    return base[:MAX_FILENAME_CHARS]


def parse_mime_allowlist(raw: str) -> frozenset[str]:
    return frozenset(m.strip().lower() for m in (raw or "").split(",") if m.strip())


async def store_upload(file: UploadFile, *, upload_dir: str, max_bytes: int,
                       allowed_mime: frozenset[str]) -> dict[str, Any]:
    mime = (file.content_type or "").split(";")[0].strip().lower()
    if mime not in allowed_mime:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="mime_not_allowed")

    upload_id = uuid.uuid4()
    filename = safe_filename(file.filename)
    target_dir = Path(upload_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{upload_id.hex}_{filename}"

    digest = hashlib.sha256()
    size = 0
    try:
        with target.open("wb") as fh:
            while True:
                chunk = await file.read(_CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                                        detail="upload_too_large")
                digest.update(chunk)
                fh.write(chunk)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    if size == 0:
        target.unlink(missing_ok=True)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="empty_upload")

    return {
        "upload_id": str(upload_id),
        "filename": filename,
        "mime": mime,
        "bytes": size,
        "sha256": digest.hexdigest(),
        "stored_at": datetime.now(timezone.utc).isoformat(),
    }
