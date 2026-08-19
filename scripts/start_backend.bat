@echo off
rem BuildMate 后端启动（Windows 必经 run_backend.py：PG+psycopg 需要 Selector 事件循环）
rem 用法：scripts\start_backend.bat [端口]
cd /d %~dp0\..
set PYTHONIOENCODING=utf-8
if "%1"=="" (set PORT=8000) else (set PORT=%1)
echo [BuildMate] 启动后端 :%PORT% ...
if exist .venv\Scripts\python.exe (
    .venv\Scripts\python.exe run_backend.py %PORT%
) else (
    python run_backend.py %PORT%
)
