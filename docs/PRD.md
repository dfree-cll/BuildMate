# BuildMate 全栈产品需求文档（PRD）

**版本**：当前设计
**状态**：可执行基线（产品范围、角色、合同和验收口径已锁定）
**更新日期**：2026-09-06
**适用仓库**：`F:\BuildMate\BuildMateDemo`
**
> 本文是 BuildMate 整体项目的产品需求，不是只描述 BIM 的单模块说明。当前 对用户提供 BIM 独立入口和问答独立入口；合同审核、标书查询、建筑规范查询等能力由问答入口的意图识别层调用受控 Workflow。后台可以保留多个能力实现，但不要求用户理解或切换多个 Agent。

架构阅读入口：[`docs/ARCHITECTURE.md`](ARCHITECTURE.md)。产品需求只描述用户能看到的入口、任务和结果，系统分层与依赖方向以该文档为准。

> BIM Agent 的国标建模 profile、模型生成字段和标准边界见 [BIM 国标建模标准](BIM_MODELING_STANDARD.md)；几何安全门禁仍按流水线规则执行。

## 0. 文档边界与决策来源

### 0.1 本 PRD 采用的内容

- `02-Vibe Coding 项目开发流程.md` 只作为研发过程约束：Research → PRD → Tech Design → AGENTS.md → Build。
- 架构图只作为目标边界：体验层、API 与安全、Orchestrator、Agent、AEC 引擎、MCP Gateway、数据基础设施和平台治理。
- 本文把已经确认的业务决策、当前仓库能力和可验收指标合并为一份产品合同。

### 0.2 不直接照搬的内容

- 附件中的示例代码、图号、固定路径、固定图层名和示例数据不是产品规则。
- PDF/DWG 的文件名、楼层、比例、轴距、墙层名、Revit 模板和项目名称必须由输入配置或项目上下文提供。
- LLM、OCR、YOLO、颜色识别只能提供候选或语义辅助，不能绕过确定性几何门禁直接决定墙柱坐标。

### 0.3 产品一句话

BuildMate 将建筑项目中的图纸、规范、投标、采购和模型操作，转化为**有证据、可验证、可审批、可追溯、可回滚**的工程决策。

## 1. 产品目标与第一性原则

### 1.1 产品目标

1. 让工程人员能够在一个项目上下文中上传资料、发起工作流、查看过程证据并得到可下载的结果。
2. 让每一个审查结论、RAG 答案、模型构件和 Revit 操作都能回溯到真实来源。
3. 让自动化负责重复、确定、可验证的工作；让人工只在不可逆或高风险决策处确认。
4. 让本地离线 Demo 和生产部署使用同一业务合同，差别只体现在基础设施适配器。
5. 先以模块化单体交付可维护的垂直切片，再按必要性拆分 Revit Bridge、MCP 和 Worker。

### 1.2 不变量（任何版本都不能破坏）

| 编号 | 不变量 | 产品含义 |
|---|---|---|
| I-01 | 事实与判断分离 | 图纸、模型、规范和结构化业务数据是事实源；LLM 只能提出判断。 |
| I-02 | 结论必须有证据 | Finding、RAGAnswer、模型构件都要有可定位的 EvidenceRef 或 source_ref。 |
| I-03 | 几何由确定性代码控制 | 单位、坐标、几何、拓扑、规则、碰撞和事务安全不能由自然语言模型决定。 |
| I-04 | 不可逆动作需人工确认 | Revit 写入、采购审批、模型发布等动作必须经过持久化审批。 |
| I-05 | 状态必须持久化 | 任务、步骤、事件、审批、RAG 检索、Model IR 和 BuildRun 不得以进程内字典作为唯一事实源。 |
| I-06 | 全部请求带上下文 | 每次业务调用都携带 `tenant_id`、适用时的 `project_id`、操作者和 trace。 |
| I-07 | 失败显式反馈 | 解析、检索、LLM、MCP、队列和 Revit 失败必须呈现可行动错误，不得静默生成假数据。 |
| I-08 | 来源适配器隔离 | PDF/DWG 只在来源适配层分叉；下游 WallEvidence、WallModel、审查、Revit 和验收合同一致。 |
| I-09 | 独立验收 | 原图与 Revit 结果必须分别渲染再比较，禁止由 WallModel 反绘原图自证。 |

### 1.3 本期成功定义

用户从前端完成：登录 → 选择租户/项目 → 上传资料 → 启动工作流 → 查看进度 → 处理人工审核 → 下载结果。

对核心 BIM 链路，必须实现：

```text
PDF / DWG / DXF
→ 来源适配
→ SourceManifest
→ SourceEntities
→ WallEvidence
→ 坐标/轴网校正
→ WallModel（墙、柱、连接、构件信息）
→ 静态门禁
→ Dry-run
→ 人工批准
→ Revit 2020 Bridge
→ 轴网→墙柱建模
→ 读回验证与 Model Diff
→ 原图/实际 Revit 视图独立叠图
→ RVT、报告和证据交付
```

验收阈值：独立叠图 `edge_iou >= 0.95`、原图边缘覆盖率 `>= 0.95`、Revit 边缘精确率 `>= 0.95`。未达到阈值时任务不能标记为交付成功。

## 2. 产品范围

### 2.1 本期范围

