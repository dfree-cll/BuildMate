# BuildMate 生产镜像（多阶段：前端构建 + 后端运行时）
# 用法：docker build -t buildmate:full .
# 挂载本地模型（BGE-M3/Reranker/分类器，语义检索需要）：
#   docker run -v "D:\新建文件夹 (2)\models:/models" -e MODELS_ROOT=/models -p 8000:8000 buildmate:full

# ═══════ Stage 1: 前端构建 ═══════
FROM node:20-slim AS frontend-build
WORKDIR /frontend
COPY frontend/package*.json ./
RUN npm ci --registry=https://registry.npmmirror.com || npm install --registry=https://registry.npmmirror.com
COPY frontend/ ./
RUN npm run build

# ═══════ Stage 2: 后端运行时 ═══════
FROM python:3.11-slim

WORKDIR /app

# 系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Python 依赖（默认 PyPI；加重试/超时，抗网络抖动）
# torch 先从 PyTorch CPU 索引装（~170MB）：PyPI 默认是 CUDA manylinux 包（906MB + 2.5GB
# nvidia 依赖），容器只做 CPU 推理纯属浪费；requirements 里的 torch==2.5.1 随后视为已满足
COPY requirements.txt .
RUN pip install --no-cache-dir --retries 5 --timeout 60 \
        --index-url https://download.pytorch.org/whl/cpu torch==2.5.1 \
 && pip install --no-cache-dir --retries 5 --timeout 60 -r requirements.txt

# 应用代码
COPY backend/ ./backend/
COPY scripts/ ./scripts/
COPY data/ ./data/
# Alembic 迁移（启动时自动 upgrade head；漏拷会导致应用启动失败）
COPY migrations/ ./migrations/
COPY alembic.ini ./
# 配置经环境变量/compose 显式注入，不再把 .env.example 打进镜像当默认 .env

# 前端静态产物（Stage 1）
COPY --from=frontend-build /frontend/static ./frontend/static

# 本地模型挂载点（BGE-M3/Reranker/分类器；不挂载则自动降级）
ENV MODELS_ROOT=/models
VOLUME ["/models"]

EXPOSE 8000

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
