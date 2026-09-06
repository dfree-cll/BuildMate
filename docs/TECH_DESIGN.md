# BuildMate 技术设计

## 1. 统一控制层

`backend/application/task_understanding.py` 提供无副作用的任务计划生成：

```text
Task Understanding → Task Decomposition → Domain Routing → Task Runtime
```

`POST /api/v2/chat/intents/preview` 保留单意图预览；`POST /api/v2/chat/tasks/preview` 返回单任务或复合 `TaskPlan`。预览不会创建任务、调用业务写入接口或执行 Revit。

现有工作台 SSE 传输也复用此应用服务：明确业务请求或复合请求直接返回 `task_plan` 事件，不再调用第二套路由 LLM。前端校验计划结构后展示中文步骤和面板入口，不额外发送一次预览 HTTP 请求。预览写入 QA 的隔离历史，可恢复查看，但历史计划不是执行授权。普通问答和不明确请求保留现有问答链路。

## 2. TaskPlan 合同

每个 `TaskStep` 包含 `intent`、`domain`、注册 `route`、参数、缺失参数和是否需要确认。领域枚举为 `knowledge`、`bim`、`bid`、`contract`、`procurement`、`negotiation`。未知领域不能进入下一步。

复合计划最多 20 步，缺少项目、Artifact 或其他必填参数时标记 `needs_clarification`；含不可逆工作流时标记 `needs_confirmation`。

当前为规则式拆句与关键词分类，支持“然后/最后/逗号”等分隔，不是任意复杂自然语言规划器；同域相邻要求合并时保留全部文本。超过 20 个分句显式拒绝，不截断需求。`route` 白名单是计划合同，不代表每个流程均已实现，尤其合同审核不能执行。现阶段仅识别项目与关联文件等基础缺参，材料、数量、审批额度等仍由对应业务表单校验。

## 3. TaskHandoffPackage

跨业务交接使用版本化结构：

```json
{
  "schema_version": "1.0",
  "source_task": "contract-123",
  "target_domain": "negotiation",
  "tenant_id": "tenant-1",
  "project_id": "project-1",
  "actor_id": "user-1",
  "trace_id": "trace-1",
  "correlation_id": "correlation-1",
  "status": "draft",
  "requires_evidence_revalidation": true,
  "facts": {},
  "evidence_refs": [],
  "risk_summary": []
}
```

工厂从可信 `RequestContext` 填入租户、项目、操作者和追踪信息；`assert_context` 校验租户/项目/操作者。合同拒绝额外字段、嵌套 facts、无来源 ID 的证据、无证据的事实/风险、超长内容及已知历史/凭证字段。格式检查不能证明事实真实，也不是通用敏感信息过滤器；包目前仅为草稿，消费前仍须查询源任务归属、证据可见性与真实内容，重新检查目标业务权限。不存在自动消费入口，禁止直接把草稿当审批结果。

## 4. 共享 RAG 与证据

`backend/ports/knowledge.py` 定义 `KnowledgeService` 协议，`backend/rag/service.py` 为现有共享实现，不属于 QA Agent；聊天接口 已按该协议使用服务。新增业务应复用 `KnowledgeSearchRequest` / `RAGAnswer`、`retrieval_run_id` 和 citations；权限由请求上下文和 RAG Repository 过滤。本轮没有改写所有业务 Agent 的检索实现，也没有给 BIM 几何链新增 LLM/RAG 依赖。

结构化价格、模型几何和 Revit 状态仍走各自确定性接口，不把实时事实强行放入 RAG。

## 5. 执行边界

当前已提供计划预览和交接合同；复合确认会创建持久化 `TaskEnvelope`、父子任务和交接记录。各子任务经过统一重试、事件、审批和审计后调用领域 Workflow，跨域自动串联仍必须通过每个领域的权限与证据门禁。

已有单域 QA、投标、采购、谈判运行时注册继续保留；未实现的是从复合计划创建父子任务、跨步交接、整体取消/恢复和统一确认卡，不是从零重建现有运行时。本轮不新增数据库表、队列系统或部署依赖。
