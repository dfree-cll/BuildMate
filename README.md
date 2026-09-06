# BuildMate

面向建筑行业的 AI 协作平台。系统保留 BIM 独立入口，同时提供独立问答入口；问答可通过受控意图路由调用建筑规范查询、标书查询、合同审核和采购/供应商 Workflow。BIM 主交付链路是 `PDF/DWG/DXF → WallEvidence → WallModel → 人工审批 → Revit 2020 → 独立叠图`。

## 能力概览

Agent 记忆已统一接入：问答历史、投标版本、采购结果、谈判阶段和 BIM 设置/续建关联。各页面可恢复历史或新建会话，记忆按账号、租户、项目和 Agent 隔离；用法与边界见 [Agent 记忆说明](docs/AGENT_MEMORY.md)。升级由启动时的 `0011_agent_memory` 迁移完成。

- **智能工作台与平台化任务控制**：SSE 流式问答，支持 `TaskPlan` 单/复合任务预览、注册领域路由和 `TaskHandoffPackage`；投标、采购、谈判在同一工作台中打开独立业务面板，补齐参数并确认后调用既有接口。合同预审通过受控 Workflow 执行，证据不足时转人工审核。
- **AEC 建模链路**：文档解析 → 图纸感知 → Model IR → BIM 编译与校验 → Windows Revit Worker。
- **通用墙体链路**：PDF/DWG adapter → WallEvidence → 确定性几何/拓扑 → 人工批准 → Revit → 独立叠图。
- **BIM 泛化设计**：通用内核、专业构件插件、来源适配器和目标编译器的演进边界见 [BIM 泛化设计](docs/BIM_GENERALIZATION.md)。
- **企业基础能力**：JWT、角色控制、租户字段、限流、重试、熔断、会话记忆与 LLM 调用统计。
- **可选基础设施**：生产可使用 PostgreSQL、RabbitMQ、Redis、MinIO/S3、Milvus；未配置时可用 SQLite 和本地向量能力运行开发模式。

## 目录结构

~~~text
BuildMateDemo/
├─ frontend/                 Vue 3 体验层
├─ backend/
│  ├─ api/                   API 与安全边界（当前业务合同与兼容入口）
│  ├─ domain/                领域合同、状态机、Model IR
│  ├─ application/           任务、制品和工作流用例
│  ├─ ports/                 Artifact 存储边界（其余适配器由应用服务直接装配）
│  ├─ adapters/              DB、Outbox、RabbitMQ、本地/S3 实现
│  ├─ rag/                   文档入库、混合检索、引用与评估
│  ├─ agents/                BIM Agent 与问答路由后的受控业务能力
│  ├─ core/                  编排、LLM、记忆、重试、熔断与可观测性
│  ├─ engines/               AEC 领域引擎：解析、Model IR、编译、校验
│  ├─ mcp/                   MCP Gateway 与工具客户端
│  └─ db/                    数据库模型、会话与方言适配
├─ workers/
│  ├─ revit_mcp/             Windows Revit MCP Worker
│  ├─ revit_bridge/          Loopback 安全 Bridge（AST/Dry-run/审批）
│  └─ pyrevit/               Revit 内执行的 pyRevit 脚本主副本
├─ scripts/                  当前初始化、运行、合同导出与 Revit 同步命令
├─ data/                     知识、已批准证据、Revit 基准与运行时数据
├─ migrations/               Alembic 数据库迁移
├─ docs/                     架构、协议和部署文档
├─ requirements.txt          唯一 Python 运行依赖清单（后端与 Worker 共用）
└─ .env.example              唯一环境变量模板
~~~

架构总览（六层、两入口、三条主链）见 [架构总览](docs/ARCHITECTURE.md)；任务理解、交接与共享 RAG 见 [技术设计](docs/TECH_DESIGN.md)；技术选型、数据库和模块索引见 [全栈总体设计](docs/DESIGN.md)。

## 本地开发

要求：Python 3.11+、Node.js 20+。Docker 和 Revit 均为可选能力。

~~~powershell
# 1. 配置环境（不填 LLM_API_KEY 即使用离线 Mock 模式）
Copy-Item .env.example .env

# 2. 安装后端依赖
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt

# 3. 初始化本地数据库与知识库
.\.venv\Scripts\python scripts/init_db.py
.\.venv\Scripts\python scripts/seed_knowledge.py

# 4. 启动后端
.\.venv\Scripts\python -m uvicorn backend.main:app --reload --port 8000
~~~

另开一个终端启动前端：

~~~powershell
cd frontend
npm ci
npm run dev
~~~

访问 <http://localhost:3000>。开发账户由初始化脚本创建；如需调整请查看 scripts/init_db.py。

### Windows 一键启动

在项目根目录双击 `start_project.bat`，或在 PowerShell 执行：

~~~powershell
.\start_project.bat
~~~

该入口使用本地 SQLite 和 Local Runner，不会修改 `.env`；LLM/Embedding Key 为空时走离线能力，已配置时使用真实服务。启动时会先验证并刷新本项目的后端、两个 MCP 服务和 Revit Bridge；如果本项目的 Vite 已在 `:3000` 运行，会保留该前端进程和浏览器会话，不因后端改动而刷新前端。其他程序占用这些端口时会明确报错且不会被终止。数据库、日志、上传和任务产物统一位于 `data/runtime` 或被 Git 忽略的数据目录，不再污染项目根目录。首次需要重建知识库时可执行 `.\start_project.bat -SeedKnowledge`。

