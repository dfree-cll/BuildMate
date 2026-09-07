# BuildMate 数据库设计

**版本**：当前设计
****数据库策略**：本地 SQLite；生产 PostgreSQL 15+
**关联总设计**：[DESIGN.md](DESIGN.md)

## 1. 设计目标

2026-09-05 增补：新增 `agent_memory_sessions` 与 `agent_memory_turns`，按租户、项目、用户、Agent、会话隔离；迁移 `0011_agent_memory`。PG 新表使用租户/创建者 RLS，SQLite 使用同一 Repository Scope；旧检查点不删除。字段、顺序号幂等策略与兼容边界见 [Agent 记忆设计](AGENT_MEMORY.md)。

数据库必须成为任务、审批、模型版本、RAG 检索和审计的持久化事实源，满足：

- 租户隔离、项目隔离和角色授权。
- 任务跨进程、跨重启恢复。
- 幂等创建、乐观锁和至少一次消息投递。
- Artifact、Model IR、RAG 文档和交付物版本不可变。
- 查询可审计、可分页、可按 trace 回放。
- SQLite 与 PostgreSQL 使用相同领域模型和 Repository 接口。

## 2. 数据库选型

### 2.1 SQLite（本地）

- 用于离线 Demo、单机开发和自动化测试。
- 开启 WAL、外键约束和 busy timeout。
- 任务 Runner 直接从数据库领取任务。
- 文件和向量使用本地目录/本地索引，数据库只保存元数据。
- 不承诺多实例高并发；不作为生产集群数据库。

### 2.2 PostgreSQL（生产）

- 提供事务、JSONB、复合索引、并发控制、备份和 RLS。
- 应用使用独立 `buildmate_app` 角色；迁移使用 owner 角色。
- 每个请求事务设置 `app.tenant_id`，RLS 缺少该值时拒绝读取。
- 任务和 Outbox 用 `FOR UPDATE SKIP LOCKED`/lease 机制领取。
- 大文件和向量不直接写进主表，使用对象存储和 Milvus；Milvus 只承担向量检索，权限和文档状态仍以 PostgreSQL 为准。

## 3. 通用字段规范

所有租户级表按适用性包含：

```text
id                UUID/字符串主键
tenant_id         租户 ID，不可为空
project_id        项目 ID，可为空（租户级资料）
created_by        创建者
created_at        UTC 时间，带时区
updated_at        UTC 时间，带时区
version           乐观锁整数
```

字段要求：金额使用 Decimal；几何/长度使用双精度并明确单位；状态使用受控枚举；JSON 只存扩展字段，不把核心查询字段藏在 JSON 中。

## 4. 核心表设计

### 4.1 租户与项目

#### `tenants`

`id, name, code, status, settings_json, created_at, updated_at, version`

唯一约束：`code`。索引：`status`。

#### `users`

`id, username, password_hash, display_name, email, status, last_login_at, created_at, updated_at, version`

唯一约束：`username`、规范化 `email`。禁止存明文密码。

#### `tenant_memberships`

`id, tenant_id, user_id, role, status, created_by, created_at, updated_at, version`

唯一约束：`tenant_id + user_id`。角色：`admin/reviewer/project/user`。

#### `projects`

`id, tenant_id, code, name, status, default_unit, coordinate_system_json, origin_json, revit_policy_json, created_by, created_at, updated_at, version`

唯一约束：`tenant_id + code`。索引：`tenant_id + status`。

#### `project_memberships`

`id, tenant_id, project_id, user_id, role_override, status, created_at, updated_at, version`

唯一约束：`project_id + user_id`。

#### `project_levels`

`id, tenant_id, project_id, code, name, elevation_m, revit_level_name, sort_order, created_at, updated_at, version`

唯一约束：`project_id + code`；索引：`project_id + sort_order`。

### 4.2 Artifact 与处理

#### `artifacts`

`id, tenant_id, project_id, kind, filename, media_type, size_bytes, sha256, storage_uri, source_role, parent_artifact_id, status, metadata_json, created_by, created_at, updated_at, version`

唯一约束：`tenant_id + sha256 + kind + parent_artifact_id`（按业务允许重复版本）。索引：`tenant_id + project_id + created_at`、`sha256`。

#### `artifact_derivatives`

`id, tenant_id, project_id, source_artifact_id, derivative_artifact_id, transform_name, transform_version, input_sha256, output_sha256, created_at`

用于保留 PDF/DWG 转换、渲染、JSON 和报告谱系。

### 4.3 任务、步骤、事件与 Outbox

#### `workflow_runs`

`id, tenant_id, project_id, workflow, status, next_action, actor_id, idempotency_key, correlation_id, current_step, input_artifact_ids_json, result_json, error_code, error_message, started_at, finished_at, created_at, updated_at, version`

唯一约束：`tenant_id + idempotency_key`。索引：`tenant_id + project_id + status`、`correlation_id`。

#### `workflow_steps`

`id, tenant_id, project_id, workflow_run_id, position, name, status, attempts, input_json, output_json, error_code, error_message, started_at, finished_at, lease_generation, created_at, updated_at, version`

唯一约束：`workflow_run_id + position`；索引：`workflow_run_id + position`。

#### `task_events`

`id, tenant_id, project_id, workflow_run_id, sequence_no, event_type, step_name, actor_id, payload_json, payload_sha256, trace_id, occurred_at`

