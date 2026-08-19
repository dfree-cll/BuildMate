"""可观测性：LLM 调用追踪（对标 EduAgent Langfuse 的轻量替代）
记录每次 LLM 调用：时间/agent/模型/耗时/输入输出字符/估算成本
存储：SQLite 表 llm_calls，无需外部服务
"""
import json
import time
import uuid

from sqlalchemy import text

from backend.db.session import engine
from backend.core.logger import get_logger

logger = get_logger(__name__)

# 估算价格（每百万 token 美元；DeepSeek 参考价）
PRICE_PER_M_IN = 0.5     # 输入
PRICE_PER_M_OUT = 2.0    # 输出


_table_ready = False


async def ensure_llm_calls_table() -> None:
    """建表（幂等；main lifespan 启动时调用一次）。
    DDL 取自 backend/db/schema.py（唯一事实源），不再维护第二份建表语句"""
    global _table_ready
    if _table_ready:
        return
    from backend.db.schema import llm_calls
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: llm_calls.create(c, checkfirst=True))
    _table_ready = True


async def record_llm_call(agent_type: str, model: str, duration_ms: float,
                          input_chars: int, output_chars: int, ok: bool = True,
                          error: str = "") -> None:
    """记录一次 LLM 调用"""
    try:
        await ensure_llm_calls_table()
        # 粗估成本（按字符折算 token，中文约 1.5 字符/token）
        in_tokens = input_chars / 1.5
        out_tokens = output_chars / 1.5
        cost = (in_tokens * PRICE_PER_M_IN + out_tokens * PRICE_PER_M_OUT) / 1e6
        async with engine.begin() as conn:
            await conn.execute(text("""
                INSERT INTO llm_calls (id, agent_type, model, start_ts, duration_ms,
                    input_chars, output_chars, est_cost_usd, ok, error)
                VALUES (:id, :at, :model, :ts, :dur, :in_c, :out_c, :cost, :ok, :err)
            """), {
                "id": str(uuid.uuid4()), "at": agent_type, "model": model,
                "ts": time.time(), "dur": duration_ms,
                "in_c": input_chars, "out_c": output_chars, "cost": round(cost, 6),
                "ok": ok, "err": error[:200],
            })
    except Exception as e:
        logger.debug("obs.record_failed", error=str(e))


async def get_call_stats(hours: int = 24) -> dict:
    """查询最近 N 小时调用统计"""
    try:
        await ensure_llm_calls_table()
        async with engine.connect() as conn:
            row = (await conn.execute(text("""
                SELECT COUNT(*), COALESCE(SUM(duration_ms), 0),
                       COALESCE(SUM(est_cost_usd), 0)
                FROM llm_calls WHERE start_ts > :ts
            """), {"ts": time.time() - hours * 3600})).fetchone()
            return {"calls": row[0], "total_ms": row[1], "est_cost_usd": round(row[2], 4)}
    except Exception:
        return {"calls": 0, "total_ms": 0, "est_cost_usd": 0}
