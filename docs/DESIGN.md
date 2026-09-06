# BuildMate 全栈总体设计

**版本**：当前设计
**状态**：开发设计基线
**日期**：2026-09-03
****关联需求**：[PRD](F:/BuildMate/BuildMateDemo/docs/PRD.md)；具体功能设计见本文第 10 节模块索引。

**BIM 国标建模标准**：[BIM_MODELING_STANDARD.md](BIM_MODELING_STANDARD.md)；BIM Agent 的单位、坐标、分类编码、构件参数、证据和交付格式以该文件为可执行基线，几何安全门禁仍按流水线规则执行。

## 1. 项目背景

BuildMate 面向建筑设计、结构审查、BIM 建模、投标、采购和供应商沟通场景。现有工作通常被分散在 CAD/PDF、规范文档、Excel、聊天工具和 Revit 中，存在以下问题：

- 图纸、规范、合同、供应商资料和模型状态相互分散。
- AI 结论缺少页码、实体、构件和版本证据，无法复核。
- 图纸识别、规则审查和 Revit 写入耦合，失败时无法定位和回滚。
- 任务状态依赖人工记忆或进程内状态，重启后无法恢复。
- 不同角色看到的数据和审批权限不清晰。
- 生成的模型可能有重复墙、断墙、异形柱缺失、坐标/角度偏差和构件参数缺失。

BuildMate 的核心价值不是 Agent 数量，而是把资料转换为有证据、可验证、可审批、可追溯、可回滚的工程决策。

当前 采用“双入口、受控路由”原则：BIM 保留独立入口，问答保留独立入口；合同审核、标书查询、建筑规范查询等能力由问答入口的意图识别层分发到白名单 Workflow。它们在产品层不再要求用户理解 Agent 名称，在技术层仍保留独立的规则、证据、审批和任务边界。

## 2. 需求背景与设计目标

### 2.1 产品目标

1. 用户从统一项目上下文上传资料并发起业务任务。
2. 系统保留原始资料、处理过程、证据、模型版本、审批和最终交付物。
3. LLM 只做意图理解、证据组织和自然语言解释；事实、几何和事务由确定性代码控制。
4. BIM Agent 从 PDF/DWG/DXF 到 Revit 2020 实现一个可审查、可审批、可验收的完整链路。
5. 本地模式无需 Docker 即可运行核心 Demo；生产模式以 PostgreSQL、RabbitMQ、Redis、MinIO/S3、Milvus 和观测栈为当前实现组合；pgvector 作为后续可插拔优化，不在本次重构中虚构为已实现能力。

### 2.2 设计不变量

- 所有业务请求均有租户、项目、操作者和 trace 上下文。
- 所有结构化 AI 输出通过 Pydantic 校验。
- 每个 Finding、RAGAnswer 和模型构件均有真实来源引用。
- Revit 写入必须经过静态门禁、Dry-run、持久化人工批准和 Bridge Transaction。
- 原始 RVT 不覆盖；失败自动回滚或明确失败。
- 原图和 Revit 实际视图独立渲染后进行叠图，目标指标均不低于 0.95。

## 3. 总体功能描述

> 架构图的唯一维护入口是 [BuildMate 架构总览](ARCHITECTURE.md)。本文保留技术细节和模块索引，不再重复维护一张容易失真的“大而全”图。

### 3.1 六层架构速览

```mermaid
flowchart TB
  U[四类界面\nBIM / 问答 / 审核端 / 管理端]
  A[统一 API 与安全\nFastAPI /api/v2 + JWT + RequestContext]
  C[统一控制\n意图路由 + Workflow Runtime + 审批审计]
  B[业务能力\nBIM Agent | 知识问答 | 合同/采购/标书 Workflow]
  E[确定性引擎\n解析几何规则 | RAG | 结构化查询 | Revit Bridge]
  D[数据底座\nPostgreSQL | 对象存储 | 向量索引 | 队列/缓存/观测]
  U --> A --> C --> B --> E --> D
  E -.任务/证据/结果.-> C
  C -.状态/交付.-> U
```