## 前端角色

| 角色 | 额外可见页面 |
|---|---|
| `admin` | 项目知识库、任务时间线、审核中心 |
| `reviewer` | 审核中心 |
| 其他项目角色 | 不显示上述管理页面 |

普通业务入口固定为“BIM Agent”和“智能工作台（QA）”。工作台内保留问答、投标审查、采购审批和供应商谈判面板；业务会话、文件和审批仍分别隔离。旧投标/采购/谈判地址会进入工作台对应面板，BIM 请求仍跳转独立建模页面。管理员知识库、任务时间线和审核端保留角色权限。

BIM 页面内的任务进度和 WallModel/Revit 审批是建模主链路的一部分，不受独立“任务时间线”管理页面隐藏影响。

## 配置

项目只保留一个配置模板：[.env.example](.env.example)。常用项：

| 场景 | 配置项 |
|---|---|
| 离线开发 | 保持 LLM_API_KEY 与 EMBEDDING_API_KEY 为空 |
| 真实 LLM | LLM_API_KEY、LLM_BASE_URL、LLM_MODEL |
| PostgreSQL / Milvus | DATABASE_URL、VECTOR_BACKEND、MILVUS_HOST、MILVUS_PORT |
| RabbitMQ / S3 | TASK_QUEUE_BACKEND、RABBITMQ_URL、ARTIFACT_STORAGE_BACKEND、S3_* |
| Revit Bridge | REVIT_BRIDGE_URL、REVIT_BRIDGE_EXECUTION_ENABLED、REVIT_BRIDGE_APPROVAL_SECRET、WALL_PIPELINE_REVIT_WRITE_TIMEOUT_SECONDS |
| 多实例 | REDIS_URL、强随机 JWT_SECRET、生产 CORS_ORIGINS |
| PDF/DWG / Revit | 墙体 YAML 配置、BIM_JSON_IN、REVIT_OUTPUT_DIR；DWG 配置 ODA_FILE_CONVERTER |

通用 PDF/DWG 墙体流水线的配置、六阶段 JSON 和审核命令见 [docs/WALL_PIPELINE.md](docs/WALL_PIPELINE.md)，输入模板见 [config/wall_pipeline.example.yaml](config/wall_pipeline.example.yaml)。

.env 仅由后端开发进程读取。Windows Revit 已启动后不会自动读取该文件：需将 BIM / Revit 两个交换目录配置为 Revit 宿主机环境变量，重启 Revit 后生效。

## Docker 基础设施

Compose 提供 PostgreSQL、RabbitMQ、Redis、Milvus、MinIO、后端/Worker 和可观测栈。先在 .env 设置强随机 JWT_SECRET，然后构建并启动：

~~~powershell
docker build -t buildmate:models .
docker compose up -d
~~~

BIM 交换目录通过 data/runtime/revit 挂载进后端容器。Revit 仍须在安装了 Revit 与 pyRevit 的 Windows 宿主机运行，不能放入 Linux 容器。

## Revit Worker

1. 在 .env 配置 BIM_JSON_IN 和 REVIT_OUTPUT_DIR 为 Windows 宿主机上的共享目录；若直接使用项目内演示目录，可设置 `BUILDMATE_PROJECT_ROOT` 为项目根目录。
2. 将同样的两个变量设置为 Revit 进程的环境变量，重启 Revit。
3. 运行 python scripts/sync_revit_ext.py，把 [workers/pyrevit](workers/pyrevit) 的主副本同步到 pyRevit 扩展目录。
4. Revit MCP 源码位于 [workers/revit_mcp](workers/revit_mcp)，使用根目录 requirements.txt 即可调试。

写操作通过独立 Bridge：

~~~powershell
.\.venv\Scripts\python -m uvicorn workers.revit_bridge.main:app --host 127.0.0.1 --port 8005
~~~

Bridge 默认 `validate-only`。启用真实写入前必须配置审批密钥，并由后端持久化 Review/Build 审批；不要把 8005 暴露到局域网或公网。

DXF 可直接处理；DWG 转换器是可选的 Windows 外部依赖，需通过 `ODA_FILE_CONVERTER` 显式配置。

## 质量检查

~~~powershell
.\.venv\Scripts\python -m pip check
docker compose config --quiet
cd frontend
npm ci
npm run build
~~~

构建产物和运行时数据均可再生，不纳入版本控制。

## 当前文档

- [架构总览](docs/ARCHITECTURE.md)
- [当前可运行基线](docs/CURRENT_STATUS.md)
- [优化记录](docs/OPTIMIZATION.md)
- [当前状态](docs/CURRENT_STATUS.md)
- [产品需求](docs/PRD.md)
- [全栈总体设计](docs/DESIGN.md)
- [数据库设计](docs/DATABASE_DESIGN.md)
- [UI / UE 设计规范](docs/UI_UX_DESIGN.md)
- [功能模块设计](docs/design/modules)
- [公共合同](docs/contracts/README.md)
- [PDF/DWG → Revit 当前操作与验收合同](docs/WALL_PIPELINE.md)
- [BIM 国标建模标准（可执行 profile）](docs/BIM_MODELING_STANDARD.md)