1. 身份认证、租户、组织、项目和项目上下文。
2. Artifact 上传、校验、内容寻址存储、版本与下载。
3. 持久化任务、步骤、事件、取消、重试、恢复和人工审批。
4. RAG 知识库：规范、标准、制度、合同、投标文件、技术资料和供应商资料的入库、检索、引用和反馈。
5. 两个用户入口：BIM 独立入口负责图纸到模型交付；问答入口负责自然语言问答和意图路由。后台能力包括规范查询、标书查询、合同审核、采购/供应商分析，并统一接入任务、证据和审批运行时。
   问答入口支持单任务和复合任务预览；复合任务必须先展示步骤、输入范围、缺失参数和审批点，再由用户确认。
6. 通用 PDF/DWG/DXF 墙柱建模链路与独立叠图验收。
7. 问答、标书查询、合同审核、采购/供应商分析和统一证据展示；其中查询可在会话内完成，审核和审批类动作必须进入任务流程。
8. Revit 2020 Windows Bridge：静态校验、Dry-run、人工批准、事务写入、读回和回滚。
9. 管理员任务时间线、知识库和运维审计；独立审核端。
10. 本地双模运行和生产部署基础设施合同；生产先采用模块化单体 + 单 Worker/Bridge，出现真实并发后再横向扩展。

### 2.2 明确非目标

- 本期不实现进度计划 Agent、造价 Agent、施工方案 Agent。
- 本期不支持无人审批的 Revit 写入。
- 本期不建设远程 Revit Worker 集群。
- 本期不把所有模块一次性拆成微服务。
- 洞口识别不作为本期墙柱交付的阻塞条件；洞口语义和宿主边界保留扩展接口，出现时必须标记置信度和未处理状态，不得伪造已建模。
- 实时建材价格不进入 RAG 事实源，必须查询结构化 `material_prices`。
- Web 搜索结果不自动写入项目知识库。

### 2.3 平台化任务理解

问答入口不直接把整段会话交给某个业务 Agent，而是产生受控 `TaskPlan`：

```text
用户输入 → 任务理解 → 单/复合判断 → 任务拆解 → 领域路由 → 参数/审批确认
```

复合任务最多 20 步，每一步必须来自注册的领域能力。跨域传递使用 `TaskHandoffPackage`，只包含结构化事实、证据引用和风险摘要；不得传递完整对话、审批记录或未验证的 LLM 文本。

RAG 是平台共享能力，供规范、标书、合同、采购、谈判和 BIM 规范校核使用。每次调用都必须带租户/项目范围，并返回真实 `retrieval_run_id` 和可验证引用。

实现边界：当前支持规则式计划预览，工作台通过同一聊天流显示计划卡，并将预览保存在本域历史。用户确认后，后端可将复合计划持久化为父子任务和交接包；各领域仍独立执行审批与证据校验。完整验收状态见架构文档第 8 节。

## 3. 用户、角色与权限

### 3.1 用户角色

| 角色 | 目标 | 主要入口 | 可见范围 |
|---|---|---|---|
| `admin` 管理员 | 管理租户、用户、项目、知识、任务、审计和系统运行 | 管理台、任务时间线、知识库、审核中心 | 所属租户内全部资源；敏感操作需二次确认 |
| `reviewer` 审核员 | 审查 WallModel、规则结果、知识待补、采购和业务审批 | 审核中心、BIM 审批、知识待补 | 被授权项目的待审核项和证据；不能管理系统配置 |
| `project` 项目工程师 | 上传图纸、发起任务、查看项目结果、提交修订 | 项目看板、BIM、问答、历史 | 自己所属项目的资料、任务和结果；可提交审批但不能代替最终管理员规则 |
| `user` 普通用户 | 使用问答和被授权查询功能 | 问答、项目看板 | 按项目成员关系读取，不能审批或查看管理数据 |
| `bim` Bridge 服务身份 | 执行已批准的 Revit 操作并回传证据 | Bridge 协议 | 仅能访问被任务授权的工作副本和交接物，不能浏览租户业务数据 |

### 3.2 权限规则

- 每个资源读取、修改和下载都同时检查租户、项目、资源状态和角色。
- 路由层只构造 `RequestContext`，业务 SQL 由 Repository Scope 统一完成租户/项目过滤。
- 生产 PostgreSQL 使用 RLS 作为第二道租户防线。
- `reviewer` 只能处理状态为 `waiting_human` 且 `next_action` 与其权限匹配的审批。
- `admin` 可处理全部审批，但仍要记录实际操作者、原因和审批前后版本。
- `project` 可批准 WallModel（如果项目策略允许），不得直接批准 Revit 写入，除非租户策略明确授权。
- `bim` 服务身份不能创建审批、修改用户权限或跳过静态门禁。
- 所有拒绝返回统一 403/404 语义，不能通过错误信息泄露其他租户资源是否存在。

### 3.3 前端可见性

| 页面/功能 | admin | reviewer | project | user |
|---|---:|---:|---:|---:|
| 仪表盘 | ✓ | ✓ | ✓ | ✓ |
| 项目/文件 | ✓ | 受授权 | ✓ | 只读/受授权 |
| BIM 上传与查看 | ✓ | ✓ | ✓ | 只读 |
| BIM WallModel 审批 | ✓ | ✓ | 按项目策略 | — |
| Revit 写入审批 | ✓ | ✓ | 默认不可 | — |
| 审核中心 | ✓ | ✓ | — | — |
| 知识库管理/检索 | ✓ | 检索/待补 | 项目检索 | 项目检索 |
| 任务时间线 | ✓ | 只看关联任务 | 只看本人/项目任务 | — |
| 问答/规范查询/标书查询 | ✓ | ✓ | ✓ | 按授权 |
| 合同审核/采购/供应商工作流 | ✓ | ✓ | ✓ | 按授权 |
| 用户、组织、系统配置、审计 | ✓ | — | — | — |

