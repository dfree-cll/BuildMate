"""BIM 模型合规审查接口（对标投标审查的上传→后台审查→轮询范式）

POST /bim/upload   上传 .ifc → ifc_parser 提取 → 规则+LLM 双轨审查（后台）
GET  /bim/reviews/{id}  轮询结果（归属人校验）
GET  /bim/reviews       本人历史列表
"""
import asyncio
import json
import os
import tempfile
import uuid

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from sqlalchemy import text

from backend.core.logger import get_logger
from backend.dependencies import get_current_user, llm_rate_limit
from backend.db.session import engine

router = APIRouter()
logger = get_logger(__name__)

REVIEW_TIMEOUT_SECONDS = 15 * 60

_bim_tasks: set = set()              # 后台任务 GC 保护


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
              file_name=COALESCE(excluded.file_name, bim_reviews.file_name),
              issues=excluded.issues,
              summary=excluded.summary, error_msg=excluded.error_msg,
              updated_at=CURRENT_TIMESTAMP
        """), {
            "id": review_id, "tenant_id": tenant_id, "user_id": user_id,
            "file_name": fields.get("file_name", "模型.ifc"),
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
    except Exception:
        pass


@router.post("/bim/upload", status_code=202, dependencies=[Depends(llm_rate_limit)])
async def bim_upload(file: UploadFile, current_user: dict = Depends(get_current_user)):
    """上传 IFC 模型 → 后台解析提取 + 规则/LLM 双轨审查（上传立即返回 202）

    大模型适配（Revit 导出常 100MB+）：
    - 流式落盘：分块校验限额并直接写临时文件，不整文件进内存
    - 解析异步化：ifcopenshell 解析大模型可达分钟级，放在后台任务里，
      避免撞前端 120s 请求超时（旧实现同步解析是隐形失败点）
    """
    from backend.config import get_settings
    max_bytes = get_settings().bim_max_upload_mb * 1024 * 1024

    if not file.filename.lower().endswith(".ifc"):
        raise HTTPException(status_code=400, detail="仅支持 .ifc 格式（IFC2X3/IFC4）")

    review_id = str(uuid.uuid4())
    # 临时文件由 tempfile 随机命名（路径不含任何用户输入，无拼接），杜绝路径穿越
    size = 0
    header_checked = False
    with tempfile.NamedTemporaryFile(delete=False, suffix="_bim.ifc") as tf:
        while True:
            block = await file.read(4 * 1024 * 1024)
            if not block:
                break
            size += len(block)
            if size > max_bytes:
                tf.close()
                try:
                    os.remove(tf.name)
                except Exception:
                    pass
                raise HTTPException(
                    status_code=413,
                    detail=f"文件过大（>{get_settings().bim_max_upload_mb}MB）")
            # 魔数：IFC 是 STEP 文本格式，头部为 ISO-10303-21（首块即可判定）
            if not header_checked:
                if b"ISO-10303-21" not in block[:256]:
                    tf.close()
                    try:
                        os.remove(tf.name)
                    except Exception:
                        pass
                    raise HTTPException(status_code=400,
                                        detail="文件不是有效的 IFC 模型（缺少 ISO-10303-21 头）")
                header_checked = True
            tf.write(block)
        tmp_path = tf.name
    if size == 0:
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        raise HTTPException(status_code=400, detail="空文件")

    await _persist(review_id, current_user["user_id"], current_user["tenant_id"],
                   "processing", file_name=file.filename)

    async def _run():
        from backend.core.ifc_parser import parse_ifc
        from backend.core.bim_review import run_bim_review
        try:
            parsed = await parse_ifc(tmp_path)
            await _persist(review_id, current_user["user_id"], current_user["tenant_id"],
                           "processing", file_name=file.filename, structured_data=parsed)
            result = await run_bim_review(parsed)
            await _persist(review_id, current_user["user_id"], current_user["tenant_id"],
                           "done", file_name=file.filename,
                           issues=result["rule_issues"], summary=result)
        except Exception as e:
            await _persist(review_id, current_user["user_id"], current_user["tenant_id"],
                           "failed", file_name=file.filename, error_msg=str(e)[:500])
            raise
        finally:
            try:
                os.remove(tmp_path)
            except Exception:
                pass

    task = asyncio.create_task(_run(), name=review_id)
    task._bim_meta = {"review_id": review_id, "user_id": current_user["user_id"],
                      "tenant_id": current_user["tenant_id"]}
    _bim_tasks.add(task)
    task.add_done_callback(_on_task_done)

    return {
        "review_id": review_id, "status": "processing", "size": size,
        "message": "IFC 模型已接收，正在后台解析并双轨审查（大模型解析需数分钟），请稍后轮询。",
    }


def _json_loads(v):
    if not v:
        return None
    try:
        return json.loads(v)
    except Exception:
        return v


@router.get("/bim/reviews/{review_id}")
async def get_bim_review(review_id: str, current_user: dict = Depends(get_current_user)):
    """轮询审查结果（processing→done/failed，15 分钟超时兜底）"""
    from datetime import datetime, timezone
    async with engine.connect() as conn:
        row = (await conn.execute(text(
            "SELECT status, structured_data, issues, summary, error_msg, created_at "
            "FROM bim_reviews WHERE id = :id AND user_id = :uid"
        ), {"id": review_id, "uid": current_user["user_id"]})).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="审查记录不存在")

    status = row[0]
    if status == "processing":
        created = row[5]
        if created:
            try:
                created_dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
                if created_dt.tzinfo is None:
                    created_dt = created_dt.replace(tzinfo=timezone.utc)
                if (datetime.now(timezone.utc) - created_dt).total_seconds() > REVIEW_TIMEOUT_SECONDS:
                    await _persist(review_id, current_user["user_id"], current_user["tenant_id"],
                                   "failed", error_msg="审查超时")
                    status = "failed"
            except Exception:
                pass
    if status == "processing":
        return {"review_id": review_id, "status": "processing", "message": "审查进行中，请稍后重试"}
    if status == "failed":
        return {"review_id": review_id, "status": "failed", "error": row[4] or "审查失败"}

    summary = _json_loads(row[3]) or {}
    parsed = _json_loads(row[1]) or {}
    return {
        "review_id": review_id, "status": "done",
        "file": {"schema": parsed.get("schema"), "building": parsed.get("building"),
                 "elements_count": parsed.get("elements_count"),
                 "total_elements": parsed.get("total_elements"),
                 "total_spaces": parsed.get("total_spaces"),
                 "properties": (parsed.get("properties") or [])[:10]},
        "rule_issues": _json_loads(row[2]) or [],
        "risk_level": summary.get("risk_level", ""),
        "verdict": summary.get("verdict", ""),
        "observations": summary.get("observations", []),
        "suggestions": summary.get("suggestions", []),
        "summary": summary.get("summary", ""),
    }


@router.get("/bim/reviews")
async def list_bim_reviews(current_user: dict = Depends(get_current_user)):
    """本人 BIM 审查历史"""
    async with engine.connect() as conn:
        rows = (await conn.execute(text(
            "SELECT id, file_name, status, created_at FROM bim_reviews "
            "WHERE user_id = :uid ORDER BY created_at DESC LIMIT 20"
        ), {"uid": current_user["user_id"]})).fetchall()
    return {"items": [
        {"review_id": r[0], "file_name": r[1], "status": r[2],
         "created_at": str(r[3]) if r[3] else None} for r in rows
    ]}