唯一约束：`workflow_run_id + sequence_no`；序号是 SSE 和审计顺序权威。

#### `outbox_events`

`id, tenant_id, project_id, aggregate_type, aggregate_id, event_type, payload_json, status, attempts, available_at, lease_until, lease_generation, published_at, last_error, created_at, updated_at`

索引：`status + available_at`。旧 lease 回执必须通过 generation/CAS 检查。

### 4.4 审查、模型与审批

#### `review_runs`

`id, tenant_id, project_id, workflow_run_id, review_type, status, verdict, risk_level, summary_json, created_at, updated_at, version`

#### `review_findings`

`id, tenant_id, project_id, review_run_id, rule_id, severity, status, description, location_json, evidence_refs_json, recommendation, created_at, updated_at, version`

约束：`evidence_refs_json` 不得为空（除非状态为 `no_evidence`）。

#### `model_ir_versions`

`id, tenant_id, project_id, workflow_run_id, source_sha256, parser_version, pipeline_version, schema_version, payload_sha256, payload_artifact_id, status, approved_at, approved_by, created_at, updated_at, version`

唯一约束：`project_id + source_sha256 + parser_version + pipeline_version + payload_sha256`。

#### `build_runs`

`id, tenant_id, project_id, workflow_run_id, model_ir_version_id, target_model_policy, target_model_uri, bridge_version, status, dry_run_json, readback_json, diff_artifact_id, output_artifact_id, rollback_status, created_at, updated_at, version`

#### `approvals`

`id, tenant_id, project_id, workflow_run_id, approval_type, requested_action, decision, comment, actor_id, input_version, decided_at, created_at, version`

唯一约束：`workflow_run_id + approval_type + input_version`；不能由客户端指定审批步骤。

#### `tool_calls`

`id, tenant_id, project_id, workflow_run_id, step_id, tool_name, actor_id, input_sha256, output_sha256, status, latency_ms, retryable, error_code, created_at`

### 4.5 RAG

#### `knowledge_documents`

`id, tenant_id, project_id, scope, source_type, source_uri, title, version, content_hash, parser_version, chunking_version, embedding_model, status, created_by, created_at, updated_at, version`

唯一约束：`tenant_id + scope + project_id + content_hash + parser_version + chunking_version + embedding_model`。

#### `knowledge_chunks`

`id, tenant_id, project_id, document_id, chunk_index, content, metadata_json, embedding_model, vector_ref, page_no, element_refs_json, token_count, content_hash, created_at`

唯一约束：`document_id + chunk_index`；向量本体存 Milvus/本地索引，`vector_ref` 回指。

#### `knowledge_ingest_jobs`

`id, tenant_id, project_id, document_id, status, attempts, error_code, error_message, pipeline_version, started_at, finished_at, created_at, updated_at, version`

#### `retrieval_runs`

`id, tenant_id, project_id, query, query_type, filters_json, index_version, top_k, latency_ms, fallback_used, created_by, created_at`

#### `retrieval_hits`

`id, tenant_id, project_id, retrieval_run_id, chunk_id, dense_score, sparse_score, rerank_score, rank, accepted, created_at`

#### `knowledge_feedback`

`id, tenant_id, project_id, retrieval_run_id, chunk_id, label, comment, operator_id, created_at`

### 4.6 LLM 与审计

#### `llm_calls`

`id, tenant_id, project_id, workflow_run_id, provider, model, purpose, input_tokens, output_tokens, latency_ms, status, error_code, cost_estimate, created_at`

不保存完整敏感 Prompt；必要时保存脱敏摘要哈希。

#### `audit_events`

`id, tenant_id, project_id, actor_id, actor_role, action, resource_type, resource_id, before_sha256, after_sha256, trace_id, metadata_json, occurred_at`

审计事件只追加不更新。

## 5. 关系与删除策略

```text
Tenant 1─N Users/Members/Projects/Artifacts/Tasks/Knowledge
Project 1─N Levels/Artifacts/Tasks/Reviews/ModelIR/Builds
WorkflowRun 1─N Steps/Events/Approvals/ToolCalls
ReviewRun 1─N Findings
KnowledgeDocument 1─N Chunks/IngestJobs
RetrievalRun 1─N Hits/Feedback
ModelIR 1─N BuildRuns
```

- 原始 Artifact、Model IR、审批、事件和审计默认不可物理删除，只能归档/失效。
- 删除项目需要管理员权限、二次确认、备份和异步清理计划。
- 生产删除必须有保留期、审计事件和恢复方案。

## 6. 事务与并发

- 创建任务、首事件、Outbox 在一个事务中提交。
- 审批写入 `approvals`、任务状态和 TaskEvent 在一个事务中提交。
- WorkflowStep 外部调用前写 `running`，结束时 CAS 更新 `succeeded/failed`。
- Outbox 使用 lease generation；旧消费者不能覆盖新状态。
- Revit Bridge 只在任务版本、Model IR 哈希和审批版本一致时执行。

## 7. 迁移、备份与验收

- Alembic 迁移必须可升级、可回滚或提供明确数据修复脚本。
- 迁移前备份，生产先在影子数据库演练。
- SQLite 测试和 PostgreSQL 集成测试必须覆盖相同 Repository 合同。
- 必测：跨租户隔离、重复提交、服务重启恢复、审批并发、Outbox 重复投递、文档重复入库和旧版本不可变。