前端导航必须按角色隐藏无权入口；后端仍必须再次校验，不能把隐藏菜单当作安全边界。

## 4. 核心业务场景

### 4.1 项目初始化

1. 管理员登录并创建租户/组织成员。
2. 管理员或项目工程师创建项目，录入项目编号、名称、默认单位、坐标原点、楼层和 Revit 工作模型策略。
3. 项目成员进入项目上下文；后续所有上传、检索、任务和审批自动携带项目 ID。
4. 系统生成项目审计起点事件。

### 4.2 资料入库与知识库

1. 用户选择项目和资料类型：规范、标准、制度、合同、投标、技术、供应商或图纸。
2. 系统校验扩展名、文件头、大小、空文件和病毒/路径安全，生成不可变 Artifact。
3. 知识资料进入 `KnowledgeIngestJob`：解析 → 清洗 → 语义分块 → 元数据和权限标注 → Embedding → Dense/BM25 索引。
4. 文档版本由内容哈希、解析器版本、分块版本和 Embedding 模型决定；重复入库必须幂等。
5. 用户检索后看到来源名称、版本、页码、段落和分数；低证据答案必须拒答或请求补充。
6. 低置信度问题进入知识待补队列，由审核员补充答案/文档，形成新版本并保留旧版本。

### 4.3 前端发起图纸到 Revit 交付（第一条端到端主线）

1. 项目工程师进入 BIM 页面，选择项目，上传一组同格式 PDF 或 DWG/DXF。
2. 前端要求填写或确认：来源角色、出图比例、楼层编码、坐标原点、Revit 工作模型路径/策略、墙柱材料和是否执行 Bridge。
3. 系统创建 Artifact 和 `wall_pipeline` Task，前端通过任务 API/SSE 显示状态；不能因页面刷新丢失任务。
4. Worker 生成六类可追溯产物：`source_manifest.json`、`source_entities.json`、`wall_evidence.json`、`wall_model.json`、`revit_result.json`、`audit_report.json`，另有原图渲染和实际 Revit 视图。
5. 确定性门禁检查墙/柱是否存在、诊断是否有错误、轴网是否满足策略、坐标/单位是否完整。门禁失败时停在 `fix_source_or_rules`，不允许审批空模型。
6. 门禁通过后暂停在 `approve_wall_model`；审核员/管理员查看数量、缩略图、证据、坐标、墙厚、墙柱连接、构件属性和警告后批准或驳回。
7. 批准后生成 Revit 静态 Dry-run：检查模板、楼层、墙类型、柱族、参数绑定、事务计划、碰撞和预计数量。
8. Dry-run 后再次暂停在 `approve_revit_write`；审批人确认将要写入的工作副本和差异。
9. Bridge 对 Revit 2020 工作副本按“轴网 → 墙体 → 柱/属性 → 读回”的顺序执行；每轮有 Transaction、失败 Rollback 和差异快照，最多收敛 5 轮。
10. Worker 读取实际 Revit 结果，独立渲染平面视图，与原始 PDF/DWG 独立渲染叠图。
11. 只有独立验收达到阈值且构件读回数量/参数一致，Task 才进入 `succeeded/delivery_complete`，前端展示 RVT、报告、叠图和下载链接。
12. 任一阶段失败都显示具体阶段、可重试性、错误和产物位置；不得显示“成功”或伪造零错误结果。

### 4.4 图纸审查

审查采用双链路：

```text
图纸感知 → Geometry Gate → 硬规则 → RAG 规范/项目标准软审查 → 报告合并 → 人工确认
```

- 硬规则负责几何、尺寸、连接、坐标和可计算的规范条件。
- RAG 负责引用规范条文、项目手册、设计说明和合同约束。
- 每个 Finding 必须展示严重级别、规则、描述、构件/位置、证据和建议。
- “未识别”“无资料”“证据不足”和“通过”是四种不同状态。

### 4.5 问答入口与意图路由

问答和 BIM 是两个独立的用户入口。问答入口接收自然语言，但只把请求分发到受控能力，不直接执行任意工具。

| 意图 | 调用能力 | 结果形态 | 是否需要人工审核 |
|---|---|---|---|
| `building_standard_query` 建筑规范查询 | RAG 规范索引 | 带版本、条文/页码和置信度的答案 | 否；证据不足时拒答 |
| `bid_query` 标书查询 | 项目标书索引 + RAG | 原文定位、摘要、对比和引用 | 否；生成正式报告时需确认 |
| `contract_review` 合同审核 | RAG + 条款结构化 + 确定性规则 | Finding、风险等级、证据和报告 | 是，高风险必须审核 |
| `procurement_query` 采购/供应商查询 | RAG + 结构化数据 + 确定性规则 | 事实、依据和风险摘要 | 视风险策略 |
| `bim_command` BIM 操作请求 | 跳转 BIM 入口并预填任务 | BIM 任务配置页和持久化任务 | 按 BIM 两次审批链路 |
| `general_question` 一般问答 | 项目知识 RAG | 可引用答案或拒答 | 否 |

