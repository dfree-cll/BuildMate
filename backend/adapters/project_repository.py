"""Tenant-scoped project persistence."""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from backend.db.session import engine
from backend.domain.contracts import RequestContext
from backend.domain.errors import ResourceNotFound, ValidationFailure


class ProjectRepository:
    async def create(self, context: RequestContext, name: str) -> dict:
        project_id = "prj_" + uuid.uuid4().hex
        try:
            async with engine.begin() as conn:
                await conn.execute(text("""
                    INSERT INTO projects (
                        id, tenant_id, name, status, created_by, version
                    ) VALUES (:id, :tenant_id, :name, 'active', :created_by, 1)
                """), {
                    "id": project_id,
                    "tenant_id": context.tenant_id,
                    "name": name.strip(),
                    "created_by": context.user_id,
                })
        except IntegrityError as error:
            raise ValidationFailure("project name already exists in this tenant") from error
        return await self.get(context, project_id)

    async def get(self, context: RequestContext, project_id: str) -> dict:
        async with engine.connect() as conn:
            row = (await conn.execute(text("""
                SELECT id, tenant_id, name, status, created_by,
                       created_at, updated_at, version
                FROM projects
                WHERE id=:id AND tenant_id=:tenant_id
            """), {
                "id": project_id,
                "tenant_id": context.tenant_id,
            })).mappings().first()
        if not row:
            raise ResourceNotFound("project not found")
        return dict(row)

    async def list(self, context: RequestContext, limit: int = 50) -> list[dict]:
        async with engine.connect() as conn:
            rows = (await conn.execute(text("""
                SELECT id, tenant_id, name, status, created_by,
                       created_at, updated_at, version
                FROM projects
                WHERE tenant_id=:tenant_id
                ORDER BY updated_at DESC LIMIT :limit
            """), {
                "tenant_id": context.tenant_id,
                "limit": max(1, min(limit, 100)),
            })).mappings().all()
        return [dict(row) for row in rows]

    async def add_level(
        self,
        context: RequestContext,
        project_id: str,
        *,
        floor_code: str,
        elevation_mm: int,
    ) -> dict:
        await self.get(context, project_id)
        level_id = "level_" + uuid.uuid4().hex
        try:
            async with engine.begin() as conn:
                await conn.execute(text("""
                    INSERT INTO bim_project_levels (
                        id, project_id, floor_code, elevation_mm
                    ) VALUES (:id, :project_id, :floor_code, :elevation_mm)
                """), {
                    "id": level_id, "project_id": project_id,
                    "floor_code": floor_code.strip(), "elevation_mm": elevation_mm,
                })
        except IntegrityError as error:
            raise ValidationFailure("floor code or elevation already exists") from error
        return {"id": level_id, "project_id": project_id,
                "floor_code": floor_code.strip(), "elevation_mm": elevation_mm}

    async def list_levels(self, context: RequestContext, project_id: str) -> list[dict]:
        await self.get(context, project_id)
        async with engine.connect() as conn:
            rows = (await conn.execute(text("""
                SELECT id, project_id, floor_code, elevation_mm, created_at, updated_at
                FROM bim_project_levels WHERE project_id=:project_id
                ORDER BY elevation_mm ASC
            """), {"project_id": project_id})).mappings().all()
        return [dict(row) for row in rows]