这张速览图只表达依赖方向；合同和采购使用 RAG、结构化事实、规则三类来源的细节，以及 BIM 的图纸到 Revit 链路，统一见架构总览文档。

### 3.2 产品模块

| 编号 | 模块 | 说明 |
|---|---|---|
| M01 | 身份与租户 | 登录、租户、成员、角色和请求上下文 |
| M02 | 组织、项目与上下文 | 项目、楼层、坐标、默认配置 |
| M03 | 文件与 Artifact | 上传、校验、版本、内容寻址和下载 |
| M04 | 任务与工作流运行时 | 任务、步骤、事件、重试、取消和恢复 |
| M05 | 意图路由与工具治理 | 问答意图识别、工作流选择、MCP ACL |
| M06 | RAG 知识库 | 文档入库、混合检索、引用和反馈 |
| M07 | BIM Agent | 图纸到模型交付的统一 Agent |
| M08 | 问答与意图路由 | 问答入口、规范/标书查询和受控任务分发 |
| M09 | 标书查询工作流 | 标书检索、定位、摘要和证据结果（后台能力） |
| M10 | 合同审核工作流 | 合同条款审查、风险和人工审核（后台能力） |
| M11 | 采购/供应商工作流 | 采购判断、谈判准备和纪要（后台能力） |
| M12 | 审核与审批中心 | 审核员/管理员处理人工门禁 |
| M13 | 证据、报告与审计 | 证据链、报告、事件和回放 |
| M14 | 前端应用壳 | 导航、项目上下文、状态和会话 |
| M15 | 管理员控制台 | 用户、任务、知识和系统管理 |
| M16 | 审核端 | 待审核业务与知识待补 |
| M17 | 基础设施与可观测性 | 本地/生产适配、指标、日志和 Trace |
| M18 | 安全与合规 | RBAC、RLS、ACL、敏感数据和审计 |
| M19 | 测试与发布治理 | Fixture、回归、评估、CI 和回滚 |

### 3.3 核心用户链路

```text
登录
→ 选择租户/项目
→ 上传 Artifact
→ 创建 WorkflowRun
→ Worker 执行步骤并发布 TaskEvent
→ 意图路由选择白名单 Workflow
→ 业务 Workflow/确定性引擎处理
→ waiting_human
→ 审核员/管理员审批
→ 继续执行或驳回
→ 生成结果、证据和报告
→ 用户查看、下载和回放
```

### 3.4 BIM Agent 链路

```text
PDF/DWG/DXF
→ 来源适配与清洗（B1）
→ WallEvidence（B2）
→ 轴网/坐标/Model IR/门禁（B3）
→ 图纸审查与 WallModel 审批（B4）
→ Dry-run/Bridge/读回/独立叠图（B5）
→ RVT、差异、审计报告和交付
```

### 3.5 问答意图链路

```text
自然语言问题
→ 意图/参数识别
→ 租户、项目和角色校验
→ 查询：RAG/结构化工具 → 引用式答案
→ 合同审核：RAG 条款依据 + 结构化事实 + 确定性规则 → 任务/审核
→ 采购/供应商：RAG 制度资质 + 结构化价格预算 + 确定性规则 → 任务/审批
→ 正式业务：创建受控 Workflow → 任务/审核/报告
→ BIM 请求：跳转 BIM 入口并预填配置
```

## 4. 技术栈与开发语言

### 4.1 后端

