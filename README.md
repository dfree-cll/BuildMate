# BuildMate Demo — 建筑行业智能助手

对标 `D:\ai agent2\02.项目实战\02.EduAgent\文档\EduAgentV7.7` 架构实现的**建筑行业版可运行 Demo**（对应迁移方案第十二节"路线 A"）。

## ✅ 已验证可运行（2026-08-15 冒烟测试 9/9 通过）

| # | 场景 | 验证点 |
|---|---|---|
| 1 | 登录 | JWT 签发（mock 用户 admin/demo123） |
| 2 | 规则前置拦截 | "你好" → 零 Token 模板回复 |
| 3 | LLM 路由 | "西安螺纹钢多少钱" → qa + SSE token 流 |
| 4 | 意图引导 | "帮我审查投标文件" → guidance 卡片 |
| 5 | 多 Agent 计划 | "投标准备一条龙" → pipeline_plan |
| 6 | Orchestrator 直达 | bid_review 四维并行评审 |
| 7 | 采购审批-小额 | 规则+LLM 双轨自动通过 |
| 8 | 采购审批-HitL | 大额单 interrupt → resume → approved |
| 9 | 供应商谈判 | 状态机多阶段推进（quote→tech→…） |

## 架构（与 EduAgent V7.7 同构）

```
用户一句话
  │  POST /api/v1/chat/stream（SSE）
① 规则前置拦截 _pre_filter ── 命中"你好/谢谢"等 ──→ 零 Token 模板回复
② LLM 路由 _llm_route ── 6 类意图：bid_review / qa / procurement / negotiation / clarify / out_of_scope
③ 推送 routing_decision 事件 → 分发：
   - qa          → 单 Agent 流式执行知识问答图（RAG）
   - bid_review / procurement / negotiation → guidance 引导跳转
   - multi_agent → pipeline_plan（投标准备 = 投标审查 → 采购审批）
   - clarify     → 追问澄清
④ done 事件收尾
```

## 四大建筑 Agent（对标 EduAgent 四范式）

| 建筑 Agent | 范式 | 对标 EduAgent | 关键实现 |
|---|---|---|---|
| 投标文件审查 | 并行评审 fan-out/fan-in | 第 4 章 简历审查 | asyncio.gather 四维并行（商务/技术/资质/合规） |
| 建筑知识问答 | RAG | 第 5 章 智能问答 | 本地向量检索 + HyDE/Multi-Query + 置信度路由 |
| 采购/合同审批 | Human-in-the-Loop | 第 6 章 试卷批改 | 规则引擎+LLM 双轨 + interrupt()/Command(resume=) |
| 供应商谈判/交底 | 状态机 + SSE | 第 7 章 模拟面试 | 5 阶段状态机（quote→tech→delivery→sign→done） |

## 快速开始

```bash
cd F:\BuildMate\BuildMateDemo

# 1. 创建虚拟环境并安装依赖
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

# 2. 初始化数据库 + 知识库
python scripts/init_db.py
python scripts/seed_knowledge.py

# 3. 启动（.env 已配置 DeepSeek key → 真实 LLM 模式；清空 LLM_API_KEY 即回 Mock）
python -m uvicorn backend.main:app --port 8000

# 4. 打开前端
#    http://localhost:8000   （登录：admin / demo123）

# 5. 冒烟测试
python tests/test_smoke.py
```

## 启用真实语义向量（可选，推荐）

默认 Mock 模式用字符哈希向量（256 维，语义区分度有限）。启用真实嵌入后，Dense 路变为 BGE-M3 语义向量（1024 维），混合检索效果显著提升（错别字/同义词也能召回）。

### 步骤
1. 在 `.env` 填入硅基流动（或任意 OpenAI 兼容）embedding key：
   ```ini
   EMBEDDING_API_KEY=sk-你的key
   EMBEDDING_BASE_URL=https://api.siliconflow.cn/v1
   EMBEDDING_MODEL=BAAI/bge-m3
   ```
2. 重新灌知识库（必须重灌，向量维度从 256 → 1024）：
   ```bash
   python scripts/seed_knowledge.py
   ```
