"""BuildMate Demo 配置中心
无 LLM Key 时进入 Mock 模式，全链路可离线运行。
"""
from functools import lru_cache
from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── 数据库（默认 SQLite，零依赖；生产切 PostgreSQL 改 DATABASE_URL）──
    database_url: str = "sqlite+aiosqlite:///./data/runtime/db/buildmate.db"
    migration_database_url: str = ""  # 生产可用表 owner 迁移，业务连接使用受 RLS 约束的 app role

    # ── Milvus 向量库（可选：配置 host/port 且可用时启用，否则回退本地）──
    milvus_host: str = ""
    milvus_port: int = 19531
    # 向量后端选择（可插拔）：auto=配置了 Milvus 即用+local 兜底；local/milvus/已注册后端名
    vector_backend: str = "auto"

    # ── 大模型（OpenAI 兼容；留空 = Mock 模式）──
    llm_api_key: str = ""
    llm_base_url: str = "https://api.siliconflow.cn/v1"
    llm_model: str = "deepseek-ai/DeepSeek-V3"
    # 可插拔 Provider：空/auto = 自动（无 key→mock，有 key→openai）；可填注册的 provider 名
    llm_provider: str = ""

    # ── 嵌入（OpenAI 兼容；留空 = 本地字符哈希向量，离线可跑）──
    embedding_api_key: str = ""
    embedding_base_url: str = "https://api.siliconflow.cn/v1"
    embedding_model: str = "BAAI/bge-m3"
    # 大型本地 BGE/CrossEncoder 权重是可选优化，不应阻塞本地对话启动。
    # 需要启用时显式设置 RAG_LOCAL_MODELS_ENABLED=true，并可单独打开重排。
    rag_local_models_enabled: bool = False
    rag_reranker_enabled: bool = False

    # ── 本地模型根目录（默认项目内 models/；可通过 MODELS_ROOT 环境变量覆盖；容器内设 /models）──
    models_root: str = "models"

    # ── JWT（安全：生产必须设置强随机密钥，见 .env.example 说明）──
    # 弱默认值仅用于本地 demo 离线跑通；生产环境缺失/弱值时启动告警（见 validate_security）
    jwt_secret: str = "buildmate-demo-secret-change-me"
    jwt_algorithm: str = "HS256"
    # H4 完整版：访问令牌短效 + 刷新令牌长效
    access_token_minutes: int = 30
    refresh_token_days: int = 7
    bcrypt_rounds: int = 12               # 测试可经 BCRYPT_ROUNDS=4 加速

    # ── 安全校验（启动时调用）：检测弱 JWT 密钥 ──
    def validate_security(self) -> list[str]:
        warnings: list[str] = []
        weak = {"buildmate-demo-secret-change-me", "buildmate-demo-secret-2026",
                "buildmate-prod-secret-2026", "buildmate-prod-secret", "CHANGE_ME"}
        if self.jwt_secret in weak or len(self.jwt_secret) < 24:
            warnings.append("JWT_SECRET 过弱或为默认值，请设置强随机密钥（生产环境强制）")
        return warnings

    # ── Redis（可选：多实例共享限流/吊销状态；空 = 进程内存回退）──
    redis_url: str = ""

    # ── v2 任务与制品基础设施（本地零依赖，生产显式启用）──
    task_queue_backend: str = "local"   # local / rabbitmq
    rabbitmq_url: str = ""
    rabbitmq_exchange: str = "buildmate.tasks"
    task_runner_poll_seconds: float = 0.5
    # Large engineering PDFs can spend several minutes in bounded OCR before
    # deterministic geometry starts.  Keep this separate from ordinary Agent
    # timeouts so one global setting does not weaken every workflow step.
    wall_pipeline_prepare_timeout_seconds: float = Field(
        default=1200.0, ge=300.0, le=3600.0
    )
    # Revit delivery includes the Bridge transaction, read-back, artifact
    # persistence and an independent source-vs-Revit overlay audit.  It must
    # not share the short timeout used by ordinary Agent steps; otherwise a
    # large but successful delivery can be marked failed during finalization.
    wall_pipeline_revit_write_timeout_seconds: float = Field(
        default=900.0, ge=180.0, le=3600.0
    )
    artifact_storage_backend: str = "local"  # local / s3（s3 adapter 由生产部署注入）
    artifact_storage_root: str = "data/uploads_v2"
    s3_endpoint_url: str = ""
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_bucket: str = "buildmate-artifacts"
    s3_region: str = "us-east-1"
    s3_cache_root: str = "data/runtime/s3-cache"

    # Optional ODA File Converter executable used by DWG source adapters.
    # Keep empty for DXF/PDF-only and local fixture runs.
    oda_file_converter: str = ""

    # ── 独立 Windows Revit Bridge ──
    revit_bridge_url: str = "http://127.0.0.1:8005"
    # Revit Routes executes the generated model transaction and then renders
    # an independent plan view.  On a medium/large sheet this routinely takes
    # longer than 90s; a short client timeout leaves Revit writing in the
    # background while the Bridge starts rollback, producing HTTP 500 and a
    # locked working-copy file.  Keep this aligned with the write workflow
    # timeout and make it configurable for slower/faster hosts.
    revit_bridge_timeout_seconds: float = Field(
        default=900.0, ge=30.0, le=3600.0
    )
    # Bridge 写操作必须同时开启执行开关并提供审批密钥；默认只能 validate/dry-run。
    revit_bridge_execution_enabled: bool = False
    # Legacy arbitrary live Revit scripts are disabled by default.  The typed
    # wall-model delivery endpoint has its own contract and is unaffected.
    revit_bridge_legacy_script_execution_enabled: bool = False
    revit_bridge_approval_secret: str = ""
    revit_bridge_work_root: str = "data/runtime/revit/bridge"
    # Optional allow-list for source/target RVT files.  Keep empty for local
    # fixture mode; production deployments should point this at a dedicated
    # shared project-model directory so a task cannot make the Bridge open an
    # arbitrary file readable by the service account.
    revit_target_root: str = ""

    # ── MCP 工具网关 ──
    mcp_default_timeout: float = 30.0     # 网关默认超时（秒），单工具可在注册表覆盖
    mcp_auth_token: str = ""              # 非空时网关请求携带 Authorization: Bearer <token>
    # 工具访问 ACL（JSON：{"角色": ["工具名", ...]}，"*"=全放行；空 = 用网关内置默认表）
    mcp_tool_acl: str = ""
    # MCP 服务地址统一由配置提供，避免 Agent 节点散落本机端口常量。
    mcp_web_search_url: str = "http://127.0.0.1:8002"
    mcp_ifc_parser_url: str = "http://127.0.0.1:8003"
    mcp_drawing_perception_url: str = "http://127.0.0.1:8004"

    # ── 编排器韧性 ──
    orchestrator_task_timeout: float = 120.0   # 单 Agent 任务级超时（秒）；Pipeline 按步骤数累计
    # 对话入口的路由和流式执行不能无限等待外部模型；较短的路由超时
    # 会回退到确定性知识问答，较长的流式超时给 RAG/LLM 留出完整回答时间。
    chat_route_timeout_seconds: float = Field(default=8.0, ge=1.0, le=60.0)
    chat_stream_timeout_seconds: float = Field(default=90.0, ge=10.0, le=600.0)
    llm_request_timeout_seconds: float = Field(default=30.0, ge=2.0, le=300.0)
    breaker_failure_threshold: int = 5         # 连续最终失败 N 次触发熔断
    breaker_cooldown_seconds: float = 30.0     # 熔断冷却期（秒），到期半开探测

    # ── 上传限额 ──
    # IFC 模型放宽（Revit 导出常 100MB+；解析已后台化，不占请求时长）
    bim_max_upload_mb: int = 200

    # ── 应用 ──
    app_name: str = "BuildMate"
    app_env: str = "local"
    log_level: str = "INFO"
    otel_exporter_otlp_endpoint: str = ""
    otel_service_name: str = "buildmate-backend"
    default_tenant_id: str = "tenant_default"
    # CORS 白名单（逗号分隔；空 = 用 main.py 的本机默认源，生产经环境变量覆盖）
    cors_origins: str = ""

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False
        extra = "ignore"

    @property
    def mock_mode(self) -> bool:
        return not bool(self.llm_api_key)


@lru_cache()
def get_settings() -> Settings:
    return Settings()
