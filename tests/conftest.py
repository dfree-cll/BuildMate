"""pytest 全局测试环境隔离（conftest 在所有测试模块导入前执行）

环境隔离（env 优先级高于 .env，必须在 import backend.* 前设置）：
- DATABASE_URL → 项目根 test_hitl.db（临时 SQLite，不碰 .env 里的 PG/业务库，
  也避免 PG 不可达时 asyncpg 长超时导致用例挂起）
- LLM_API_KEY 置空 → 强制 Mock 模式（离线、确定性、零费用）
- MODELS_ROOT 指向不存在目录 → 跳过本地大模型（BGE/Reranker/分类器）加载
- MILVUS_HOST 置空 → 禁用 Milvus

数据隔离（autouse 夹具）：
- 每用例前幂等建表（复用 scripts/init_db 的 DDL）
- 知识库为空时灌入 data/knowledge* 种子（复用 seed_knowledge 的分块逻辑），
  保证 test_qa_nodes 等依赖检索的用例离线可跑
- 每用例后 engine.dispose()：pytest-asyncio 每用例新事件循环，
  跨 loop 复用连接池会报 "attached to a different loop"
"""
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

# psycopg（PostgresSaver）在 Windows 上要求 Selector 事件循环，
# Proactor（pytest-asyncio 默认）会连接失败无限重试。必须在任何 loop 创建前设置。
if sys.platform == "win32":
    import asyncio
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{(_ROOT / 'test_hitl.db').as_posix()}")
os.environ.setdefault("LLM_API_KEY", "")
os.environ.setdefault("EMBEDDING_API_KEY", "")
os.environ.setdefault("MILVUS_HOST", "")
os.environ.setdefault("MODELS_ROOT", str(_ROOT / "no_such_models_dir"))
os.environ.setdefault("BCRYPT_ROUNDS", "4")   # 测试降低哈希成本（生产默认 12）

sys.path.insert(0, str(_ROOT))

import pytest
from sqlalchemy import text


async def _seed_knowledge_if_empty() -> None:
    """知识库为空时灌入 data/knowledge + data/knowledge_real（幂等，只首个用例承担）"""
    from backend.db.session import engine
    from backend.services.vector_store import add_chunks
    from scripts.seed_knowledge import split_markdown_documents

    async with engine.connect() as conn:
        n = (await conn.execute(text("SELECT COUNT(*) FROM knowledge_chunks"))).scalar()
    if n:
        return
    for kb_dir in (_ROOT / "data" / "knowledge", _ROOT / "data" / "knowledge_real"):
        if not kb_dir.exists():
            continue
        for fp in sorted(kb_dir.glob("*.md")):
            chunks = split_markdown_documents(fp.read_text(encoding="utf-8"), fp.name)
            if chunks:
                await add_chunks(chunks, tenant_id="tenant_default")


@pytest.fixture(autouse=True)
async def _test_db():
    from backend.db.session import engine
    from backend.db.schema import METADATA

    # 每用例重置共享状态存储（限流计数/黑名单/版本缓存，内存回退态；
    # Redis 模式下 clear_all 不清远端，CI 默认走内存回退）
    from backend.core.state_store import state_store
    await state_store.clear_all()

    async with engine.begin() as conn:
        await conn.run_sync(METADATA.create_all)
    try:
        await _seed_knowledge_if_empty()
    except Exception as e:  # 种子失败不阻断非检索类用例
        print(f"[conftest] 知识库种子灌入失败（检索类用例可能失败）: {e}")
    yield
    await engine.dispose()
