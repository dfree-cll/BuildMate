param(
    [switch]$SeedKnowledge
)

$ErrorActionPreference = "Stop"

# This entry point deliberately uses process-scoped local settings.  It does
# not change .env, so a production-style .env can remain in the repository
# while the demo still starts without PostgreSQL, Redis, Milvus, or an LLM key.
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
$frontendPath = Join-Path $projectRoot "frontend"
$runtimePath = Join-Path $projectRoot "data\runtime"
$runtimeDatabasePath = Join-Path $runtimePath "db"
$runtimeLogPath = Join-Path $runtimePath "logs"

if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    Write-Error "Missing .venv\Scripts\python.exe. Create the virtual environment and install requirements.txt first."
    exit 1
}

if (-not (Test-Path -LiteralPath (Join-Path $frontendPath "package.json") -PathType Leaf)) {
    Write-Error "Missing frontend\package.json. The project directory is incomplete."
    exit 1
}

$nodeCommand = Get-Command node.exe -ErrorAction SilentlyContinue
$npmCommand = Get-Command npm.cmd -ErrorAction SilentlyContinue
if (-not $nodeCommand -or -not $npmCommand) {
    Write-Error "Node.js/npm not found. Install Node.js 20 or newer and try again."
    exit 1
}

if (-not (Test-Path -LiteralPath (Join-Path $frontendPath "node_modules") -PathType Container)) {
    Write-Error "Missing frontend\node_modules. Run: cd frontend; npm ci"
    exit 1
}

New-Item -ItemType Directory -Force -Path $runtimeDatabasePath, $runtimeLogPath | Out-Null

# Keep all settings local to this PowerShell process.  These values override
# .env only for the services launched by this script.
$env:APP_ENV = "local"
$env:APP_DEBUG = "true"
$env:DATABASE_URL = "sqlite+aiosqlite:///./data/runtime/db/buildmate_local.db"
$env:MIGRATION_DATABASE_URL = ""
$env:TASK_QUEUE_BACKEND = "local"
$env:ARTIFACT_STORAGE_BACKEND = "local"
$env:VECTOR_BACKEND = "local"
$env:MILVUS_HOST = ""
$env:REDIS_URL = ""
$env:RABBITMQ_URL = ""
# Keep the local one-click profile responsive.  These optional multi-GB
# models can be enabled explicitly in a production environment; loading a
# partial model during a demo otherwise makes the Send button appear stuck.
$env:RAG_LOCAL_MODELS_ENABLED = "false"
$env:RAG_RERANKER_ENABLED = "false"
# LLM/Embedding credentials deliberately follow .env.  Empty values select
# the deterministic local fallback; configured values enable the real service.
# The backend and Bridge still require the persisted WallModel and Revit-write
# approvals before any model side effect.  Leave this unset so the local
# demo's .env approval policy is honored instead of forcing validate-only mode.
$env:REVIT_BRIDGE_EXECUTION_ENABLED = $null
$env:REVIT_BRIDGE_LEGACY_SCRIPT_EXECUTION_ENABLED = "false"

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " BuildMate local one-click startup" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "Database: SQLite (data/runtime/db/buildmate_local.db)"
Write-Host "Task queue: local runner"
Write-Host "LLM: follows .env (empty key = mock/offline)"
Write-Host "Frontend: http://localhost:3000"
Write-Host "Backend: http://127.0.0.1:8000"
Write-Host "Revit Bridge: http://127.0.0.1:8005 (requires Revit 2020 + pyRevit Routes)"
Write-Host "Logs: data/runtime/logs/*.log"
Write-Host "Press Ctrl+C to stop all services."
Write-Host ""

$startArgs = @("scripts/start_all.py")
if ($SeedKnowledge) {
    $startArgs += "--seed"
    Write-Host "Knowledge seeding enabled (--seed); the first startup may take longer." -ForegroundColor Yellow
}

try {
    & $pythonPath @startArgs
    $exitCode = $LASTEXITCODE
}
catch {
    Write-Error "Startup failed: $($_.Exception.Message)"
    exit 1
}

if ($exitCode -ne 0) {
    Write-Error "Service process exited with code $exitCode. Check data/runtime/logs/*.log."
}
exit $exitCode