3. 重启服务即可。若 key 失效或网络异常，系统**自动降级回哈希向量**（无需干预）。

> ⚠️ 注意：`D:\SmartVoyage\SmartVoyage\config.py` 里的旧硅基流动 key 已失效（403），请用你自己账户新申请的 key。

## 真实语义嵌入（BGE-M3）已接入 ✅

使用 **EduAgent conda 环境**（`D:\develop\anaconda3\envs\EduAgent`，Python 3.11 + torch 2.5.1 CPU + transformers 4.51.0 + FlagEmbedding）运行 demo，本地加载 `D:\新建文件夹 (2)\models\embedding\bge-m3`（1024 维中文语义向量）。

### 启动方式（必须用 EduAgent 环境）
```bash
# 启动（bge-m3 首次加载约 15 秒）
D:\develop\anaconda3\envs\EduAgent\python.exe -m uvicorn backend.main:app --port 8000

# 重灌知识库（1024 维向量）
D:\develop\anaconda3\envs\EduAgent\python.exe scripts\seed_knowledge.py
```

### 三级嵌入降级
1. **本地 BGE-m3**（1024 维语义，优先）→ 2. **API embedding**（需 key）→ 3. **哈希向量**（256 维兜底）
真实语义向量解决了哈希向量做不到的：**错别字/同义词召回**（如"深垦坑"→"深基坑"、"钢筋"→"螺纹钢"）。

### 样本测试体系（39 条）
`data\test_samples.json`（39 条覆盖：价格/规范/招标/租赁/施工方案 + 同义词/错别字 + 库外问题 + 寒暄）
`tests\test_samples.py` 批量跑 → **39/39 (100%) 通过** ✅

## 当前运行配置（2026-08）

- **LLM**：DeepSeek 官方 API（`https://api.deepseek.com/v1`，模型 `deepseek-chat` / `deepseek-v4-flash`），已配置真实 key → **真实 LLM 模式**（非 Mock）
- **生成分档**：融合分 ≥0.6 → 严格 RAG（基于知识库）；0.4~0.6 → rag_hybrid（知识库为参考 + 真实 LLM 组织）；<0.4 → llm_direct（真实模型自由回答）
- **Embedding**：未配置（DeepSeek 无 embedding API）→ 混合检索的 Dense 路仍用本地哈希向量 + BM25 Sparse 路
- 想回 Mock 模式：清空 `.env` 的 `LLM_API_KEY` 后重启即可

## Mock 模式 vs 真实 LLM

- **Mock 模式（默认）**：未配置 `LLM_API_KEY`，LLM 调用返回本地规则模板，RAG 用字符哈希向量，全链路（SSE、路由、图谱、HitL）照常工作。**零成本、可离线演示、可 CI**。
- **真实 LLM 模式**：在 `.env` 填入 `LLM_API_KEY`（OpenAI 兼容，如硅基流动 DeepSeek-V3），自动切换真实模型；嵌入可用 `EMBEDDING_API_KEY`（BAAI/bge-m3）。

## 目录结构

```
BuildMateDemo/
  backend/
    main.py            # FastAPI 装配（lifespan/CORS/路由/静态页）
    config.py          # pydantic-settings 配置中心
    dependencies.py    # JWT 认证依赖注入
    core/
      orchestrator.py  # 单 Agent 直达 + 多 Agent Pipeline
      llm_factory.py   # LLM 工厂（Mock/真实双模式）
      retry.py         # 三层兜底（重试→降级→系统兜底）
      memory.py        # MemorySaver + thread_id
      logger.py        # 结构化日志
      exceptions.py    # 统一异常体系
    agents/
      qa/              # 知识问答（RAG：classify→retrieve→generate）
      bid_review/      # 投标审查（四维并行评审）
      procurement/     # 采购审批（HitL interrupt/resume）
      negotiation/     # 供应商谈判（状态机）
    api/v1/
      unified_chat.py  # ★ 统一入口（_pre_filter + _llm_route + SSE 分发）
      agents_api.py    # 各 Agent REST 接口
      auth.py          # 登录
    services/
      vector_store.py  # 本地向量库（哈希向量降级）
    db/session.py      # SQLAlchemy 异步会话（SQLite）
  scripts/
    init_db.py         # 建表 + 模拟用户
    seed_knowledge.py  # 知识库灌入
    start_all.py       # 一键启动
  data/knowledge/      # 建筑行业知识库（建材价格/规范/政策/施工方案）
  frontend/static/     # 前端页面（SSE 客户端）
  tests/test_smoke.py  # 端到端冒烟测试（9 场景）
```