处理流程为：提取意图和参数 → 校验租户/项目/角色 → 展示缺失参数或执行预览 → 调用白名单 Workflow → 返回可读结果和证据。意图置信度不足时，系统必须澄清，不得猜测。

### 4.6 建筑规范与标书查询

- 规范查询优先检索已批准的标准、规范和项目制度版本；答案必须显示来源、版本、页码和适用范围。
- 标书查询只能检索当前项目授权文件，可按章节、关键词、条款和响应项定位；不同版本并列展示，不覆盖历史文件。
- 查询结果是即时对话结果，不自动产生审批结论；用户要求形成正式审查报告时，升级为持久化任务。

### 4.7 合同审核 Workflow

- 用户从问答中提出“审核这份合同”或在审核端选择合同，系统创建合同审核任务。
- 规则引擎先检查期限、金额、付款、违约、责任、范围和版本等可计算事实；RAG 提供制度和合同依据；LLM 只负责风险归纳和可读解释。
- 每条 Finding 必须引用原文件页码/段落和规则版本；高风险、证据不足或版本冲突进入审核端。
- 审核结果、修改建议和最终确认均持久化，不能以聊天消息代替正式审批。

### 4.8 采购与供应商 Workflow

- 采购申请和谈判准备作为后台 Workflow，通过问答意图或审核端触发。
- 金额、数量、供应商、预算和价格是结构化事实；制度、合同和资质资料由 RAG 提供依据。
- 采购审批、供应商承诺和正式纪要仍需按角色和风险策略人工确认。

## 5. Agent 产品合同

### 5.1 用户入口与意图路由边界

当前 保留两个独立用户入口：

- **BIM 入口**：面向图纸到 Revit 交付，使用表单和步骤条，保证不熟悉自然语言的用户也能完成建模。
- **问答入口**：面向规范、标书、合同和项目资料的自然语言交互，由意图识别层调用受控 Workflow。

问答入口的编排助手负责：意图识别、参数提取、工作流选择、证据组织、自然语言解释和下一步建议。
确定性引擎负责：解析、几何、单位、坐标、规则、模型编译、碰撞、回读和独立比较。

意图识别只允许返回注册的路由，不得生成任意工具名或任意代码。合同审核、采购审批和正式报告生成必须创建持久化任务；规范/标书查询可以在会话内即时返回。

### 5.2 BIM Agent 定义

BIM Agent 是面向用户的单一业务 Agent，目标是完成“图纸到可验收模型交付”。它负责：

- 接收项目、楼层、比例、坐标、材料和 Revit 工作模型配置。
- 按固定顺序编排来源适配、几何识别、Model IR、门禁、审查、审批、Dry-run、Revit 写入、读回和独立叠图。
- 汇总每个阶段的证据、风险、状态、审批动作和最终交付物。
- 在门禁失败、证据不足或 Revit 失败时给出可行动的下一步，而不是生成假成功。

BIM Agent 不能直接猜测墙柱坐标、自由生成 Revit 脚本、跳过人工审批或把中间 JSON 当作最终模型。它只能调用经过 ACL 和 Schema 校验的确定性工具：PDF/DWG 适配器、坐标/轴网引擎、WallEvidence/Model IR 校验器、碰撞与拓扑门禁、Revit Bridge、读回校验器和独立叠图审计器。

内部 B1~B5 阶段属于 BIM Agent 的执行步骤，不是独立 Agent、独立权限或独立前端任务。

### 5.3 意图路由输出

```json
{
  "intent": "building_standard_query|bid_query|contract_review|procurement_query|bim_command|general_question",
  "confidence": 0.0,
  "parameters": {
    "artifact_ids": [],
    "project_id": null,
    "document_type": null,
    "query": ""
  },
  "route": "rag.answer|bid.search|workflow.bid_review|workflow.contract_review|workflow.procurement|workflow.wall_pipeline|clarify",
  "requires_confirmation": false,
  "missing_parameters": []
}
```

`route` 必须经过服务端白名单和角色 ACL 校验；置信度不足、项目上下文缺失或参数不完整时只允许 `clarify`，不能执行副作用动作。

### 5.4 统一任务输入

```json
{
  "task_id": "task_xxx",
  "tenant_id": "tenant_xxx",
  "project_id": "project_xxx",
  "actor_id": "user_xxx",
  "workflow": "wall_pipeline",
  "input_artifact_ids": ["artifact_xxx"],
  "idempotency_key": "client-generated-key",
  "correlation_id": "corr_xxx",
  "schema_version": "task-envelope/2.0"
}
```

### 5.5 统一 Agent 输出

```json
{
  "status": "succeeded|waiting_human|failed",
  "answer": "用户可读的结论",
  "structured_output": {},
  "findings": [],
  "artifact_ids": [],
  "evidence_refs": [],
  "confidence": 0.0,
  "next_action": "approve|clarify|retry|reject|none",
  "fallback_used": false
}
```

所有结构化 LLM 输出必须由 Pydantic 校验；连续两次非法输出后任务失败并显示错误，不得把原文传给下一步。

### 5.6 运行限制

- 单任务最多 20 步、12 次工具调用。
- 每个外部调用配置超时、有限重试、熔断和可观察错误。
- 消息采用至少一次投递；消费者按任务/幂等键安全重复消费。
- 每一步在调用外部服务前后分别落库输入、输出、耗时、尝试次数和错误。

## 6. BIM Agent 内部确定性能力要求（全项目中的核心垂直切片）

