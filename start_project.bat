@echo off
setlocal
cd /d "%~dp0"

rem BuildMate Windows one-click startup entry point
rem Optional: start_project.bat -SeedKnowledge
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_project.ps1" %*

if errorlevel 1 (
    echo.
    echo Startup failed. Check the messages above or logs_*.log.
    pause
)
endlocal
