"""Immutable, reproducible Model IR v2 persistence."""

from __future__ import annotations

import json
import uuid

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from backend.db.session import engine
from backend.domain.contracts import RequestContext
from backend.domain.errors import ResourceNotFound, ValidationFailure
from backend.domain.model_ir import ModelIRV2


class ModelIRRepository:
    async def save(self, context: RequestContext, artifact_id: str, model: ModelIRV2) -> tuple[dict, bool]:
        if not context.project_id:
            raise ValidationFailure("Model IR persistence requires a project context")
        if artifact_id not in model.provenance.source_artifact_ids:
            raise ValidationFailure("source_artifact_id must be present in Model IR provenance")
        payload = model.model_dump_json()
        payload_hash = model.canonical_sha256()
        provenance = model.provenance
        existing = await self.find_reproducible(
            context, provenance.source_sha256, provenance.pipeline_version,
            provenance.parser_version,
        )
        if existing:
            return existing, False
        model_id = "ir_" + uuid.uuid4().hex
        try:
            async with engine.begin() as conn:
                await conn.execute(text("""
                    INSERT INTO model_ir_versions (
                        id, tenant_id, project_id, source_artifact_id,
                        source_sha256, schema_version, pipeline_version,
                        parser_version, payload, payload_sha256, status,
                        created_by, version
                    ) VALUES (
                        :id, :tenant_id, :project_id, :artifact_id,
                        :source_sha256, :schema_version, :pipeline_version,
                        :parser_version, :payload, :payload_sha256, 'pending',
                        :created_by, 1
                    )
                """), {
                    "id": model_id, "tenant_id": context.tenant_id,
                    "project_id": context.project_id, "artifact_id": artifact_id,
                    "source_sha256": provenance.source_sha256,
                    "schema_version": model.schema_version,
                    "pipeline_version": provenance.pipeline_version,
                    "parser_version": provenance.parser_version,
                    "payload": payload, "payload_sha256": payload_hash,
                    "created_by": context.user_id,
                })
        except IntegrityError:
            existing = await self.find_reproducible(
                context, provenance.source_sha256, provenance.pipeline_version,
                provenance.parser_version,
            )
            if existing:
                return existing, False
            raise
        return await self.get(context, model_id), True

    async def find_reproducible(
        self,
        context: RequestContext,
        source_hash: str,
        pipeline_version: str,
        parser_version: str,
    ) -> dict | None:
        async with engine.connect() as conn:
            row = (await conn.execute(text("""
                SELECT id, tenant_id, project_id, source_artifact_id, source_sha256,
                       schema_version, pipeline_version, parser_version, payload,
                       payload_sha256, status, created_by, created_at, version
                FROM model_ir_versions
                WHERE tenant_id=:tenant_id AND project_id=:project_id
                  AND source_sha256=:source_hash AND pipeline_version=:pipeline_version
                  AND parser_version=:parser_version
            """), {
                "tenant_id": context.tenant_id, "project_id": context.project_id,
                "source_hash": source_hash, "pipeline_version": pipeline_version,
                "parser_version": parser_version,
            })).mappings().first()
        return self._row(row) if row else None

    async def get(self, context: RequestContext, model_id: str) -> dict:
        async with engine.connect() as conn:
            row = (await conn.execute(text("""
                SELECT id, tenant_id, project_id, source_artifact_id, source_sha256,
                       schema_version, pipeline_version, parser_version, payload,
                       payload_sha256, status, created_by, created_at, version
                FROM model_ir_versions
                WHERE id=:id AND tenant_id=:tenant_id AND project_id=:project_id
            """), {
                "id": model_id, "tenant_id": context.tenant_id,
                "project_id": context.project_id,
            })).mappings().first()
        if not row:
            raise ResourceNotFound("Model IR version not found")
        return self._row(row)

    async def set_review_status(self, context: RequestContext, model_id: str, status: str) -> None:
        if status not in {"approved", "rejected"}:
            raise ValueError("invalid Model IR review status")
        async with engine.begin() as conn:
            result = await conn.execute(text("""
                UPDATE model_ir_versions SET status=:status, version=version + 1
                WHERE id=:id AND tenant_id=:tenant_id AND project_id=:project_id
            """), {
                "status": status, "id": model_id,
                "tenant_id": context.tenant_id, "project_id": context.project_id,
            })
            if result.rowcount != 1:
                raise ResourceNotFound("Model IR version not found")

    @staticmethod
    def _row(row) -> dict:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item
