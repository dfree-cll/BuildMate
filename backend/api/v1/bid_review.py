"""标书审查 Agent REST 接口"""
import asyncio
import json
import uuid
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel, Field

from backend.core.orchestrator import AgentType, get_orchestrator
from backend.dependencies import get_current_user, llm_rate_limit
from sqlalchemy import text
from backend.db.session import engine

router = APIRouter()


class BidReviewBody(BaseModel):
    doc_text: str = Field("", description="投标文件文本（留空则用 mock 文档）")
    session_id: str = "default"


# ── 投标审查 ──────────────────────────────────────────────
# ── 投标审查（后台任务 + 轮询，对标行业范式/4.9 简历审查范式）──
_bid_tasks: set = set()          # 后台任务 GC 保护


def _get_bid_graph():
    """投标图复用 orchestrator 的懒加载缓存（删掉本模块的重复缓存层）"""
    return get_orchestrator()._get_agent_graph(AgentType.BID_REVIEW)


async def _persist_bid(review_id: str, user_id: str, tenant_id: str, status: str, **fields):
    """写/更新 bid_reviews 表（JSONB 用 json.dumps ensure_ascii=False）"""
    async with engine.begin() as conn:
        # upsert
        await conn.execute(text("""
            INSERT INTO bid_reviews (id, tenant_id, user_id, doc_name, structured_data, scores, issues, summary, status, error_msg, created_at, updated_at)
            VALUES (:id, :tenant_id, :user_id, :doc_name, :structured_data, :scores, :issues, :summary, :status, :error_msg, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT (id) DO UPDATE SET
              status=excluded.status, scores=excluded.scores, issues=excluded.issues,
              summary=excluded.summary, error_msg=excluded.error_msg, updated_at=CURRENT_TIMESTAMP
        """), {
            "id": review_id, "tenant_id": tenant_id, "user_id": user_id,
            "doc_name": fields.get("doc_name", "投标文件"),
            "structured_data": fields.get("structured_data"),
            "scores": json.dumps(fields.get("scores", {}), ensure_ascii=False) if fields.get("scores") else None,
            "issues": json.dumps(fields.get("issues", []), ensure_ascii=False) if fields.get("issues") else None,
            "summary": json.dumps(fields.get("summary", {}), ensure_ascii=False) if fields.get("summary") else None,
            "status": status, "error_msg": fields.get("error_msg"),
        })


def _on_bid_task_done(task):
    _bid_tasks.discard(task)
    try:
        if not task.cancelled() and task.exception() is not None:
            # 后台失败 → 标记 failed（回写真实 user_id/tenant_id，保证查询端可见）
            exc = task.exception()
            meta = getattr(task, "_bid_meta", {})
            asyncio.ensure_future(_persist_bid(
                meta.get("review_id", task.get_name()),
                meta.get("user_id", ""), meta.get("tenant_id", ""),
                "failed", error_msg=str(exc)[:500]))
    except Exception:
        pass


@router.post("/bid-review/upload", status_code=202, dependencies=[Depends(llm_rate_limit)])
async def bid_review_upload(file: UploadFile, current_user: dict = Depends(get_current_user)):
    """上传投标 PDF → 解析文本 → 后台四维评审"""
    import tempfile
    import os
    MAX_UPLOAD = 20 * 1024 * 1024
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="仅支持 PDF 格式")
    # ★ H5 修复：分块读取边读边限额（旧代码先整体 read 再查大小，超大文件会先全量进内存）
    chunks: list[bytes] = []
    size = 0
    while True:
        block = await file.read(1024 * 1024)
        if not block:
            break
        size += len(block)
        if size > MAX_UPLOAD:
            raise HTTPException(status_code=413, detail="文件过大（>20MB）")
        chunks.append(block)
    content = b"".join(chunks)
    if not content:
        raise HTTPException(status_code=400, detail="空文件")
    # ★ 魔数校验：PDF 以 %PDF- 开头（防伪装 .pdf 的其他文件）
    if not content[:5].startswith(b"%PDF-"):
        raise HTTPException(status_code=400, detail="文件不是有效的 PDF（魔数校验失败）")

    review_id = str(uuid.uuid4())
    # 临时文件由 tempfile 随机命名（路径不含任何用户输入，无拼接）
    with tempfile.NamedTemporaryFile(delete=False, suffix="_bid.pdf") as tf:
        tf.write(content)
        tmp_path = tf.name

    # 解析 PDF（★ M5：解析失败立即清理临时文件，不留残骸）
    from backend.core.pdf_parser import extract_pdf_text
    try:
        parsed = await extract_pdf_text(tmp_path)
    except Exception as e:
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        raise HTTPException(status_code=422, detail=f"PDF 解析失败：{str(e)[:200]}")
    doc_text = parsed["raw_text"]

    # 后台评审
    graph = _get_bid_graph()
    state = {
        "user_id": current_user["user_id"], "tenant_id": current_user["tenant_id"],
        "session_id": "bid-" + str(uuid.uuid4())[:8], "review_id": review_id,
        "doc_text": doc_text, "original_query": file.filename,
    }
    await _persist_bid(review_id, current_user["user_id"], current_user["tenant_id"], "processing",
                       doc_name=file.filename)

    async def _run():
        try:
            result = await graph.ainvoke(state)
            await _persist_bid(
                review_id, current_user["user_id"], current_user["tenant_id"], "done",
                doc_name=file.filename,
                scores={"weighted_score": result.get("weighted_score", 0),
                        "dimensions": result.get("dimension_scores", [])},
                issues=result.get("issues", []),
                summary=result.get("summary", {}),
            )
        finally:
            # 无论成败都清理临时 PDF（防泄漏）
            try: os.remove(tmp_path)
            except Exception: pass

    task = asyncio.create_task(_run(), name=review_id)
    task._bid_meta = {"review_id": review_id, "user_id": current_user["user_id"],
                      "tenant_id": current_user["tenant_id"]}
    _bid_tasks.add(task)
    task.add_done_callback(_on_bid_task_done)

    return {"review_id": review_id, "status": "processing", "pages": parsed["page_count"],
            "chars": len(doc_text), "message": "PDF 已上传并解析，正在四维评审中。"}