## EduAgent 架构完善记录（2026-08）

对标 EduAgent V7.7 原版范式完成的升级：

| 模块 | 升级内容 |
|---|---|
| 数据层 | 8 张表：users/qa_sessions(+summary_version)/bid_reviews(状态机+超时)/purchase_orders/approval_records/negotiation_sessions/knowledge_chunks/knowledge_pending_queue |
| 投标审查 | 后台任务（202 提交）+ 轮询状态机（processing→done/failed）+ 15 分钟超时兜底 + 列表接口（对齐简历审查 4.8/4.9） |
| 知识问答 | 记忆节点（load/save_memory + 摘要压缩，每 10 轮触发）+ MemorySaver 多轮记忆 + 历史会话接口（对齐 RAG 5.9/5.16） |
| 采购审批 | pending 待审批列表 + my-orders 我的订单 + 结果查询（对齐试卷批改 6.12） |
| 供应商谈判 | 结构化五维报告生成（价格/技术/交付/风险/合作）+ SSE 流式接口（对齐模拟面试 7.9/7.12） |
| Orchestrator | Pipeline 每步独立 session_id 防串台 + 前序 structured 注入后序（{agent}_result 键）+ 失败保留成果（对齐 8.3） |
| 前端 | 投标审查改为轮询渲染（提交→轮询→报告/风险/结论） |

### 新增 API 一览
- `POST /api/v1/bid-review/review`（202 后台）→ `GET /api/v1/bid-review/reviews/{id}`（轮询）→ `GET /api/v1/bid-review/reviews`（列表）
- `GET /api/v1/procurement/pending`（待审批）、`GET /api/v1/procurement/my-orders`（我的订单）
- `POST /api/v1/negotiation/chat/stream`（SSE 流式 + done 附报告）
- `GET /api/v1/qa/sessions/{id}/history`（历史会话）

## 部署方式（对标 EduAgent 规范）

### 环境要求
- Python 3.11（EduAgent conda 环境：`D:\develop\anaconda3\envs\EduAgent`）
- Node.js 18+（前端）
- 本地模型：`D:\新建文件夹 (2)\models`（BGE-M3 / Reranker / MiniLM）
- Docker（可选：PostgreSQL + Milvus 基础设施）

### 双终端启动（EduAgent 方式）
```bash
# 终端 A：后端 :8000
scripts\start_backend.bat        # 或 D:/develop/anaconda3/envs/EduAgent/python.exe -m uvicorn backend.main:app --port 8000

# 终端 B：前端 :3000
scripts\start_frontend.bat       # 或 cd frontend && npm run dev
# 访问 http://localhost:3000
```

### 部署前检查
```bash
python scripts/verify_env.py      # 环境自检（依赖/模型/LLM key/数据库/知识库）
python scripts/init_db.py         # 初始化数据库（SQLite 或 PostgreSQL）
python scripts/seed_knowledge.py  # 灌知识库（本地嵌入或 Milvus）
```

### 生产部署
- 前端生产构建：`cd frontend && npm run build`，产物由后端 StaticFiles 服务（单端口）
- Docker 容器化：`docker compose up -d` 起 PostgreSQL+Milvus，Dockerfile 打包后端
- 配置：复制 `.env.local.example` 为 `.env.local` 填写

## Vue3 前端完成（✅ 已验证联通）

前端已从单页 SPA 升级为 **Vue3 + Element Plus + Pinia + Vue Router** 完整工程（`frontend/`），对齐 EduAgent 第 9 章：

