#!/usr/bin/env bash
# BuildMate 一键部署脚本（Linux/macOS）
# 说明：
#   - 本机部署：直接运行（默认使用 models/ 本地模型，MODELS_ROOT 可覆盖）
#   - Docker 部署：docker compose up -d（MODELS_PATH 环境变量指定模型目录）
#     例：MODELS_PATH=/path/to/models docker compose up -d
set -e
cd "$(dirname "$0")"

echo "===== BuildMate 部署 ====="

echo "[1/4] 构建前端..."
cd frontend
npm install --registry=https://registry.npmmirror.com
npm run build
cd ..

echo "[2/4] 初始化数据库..."
python scripts/init_db.py

echo "[3/4] 抓取真实建材价格..."
python scripts/fetch_material_prices.py --force

echo "[4/4] 灌入知识库..."
python scripts/seed_knowledge.py

echo ""
echo "启动服务：python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000"
echo "访问：http://localhost:8000"
