"""BuildMate Demo 应用入口
启动：uvicorn backend.main:app --port 8000
"""
import asyncio
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))  # 项目根

# Windows 下 psycopg 需要 Selector 事件循环（默认 Proactor 不兼容）
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.config import get_settings
from backend.core.logger import configure_logging, get_logger
from backend.api.router import api_router
from backend.api.v2.router import api_v2_router
from backend.core.llm_factory import LLMFactory
from backend.domain.errors import (
    DependencyFailure,
    DomainError,
    InvalidTransition,
    PolicyFailure,
    ResourceNotFound,
    ValidationFailure,
)
from backend.core.metrics import API_LATENCY, API_REQUESTS

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
        _cfg.set_main_option(
            "sqlalchemy.url", settings.migration_database_url or settings.database_url
        )
        alembic_command.upgrade(_cfg, "head")
    try:
        import asyncio as _aio
        await _aio.to_thread(_run_alembic)
        logger.info("app.migrations_applied", backend="alembic")
    except Exception as e:
        logger.error("app.migrations_failed", error=str(e))
        raise

    # ② 本地模型预热——已停用：预热在启动时加载三个模型（即使串行）在 Docker/WSL
    # 占用内存的环境下仍会进程级崩溃（exit 1，无 traceback）。
    # 检索与重排模型按实际请求懒加载（约 15s 首请求延迟），启动稳定、功能不受影响。
    # 如需恢复预热：确保可用内存 >8G 后取消注释下方代码。

    # ③ 可观测性表预建（M10：避免每次 LLM 调用都 CREATE TABLE）
    try:
        from backend.core.observability import ensure_llm_calls_table
        await ensure_llm_calls_table()
    except Exception as e:
        logger.warning("app.obs_table_init_failed", error=str(e)[:200])

    local_runner = None
    local_runner_task = None
    if settings.task_queue_backend == "local":
        from backend.workers.local_runner import LocalDatabaseRunner
        from backend.workers.workflows import get_default_runtime

        local_runner = LocalDatabaseRunner(get_default_runtime())
        local_runner_task = asyncio.create_task(local_runner.run(), name="buildmate-local-task-runner")
        logger.info("app.task_runner_started", backend="local-database")

    yield
    # M12：shutdown 释放资源（原实现只清 LLM 缓存，连接全部悬空）
    LLMFactory.clear_cache()
    if local_runner is not None and local_runner_task is not None:
        local_runner.stop()
        try:
            await asyncio.wait_for(local_runner_task, timeout=5)
        except asyncio.TimeoutError:
            local_runner_task.cancel()
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


app = FastAPI(title=settings.app_name, description="建筑行业智能助手 Demo",
               lifespan=lifespan)

from backend.core.telemetry import configure_telemetry
configure_telemetry(app, settings.otel_exporter_otlp_endpoint, settings.otel_service_name)


@app.middleware("http")
async def observe_http(request: Request, call_next):
    started = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        route = request.scope.get("route")
        route_path = getattr(route, "path", "unmatched")
        API_REQUESTS.labels(request.method, route_path, str(status_code)).inc()
        API_LATENCY.labels(request.method, route_path).observe(time.perf_counter() - started)

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
app.include_router(api_v2_router, prefix="/api/v2")


@app.exception_handler(DomainError)
async def domain_error_handler(_: Request, exc: DomainError):
    """Expose expected v2 failures without leaking implementation details."""
    if isinstance(exc, ResourceNotFound):
        status_code = 404
    elif isinstance(exc, InvalidTransition):
        status_code = 409
    elif isinstance(exc, PolicyFailure):
        status_code = 403
    elif isinstance(exc, DependencyFailure):
        status_code = 503
    elif isinstance(exc, ValidationFailure):
        status_code = 422
    else:
        status_code = 400
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": exc.code, "message": str(exc)}},
    )

# ── MCP 工具层──────────────────────
# MCP Server 作为独立进程运行：
#   python backend/mcp/web_search_server.py       # :8002
# 通过 MCP Client 调用（backend/mcp/client.py）。

@app.get("/health")
async def health():
    # Keep the active process revision visible to the local UI/operator.  A
    # long-lived Windows process can otherwise retain an older Pydantic model
    # after source files change, producing misleading ``extra_forbidden``
    # errors for newly added WallModel fields such as ``beams``.
    from backend.engines.wall_pipeline.contracts import WallModel

    return {
        "status": "ok",
        "app": settings.app_name,
        "mock_mode": settings.mock_mode,
        "api_versions": ["v1", "v2"],
        "runtime_contracts": {
            "wall_model_schema": WallModel.model_fields["schema_version"].default,
            "wall_model_supports_beams": "beams" in WallModel.model_fields,
        },
    }


@app.get("/metrics", include_in_schema=False)
async def metrics():
    from fastapi.responses import Response
    try:
        from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
    except ImportError:
        return Response(
            "# prometheus-client is not installed; metrics are unavailable\n",
            media_type="text/plain; version=0.0.4",
        )
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# 前端静态页面（SSE 客户端）
static_dir = Path(__file__).parent.parent / "frontend" / "static"
if static_dir.exists():
    app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")