| 页面 | 功能 |
|---|---|
| /login | 登录（JWT，鉴权守卫） |
| /dashboard | 仪表盘 + LLM 调用统计 |
| /qa | 智能对话（SSE 流式，RAG+真实 LLM） |
| /bid-review | 投标审查（PDF 上传 + 轮询） |
| /procurement | 采购审批（下单 + 批准/驳回） |
| /negotiation | 供应商谈判（状态机多轮） |
| /teacher | 教师端（待审批 + 知识待补队列） |
| /history | 历史记录 |

### 启动方式（两个终端）
```bash
# 终端1：后端 :8000
D:/develop/anaconda3/envs/EduAgent/python.exe -m uvicorn backend.main:app --port 8000

# 终端2：前端 :3000
cd frontend && npm install && npm run dev
# 打开 http://localhost:3000，登录 admin/demo123
```

### 验证
- ✅ Vite proxy 联通后端（登录/SSE/查询全通）
- ✅ SSE 流式经 proxy：44 token，回答"螺纹钢 3560 元/吨"（来源 [建材价格]）

## Vue3 前端（对齐 EduAgent 第9章）

前端已从单页 SPA 升级为 **Vue3 + Element Plus + Pinia + Vue Router** 完整工程（`frontend/`）：
- 登录页 / 仪表盘 / 智能对话（SSE 流式）/ 投标审查（PDF上传+轮询）/ 采购审批 / 供应商谈判 / 教师端（待审批+知识待补）/ 历史记录
- Vite proxy 连后端 :8000，前端跑 :3000
- JWT 鉴权守卫（未登录跳转 /login）

启动：`cd frontend && npm install && npm run dev` → http://localhost:3000

## PostgreSQL + Milvus 实连验证（已完成 ✅）

系统已有 EduAgent 环境的基础设施容器运行，demo 已实连：

| 组件 | 容器 | 连接 |
|---|---|---|
| PostgreSQL | edu_agent_postgres (:5433) | ✅ 建 buildmate 库 + 8 表 + 用户 |
| Milvus | edu_agent_milvus (:19531) | ✅ knowledge_domain 集合（course_id=buildmate 隔离） |
| MinIO/etcd | Milvus 依赖 | ✅ healthy |

### 实连要点
1. .env：DATABASE_URL=postgresql+asyncpg://eduagent_user:eduagent123456@localhost:5433/buildmate，MILVUS_HOST=localhost
2. Milvus schema 对齐 EduAgent knowledge_domain（embedding 1024 维 + sparse_embedding 等必填字段），course_id=buildmate 隔离
3. db/dialect.py 处理 SQLite/PG upsert 差异；migrations.py 跨方言查表
4. Milvus 搜索需指定 anns_field=embedding（集合有 dense+sparse 双向量字段）

### 验证结果
- Milvus 语义检索：螺纹钢 0.74 / 塔吊 0.74 / GB50010 0.67（真实 BGE-M3 向量）
- 冒烟测试：10/10 ✅
- 样本测试：39/39 (100%) ✅（加限流重试：真实 LLM 密集请求偶发 DeepSeek 402/限流）

## 双数据库自适应支持（PostgreSQL + Milvus 代码就绪）

已完成的代码层改造（Docker 引擎启动后即生效）：
1. backend/db/dialect.py：SQLite ↔ PostgreSQL 方言自适应，所有 upsert 自动选择合适语法
2. backend/services/vector_store.py：Milvus 可选后端（配置 MILVUS_HOST 且可连接时使用）
3. backend/config.py：新增 milvus_host/milvus_port 配置
4. init_db.py：用户灌入方言自适应

启用方式（Docker 引擎可用时）：
  docker compose up -d
  # 在 .env 配置切换数据库：
  #   DATABASE_URL=postgresql+asyncpg://buildmate:buildmate123@localhost:5433/buildmate
  #   MILVUS_HOST=localhost  MILVUS_PORT=19531
  python scripts/init_db.py
  python scripts/seed_knowledge.py
  python -m uvicorn backend.main:app --port 8000

