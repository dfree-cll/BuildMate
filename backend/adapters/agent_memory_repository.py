"""Private, scoped agent history. Checkpoints resume execution; this stores user-visible memory."""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import and_, select, update, func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from backend.db.schema import agent_memory_sessions as sessions, agent_memory_turns as turns
from backend.db.session import engine
from backend.db.rls import activate_rls_context
from backend.domain.contracts import RequestContext


def memory_key(context: RequestContext, agent: str, session_id: str) -> str:
    parts = [context.tenant_id, context.project_id, context.user_id, agent, session_id]
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()


def _scope(table, context):
    # A missing project means personal context, NEVER all projects.
    return and_(table.c.tenant_id == context.tenant_id,
                table.c.project_id == context.project_id,
                table.c.created_by == context.user_id)


def _insert(conn, table):
    return (pg_insert if conn.dialect.name == "postgresql" else sqlite_insert)(table)


class AgentMemoryRepository:
    async def sync_bim(self, context: RequestContext) -> None:
        """Materialize recent successful task receipts, never rerun Revit to rebuild memory."""
        from backend.db.schema import workflow_runs as runs
        activate_rls_context(context)
        async with engine.connect() as conn:
            rows = (await conn.execute(select(runs.c.id, runs.c.options, runs.c.result).where(
                runs.c.tenant_id == context.tenant_id, runs.c.project_id == context.project_id,
                runs.c.actor_id == context.user_id, runs.c.workflow == "wall_pipeline",
                runs.c.status == "succeeded",
            ).order_by(runs.c.created_at.desc()).limit(100))).mappings().all()
        prepared = []
        for row in reversed(rows):
            options, result = json.loads(row["options"]), json.loads(row["result"] or "{}")
            session_id = options.get("memory_session_id") or "project-model"
            key = memory_key(context, "drawing2bim", session_id)
            entry_id = hashlib.sha256(f"{key}:{row['id']}".encode()).hexdigest()
            prepared.append((entry_id, row, options, result, session_id))
        if not prepared:
            return
        async with engine.connect() as conn:
            existing = set((await conn.execute(select(turns.c.id).where(
                _scope(turns, context), turns.c.id.in_([item[0] for item in prepared]),
            ))).scalars())
        for entry_id, row, options, result, session_id in prepared:
            if entry_id in existing:
                continue
            await self.append(context, "drawing2bim", session_id,
                              row["id"], "BIM 已完成任务 " + row["id"], result.get("answer", ""),
                              {"task_id": row["id"], "artifact_ids": result.get("artifact_ids", []),
                               "continued_from": options.get("continue_from_task_id"),
                               "level": options.get("level", {}), "revit": options.get("revit", {})})

    async def ensure(self, context: RequestContext, agent: str, session_id: str) -> str:
        if not session_id or len(session_id) > 128:
            raise ValueError("memory session_id must contain 1–128 characters")
        activate_rls_context(context)
        key = memory_key(context, agent, session_id)
        async with engine.begin() as conn:
            await conn.execute(_insert(conn, sessions).values(
                id=key, tenant_id=context.tenant_id, project_id=context.project_id,
                created_by=context.user_id, agent=agent, session_id=session_id,
            ).on_conflict_do_nothing(index_elements=[sessions.c.id]))
        return key

    async def read(self, context: RequestContext, agent: str, session_id: str, limit: int = 20) -> dict:
        activate_rls_context(context)
        key = memory_key(context, agent, session_id)
        async with engine.connect() as conn:
            row = (await conn.execute(select(sessions).where(
                _scope(sessions, context), sessions.c.id == key))).mappings().first()
            if not row:
                return {"session_id": session_id, "summary": "", "preferences": {}, "turns": []}
            history = (await conn.execute(select(turns).where(
                _scope(turns, context), turns.c.memory_id == key,
            ).order_by(turns.c.seq.desc()).limit(min(max(limit, 1), 100)))).mappings().all()
        result = dict(row)
        result["preferences"] = json.loads(result["preferences"])
        result["turns"] = [{**dict(t), "result": json.loads(t["result"])} for t in reversed(history)]
        return result

    async def list_sessions(self, context: RequestContext, agent: str) -> list[dict]:
        activate_rls_context(context)
        async with engine.connect() as conn:
            rows = (await conn.execute(select(
                sessions.c.session_id, sessions.c.title, sessions.c.updated_at,
            ).where(_scope(sessions, context), sessions.c.agent == agent)
              .order_by(sessions.c.updated_at.desc(), sessions.c.id).limit(50))).mappings().all()
        return [dict(row) for row in rows]

    async def append(self, context: RequestContext, agent: str, session_id: str,
                     turn_id: str, user_text: str, answer: str, result: dict | None = None) -> None:
        key = await self.ensure(context, agent, session_id)
        entry_id = hashlib.sha256(f"{key}:{turn_id}".encode()).hexdigest()
        result_json = json.dumps(result or {}, ensure_ascii=False, default=str)
        if len(result_json) > 16000:
            result_json = json.dumps({"truncated": True, "reference": turn_id})
        async with engine.begin() as conn:
            # Serialize writers in both PG and SQLite BEFORE selecting max(seq).
            await conn.execute(update(sessions).where(_scope(sessions, context), sessions.c.id == key)
                               .values(version=sessions.c.version + 1))
            if (await conn.execute(select(turns.c.id).where(
                _scope(turns, context), turns.c.id == entry_id))).first():
                return  # task replay must not add another memory turn
            seq = (await conn.execute(select(func.coalesce(func.max(turns.c.seq), 0) + 1)
                                      .where(_scope(turns, context), turns.c.memory_id == key))).scalar_one()
            await conn.execute(turns.insert().values(
                id=entry_id, memory_id=key, tenant_id=context.tenant_id, project_id=context.project_id,
                created_by=context.user_id, seq=seq, user_text=user_text[:4000],
                answer=answer[:12000], result=result_json,
            ))
            # Deterministic rolling digest, not an LLM-invented fact. No extra model latency.
            older = (await conn.execute(select(turns.c.user_text, turns.c.answer).where(
                _scope(turns, context), turns.c.memory_id == key, turns.c.seq <= seq - 10,
            ).order_by(turns.c.seq.desc()).limit(20))).all()
            summary = "\n".join(f"用户：{q[:100]}；历史回复（未复核）：{a[:180]}" for q, a in reversed(older))
            values = {"summary": summary[:6000], "updated_at": func.current_timestamp()}
            if seq == 1:
                values["title"] = user_text[:80] or "新会话"
            await conn.execute(update(sessions).where(_scope(sessions, context), sessions.c.id == key).values(**values))

    async def preferences(self, context: RequestContext, agent: str, session_id: str, value: dict) -> None:
        key = await self.ensure(context, agent, session_id)
        async with engine.begin() as conn:
            await conn.execute(update(sessions).where(_scope(sessions, context), sessions.c.id == key).values(
                preferences=json.dumps(value, ensure_ascii=False), updated_at=func.current_timestamp(),
                version=sessions.c.version + 1,
            ))
