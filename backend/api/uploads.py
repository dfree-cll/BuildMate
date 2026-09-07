"""Shared bounded readers for legacy multipart upload endpoints."""

from __future__ import annotations

from fastapi import HTTPException, UploadFile


_UPLOAD_CHUNK_BYTES = 1024 * 1024


async def read_upload_limited(
    upload: UploadFile,
    *,
    max_bytes: int,
    too_large_detail: str,
) -> bytes:
    """Read one upload without accepting more than ``max_bytes`` in memory."""

    chunks: list[bytes] = []
    size = 0
    while True:
        block = await upload.read(_UPLOAD_CHUNK_BYTES)
        if not block:
            return b"".join(chunks)
        size += len(block)
        if size > max_bytes:
            raise HTTPException(status_code=413, detail=too_large_detail)
        chunks.append(block)
