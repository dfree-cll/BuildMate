"""BuildMate Demo 应用入口（对标 EduAgent 8.6 main 集成）
启动：uvicorn backend.main:app --port 8000
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))  # 项目根

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from backend.config import get_settings
from backend.core.logger import configure_logging, get_logger
from backend.api.router import api_router
from backend.core.llm_factory import LLMFactory

settings = get_settings()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    logger.info("app.startup", app=settings.app_name, env=settings.app_env, mock_mode=settings.mock_mode)

    # 安全自检：弱 JWT 密钥等告警（不阻断启动，便于 demo 离线跑；生产需按提示加固）
    for w in settings.validate_security():
        logger.warning("app.security_warning", detail=w)

    # 记忆持久化：预初始化 SQLite saver（AsyncSqliteSaver 需在事件循环内 setup；
    # 必须先于任何 graph 编译调用，保证 HitL interrupt 检查点跨重启可恢复）
    # M2：初始化失败直接终止启动（fail-fast）——旧的"只告警继续跑"会让 HitL 整体静默失效
    from backend.core.memory import init_memory_savers
    await init_memory_savers()

    # ① 数据库迁移：Alembic 版本化升级到 head（企业化：替代旧"启动幂等补丁"。
    # 基线迁移对存量库自动收敛，对新库直接建表；失败即终止启动——schema 不对就不该跑。
    # 注意：alembic env.py 内部用 asyncio.run 起独立事件循环，因此这里必须放线程池执行，
    # 不能在当前事件循环里直接调（否则 RuntimeError: cannot be called from a running loop））
    def _run_alembic():
        from alembic import command as alembic_command
        from alembic.config import Config as AlembicConfig
        _root = Path(__file__).parent.parent
        _cfg = AlembicConfig(str(_root / "alembic.ini"))
        _cfg.set_main_option("script_location", str(_root / "migrations"))
        _cfg.set_main_option("sqlalchemy.url", settings.database_url)
        alembic_command.upgrade(_cfg, "head")
    try:
        import asyncio as _aio
        await _aio.to_thread(_run_alembic)
        logger.info("app.migrations_applied", backend="alembic")
    except Exception as e:
        logger.error("app.migrations_failed", error=str(e))
        raise

    # ② 本地模型预热（reranker + 分类器 + BGE-M3，并行加载，避免首请求慢）
    import asyncio as _aio
    try:
        from backend.core.reranker import BGEReranker
        from backend.core.query_classifier import QueryClassifier
        from backend.core.knowledge_base import TextVectorizer
        loop = _aio.get_running_loop()
        await _aio.gather(
            loop.run_in_executor(None, BGEReranker.get_instance),
            loop.run_in_executor(None, QueryClassifier.get_instance),
            loop.run_in_executor(None, TextVectorizer._get_local_bge),
        )
        logger.info("app.local_models_warmed_up")
    except Exception as e:
        logger.warning("app.local_models_warmup_failed", error=str(e)[:200])

    # ③ 可观测性表预建（M10：避免每次 LLM 调用都 CREATE TABLE）
    try:
        from backend.core.observability import ensure_llm_calls_table
        await ensure_llm_calls_table()
    except Exception as e:
        logger.warning("app.obs_table_init_failed", error=str(e)[:200])

    yield
    # M12：shutdown 释放资源（原实现只清 LLM 缓存，连接全部悬空）
    LLMFactory.clear_cache()
    try:
        from backend.core.memory import close_memory_savers
        await close_memory_savers()
    except Exception as e:
        logger.warning("app.memory_savers_close_failed", error=str(e)[:200])
    try:
        from backend.db.session import engine
        await engine.dispose()
    except Exception as e:
        logger.warning("app.engine_dispose_failed", error=str(e)[:200])
    logger.info("app.shutdown")


app = FastAPI(title=settings.app_name, description="建筑行业智能助手 Demo（对标 EduAgent V7.7 架构）",
               lifespan=lifespan)

# CORS：* + credentials=True 会被浏览器拒绝（且扩大 CSRF 面）。
# 默认显式本机源；生产通过 CORS_ORIGINS 环境变量配置（逗号分隔，Low 项：原硬编码）
_CORS_ORIGINS = [o.strip() for o in settings.cors_origins.split(",") if o.strip()] or [
    "http://localhost:3000", "http://127.0.0.1:3000",
    "http://localhost:8000", "http://127.0.0.1:8000",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix="/api/v1")

# ── MCP 工具层（对标 EduAgent 8.6）──────────────────────
# MCP Server 作为独立进程运行：
#   D:/develop/anaconda3/envs/EduAgent/python.exe backend/mcp/knowledge_base_server.py  # :8001
#   D:/develop/anaconda3/envs/EduAgent/python.exe backend/mcp/web_search_server.py       # :8002
# 通过 MCP Client 调用（backend/mcp/client.py）。

@app.get("/health")
async def health():
    return {"status": "ok", "app": settings.app_name, "mock_mode": settings.mock_mode}


# 前端静态页面（SSE 客户端）
static_dir = Path(__file__).parent.parent / "frontend" / "static"
if static_dir.exists():
    app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")