"""数据库 Schema 唯一事实源（SQLAlchemy Core）

企业化改造：此前建表 DDL 散落在 scripts/init_db.py（字符串）、observability.py、
vector_store.py 各自为政——现在统一到这里，供三处消费：
  ① Alembic 迁移（migrations/env.py 的 target_metadata）
  ② init_db.py（METADATA.create_all，demo/离线快速建库）
  ③ 本地初始化脚本直接 create_all

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
    # v2 RAG metadata.  Nullable columns preserve the existing seed/import path;
    # new ingestion always fills them and searches enforce scope explicitly.
    Column("project_id", String(64)),
    Column("scope", String(16), nullable=False, server_default="tenant"),
    Column("metadata_json", Text),
    Column("embedding_model", String(128)),
    Column("page_no", Integer),
    Column("element_refs", Text),
    Column("token_count", Integer),
    Column("chunk_version", String(32), nullable=False, server_default="1"),
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

# ── 图纸→BIM 成长闭环 ──────────────────────────────────────
drawing_profiles = Table(
    "drawing_profiles", METADATA,
    Column("id", Text, primary_key=True),
    Column("tenant_id", String(64), nullable=False, server_default="tenant_default"),
    Column("name", String(128), nullable=False),
    Column("organization", String(128)),
    Column("discipline", String(32), nullable=False, server_default="unknown"),
    Column("signature", String(128), nullable=False),
    Column("config", Text, nullable=False),
    Column("sample_count", Integer, nullable=False, server_default="0"),
    Column("success_count", Integer, nullable=False, server_default="0"),
    Column("is_active", Boolean, nullable=False, server_default=text("true")),
    Column("created_by", Text),
    Column("created_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
    Column("updated_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
    UniqueConstraint("tenant_id", "signature", name="uq_drawing_profile_signature"),
)

build_runs = Table(
    "build_runs", METADATA,
    Column("id", Text, primary_key=True),
    Column("tenant_id", String(64), nullable=False, server_default="tenant_default"),
    Column("user_id", Text),
    Column("review_id", Text, nullable=False),
    Column("profile_id", Text),
    Column("source_path", Text),
    Column("source_hash", String(64)),
    Column("schema_version", String(16)),
    Column("pipeline_version", String(32)),
    Column("model_scope", String(32)),
    Column("status", String(24), nullable=False, server_default="pending"),
    Column("quality", Text),
    Column("gate_status", Text),
    Column("result", Text),
    Column("error_msg", Text),
    Column("started_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
    Column("finished_at", DateTime),
)

extraction_feedback = Table(
    "extraction_feedback", METADATA,
    Column("id", Text, primary_key=True),
    Column("tenant_id", String(64), nullable=False, server_default="tenant_default"),
    Column("profile_id", Text),
    Column("build_id", Text),
    Column("source_element_id", Text),
    Column("predicted_type", String(64)),
    Column("correct_type", String(64)),
    Column("predicted_geometry", Text),
    Column("correct_geometry", Text),
    Column("reason", Text),
    Column("operator", Text),
    Column("created_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
)

element_mappings = Table(
    "element_mappings", METADATA,
    Column("id", Text, primary_key=True),
    Column("tenant_id", String(64), nullable=False, server_default="tenant_default"),
    Column("build_id", Text, nullable=False),
    Column("source_element_id", Text),
    Column("ir_element_id", Text, nullable=False),
    Column("revit_element_id", Text),
    Column("element_type", String(64)),
    Column("creation_mode", String(32)),
    Column("validation", Text),
    Column("created_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
    UniqueConstraint("build_id", "ir_element_id", name="uq_build_ir_element"),
)

# ── 项目级 Revit 模型 ──────────────────────────────────────
# 楼层不是一次建模任务的临时属性：同一 project 的所有楼层必须共享一套
# 坐标、标高和目标 RVT。因此将项目与标高表独立持久化，禁止从单张图纸推断并覆盖。
bim_projects = Table(
    "bim_projects", METADATA,
    Column("id", String(64), primary_key=True),
    Column("tenant_id", String(64), nullable=False, server_default="tenant_default"),
    Column("owner_id", Text, nullable=False),
    Column("name", String(128), nullable=False),
    Column("template_path", Text),
    Column("model_path", Text),
    Column("status", String(24), nullable=False, server_default="active"),
    Column("created_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
    Column("updated_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
)

bim_project_levels = Table(
    "bim_project_levels", METADATA,
    Column("id", String(64), primary_key=True),
    Column("project_id", String(64), nullable=False),
    Column("floor_code", String(32), nullable=False),
    Column("elevation_mm", Integer, nullable=False),
    Column("created_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
    Column("updated_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
    UniqueConstraint("project_id", "floor_code", name="uq_bim_project_floor"),
    UniqueConstraint("project_id", "elevation_mm", name="uq_bim_project_elevation"),
)

# ── v2 domain kernel ────────────────────────────────────────────────
# These tables form the durable source of truth for workflows.  Agents,
# queues and SSE streams are projections of this state, never replacements.
tenants = Table(
    "tenants", METADATA,
    Column("id", String(64), primary_key=True),
    Column("name", String(128), nullable=False),
    Column("status", String(24), nullable=False, server_default="active"),
    Column("created_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
    Column("updated_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
    Column("version", Integer, nullable=False, server_default="1"),
)

tenant_memberships = Table(
    "tenant_memberships", METADATA,
    Column("id", String(64), primary_key=True),
    Column("tenant_id", String(64), nullable=False),
    Column("user_id", Text, nullable=False),
    Column("role", String(32), nullable=False, server_default="user"),
    Column("status", String(24), nullable=False, server_default="active"),
    Column("created_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
    Column("updated_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
    Column("version", Integer, nullable=False, server_default="1"),
    UniqueConstraint("tenant_id", "user_id", name="uq_tenant_membership_user"),
)

projects = Table(
    "projects", METADATA,
    Column("id", String(64), primary_key=True),
    Column("tenant_id", String(64), nullable=False),
    Column("name", String(128), nullable=False),
    Column("status", String(24), nullable=False, server_default="active"),
    Column("created_by", Text, nullable=False),
    Column("created_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
    Column("updated_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
    Column("version", Integer, nullable=False, server_default="1"),
    UniqueConstraint("tenant_id", "name", name="uq_project_tenant_name"),
)

artifacts = Table(
    "artifacts", METADATA,
    Column("id", String(64), primary_key=True),
    Column("tenant_id", String(64), nullable=False),
    Column("project_id", String(64)),
    Column("kind", String(32), nullable=False),
    Column("filename", String(256), nullable=False),
    Column("media_type", String(128)),
    Column("storage_uri", Text, nullable=False),
    Column("sha256", String(64), nullable=False),
    Column("size_bytes", Integer, nullable=False),
    Column("status", String(24), nullable=False, server_default="active"),
    Column("created_by", Text, nullable=False),
    Column("created_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
    Column("updated_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
    Column("version", Integer, nullable=False, server_default="1"),
)

workflow_runs = Table(
    "workflow_runs", METADATA,
    Column("id", String(64), primary_key=True),
    Column("tenant_id", String(64), nullable=False),
    Column("project_id", String(64)),
    Column("actor_id", Text, nullable=False),
    Column("workflow", String(64), nullable=False),
    Column("status", String(24), nullable=False, server_default="queued"),
    Column("idempotency_key", String(128), nullable=False),
    Column("correlation_id", String(128), nullable=False),
    Column("schema_version", String(16), nullable=False, server_default="2.0"),
    Column("input_artifact_ids", Text, nullable=False, server_default="[]"),
    Column("options", Text, nullable=False, server_default="{}"),
    Column("result", Text),
    Column("error_type", String(24)),
    Column("error_message", Text),
    Column("created_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
    Column("updated_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
    Column("version", Integer, nullable=False, server_default="1"),
    UniqueConstraint("tenant_id", "idempotency_key", name="uq_workflow_tenant_idempotency"),
)

workflow_steps = Table(
    "workflow_steps", METADATA,
    Column("id", String(64), primary_key=True),
    Column("run_id", String(64), nullable=False),
    Column("tenant_id", String(64), nullable=False),
    Column("project_id", String(64)),
    Column("name", String(64), nullable=False),
    Column("position", Integer, nullable=False),
    Column("status", String(24), nullable=False, server_default="queued"),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("input_payload", Text),
    Column("output_payload", Text),
    Column("error_message", Text),
    Column("started_at", DateTime),
    Column("finished_at", DateTime),
    Column("created_by", Text, nullable=False),
    Column("created_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
    Column("updated_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
    Column("version", Integer, nullable=False, server_default="1"),
    UniqueConstraint("run_id", "position", name="uq_workflow_step_position"),
)

task_events = Table(
    "task_events", METADATA,
    Column("id", String(64), primary_key=True),
    Column("run_id", String(64), nullable=False),
    Column("tenant_id", String(64), nullable=False),
    Column("project_id", String(64)),
    Column("seq", Integer, nullable=False),
    Column("type", String(64), nullable=False),
    Column("stage", String(64), nullable=False, server_default=""),
    Column("status", String(24), nullable=False),
    Column("payload", Text, nullable=False, server_default="{}"),
    Column("trace_id", String(128), nullable=False),
    Column("created_by", Text, nullable=False),
    Column("occurred_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
    Column("version", Integer, nullable=False, server_default="1"),
    UniqueConstraint("run_id", "seq", name="uq_task_event_run_seq"),
)

approvals = Table(
    "approvals", METADATA,
    Column("id", String(64), primary_key=True),
    Column("run_id", String(64), nullable=False),
    Column("tenant_id", String(64), nullable=False),
    Column("project_id", String(64)),
    Column("action", String(64), nullable=False),
    Column("decision", String(24), nullable=False),
    Column("reason", Text),
    Column("created_by", Text, nullable=False),
    Column("created_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
    Column("version", Integer, nullable=False, server_default="1"),
)

review_findings = Table(
    "review_findings", METADATA,
    Column("id", String(64), primary_key=True),
    Column("run_id", String(64), nullable=False),
    Column("tenant_id", String(64), nullable=False),
    Column("project_id", String(64)),
    Column("rule_id", String(64)),
    Column("track", String(16), nullable=False),
    Column("severity", String(16), nullable=False),
    Column("confidence", Float, nullable=False),
    Column("description", Text, nullable=False),
    Column("suggestion", Text),
    Column("evidence_refs", Text, nullable=False, server_default="[]"),
    Column("status", String(24), nullable=False, server_default="open"),
    Column("created_by", Text, nullable=False),
    Column("created_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
    Column("updated_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
    Column("version", Integer, nullable=False, server_default="1"),
)

review_runs_v2 = Table(
    "review_runs_v2", METADATA,
    Column("id", String(64), primary_key=True),
    Column("tenant_id", String(64), nullable=False),
    Column("project_id", String(64), nullable=False),
    Column("workflow_run_id", String(64)),
    Column("model_ir_version_id", String(64), nullable=False),
    Column("status", String(24), nullable=False, server_default="pending"),
    Column("verdict", String(24)),
    Column("report", Text),
    Column("created_by", Text, nullable=False),
    Column("created_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
    Column("updated_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
    Column("version", Integer, nullable=False, server_default="1"),
)

model_ir_versions = Table(
    "model_ir_versions", METADATA,
    Column("id", String(64), primary_key=True),
    Column("tenant_id", String(64), nullable=False),
    Column("project_id", String(64), nullable=False),
    Column("source_artifact_id", String(64), nullable=False),
    Column("source_sha256", String(64), nullable=False),
    Column("schema_version", String(16), nullable=False),
    Column("pipeline_version", String(32), nullable=False),
    Column("parser_version", String(32), nullable=False),
    Column("payload", Text, nullable=False),
    Column("payload_sha256", String(64), nullable=False),
    Column("status", String(24), nullable=False, server_default="pending"),
    Column("created_by", Text, nullable=False),
    Column("created_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
    Column("version", Integer, nullable=False, server_default="1"),
    UniqueConstraint(
        "tenant_id", "project_id", "source_sha256", "pipeline_version", "parser_version",
        name="uq_model_ir_reproducible_version",
    ),
)

model_build_runs = Table(
    "model_build_runs", METADATA,
    Column("id", String(64), primary_key=True),
    Column("tenant_id", String(64), nullable=False),
    Column("project_id", String(64), nullable=False),
    Column("workflow_run_id", String(64)),
    Column("model_ir_version_id", String(64), nullable=False),
    Column("status", String(32), nullable=False, server_default="pending_dry_run"),
    Column("dry_run_report", Text),
    Column("approval_id", String(64)),
    Column("result", Text),
    Column("diff", Text),
    Column("rollback_snapshot_uri", Text),
    Column("created_by", Text, nullable=False),
    Column("created_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
    Column("updated_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
    Column("version", Integer, nullable=False, server_default="1"),
)

chat_sessions_v2 = Table(
    "chat_sessions_v2", METADATA,
    Column("id", String(64), primary_key=True),
    Column("tenant_id", String(64), nullable=False),
    Column("project_id", String(64)),
    Column("title", String(256), nullable=False, server_default="新对话"),
    Column("status", String(24), nullable=False, server_default="active"),
    Column("created_by", Text, nullable=False),
    Column("created_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
    Column("updated_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
    Column("version", Integer, nullable=False, server_default="1"),
)

chat_messages_v2 = Table(
    "chat_messages_v2", METADATA,
    Column("id", String(64), primary_key=True),
    Column("session_id", String(64), nullable=False),
    Column("tenant_id", String(64), nullable=False),
    Column("project_id", String(64)),
    Column("seq", Integer, nullable=False),
    Column("role", String(16), nullable=False),
    Column("content", Text, nullable=False),
    Column("retrieval_run_id", String(64)),
    Column("citations", Text, nullable=False, server_default="[]"),
    Column("confidence", Float),
    Column("grounded", Boolean, nullable=False, server_default=text("false")),
    Column("created_by", Text, nullable=False),
    Column("created_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
    Column("version", Integer, nullable=False, server_default="1"),
    UniqueConstraint("session_id", "seq", name="uq_chat_message_session_seq"),
)

tool_calls = Table(
    "tool_calls", METADATA,
    Column("id", String(64), primary_key=True),
    Column("run_id", String(64), nullable=False),
    Column("step_id", String(64)),
    Column("tenant_id", String(64), nullable=False),
    Column("project_id", String(64)),
    Column("tool_name", String(128), nullable=False),
    Column("tool_version", String(32), nullable=False, server_default="1"),
    Column("status", String(24), nullable=False),
    Column("input_payload", Text),
    Column("output_payload", Text),
    Column("error_message", Text),
    Column("duration_ms", Float),
    Column("trace_id", String(128), nullable=False),
    Column("created_by", Text, nullable=False),
    Column("created_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
    Column("version", Integer, nullable=False, server_default="1"),
)

# ── v2 RAG registry and audit ──────────────────────────────────────
knowledge_documents = Table(
    "knowledge_documents", METADATA,
    Column("id", String(64), primary_key=True),
    Column("tenant_id", String(64), nullable=False),
    Column("project_id", String(64)),
    Column("scope", String(16), nullable=False),
    Column("source_type", String(32), nullable=False),
    Column("source_uri", Text, nullable=False),
    Column("title", String(256), nullable=False),
    Column("document_version", String(32), nullable=False, server_default="1"),
    Column("content_hash", String(64), nullable=False),
    Column("parser_version", String(32), nullable=False),
    Column("chunking_version", String(32), nullable=False),
    Column("embedding_model", String(128), nullable=False),
    Column("metadata_json", Text, nullable=False, server_default="{}"),
    Column("status", String(24), nullable=False, server_default="indexing"),
    Column("created_by", Text, nullable=False),
    Column("created_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
    Column("updated_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
    Column("version", Integer, nullable=False, server_default="1"),
    UniqueConstraint(
        "tenant_id", "project_id", "content_hash", "parser_version",
        "chunking_version", "embedding_model",
        name="uq_knowledge_document_index_version",
    ),
)

knowledge_ingest_jobs = Table(
    "knowledge_ingest_jobs", METADATA,
    Column("id", String(64), primary_key=True),
    Column("document_id", String(64), nullable=False),
    Column("tenant_id", String(64), nullable=False),
    Column("project_id", String(64)),
    Column("status", String(24), nullable=False, server_default="queued"),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("error", Text),
    Column("pipeline_version", String(32), nullable=False),
    Column("created_by", Text, nullable=False),
    Column("created_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
    Column("updated_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
    Column("version", Integer, nullable=False, server_default="1"),
)

retrieval_runs = Table(
    "retrieval_runs", METADATA,
    Column("id", String(64), primary_key=True),
    Column("tenant_id", String(64), nullable=False),
    Column("project_id", String(64)),
    Column("query", Text, nullable=False),
    Column("query_type", String(32), nullable=False),
    Column("scope", String(16), nullable=False),
    Column("filters_json", Text, nullable=False, server_default="{}"),
    Column("hit_ids", Text, nullable=False, server_default="[]"),
    Column("index_version", String(64), nullable=False),
    Column("latency_ms", Float, nullable=False),
    Column("status", String(24), nullable=False),
    Column("created_by", Text, nullable=False),
    Column("created_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
    Column("version", Integer, nullable=False, server_default="1"),
)

knowledge_feedback = Table(
    "knowledge_feedback", METADATA,
    Column("id", String(64), primary_key=True),
    Column("retrieval_run_id", String(64), nullable=False),
    Column("chunk_id", Text),
    Column("tenant_id", String(64), nullable=False),
    Column("project_id", String(64)),
    Column("label", String(24), nullable=False),
    Column("operator", Text, nullable=False),
    Column("comment", Text),
    Column("created_by", Text, nullable=False),
    Column("created_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
    Column("version", Integer, nullable=False, server_default="1"),
)

outbox_events = Table(
    "outbox_events", METADATA,
    Column("id", String(64), primary_key=True),
    Column("aggregate_type", String(64), nullable=False),
    Column("aggregate_id", String(64), nullable=False),
    Column("tenant_id", String(64), nullable=False),
    Column("project_id", String(64)),
    Column("event_type", String(64), nullable=False),
    Column("routing_key", String(128), nullable=False),
    Column("payload", Text, nullable=False),
    Column("status", String(24), nullable=False, server_default="pending"),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("last_error", Text),
    Column("available_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
    Column("published_at", DateTime),
    Column("created_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
    Column("version", Integer, nullable=False, server_default="1"),
)

agent_memory_sessions = Table(
    "agent_memory_sessions", METADATA,
    Column("id", String(64), primary_key=True),
    Column("tenant_id", String(64), nullable=False),
    Column("project_id", String(64)),
    Column("created_by", String(128), nullable=False),
    Column("agent", String(32), nullable=False),
    Column("session_id", String(128), nullable=False),
    Column("title", String(256), nullable=False, server_default="新会话"),
    Column("summary", Text, nullable=False, server_default=""),
    Column("preferences", Text, nullable=False, server_default="{}"),
    Column("created_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
    Column("updated_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
    Column("version", Integer, nullable=False, server_default="1"),
)
agent_memory_turns = Table(
    "agent_memory_turns", METADATA,
    Column("id", String(64), primary_key=True),
    Column("memory_id", String(64), nullable=False),
    Column("tenant_id", String(64), nullable=False),
    Column("project_id", String(64)),
    Column("created_by", String(128), nullable=False),
    Column("seq", Integer, nullable=False),
    Column("user_text", Text, nullable=False),
    Column("answer", Text, nullable=False),
    Column("result", Text, nullable=False, server_default="{}"),
    Column("created_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
    UniqueConstraint("memory_id", "seq", name="uq_agent_memory_turn_seq"),
)
Index("idx_agent_memory_scope", agent_memory_sessions.c.tenant_id,
      agent_memory_sessions.c.project_id, agent_memory_sessions.c.created_by,
      agent_memory_sessions.c.agent, agent_memory_sessions.c.updated_at)
Index("idx_agent_memory_turns", agent_memory_turns.c.memory_id, agent_memory_turns.c.seq)

# 常用查询索引（业务列表按用户+时间倒序）
Index("idx_bid_user_created", bid_reviews.c.user_id, bid_reviews.c.created_at.desc())
Index("idx_po_user_created", purchase_orders.c.user_id, purchase_orders.c.created_at.desc())
Index("idx_build_review", build_runs.c.review_id, build_runs.c.started_at.desc())
Index("idx_feedback_profile", extraction_feedback.c.profile_id,
      extraction_feedback.c.created_at.desc())
Index("idx_bim_project_tenant", bim_projects.c.tenant_id, bim_projects.c.updated_at.desc())
Index("idx_bim_project_level", bim_project_levels.c.project_id,
      bim_project_levels.c.elevation_mm)
Index("idx_project_tenant_updated", projects.c.tenant_id, projects.c.updated_at.desc())
Index("idx_artifact_scope", artifacts.c.tenant_id, artifacts.c.project_id,
      artifacts.c.created_at.desc())
Index("idx_workflow_scope_status", workflow_runs.c.tenant_id, workflow_runs.c.project_id,
      workflow_runs.c.status, workflow_runs.c.created_at.desc())
Index("idx_task_event_stream", task_events.c.tenant_id, task_events.c.run_id,
      task_events.c.seq)
Index("idx_model_ir_scope", model_ir_versions.c.tenant_id, model_ir_versions.c.project_id,
      model_ir_versions.c.created_at.desc())
Index("idx_review_run_scope", review_runs_v2.c.tenant_id, review_runs_v2.c.project_id,
      review_runs_v2.c.created_at.desc())
Index("idx_model_build_scope", model_build_runs.c.tenant_id, model_build_runs.c.project_id,
      model_build_runs.c.created_at.desc())
Index("idx_chat_session_scope", chat_sessions_v2.c.tenant_id, chat_sessions_v2.c.project_id,
      chat_sessions_v2.c.updated_at.desc())
Index("idx_chat_message_stream", chat_messages_v2.c.tenant_id, chat_messages_v2.c.session_id,
      chat_messages_v2.c.seq)
Index("idx_knowledge_document_scope", knowledge_documents.c.tenant_id,
      knowledge_documents.c.project_id, knowledge_documents.c.scope,
      knowledge_documents.c.status)
Index("idx_knowledge_chunk_scope", knowledge_chunks.c.tenant_id,
      knowledge_chunks.c.project_id, knowledge_chunks.c.scope)
Index("idx_retrieval_scope", retrieval_runs.c.tenant_id, retrieval_runs.c.project_id,
      retrieval_runs.c.created_at.desc())
Index("idx_outbox_pending", outbox_events.c.status, outbox_events.c.available_at)
