"""S3/MinIO artifact adapter with a bounded local read cache."""

from __future__ import annotations

import asyncio
from pathlib import Path

from backend.domain.errors import DependencyFailure, ResourceNotFound, ValidationFailure


class S3ArtifactStorage:
    def __init__(
        self,
        *,
        endpoint_url: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        region: str,
        cache_root: Path,
    ) -> None:
        if not endpoint_url or not access_key or not secret_key or not bucket:
            raise ValidationFailure("S3 artifact storage configuration is incomplete")
        try:
            import boto3
        except ImportError as exc:
            raise DependencyFailure("boto3 is required for S3 artifact storage") from exc
        self._client = boto3.client(
            "s3", endpoint_url=endpoint_url,
            aws_access_key_id=access_key, aws_secret_access_key=secret_key,
            region_name=region,
        )
        self._bucket = bucket
        self._cache_root = cache_root.resolve()

    async def put(self, tenant_id: str, sha256: str, source: Path) -> str:
        safe_tenant = "".join(ch for ch in tenant_id if ch.isalnum() or ch in "-_")
        if not safe_tenant or len(sha256) != 64:
            raise ValidationFailure("invalid artifact storage key")
        key = f"{safe_tenant}/{sha256[:2]}/{sha256}"
        try:
            await asyncio.to_thread(self._client.upload_file, str(source), self._bucket, key)
        except Exception as exc:
            raise DependencyFailure(f"S3 upload failed: {str(exc)[:300]}") from exc
        return f"s3://{self._bucket}/{key}"

    async def resolve(self, storage_uri: str) -> Path:
        prefix = f"s3://{self._bucket}/"
        if not storage_uri.startswith(prefix):
            raise ValidationFailure("artifact S3 URI does not match configured bucket")
        key = storage_uri.removeprefix(prefix)
        target = (self._cache_root / key).resolve()
        if self._cache_root not in target.parents:
            raise ValidationFailure("artifact cache path escapes configured root")
        if target.is_file():
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".partial")
        try:
            await asyncio.to_thread(self._client.download_file, self._bucket, key, str(temporary))
            temporary.replace(target)
        except Exception as exc:
            if temporary.exists():
                temporary.unlink()
            raise ResourceNotFound(f"S3 artifact is unavailable: {str(exc)[:300]}") from exc
        return target
