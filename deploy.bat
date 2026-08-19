@echo off
REM BuildMate 一键部署脚本（Linux/macOS 用 deploy.sh；Windows 用本脚本）
echo ============================================
echo  BuildMate 部署
echo ============================================

echo [1/4] 构建前端...
cd frontend
call npm install --registry=https://registry.npmmirror.com 2>nul
call npm run build
if errorlevel 1 goto :fail
cd ..

echo [2/4] 初始化数据库...
call .venv\Scripts\python.exe scripts\init_db.py 2>nul || python scripts\init_db.py

echo [3/4] 抓取真实建材价格...
call .venv\Scripts\python.exe scripts\fetch_material_prices.py --force 2>nul || python scripts\fetch_material_prices.py --force

echo [4/4] 灌入知识库...
call .venv\Scripts\python.exe scripts\seed_knowledge.py 2>nul || python scripts\seed_knowledge.py

echo.
echo 启动服务：python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
echo 访问：http://localhost:8000
goto :eof

:fail
echo ❌ 部署失败，请检查上方错误信息。
