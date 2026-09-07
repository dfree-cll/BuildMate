# BuildMate 公共合同


代码事实源：

- `backend/domain/contracts.py`：RequestContext、TaskEnvelope、TaskEvent、AgentResult、EvidenceRef。
- `backend/domain/intent.py`：问答意图路由使用注册意图、参数、置信度、证据来源（RAG/结构化/规则/BIM）和白名单 route；问答不得生成任意工具或工作流。
- `backend/domain/model_ir.py`：Model IR 合同。
- `backend/rag/contracts.py`：KnowledgeSearchRequest、KnowledgeHit、RAGAnswer。
- `workers/revit_bridge/contracts.py`：Revit Bridge 请求/响应。
- `backend/engines/wall_pipeline/contracts.py`：SourceManifest、SourceEntities、WallEvidence、WallModel、RevitResult、AuditReport。
- `ModelingStandardConfig`：BIM Agent 默认国标建模 profile；标准信息随 SourceManifest/WallModel 和 Revit 构件参数交付。
- `docs/contracts/wall_pipeline/*.schema.json`：上述 Pydantic 合同导出的 JSON Schema；用 `scripts/export_wall_pipeline_schemas.py` 更新。
- `/openapi.json`：当前 API OpenAPI。

## 兼容规则

- `TaskEnvelope.schema_version` 与 `ModelIR.schema_version` 当前为 `2.x`。
- `TaskEnvelope.options` 保存工作流的可复现配置（例如墙体流水线的单位、比例、楼层和 Revit 目标）；任务创建时与幂等键一起确定。
- 增加可选字段为向后兼容；删除、改名、含义变化必须提升主版本。
- LLM 生成的数据只有通过上述 Pydantic 合同后才可持久化或进入下一步。
- 引用只允许指向本次召回的 `KnowledgeHit.chunk_id`。
- Revit 只接受 `gate.status=pass` 且 `review_status=approved` 的 `wall_model`；每面墙必须带 evidence_ids 和 source_refs。
- 墙体主链路通过 `/tools/run-wall-model` 传递已批准的 WallModel/`revit_result` DTO，
  不把任意 Python 脚本暴露给墙体工作流；成功响应必须包含隔离 RVT、实际视图 PNG
  及其 SHA-256，才能进入独立叠图验收。

## 核心状态机

```text
queued -> running -> waiting_human -> resumed
       -> succeeded | failed | canceled
```

Build 状态（静态 Dry-run 报告写入后进入等待审批）：

```text
waiting_approval -> approved/rejected
approved -> bridge_running -> succeeded/failed/rolled_back
```

墙体 Revit 状态：

```text
pending_approval -> ready_for_dry_run -> dry_run_passed
-> write_approved -> succeeded | failed | rolled_back
```