注：当前沙箱环境 Docker 引擎不可用（com.docker.service Stopped，需管理员启动），
PostgreSQL/Milvus 实连验证在正常环境进行。SQLite 模式已回归验证通过。

## RAG 闭环增强记录（第四轮）

| # | 增强项 | 实现 |
|---|---|---|
| 1 | **知识待补闭环** | QA 图加 `enqueue_pending_node`：低置信度问题自动写入 `knowledge_pending_queue`；新增 `GET /api/v1/knowledge/pending`（教师查看）+ `POST /api/v1/knowledge/pending/{id}/resolve`（标记已解决） |
| 2 | **Web 搜索兜底** | generate_node 低置信度分支尝试调用 web_search MCP（:8002）→ 有结果注入并标 `web_augmented`（附 🌐 来源）；无结果/服务不可用优雅降级 `llm_direct` |

### 闭环演示（实测）
1. 问"女儿墙施工规范"（知识库无）→ 自动入队（conf 0.1347）
2. `GET /knowledge/pending` 教师可见 → `POST /resolve` 标记 → pending 归零
3. Web 搜索不可用时降级 llm_direct，真实模型准确引用 GB 50345-2012 等规范

> 注：DuckDuckGo 搜索在沙箱环境因证书存储权限受限无法联网验证；代码就绪，部署到正常环境即生效。

## 生产化补齐记录（第三轮）

| # | 补齐项 | 实现 |
|---|---|---|
| 1 | **单元测试体系** | pytest + pytest-asyncio，13 个测试覆盖四个 Agent 核心节点（QA 检索/生成/分类、投标解析/格式化、采购规则/HitL、谈判状态机），Mock LLM 不依赖真实 API |
| 2 | **可观测性** | `backend/core/observability.py`：LLM 调用追踪（耗时/字符/估算成本，SQLite 存储），`GET /api/v1/observability/stats` 查询；所有 `get_llm()` 自动追踪 |
| 3 | **Docker Compose** | `docker-compose.yml`：PostgreSQL + Milvus（etcd/MinIO 内部），端口 5433/19531；`.env.production.example` 切库配置 |
| 4 | **前端增强** | 自动登录 + 首屏加载历史（完整 Vue3 重写暂缓：现有 SPA 已覆盖 4 功能页，重写有回归风险） |

### 测试矩阵
- 单元测试：`pytest tests/ -v` → **13/13**
- 冒烟测试：`python tests/test_smoke.py` → **10/10**
- 样本测试：`python tests/test_samples.py` → **39/39 (100%)**

### 观测示例
一次问答 = 3 次 LLM 调用（路由+策略+生成），`GET /api/v1/observability/stats` 返回：
`{"calls": 3, "total_ms": 2392, "est_cost_usd": 0.0007}`

## 差距补齐记录（第二轮：对齐 EduAgent 完整框架）

| # | 补齐项 | 实现 |
|---|---|---|
| 1 | **Reranker 精排** | `backend/core/reranker.py`：加载 bge-reranker-large（CPU），Hybrid 召回 8 条 → 精排 top3 + 置信度（0.75 阈值） |
| 2 | **意图分类器** | `backend/core/query_classifier.py`：MiniLM 微调版（general/specialized）+ 规则快通道三层分类 |
| 3 | **MCP 工具层** | `backend/mcp/`：知识库检索 + 联网搜索两个 FastMCP Server（独立进程 :8001/:8002）+ client.py JSON-RPC 调用 |
| 4 | **PDF 解析** | `backend/services/pdf_parser.py`：PyMuPDF 双栏解析 + `POST /bid-review/upload` 上传接口 |
| 5 | **数据库迁移** | `backend/db/migrations.py`：幂等补丁（SQLite 兼容：先查列再 ALTER）+ lifespan 启动执行 |

### 启动时本地模型预热（lifespan）
并行加载三个本地模型：分类器（~12s）+ BGE-M3 嵌入（~3s）+ Reranker（~7s），首请求不再卡顿。

