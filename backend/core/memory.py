"""记忆管理
Saver 持久化（修复：MemorySaver 进程内存态，重启后 interrupt 检查点丢失，
导致 HitL 的 Command(resume=) 找不到中断点）

后端选择（按 DATABASE_URL 自动）：
- PostgreSQL → AsyncPostgresSaver（企业化：单一共享实例 + 连接池，支持多实例高可用；
  业务库与检查点同库，随 PG 一起备份/扩容）
- SQLite（默认/demo）→ 每 Agent 独立 AsyncSqliteSaver + WAL

注意：saver 需要运行中的事件循环初始化，因此：
  ① init_memory_savers() 必须在事件循环内调用（main lifespan 负责，失败 fail-fast）；
  ② graph 编译在 lifespan 之后进行（orchestrator 懒加载时 saver 已就绪）；
  ③ get_memory_saver() 未初始化时直接报错（不提供静默坏兜底）。
PG 切换说明：原 SQLite checkpoints.db 里的历史检查点不会迁移（HitL 中断需重新发起）。
"""
from pathlib import Path

from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, SystemMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from backend.core.llm_factory import get_llm
from backend.core.logger import get_logger

logger = get_logger(__name__)

# SQLite 检查点是运行时状态，统一收敛到 data/runtime，避免污染项目根目录。
_CHECKPOINTS_DB = str(
    Path(__file__).resolve().parent.parent.parent
    / "data" / "runtime" / "db" / "checkpoints.db"
)

_SAVER_AGENTS = ("qa", "bid_review", "procurement", "negotiation", "drawing2bim", "default")

# SQLite 模式：每 Agent 独立 saver（分散文件锁竞争）；PG 模式：单一共享 saver + 连接池
_memory_savers: dict[str, AsyncSqliteSaver] = {}
_pg_pool = None            # psycopg AsyncConnectionPool
_pg_saver = None           # AsyncPostgresSaver（共享）


def _pg_conninfo(url: str | None = None) -> str:
    """DATABASE_URL（sqlalchemy 形式）→ psycopg conninfo：postgresql+asyncpg:// → postgresql://"""
    from backend.config import get_settings
    url = url or get_settings().database_url
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def init_memory_savers(*, setup: bool = True) -> None:
    """在事件循环内初始化 saver（main lifespan 调用，失败即终止启动）"""
    global _pg_pool, _pg_saver
    from backend.db.dialect import is_postgres
    if is_postgres():
        try:
            from backend.config import get_settings
            from psycopg import AsyncConnection
            from psycopg_pool import AsyncConnectionPool
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
            settings = get_settings()
            if setup:
                # Schema changes use the existing migration identity. Runtime
                # pools keep the restricted application role used by Compose.
                async with await AsyncConnection.connect(
                    _pg_conninfo(settings.migration_database_url),
                    autocommit=True, prepare_threshold=0, connect_timeout=10,
                ) as migration_connection:
                    await AsyncPostgresSaver(migration_connection).setup()
            _pg_pool = AsyncConnectionPool(
                conninfo=_pg_conninfo(),
                kwargs={"autocommit": True, "prepare_threshold": 0, "connect_timeout": 10},
                min_size=1, max_size=8, open=False,
            )
            await _pg_pool.open()
            await _pg_pool.wait(timeout=10)
            _pg_saver = AsyncPostgresSaver(_pg_pool)
            logger.info("memory.savers_initialized", count=1, backend="postgres", shared=True)
            return
        except Exception as ex:
            if _pg_pool is not None:
                await _pg_pool.close()
            _pg_pool, _pg_saver = None, None
            raise RuntimeError("PostgreSQL checkpoint initialization failed; refusing local memory fallback") from ex
    import aiosqlite
    Path(_CHECKPOINTS_DB).parent.mkdir(parents=True, exist_ok=True)
    for agent in _SAVER_AGENTS:
        if agent not in _memory_savers:
            conn = await aiosqlite.connect(_CHECKPOINTS_DB)
            await conn.execute("PRAGMA journal_mode=WAL")
            saver = AsyncSqliteSaver(conn)
            await saver.setup()
            _memory_savers[agent] = saver
    logger.info("memory.savers_initialized", count=len(_memory_savers), backend="sqlite")