### 6.1 来源适配

| 来源 | 处理方式 | 失败行为 |
|---|---|---|
| PDF | PyMuPDF 读取矢量路径/文字并渲染原图；扫描页按策略进入 OCR | 无有效矢量/文本且未启用 OCR 时明确停止 |
| DWG | ODA File Converter 转 DXF，处理版本、代理对象和 XREF | ODA 未配置、转换失败或输出为空时明确停止 |
| DXF | ezdxf 读取 LINE、LWPOLYLINE、POLYLINE、MLINE、INSERT、HATCH 边界 | 文件损坏或实体超过上限时明确失败并给出限制 |
| IFC/图片 | 不属于 BIM Agent 的墙体主链路 | 作为独立交换/读取能力处理，不进入 WallEvidence→WallModel |

### 6.2 统一产物链

```text
source_manifest.json
→ source_entities.json
→ wall_evidence.json
→ wall_model.json
→ revit_result.json
→ audit_report.json
```

所有产物不可变、带 SHA-256 和上游哈希。换一份 PDF/DWG 只改变配置和输入 Artifact，不改变下游 JSON 合同和核心算法。

### 6.3 WallEvidence / WallModel

WallEvidence 必须包含来源文件、frame、页码/实体定位、几何证据、图层/样式、单位和置信度。WallModel 至少包含：

- 墙体：起终点、中心线/边界、厚度、标高、证据、拓扑、构件类型。
- 柱：矩形或异形轮廓、中心/顶点、标高、来源、置信度和类型标记。
- 连梁：起终点、宽/高、楼层归属、梁顶/梁底实际标高、标高来源/状态和证据引用；
  只解析到一侧时由已核定梁高确定性补齐另一侧，缺失或冲突必须显式提示。
- 轴网：轴号、方向、位置、来源和坐标变换。
- 连接点：T/L/Z 拓扑关系、宿主构件、连接状态。
- 构件信息：类别、类型名、类型标记、材料、材料状态、分类码、数量基础、长度/面积/体积、来源引用。

图纸图例、构件表、墙/柱表和大样中的材料、强度、墙厚、柱截面、构件标号等信息必须写入构件实例参数或类型参数，不能只生成说明文字或独立明细表。规格解析按大样/详图、表格、图例、普通标注的优先级进行，并保存原文和证据；几何测量值只做校验，不能通过四舍五入伪造规格。没有规格证据时明确标记 `unresolved`，证据与几何冲突时标记 `conflict` 并转人工审核。洞口本期不阻塞墙柱交付；识别到时作为语义标注和待处理项保存。

### 6.4 确定性几何规则

- PDF/DWG 不按图号、文件名或当前项目固定边界分支。
- 统一毫米制输出，记录完整 transform chain、原点和比例。
- INSERT 递归展开；多套轴网分区；旋转图纸精确扶正。
- 柱位置不强制吸附轴线；墙/梁可依据柱边和轴网约束。
- 同一墙体多条证据去重；墙厚由闭合条带/双线距离和配置容差决定。
- 墙体自动分段并审计断墙；T/L/Z 节点必须有连接关系和连接门禁。
- 异形柱按闭合多边形保留轮廓，不退化为矩形占位。
- 墙柱碰撞、重复实体和重叠区进入静态报告；有未处理碰撞时禁止写入。

### 6.5 Revit 2020 交付

- Bridge 默认监听 `127.0.0.1:8005`，仅接受已批准、签名/哈希匹配的 WallModel 和 Dry-run 交接物。
- 使用 Revit API/pyRevit；先创建轴网，再创建墙体、柱和参数。
- 在 Revit 2020 中使用兼容的共享参数文件格式；参数写入构件信息，不依赖用户手工建明细表。
- 每个墙/柱至少写入：`BM_ElementId`、`BM_Category`、`BM_TypeName`、`BM_TypeMark`、`BM_Material`、`BM_MaterialStatus`、`BM_Classification`、`BM_ClassificationSystem`、`BM_StandardProfile`、`BM_Units`、`BM_CoordinateFrame`、`BM_Level`、几何尺寸、数量、来源引用和置信度。
- 实际写入只发生在隔离工作副本；事务失败自动 Rollback；成功后读回元素 ID、类别、类型、参数、数量和视图。
- 输出最终 RVT、实际平面视图、Model Diff、独立叠图和审计报告；不得覆盖用户原始模型。

## 7. RAG 产品要求

### 7.1 数据边界

进入 RAG：法规、规范、标准、企业制度、项目手册、投标文件、合同、技术文档、供应商资质与历史资料。
不进入 RAG：实时材料价格、图纸几何、Model IR、Revit 当前状态和未经确认的 Web 结果。

### 7.2 文档生命周期

```text
登记 → 文件校验 → 解析/OCR → 清洗 → 语义分块 → 权限标注
→ Embedding → Dense + BM25 → 可选 Reranker → 可检索
→ 反馈/过期 → 新版本替换（旧版本只读保留）
```

文档版本由以下组合判定：

```text
content_hash + parser_version + chunking_version + embedding_model
```

### 7.3 检索与回答

检索请求必须带 `tenant_id`、`project_id`、`scope`、文档类型、专业和版本过滤。默认 Dense 0.7 + BM25 0.3，Reranker 可用则覆盖排序；Reranker 不可用原因写入 `fallback_used` 和命中元数据。

回答必须返回：

