"""Tenant-scoped persistent chat sessions and ordered messages."""

from __future__ import annotations

import json
import uuid

from sqlalchemy import text

from backend.db.session import engine
from backend.domain.contracts import RequestContext
from backend.domain.errors import ResourceNotFound
from backend.rag.contracts import RAGAnswer


class ChatRepository:
    async def create_session(self, context: RequestContext, title: str) -> dict:
        session_id = "chat_" + uuid.uuid4().hex
        async with engine.begin() as conn:
            await conn.execute(text("""
                INSERT INTO chat_sessions_v2 (
                    id, tenant_id, project_id, title, status, created_by, version
                ) VALUES (
                    :id, :tenant_id, :project_id, :title, 'active', :created_by, 1
                )
            """), {
                "id": session_id, "tenant_id": context.tenant_id,
                "project_id": context.project_id, "title": title[:256],
                "created_by": context.user_id,
            })
        return await self.get_session(context, session_id)

    async def get_session(self, context: RequestContext, session_id: str) -> dict:
        async with engine.connect() as conn:
            row = (await conn.execute(text("""
                SELECT id, tenant_id, project_id, title, status, created_by,
                       created_at, updated_at, version
                FROM chat_sessions_v2
                WHERE id=:id AND tenant_id=:tenant_id
                  AND ((CAST(:project_id AS TEXT) IS NULL AND project_id IS NULL)
                       OR project_id=CAST(:project_id AS TEXT))
                  AND created_by=:user_id
            """), {
                "id": session_id, "tenant_id": context.tenant_id,
                "project_id": context.project_id, "user_id": context.user_id,
            })).mappings().first()
        if not row:
            raise ResourceNotFound("chat session not found")
        return dict(row)

    async def append_user_and_answer(
        self,
        context: RequestContext,
        session_id: str,
        user_content: str,
        answer: RAGAnswer,
    ) -> list[dict]:
        await self.get_session(context, session_id)
        async with engine.begin() as conn:
            # Serialize seq allocation for concurrent messages in a single session.
            await conn.execute(text("""
                UPDATE chat_sessions_v2 SET updated_at=CURRENT_TIMESTAMP, version=version+1
                WHERE id=:id AND tenant_id=:tenant_id AND created_by=:user_id
            """), {"id": session_id, "tenant_id": context.tenant_id, "user_id": context.user_id})
            next_seq = int((await conn.execute(text("""
                SELECT COALESCE(MAX(seq), 0) + 1 FROM chat_messages_v2
                WHERE session_id=:session_id AND tenant_id=:tenant_id
            """), {
                "session_id": session_id, "tenant_id": context.tenant_id,
            })).scalar_one())
            messages = [
                ("user", user_content, None, [], None, False),
                (
                    "assistant", answer.answer, answer.retrieval_run_id,
                    [item.model_dump(mode="json") for item in answer.citations],
                    answer.confidence, answer.grounded,
                ),
            ]
            for offset, (role, content, retrieval_id, citations, confidence, grounded) in enumerate(messages):
                await conn.execute(text("""
                    INSERT INTO chat_messages_v2 (
                        id, session_id, tenant_id, project_id, seq, role,
                        content, retrieval_run_id, citations, confidence,
                        grounded, created_by, version
                    ) VALUES (
                        :id, :session_id, :tenant_id, :project_id, :seq, :role,
                        :content, :retrieval_run_id, :citations, :confidence,
                        :grounded, :created_by, 1
                    )
                """), {
                    "id": "msg_" + uuid.uuid4().hex, "session_id": session_id,
                    "tenant_id": context.tenant_id, "project_id": context.project_id,
                    "seq": next_seq + offset, "role": role, "content": content,
                    "retrieval_run_id": retrieval_id,
                    "citations": json.dumps(citations, ensure_ascii=False),
                    "confidence": confidence, "grounded": bool(grounded),
                    "created_by": context.user_id,
                })
        return await self.list_messages(context, session_id, after_seq=next_seq - 1)

    async def list_messages(self, context: RequestContext, session_id: str, after_seq: int = 0,
                            *, limit: int = 100, latest: bool = False) -> list[dict]:
        await self.get_session(context, session_id)
        async with engine.connect() as conn:
            ordering = "DESC" if latest else "ASC"
            rows = (await conn.execute(text(f"""
                SELECT id, session_id, seq, role, content, retrieval_run_id,
                       citations, confidence, grounded, created_at
                FROM chat_messages_v2
                WHERE session_id=:session_id AND tenant_id=:tenant_id AND seq>:after_seq
                ORDER BY seq {ordering} LIMIT :limit
            """), {
                "session_id": session_id, "tenant_id": context.tenant_id,
                "after_seq": max(0, after_seq),
                "limit": min(max(limit, 1), 100),
            })).mappings().all()
        result = []
        for row in reversed(rows) if latest else rows:
            item = dict(row)
            item["citations"] = json.loads(item["citations"] or "[]")
            item["grounded"] = bool(item["grounded"])
            result.append(item)
        return result
