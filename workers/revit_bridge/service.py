"""Safe Revit Bridge use cases.

Write execution is fail-closed: disabled by default, requires a constant-time
approval token match, copies the source model, and never reports success without
a real Revit response/output file.
"""

from __future__ import annotations

import hashlib
import asyncio
import json
import logging
import math
import os
import re
import shutil
import stat
from pathlib import Path

import httpx

from backend.config import get_settings
from backend.domain.errors import DependencyFailure, PolicyFailure, ValidationFailure
from backend.domain.approval_token import (
    approval_digest,
    normalize_target_path,
    verify_approval_token,
)
from backend.mcp.revit_client import RevitMCPClient, RevitMCPError
from backend.engines.wall_pipeline.contracts import RevitResult, WallModel
from backend.engines.wall_pipeline.io import canonical_sha256
from workers.pyrevit.wall_model_contract import normalize_wall_model_payload
from workers.revit_bridge.contracts import (
    BridgeResult,
    DiffRequest,
    ImportIFCRequest,
    PresentWallModelRequest,
    RunScriptRequest,
    RunWallModelRequest,
)
from workers.revit_bridge.security import has_explicit_transaction, validate_revit_script
from workers.revit_bridge.wall_compiler import (
    ELEMENT_PARAMETER_FIELDS,
    SHARED_PARAMETER_FILE_NAME,
    shared_parameter_file_content,
    WALL_COMPILER_VERSION,
    compiled_plan_sha256,
    compile_wall_model_script as _typed_compile_wall_model_script,
)


logger = logging.getLogger(__name__)


_REVIT_ROUTE_COMPACT_THRESHOLD_BYTES = 160_000
_REVIT_ROUTES_BASE_URL = "http://127.0.0.1:48884/revit_mcp"
_REVIT_ROUTES_EXECUTE_URL = "http://127.0.0.1:48884/revit_mcp/execute_code/"


def _write_shared_parameter_file(work_dir: Path) -> Path:
    """Materialize the deterministic BM_* definitions inside one build scope."""

    path = (work_dir / SHARED_PARAMETER_FILE_NAME).resolve()
    if work_dir.resolve() not in path.parents:
        raise ValidationFailure("shared parameter file escaped the build workspace")
    # Revit 2020's shared-parameter reader is most reliable with the native
    # UTF-16 text format (including the BOM); this also keeps the file valid
    # if a future field description contains non-ASCII text.
    path.write_text(shared_parameter_file_content(), encoding="utf-16")
    return path


def _route_execution_code(script: str, *, payload_path: Path | None = None) -> str:
    """Keep large generated programs below the pyRevit Routes transport limit.

    The approved plan and compiled-script hashes remain bound to the original
    deterministic program.  This wrapper changes only its local transport:
    IronPython reads the exact UTF-8 bytes from the Bridge-owned build
    directory and verifies their SHA-256 before compiling and executing them.
    Keeping the outer request small also prevents pyRevit's diagnostic
    ``code_attempted`` echo from turning a useful Revit exception into an
    opaque empty HTTP 502 response.
    """

    encoded = script.encode("utf-8")
    if len(encoded) <= _REVIT_ROUTE_COMPACT_THRESHOLD_BYTES:
        return script
    if payload_path is None:
        raise ValueError("large Revit route programs require a scoped payload path")
    payload_path.write_bytes(encoded)
    expected_sha256 = hashlib.sha256(encoded).hexdigest()
    bootstrap = (
        "# -*- coding: utf-8 -*-\n"
        "import hashlib\n"
        "_buildmate_route_file = open(%r, 'rb')\n"
        "try:\n"
        "    _buildmate_route_code = _buildmate_route_file.read()\n"
        "finally:\n"
        "    _buildmate_route_file.close()\n"
        "if hashlib.sha256(_buildmate_route_code).hexdigest() != %r:\n"
        "    raise Exception('BuildMate route payload hash mismatch')\n"
        "exec(compile(_buildmate_route_code.decode('utf-8'), '<buildmate-wall-model>', 'exec'))\n"
    ) % (str(payload_path), expected_sha256)
    return bootstrap


async def _execute_revit_route(code: str, description: str, timeout: float) -> dict:
    """Execute an approved generated program through local pyRevit Routes.

    The Bridge is already the security boundary for typed WallModel writes.
    Calling the loopback route directly avoids an unnecessary FastMCP stdio
    child whose extra HTTP forwarding layer can turn Revit 2020 responses into
    opaque 502s.  The response is normalized to the existing MCP envelope so
    all downstream marker and failure validation remains unchanged.
    """

    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.post(
                _REVIT_ROUTES_EXECUTE_URL,
                json={
                    "code": code,
                    "description": description,
                    "use_transaction": False,
                },
            )
    except httpx.HTTPError as exc:
        raise RevitMCPError(f"pyRevit Routes request failed: {str(exc)[:300]}") from exc
    if response.status_code != 200:
        return {
            "text": f"Error: {response.status_code} - {response.text}",
            "raw": {"isError": False},
        }
    try:
        payload = response.json()
    except ValueError:
        return {"text": response.text, "raw": {"isError": False}}
    output = payload.get("output") if isinstance(payload, dict) else None
    if isinstance(output, str) and output.strip():
        return {"text": output, "raw": {"isError": False}}
    return {
        "text": json.dumps(payload, ensure_ascii=False) if isinstance(payload, dict) else str(payload),
        "raw": {"isError": False},
    }


async def _open_revit_document_route(file_path: Path, timeout: float) -> dict:
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.post(
                _REVIT_ROUTES_BASE_URL + "/open_document/",
                json={"file_path": str(file_path), "detach": False, "audit": False},
            )
    except httpx.HTTPError as exc:
        raise RevitMCPError(f"pyRevit document route failed: {str(exc)[:300]}") from exc
    if response.status_code != 200:
        return {
            "text": f"Error: {response.status_code} - {response.text}",
            "raw": {"isError": False},
        }
    try:
        payload = response.json()
    except ValueError:
        return {"text": response.text, "raw": {"isError": False}}
    return {
        "text": json.dumps(payload, ensure_ascii=False),
        "raw": {"isError": False},
    }


def _wall_model_working_copy(work_dir: Path, build_id: str) -> Path:
    """Give every typed delivery a Revit-unique document title.

    Revit refuses to open two documents with the same file name even when
    their directories differ.  Operators commonly keep the previous audited
    ``working_copy.rvt`` open while running the next floor, so a fixed name
    makes the next delivery fail inside pyRevit Routes with an opaque 502.
    ``build_id`` is already restricted to safe filename characters by the
    request contract.
    """

    return work_dir / ("buildmate_%s.rvt" % build_id)


