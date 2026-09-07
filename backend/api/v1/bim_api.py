"""Retired BIM compatibility adapter.

The module remains importable for data migration and regression fixtures, but
is hidden from the public OpenAPI schema and is not used by the frontend. New
BIM traffic must use ``/api/v2/workflows`` with ``workflow=wall_pipeline``.
"""
import asyncio
import json
import os
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from pydantic import BaseModel, Field
from langgraph.types import Command
from sqlalchemy import text

from backend.core.orchestrator import AgentType, get_orchestrator
from backend.api.uploads import read_upload_limited
from backend.core.memory import build_config
from backend.dependencies import get_current_user, require_role, llm_rate_limit
from backend.core.logger import get_logger
from backend.config import get_settings
from backend.db.session import engine
from backend.engines.drawing_guidance import DWG_GUIDANCE

router = APIRouter()
logger = get_logger(__name__)

# ── 上传配置 ──
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
_UPLOAD_DIR = os.path.join(_PROJECT_ROOT, "data", "uploads")
_WALL_SOURCE_EXTS = {".dxf", ".pdf", ".dwg"}
_LEGACY_IMAGE_EXTS = {".png", ".jpg", ".jpeg"}
_ALLOWED_EXTS = _WALL_SOURCE_EXTS | {".ifc"} | _LEGACY_IMAGE_EXTS
_MAX_UPLOAD = 50 * 1024 * 1024   # 50MB
_LEGACY_SOURCE_ROOTS = tuple(
    Path(_PROJECT_ROOT, "data", name).resolve()
    for name in ("uploads", "uploads_v2", "runtime", "revit", "generated")
)
WALL_V2_GUIDANCE = (
    "BIM 统一入口为 /api/v2/artifacts + /api/v2/workflows（workflow=wall_pipeline），"
    "以执行 WallEvidence→WallModel→Revit 审批链路。DWG 请先由 ODA Converter 转为 DXF。"
)

_bim_tasks: set = set()              # 后台任务 GC 保护


class BimConfirmBody(BaseModel):
    decision: str = Field(..., pattern="^(approved|rejected)$")
    comment: str = ""


class BimReviewBody(BaseModel):
    """JSON 模式提交（黄金基准注入 / MCP 网关路径）"""
    drawing_path: str = Field("", description="可选：图纸文件路径（PDF，经 MCP 网关解析）")
    ifc_path: str = Field("", description="可选：IFC 文件路径（经 MCP 网关解析生成基准）")
    golden_baseline: list[dict] = Field(default_factory=list, description="可选：手工/上游注入的黄金基准 JSON")
    drawing_review_key: str = Field("", description="可选：图纸标识（变更检测哈希 key）")
    generate_ifc: bool = Field(False, description="审查通过后是否生成 IFC 模型")
    perception_mode: str = Field("auto", description="感知模式：text/vision/auto")
    session_id: str = "default"


def _validate_legacy_source_path(value: str, field_name: str) -> str:
    """Restrict retired JSON path inputs when the service is production.

    The v1 endpoint remains only for migration fixtures.  In production an
    arbitrary server path would turn that compatibility surface into a local
    file-read primitive.  Local/test mode keeps the historical contract so
    existing fixtures and offline demonstrations remain runnable; new traffic
    should use artifact IDs through the v2 workflow.
    """

    if not value:
        return ""
    candidate = Path(value).expanduser().resolve(strict=False)
    if str(get_settings().app_env).lower() != "production":
        return str(candidate)
    if not any(candidate == root or root in candidate.parents for root in _LEGACY_SOURCE_ROOTS):
        raise HTTPException(
            status_code=400,
            detail=f"{field_name} must reference an uploaded BuildMate artifact",
        )
    return str(candidate)


def _get_d2b_graph():
    return get_orchestrator()._get_agent_graph(AgentType.DRAWING2BIM)


def _review_config(review_id: str) -> dict:
    """thread 归属固定 system 前缀：创建与确认同线程（规避跨用户线程错位）"""
    return build_config("system", review_id)


