"""Evidence-bearing review runs and human decisions."""

from __future__ import annotations

import json
import uuid

from sqlalchemy import text

from backend.adapters.approval_repository import insert_approval
from backend.adapters.model_ir_repository import ModelIRRepository
from backend.db.session import engine
from backend.domain.contracts import EvidenceRef, RequestContext
from backend.domain.errors import InvalidTransition, ResourceNotFound, ValidationFailure


class ReviewRepository:
    async def create(
        self,
        context: RequestContext,
        model_ir_version_id: str,
        findings: list[dict],
        report: dict,
    ) -> dict:
        await ModelIRRepository().get(context, model_ir_version_id)
        review_id = "review_" + uuid.uuid4().hex
        normalized = []
        for finding in findings:
            evidence = [EvidenceRef.model_validate(item) for item in finding.get("evidence_refs", [])]
            if not evidence:
                raise ValidationFailure("every review finding requires evidence")
            normalized.append((finding, evidence))
        async with engine.begin() as conn:
            await conn.execute(text("""
                INSERT INTO review_runs_v2 (
                    id, tenant_id, project_id, model_ir_version_id,
                    status, report, created_by, version
                ) VALUES (
                    :id, :tenant_id, :project_id, :model_ir_version_id,
                    'waiting_human', :report, :created_by, 1
                )
            """), {
                "id": review_id, "tenant_id": context.tenant_id,
                "project_id": context.project_id,
                "model_ir_version_id": model_ir_version_id,
                "report": json.dumps(report, ensure_ascii=False),
                "created_by": context.user_id,
            })
            for finding, evidence in normalized:
                await conn.execute(text("""
                    INSERT INTO review_findings (
                        id, run_id, tenant_id, project_id, rule_id, track,
                        severity, confidence, description, suggestion,
                        evidence_refs, status, created_by, version
                    ) VALUES (
                        :id, :run_id, :tenant_id, :project_id, :rule_id, :track,
                        :severity, :confidence, :description, :suggestion,
                        :evidence_refs, 'open', :created_by, 1
                    )
                """), {
                    "id": "finding_" + uuid.uuid4().hex, "run_id": review_id,
                    "tenant_id": context.tenant_id, "project_id": context.project_id,
                    "rule_id": finding.get("rule_id"), "track": finding.get("track", "hard"),
                    "severity": finding.get("severity", "warning"),
                    "confidence": float(finding.get("confidence", 1.0)),
                    "description": str(finding.get("description", "")),
                    "suggestion": finding.get("suggestion"),
                    "evidence_refs": json.dumps([item.model_dump(mode="json") for item in evidence], ensure_ascii=False),
                    "created_by": context.user_id,
                })
        return await self.get(context, review_id)

    async def decide(self, context: RequestContext, review_id: str, decision: str, reason: str | None) -> dict:
        review = await self.get(context, review_id)
        if review["status"] != "waiting_human":
            raise InvalidTransition("review is not waiting for a human decision")
        if decision not in {"approved", "rejected"}:
            raise ValidationFailure("invalid review decision")
        async with engine.begin() as conn:
            approval_id = await insert_approval(
                conn,
                run_id=review_id,
                tenant_id=context.tenant_id,
                project_id=context.project_id,
                context=context,
                action="model_ir_review",
                decision=decision,
                reason=reason,
                actor_id=context.user_id,
            )
            updated = await conn.execute(text("""
                UPDATE review_runs_v2
                SET status='completed', verdict=:decision,
                    updated_at=CURRENT_TIMESTAMP, version=version + 1
                WHERE id=:id AND tenant_id=:tenant_id AND project_id=:project_id
                  AND status='waiting_human' AND version=:version
            """), {
                "decision": decision, "id": review_id,
                "tenant_id": context.tenant_id, "project_id": context.project_id,
                "version": review["version"],
            })
            if updated.rowcount != 1:
                raise InvalidTransition("review was decided concurrently")
        await ModelIRRepository().set_review_status(
            context, review["model_ir_version_id"], decision
        )
        result = await self.get(context, review_id)
        result["approval_id"] = approval_id
        return result

    async def get(self, context: RequestContext, review_id: str) -> dict:
        async with engine.connect() as conn:
            row = (await conn.execute(text("""
                SELECT id, tenant_id, project_id, workflow_run_id,
                       model_ir_version_id, status, verdict, report,
                       created_by, created_at, updated_at, version
                FROM review_runs_v2
                WHERE id=:id AND tenant_id=:tenant_id AND project_id=:project_id
            """), {
                "id": review_id, "tenant_id": context.tenant_id,
                "project_id": context.project_id,
            })).mappings().first()
            if not row:
                raise ResourceNotFound("review not found")
            findings = (await conn.execute(text("""
                SELECT id, rule_id, track, severity, confidence, description,
                       suggestion, evidence_refs, status
                FROM review_findings
                WHERE run_id=:run_id AND tenant_id=:tenant_id
                ORDER BY created_at ASC
            """), {"run_id": review_id, "tenant_id": context.tenant_id})).mappings().all()
        result = dict(row)
        result["report"] = json.loads(result["report"] or "{}")
        result["findings"] = []
        for finding in findings:
            item = dict(finding)
            item["evidence_refs"] = json.loads(item["evidence_refs"] or "[]")
            result["findings"].append(item)
        return result