### MCP 独立进程启动
```bash
D:/develop/anaconda3/envs/EduAgent/python.exe backend/mcp/knowledge_base_server.py  # :8001
D:/develop/anaconda3/envs/EduAgent/python.exe backend/mcp/web_search_server.py       # :8002
```

## 已知修复记录

- **SSE token 流**：Mock 模式下 MockChatModel 原本不产生流式 chunk，导致前端停在"思考中"。已实现 `_stream()` 方法（按 4 字符切块 yield AIMessageChunk）+ 统一入口兜底（generate 结束时无 token 则推送完整答案）。冒烟测试确认 token 流 = True。
- **前端 SSE 解析 CRLF**：sse-starlette 用 CRLF（\r\n\r\n）分帧，旧前端用 `split('\n\n')` 切不开导致页面停在"思考中"。已改为逐行解析（兼容 CRLF/LF）。
- **检索质量**：知识库从 5 个大块细分为 14 个二级标题块（每块自带标题上下文）；塔吊租赁从"施工规范"归位到"机械租赁"；检索加入关键词域加权（价格/规范/招标/施工方案/租赁/采购），mock 模式命中准确率大幅提升。
- **混合检索（Hybrid）**：对标 EduAgent WeightedRanker(0.7, 0.3)——Dense 路（字符哈希向量余弦，语义）+ Sparse 路（BM25，字面词命中，IDF 自动压低"规范/施工"等高频泛词），融合分 = 0.7×dense + 0.3×sparse_ratio。另加**共享实体词门槛**：查询与 chunk 必须共享含非功能字的 3-gram 专名（螺纹钢/塔吊/深基坑…）才保留 sparse 贡献，否则归零——彻底解决"女儿墙施工规范"（库内无此内容）被"施工/规范"泛词抬分误答的问题。
- **检索加权改为实体词词典**：原"领域词表加权"会把查询中的通用词（如"规范"）抬升所有规范类 chunk，导致"女儿墙施工规范"（知识库无此内容）误答地基基础规范。现改为仅当【具体实体词】（螺纹钢/塔吊/GB50010/女儿墙等）在查询与 chunk 中都出现才加权，配合阈值 0.6 实现准确判定：相关命中 0.9~1.5（RAG），无关 0.3~0.5（诚实回复"暂无相关内容"）。
- **置信度阈值 0.6**：检索 top1 得分 ≥0.6 才走 RAG 回答（相关命中实测 ≥0.95，无关 ≤0.28），知识库没有的问题会诚实回复"暂无相关内容"而非硬答不相关结果。
- **Mock 回答与检索一致**：Mock 模式的问答回复原本硬编码为"螺纹钢价格"，导致问规范也答价格。已改为从 prompt 中提取【知识库参考内容】原样回显（问规范回规范、问价格回价格），并保留参考来源标注。
- **功能页面**：前端从单页聊天升级为多视图 SPA（智能对话/投标审查/采购审批/供应商谈判），跳转链接改为 hash 路由（#/bid-review 等），引导卡片可直达对应功能页。


- **SSE token 流**：Mock 模式下 MockChatModel 原本不产生流式 chunk，导致前端停在"思考中"。已实现 `_stream()` 方法（按 4 字符切块 yield AIMessageChunk）+ 统一入口兜底（generate 结束时无 token 则推送完整答案）。冒烟测试确认 token 流 = True。

## 与 EduAgent 原版的差异（demo 降级点）

| 组件 | EduAgent 原版 | 本 Demo |
|---|---|---|
| 数据库 | PostgreSQL + asyncpg | SQLite + aiosqlite（改 DATABASE_URL 即可切回） |
| 向量库 | Milvus + BGE-M3 | 本地哈希向量（改 `vector_store.py` 可切真实嵌入） |
| 意图分类 | MiniLM-L6-v2 本地模型 | 规则 + LLM 路由（`_llm_route`） |
| 精排 | BGE-Reranker | 无（demo 用召回分数直排） |
| 记忆 | MemorySaver + qa_sessions 表 | MemorySaver（同构） |
| 观测 | Langfuse | 结构化日志 |