# ── 持久化（bim_reviews 表，ON CONFLICT 照抄投标模式）──────────
async def _persist(review_id: str, user_id: str, tenant_id: str, status: str, **fields):
    async with engine.begin() as conn:
        await conn.execute(text("""
            INSERT INTO bim_reviews (id, tenant_id, user_id, file_name, structured_data,
                                     issues, summary, status, error_msg,
                                     created_at, updated_at)
            VALUES (:id, :tenant_id, :user_id, :file_name, :structured_data,
                    :issues, :summary, :status, :error_msg,
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT (id) DO UPDATE SET
              status=excluded.status,
              structured_data=COALESCE(excluded.structured_data, bim_reviews.structured_data),
              file_name=COALESCE(bim_reviews.file_name, excluded.file_name),
              issues=excluded.issues,
              summary=excluded.summary, error_msg=excluded.error_msg,
              updated_at=CURRENT_TIMESTAMP
        """), {
            "id": review_id, "tenant_id": tenant_id, "user_id": user_id,
            "file_name": fields.get("file_name", "图纸.dxf"),
            "structured_data": (json.dumps(fields["structured_data"], ensure_ascii=False)
                                if isinstance(fields.get("structured_data"), dict)
                                else fields.get("structured_data")),
            "issues": json.dumps(fields["issues"], ensure_ascii=False) if fields.get("issues") else None,
            "summary": json.dumps(fields["summary"], ensure_ascii=False) if fields.get("summary") else None,
            "status": status, "error_msg": fields.get("error_msg"),
        })


def _on_task_done(task):
    _bim_tasks.discard(task)
    try:
        if not task.cancelled() and task.exception() is not None:
            meta = getattr(task, "_bim_meta", {})
            asyncio.ensure_future(_persist(
                meta.get("review_id", task.get_name()),
                meta.get("user_id", ""), meta.get("tenant_id", ""),
                "failed", error_msg=str(task.exception())[:500]))
    except Exception as exc:
        # A done-callback runs outside the request/worker error path.  Do not
        # silently discard a failed persistence attempt: the review row is the
        # only durable status visible to legacy clients and operators.
        logger.error(
            "bim.review_done_callback_failed",
            error=str(exc)[:300],
            exc_info=True,
        )


def _persist_from_result(result: dict, meta: dict):
    """评审结果落库（done / pending_confirmation）"""
    notes = (result.get("drawing_notes") or "")[:3000]
    source = {
        "path": meta.get("source_path", ""),
        "kind": meta.get("source_kind", ""),
    }
    interrupted = "compliance_report" not in result
    if interrupted:
        merged = result.get("merged_report", {})
        return _persist(
            meta["review_id"], meta["user_id"], meta["tenant_id"],
            "pending_confirmation",
            structured_data={"risk_level": merged.get("risk_level"),
                             "hard_count": merged.get("hard_count", 0),
                             "soft_count": merged.get("soft_count", 0),
                             "hitl_items": merged.get("violations", []),
                             "design_notes": notes,
                             "rule_stats": result.get("rule_stats", {}),
                             "element_stats": result.get("element_stats", {}),
                             "slab_categories": result.get("slab_categories", {}),
                             "slab_finishing_count": result.get("slab_finishing_count", 0),
                             "source": source,
                             "status": "pending_confirmation"},
            issues=[], summary={"message": "审查发现违规或低置信度条目，需人工确认。"},
        )
    report = result.get("compliance_report", {})
    return _persist(
        meta["review_id"], meta["user_id"], meta["tenant_id"], "done",
        structured_data={
            "verdict": report.get("verdict"),
            "risk_level": report.get("risk_level"),
            "hard_count": report.get("hard_count", 0),
            "soft_count": report.get("soft_count", 0),
            "compliance_report": report,
            "structured_output": result.get("structured_output"),
            "ifc_generation": result.get("ifc_generation"),
            "design_notes": notes,
            "slab_categories": result.get("slab_categories", {}),
            "rule_stats": result.get("rule_stats", {}),
            "element_stats": result.get("element_stats", {}),
            "slab_finishing_count": result.get("slab_finishing_count", 0),
            "source": source,
            "status": "done",
        },
        issues=[], summary={"message": report.get("conclusion", "")},
    )