| 层 | 技术 | 责任 |
|---|---|---|
| 运行时 | Python 3.11+ | 主业务、Worker、几何和 Agent |
| HTTP API | FastAPI | 当前 API、认证、SSE、OpenAPI |
| 数据校验 | Pydantic + JSON Schema | 请求、事件、AgentResult、Model IR |
| ORM/数据库访问 | SQLAlchemy 2.x | Repository、事务和双数据库适配 |
| 迁移 | Alembic | 版本化 Schema 与回滚 |
| Agent 编排 | LangGraph | 有限状态、检查点和人工中断 |
| 解析 | PyMuPDF、ezdxf、ODA Converter | PDF/DXF/DWG 来源适配 |
| 几何 | NumPy、Shapely、OpenCV | 坐标、拓扑、门禁和独立叠图 |
| OCR/语义辅助 | RapidOCR、Ultralytics YOLO | 轴号、符号和辅助语义，不决定墙坐标 |
| 队列 | 本地 DB Runner / RabbitMQ | 本地与生产双模 |
| 缓存 | Redis（生产） | 会话、短缓存和限流 |
| 向量检索 | 本地索引 / Milvus（生产可选） | Dense + BM25 混合检索；向量库不承担权限事实 |
| 对象存储 | 本地文件 / MinIO/S3 | Artifact 和交付物 |

### 4.2 前端

- Vue 3 + TypeScript + Vite。
- Pinia 按领域拆分状态。
- UI 采用 Apple 风格的克制视觉（白色半透明面板、黑色正文、蓝色主操作、18px 圆角、细边框和低幅动效）以及 Bootstrap 兼容的栅格/间距原则；组件继续复用 Element Plus，详见 [UI/UE 设计规范](UI_UX_DESIGN.md)。
- Axios/Fetch 消费 当前 API；SSE + 轮询兜底。
- 所有页面支持 loading、空状态、失败、无权限和等待审批状态。

### 4.3 Revit 集成

- Revit 2020 API + pyRevit/pyRevit Routes。
- 独立 Windows Bridge 监听 `127.0.0.1:8005`。
- 只接受已审批且哈希匹配的 WallModel/Dry-run 交接物。
- 轴网先于墙柱，Transaction 失败 Rollback，读回后独立叠图。

## 5. 数据库选择

### 5.1 选择结论

- **本地开发/Demo**：SQLite，零外部依赖，支持数据库 Task Runner 和本地向量检索。
- **生产环境**：PostgreSQL，提供事务、并发、JSONB、索引、备份和 RLS 租户防线。
- **向量数据**：当前生产可使用 Milvus，文档元数据和权限始终保存在 PostgreSQL，向量库不作为权限事实源；pgvector 需单独完成迁移和性能验证后再启用。
- **缓存/限流**：生产使用 Redis；不能用 Redis 代替任务和审批事实源。
- **文件/交付物**：本地使用项目数据目录，生产使用 MinIO/S3；数据库只保存 URI、哈希和元数据。

### 5.2 数据访问原则

- API 禁止直接拼接业务 SQL，全部通过 Repository Scope。
- 所有租户实体带 `tenant_id`，项目实体按需带 `project_id`。
- 生产 PostgreSQL 启用 RLS；应用事务设置 `app.tenant_id`。
- 任务创建、首事件和 Outbox 在同一事务中提交。
- 所有更新使用乐观锁 `version`；长任务使用幂等键。

## 6. 系统分层与目录建议

```text
backend/
  domain/          实体、值对象、状态机、错误
  application/     用例、事务、工作流服务
  agents/          BIM Agent 与受控业务能力实现
  engines/         解析、几何、规则、Model IR
  rag/             文档、检索、证据、评估
  ports/           当前仅保留实际被实现和注入的 ArtifactStorage 接口
  adapters/        SQLite/PostgreSQL、Rabbit、S3、Milvus、MCP
  api/v2/          稳定 API 合同
  workers/         任务消费者
frontend/
  src/views/       页面
  src/stores/      Pinia 领域状态
  src/api/         API 客户端
workers/revit_bridge/  Windows Bridge
tests/             单元、合同、集成和端到端测试
docs/design/modules/   功能模块设计文件
```

依赖只能向内：`api → application → domain/ports`；需要多实现替换的边界才新增 port，避免保留零实现/零调用的抽象；Agent 调用 application 和工具，不直接访问数据库。

## 7. API 与事件总体设计