```json
{
  "answer": "...",
  "citations": [{"chunk_id":"...", "document_id":"...", "source_name":"...", "page_no":12}],
  "confidence": 0.91,
  "grounded": true,
  "abstained": false,
  "retrieval_run_id": "retrieval_xxx"
}
```

引用 ID、页码和来源必须能对应本次真实 `KnowledgeHit`；无效引用视为非法输出。低置信度回答要拒答或进入知识待补队列。

### 7.4 RAG 与意图路由能力

- 问答入口：规范、制度、项目知识和一般问题的引用式回答。
- 建筑规范查询：使用标准/规范索引，按专业、版本和适用范围过滤。
- 标书查询：使用当前项目标书索引，支持定位、摘要和版本对比。
- 合同审核：RAG 提供条款依据，规则引擎和审核 Workflow 负责正式结论。
- 采购/供应商：制度、合同、资质资料走 RAG；实时价格走结构化查询。
- 图纸审查/BIM：RAG 只提供软审查依据，几何、坐标、构件和模型状态仍使用确定性接口。

## 8. 前端产品需求

### 8.1 信息架构

1. 登录/刷新令牌/退出。
2. 仪表盘：项目上下文、待办、最近任务和成本/健康摘要。
3. 项目：项目列表、成员、楼层、坐标、默认模板和 Artifact。
4. BIM：独立上传、配置、实时状态、WallModel 审核、Dry-run 审批、交付下载、独立叠图。
5. 问答：会话、意图识别、引用、置信度、拒答和反馈。
6. 标书查询：项目文件检索、定位、摘要、版本对比和可选正式报告。
7. 合同审核：合同上传/选择、条款 Finding、风险、证据和审核流转。
8. 采购/供应商：申请、规则检查、证据、审批和谈判准备。
9. 审核中心（管理员/审核员）：待审核任务、知识待补、审批详情、通过/驳回/退回补件。
10. 任务时间线（管理员）：全量任务、步骤、事件、重试、取消和失败原因。
11. 知识库（管理员）：文档登记、索引状态、版本、检索测试和反馈。
12. 历史：按项目、任务、Artifact 和审批搜索与回放。

### 8.2 BIM 页面交互

- 上传限制 `.pdf,.dwg,.dxf`，同一任务不得混合 PDF 与 CAD 文件族。
- 必填字段：项目、楼层编码、Revit 工作模型策略/路径、墙柱材料、PDF 比例（PDF 时）、坐标原点。
- 提交前展示配置摘要和风险提示；提交后返回任务 ID。
- 任务卡展示状态、阶段、墙数、柱数、连接点、洞口待处理数、门禁结果、步骤表和产物下载。
- 审批详情必须同时展示用户可读摘要和可展开的 JSON 原文，不要求用户阅读原始 JSON 才能判断。
- 叠图显示原图边缘、Revit 边缘、未覆盖区、额外边缘和指标；明确“独立渲染”。
- Revit 交付显示工作副本路径、版本、读回数量、构件参数写入数量和回滚状态。

### 8.3 通用前端状态

Pinia 按领域拆分：`auth`、`tenant/project`、`artifact`、`task`、`review`、`modeling`、`chat`、`knowledge`。所有长任务支持 SSE（`Last-Event-ID`）和轮询兜底；刷新页面后用任务 ID 恢复视图。

## 9. API 产品合同

### 9.1 核心 API

```text
POST   /api/v2/auth/login
POST   /api/v2/auth/refresh
POST   /api/v2/auth/logout
GET    /api/v2/me

POST   /api/v2/tenants/{id}/members
GET    /api/v2/projects
POST   /api/v2/projects
GET    /api/v2/projects/{id}
POST   /api/v2/projects/{id}/levels

POST   /api/v2/artifacts
GET    /api/v2/artifacts/{id}
GET    /api/v2/artifacts/{id}/download

POST   /api/v2/knowledge/documents
POST   /api/v2/knowledge/ingest
POST   /api/v2/knowledge/search
GET    /api/v2/knowledge/retrieval-runs/{id}
POST   /api/v2/knowledge/feedback

POST   /api/v2/workflows
GET    /api/v2/tasks/{task_id}
GET    /api/v2/tasks/{task_id}/events
POST   /api/v2/tasks/{task_id}/cancel
POST   /api/v2/tasks/{task_id}/resume

GET    /api/v2/reviews/{review_id}
POST   /api/v2/reviews/{review_id}/decision
POST   /api/v2/builds
GET    /api/v2/builds/{build_id}
POST   /api/v2/builds/{build_id}/approve
GET    /api/v2/builds/{build_id}/diff

POST   /api/v2/chat/sessions
POST   /api/v2/chat/sessions/{id}/messages
GET    /api/v2/chat/sessions/{id}/events
POST   /api/v2/chat/intents/preview
```

### 9.2 通用请求头与响应

- `Authorization: Bearer <token>`。
- `Idempotency-Key`：创建 Artifact、Workflow、审批和恢复请求必须支持。
- `X-Trace-Id` / `X-Correlation-Id`：缺失时服务端生成并在响应返回。
- SSE 使用 `Last-Event-ID` 断点续传；断线后自动轮询。
- 错误结构：

```json
{
  "error": {
    "code": "GEOMETRY_GATE_FAILED",
    "message": "确定性门禁未通过",
    "details": [{"field":"grid_present","reason":"未识别轴网"}],
    "retryable": false,
    "trace_id": "trace_xxx"
  }
}
```

