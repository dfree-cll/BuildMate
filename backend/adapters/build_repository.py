"""Model build lifecycle: approved IR -> dry-run -> human approval."""

from __future__ import annotations

import json
import re
import uuid

from sqlalchemy import text

from backend.adapters.approval_repository import insert_approval
from backend.adapters.model_ir_repository import ModelIRRepository
from backend.db.session import engine
from backend.domain.contracts import RequestContext
from backend.domain.errors import InvalidTransition, PolicyFailure, ResourceNotFound, ValidationFailure


class BuildRepository:
    async def create(self, context: RequestContext, model_ir_version_id: str) -> dict:
        model = await ModelIRRepository().get(context, model_ir_version_id)
        if model["status"] != "approved":
            raise PolicyFailure("only an approved Model IR can enter Revit dry-run")
        build_id = "build_" + uuid.uuid4().hex
        dry_run = {
            "valid": True,
            "model_ir_sha256": model["payload_sha256"],
            "element_count": len(model["payload"]["model_elements"]),
            "checks": ["schema", "units", "coordinates", "evidence", "approval"],
            "note": "static compilation gate passed; Revit was not modified",
        }
        async with engine.begin() as conn:
            await conn.execute(text("""
                INSERT INTO model_build_runs (
                    id, tenant_id, project_id, model_ir_version_id, status,
                    dry_run_report, created_by, version
                ) VALUES (
                    :id, :tenant_id, :project_id, :model_ir_version_id,
                    'waiting_approval', :dry_run_report, :created_by, 1
                )
            """), {
                "id": build_id, "tenant_id": context.tenant_id,
                "project_id": context.project_id,
                "model_ir_version_id": model_ir_version_id,
                "dry_run_report": json.dumps(dry_run, ensure_ascii=False),
                "created_by": context.user_id,
            })
        return await self.get(context, build_id)

    async def approve(self, context: RequestContext, build_id: str, decision: str, reason: str | None) -> dict:
        build = await self.get(context, build_id)
        if build["status"] != "waiting_approval":
            raise InvalidTransition("build is not waiting for approval")
        if decision not in {"approved", "rejected"}:
            raise ValidationFailure("invalid build decision")
        target = "approved" if decision == "approved" else "rejected"
        async with engine.begin() as conn:
            approval_id = await insert_approval(
                conn,
                run_id=build_id,
                tenant_id=context.tenant_id,
                project_id=context.project_id,
                context=context,
                action="revit_write",
                decision=decision,
                reason=reason,
                actor_id=context.user_id,
            )
            updated = await conn.execute(text("""
                UPDATE model_build_runs
                SET status=:status, approval_id=:approval_id,
                    updated_at=CURRENT_TIMESTAMP, version=version + 1
                WHERE id=:id AND tenant_id=:tenant_id AND project_id=:project_id
                  AND status='waiting_approval' AND version=:version
            """), {
                "status": target, "approval_id": approval_id, "id": build_id,
                "tenant_id": context.tenant_id, "project_id": context.project_id,
                "version": build["version"],
            })
            if updated.rowcount != 1:
                raise InvalidTransition("build was decided concurrently")
        return await self.get(context, build_id)

    async def get(self, context: RequestContext, build_id: str) -> dict:
        async with engine.connect() as conn:
            row = (await conn.execute(text("""
                SELECT id, tenant_id, project_id, workflow_run_id,
                       model_ir_version_id, status, dry_run_report, approval_id,
                       result, diff, rollback_snapshot_uri, created_by,
                       created_at, updated_at, version
                FROM model_build_runs
                WHERE id=:id AND tenant_id=:tenant_id AND project_id=:project_id
            """), {
                "id": build_id, "tenant_id": context.tenant_id,
                "project_id": context.project_id,
            })).mappings().first()
        if not row:
            raise ResourceNotFound("build not found")
        result = dict(row)
        for field in ("dry_run_report", "result", "diff"):
            result[field] = json.loads(result[field]) if result[field] else None
        return result

    async def issue_bridge_token(
        self,
        context: RequestContext,
        build_id: str,
        action: str,
        *,
        script_sha256: str | None = None,
        target_model_path: str | None = None,
        source_model_sha256: str | None = None,
    ) -> str:
        build = await self.get(context, build_id)
        if build["status"] != "approved" or not build["approval_id"]:
            raise PolicyFailure("build has no persisted Revit approval")
        if action not in {"run_revit_script", "import_ifc_to_rvt"}:
            raise ValidationFailure("unsupported Revit approval action")
        if action == "run_revit_script":
            # A generic script token is a capability for one exact script,
            # one source snapshot, and one target path.  Never mint the old
            # build/action-only token: it could be replayed for another file
            # or script.  IFC keeps its historical unbound contract.
            if not script_sha256 or not target_model_path or not source_model_sha256:
                raise PolicyFailure(
                    "run_revit_script token requires script_sha256, "
                    "target_model_path, and source_model_sha256"
                )
            digest_pattern = re.compile(r"^[a-f0-9]{64}$")
            if not digest_pattern.fullmatch(script_sha256):
                raise ValidationFailure("script_sha256 must be a lowercase SHA-256 digest")
            if not digest_pattern.fullmatch(source_model_sha256):
                raise ValidationFailure("source_model_sha256 must be a lowercase SHA-256 digest")
        from backend.config import get_settings
        from backend.domain.approval_token import issue_approval_token

        claims = {"build_id": build_id, "action": action}
        if action == "run_revit_script":
            claims.update({
                "script_sha256": script_sha256,
                "target_model_path": target_model_path,
                "source_model_sha256": source_model_sha256,
            })
        return issue_approval_token(
            get_settings().revit_bridge_approval_secret,
            **claims,
        )