- 新业务 API 使用当前接口合同，带认证、幂等键和 trace；旧接口仅保留存量兼容入口，禁止新增兼容功能。BIM 统一走 `wall_pipeline`；问答通过聊天接口意图路由调用规范查询、标书查询和合同审核 Workflow。
- 创建任务返回 `task_id`、当前状态、下一步动作和事件游标。
- 任务事件统一包含序号、类型、时间、操作者、步骤、摘要和数据哈希。
- SSE 支持 `Last-Event-ID`；断线后轮询 `GET /tasks/{id}`。
- 领域错误统一为 `code/message/details/retryable/trace_id`。
- 外部服务错误不能被吞掉；重试次数和最终错误必须进入任务步骤。
- 本地 Runner 和 RabbitMQ Worker 共用同一 WorkflowRuntime；RabbitMQ 失败最多重投 3 次，随后进入 DLQ，禁止热循环。
- `SourceEntities` 只对同一输入哈希和解析配置复用确定性缓存；审批、Dry-run、Revit 写入和独立叠图永不缓存。

## 8. 安全与数据流设计

```text
用户 → JWT/API → RequestContext → Repository Scope/RLS
                         ↓
                    Workflow/Agent
                         ↓ ACL + Schema
                    MCP/Bridge/外部服务
```

- LLM 不直接访问数据库、文件系统或 Revit。
- Bridge 不具备用户管理、审批和跨项目读取能力。
- Artifact 下载必须经权限校验，不返回任意本地路径。
- 日志脱敏，不记录密码、Token、密钥和完整敏感原文。

## 9. 质量与验收总则

- 后端全量测试 0 失败；前端 lint/typecheck/build 通过。
- 本地模式不启动 Docker 可运行核心 Demo。
- 生产 Compose 健康检查通过。
- 前端真实完成一次上传 → 两次审批 → Revit 2020 → 读回 → 独立叠图 → 下载。
- BIM 独立 edge IoU、源覆盖率、Revit 精确率和相似度均 `>= 0.95`。
- 失败能定位到阶段，任务可恢复或明确失败，不能用假数据标记成功。

## 10. 关联设计文件

- [当前 架构总览](ARCHITECTURE.md)
- [数据库设计](DATABASE_DESIGN.md)
- [UI/UE 界面设计约束](UI_UX_DESIGN.md)
- [M01 身份与租户](design/modules/M01_IDENTITY_TENANT.md)
- [M02 组织与项目](design/modules/M02_PROJECT_CONTEXT.md)
- [M03 文件与 Artifact](design/modules/M03_ARTIFACT.md)
- [M04 任务运行时](design/modules/M04_TASK_RUNTIME.md)
- [M05 意图路由与工具治理](design/modules/M05_ORCHESTRATOR.md)
- [M06 RAG 知识库](design/modules/M06_RAG.md)
- [M07 BIM Agent](design/modules/M07_BIM_AGENT.md)
- [M08 问答与意图路由](design/modules/M08_QA_AGENT.md)
- [M09 标书查询工作流](design/modules/M09_BID_REVIEW_AGENT.md)
- [M10 合同审核工作流](design/modules/M10_PROCUREMENT_AGENT.md)
- [M11 采购/供应商工作流](design/modules/M11_NEGOTIATION_AGENT.md)
- [M12 审核与审批中心](design/modules/M12_APPROVAL_CENTER.md)
- [M13 证据报告审计](design/modules/M13_EVIDENCE_AUDIT.md)
- [M14 前端应用壳](design/modules/M14_FRONTEND_SHELL.md)
- [M15 管理员控制台](design/modules/M15_ADMIN_CONSOLE.md)
- [M16 审核端](design/modules/M16_REVIEW.md)
- [M17 基础设施与可观测性](design/modules/M17_INFRA_OBSERVABILITY.md)
- [M18 安全与合规](design/modules/M18_SECURITY_COMPLIANCE.md)
- [M19 测试与发布治理](design/modules/M19_TEST_RELEASE.md)
