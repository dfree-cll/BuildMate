# 本地模型目录（对标 EduAgent models/ 规范）

模型文件存放在 `D:\新建文件夹 (2)\models`（EduAgent 环境预留的模型仓库）：

| 目录 | 模型 | 用途 |
|---|---|---|
| embedding/bge-m3 | BGE-M3 (1024 维) | 语义嵌入（RAG dense 路） |
| reranker/bge-reranker-large | BGE-Reranker-Large | 检索精排 |
| classifier/query-classifier-finetuned | MiniLM 微调版 | 意图分类 |

部署到新机器时，将整个 models/ 目录拷贝到目标机，并在 .env.local 设置 MODELS_ROOT 指向它。
