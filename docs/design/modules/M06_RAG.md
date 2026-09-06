# M06 RAG 知识库设计

## 功能描述

为法规、规范、标准、企业制度、项目手册、合同、投标、技术和供应商资料提供版本化、权限隔离、可引用的知识检索服务。

## 功能实现流程

1. 登记文档来源、scope、项目、文档类型、版本和权限。
2. 校验文件并创建不可变 Artifact。
3. 解析 TXT/MD/PDF/DOCX/IFC；扫描 PDF 无文本时转 OCR 或显式失败。
4. 清洗、语义分块，保留页码、元素引用和元数据。
5. 生成 Embedding，生产写入 Milvus/本地索引并建立 BM25 索引；向量库只承担检索，权限仍由关系库控制。
6. 查询时先按租户/项目/scope/专业/版本过滤，再 Dense + BM25 召回。
7. 可用时 Reranker 精排，组装上下文并生成有引用回答。
8. 校验引用是否来自本次命中；低置信度拒答或进入知识待补。

## 业务规则

- 文档版本由 `content_hash + parser_version + chunking_version + embedding_model` 唯一确定。
- 向量库不是权限事实源，检索必须先经过关系库权限过滤。
- RAG 不存实时价格、图纸几何、Model IR、Revit 状态和未经确认 Web 结果。
- Dense 默认 0.7、BM25 默认 0.3；Reranker 不可用必须标记降级。
- 入库是异步、可重试、幂等任务；文档登记成功不等于索引完成，页面必须区分 `registered/processing/ready/failed`。
- 引用必须有真实 chunk、文档、来源名称和页码（适用时）。
- 低置信度问题进入知识待补，审核员补充形成新版本，不覆盖旧版本。

## 使用角色

- `admin`：文档、索引、版本、评估和权限管理。
- `reviewer`：查看待补、补充知识和反馈。
- `project/user`：按授权项目检索和查看引用。
- 问答入口通过意图路由调用规范查询、标书查询、合同审核和采购/供应商 Workflow；各 Workflow 通过统一 Port 使用。

## 界面设计要求

- 知识库页显示文档状态、版本、索引进度和失败原因。
- 检索结果显示来源、版本、页码、片段、Dense/BM25/Rerank 分数。
- 回答先显示结论，再显示可展开引用；无证据明确写“系统拒绝猜测”。
- 审核员看到问题、召回片段、原文和补充答案入口。

## API 与数据

- `POST /api/v2/knowledge/documents`、`POST /knowledge/ingest`、`POST /knowledge/search`、`GET /retrieval-runs/{id}`、`POST /knowledge/feedback`。
- 表：`knowledge_documents`、`knowledge_chunks`、`knowledge_ingest_jobs`、`retrieval_runs`、`retrieval_hits`、`knowledge_feedback`。

## 异常与验收

- 解析失败、OCR 缺失、Embedding/向量库不可用、Reranker 降级和无命中均可见。
- 重复入库幂等、跨租户检索隔离、无效引用被拒绝。
- 评估记录 Recall@K、MRR、nDCG、引用覆盖率和答案有据率。