禁止向前端返回内部堆栈、密码、Token、完整 LLM 原始密钥或其他租户数据。

## 10. 数据与状态要求

### 10.1 领域实体

至少包含：`Tenant`、`User`、`TenantMembership`、`Project`、`Artifact`、`WorkflowRun`、`WorkflowStep`、`TaskEvent`、`ReviewRun`、`ReviewFinding`、`ModelIRVersion`、`BuildRun`、`Approval`、`ToolCall`、`KnowledgeDocument`、`KnowledgeChunk`、`KnowledgeIngestJob`、`RetrievalRun`、`KnowledgeFeedback`、`LLMCall`、`OutboxEvent`。

所有租户级表统一带：

```text
tenant_id, project_id（适用时）, created_by,
created_at, updated_at, version
```

### 10.2 任务状态机

```text
queued → running → waiting_human → resumed → succeeded
                                  ↘ failed / canceled
```

审批动作由服务端根据持久化 `next_action` 推导，客户端不能自行指定“批准哪一步”。拒绝必须留下原因并把任务置为取消或修订待提交。

### 10.3 Model IR

```json
{
  "schema_version": "model-ir/2.0",
  "project": {},
  "units": "m",
  "coordinate_system": {},
  "transform_chain": [],
  "levels": [],
  "grid": [],
  "model_elements": [],
  "provenance": {}
}
```

每个构件必须包含 `element_id`、`type`、`geometry`、`properties`、`source_refs`、`confidence`、`review_status`。同一文件哈希、解析器版本和 Pipeline 版本必须得到可复现的 IR。

## 11. 任务编排与基础设施

### 11.1 本地模式

- SQLite、项目本地文件、SQLite/本地向量检索、数据库 Task Runner、Mock LLM。
- 可选 Revit Bridge；没有 Revit 时可以跑到交接物和 Dry-run，但不能伪造最终 RVT 交付。
- 支持演示数据重置，但必须有非生产环境保护和明确提示。

### 11.2 生产模式

- PostgreSQL（RLS）、RabbitMQ、Redis、MinIO/S3、Milvus。
- Prometheus、Grafana、Loki、OpenTelemetry。
- Windows Revit 2020 Bridge。

RabbitMQ 拓扑：

```text
exchange: buildmate.tasks
routes: review.run, modeling.run, artifact.process, hitl.resume, agent.*.run
失败 → retry queue → dead-letter queue
```

### 11.3 可靠性

- Outbox 事件与任务创建同事务；领取使用 lease generation/CAS，旧回执不能覆盖新领取状态。
- Worker 重启后从 `queued/running` 可恢复或明确转失败，不依赖内存队列。
- 外部依赖按接口设置连接/读取超时、有限重试和熔断。
- 上传、任务、审批和 Bridge 操作都需要幂等键或任务版本。

## 12. 安全、合规与审计

- JWT 登录/刷新/退出；密码不进入日志。
- RBAC + 项目成员关系 + Repository Scope + 生产 RLS。
- Artifact 下载使用权限检查和短时授权，不暴露本地任意路径。
- MCP Tool ACL 按角色、工作流和项目授权；工具参数用 Pydantic 校验。
- Revit 脚本 AST 白名单：禁止任意文件、进程、网络、动态执行；只读副本、Transaction、Dry-run、人工批准、Rollback。
- 每个任务事件记录操作者、角色、租户/项目、时间、trace、输入/输出哈希、状态变化和错误。
- 业务日志不记录 Token、密码、完整 Prompt 中的敏感资料和原始文件内容。

## 13. 可观测性与运营指标

### 13.1 系统指标

- API P50/P95 延迟、4xx/5xx、上传失败率。
- 队列长度、任务等待时间、步骤耗时、重试和死信数。
- LLM token、费用、超时、结构化输出失败率。
- RAG Recall@K、MRR、nDCG、命中率、拒答率、引用覆盖率和答案有据率。
- PDF/DWG 解析失败率、Geometry Gate 失败率、墙柱碰撞数、重复率和拓扑断点数。
- HITL 等待时长、审批通过/驳回率。
- Bridge 可用性、事务回滚率、读回差异数、独立叠图指标。

### 13.2 用户价值指标

- 从上传到可读报告的完成率。
- 从图纸到可验收 RVT 的完成率。
- 人工修改次数和重复任务率。
- 每个结论可定位证据的比例。
- 用户从前端恢复任务和下载交付物的成功率。

## 14. 研发交付计划

每个里程碑都必须可独立回滚、可测试、可演示；完成后更新代码、文档、测试数量和实际运行状态。

