"""BuildMate Demo 配置中心
无 LLM Key 时进入 Mock 模式，全链路可离线运行。
"""
from functools import lru_cache
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── 数据库（默认 SQLite，零依赖；生产切 PostgreSQL 改 DATABASE_URL）──
    database_url: str = "sqlite+aiosqlite:///./buildmate.db"

    # ── Milvus 向量库（可选：配置 host/port 且可用时启用，否则回退本地）──
    milvus_host: str = ""
    milvus_port: int = 19531
    # 向量后端选择（可插拔）：auto=配置了 Milvus 即用+local 兜底；local/milvus/已注册后端名
    vector_backend: str = "auto"

    # ── 大模型（OpenAI 兼容；留空 = Mock 模式）──
    llm_api_key: str = ""
    llm_base_url: str = "https://api.siliconflow.cn/v1"
    llm_model: str = "deepseek-ai/DeepSeek-V3"
    llm_temperature: float = 0.1
    # 可插拔 Provider：空/auto = 自动（无 key→mock，有 key→openai）；可填注册的 provider 名
    llm_provider: str = ""

    # ── 嵌入（OpenAI 兼容；留空 = 本地字符哈希向量，离线可跑）──
    embedding_api_key: str = ""
    embedding_base_url: str = "https://api.siliconflow.cn/v1"
    embedding_model: str = "BAAI/bge-m3"

    # ── 本地模型根目录（默认项目内 models/；可通过 MODELS_ROOT 环境变量覆盖；容器内设 /models）──
    models_root: str = "models"

    # ── JWT（安全：生产必须设置强随机密钥，见 .env.example 说明）──
    # 弱默认值仅用于本地 demo 离线跑通；生产环境缺失/弱值时启动告警（见 validate_security）
    jwt_secret: str = "buildmate-demo-secret-change-me"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 1440        # 兼容保留（旧配置），新代码用下面两项
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

    # ── 上传限额 ──
    # IFC 模型放宽（Revit 导出常 100MB+；解析已后台化，不占请求时长）
    bim_max_upload_mb: int = 200

    # ── 应用 ──
    app_name: str = "BuildMate"
    app_env: str = "local"
    app_debug: bool = True
    log_level: str = "INFO"
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
