"""图纸→BIM 成长闭环持久化服务。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from typing import Any

from sqlalchemy import text

from backend.db.session import engine


def file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def drawing_fingerprint(path: str) -> dict[str, Any]:
    """提取与具体坐标无关的 DXF 特征，用于识别同一类制图模板。"""
    import ezdxf

    doc = ezdxf.readfile(path)
    layers = sorted({_normalise_token(layer.dxf.name) for layer in doc.layers
                     if layer.dxf.name not in ("0", "Defpoints")})
    entity_types: dict[str, int] = {}
    entity_layers: dict[str, int] = {}
    for entity in doc.modelspace():
        typ = entity.dxftype()
        entity_types[typ] = entity_types.get(typ, 0) + 1
        layer = _normalise_token(getattr(entity.dxf, "layer", ""))
        if layer:
            entity_layers[layer] = entity_layers.get(layer, 0) + 1
    blocks = sorted({_normalise_token(block.name) for block in doc.blocks
                     if not block.name.startswith("*")})
    # 数量只取数量级，避免同模板不同楼层因为构件个数不同而失配。
    type_bands = {k: _count_band(v) for k, v in sorted(entity_types.items())}
    features = {"layers": layers, "blocks": blocks, "entity_type_bands": type_bands,
                "active_layers": sorted(entity_layers, key=entity_layers.get, reverse=True)[:20]}
    canonical = json.dumps(features, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    features["signature"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
    return features


def _normalise_token(value: str) -> str:
    return re.sub(r"[\s_\-]+", "", str(value or "")).upper()[:80]


def _count_band(count: int) -> int:
    if count <= 0:
        return 0
    return min(6, len(str(count)))


def normalize_floor_code(value: str) -> str:
    """Return the stable, project-wide floor identifier used by RVT and IR."""
    code = re.sub(r"\s+", "", str(value or "").upper())
    if not re.match(r"^(B[1-9]\d*|[1-9]\d*F|RF)$", code):
        raise ValueError("floor_code 必须是 B1、1F 或 RF 格式")
    return code


async def create_bim_project(tenant_id: str, owner_id: str, name: str,
                             project_id: str = "", template_path: str = "",
                             model_path: str = "") -> str:
    """Create the project container; levels are registered separately and never inferred."""
    requested = re.sub(r"[^A-Za-z0-9_-]", "", project_id or "").upper()
    if project_id and (not requested or len(requested) > 64):
        raise ValueError("project_id 只能包含字母、数字、下划线或连字符")
    resolved_id = requested or ("PRJ-" + uuid.uuid4().hex[:10].upper())
    async with engine.begin() as conn:
        await conn.execute(text("""
            INSERT INTO bim_projects
              (id, tenant_id, owner_id, name, template_path, model_path, status)
            VALUES (:id, :tenant, :owner, :name, :template, :model, 'active')
        """), {"id": resolved_id, "tenant": tenant_id, "owner": owner_id,
                "name": name, "template": template_path or None,
                "model": model_path or None})
    return resolved_id


async def register_project_level(tenant_id: str, project_id: str, floor_code: str,
                                 elevation_mm: int) -> dict[str, Any]:
    """Append a level to the project ledger, rejecting name/elevation conflicts."""
    code = normalize_floor_code(floor_code)
    async with engine.begin() as conn:
        project = (await conn.execute(text("""
            SELECT id FROM bim_projects WHERE id=:id AND tenant_id=:tenant AND status='active'
        """), {"id": project_id, "tenant": tenant_id})).fetchone()
        if not project:
            raise LookupError("项目不存在或不可用")
        rows = (await conn.execute(text("""
            SELECT floor_code, elevation_mm FROM bim_project_levels WHERE project_id=:project
        """), {"project": project_id})).fetchall()
        for existing_code, existing_elevation in rows:
            if existing_code == code:
                if int(existing_elevation) != int(elevation_mm):
                    raise ValueError("同名标高高程不一致，禁止覆盖")
                return {"floor_code": code, "elevation_mm": int(existing_elevation),
                        "created": False}
            if int(existing_elevation) == int(elevation_mm):
                raise ValueError("同一项目不允许两个楼层使用相同高程")
        await conn.execute(text("""
            INSERT INTO bim_project_levels (id, project_id, floor_code, elevation_mm)
            VALUES (:id, :project, :floor, :elevation)
        """), {"id": "LVL-" + uuid.uuid4().hex[:12].upper(),
                "project": project_id, "floor": code, "elevation": int(elevation_mm)})
    return {"floor_code": code, "elevation_mm": int(elevation_mm), "created": True}


async def get_project_levels(tenant_id: str, project_id: str) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        rows = (await conn.execute(text("""
            SELECT level.floor_code, level.elevation_mm
            FROM bim_project_levels AS level
            JOIN bim_projects AS project ON project.id=level.project_id
            WHERE project.id=:id AND project.tenant_id=:tenant
            ORDER BY level.elevation_mm
        """), {"id": project_id, "tenant": tenant_id})).fetchall()
    return [{"floor_code": row[0], "name": "标高 " + row[0],
             "elevation": int(row[1])} for row in rows]


def _profile_similarity(current: dict, saved: dict) -> float:
    scores = []
    for key in ("layers", "blocks", "active_layers"):
        left, right = set(current.get(key) or []), set(saved.get(key) or [])
        if left or right:
            scores.append(len(left & right) / len(left | right))
    left_types = set((current.get("entity_type_bands") or {}).keys())
    right_types = set((saved.get("entity_type_bands") or {}).keys())
    if left_types or right_types:
        scores.append(len(left_types & right_types) / len(left_types | right_types))
    return sum(scores) / len(scores) if scores else 0.0


async def match_drawing_profile(tenant_id: str, path: str,
                                threshold: float = 0.65) -> tuple[dict, dict | None]:
    fingerprint = drawing_fingerprint(path)
    async with engine.connect() as conn:
        rows = (await conn.execute(text("""
            SELECT id, name, signature, config FROM drawing_profiles
            WHERE tenant_id=:tenant AND is_active=true
        """), {"tenant": tenant_id})).fetchall()
    best, best_score = None, 0.0
    for row in rows:
        config = json.loads(row[3]) if row[3] else {}
        if row[2] == fingerprint["signature"]:
            score = 1.0
        else:
            score = _profile_similarity(fingerprint, config.get("fingerprint") or {})
        if score > best_score:
            best_score = score
            best = {"profile_id": row[0], "name": row[1], "config": config,
                    "match_score": round(score, 4)}
    return fingerprint, best if best and best_score >= threshold else None


async def create_build_run(build_id: str, review_id: str, tenant_id: str,
                           user_id: str, source_path: str,
                           profile_id: str | None = None) -> None:
    source_hash = file_sha256(source_path) if os.path.isfile(source_path) else None
    async with engine.begin() as conn:
        await conn.execute(text("""
            INSERT INTO build_runs
              (id, tenant_id, user_id, review_id, profile_id, source_path, source_hash,
               schema_version, pipeline_version, status)
            VALUES
              (:id, :tenant, :user, :review, :profile, :path, :hash, '2.0',
               'wall-overlap-v2', 'extracting')
        """), {"id": build_id, "tenant": tenant_id, "user": user_id,
                "review": review_id, "profile": profile_id,
                "path": source_path, "hash": source_hash})


async def update_build_run(build_id: str, status: str, *, model_scope: str | None = None,
                           quality: dict | None = None, gates: dict | None = None,
                           result: dict | None = None, error: str | None = None) -> None:
    finished = status in ("blocked", "done", "error")
    async with engine.begin() as conn:
        await conn.execute(text("""
            UPDATE build_runs SET
              status=:status,
              model_scope=COALESCE(:scope, model_scope),
              quality=COALESCE(:quality, quality),
              gate_status=COALESCE(:gates, gate_status),
              result=COALESCE(:result, result),
              error_msg=:error,
              finished_at=CASE WHEN :finished THEN CURRENT_TIMESTAMP ELSE finished_at END
            WHERE id=:id
        """), {"id": build_id, "status": status, "scope": model_scope,
                "quality": json.dumps(quality, ensure_ascii=False) if quality is not None else None,
                "gates": json.dumps(gates, ensure_ascii=False) if gates is not None else None,
                "result": json.dumps(result, ensure_ascii=False) if result is not None else None,
                "error": error, "finished": finished})


async def persist_element_mappings(build_id: str, tenant_id: str,
                                   created: list[dict]) -> None:
    async with engine.begin() as conn:
        for item in created:
            ir_id = str(item.get("input_id") or "")
            if not ir_id:
                continue
            await conn.execute(text("""
                INSERT INTO element_mappings
                  (id, tenant_id, build_id, source_element_id, ir_element_id,
                   revit_element_id, element_type, creation_mode, validation)
                VALUES (:id, :tenant, :build, :source, :ir, :revit, :type, :mode, :validation)
                ON CONFLICT (build_id, ir_element_id) DO UPDATE SET
                  revit_element_id=excluded.revit_element_id,
                  element_type=excluded.element_type,
                  creation_mode=excluded.creation_mode,
                  validation=excluded.validation
            """), {"id": str(uuid.uuid4()), "tenant": tenant_id, "build": build_id,
                    "source": item.get("source_element_id"), "ir": ir_id,
                    "revit": str(item.get("revit_element_id") or ""),
                    "type": item.get("kind"), "mode": item.get("mode"),
                    "validation": json.dumps(item.get("validation") or {}, ensure_ascii=False)})


async def create_profile(tenant_id: str, user_id: str, name: str, signature: str,
                         discipline: str, config: dict,
                         organization: str = "") -> str:
    profile_id = "DP-" + str(uuid.uuid4())[:8].upper()
    async with engine.begin() as conn:
        await conn.execute(text("""
            INSERT INTO drawing_profiles
              (id, tenant_id, name, organization, discipline, signature, config, created_by)
            VALUES (:id, :tenant, :name, :org, :discipline, :signature, :config, :user)
        """), {"id": profile_id, "tenant": tenant_id, "name": name,
                "org": organization, "discipline": discipline,
                "signature": signature, "config": json.dumps(config, ensure_ascii=False),
                "user": user_id})
    return profile_id


async def add_feedback(tenant_id: str, operator: str, payload: dict[str, Any]) -> str:
    feedback_id = "FB-" + str(uuid.uuid4())[:8].upper()
    async with engine.begin() as conn:
        await conn.execute(text("""
            INSERT INTO extraction_feedback
              (id, tenant_id, profile_id, build_id, source_element_id,
               predicted_type, correct_type, predicted_geometry, correct_geometry,
               reason, operator)
            VALUES (:id, :tenant, :profile, :build, :source, :predicted, :correct,
                    :predicted_geometry, :correct_geometry, :reason, :operator)
        """), {"id": feedback_id, "tenant": tenant_id,
                "profile": payload.get("profile_id"), "build": payload.get("build_id"),
                "source": payload.get("source_element_id"),
                "predicted": payload.get("predicted_type"),
                "correct": payload.get("correct_type"),
                "predicted_geometry": json.dumps(payload.get("predicted_geometry"), ensure_ascii=False),
                "correct_geometry": json.dumps(payload.get("correct_geometry"), ensure_ascii=False),
                "reason": payload.get("reason", ""), "operator": operator})
    return feedback_id