async def _run_review_background(state: dict, review_id: str, meta: dict):
    """后台跑 drawing2bim 管线：invoke 图 → 落库（interrupt 时状态 pending_confirmation）"""
    try:
        graph = _get_d2b_graph()
        config = _review_config(review_id)
        result = await graph.ainvoke(state, config=config)
        await _persist_from_result(result, meta)
    except Exception as e:
        logger.warning("bim.review_task_failed", review_id=review_id, error=str(e)[:200])
        await _persist(meta["review_id"], meta["user_id"], meta["tenant_id"],
                       "failed", error_msg=str(e)[:500])


# ── JSON 模式提交（黄金基准注入 / MCP 网关路径）──────────
# Deprecated compatibility endpoint.  IFC/image and injected baselines remain
# supported here.  Direct wall paths are retained only for old non-Revit
# clients; all wall-to-Revit delivery must use the v2 artifact + workflow
# contract (the upload endpoint enforces this boundary).
@router.post("/bim/review", dependencies=[Depends(llm_rate_limit)], deprecated=True)
async def submit_review(body: BimReviewBody, current_user: dict = Depends(get_current_user)):
    """提交黄金基准审查（JSON 模式：服务器路径/注入基准；异步后台 + 持久化）"""
    if not body.drawing_path and not body.ifc_path and not body.golden_baseline:
        raise HTTPException(status_code=400,
                            detail="请提供 drawing_path、ifc_path 或 golden_baseline 至少一项")

    # Validate legacy paths before they enter the background graph.  The v2
    # artifact workflow never accepts server paths; this guard only protects
    # the retired compatibility endpoint.
    drawing_path = _validate_legacy_source_path(body.drawing_path, "drawing_path")
    ifc_path = _validate_legacy_source_path(body.ifc_path, "ifc_path")

    review_id = "BIM-" + str(uuid.uuid4())[:8].upper()
    user_id, tenant_id = current_user["user_id"], current_user["tenant_id"]
    source_path = drawing_path or ifc_path
    source_kind = "ifc" if ifc_path else (os.path.splitext(drawing_path)[1].lower().lstrip(".") or "json")
    meta = {"review_id": review_id, "user_id": user_id, "tenant_id": tenant_id,
            "source_path": source_path, "source_kind": source_kind}
    state = {
        "user_id": user_id, "tenant_id": tenant_id,
        "role": current_user["role"],
        "session_id": body.session_id, "input_text": f"合规审查 {review_id}",
        "drawing_path": drawing_path, "ifc_path": ifc_path,
        "drawing_review_key": body.drawing_review_key or body.session_id,
        "golden_baseline": body.golden_baseline,
        "generate_ifc": body.generate_ifc,
        "perception_mode": body.perception_mode,
    }
    await _persist(review_id, user_id, tenant_id, "pending",
                   structured_data={"status": "pending",
                                    "source": {"path": source_path, "kind": source_kind}},
                   issues=[], summary={})
    task = asyncio.ensure_future(_run_review_background(state, review_id, meta))
    task._bim_meta = meta
    _bim_tasks.add(task)
    task.add_done_callback(_on_task_done)
    return {"review_id": review_id, "status": "pending", "message": "审查已提交，后台执行中"}