@router.post("/bid-review/review", status_code=202, dependencies=[Depends(llm_rate_limit)])
async def bid_review(body: BidReviewBody, current_user: dict = Depends(get_current_user)):
    """提交投标文件 → 后台四维并行评审 → 返回 review_id 供轮询"""
    review_id = str(uuid.uuid4())
    graph = _get_bid_graph()
    state = {
        "user_id": current_user["user_id"], "tenant_id": current_user["tenant_id"],
        "session_id": body.session_id, "review_id": review_id,
        "doc_text": body.doc_text, "original_query": body.doc_text[:200] or "投标文件审查",
    }
    await _persist_bid(review_id, current_user["user_id"], current_user["tenant_id"], "processing")

    async def _run():
        result = await graph.ainvoke(state)
        await _persist_bid(
            review_id, current_user["user_id"], current_user["tenant_id"], "done",
            doc_name=state["original_query"][:100],
            scores={"weighted_score": result.get("weighted_score", 0),
                    "dimensions": result.get("dimension_scores", [])},
            issues=result.get("issues", []),
            summary=result.get("summary", {}),
        )
        return result

    task = asyncio.create_task(_run(), name=review_id)
    _bid_tasks.add(task)
    task.add_done_callback(_on_bid_task_done)

    return {"review_id": review_id, "status": "processing",
            "message": "投标文件已提交，正在四维并行评审中，预计 30-60 秒完成。"}


@router.get("/bid-review/reviews/{review_id}")
async def get_bid_review(review_id: str, current_user: dict = Depends(get_current_user)):
    """轮询评审结果（状态机 processing→done→failed→404；15 分钟超时兜底，对标行业范式）"""
    REVIEW_TIMEOUT_SECONDS = 15 * 60
    async with engine.connect() as conn:
        row = (await conn.execute(text(
            "SELECT status, scores, issues, summary, error_msg, created_at FROM bid_reviews WHERE id = :id AND user_id = :uid"
        ), {"id": review_id, "uid": current_user["user_id"]})).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="评审记录不存在")

    status = row[0]
    # 超时兜底：processing 且超过 15 分钟 → 标记 failed（created 可能是 str，用字符串比较）
    if status == "processing":
        created = row[5]
        from datetime import datetime, timezone
        if created:
            try:
                created_dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
                # SQLite 返回 naive datetime → 补时区为 UTC（否则与 aware now 相减 TypeError）
                if created_dt.tzinfo is None:
                    created_dt = created_dt.replace(tzinfo=timezone.utc)
                if (datetime.now(timezone.utc) - created_dt).total_seconds() > REVIEW_TIMEOUT_SECONDS:
                    await _persist_bid(review_id, current_user["user_id"], current_user["tenant_id"], "failed", error_msg="评审超时")
                    status = "failed"
            except Exception:
                pass

    if status == "processing":
        return {"review_id": review_id, "status": "processing", "message": "评审进行中，请稍后重试"}
    if status == "failed":
        return {"review_id": review_id, "status": "failed", "error": row[4] or "评审失败"}
    # done
    def _json_loads(v):
        if not v: return None
        try: return json.loads(v)
        except Exception: return v
    return {
        "review_id": review_id, "status": "done",
        "weighted_score": (_json_loads(row[1]) or {}).get("weighted_score", 0) if row[1] else 0,
        "dimensions": (_json_loads(row[1]) or {}).get("dimensions", []) if row[1] else [],
        "issues": _json_loads(row[2]) or [],
        "summary": _json_loads(row[3]) or {},
    }


@router.get("/bid-review/reviews")
async def list_bid_reviews(current_user: dict = Depends(get_current_user)):
    """本人评审列表（倒序，对齐行业范式）"""
    async with engine.connect() as conn:
        rows = (await conn.execute(text(
            "SELECT id, doc_name, status, created_at FROM bid_reviews WHERE user_id = :uid ORDER BY created_at DESC LIMIT 20"
        ), {"uid": current_user["user_id"]})).fetchall()
    return {"items": [
        {"review_id": r[0], "doc_name": r[1], "status": r[2], "created_at": str(r[3]) if r[3] else None}
        for r in rows
    ]}
