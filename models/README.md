# 本地模型目录

模型文件放在 `MODELS_ROOT` 指向的目录（默认项目内 `models/`，可通过环境变量覆盖；容器内为 `/models`）：

| 目录 | 模型 | 用途 |
|---|---|---|
| embedding/bge-m3 | BGE-M3 (1024 维) | 语义嵌入（RAG dense 路） |
| reranker/bge-reranker-large | BGE-Reranker-Large | 检索精排 |
| classifier/query-classifier-finetuned | MiniLM 微调版 | 意图分类 |

> 未配置时自动降级：API embedding → 本地哈希向量（demo 仍可运行）。