# ── 上传：202 立即返回，后台跑审查管线（多文件）──────────
@router.post("/bim/upload", status_code=202, dependencies=[Depends(llm_rate_limit)])
async def bim_upload(files: list[UploadFile] = File(...),
                     generate_ifc: bool = Form(False),
                     perception_mode: str = Form("auto"),
                     current_user: dict = Depends(get_current_user)):
    """上传 IFC/图片进入兼容 Agent；PDF/DWG/DXF 必须走 v2 墙体流水线。"""
    if not files:
        raise HTTPException(status_code=400, detail="请至少上传一个文件")
    if len(files) > 5:
        raise HTTPException(status_code=400, detail="最多上传 5 份文件")

    reviews = []
    for file in files:
        filename = file.filename or "drawing"
        ext = os.path.splitext(filename)[1].lower()
        if ext in _WALL_SOURCE_EXTS:
            # Do not let the legacy endpoint feed a wall source to the old
            # perception Agent.  The v2 artifact/workflow boundary performs
            # ODA conversion, deterministic geometry gates and HITL before
            # any Revit hand-off.
            logger.info("bim.upload_wall_source_requires_v2", filename=filename)
            raise HTTPException(status_code=400, detail=WALL_V2_GUIDANCE)
        if ext not in _ALLOWED_EXTS:
            raise HTTPException(status_code=400,
                                detail=f"{filename}：不支持的文件格式（支持 IFC / PNG / JPG）")

        content = await read_upload_limited(
            file,
            max_bytes=_MAX_UPLOAD,
            too_large_detail=f"{filename}：文件过大（>50MB）",
        )
        if not content:
            raise HTTPException(status_code=400, detail=f"{filename}：空文件")

        os.makedirs(_UPLOAD_DIR, exist_ok=True)
        # 保存到隔离上传目录；save_upload 使用 ODA 转换 DWG。
        from backend.engines.adaptive_pipeline import save_upload
        try:
            drawing_path = save_upload(content, filename)
        except RuntimeError as e:
            # DWG 转换失败（私有格式/损坏/版本不支持）→ 400 + 指引，而非 500 裸异常
            logger.warning("bim.upload_convert_failed", filename=filename, error=str(e)[:150])
            raise HTTPException(status_code=400, detail=DWG_GUIDANCE)
        logger.info("bim.upload_saved", filename=filename, size=len(content), dxf=drawing_path)

        review_id = "BIM-" + str(uuid.uuid4())[:8].upper()
        user_id, tenant_id = current_user["user_id"], current_user["tenant_id"]
        source_kind = os.path.splitext(drawing_path)[1].lower().lstrip(".")
        meta = {"review_id": review_id, "user_id": user_id, "tenant_id": tenant_id,
                "source_path": drawing_path, "source_kind": source_kind}

        state = {
            "user_id": user_id, "tenant_id": tenant_id,
            "role": current_user["role"],
            "session_id": review_id, "input_text": f"合规审查 {review_id}",
            "drawing_path": drawing_path if ext in _LEGACY_IMAGE_EXTS else "",
            "ifc_path": drawing_path if ext == ".ifc" else "",
            "drawing_review_key": review_id,
            "golden_baseline": [],
            "generate_ifc": generate_ifc,
            "perception_mode": perception_mode,
        }

        # 先落 pending（轮询可立即查）
        await _persist(review_id, user_id, tenant_id, "pending",
                       structured_data={"status": "pending",
                                        "source": {"path": drawing_path, "kind": source_kind}},
                       issues=[], summary={},
                       file_name=filename)

        task = asyncio.ensure_future(_run_review_background(state, review_id, meta))
        task._bim_meta = meta
        _bim_tasks.add(task)
        task.add_done_callback(_on_task_done)

        reviews.append({"review_id": review_id, "file_name": filename, "status": "pending"})

    return {"reviews": reviews, "message": f"{len(reviews)} 份文件已上传，后台审查中（图纸→BIM 管线）"}


# ── 轮询 / 历史 / 确认 ──────────
@router.get("/bim/reviews/{review_id}")
async def get_bim_review(review_id: str, current_user: dict = Depends(get_current_user)):
    async with engine.connect() as conn:
        row = (await conn.execute(text(
            "SELECT id, user_id, tenant_id, file_name, structured_data, status, error_msg "
            "FROM bim_reviews WHERE id = :id AND tenant_id = :tenant_id"),
            {"id": review_id, "tenant_id": current_user["tenant_id"]})).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="审查记录不存在")
    if row[1] != current_user["user_id"] and current_user["role"] not in ("admin", "reviewer"):
        raise HTTPException(status_code=403, detail="无权查看该记录")
    return {
        "review_id": row[0], "status": row[5],
        "file_name": row[3],
        "structured_data": json.loads(row[4]) if row[4] else None,
        "error_msg": row[6],
    }

