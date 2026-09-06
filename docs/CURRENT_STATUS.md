# BuildMate 当前可运行基线

更新日期：2026-09-07

最新 BIM 修复：图纸声明墙厚在墙面配对前参与识别，解决 800 mm 厚墙被默认 600 mm 上限过滤的问题；平面交付视图改为白色轮廓并移除强制实心填充，三维保留灰色显示；核心筒 L/T 节点改为按相交墙半厚度修剪，避免双向累计修剪造成墙体断缝；Revit 交付会显式打开墙、结构柱、结构梁和轴网类别，并同步写入墙体结构标志，避免模板隐藏或显示为普通墙。原始任务证据只读重放为 122 面墙（原 120）、225 根柱，几何 Gate 通过；相关回归通过。尚未重新交付 RVT，详情与验证边界见 [WALL_PIPELINE.md](WALL_PIPELINE.md#墙厚声明与交付显示2026-09-05)。

新增统一 Agent 记忆：各业务页面可恢复历史会话；QA/投标/采购/谈判使用 scoped 历史上下文；BIM 可显式保存/填入建模设置，并恢复成功任务关联。记忆不代替证据和审批。升级、旧会话兼容性及验证范围见 [AGENT_MEMORY.md](AGENT_MEMORY.md)。

当前设计：统一维护基线
当前增量：规则式单/复合任务理解与计划卡已接入工作台聊天流，和任务预览接口复用同一应用服务；明确业务请求不再重复 LLM 路由。计划可保存在 QA 隔离历史，恢复后仍标记尚未执行。复合任务已支持父子任务持久化、幂等创建和交接包记录；合同预审已接入受控 Workflow，并在证据不足时暂停人工审核。BIM 与 Revit 链路本轮未修改。

前端发送链路修复：Windows 一键启动器现在会同时停止 venv 启动包装进程和实际监听进程，刷新前先检查 Python 语法，单个服务启动失败不会拖垮已就绪的 QA API；当前项目/记忆请求与兼容入口统一支持访问令牌刷新。QA 支持中文输入法、点击和回车提交，空 SSE 响应会明确报错并保留原问题；投标、采购、谈判和 BIM 在记忆恢复期间不会清除未提交的输入。

架构总览：[`docs/ARCHITECTURE.md`](ARCHITECTURE.md)。当前只按“体验、接入、控制、业务、确定性能力、数据运行”六层理解系统；不要把 M01–M19 当作 19 个相互独立的产品入口。


## 当前产品边界

- 当前 BIM 唯一产品链路：`PDF/DWG/DXF → SourceManifest → SourceEntities → WallEvidence → WallModel → 人工审批 → Revit 2020 → 独立叠图`。
- 当前系统承载项目、Artifact、RAG、任务、审核、建模和聊天合同；BIM 与问答是两个独立用户入口。
- 问答入口的受控意图路由合同已确定：建筑规范/标书查询走查询能力，合同审核和采购/供应商请求走持久化 Workflow；BIM 请求回到独立 BIM 入口。
- 当前已提供 `POST /api/v2/chat/intents/preview`：只做意图、参数、置信度和路由预览，不产生副作用。合同审核通过 `contract_review` Workflow 执行，预览接口本身不代表审核已完成。
- 路由合同明确：合同审核和采购/供应商任务必须记录 `rag`、`structured`、`rules` 三类来源；BIM 路由记录 `bim` 与 `rules` 来源。
- 旧兼容入口仅作为投标、采购、谈判和旧 QA 的存量兼容入口，禁止新增功能；当前新问答路由和正式审核任务必须走当前合同；兼容路由仅在迁移完成前保留。
- 前端管理页面：项目知识库和独立任务时间线仅 `admin` 可见；审核中心为 `admin` / `reviewer` 可见，界面统一称“审核端”。

## 当前运行时

- 本地：SQLite + Local Database Runner + 本地 Artifact/向量索引，无需 Docker。
- 生产：PostgreSQL（含 RLS）+ RabbitMQ + Redis + MinIO/S3 + Milvus；向量库不承担权限事实。
- Revit：独立 Windows Bridge，默认回环地址；Revit 2020 写入必须通过 Dry-run、持久化审批、Transaction、读回和独立叠图。
- Revit 交付步骤使用独立的 `WALL_PIPELINE_REVIT_WRITE_TIMEOUT_SECONDS`（默认 900 秒），覆盖 Bridge 写入、读回、制品持久化和独立叠图审计；不会再受普通 Agent 120 秒步骤上限影响。
- `SourceEntities` 解析按输入文件哈希、解析配置和缓存版本复用；审批、Dry-run、Revit 写入与独立审计不使用缓存。
- 连梁标高已进入完整证据链：解析图纸标注或连梁表中的梁顶/梁底相对标高，关联连梁编号和原始文字证据；只给出一侧标高时按已核定梁高推导另一侧。若图纸说明“未注明连梁梁顶标高同该层顶板标高”，且用户提供了明确楼层范围，则标记 `level_default` 并使用该层顶标高；没有说明时保持“标高待核定”，冲突时阻断审核，不再把楼层 `0m` 冒充图纸标高。Revit 按梁实体包络的底/顶标高对齐构件，并读回校验位置误差。
- 连梁表识别已兼容 PDF/OCR 把小数点拆开的单元格（如 `0. 150`、`0。150`），并要求标高标题、数值和 `LL/KL` 编号位于同页同 frame、同轴且处于同一标高列容差内；说明文字、管线 `DN150/h+...` 和邻近表格数字不会再被当作连梁标高。
- 前端楼层输入采用实体底~顶区间（例如 `-6.4~0`，单位 m）；该区间确定本层墙柱实际底顶与高度，Revit 原生 Level 仅作为宿主，标高不一致时通过 Base Offset/族偏移落到输入区间；首个地下室区间自动识别为 `B1`，其他地下层要求显式编码。
- 维护基线已收敛：知识种子统一走 RAG 当前 ingestion；墙体工作流统一产物证据构造、原子复制和失败分类；不可达的进程内多 Agent 串联执行器已删除，多步骤任务只能走持久化 WorkflowRuntime。
- 前端投标报告、聊天气泡和 API 错误提示已共用组件级工具；运行时产物统一位于可再生数据目录。

## 数据目录

- `data/approved_profiles/`：按输入 SHA-256 锁定的已批准补充证据，是当前代码依赖，不得放回 `runtime`。
- `data/revit/`：本地演示的 Revit 基准工作模型。
- `data/runtime/`、`data/uploads*`、`data/generated/`、`data/deliveries/`：可再生运行数据，均被 Git 忽略。
- `data/runtime/wall_pipeline/.source-cache/`：按输入哈希和解析配置复用的 SourceEntities 缓存，可安全清空，清空后自动重建。

## 已验证检查

2026-09-07 当前增量：前端生产构建通过；已在 Edge 中实测 QA 点击发送、中文回车发送、谈判面板发送，以及后端自动刷新后无需重启前端继续发送；六个本地服务刷新后均恢复为 OK。BIM 建模仍需在 Windows Revit 环境执行。

```text
前端 `npm run build` 已通过；仅有第三方包注释和 chunk size 警告。`docker compose config --quiet` 已通过（Docker 配置文件权限警告不影响解析）；真实 Revit 2020 Bridge 冒烟和生产 Compose 启动仍需在对应 Windows/生产环境执行。
```

## 启动

Windows 本地环境使用唯一入口：

```powershell
.\start_project.bat
```

脚本会刷新本项目的后端、MCP 与 Bridge；如果前端 Vite 已在 `:3000` 运行则复用现有进程，后端改动不会重启前端或丢失浏览器会话。Vite 自身负责前端文件热更新；脚本不会清理数据库或 Revit 交付文件。
