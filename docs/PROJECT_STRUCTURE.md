# BuildMate 项目结构清单（2026-08-19 重新归类）

> 新文件去向约定：新 Agent → `backend/agents/<name>/`（graph/nodes/state/prompts 四件套）；
> 新 API → `backend/api/v1/`（router.py 聚合）；新表 → `backend/db/schema.py` + `alembic revision --autogenerate`；
> 新测试 → `tests/test_*.py`（conftest 自动隔离环境）；数据资产 → `data/`；部署物 → 根目录。

## 一、后端源码 `backend/`

| 目录/文件 | 职责 |
|---|---|
| `main.py` | 应用入口：lifespan（Alembic 迁移/saver/预热/资源释放）、CORS、路由、静态挂载 |
| `config.py` | 配置中心（pydantic-settings，全量环境变量见 .env.example） |
| `dependencies.py` | JWT 认证依赖、角色校验、LLM 端点限流 |
| `api/v1/` | REST 接口层：auth（认证）、unified_chat（SSE 统一入口）、agents_api（四 Agent）、bim_api（BIM 审图） |
| `agents/` | 四大业务 Agent（LangGraph）：qa（RAG）、bid_review（并行评审）、procurement（HitL 审批）、negotiation（状态机） |
| `core/` | 横向设施：llm_factory（Provider 注册表·可插拔）、state_store（Redis/内存共享状态·可插拔）、memory（检查点 SQLite/PG）、security（bcrypt）、observability、llm_text（LLM 输出解析）、orchestrator、retry、reranker、query_classifier、logger、exceptions |
| `db/` | 数据层：schema.py（**建表唯一事实源**）、session.py（engine）、dialect.py（跨方言 upsert） |
| `services/` | 业务服务：vector_store（向量检索·后端注册表可插拔）、pdf_parser、ifc_parser、bim_review、material_prices |
| `mcp/` | MCP 工具：client + 两个独立 Server（knowledge_base:8001 / web_search:8002） |
| `data/mock/` | 内置 mock 用户（离线 demo 回退） |

## 二、前端 `frontend/`

| 位置 | 职责 |
|---|---|
| `src/api/` | axios client（401 自动刷新重试）、SSE 消费、全部接口函数 |
| `src/views/` | 7 个页面：Dashboard/QAChat/BidReview/BimReview/Procurement/Negotiation/Teacher/History/Login |
| `src/stores/` `src/router/` | Pinia auth store（token/refresh）、路由守卫 |
| `static/` | vite 构建产物（FastAPI 直接挂载，**勿手改**，`npm run build` 再生） |

## 三、测试 `tests/`（13 文件，51+ 用例，离线自洽）

- 单元：`test_*_nodes.py`（四 Agent）、`test_state_store.py`、`test_pluggability.py`
- API 回归：`test_procurement_hitl.py`（H1）、`test_fix_regressions.py`（H2-H6/M）、`test_auth_system.py`（认证全链路）、`test_bim_and_knowledge.py`（BIM + 知识闭环）
- 纯函数：`test_qa_nodes.py` 等
- **手动脚本**（需活服务器，CI 排除）：`test_smoke.py`、`test_samples.py`
- `conftest.py`：全_suite 环境隔离（Mock LLM/临时 SQLite/模型跳过/限流重置）

## 四、数据资产 `data/`

`knowledge/`（demo 知识）、`knowledge_real/`（真实法规规范 MD）、`bim/demo_wall.ifc`（BIM 示例模型）、`mock/sample_bid.pdf`、`test_samples.json`（批量测试样本）

## 五、数据库迁移 `migrations/`（Alembic）

`versions/0001_baseline`（存量库收敛·表清单已冻结）→ `0002_bim_reviews`。
此后改表一律 `alembic revision --autogenerate`，勿改 baseline。

## 六、脚本 `scripts/`（按用途分三类）

- **初始化**：init_db.py（建库+种子用户）、seed_knowledge.py（灌知识库）、verify_env.py（CI 环境自检）
- **数据抓取**：fetch_material_prices.py（建材价格）、fetch_knowledge_sources/fetch_building_standards/fetch_gb51251.py（法规文档）
- **启动**：start_all.py、start_backend.bat、start_frontend.bat（本地开发）

## 七、部署（根目录）

`Dockerfile`、`docker-compose.yml`（backend/milvus/minio/etcd/postgres 栈）、`deploy.sh`/`deploy.bat`（一键部署：前端构建→建库→抓价格→灌知识）、`.dockerignore`、`alembic.ini`、`pytest.ini`、`requirements*.txt`

### 本地开发（PyCharm）

已内置两个运行配置（`.idea/runConfigurations/`，PyCharm 打开项目即可见）：

1. **BuildMate Backend (uvicorn :8000)**——用项目 `.venv` 起 `uvicorn backend.main:app --port 8000 --reload`，自动加载 `.env`；
2. **BuildMate Frontend (vite dev :3000)**——`npm run dev`，`/api` 自动代理到 8000。

⚠️ 若 8000 被 Docker 容器占用（`buildmate_backend`），本地后端起不来——先 `docker compose stop backend`。
改完前端源码想立即看到：跑 Frontend 配置访问 **3000**（热更新）；8000 页面吃的是**镜像内的构建产物**，必须 `docker build` + 重建容器才更新。

## 八、文档 `docs/`

`PROJECT_STRUCTURE.md`（本文）、`audit-2026-08-17/`（历史审计材料）

## 九、第三方与其他

| 位置 | 说明 |
|---|---|
| `vendor/openstd_spider/` | 第三方 vendored 爬虫（国标文库抓取，供 fetch_gb51251 等使用） |
| `models/README.md` | 本地大模型目录说明（实际模型在 MODELS_PATH） |
| `_attic/` | 零引用文件暂存区（确认不需要可整目录删） |

## 十、运行时产物（根目录，gitignore 已排除，勿提交）

`buildmate.db`（业务库·SQLite 模式）、`checkpoints.db`（LangGraph 检查点·SQLite 模式）、
`.env`（真实密钥）、`.mimosa/`（安全扫描状态）、`.idea/`、`__pycache__/`、`.pytest_cache/`

**注**：生产/PG 模式下业务库与检查点均在 PostgreSQL（Docker compose 的 buildmate_postgres），上述 SQLite 文件仅本地默认模式产生。