@router.get("/bim/reviews")
async def list_bim_reviews(current_user: dict = Depends(get_current_user)):
    """Return the authenticated user's BIM review history.

    The v1 endpoint is a personal-history view (matching the original
    contract), so a tenant filter alone is not sufficient: users in the same
    tenant must not learn one another's file names or review states.  Admin
    and reviewer management views should use a dedicated, explicitly scoped
    endpoint rather than broadening this compatibility route.
    """
    async with engine.connect() as conn:
        rows = (await conn.execute(text(
            "SELECT id, file_name, status, created_at FROM bim_reviews "
            "WHERE tenant_id = :tenant_id AND user_id = :user_id "
            "ORDER BY created_at DESC LIMIT 50"),
            {"tenant_id": current_user["tenant_id"],
             "user_id": current_user["user_id"]})).fetchall()
    reviews = [{"review_id": r[0], "file_name": r[1], "status": r[2],
                "created_at": str(r[3]) if r[3] else None}
               for r in rows]
    # ``reviews`` is the current frontend contract; ``items`` preserves the
    # response key used by older v1 clients.
    return {"reviews": reviews, "items": reviews}


@router.post("/bim/reviews/{review_id}/confirm")
async def confirm_bim_review(review_id: str, body: BimConfirmBody,
                             current_user: dict = Depends(require_role("admin", "reviewer"))):
    """人工确认：Command(resume=...) 恢复被 interrupt 的图（仅 admin/reviewer）"""
    # The LangGraph checkpoint key is derived from ``review_id`` only.  Bind
    # the review to the authenticated tenant before loading that checkpoint;
    # otherwise a reviewer who guesses another tenant's id could resume and
    # rewrite its persisted result.
    async with engine.connect() as conn:
        row = (await conn.execute(text(
            "SELECT id, user_id, tenant_id, status, structured_data "
            "FROM bim_reviews WHERE id = :id AND tenant_id = :tenant_id"),
            {"id": review_id, "tenant_id": current_user["tenant_id"]})).fetchone()
    if not row or row[3] != "pending_confirmation":
        raise HTTPException(status_code=404, detail="该审查不存在或没有待确认任务")
    if row[1] != current_user["user_id"] and current_user["role"] not in ("admin", "reviewer"):
        raise HTTPException(status_code=403, detail="无权确认该记录")
    graph = _get_d2b_graph()
    config = _review_config(review_id)

    snap = await graph.aget_state(config)
    if snap is None or not snap.next:
        raise HTTPException(status_code=404, detail="该审查不存在或没有待确认任务")

    resume_data = {"decision": body.decision, "comment": body.comment,
                   "operator": current_user["user_id"]}
    result = await graph.ainvoke(Command(resume=resume_data), config=config)

    report = result.get("compliance_report", {})
    # 恢复后持久化时保留首次上传绑定的源文件，避免人工确认覆盖 source。
    previous = json.loads(row[4]) if row[4] else {}
    source = previous.get("source") or {}
    meta = {"review_id": review_id, "user_id": current_user["user_id"],
            "tenant_id": current_user["tenant_id"],
            "source_path": source.get("path", ""),
            "source_kind": source.get("kind", "")}
    await _persist_from_result(result, meta)
    logger.info("bim.confirmed", review_id=review_id, decision=body.decision,
                operator=current_user["user_id"])
    return {
        "review_id": review_id,
        "status": "done",
        "verdict": report.get("verdict"),
        "risk_level": report.get("risk_level"),
        "compliance_report": report,
        "structured_output": result.get("structured_output"),
        "ifc_generation": result.get("ifc_generation"),
        "message": result.get("content", "确认完成"),
    }
