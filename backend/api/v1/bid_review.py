"""标书审查 Agent REST 接口"""
import asyncio
import json
import uuid
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from pydantic import BaseModel, Field

from backend.core.orchestrator import AgentType, get_orchestrator
from backend.api.uploads import read_upload_limited
from backend.dependencies import get_current_user, llm_rate_limit
from sqlalchemy import text
from backend.db.session import engine
from backend.core.memory import build_config

router = APIRouter()


class BidReviewBody(BaseModel):
    project_id: str | None = Field(default=None, max_length=64)
    doc_text: str = Field("", description="投标文件文本（留空则用 mock 文档）")
    session_id: str = Field("default", min_length=1, max_length=128)


# ── 投标审查 ──────────────────────────────────────────────
# ── 投标审查（后台任务 + 轮询，对标行业范式/4.9 简历审查范式）──
_bid_tasks: set = set()          # 后台任务 GC 保护


def _get_bid_graph():
    """投标图复用 orchestrator 的懒加载缓存（删掉本模块的重复缓存层）"""
    return get_orchestrator()._get_agent_graph(AgentType.BID_REVIEW)


def _bid_config(state: dict) -> dict:
    # Each version has its own execution checkpoint; memory links the versions.
    return build_config(state["user_id"], state["review_id"], tenant_id=state["tenant_id"],
                        project_id=state.get("project_id"), agent="bid_review")


async def _persist_bid(review_id: str, user_id: str, tenant_id: str, status: str, **fields):
    """写/更新 bid_reviews 表（JSONB 用 json.dumps ensure_ascii=False）"""
    async with engine.begin() as conn:
        # upsert
        await conn.execute(text("""
            INSERT INTO bid_reviews (id, tenant_id, user_id, doc_name, structured_data, scores, issues, summary, status, error_msg, created_at, updated_at)
            VALUES (:id, :tenant_id, :user_id, :doc_name, :structured_data, :scores, :issues, :summary, :status, :error_msg, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT (id) DO UPDATE SET
              status=excluded.status, structured_data=excluded.structured_data,
              scores=excluded.scores, issues=excluded.issues,
              summary=excluded.summary, error_msg=excluded.error_msg, updated_at=CURRENT_TIMESTAMP
        """), {
            "id": review_id, "tenant_id": tenant_id, "user_id": user_id,
            "doc_name": fields.get("doc_name", "投标文件"),
            "structured_data": json.dumps(fields.get("structured_data") or {}, ensure_ascii=False)
                              if fields.get("structured_data") else None,
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
async def bid_review_upload(file: UploadFile, current_user: dict = Depends(get_current_user),
                            session_id: str = Form("default", max_length=128),
                            project_id: str | None = Form(None)):
    """上传投标 PDF → 解析文本 → 后台四维评审"""
    import tempfile
    import os
    MAX_UPLOAD = 500 * 1024 * 1024   # 单文件上限 500MB（盖章扫描件 PDF 可超 100MB）
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="仅支持 PDF 格式")
    # ★ H5 修复：分块读取边读边限额（旧代码先整体 read 再查大小，超大文件会先全量进内存）
    content = await read_upload_limited(
        file,
        max_bytes=MAX_UPLOAD,
        too_large_detail=f"文件过大（>500MB）：{file.filename}",
    )
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
    from backend.engines.pdf_parser import extract_pdf_text
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
        "session_id": session_id, "review_id": review_id, "project_id": project_id,
        "memory_turn_id": review_id,
        "doc_text": doc_text, "original_query": file.filename,
    }
    await _persist_bid(review_id, current_user["user_id"], current_user["tenant_id"], "processing",
                       doc_name=file.filename)

    async def _run():
        try:
            result = await graph.ainvoke(state, config=_bid_config(state))
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


# ── 多文件上传（技术标/商务标/资质分开投，PDF/Word/图片混合）──
_BID_MAX_FILES = 5
_BID_MAX_UPLOAD = 500 * 1024 * 1024   # 单份上限 500MB（盖章扫描件 PDF 可超 100MB）
_BID_ALLOWED_EXTS = {".pdf", ".docx", ".jpg", ".jpeg", ".png", ".bmp", ".txt", ".md"}


def _magic_ok(content: bytes, ext: str) -> bool:
    """按扩展名做魔数校验（防伪装扩展名的其他文件）"""
    if ext == ".pdf":
        return content[:5].startswith(b"%PDF-")
    if ext == ".docx":
        return content[:4] == b"PK\x03\x04"
    if ext in (".jpg", ".jpeg"):
        return content[:3] == b"\xff\xd8\xff"
    if ext == ".png":
        return content[:8] == b"\x89PNG\r\n\x1a\n"
    if ext == ".bmp":
        return content[:2] == b"BM"
    return True  # txt/md 无魔数


