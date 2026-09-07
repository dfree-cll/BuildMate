"""FastAPI entrypoint for the loopback-only Windows Revit Bridge.

Run on the Windows/Revit host:
    uvicorn workers.revit_bridge.main:app --host 127.0.0.1 --port 8005
"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from backend.domain.errors import DependencyFailure, DomainError, PolicyFailure, ValidationFailure
from workers.revit_bridge.contracts import (
    DiffRequest,
    ImportIFCRequest,
    PresentWallModelRequest,
    RunScriptRequest,
    RunWallModelRequest,
    ValidateScriptRequest,
)
from workers.revit_bridge.service import RevitBridgeService
from backend.core.metrics import REVIT_BRIDGE_UP

app = FastAPI(title="BuildMate Revit Bridge", version="2.0")
service = RevitBridgeService()


@app.middleware("http")
async def require_loopback(request: Request, call_next):
    client_host = request.client.host if request.client else ""
    if client_host not in {"127.0.0.1", "::1", "localhost"}:
        return JSONResponse(status_code=403, content={"error": "Revit Bridge only accepts loopback requests"})
    return await call_next(request)


@app.exception_handler(DomainError)
async def domain_error_handler(_: Request, exc: DomainError):
    status = 403 if isinstance(exc, PolicyFailure) else 503 if isinstance(exc, DependencyFailure) else 422
    error = {"code": exc.code, "message": str(exc)}
    details = getattr(exc, "details", None)
    if isinstance(details, dict) and details:
        error["details"] = details
    return JSONResponse(status_code=status, content={"error": error})


@app.get("/health")
async def health():
    REVIT_BRIDGE_UP.set(1)
    return {
        "status": "ok",
        "execution_enabled": service.settings.revit_bridge_execution_enabled,
        "mode": "write-enabled" if service.settings.revit_bridge_execution_enabled else "validate-only",
    }


@app.get("/tools/health", include_in_schema=False)
async def tool_health():
    """MCP-style compatibility name for Bridge health checks."""
    return await health()


@app.post("/tools/validate-script")
async def validate_script(request: ValidateScriptRequest):
    return service.validate(request.script, modifying=request.modifying)


@app.post("/tools/run-revit-script")
async def run_revit_script(request: RunScriptRequest):
    return await service.run_script(request)


@app.post("/tools/run-wall-model")
async def run_wall_model(request: RunWallModelRequest):
    return await service.run_wall_model(request)


@app.post("/tools/run_wall_model", include_in_schema=False)
async def run_wall_model_compat(request: RunWallModelRequest):
    return await service.run_wall_model(request)


@app.post("/tools/present-wall-model")
async def present_wall_model(request: PresentWallModelRequest):
    return await service.present_wall_model(request)


@app.post("/tools/run_revit_script", include_in_schema=False)
async def run_revit_script_compat(request: RunScriptRequest):
    return await service.run_script(request)


@app.post("/tools/import-ifc-to-rvt")
async def import_ifc_to_rvt(request: ImportIFCRequest):
    return await service.import_ifc(request)


@app.post("/tools/import_ifc_to_rvt", include_in_schema=False)
async def import_ifc_to_rvt_compat(request: ImportIFCRequest):
    return await service.import_ifc(request)


@app.post("/tools/export-rvt-diff")
async def export_rvt_diff(request: DiffRequest):
    return service.diff(request)


@app.post("/tools/export_rvt_diff", include_in_schema=False)
async def export_rvt_diff_compat(request: DiffRequest):
    return service.diff(request)
