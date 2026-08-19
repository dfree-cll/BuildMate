@echo off
rem BuildMate 前端启动（对标 EduAgent：cd frontend && npm run dev）
rem 用法：scripts\start_frontend.bat
cd /d %~dp0\..\frontend
echo [BuildMate] 启动前端 :3000 ...
npm run dev