@router.post("/bid-review/upload-multi", status_code=202, dependencies=[Depends(llm_rate_limit)])
async def bid_review_upload_multi(files: list[UploadFile] = File(...),
                                  current_user: dict = Depends(get_current_user),
                                  session_id: str = Form("default", max_length=128),
                                  project_id: str | None = Form(None)):
    """上传多份投标文件（技术标/商务标/资质可分开；PDF/Word/图片混合）
    只收文件立即返回 202，解析+OCR+评审全部在后台任务执行（大扫描件解析需数分钟，不能阻塞请求）"""
    import tempfile
    import os

    if len(files) > _BID_MAX_FILES:
        raise HTTPException(status_code=400, detail=f"最多上传 {_BID_MAX_FILES} 份文件")

    tmp_files = []   # [{filename, tmp_path, ext}]
    for file in files:
        ext = os.path.splitext(file.filename or "")[1].lower()
        if ext not in _BID_ALLOWED_EXTS:
            raise HTTPException(status_code=400,
                                detail=f"不支持的文件类型：{ext}（支持 PDF/Word/图片/txt）")

        # 分块读取 + 限额（边读边查，超大文件不先进内存）
        content = await read_upload_limited(
            file,
            max_bytes=_BID_MAX_UPLOAD,
            too_large_detail=f"文件过大（>500MB）：{file.filename}",
        )
        if not content:
            raise HTTPException(status_code=400, detail=f"空文件：{file.filename}")
        if not _magic_ok(content, ext):
            raise HTTPException(status_code=400,
                                detail=f"文件内容与扩展名不符：{file.filename}")

        # 只落临时文件，解析放后台（大文件同步解析会撑爆请求超时）
        with tempfile.NamedTemporaryFile(delete=False, suffix=f"_bid{ext}") as tf:
            tf.write(content)
            tmp_files.append({"filename": file.filename, "tmp_path": tf.name, "ext": ext})

    if not tmp_files:
        raise HTTPException(status_code=400, detail="未收到有效文件")

    review_id = str(uuid.uuid4())
    doc_names = "、".join(f["filename"] for f in tmp_files)
    graph = _get_bid_graph()
    state = {
        "user_id": current_user["user_id"], "tenant_id": current_user["tenant_id"],
        "session_id": session_id, "review_id": review_id, "project_id": project_id,
        "memory_turn_id": review_id,
        "doc_text": "", "original_query": doc_names,
    }
    await _persist_bid(review_id, current_user["user_id"], current_user["tenant_id"], "processing",
                       doc_name=doc_names)

    async def _run():
        from backend.engines.pdf_parser import parse_document

        async def _parse_one(f):
            parsed = await parse_document(f["tmp_path"], f["filename"])
            return {"filename": f["filename"], "raw_text": parsed.get("raw_text", "")}

        documents = []
        try:
            # 后台并行解析（多份扫描件同时 OCR，总时长 ≈ 单份时长；不阻塞上传请求）
            documents = await asyncio.gather(*[_parse_one(f) for f in tmp_files])
            state["documents"] = documents
            state["doc_text"] = "\n\n===== 文件分隔 =====\n\n".join(
                f"【{d['filename']}】\n{d['raw_text']}" for d in documents)

            result = await graph.ainvoke(state, config=_bid_config(state))
            await _persist_bid(
                review_id, current_user["user_id"], current_user["tenant_id"], "done",
                doc_name=doc_names,
                structured_data=result.get("structured_output") or {},
                scores={"weighted_score": result.get("weighted_score", 0),
                        "dimensions": result.get("dimension_scores", [])},
                issues=result.get("issues", []),
                summary=result.get("summary", {}),
            )
        except Exception as e:
            await _persist_bid(review_id, current_user["user_id"], current_user["tenant_id"],
                               "failed", error_msg=str(e)[:500])
        finally:
            # 无论成败都清理临时文件（防泄漏）
            for f in tmp_files:
                try:
                    os.remove(f["tmp_path"])
                except Exception:
                    pass

    task = asyncio.create_task(_run(), name=review_id)
    task._bid_meta = {"review_id": review_id, "user_id": current_user["user_id"],
                      "tenant_id": current_user["tenant_id"]}
    _bid_tasks.add(task)
    task.add_done_callback(_on_bid_task_done)

    return {"review_id": review_id, "status": "processing", "files": [f["filename"] for f in tmp_files],
            "message": f"{len(tmp_files)} 份文件已接收，解析+评审进行中（大文件需数分钟）。"}


@router.post("/bid-review/review", status_code=202, dependencies=[Depends(llm_rate_limit)])
async def bid_review(body: BidReviewBody, current_user: dict = Depends(get_current_user)):
    """提交投标文件 → 后台四维并行评审 → 返回 review_id 供轮询"""
    review_id = str(uuid.uuid4())
    graph = _get_bid_graph()
    state = {
        "user_id": current_user["user_id"], "tenant_id": current_user["tenant_id"],
        "session_id": body.session_id, "review_id": review_id, "project_id": body.project_id,
        "memory_turn_id": review_id,
        "doc_text": body.doc_text, "original_query": body.doc_text[:200] or "投标文件审查",
    }
    await _persist_bid(review_id, current_user["user_id"], current_user["tenant_id"], "processing")

    async def _run():
        result = await graph.ainvoke(state, config=_bid_config(state))
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
            "SELECT status, scores, issues, summary, error_msg, created_at, structured_data FROM bid_reviews WHERE id = :id AND user_id = :uid"
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
        "structured_data": _json_loads(row[6]) or {},
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