class RevitBridgeService:
    def __init__(self) -> None:
        self.settings = get_settings()
        # A single Bridge process may receive duplicate deliveries at the same
        # time (RabbitMQ is at-least-once).  Serialize writes for one target so
        # working_copy.rvt and its rendered view cannot be interleaved.
        self._build_locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, key: str) -> asyncio.Lock:
        lock = self._build_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._build_locks[key] = lock
        return lock

    def validate(self, script: str, *, modifying: bool) -> dict:
        violations = validate_revit_script(script)
        if modifying and not has_explicit_transaction(script):
            violations.append(_transaction_violation())
        return {
            "valid": not violations,
            "violations": [item.__dict__ for item in violations],
        }

    def _require_execution_approval(
        self,
        token: str | None,
        *,
        build_id: str,
        action: str,
        wall_model_sha256: str | None = None,
        script_sha256: str | None = None,
        target_model_path: str | None = None,
        source_model_sha256: str | None = None,
        approval_digest_value: str | None = None,
    ) -> None:
        if not self.settings.revit_bridge_execution_enabled:
            raise PolicyFailure("Revit Bridge execution is disabled")
        expected = self.settings.revit_bridge_approval_secret
        if len(expected) < 32 or not token:
            raise PolicyFailure("a valid persisted human approval token is required")
        try:
            verify_approval_token(
                token,
                expected,
                build_id=build_id,
                action=action,
                wall_model_sha256=wall_model_sha256,
                script_sha256=script_sha256,
                target_model_path=target_model_path,
                source_model_sha256=source_model_sha256,
                approval_digest=approval_digest_value,
            )
        except ValueError as exc:
            # Path normalization can fail before the token verifier gets to
            # its typed policy error.  Keep the Bridge fail-closed and expose
            # one stable policy failure to callers.
            raise PolicyFailure("invalid or expired Revit approval token") from exc

    def _work_dir(self, build_id: str) -> Path:
        root = Path(self.settings.revit_bridge_work_root).resolve()
        target = (root / build_id).resolve()
        if root != target and root not in target.parents:
            raise ValidationFailure("invalid build work directory")
        target.mkdir(parents=True, exist_ok=True)
        return target

    def _validate_target_path(self, value: str | Path) -> Path:
        """Resolve an RVT path and enforce the optional deployment allow-list."""

        try:
            path = Path(value).expanduser().resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise ValidationFailure("Revit target path is invalid") from exc
        if path.suffix.lower() != ".rvt":
            raise ValidationFailure("Revit target path must be an RVT file")
        raw_root = str(getattr(self.settings, "revit_target_root", "") or "").strip()
        if raw_root:
            try:
                root = Path(raw_root).expanduser().resolve(strict=False)
            except (OSError, RuntimeError) as exc:
                raise ValidationFailure("revit_target_root is invalid") from exc
            if not root.is_dir():
                raise ValidationFailure("revit_target_root must be an existing directory")
            if path != root and root not in path.parents:
                raise PolicyFailure("Revit target path is outside the configured target root")
        return path

    async def run_script(self, request: RunScriptRequest) -> BridgeResult:
        """Validate/execute a script and recover a failed working copy."""

        source: Path | None = None
        working_copy: Path | None = None
        initial_hash: str | None = None
        source_snapshot_hash: str | None = None
        rollback_state = "not_needed"
        rollback_hash: str | None = None
        if not request.dry_run:
            try:
                source = Path(request.source_model_path).resolve()
                working_copy = self._work_dir(request.build_id) / "working_copy.rvt"
                if source.is_file():
                    source_snapshot_hash = _sha256(source)
                if working_copy.is_file():
                    initial_hash = _sha256(working_copy)
            except Exception:
                source = None
                working_copy = None
        try:
            return await self._run_script_impl(request)
        except Exception:
            if source is not None and working_copy is not None:
                try:
                    current_hash = _sha256(working_copy) if working_copy.is_file() else None
                    if current_hash is not None and current_hash != initial_hash:
                        _restore_working_copy(source, working_copy)
                        for stale_view in working_copy.parent.glob(
                            "build_%s_actual_view*.png" % request.build_id
                        ):
                            if stale_view.is_file():
                                stale_view.unlink()
                except Exception as rollback_error:
                    logger.exception(
                        "failed to restore generic Revit working copy: %s",
                        rollback_error,
                    )
            raise

    async def _run_script_impl(self, request: RunScriptRequest) -> BridgeResult:
        report = self.validate(request.script, modifying=request.modifying)
        if not report["valid"]:
            raise ValidationFailure(json.dumps(report["violations"], ensure_ascii=False))
        source = self._validate_target_path(request.source_model_path)
        if not source.is_file():
            raise ValidationFailure("source RVT does not exist")
        source_hash = _sha256(source)
        if request.dry_run:
            return BridgeResult(
                status="validated", build_id=request.build_id, dry_run=True,
                message="static validation passed; Revit was not modified",
                details={"source_sha256": source_hash, "convergence_round": request.convergence_round, **report},
            )

        if not self.settings.revit_bridge_legacy_script_execution_enabled:
            raise PolicyFailure(
                "legacy arbitrary Revit script execution is disabled"
            )
        expected_script_hash = hashlib.sha256(
            request.script.encode("utf-8")
        ).hexdigest()
        if not request.script_sha256 or not request.source_model_sha256:
            raise PolicyFailure(
                "legacy Revit script execution requires script_sha256 and source_model_sha256"
            )
        if request.script_sha256 != expected_script_hash:
            raise PolicyFailure("script_sha256 does not match script contents")
        if request.source_model_sha256 != source_hash:
            raise PolicyFailure("source_model_sha256 does not match source RVT")

        self._require_execution_approval(
            request.approval_token,
            build_id=request.build_id,
            action="run_revit_script",
            target_model_path=str(source),
            source_model_sha256=source_hash,
            script_sha256=request.script_sha256,
        )
        work_dir = self._work_dir(request.build_id)
        working_copy = work_dir / "working_copy.rvt"
        if source == working_copy.resolve():
            raise ValidationFailure("source RVT cannot be the Bridge working-copy path")
        # Revit keeps an opened RVT locked on Windows.  Surface that as a
        # dependency failure (HTTP 503) instead of letting PermissionError
        # escape as an opaque HTTP 500.  This also gives the task runtime a
        # retryable, actionable error when a previous delivery window is
        # still open in Revit.
        try:
            shutil.copy2(source, working_copy)
            _ensure_writable_copy(working_copy)
        except PermissionError as exc:
            raise DependencyFailure(
                "Revit working copy is locked or read-only; close the previous "
                "BuildMate RVT window in Revit 2020 and retry"
            ) from exc
        except OSError as exc:
            raise DependencyFailure(
                f"cannot prepare Revit working copy: {str(exc)[:300]}"
            ) from exc
        before_hash = _sha256(working_copy)
        if before_hash != source_hash:
            raise DependencyFailure("source RVT changed while preparing the working copy")
        try:
            async with RevitMCPClient(timeout=self.settings.revit_bridge_timeout_seconds) as client:
                wrapped_script = _wrap_working_copy_script(request.script, working_copy)
                response = await asyncio.wait_for(
                    client.call_tool(
                        "execute_revit_code",
                        {"code": wrapped_script, "description": f"BuildMate {request.build_id}"},
                    ),
                    timeout=self.settings.revit_bridge_timeout_seconds,
                )
        except (RevitMCPError, asyncio.TimeoutError) as exc:
            raise DependencyFailure(f"Revit execution failed: {str(exc)[:300]}") from exc
        if not working_copy.is_file():
            raise DependencyFailure("Revit execution removed the working model")
        response_text = _require_success_response(response)
        if not re.search(r"(?m)^\s*BUILDMATE_WORKING_COPY_SAVED\s*$", response_text):
            raise DependencyFailure("Revit did not confirm saving the isolated working copy")
        after_hash = _sha256(working_copy)
        if request.modifying and after_hash == before_hash:
            raise DependencyFailure("Revit returned success but the working copy is unchanged")
        return BridgeResult(
            status="executed", build_id=request.build_id, dry_run=False,
            message="Revit returned successfully; read-back validation is still required",
            output_path=str(working_copy),
            details={
                "before_sha256": before_hash,
                "after_sha256": after_hash,
                "script_sha256": request.script_sha256,
                "source_model_sha256": source_hash,
                "target_model_path": str(source),
                "convergence_round": request.convergence_round,
                "revit_response": response_text[:2_000],
            },
        )

    async def run_wall_model(self, request: RunWallModelRequest) -> BridgeResult:
        try:
            source_key = normalize_target_path(request.source_model_path)
        except ValueError as exc:
            raise ValidationFailure("source RVT path is invalid") from exc
        # Different builds on one RVT must serialize their snapshot/read-back
        # work; unrelated targets may proceed concurrently.
        async with self._lock_for(source_key):
            return await self._run_wall_model(request)

    async def _run_wall_model(self, request: RunWallModelRequest) -> BridgeResult:
        """Run one delivery and restore the isolated copy on every failed attempt.

        The generated Revit program saves before rendering so the PNG is tied
        to persisted model bytes.  That means a later export/read-back error
        cannot be repaired with a Revit transaction rollback alone.  The
        Bridge therefore treats the source RVT as the recovery snapshot and
        restores ``working_copy.rvt`` whenever this attempt changed it but did
        not complete all read-back gates.
        """
        source: Path | None = None
        working_copy: Path | None = None
        initial_hash: str | None = None
        source_snapshot_hash: str | None = None
        rollback_state = "not_needed"
        rollback_hash: str | None = None
        if not request.dry_run:
            try:
                source = Path(request.source_model_path).resolve()
                working_copy = _wall_model_working_copy(
                    self._work_dir(request.build_id), request.build_id
                )
                if source.is_file():
                    source_snapshot_hash = _sha256(source)
                if working_copy.is_file():
                    initial_hash = _sha256(working_copy)
            except Exception:
                # The implementation below emits the canonical validation
                # error; do not turn a malformed request into a rollback error.
                source = None
                working_copy = None
        try:
            return await self._run_wall_model_impl(request)
        except Exception as error:
            if source is not None and working_copy is not None:
                try:
                    current_hash = _sha256(working_copy) if working_copy.is_file() else None
                    # Do not overwrite a previous successful copy when the
                    # request failed before this attempt copied the source.
                    if current_hash is not None and current_hash != initial_hash:
                        if source_snapshot_hash and _sha256(source) == source_snapshot_hash:
                            _restore_working_copy(source, working_copy)
                            rollback_hash = _sha256(working_copy)
                            if rollback_hash != source_snapshot_hash:
                                raise RuntimeError("rollback verification hash mismatch")
                            rollback_state = "restored"
                            for stale_view in working_copy.parent.glob(
                                "build_%s_actual_view*.png" % request.build_id
                            ):
                                if stale_view.is_file():
                                    stale_view.unlink()
                        else:
                            rollback_state = "unknown"
                            rollback_hash = current_hash
                    else:
                        rollback_state = "not_needed"
                except Exception as rollback_error:
                    rollback_state = "failed"
                    try:
                        rollback_hash = _sha256(working_copy) if working_copy.is_file() else None
                    except Exception:
                        rollback_hash = None
                    logger.exception(
                        "failed to restore Revit working copy after delivery error: %s",
                        rollback_error,
                    )
            setattr(error, "rollback_state", rollback_state)
            setattr(error, "rollback_hash", rollback_hash)
            setattr(error, "details", {
                "rollback_state": rollback_state,
                "rollback_hash": rollback_hash,
            })
            raise

    async def _run_wall_model_impl(self, request: RunWallModelRequest) -> BridgeResult:
        """Compile and apply an approved WallModel on an isolated RVT copy.

        This endpoint deliberately accepts a typed DTO rather than Python
        source.  The Bridge owns the compiler, transaction, read-back count
        and actual-view export, so the backend cannot accidentally turn a
        wall delivery task into arbitrary Revit code execution.
        """
        try:
            wall_model = WallModel.model_validate(request.wall_model)
            revit_result = RevitResult.model_validate(request.revit_result)
            # Run the same pure-Python contract used by the pyRevit worker.
            compiled = normalize_wall_model_payload(wall_model.model_dump(mode="json"))
        except Exception as exc:
            raise ValidationFailure(f"invalid wall-model delivery payload: {str(exc)[:500]}") from exc

        model_hash = canonical_sha256(wall_model)
        plan_hash = compiled_plan_sha256(compiled)
        if revit_result.wall_model_sha256 != model_hash:
            raise ValidationFailure("Revit result does not reference this wall model")
        if wall_model.review_status != "approved":
            raise PolicyFailure("wall model must be human-approved before Bridge execution")
        allowed_statuses = {"dry_run_passed", "write_approved"} if request.dry_run else {"write_approved"}
        if revit_result.status not in allowed_statuses:
            raise PolicyFailure("wall-model delivery requires a matching Revit approval state")
        if not request.dry_run and (
            revit_result.approval is None
            or revit_result.approval.status != "approved"
        ):
            raise PolicyFailure("Revit write approval record is missing or not approved")

        source = self._validate_target_path(request.source_model_path)
        if not source.is_file():
            raise ValidationFailure("source RVT does not exist")
        configured_target = self._validate_target_path(
            wall_model.revit.target_model_path or ""
        )
        if configured_target != source:
            raise ValidationFailure(
                "source_model_path must match wall_model.revit.target_model_path"
            )
        current_source_hash = _sha256(source)
        result_snapshot = (revit_result.readback or {}).get("source_model_sha256")
        expected_source_hash = request.source_model_sha256 or result_snapshot
        if request.source_model_sha256 and result_snapshot and (
            request.source_model_sha256 != result_snapshot
        ):
            raise ValidationFailure(
                "request source RVT hash does not match the approved dry-run snapshot"
            )
        if expected_source_hash and expected_source_hash != current_source_hash:
            raise PolicyFailure(
                "target RVT changed after the approved dry-run; run a new dry-run"
            )
        if not request.dry_run and not expected_source_hash:
            raise PolicyFailure(
                "live Revit delivery requires a source RVT hash captured by dry-run"
            )
        wall_count = sum(
            1 for item in (compiled.get("model_elements") or [])
            if (item or {}).get("type") == "Wall"
        )
        column_count = sum(
            1 for item in (compiled.get("model_elements") or [])
            if (item or {}).get("type") == "Column"
        )
        beam_count = sum(
            1 for item in (compiled.get("model_elements") or [])
            if (item or {}).get("type") == "Beam"
        )
        grid_count = sum(
            len((grid or {}).get("x_axes") or []) + len((grid or {}).get("y_axes") or [])
            for grid in (compiled.get("grids") or [])
        )
        openings = compiled.get("openings") or []
        opening_count = len(openings)
        matched_opening_count = sum(
            1 for item in openings if (item or {}).get("status") == "matched"
        )
        opening_host_count = len({
            str(host_id)
            for item in openings
            if (item or {}).get("status") == "matched"
            for host_id in ((item or {}).get("host_wall_ids") or [])
        })
        elements = compiled.get("model_elements") or []
        quantity_takeoff = {
            "wall_gross_volume_m3": round(sum(
                float(((item or {}).get("quantities") or {}).get("gross_volume_m3") or 0.0)
                for item in elements if (item or {}).get("type") == "Wall"
            ), 6),
            "column_gross_volume_m3": round(sum(
                float(((item or {}).get("quantities") or {}).get("gross_volume_m3") or 0.0)
                for item in elements if (item or {}).get("type") == "Column"
            ), 6),
            "beam_gross_volume_m3": round(sum(
                float(((item or {}).get("quantities") or {}).get("gross_volume_m3") or 0.0)
                for item in elements if (item or {}).get("type") == "Beam"
            ), 6),
            "irregular_column_count": sum(
                1 for item in elements
                if (item or {}).get("type") == "Column"
                and (item or {}).get("profile_kind") == "irregular"
            ),
            "unspecified_material_count": sum(
                1 for item in elements
                if (((item or {}).get("construction") or {}).get("material_status")
                    == "unspecified")
            ),
        }
        element_properties = {
            "storage": ["Mark", "Comments", "ApplicationDataId"],
            "named_parameter_prefix": "BM_",
            "named_parameter_count": len(ELEMENT_PARAMETER_FIELDS),
            "fields": [
                "element_id", "category", "type_name", "type_mark",
                "material_name", "material_status", "classification_code",
                "quantity_basis", "quantity_scope", "level_id",
                "thickness_mm", "height_mm", "width_mm", "depth_mm",
                "profile_kind", "length_m", "footprint_area_m2",
                "side_area_m2", "gross_volume_m3", "source_ref_count",
                "confidence",
            ],
            "schedule_created": False,
        }
        if request.dry_run:
            return BridgeResult(
                status="validated", build_id=request.build_id, dry_run=True,
                message="approved WallModel compiled; Revit was not modified",
                details={
                    "wall_model_sha256": model_hash,
                    "compiler": WALL_COMPILER_VERSION,
                    "compiled_plan_sha256": plan_hash,
                    "wall_count": wall_count,
                    "column_count": column_count,
                    "beam_count": beam_count,
                    "grid_count": grid_count,
                    "opening_count": opening_count,
                    "matched_opening_count": matched_opening_count,
                    "opening_host_count": opening_host_count,
                    "quantity_takeoff": quantity_takeoff,
                    "element_properties": element_properties,
                    "source_model_sha256": current_source_hash,
                    "target_model_path": normalize_target_path(str(source)),
                    "tenant_id": wall_model.tenant_id,
                    "project_id": wall_model.project_id,
                    "convergence_round": request.convergence_round,
                },
            )

        approval_binding = approval_digest(revit_result.approval)
        self._require_execution_approval(
            request.approval_token,
            build_id=request.build_id,
            action="run_wall_model",
            wall_model_sha256=model_hash,
            target_model_path=str(source),
            source_model_sha256=current_source_hash,
            approval_digest_value=approval_binding,
        )
        work_dir = self._work_dir(request.build_id)
        _write_shared_parameter_file(work_dir)
        working_copy = _wall_model_working_copy(work_dir, request.build_id)
        if source == working_copy.resolve():
            raise ValidationFailure("source RVT cannot be the Bridge working-copy path")

        # RabbitMQ/local retries are at-least-once.  A completed build is a
        # durable capability result, so replay the exact receipt instead of
        # opening Revit and creating a second set of elements.  Reusing a
        # build id for different content is rejected rather than silently
        # mixing two approvals in one work directory.
        receipt_path = work_dir / "delivery_receipt.json"
        existing_receipt = _read_delivery_receipt(receipt_path)
        if existing_receipt is not None:
            receipt_build_id = existing_receipt.get("build_id")
            if receipt_build_id is not None and receipt_build_id != request.build_id:
                raise PolicyFailure("Revit delivery receipt build_id does not match the request")
            if (
                existing_receipt.get("wall_model_sha256") != model_hash
                or existing_receipt.get("source_model_sha256") != current_source_hash
                or existing_receipt.get("approval_digest") != approval_binding
            ):
                raise PolicyFailure("Revit build id is already bound to different content")
            try:
                receipt_target = normalize_target_path(
                    str(existing_receipt.get("target_model_path") or "")
                )
                current_target = normalize_target_path(str(source))
            except ValueError as exc:
                raise PolicyFailure("Revit delivery receipt has an invalid target path") from exc
            if receipt_target != current_target:
                raise PolicyFailure("Revit build id is already bound to a different target RVT")
            receipt_output = _receipt_artifact_path(
                existing_receipt.get("output_path"), work_dir, suffix=".rvt"
            )
            receipt_view = _receipt_artifact_path(
                existing_receipt.get("actual_view_path"), work_dir, suffix=".png"
            )
            if receipt_output is None or receipt_view is None:
                raise PolicyFailure(
                    "Revit delivery receipt contains an artifact path outside the build workspace"
                )
            if not _receipt_detail_paths_are_scoped(
                existing_receipt.get("details"), work_dir
            ):
                raise PolicyFailure(
                    "Revit delivery receipt contains an unscoped detail path"
                )
            details = existing_receipt.get("details") or {}
            details_digest = existing_receipt.get("details_sha256")
            if not isinstance(details_digest, str) or not re.fullmatch(
                r"[a-f0-9]{64}", details_digest
            ):
                # Receipts from before the detail-integrity field are stale;
                # rebuild the scoped copy instead of trusting their nested
                # read-back values.
                receipt_path.unlink(missing_ok=True)
                existing_receipt = None
            elif details_digest != _details_sha256(details):
                raise PolicyFailure(
                    "Revit delivery receipt detail integrity check failed"
                )
            elif not _receipt_details_match_identity(
                details,
                wall_model_sha256=model_hash,
                source_model_sha256=current_source_hash,
                target_model_path=current_target,
                output_path=receipt_output,
                actual_view_path=receipt_view,
                after_sha256=existing_receipt.get("after_sha256"),
                actual_view_sha256=existing_receipt.get("actual_view_sha256"),
            ):
                raise PolicyFailure(
                    "Revit delivery receipt details do not match its identity"
                )
            if (
                existing_receipt is not None
                and
                receipt_output.is_file()
                and receipt_view.is_file()
                and _sha256(receipt_output) == existing_receipt.get("after_sha256")
                and _sha256(receipt_view) == existing_receipt.get("actual_view_sha256")
            ):
                return BridgeResult(
                    status="executed", build_id=request.build_id, dry_run=False,
                    message="previously completed WallModel delivery replayed idempotently",
                    output_path=str(receipt_output),
                    details=dict(existing_receipt.get("details") or {}),
                )
            # A receipt without its immutable outputs is not a valid success;
            # remove only this scoped receipt and run a fresh isolated attempt.
            receipt_path.unlink(missing_ok=True)
        try:
            shutil.copy2(source, working_copy)
        except PermissionError as exc:
            # A previous timed-out Routes call can leave Revit holding the
            # isolated document open.  Let the API return a typed dependency
            # failure instead of leaking PermissionError as an opaque HTTP
            # 500, and avoid starting a second write against a locked file.
            raise DependencyFailure(
                "Revit working copy is locked by an active document; "
                "close the previous BuildMate model or wait for Revit to finish"
            ) from exc
        _ensure_writable_copy(working_copy)
        before_hash = _sha256(working_copy)
        if before_hash != current_source_hash:
            # The source may be replaced while it is being copied. Never
            # execute against bytes that differ from the snapshot bound to the
            # human-approved token and dry-run result.
            raise DependencyFailure(
                "source RVT changed while preparing the isolated working copy"
            )
        actual_prefix = work_dir / ("build_%s_actual_view" % request.build_id)
        # A retry reuses the same scoped work directory.  Remove only the
        # previous attempt's rendered files so a failed run can never be
        # mistaken for a fresh actual-view result.
        for stale_view in work_dir.glob(actual_prefix.name + "*.png"):
            if stale_view.is_file():
                stale_view.unlink()
        script = _typed_compile_wall_model_script(
            compiled, actual_prefix=actual_prefix,
            expected_wall_count=wall_count,
            expected_column_count=column_count,
            expected_beam_count=beam_count,
        )
        try:
            compile(script, "<buildmate-wall-model>", "exec")
        except SyntaxError as exc:
            raise ValidationFailure(f"compiled WallModel script is invalid: {exc.msg}") from exc
        try:
            # The script is generated solely from a validated WallModel.  It
            # never accepts user-authored source, and therefore does not use
            # the general-purpose script endpoint's arbitrary-code contract.
            wrapped_script = _wrap_working_copy_script(script, working_copy)
            route_payload_path = work_dir / "route_wall_model_payload.py"
            route_script = _route_execution_code(
                wrapped_script,
                payload_path=route_payload_path,
            )
            description = f"BuildMate WallModel {request.build_id}"
            if route_payload_path.is_file():
                response = await asyncio.wait_for(
                    _execute_revit_route(
                        route_script,
                        description,
                        self.settings.revit_bridge_timeout_seconds,
                    ),
                    timeout=self.settings.revit_bridge_timeout_seconds,
                )
            else:
                # Keep the ordinary MCP adapter for small generated programs;
                # only large payloads need the scoped-file Routes transport.
                async with RevitMCPClient(
                    timeout=self.settings.revit_bridge_timeout_seconds
                ) as client:
                    response = await asyncio.wait_for(
                        client.call_tool(
                            "execute_revit_code",
                            {"code": route_script, "description": description},
                        ),
                        timeout=self.settings.revit_bridge_timeout_seconds,
                    )
            if (
                route_payload_path.is_file()
                and
                _is_opaque_route_502(response)
                and working_copy.is_file()
                and _sha256(working_copy) == before_hash
            ):
                # Retry only an empty transport failure against an untouched
                # isolated RVT. Any persisted partial write fails closed.
                await asyncio.sleep(2.0)
                response = await asyncio.wait_for(
                    _execute_revit_route(
                        route_script,
                        description,
                        self.settings.revit_bridge_timeout_seconds,
                    ),
                    timeout=self.settings.revit_bridge_timeout_seconds,
                )
        except asyncio.TimeoutError as exc:
            raise DependencyFailure(
                "Revit WallModel execution timed out; the Revit window may be "
                "busy or still processing the previous delivery"
            ) from exc
        except RevitMCPError as exc:
            raise DependencyFailure(
                f"Revit WallModel execution failed: {str(exc)[:300]}"
            ) from exc
        if not working_copy.is_file():
            raise DependencyFailure("Revit execution removed the isolated working model")
        response_text = _require_success_response(response)
        route_payload_path.unlink(missing_ok=True)
        coordinate_marker = re.search(
            r"(?m)^\s*BUILDMATE_COORDINATE_APPLIED\s+"
            r"offset_x_mm=([-+]?\d+(?:\.\d+)?)\s+"
            r"offset_y_mm=([-+]?\d+(?:\.\d+)?)\s+"
            r"angle_rad=([-+]?\d+(?:\.\d+)?)\s*$",
            response_text,
        )
        if coordinate_marker is None:
            raise DependencyFailure("Revit did not return coordinate-transform read-back")
        coordinate_readback = {
            "offset_x_mm": float(coordinate_marker.group(1)),
            "offset_y_mm": float(coordinate_marker.group(2)),
            "angle_rad": float(coordinate_marker.group(3)),
        }
        geometry_marker = re.search(
            r"(?m)^\s*BUILDMATE_GEOMETRY_READBACK\s+"
            r"wall_xy_mm=([-+]?\d+(?:\.\d+)?)\s+"
            r"wall_thickness_mm=([-+]?\d+(?:\.\d+)?)\s+"
            r"column_xy_mm=([-+]?\d+(?:\.\d+)?)\s+"
            r"column_height_mm=([-+]?\d+(?:\.\d+)?)\s*$",
            response_text,
        )
        if geometry_marker is None:
            raise DependencyFailure("Revit did not return geometry-tolerance read-back")
        geometry_readback = {
            "max_wall_xy_error_mm": float(geometry_marker.group(1)),
            "max_wall_thickness_error_mm": float(geometry_marker.group(2)),
            "max_column_xy_error_mm": float(geometry_marker.group(3)),
            "max_column_height_error_mm": float(geometry_marker.group(4)),
        }
        beam_elevation_marker = re.search(
            r"(?m)^\s*BUILDMATE_BEAM_ELEVATION_READBACK\s+"
            r"max_base_z_mm=([-+]?\d+(?:\.\d+)?)\s*$",
            response_text,
        )
        if beam_count and beam_elevation_marker is None:
            raise DependencyFailure(
                "Revit did not return coupling-beam elevation read-back"
            )
        if beam_elevation_marker is not None:
            geometry_readback["max_beam_base_elevation_error_mm"] = float(
                beam_elevation_marker.group(1)
            )
        if any(value > 5.0 for value in geometry_readback.values()):
            raise DependencyFailure("Revit geometry read-back exceeds the 5 mm gate")
        marker = re.search(
            r"(?m)^\s*BUILDMATE_WALL_MODEL_APPLIED\s+"
            r"walls=(\d+)\s+grids=(\d+)\s+element_ids=([0-9,]+)\s*$",
            response_text,
        )
        if marker is None:
            raise DependencyFailure("Revit did not confirm WallModel application")
        marker_wall_count = int(marker.group(1))
        marker_grid_count = int(marker.group(2))
        if marker_wall_count != wall_count or marker_grid_count != grid_count:
            raise DependencyFailure(
                "Revit read-back counts do not match the approved WallModel"
            )
        column_marker = re.search(
            r"(?m)^\s*BUILDMATE_COLUMN_MODEL_APPLIED\s+"
            r"columns=(\d+)\s+element_ids=([0-9,]*)\s*$",
            response_text,
        )
        if column_count and column_marker is None:
            raise DependencyFailure("Revit did not confirm column model application")
        marker_column_count = int(column_marker.group(1)) if column_marker else 0
        if marker_column_count != column_count:
            raise DependencyFailure("Revit column read-back count mismatch")
        beam_marker = re.search(
            r"(?m)^\s*BUILDMATE_BEAM_MODEL_APPLIED\s+"
            r"beams=(\d+)\s+element_ids=([0-9,]*)\s*$",
            response_text,
        )
        if beam_count and beam_marker is None:
            raise DependencyFailure("Revit did not confirm coupling-beam model application")
        marker_beam_count = int(beam_marker.group(1)) if beam_marker else 0
        if marker_beam_count != beam_count:
            raise DependencyFailure("Revit coupling-beam read-back count mismatch")
        element_properties_marker = re.search(
            r"(?m)^\s*BUILDMATE_ELEMENT_PROPERTIES_APPLIED\s+"
            r"elements=(\d+)\s+walls=(\d+)\s+columns=(\d+)"
            r"(?:\s+beams=(\d+))?\s+named_parameters=(\d+)\s*$",
            response_text,
        )
        # Keep accepting the legacy marker while older Revit/pyRevit bridge
        # instances are being restarted.  Both markers describe properties
        # persisted on each element; this workflow never creates a schedule.
        schedule_marker = re.search(
            r"(?m)^\s*BUILDMATE_SCHEDULE_DATA_APPLIED\s+"
            r"elements=(\d+)\s+walls=(\d+)\s+columns=(\d+)"
            r"(?:\s+beams=(\d+))?\s*$",
            response_text,
        )
        property_marker = element_properties_marker or schedule_marker
        if property_marker is None:
            raise DependencyFailure("Revit did not confirm element properties")
        if (
            int(property_marker.group(1)) != wall_count + column_count + beam_count
            or int(property_marker.group(2)) != wall_count
            or int(property_marker.group(3)) != column_count
            or int(property_marker.group(4) or 0) != beam_count
        ):
            raise DependencyFailure("Revit element-property read-back count mismatch")
        if element_properties_marker is not None and element_properties_marker.group(4) is not None:
            expected_named_parameters = (wall_count + column_count + beam_count) * len(ELEMENT_PARAMETER_FIELDS)
            if int(element_properties_marker.group(5)) != expected_named_parameters:
                raise DependencyFailure("Revit named element-parameter read-back count mismatch")
            element_properties["written_named_parameter_count"] = int(
                element_properties_marker.group(5)
            )
        opening_marker = re.search(
            r"(?m)^\s*BUILDMATE_OPENING_SEMANTICS_APPLIED\s+"
            r"openings=(\d+)\s+matched=(\d+)\s+hosts=(\d+)\s+"
            r"marks=([A-Za-z0-9_,.\-]*)\s*$",
            response_text,
        )
        if opening_marker is None:
            raise DependencyFailure("Revit did not confirm opening semantic metadata")
        expected_opening_marks = sorted({
            str((item or {}).get("mark") or "")
            for item in openings
            if (item or {}).get("status") == "matched"
            and str((item or {}).get("mark") or "")
        })
        actual_opening_marks = sorted(
            item for item in opening_marker.group(4).split(",") if item
        )
        if (
            int(opening_marker.group(1)) != opening_count
            or int(opening_marker.group(2)) != matched_opening_count
            or int(opening_marker.group(3)) != opening_host_count
            or actual_opening_marks != expected_opening_marks
        ):
            raise DependencyFailure("Revit opening semantic read-back mismatch")
        expected_cuts = sum(item.get("cut_status") == "ready" for item in openings)
        cut_marker = re.search(
            r"(?m)^\s*BUILDMATE_OPENING_CUTS_APPLIED\s+count=(\d+)\s+ids=([0-9,]*)\s*$",
            response_text,
        )
        cut_ids = [item for item in cut_marker.group(2).split(",") if item] if cut_marker else []
        if (expected_cuts and cut_marker is None) or (cut_marker and (
            int(cut_marker.group(1)) != expected_cuts or len(cut_ids) != expected_cuts
            or len(set(cut_ids)) != len(cut_ids)
        )):
            raise DependencyFailure("Revit physical opening cut read-back is missing or mismatched")
        opening_storage_marker = re.search(
            r"(?m)^\s*BUILDMATE_OPENING_MARKERS_APPLIED\s+"
            r"own=(\d+)\s+host_receipt=(\d+)\s*$",
            response_text,
        )
        own_opening_marker_count = (
            int(opening_storage_marker.group(1)) if opening_storage_marker else 0
        )
        host_receipt_marker_count = (
            int(opening_storage_marker.group(2)) if opening_storage_marker else 0
        )
        if expected_cuts and (
            opening_storage_marker is None
            or own_opening_marker_count + host_receipt_marker_count != expected_cuts
        ):
            raise DependencyFailure("Revit physical opening identity receipt is missing or mismatched")
        opening_semantics = {
            "opening_count": opening_count,
            "matched_opening_count": matched_opening_count,
            "host_wall_count": opening_host_count,
            "marks": expected_opening_marks,
            "vertical_cut_status": "verified" if expected_cuts else "not_requested_without_sill_and_head_evidence",
            "cut_count": expected_cuts,
            "pending_cut_count": opening_count - expected_cuts,
            "created_opening_ids": cut_ids,
            "own_marker_count": own_opening_marker_count,
            "host_receipt_marker_count": host_receipt_marker_count,
        }
        topology_marker = re.search(
            r"(?m)^\s*BUILDMATE_WALL_TOPOLOGY_APPLIED\s+"
            r"junctions=(\d+)\s+joined_pairs=(\d+)\s+kinds=([A-Za-z,]*)\s*$",
            response_text,
        )
        if topology_marker is None:
            raise DependencyFailure("Revit did not confirm WallModel topology application")
        if int(topology_marker.group(1)) != len(compiled.get("junctions") or []):
            raise DependencyFailure("Revit topology read-back count mismatch")
        if len(compiled.get("junctions") or []) and int(topology_marker.group(2)) <= 0:
            raise DependencyFailure("Revit returned no joined wall pairs for the approved topology")
        topology_verified_marker = re.search(
            r"(?m)^\s*BUILDMATE_WALL_TOPOLOGY_VERIFIED\s+"
            r"joined_pairs=(\d+)\s+snapped_endpoints=(\d+)\s+"
            r"max_snap_mm=([-+]?\d+(?:\.\d+)?)\s*$",
            response_text,
        )
        if topology_verified_marker is None:
            raise DependencyFailure("Revit did not return verified wall-join read-back")
        verified_joined_pairs = int(topology_verified_marker.group(1))
        snapped_endpoint_count = int(topology_verified_marker.group(2))
        maximum_topology_snap_mm = float(topology_verified_marker.group(3))
        if (
            verified_joined_pairs != int(topology_marker.group(2))
            or snapped_endpoint_count < 0
            or not math.isfinite(maximum_topology_snap_mm)
            or maximum_topology_snap_mm < 0.0
        ):
            raise DependencyFailure("Revit verified wall-join read-back is inconsistent")
        refs_marker = re.search(
            r"(?m)^\s*BUILDMATE_WALL_TOPOLOGY_REFS\s+refs=([A-Za-z0-9_.:,\-]*)\s*$",
            response_text,
        )
        if refs_marker is None:
            raise DependencyFailure("Revit did not return per-junction topology read-back")
        expected_refs = {
            str(item.get("id")): str(item.get("kind"))
            for item in (compiled.get("junctions") or [])
        }
        actual_refs = {}
        actual_ref_counts = {}
        for value in [item for item in refs_marker.group(1).split(",") if item]:
            parts = value.split(":")
            if len(parts) != 3:
                raise DependencyFailure("invalid Revit topology reference read-back")
            junction_id, kind, count_text = parts
            try:
                count = int(count_text)
            except ValueError as exc:
                raise DependencyFailure("invalid Revit topology join count") from exc
            if count <= 0:
                raise DependencyFailure("Revit returned an unjoined topology junction")
            if junction_id in actual_refs:
                raise DependencyFailure("Revit returned duplicate topology junction references")
            actual_refs[junction_id] = kind
            actual_ref_counts[junction_id] = count
        if actual_refs != expected_refs:
            raise DependencyFailure("Revit topology junction read-back does not match the approved model")
        marker_joined_pairs = int(topology_marker.group(2))
        if actual_ref_counts:
            total_ref_pairs = sum(actual_ref_counts.values())
            if (
                marker_joined_pairs < max(actual_ref_counts.values())
                or marker_joined_pairs > total_ref_pairs
            ):
                raise DependencyFailure(
                    "Revit topology joined-pair count is inconsistent with junction references"
                )
        elif marker_joined_pairs != 0:
            raise DependencyFailure(
                "Revit topology reported joined pairs without junction references"
            )
        expected_kinds = sorted(set(expected_refs.values()))
        if sorted(item for item in topology_marker.group(3).split(",") if item) != expected_kinds:
            raise DependencyFailure("Revit topology kind read-back does not match the approved model")
        if not re.search(r"(?m)^\s*BUILDMATE_PERSISTED_BEFORE_RENDER\s*$", response_text):
            raise DependencyFailure("Revit rendered the view before confirming a saved model")
        if not re.search(r"(?m)^\s*BUILDMATE_WORKING_COPY_SAVED\s*$", response_text):
            raise DependencyFailure("Revit did not confirm saving the isolated working copy")
        created_element_ids = [item for item in marker.group(3).split(",") if item]
        if len(created_element_ids) != wall_count:
            raise DependencyFailure("Revit returned an incomplete wall element-id read-back")
        if len(set(created_element_ids)) != len(created_element_ids):
            raise DependencyFailure("Revit returned duplicate wall element IDs")
        created_column_ids = (
            [item for item in column_marker.group(2).split(",") if item]
            if column_marker else []
        )
        if len(created_column_ids) != column_count:
            raise DependencyFailure("Revit returned an incomplete column element-id read-back")
        if len(set(created_column_ids)) != len(created_column_ids):
            raise DependencyFailure("Revit returned duplicate column element IDs")
        created_beam_ids = (
            [item for item in beam_marker.group(2).split(",") if item]
            if beam_marker else []
        )
        if len(created_beam_ids) != beam_count:
            raise DependencyFailure("Revit returned an incomplete coupling-beam element-id read-back")
        if len(set(created_beam_ids)) != len(created_beam_ids):
            raise DependencyFailure("Revit returned duplicate coupling-beam element IDs")

        actual_candidates = sorted(
            (item for item in work_dir.glob(actual_prefix.name + "*.png") if item.is_file()),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        if not actual_candidates:
            raise DependencyFailure("Revit applied the model but returned no actual plan-view PNG")
        actual_view = actual_candidates[0]
        after_hash = _sha256(working_copy)
        if after_hash == before_hash:
            raise DependencyFailure("Revit returned success but the working copy is unchanged")
        topology_kinds = [item for item in topology_marker.group(3).split(",") if item]
        topology_rows = [
            {
                "id": key,
                "kind": actual_refs[key],
                "joined_pairs": actual_ref_counts[key],
            }
            for key in sorted(actual_refs)
        ]
        topology_payload = {
            "junction_count": int(topology_marker.group(1)),
            "joined_pairs": int(topology_marker.group(2)),
            "snapped_endpoint_count": snapped_endpoint_count,
            "maximum_snap_mm": maximum_topology_snap_mm,
            "kinds": topology_kinds,
            "junctions": topology_rows,
        }
        result = BridgeResult(
            status="executed", build_id=request.build_id, dry_run=False,
            message="approved WallModel applied to an isolated RVT and independently rendered",
            output_path=str(working_copy),
            details={
                "before_sha256": before_hash,
                "after_sha256": after_hash,
                "wall_model_sha256": model_hash,
                "compiler": WALL_COMPILER_VERSION,
                "compiled_plan_sha256": plan_hash,
                "source_model_sha256": current_source_hash,
                "actual_view_path": str(actual_view),
                "actual_view_sha256": _sha256(actual_view),
                "output_path": str(working_copy),
                "wall_count": wall_count,
                "column_count": column_count,
                "beam_count": beam_count,
                "grid_count": grid_count,
                "opening_count": opening_count,
                "matched_opening_count": matched_opening_count,
                "opening_host_count": opening_host_count,
                "opening_semantics": opening_semantics,
                "quantity_takeoff": quantity_takeoff,
                "element_properties": element_properties,
                "target_model_path": normalize_target_path(str(source)),
                "tenant_id": wall_model.tenant_id,
                "project_id": wall_model.project_id,
                "created_element_ids": created_element_ids,
                "created_column_ids": created_column_ids,
                "created_beam_ids": created_beam_ids,
                "readback": {
                    "wall_count": marker_wall_count,
                    "column_count": marker_column_count,
                    "beam_count": marker_beam_count,
                    "grid_count": marker_grid_count,
                    "opening_count": opening_count,
                    "matched_opening_count": matched_opening_count,
                    "opening_host_count": opening_host_count,
                    "opening_semantics": opening_semantics,
                    "quantity_takeoff": quantity_takeoff,
                    "element_properties": element_properties,
                    "source_model_sha256": current_source_hash,
                    "before_sha256": before_hash,
                    "after_sha256": after_hash,
                    "output_path": str(working_copy),
                    "actual_view_path": str(actual_view),
                    "actual_view_sha256": _sha256(actual_view),
                    "wall_model_sha256": model_hash,
                    "compiler": WALL_COMPILER_VERSION,
                    "compiled_plan_sha256": plan_hash,
                    "target_model_path": normalize_target_path(str(source)),
                    "tenant_id": wall_model.tenant_id,
                    "project_id": wall_model.project_id,
                    "topology": topology_payload,
                    "geometry_readback": geometry_readback,
                    "created_column_ids": created_column_ids,
                    "created_beam_ids": created_beam_ids,
                },
                "topology": topology_payload,
                "coordinate_transform": coordinate_readback,
                "geometry_readback": geometry_readback,
                "convergence_round": request.convergence_round,
                "revit_response": response_text[:2_000],
            },
        )
        _write_delivery_receipt(
            receipt_path,
            build_id=request.build_id,
            wall_model_sha256=model_hash,
            source_model_sha256=current_source_hash,
            approval_digest=approval_binding,
            target_model_path=source,
            output_path=working_copy,
            actual_view_path=actual_view,
            after_sha256=after_hash,
            actual_view_sha256=result.details["actual_view_sha256"],
            details=result.details,
        )
        return result

    async def present_wall_model(self, request: PresentWallModelRequest) -> BridgeResult:
        """Open one verified delivery and focus Revit on its audited plan view."""

        work_dir = self._work_dir(request.build_id)
        receipt = _read_delivery_receipt(work_dir / "delivery_receipt.json")
        if receipt is None:
            raise ValidationFailure("Revit delivery receipt does not exist")
        if receipt.get("build_id") != request.build_id:
            raise PolicyFailure("Revit delivery receipt build_id does not match the request")
        output_path = _receipt_artifact_path(
            receipt.get("output_path"), work_dir, suffix=".rvt"
        )
        if output_path is None or not output_path.is_file():
            raise ValidationFailure("verified Revit delivery RVT is missing")
        output_hash = _sha256(output_path)
        if (
            output_hash != request.output_sha256
            or output_hash != receipt.get("after_sha256")
        ):
            raise PolicyFailure("Revit delivery RVT hash does not match the audited output")
        details = receipt.get("details") or {}
        if details.get("after_sha256") != output_hash:
            raise PolicyFailure("Revit delivery detail hash does not match the audited output")
        actual_view_path = _receipt_artifact_path(
            receipt.get("actual_view_path"), work_dir, suffix=".png"
        )
        if actual_view_path is None or not actual_view_path.is_file():
            raise ValidationFailure("verified Revit delivery view is missing")
        view_name = _receipt_delivery_view_name(actual_view_path)
        if request.view == "3d":
            view_name = "BM-3D-" + view_name.removeprefix("BM-ACTUAL-")
        expected_counts = {
            "walls": _receipt_count(details, "wall_count"),
            "columns": _receipt_count(details, "column_count"),
            "grids": _receipt_count(details, "grid_count"),
        }
        # Revit does not expose plan-only grid datums as visible elements in
        # a 3D view; walls and columns remain the authoritative 3D check.
        if request.view == "3d":
            expected_counts["grids"] = 0
        presentation_response: dict
        try:
            async with RevitMCPClient(
                timeout=self.settings.revit_bridge_timeout_seconds
            ) as client:
                active_response = await asyncio.wait_for(
                    client.call_tool(
                        "execute_revit_code",
                        {
                            "code": "print('BUILDMATE_ACTIVE_DOCUMENT_PATH=' + str(doc.PathName or ''))",
                            "description": "BuildMate inspect active delivery document",
                        },
                    ),
                    timeout=self.settings.revit_bridge_timeout_seconds,
                )
                active_text = _require_success_response(active_response)
                active_match = re.search(
                    r"(?m)^BUILDMATE_ACTIVE_DOCUMENT_PATH=(.*)$", active_text
                )
                active_path = active_match.group(1).strip() if active_match else ""
                try:
                    active_target = normalize_target_path(active_path)
                except ValueError:
                    active_target = ""
                if active_target != normalize_target_path(str(output_path)):
                    open_response = await asyncio.wait_for(
                        client.call_tool(
                            "open_document",
                            {
                                "file_path": str(output_path),
                                "detach": False,
                                "audit": False,
                            },
                        ),
                        timeout=self.settings.revit_bridge_timeout_seconds,
                    )
                    _require_success_response(open_response)
                presentation_response = await asyncio.wait_for(
                    client.call_tool(
                        "execute_revit_code",
                        {
                            "code": _compile_delivery_presentation_script(
                                output_path,
                                view_name=view_name,
                                wall_count=expected_counts["walls"],
                                column_count=expected_counts["columns"],
                                grid_count=expected_counts["grids"],
                                view_kind=request.view,
                            ),
                            "description": "BuildMate present audited wall delivery",
                        },
                    ),
                    timeout=self.settings.revit_bridge_timeout_seconds,
                )
            response_text = _require_success_response(presentation_response)
        except (RevitMCPError, asyncio.TimeoutError, DependencyFailure) as exc:
            if "Error: 502 -" not in str(exc):
                raise DependencyFailure(
                    f"Revit delivery presentation failed: {str(exc)[:300]}"
                ) from exc
            presentation_response = await _present_wall_model_via_routes(
                output_path,
                view_name=view_name,
                expected_counts=expected_counts,
                timeout=self.settings.revit_bridge_timeout_seconds,
                view_kind=request.view,
            )
            response_text = _require_success_response(presentation_response)
        marker = re.search(
            r"(?m)^BUILDMATE_DELIVERY_PRESENTED view=(\S+) walls=(\d+) columns=(\d+) grids=(\d+)$",
            response_text,
        )
        if marker is None:
            raise DependencyFailure("Revit did not confirm presenting the delivery view")
        actual_counts = {
            "walls": int(marker.group(2)),
            "columns": int(marker.group(3)),
            "grids": int(marker.group(4)),
        }
        if marker.group(1) != view_name or any(
            actual_counts[key] < expected_counts[key] for key in expected_counts
        ):
            raise DependencyFailure(
                "Revit delivery presentation is missing audited elements: "
                f"actual={actual_counts} expected_at_least={expected_counts}"
            )
        return BridgeResult(
            status="presented",
            build_id=request.build_id,
            message="audited Revit delivery opened and focused in the running Revit instance",
            output_path=str(output_path),
            details={
                "output_sha256": output_hash,
                "view_name": view_name,
                "visible_counts": actual_counts,
            },
        )

    async def import_ifc(self, request: ImportIFCRequest) -> BridgeResult:
        source = Path(request.ifc_path).resolve()
        if not source.is_file():
            raise ValidationFailure("source IFC does not exist")
        if request.dry_run:
            return BridgeResult(
                status="validated", build_id=request.build_id, dry_run=True,
                message="IFC input validated; Revit was not started",
                details={"source_sha256": _sha256(source), "convergence_round": request.convergence_round},
            )
        self._require_execution_approval(
            request.approval_token, build_id=request.build_id, action="import_ifc_to_rvt"
        )
        work_dir = self._work_dir(request.build_id)
        try:
            async with RevitMCPClient(timeout=self.settings.revit_bridge_timeout_seconds) as client:
                output = await asyncio.wait_for(
                    client.convert_ifc_to_rvt(str(source), str(work_dir)),
                    timeout=self.settings.revit_bridge_timeout_seconds,
                )
        except (RevitMCPError, asyncio.TimeoutError) as exc:
            raise DependencyFailure(f"IFC import failed: {str(exc)[:300]}") from exc
        output_path = Path(output)
        if not output_path.is_file():
            raise DependencyFailure("Revit reported success without an RVT output")
        return BridgeResult(
            status="executed", build_id=request.build_id,
            message="IFC imported into an isolated RVT output",
            output_path=str(output_path),
            details={
                "output_sha256": _sha256(output_path),
                "convergence_round": request.convergence_round,
            },
        )

    def diff(self, request: DiffRequest) -> BridgeResult:
        before = Path(request.before_path).resolve()
        after = Path(request.after_path).resolve()
        if not before.is_file() or not after.is_file():
            raise ValidationFailure("both model files must exist")
        before_hash, after_hash = _sha256(before), _sha256(after)
        return BridgeResult(
            status="different" if before_hash != after_hash else "identical",
            message="binary snapshot comparison completed; element-level diff requires Revit read-back",
            details={
                "before_sha256": before_hash,
                "after_sha256": after_hash,
                "before_size": before.stat().st_size,
                "after_size": after.stat().st_size,
            },
        )


def _transaction_violation():
    from workers.revit_bridge.security import ScriptViolation
    return ScriptViolation(1, "transaction_required", "modifying scripts require an explicit Revit Transaction")


def _require_success_response(response: dict) -> str:
    """Reject formatted MCP error envelopes before inspecting success markers.

    The Revit MCP server includes the attempted source code in diagnostic
    errors.  A plain substring search could therefore mistake an echoed
    success marker for a completed transaction.
    """

    if not isinstance(response, dict):
        raise DependencyFailure("Revit returned an invalid response envelope")
    raw = response.get("raw")
    if isinstance(raw, dict) and raw.get("isError"):
        raise DependencyFailure("Revit MCP returned an error response")
    text = response.get("text")
    if not isinstance(text, str) or not text.strip():
        raise DependencyFailure("Revit returned an empty response")
    execution_error = re.search(
        r"(?m)^BUILDMATE_EXECUTION_ERROR\s+type=([^\s]+)\s+message=(.*)$",
        text,
    )
    if execution_error is not None:
        raise DependencyFailure(
            "Revit execution failed: %s: %s" % (
                execution_error.group(1)[:120],
                execution_error.group(2)[:350],
            )
        )
    if (
        "=== ERROR DETAILS ===" in text
        or re.search(r"(?m)^\s*Status:\s*error\b", text)
        or "Error during code execution" in text
    ):
        raise DependencyFailure("Revit MCP returned an error response")
    # The pyRevit Routes adapter used by Revit 2020 can return an HTTP error
    # as ordinary text (with ``raw.isError == false``).  Treat that envelope
    # as a failure before looking for delivery markers; otherwise an error's
    # echoed source code can be mistaken for a successful write and the real
    # Revit exception is lost.
    if re.match(r"^\s*Error:\s*\d{3}\s*-", text):
        detail = text
        try:
            payload = json.loads(text.split(" - ", 1)[1])
            if isinstance(payload, dict):
                detail = str(payload.get("error") or payload.get("message") or detail)
                partial = payload.get("partial_output")
                if partial:
                    detail += "; partial_output=" + str(partial)[:300]
        except (IndexError, TypeError, ValueError, json.JSONDecodeError):
            pass
        raise DependencyFailure(
            "Revit MCP returned an error response: " + detail[:500]
        )
    return text


def _is_opaque_route_502(response: dict) -> bool:
    if not isinstance(response, dict):
        return False
    text = response.get("text")
    return isinstance(text, str) and bool(
        re.fullmatch(r"\s*Error:\s*502\s*-\s*", text)
    )


async def _present_wall_model_via_routes(
    output_path: Path,
    *,
    view_name: str,
    expected_counts: dict[str, int],
    timeout: float,
    view_kind: str = "plan",
) -> dict:
    active_response = await _execute_revit_route(
        "if doc is None:\n"
        "    print('BUILDMATE_ACTIVE_DOCUMENT_PATH=')\n"
        "else:\n"
        "    print('BUILDMATE_ACTIVE_DOCUMENT_PATH=' + str(doc.PathName or ''))",
        "BuildMate inspect active delivery document",
        timeout,
    )
    active_text = _require_success_response(active_response)
    active_match = re.search(r"(?m)^BUILDMATE_ACTIVE_DOCUMENT_PATH=(.*)$", active_text)
    active_path = active_match.group(1).strip() if active_match else ""
    try:
        active_target = normalize_target_path(active_path)
    except ValueError:
        active_target = ""
    if active_target != normalize_target_path(str(output_path)):
        open_response = await _open_revit_document_route(output_path, timeout)
        _require_success_response(open_response)
    return await _execute_revit_route(
        _compile_delivery_presentation_script(
            output_path,
            view_name=view_name,
            wall_count=expected_counts["walls"],
            column_count=expected_counts["columns"],
            grid_count=expected_counts["grids"],
            view_kind=view_kind,
        ),
        "BuildMate present audited wall delivery",
        timeout,
    )


def _compile_wall_model_script(
    compiled: dict,
    *,
    actual_prefix: Path,
    expected_wall_count: int,
) -> str:
    """Generate the small, deterministic Revit program for a WallModel."""
    elements = compiled.get("model_elements") or []
    grids = compiled.get("grids") or []
    level = ((compiled.get("project") or {}).get("levels") or [{}])[0]
    payload = {
        "project_id": (compiled.get("project") or {}).get("project_id", ""),
        "tenant_id": (compiled.get("project") or {}).get("tenant_id", ""),
        "level_name": level.get("name", ""),
        "level_elevation_mm": float(level.get("elevation") or 0.0),
        "floor_code": (compiled.get("build") or {}).get("floor_code", ""),
        "elements": elements,
        "grids": grids,
    }
    literal = repr(payload)
    if len(literal) > 1_500_000:
        raise ValidationFailure("compiled WallModel script exceeds 1.5 MB")
    prefix = str(Path(actual_prefix).resolve())
    return f'''from Autodesk.Revit.DB import *
from System.Collections.Generic import List
import math

DATA = {literal}
MM_PER_FOOT = 304.8
PROJECT_ID = {payload["project_id"]!r}
FLOOR_CODE = {payload["floor_code"]!r}
ACTUAL_PREFIX = {prefix!r}

def _name(item):
    try:
        return Element.Name.GetValue(item)
    except Exception:
        return ""

def _level(doc):
    wanted = DATA.get("level_name") or ""
    for item in FilteredElementCollector(doc).OfClass(Level):
        if _name(item) == wanted:
            return item
    raise Exception("Revit level not found: " + wanted)

def _wall_type(doc, thickness_mm):
    target = float(thickness_mm or 0.0) / MM_PER_FOOT
    basic = []
    for item in FilteredElementCollector(doc).OfClass(WallType):
        try:
            if item.FamilyName in ("Basic Wall", "基本墙"):
                basic.append(item)
                if target > 0.0 and abs(float(item.Width) - target) < (5.0 / MM_PER_FOOT):
                    return item
        except Exception:
            pass
    if not basic:
        raise Exception("no Basic Wall type exists in target model")
    base = basic[0]
    if target <= 0.0:
        return base
    duplicate_name = "BM_Wall_%dmm" % round(float(thickness_mm))
    candidate = None
    for item in basic:
        if _name(item) == duplicate_name:
            candidate = item
            break
    if candidate is None:
        candidate = base.Duplicate(duplicate_name)
    compound = candidate.GetCompoundStructure()
    if compound is None or compound.LayerCount <= 0:
        raise Exception("wall type has no editable compound structure")
    widths = [compound.GetLayerWidth(index) for index in range(compound.LayerCount)]
    layer = max(range(len(widths)), key=lambda index: widths[index])
    new_width = widths[layer] + target - sum(widths)
    if new_width <= 0.001:
        raise Exception("requested wall thickness is incompatible with wall layers")
    compound.SetLayerWidth(layer, new_width)
    candidate.SetCompoundStructure(compound)
    doc.Regenerate()
    if abs(float(candidate.Width) - target) >= (5.0 / MM_PER_FOOT):
        raise Exception("wall type thickness read-back mismatch")
    return candidate

def _line(item, z_mm=0.0):
    start = item["start"]
    end = item["end"]
    return Line.CreateBound(
        XYZ(float(start[0]) / MM_PER_FOOT, float(start[1]) / MM_PER_FOOT, (float(start[2]) + z_mm) / MM_PER_FOOT),
        XYZ(float(end[0]) / MM_PER_FOOT, float(end[1]) / MM_PER_FOOT, (float(end[2]) + z_mm) / MM_PER_FOOT),
    )

level = None
created_walls = []
created_grids = []
transaction = Transaction(doc, "BuildMate approved WallModel")
transaction.Start()
try:
    level = _level(doc)
    level_z = float(level.Elevation) * MM_PER_FOOT
    for grid_set in DATA.get("grids") or []:
        labels_x = grid_set.get("x_axis_labels") or []
        labels_y = grid_set.get("y_axis_labels") or []
        x_values = [float(value) for value in (grid_set.get("x_axes") or [])]
        y_values = [float(value) for value in (grid_set.get("y_axes") or [])]
        x_min = (min(x_values) - 1000.0) if x_values else -1000.0
        x_max = (max(x_values) + 1000.0) if x_values else 1000.0
        y_min = (min(y_values) - 1000.0) if y_values else -1000.0
        y_max = (max(y_values) + 1000.0) if y_values else 1000.0
        for index, value in enumerate(grid_set.get("x_axes") or []):
            item = {{"start": [float(value), y_min, 0.0], "end": [float(value), y_max, 0.0]}}
            grid = Grid.Create(doc, _line(item))
            if index < len(labels_x):
                try:
                    grid.Name = labels_x[index]
                except Exception:
                    pass
            created_grids.append(grid)
        for index, value in enumerate(grid_set.get("y_axes") or []):
            item = {{"start": [x_min, float(value), 0.0], "end": [x_max, float(value), 0.0]}}
            grid = Grid.Create(doc, _line(item))
            if index < len(labels_y):
                try:
                    grid.Name = labels_y[index]
                except Exception:
                    pass
            created_grids.append(grid)
    for item in DATA.get("elements") or []:
        if item.get("type") != "Wall":
            continue
        line = _line(item, level_z)
        if line.Length <= 0.01:
            raise Exception("degenerate wall: " + str(item.get("id")))
        wall = Wall.Create(doc, line, level.Id, False)
        WallUtils.DisallowWallJoinAtEnd(wall, 0)
        WallUtils.DisallowWallJoinAtEnd(wall, 1)
        wall.Location.Curve = line
        wall.ChangeTypeId(_wall_type(doc, item.get("thickness")).Id)
        height = float(item.get("height") or 0.0) / MM_PER_FOOT
        parameter = wall.get_Parameter(BuiltInParameter.WALL_USER_HEIGHT_PARAM)
        if parameter is None or parameter.IsReadOnly:
            raise Exception("wall height parameter is unavailable: " + str(item.get("id")))
        parameter.Set(height)
        marker = "BUILDMATE_AUTO:P=%s;F=%s;I=%s" % (PROJECT_ID, FLOOR_CODE, item.get("id"))
        comments = wall.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
        if comments is not None and not comments.IsReadOnly:
            comments.Set(marker)
        created_walls.append((wall, item))
    if len(created_walls) != {expected_wall_count}:
        raise Exception("wall count mismatch: expected %d actual %d" % ({expected_wall_count}, len(created_walls)))
    for wall, item in created_walls:
        curve = wall.Location.Curve
        actual_start = curve.GetEndPoint(0)
        actual_end = curve.GetEndPoint(1)
        expected_start = item["start"]
        expected_end = item["end"]
        direct_error = max(
            abs(actual_start.X * MM_PER_FOOT - expected_start[0]),
            abs(actual_start.Y * MM_PER_FOOT - expected_start[1]),
            abs(actual_end.X * MM_PER_FOOT - expected_end[0]),
            abs(actual_end.Y * MM_PER_FOOT - expected_end[1]),
        )
        reverse_error = max(
            abs(actual_start.X * MM_PER_FOOT - expected_end[0]),
            abs(actual_start.Y * MM_PER_FOOT - expected_end[1]),
            abs(actual_end.X * MM_PER_FOOT - expected_start[0]),
            abs(actual_end.Y * MM_PER_FOOT - expected_start[1]),
        )
        if min(direct_error, reverse_error) > 5.0:
            raise Exception("wall geometry read-back mismatch: " + str(item.get("id")))
        expected_width = float(item.get("thickness") or 0.0)
        if expected_width > 0.0 and abs(float(wall.WallType.Width) * MM_PER_FOOT - expected_width) > 5.0:
            raise Exception("wall thickness read-back mismatch: " + str(item.get("id")))
    transaction.Commit()
except Exception:
    try:
        transaction.RollBack()
    except Exception:
        pass
    raise

doc.Regenerate()
view = None
view_name = "BM-ACTUAL-" + FLOOR_CODE
for candidate in FilteredElementCollector(doc).OfClass(ViewPlan):
    if candidate.IsTemplate:
        continue
    try:
        if _name(candidate) == view_name:
            view = candidate
            break
    except Exception:
        pass
if view is None:
    view_type = None
    for candidate in FilteredElementCollector(doc).OfClass(ViewFamilyType):
        try:
            if candidate.ViewFamily == ViewFamily.FloorPlan:
                view_type = candidate
                break
        except Exception:
            pass
    if view_type is None:
        raise Exception("no floor-plan view type for actual audit")
    view_transaction = Transaction(doc, "BuildMate actual audit view")
    view_transaction.Start()
    try:
        view = ViewPlan.Create(doc, view_type.Id, level.Id)
        view.Name = view_name
        view_transaction.Commit()
    except Exception:
        try:
            view_transaction.RollBack()
        except Exception:
            pass
        raise
options = ImageExportOptions()
options.ExportRange = ExportRange.SetOfViews
view_ids = List[ElementId]()
view_ids.Add(view.Id)
options.SetViewsAndSheets(view_ids)
options.FilePath = ACTUAL_PREFIX
options.HLRandWFViewsFileType = ImageFileType.PNG
options.ImageResolution = ImageResolution.DPI_150
options.ZoomType = ZoomFitType.FitToPage
options.PixelSize = 2400
doc.ExportImage(options)
    print("BUILDMATE_WALL_MODEL_APPLIED walls=%d grids=%d element_ids=%s" % (
    len(created_walls), len(created_grids),
    ",".join([str(wall.Id.IntegerValue) for wall, item in created_walls])))
'''


# Compatibility export for callers/tests that imported the former private
# helper.  The typed compiler above is the only implementation used at run
# time; the legacy body remains below solely to avoid a breaking import while
# older extensions migrate.
_compile_wall_model_script = _typed_compile_wall_model_script



def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _restore_working_copy(source: Path, working_copy: Path) -> None:
    """Restore a failed isolated attempt without exposing partial RVT bytes.

    ``os.replace`` makes the recovery snapshot atomic on the local Windows
    volume.  If Revit still holds the file open, remove the temporary file and
    let the caller surface the original delivery failure; an unverified copy
    must never be returned as a successful artifact.
    """

    source = source.resolve()
    working_copy = working_copy.resolve()
    if source == working_copy:
        raise ValueError("source RVT cannot be the Bridge working-copy path")
    if not source.is_file():
        raise FileNotFoundError(source)
    temporary = working_copy.with_name(working_copy.name + ".rollback.tmp")
    try:
        if temporary.exists():
            temporary.unlink()
        shutil.copy2(source, temporary)
        _ensure_writable_copy(temporary)
        # Replacing a read-only destination can fail on Windows even when the
        # directory itself is writable.  Clear only the isolated copy's bit;
        # the approved source snapshot is never modified.
        if working_copy.exists():
            _ensure_writable_copy(working_copy)
        os.replace(temporary, working_copy)
    finally:
        if temporary.exists():
            temporary.unlink()


def _ensure_writable_copy(path: Path) -> None:
    """Clear a source file's DOS read-only bit on an isolated RVT copy.

    Autodesk sample RVTs are commonly distributed read-only.  ``shutil.copy2``
    intentionally preserves that metadata, but Revit must save the working
    copy during a real delivery.  This helper is scoped to generated files and
    never changes the approved source model.
    """

    try:
        current_mode = path.stat().st_mode
        path.chmod(current_mode | stat.S_IWRITE)
    except OSError as exc:
        raise DependencyFailure(
            "Revit working copy is not writable: " + str(path)
        ) from exc


def _details_sha256(details: dict) -> str:
    """Hash receipt details using a stable JSON representation."""

    encoded = json.dumps(
        details,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _receipt_details_match_identity(
    details: dict,
    *,
    wall_model_sha256: str,
    source_model_sha256: str,
    target_model_path: str,
    output_path: Path,
    actual_view_path: Path,
    after_sha256: object,
    actual_view_sha256: object,
) -> bool:
    """Ensure nested receipt facts cannot contradict top-level identity."""

    try:
        detail_target = normalize_target_path(str(details.get("target_model_path") or ""))
    except ValueError:
        return False
    if detail_target != target_model_path:
        return False
    if details.get("wall_model_sha256") != wall_model_sha256:
        return False
    if details.get("source_model_sha256") != source_model_sha256:
        return False
    if details.get("after_sha256") != after_sha256:
        return False
    if details.get("actual_view_sha256") != actual_view_sha256:
        return False
    try:
        if Path(str(details.get("output_path") or "")).resolve() != output_path.resolve():
            return False
        if Path(str(details.get("actual_view_path") or "")).resolve() != actual_view_path.resolve():
            return False
    except (OSError, RuntimeError):
        return False
    readback = details.get("readback")
    if not isinstance(readback, dict):
        return False
    if (
        readback.get("wall_model_sha256") != wall_model_sha256
        or readback.get("source_model_sha256") != source_model_sha256
        or readback.get("after_sha256") != after_sha256
        or readback.get("target_model_path") != target_model_path
    ):
        return False
    return True


def _read_delivery_receipt(path: Path) -> dict | None:
    """Read a versioned completion receipt, ignoring stale legacy data.

    Receipts written before target binding was introduced do not contain the
    new identity field and are treated as stale so the next attempt can
    safely rebuild the scoped working copy.
    """

    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    # Missing target_model_path identifies a receipt from the pre-binding
    # contract.  It is intentionally ignored for backwards-compatible
    # re-execution instead of being trusted for idempotent replay.
    if "target_model_path" not in value:
        return None
    required_hashes = (
        "wall_model_sha256",
        "source_model_sha256",
        "approval_digest",
        "after_sha256",
        "actual_view_sha256",
    )
    # Keep a clearly legacy/non-string target shape compatible with old files;
    # it cannot prove a completed target-bound delivery.  Once a valid target
    # binding exists, malformed identity fields must fail closed: treating
    # them as stale could execute the same approved write twice.
    if not isinstance(value.get("target_model_path"), str) or not value["target_model_path"].strip():
        return None
    if any(
        not isinstance(value.get(name), str)
        or re.fullmatch(r"[a-f0-9]{64}", value[name]) is None
        for name in required_hashes
    ):
        raise PolicyFailure(
            "Revit delivery receipt has invalid target-bound identity fields"
        )
    if not isinstance(value.get("output_path"), str) or not value["output_path"].strip():
        raise PolicyFailure("Revit delivery receipt output path is invalid")
    if not isinstance(value.get("actual_view_path"), str) or not value["actual_view_path"].strip():
        raise PolicyFailure("Revit delivery receipt actual-view path is invalid")
    if not isinstance(value.get("details"), dict):
        raise PolicyFailure("Revit delivery receipt details are invalid")
    details_digest = value.get("details_sha256")
    if not isinstance(details_digest, str) or re.fullmatch(
        r"[a-f0-9]{64}", details_digest
    ) is None:
        # A target-bound receipt without the integrity field is not safely
        # replayable.  Require operator inspection instead of risking a
        # duplicate Revit write.
        raise PolicyFailure(
            "Revit delivery receipt detail integrity field is missing or invalid"
        )
    if details_digest != _details_sha256(value["details"]):
        # A corrupted receipt must never be considered stale: silently
        # rebuilding would turn a durable at-least-once delivery into a
        # possible second Revit write.
        raise PolicyFailure(
            "Revit delivery receipt detail integrity check failed"
        )
    return value


def _receipt_artifact_path(
    value: object,
    work_dir: Path,
    *,
    suffix: str,
) -> Path | None:
    """Return a receipt artifact path only when it stays in ``work_dir``."""

    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value).expanduser()
    if not path.is_absolute() or path.suffix.lower() != suffix.lower():
        return None
    try:
        resolved = path.resolve(strict=False)
        root = work_dir.resolve(strict=False)
    except OSError:
        return None
    if resolved != root and root not in resolved.parents:
        return None
    return resolved


def _receipt_detail_paths_are_scoped(value: object, work_dir: Path) -> bool:
    """Validate path-like fields nested in the immutable receipt details."""

    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = str(key).lower()
            # The approved source/target RVT is intentionally outside the
            # per-build output directory.  It is checked against the signed
            # target identity separately; only generated artifact paths must
            # remain scoped to ``work_dir``.
            if normalized_key == "target_model_path":
                continue
            if normalized_key.endswith("_path") or normalized_key == "path":
                if _receipt_artifact_path(item, work_dir, suffix=Path(str(item or "")).suffix) is None:
                    return False
            elif not _receipt_detail_paths_are_scoped(item, work_dir):
                return False
        return True
    if isinstance(value, list):
        return all(_receipt_detail_paths_are_scoped(item, work_dir) for item in value)
    return True


def _write_delivery_receipt(
    path: Path,
    *,
    build_id: str | None = None,
    wall_model_sha256: str,
    source_model_sha256: str,
    approval_digest: str,
    target_model_path: Path,
    output_path: Path,
    actual_view_path: Path,
    after_sha256: str,
    actual_view_sha256: str,
    details: dict,
) -> None:
    """Atomically persist the idempotency identity after all read-back gates."""

    payload = {
        **({"build_id": build_id} if build_id else {}),
        "wall_model_sha256": wall_model_sha256,
        "source_model_sha256": source_model_sha256,
        "approval_digest": approval_digest,
        "target_model_path": normalize_target_path(str(target_model_path)),
        "output_path": str(output_path.resolve()),
        "actual_view_path": str(actual_view_path.resolve()),
        "after_sha256": after_sha256,
        "actual_view_sha256": actual_view_sha256,
        "details": details,
        "details_sha256": _details_sha256(details),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _receipt_delivery_view_name(actual_view_path: Path) -> str:
    marker = "BM-ACTUAL-"
    stem = actual_view_path.stem
    offset = stem.rfind(marker)
    if offset < 0:
        raise PolicyFailure("Revit delivery receipt has no BuildMate delivery view")
    view_name = stem[offset:].strip()
    if not view_name or len(view_name) > 255 or any(char in view_name for char in "\r\n"):
        raise PolicyFailure("Revit delivery receipt has an invalid delivery view name")
    return view_name


def _receipt_count(details: dict, field: str) -> int:
    value = details.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PolicyFailure(f"Revit delivery receipt has an invalid {field}")
    return value


def _compile_delivery_presentation_script(
    output_path: Path,
    *,
    view_name: str,
    wall_count: int,
    column_count: int,
    grid_count: int,
    view_kind: str = "plan",
) -> str:
    """Generate the fixed UI-only program used after the independent audit."""

    return f'''import os
from System.Collections.Generic import List
EXPECTED_PATH = {str(output_path.resolve())!r}
VIEW_NAME = {view_name!r}
VIEW_KIND = {view_kind!r}
EXPECTED_WALLS = {wall_count}
EXPECTED_COLUMNS = {column_count}
EXPECTED_GRIDS = {grid_count}

def _normalized_path(value):
    return os.path.normcase(os.path.abspath(str(value or "")))

if doc is None:
    raise Exception("Revit document is unavailable after opening the audited delivery")
if _normalized_path(doc.PathName) != _normalized_path(EXPECTED_PATH):
    raise Exception("active Revit document is not the audited delivery")
if uidoc is None:
    raise Exception("Revit UI document is unavailable after opening the audited delivery")

target = None
for candidate in DB.FilteredElementCollector(doc).OfClass(DB.View):
    if not candidate.IsTemplate and candidate.Name == VIEW_NAME:
        target = candidate
        break
if target is None:
    raise Exception("BuildMate delivery view not found: " + VIEW_NAME)

# A user can leave the delivery view with temporary isolate or categories
# hidden after inspecting the model.  Restore the four delivery categories
# before counting/activating so a later "present" call cannot show a grid-only
# view while the RVT itself still contains the audited elements.
visibility_tx = DB.Transaction(doc, "BuildMate restore delivery visibility")
visibility_tx.Start()
try:
    if VIEW_KIND == "3d":
        # Recreate the operator-facing 3D view to clear any stale permanent
        # hide/isolate state saved by an earlier Revit session.
        try:
            doc.Delete(target.Id)
            family_type = next(
                item for item in DB.FilteredElementCollector(doc).OfClass(DB.ViewFamilyType)
                if item.ViewFamily == DB.ViewFamily.ThreeDimensional
            )
            target = DB.View3D.CreateIsometric(doc, family_type.Id)
            target.Name = VIEW_NAME
        except Exception:
            raise Exception("unable to recreate BuildMate 3D delivery view")
    try:
        if target.IsInTemporaryViewMode(DB.TemporaryViewMode.TemporaryHideIsolate):
            target.DisableTemporaryViewMode(DB.TemporaryViewMode.TemporaryHideIsolate)
    except Exception:
        pass
    for category in (
        DB.BuiltInCategory.OST_Walls,
        DB.BuiltInCategory.OST_StructuralColumns,
        DB.BuiltInCategory.OST_StructuralFraming,
        DB.BuiltInCategory.OST_Grids,
    ):
        try:
            if target.CanCategoryBeHidden(DB.ElementId(category)):
                target.SetCategoryHidden(DB.ElementId(category), False)
        except Exception:
            pass
    if VIEW_KIND != "3d":
        # Keep plan views as linework only: black projection/cut edges and no
        # surface or cut fill.  This is applied on every presentation call so
        # an older delivery cannot retain a stale shaded override.
        try:
            target.ViewTemplateId = DB.ElementId.InvalidElementId
            target.DisplayStyle = DB.DisplayStyle.Wireframe
            line_style = DB.OverrideGraphicSettings()
            line_style.SetProjectionLineColor(DB.Color(0, 0, 0))
            line_style.SetCutLineColor(DB.Color(0, 0, 0))
            line_style.SetSurfaceForegroundPatternVisible(False)
            line_style.SetSurfaceBackgroundPatternVisible(False)
            line_style.SetCutForegroundPatternVisible(False)
            line_style.SetCutBackgroundPatternVisible(False)
            for category in (
                DB.BuiltInCategory.OST_Walls,
                DB.BuiltInCategory.OST_StructuralColumns,
                DB.BuiltInCategory.OST_StructuralFraming,
            ):
                for element in DB.FilteredElementCollector(doc, target.Id).OfCategory(category).WhereElementIsNotElementType():
                    target.SetElementOverrides(element.Id, line_style)
        except Exception:
            # Graphics are presentation-only; audited element checks below
            # remain authoritative if a template lacks an override capability.
            pass
    if VIEW_KIND == "3d":
        # Recreate the 3D visibility set.  Older deliveries converted a
        # temporary isolate to a permanent one before all elements existed,
        # which leaves a perfectly valid RVT with an empty-looking 3D view.
        visible_ids = List[DB.ElementId]()
        for category in (
            DB.BuiltInCategory.OST_Walls,
            DB.BuiltInCategory.OST_StructuralColumns,
            DB.BuiltInCategory.OST_StructuralFraming,
        ):
            for element in DB.FilteredElementCollector(doc).OfCategory(category).WhereElementIsNotElementType():
                visible_ids.Add(element.Id)
        if visible_ids.Count:
            try:
                target.IsolateElementsTemporary(visible_ids)
                target.ConvertTemporaryHideIsolateToPermanent()
            except Exception:
                pass
    # The operator may have left a stale/empty crop region in the saved view.
    # Delivery presentation must show the model rather than only the grids;
    # the independent audit already used its own immutable render, so it is
    # safe to disable the presentation crop here and fit the UI afterwards.
    try:
        if VIEW_KIND == "3d":
            target.IsSectionBoxActive = False
        else:
            target.CropBoxActive = False
            target.CropBoxVisible = False
    except Exception:
        pass
    doc.Regenerate()
    visibility_tx.Commit()
except Exception:
    try:
        visibility_tx.RollBack()
    except Exception:
        pass
    raise

def _visible_count(category):
    return DB.FilteredElementCollector(doc, target.Id).OfCategory(category).WhereElementIsNotElementType().GetElementCount()

actual_walls = _visible_count(DB.BuiltInCategory.OST_Walls)
actual_columns = _visible_count(DB.BuiltInCategory.OST_StructuralColumns)
actual_grids = _visible_count(DB.BuiltInCategory.OST_Grids)
# A delivery copy can contain a small number of pre-existing template
# elements (or elements left by an interrupted previous attempt).  They do
# not invalidate the audited BuildMate elements.  What must never happen is
# that an audited category is missing from the active view.
if actual_walls < EXPECTED_WALLS or actual_columns < EXPECTED_COLUMNS or actual_grids < EXPECTED_GRIDS:
    raise Exception(
        "delivery view is missing audited elements: "
        "actual walls=%d columns=%d grids=%d expected at least walls=%d columns=%d grids=%d"
        % (actual_walls, actual_columns, actual_grids,
           EXPECTED_WALLS, EXPECTED_COLUMNS, EXPECTED_GRIDS)
    )

uidoc.ActiveView = target
uidoc.RefreshActiveView()
zoomed = False
for ui_view in uidoc.GetOpenUIViews():
    if ui_view.ViewId == target.Id:
        crop = target.CropBox
        ui_view.ZoomAndCenterRectangle(crop.Min, crop.Max)
        zoomed = True
        break
if not zoomed:
    raise Exception("BuildMate delivery UI view is not open")
uidoc.RefreshActiveView()
print("BUILDMATE_DELIVERY_PRESENTED view=%s walls=%d columns=%d grids=%d" % (
    VIEW_NAME, actual_walls, actual_columns, actual_grids))
'''


def _wrap_working_copy_script(script: str, working_copy: Path) -> str:
    """Bind ``doc`` to an isolated RVT and close it on every path.

    Revit 2020 can make a document active while exporting its plan view.  An
    active document cannot be closed through ``Document.Close``; on the
    successful, already-saved path we therefore post Revit's own Close command
    instead.  A failed build is never saved merely to make cleanup easier.
    """
    # The pyRevit/IronPython executor rejects ``from ... import *`` when it is
    # nested inside the wrapper's ``try`` block (``import *`` is module-scope
    # syntax there).  Hoist the compiler's fixed imports while keeping the
    # generated engineering program itself inside the guarded transaction.
    lines = script.splitlines()
    imports = []
    body = []
    for line in lines:
        stripped = line.strip()
        if stripped in {
            "from Autodesk.Revit.DB import *",
            "from Autodesk.Revit.DB.Structure import StructuralType",
            "from System.Collections.Generic import List",
            "from Autodesk.Revit.UI import RevitCommandId, PostableCommand",
            "import math",
        }:
            imports.append(stripped)
        else:
            body.append(line)
    indented = "\n".join("    " + line for line in body)
    hoisted = "\n".join(imports)
    return (
        hoisted + "\n"
        "_buildmate_source_doc = doc\n"
        "_buildmate_app = None\n"
        "if _buildmate_source_doc is not None:\n"
        "    _buildmate_app = getattr(_buildmate_source_doc, 'Application', None)\n"
        "if _buildmate_app is None:\n"
        "    _buildmate_host_app = getattr(revit, 'HOST_APP', None)\n"
        "    if _buildmate_host_app is not None:\n"
        "        _buildmate_app = getattr(_buildmate_host_app, 'app', None)\n"
        "        if _buildmate_app is None:\n"
        "            _buildmate_uiapp = getattr(_buildmate_host_app, 'uiapp', None)\n"
        "            _buildmate_app = getattr(_buildmate_uiapp, 'Application', None)\n"
        "if _buildmate_app is None:\n"
        "    raise Exception('BuildMate Revit application is unavailable; keep Revit open with pyRevit Routes enabled')\n"
        f"_buildmate_target_doc = _buildmate_app.OpenDocumentFile({str(working_copy)!r})\n"
        "if _buildmate_target_doc is None:\n"
        "    raise Exception('BuildMate could not open the isolated RVT working copy')\n"
        "doc = _buildmate_target_doc\n"
        "try:\n"
        f"{indented}\n"
        "    _buildmate_target_doc.Save()\n"
        "    print('BUILDMATE_WORKING_COPY_SAVED')\n"
        "except Exception as _buildmate_execution_error:\n"
        "    try:\n"
        "        _buildmate_target_doc.Close(False)\n"
        "    except Exception:\n"
        "        pass\n"
        "    _buildmate_error_type = type(_buildmate_execution_error).__name__\n"
        "    _buildmate_error_message = str(_buildmate_execution_error).replace('\\r', ' ').replace('\\n', ' ')[:500]\n"
        "    print('BUILDMATE_EXECUTION_ERROR type=%s message=%s' % (_buildmate_error_type, _buildmate_error_message))\n"
        "else:\n"
        "    try:\n"
        "        _buildmate_target_doc.Close(False)\n"
        "        print('BUILDMATE_WORKING_COPY_CLOSED')\n"
        "    except Exception:\n"
        "        # Revit 2020 may promote the opened document to the active UI\n"
        "        # document while exporting the actual view.  In that state\n"
        "        # Document.Close is prohibited and posting Close is\n"
        "        # asynchronous through the pyRevit executor.  The model was\n"
        "        # already saved and independently rendered above, so retain\n"
        "        # the saved document and let the operator close it in Revit.\n"
        "        print('BUILDMATE_WORKING_COPY_CLOSE_SKIPPED')\n"
    )