def get_memory_saver(agent_type: str = "default"):
    """获取持久化 saver（graph 编译期调用；必须先 init_memory_savers 完成初始化）"""
    if _pg_saver is not None:
        return _pg_saver                      # PG：所有 Agent 共享
    saver = _memory_savers.get(agent_type)
    if saver is None:
        raise RuntimeError(
            f"memory saver '{agent_type}' 未初始化：请在应用 lifespan 中调用 init_memory_savers()"
            f"（fail-fast；若在脚本中直接 build 图，请先 asyncio.run(init_memory_savers())）")
    return saver


async def close_memory_savers() -> None:
    """关闭全部 saver 连接（应用 shutdown 调用）"""
    global _pg_pool, _pg_saver
    for saver in _memory_savers.values():
        try:
            await saver.conn.close()
        except Exception:
            pass
    _memory_savers.clear()
    if _pg_saver is not None or _pg_pool is not None:
        try:
            if _pg_pool is not None:
                await _pg_pool.close()
        except Exception:
            pass
        _pg_saver, _pg_pool = None, None
    logger.info("memory.savers_closed")


def build_thread_id(user_id: str, session_id: str) -> str:
    return f"user_{user_id}_session_{session_id}"


def build_config(user_id: str, session_id: str, *, tenant_id: str | None = None,
                 project_id: str | None = None, agent: str | None = None) -> dict:
    if tenant_id is not None and agent is not None:
        import hashlib
        import json
        key = hashlib.sha256(json.dumps([tenant_id, project_id, user_id, agent, session_id]).encode()).hexdigest()
        return {"configurable": {"thread_id": "scoped_" + key}}
    # Kept for retired migration scripts; public entrypoints use scoped keys.
    return {"configurable": {"thread_id": build_thread_id(user_id, session_id)}}

# ═══════════════ 记忆控制策略═══════════════


def trim_messages_to_window(messages: list[BaseMessage], window_size: int = 10) -> list[BaseMessage]:
    """滑动窗口裁剪：SystemMessage 置顶，保留最近 window_size 轮对话（1轮=2条）"""
    system_messages = [m for m in messages if isinstance(m, SystemMessage)]
    dialogue = [m for m in messages if not isinstance(m, SystemMessage)]
    max_dialogue = window_size * 2
    if len(dialogue) > max_dialogue:
        dialogue = dialogue[-max_dialogue:]
    return system_messages + dialogue


def should_trigger_summary(messages: list[BaseMessage], threshold: int = 10) -> bool:
    """判断对话轮数是否超过阈值（每 10 轮触发一次压缩）"""
    dialogue_count = sum(1 for m in messages if isinstance(m, (HumanMessage, AIMessage)))
    rounds = dialogue_count // 2
    return rounds >= threshold and rounds % threshold == 0


async def compress_to_summary(messages: list[BaseMessage], existing_summary: str | None = None) -> str:
    """将历史对话压缩为结构化摘要（增量压缩，保留跨窗口关键信息）"""
    SUMMARY_PROMPT = """请将以下建筑行业咨询对话压缩为结构化摘要。

【压缩规则】
必须保留：用户明确询问的材料/规范/项目信息、反复出现的关注点、最终结论或报价
可以丢弃：已在上次摘要记录的内容、寒暄、已解决且不相关的内容

【上一次摘要】
{previous_summary}

【本次新增对话】
{conversation}

请直接输出摘要文本，不要加任何前缀。"""
    conversation = "\n".join(
        f"{'用户' if isinstance(m, HumanMessage) else '助手'}：{m.content}"
        for m in messages
        if isinstance(m, (HumanMessage, AIMessage)) and isinstance(m.content, str)
    )
    prompt = SUMMARY_PROMPT.format(previous_summary=existing_summary or "（无）", conversation=conversation)
    try:
        llm = get_llm("summarize")
        resp = await llm.ainvoke([HumanMessage(content=prompt)])
        summary = resp.content if isinstance(resp.content, str) else str(resp.content)
        return summary.strip()
    except Exception as e:
        logger.warning("memory.summary_failed", error=str(e))
        return existing_summary or ""
