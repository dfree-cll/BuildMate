"""数据库 Schema 唯一事实源（SQLAlchemy Core）

企业化改造：此前建表 DDL 散落在 scripts/init_db.py（字符串）、observability.py、
vector_store.py 各自为政——现在统一到这里，供三处消费：
  ① Alembic 迁移（migrations/env.py 的 target_metadata）
  ② init_db.py（METADATA.create_all，demo/离线快速建库）
  ③ 测试夹具（conftest 直接 create_all）

注意：与运行时手写 SQL 的兼容性——列名/类型/约束必须与各 upsert SQL 保持一致
（dialect.py、agents_api.py 等）。修改本文件后用 `alembic revision --autogenerate`
生成增量迁移，不要直接改线上表。
"""
from sqlalchemy import (
    MetaData, Table, Column, Text, String, Integer, Float, Numeric,
    Boolean, DateTime, UniqueConstraint, Index, text,
)

METADATA = MetaData()

users = Table(
    "users", METADATA,
    Column("id", Text, primary_key=True),
    Column("tenant_id", String(64), nullable=False, server_default="tenant_default"),
    Column("username", String(64), nullable=False, unique=True),
    Column("email", String(128), nullable=False),
    Column("password_hash", String(256), nullable=False),
    Column("role", String(16), nullable=False, server_default="user"),
    Column("is_active", Boolean, nullable=False, server_default=text("true")),
    Column("token_version", Integer, nullable=False, server_default="0"),
    Column("created_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
)

qa_sessions = Table(
    "qa_sessions", METADATA,
    Column("id", Text, primary_key=True),
    Column("tenant_id", String(64), nullable=False, server_default="tenant_default"),
    Column("user_id", Text),
    Column("thread_id", String(128), nullable=False, unique=True),
    Column("summary", Text),
    Column("summary_version", Integer, nullable=False, server_default="0"),
    Column("created_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
    Column("updated_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
)

bid_reviews = Table(
    "bid_reviews", METADATA,
    Column("id", Text, primary_key=True),
    Column("tenant_id", String(64), nullable=False, server_default="tenant_default"),
    Column("user_id", Text),
    Column("doc_name", String(256), nullable=False),
    Column("structured_data", Text),
    Column("scores", Text),
    Column("issues", Text),
    Column("summary", Text),
    Column("status", String(16), nullable=False, server_default="pending"),
    Column("error_msg", Text),
    Column("created_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
    Column("updated_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
    # status 取值约束由应用层维护（pending/processing/done/failed）；
    # 不加 DB CHECK 以便跨 SQLite/PG 的 ALTER 兼容（存量库补列时无法补约束）
)

bim_reviews = Table(
    "bim_reviews", METADATA,
    Column("id", Text, primary_key=True),
    Column("tenant_id", String(64), nullable=False, server_default="tenant_default"),
    Column("user_id", Text),
    Column("file_name", String(256), nullable=False),
    Column("structured_data", Text),   # ifc_parser 提取结果 JSON
    Column("issues", Text),            # 规则引擎问题 JSON
    Column("summary", Text),           # 双轨合并审查结论 JSON
    Column("status", String(16), nullable=False, server_default="pending"),
    Column("error_msg", Text),
    Column("created_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
    Column("updated_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
)

purchase_orders = Table(
    "purchase_orders", METADATA,
    Column("id", Text, primary_key=True),
    Column("tenant_id", String(64), nullable=False, server_default="tenant_default"),
    Column("user_id", Text),
    Column("order_no", String(64), nullable=False, unique=True),
    Column("material_name", String(128), nullable=False),
    Column("quantity", Integer, nullable=False),
    Column("unit_price", Numeric(12, 2), nullable=False),
    Column("total_amount", Numeric(14, 2), nullable=False),
    Column("status", String(20), nullable=False, server_default="pending"),
    Column("ai_result", Text),
    Column("approved_by", Text),
    Column("created_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
)

approval_records = Table(
    "approval_records", METADATA,
    Column("id", Text, primary_key=True),
    Column("tenant_id", String(64), nullable=False, server_default="tenant_default"),
    Column("order_id", Text),
    Column("action", String(20), nullable=False),
    Column("comment", Text),
    Column("operator", Text),
    Column("created_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
)

negotiation_sessions = Table(
    "negotiation_sessions", METADATA,
    Column("id", Text, primary_key=True),
    Column("tenant_id", String(64), nullable=False, server_default="tenant_default"),
    Column("user_id", Text),
    Column("thread_id", String(128), nullable=False, unique=True),
    Column("stage", String(32), nullable=False, server_default="quote"),
    Column("context", Text),
    Column("status", String(16), nullable=False, server_default="in_progress"),
    Column("created_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
)

knowledge_pending_queue = Table(
    "knowledge_pending_queue", METADATA,
    Column("id", Text, primary_key=True),
    Column("tenant_id", String(64), nullable=False, server_default="tenant_default"),
    Column("user_id", Text),
    Column("question", Text, nullable=False),
    Column("confidence", Float, nullable=False, server_default="0"),
    Column("status", String(16), nullable=False, server_default="pending"),
    Column("created_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
)

knowledge_chunks = Table(
    "knowledge_chunks", METADATA,
    Column("id", Text, primary_key=True),
    Column("content", Text, nullable=False),
    Column("vector", Text, nullable=False),          # JSON 向量（维度无关）
    Column("source_name", Text),
    Column("doc_id", Text),
    Column("chunk_index", Integer),
    Column("tenant_id", Text, server_default="tenant_default"),
    Column("updated_at", Integer),
)

material_prices = Table(
    "material_prices", METADATA,
    Column("id", Text, primary_key=True),
    Column("material", String(64), nullable=False),
    Column("spec", String(64), nullable=False, server_default=""),
    Column("market", String(64), nullable=False),
    Column("price_low", Numeric(12, 2), nullable=False),
    Column("price_high", Numeric(12, 2), nullable=False),
    Column("unit", String(16), nullable=False, server_default="元/吨"),
    Column("price_date", String(16)),
    Column("source", String(64), nullable=False),
    Column("updated_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
    UniqueConstraint("material", "spec", "market", "source", name="uq_material_price"),
)

llm_calls = Table(
    "llm_calls", METADATA,
    Column("id", Text, primary_key=True),
    Column("agent_type", Text),
    Column("model", Text),
    Column("start_ts", Float),
    Column("duration_ms", Float),
    Column("input_chars", Integer),
    Column("output_chars", Integer),
    Column("est_cost_usd", Float),
    Column("ok", Boolean),
    Column("error", Text),
)

# 常用查询索引（业务列表按用户+时间倒序）
Index("idx_bid_user_created", bid_reviews.c.user_id, bid_reviews.c.created_at.desc())
Index("idx_po_user_created", purchase_orders.c.user_id, purchase_orders.c.created_at.desc())