| 阶段 | 交付内容 | 退出条件 |
|---|---|---|
| M2 数据与隔离 | Repository Scope、SQLite/PostgreSQL、RLS、乐观锁、Outbox | 跨租户测试全部拒绝，重启可恢复任务 |
| M3 RAG | 文档登记、解析、分块、混合检索、引用校验、反馈 | 黄金问题集达到目标 Recall 和有据率 |
| M4 Runtime | 本地 Runner、Rabbit Adapter、任务事件、重试、取消、HITL | 任务可暂停/恢复/失败/重启恢复 |
| M5 BIM 感知 | PDF/DWG/DXF 适配、WallEvidence、轴网、墙柱、拓扑、参数 | 换输入配置不改核心代码，WallModel 可复现 |
| M6 Review | 硬规则、RAG 软审查、证据报告、审核端审批 | 每个 finding 有证据，门禁失败不能建模 |
| M7 Revit | Revit 2020 Bridge、Dry-run、写入、读回、回滚、独立叠图 | 独立指标 ≥95%，参数回读一致 |
| M8 意图路由 | 问答入口接入规范查询、标书查询、合同审核和采购/供应商 Workflow | 路由白名单、参数补全、权限和证据一致 |
| M9 当前 API | 稳定 OpenAPI、幂等、SSE、轮询、错误合同 | BIM/项目/RAG/任务/审核/建模前端只依赖当前接口；存量业务页暂由兼容层承载并禁止新增兼容功能 |
| M10 前端 | 角色导航、项目上下文、任务时间线、审核中心、各业务页面 | 页面刷新不丢任务，关键链路可用 |
| M11 生产化 | Compose、队列、对象存储、向量库、监控、备份 | 本地/生产双模启动和健康检查通过 |

## 15. 测试策略

### 15.1 单元与合同测试

- Pydantic/JSON Schema：非法单位、坐标、引用、任务状态和 LLM 输出拒绝。
- Repository：租户/项目 Scope、版本、幂等、乐观锁和 RLS。
- Geometry：单位换算、旋转、去重、墙厚、断墙、T/L/Z、异形柱和碰撞。
- RAG：重复入库、版本切换、Dense/BM25/Reranker、降级标记、引用校验和低置信度拒答。
- API：认证、角色、幂等、错误映射、SSE 断点和轮询兜底。

### 15.2 业务集成测试

- PDF → SourceManifest → WallEvidence → WallModel。
- DWG → ODA/DXF → 同一 WallModel 合同。
- WallModel → Review → HITL → Dry-run。
- 已批准 IR → Revit 2020 Bridge → 轴网 → 墙柱 → 参数 → 读回。
- Revit 失败 → Rollback；服务重启 → 任务恢复；消息重复 → 不重复建模。
- 非法 LLM 输出、MCP 超时、Reranker 不可用、PDF 实体超限均必须显式失败。
- 跨租户 Artifact、任务、知识、审批和下载全部拒绝。

### 15.3 独立叠图验收

验收输入必须是：

1. 原始 PDF/DWG 经过独立渲染得到的源图边缘。
2. Revit 实际工作副本/实际视图经过独立导出得到的模型边缘。

禁止使用 WallModel 反绘源图。验收报告至少包含：注册方式、边缘容差、IoU、源覆盖率、Revit 精确率、相似度、源图和实际视图哈希、错误列表。任一指标低于 0.95，任务状态必须为失败或待修订，不能交付成功。

### 15.4 最终验收清单

- [ ] 后端全量测试 0 失败，跳过项有原因。
- [ ] 前端 lint/typecheck/build 通过。
- [ ] 本地模式不启动 Docker 即可跑核心 Demo。
- [ ] 生产 Compose 可启动数据库、队列、缓存、对象存储、向量库和监控。
- [ ] 前端完成一次真实：登录 → 上传 → 任务 → 两次审批 → Revit 2020 写入 → 回读 → 独立叠图 → 下载。
- [ ] 换另一张 PDF/DWG 只改变输入配置和文件清单，核心代码不改。
- [ ] 墙柱、轴网、T/L/Z 连接、异形柱和构件参数可追溯。
- [ ] 构件信息写入 Revit 参数，不能只输出明细表。
- [ ] 每个审查结论、RAG 答案和模型构件均有真实证据引用。
- [ ] Revit 原模型不被覆盖；失败能回滚或明确失败。
- [ ] 文档、代码、测试数量和实际运行状态一致。

## 16. 交付物目录

| 交付物 | 位置/形式 | 责任 |
|---|---|---|
| 本 PRD | `docs/PRD.md` | 产品/研发共同基线 |
| 全栈总体设计 | `docs/DESIGN.md` | 后端/前端/平台 |
| 数据库设计 | `docs/DATABASE_DESIGN.md` | 后端/数据 |
| UI / UE 设计规范 | `docs/UI_UX_DESIGN.md` | 产品/前端 |
| 功能模块设计 | `docs/design/modules/` | 各模块负责人 |
| API/事件/Agent/RAG 合同 | `docs/contracts/` | 后端/前端 |
| 开发约束 | 根目录及模块 `AGENTS.md` | 全体研发 |
| BIM 细节 | `docs/WALL_PIPELINE.md` | BIM/几何/Revit |
| 当前 API OpenAPI | 运行时 `/docs` 与版本化文件 | 后端 |
| 运营手册 | `docs/OPERATIONS.md` | 平台/运维 |

## 17. 当前实现状态说明

当前仓库已经具备一部分可运行能力，包括 当前 API、任务持久化、RAG 基础链路、BIM 墙柱流水线、Revit 2020 Bridge、BIM/问答双入口和意图预览接口；这些是实现基线，不等同于全部 PRD 已验收。后续每次发布必须在 `docs/CURRENT_STATUS.md` 中记录：真实运行的前端任务 ID、代码版本、构件数量、参数写入数量、独立叠图指标、已知限制和未运行的检查。

任何“成功”状态必须同时满足：任务持久化成功、实际交付物存在、Bridge 读回成功、独立叠图通过、前端可见并可下载。仅生成 JSON、仅 API 成功、仅 Dry-run 成功或仅看到审批按钮，都不能称为最终交付。
