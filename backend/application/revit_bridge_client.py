"""HTTP adapter for the loopback Windows Revit Bridge.

The application talks in versioned DTOs and never constructs Revit Python
source.  Transport failures are surfaced as dependency failures so the task
runtime can retry/dead-letter them instead of claiming a successful delivery.
"""

from __future__ import annotations

import asyncio

import httpx

from backend.config import get_settings
from backend.domain.errors import DependencyFailure, PolicyFailure, ValidationFailure
from workers.revit_bridge.contracts import (
    BridgeResult,
    PresentWallModelRequest,
    RunWallModelRequest,
)


class RevitBridgeClient:
    def __init__(self) -> None:
        settings = get_settings()
        self._url = settings.revit_bridge_url.rstrip("/")
        self._timeout = settings.revit_bridge_timeout_seconds

    async def run_wall_model(self, request: RunWallModelRequest) -> BridgeResult:
        return await self._post_with_retry("run-wall-model", request.model_dump(mode="json"))

    async def present_wall_model(self, request: PresentWallModelRequest) -> BridgeResult:
        return await self._post_with_retry("present-wall-model", request.model_dump(mode="json"))

    async def _post_with_retry(self, tool: str, payload: dict) -> BridgeResult:
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=self._timeout, trust_env=False) as client:
                    response = await client.post(
                        f"{self._url}/tools/{tool}",
                        json=payload,
                    )
                    if response.status_code >= 400:
                        if response.status_code in {502, 503, 504} and attempt == 0:
                            # A Bridge may be restarting Revit or its MCP
                            # child.  Retry only transport-level availability
                            # failures; policy/validation responses are
                            # deterministic and must reach the caller intact.
                            await asyncio.sleep(0)
                            continue
                        self._raise_http_failure(response)
                    return BridgeResult.model_validate(response.json())
            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                last_error = exc
                if attempt == 0:
                    await asyncio.sleep(0)
                    continue
                raise DependencyFailure(
                    f"Revit Bridge unavailable after retry: {str(exc)[:300]}"
                ) from exc
            except (httpx.HTTPError, ValueError) as exc:
                raise DependencyFailure(f"invalid Revit Bridge response: {str(exc)[:300]}") from exc
        raise DependencyFailure(f"Revit Bridge unavailable: {str(last_error)[:300]}")

    @staticmethod
    def _raise_http_failure(response: httpx.Response) -> None:
        message = response.text[:500]
        details: dict | None = None
        try:
            payload = response.json()
            error = payload.get("error") or {}
            message = str(error.get("message") or message)
            if isinstance(error.get("details"), dict):
                details = error["details"]
        except ValueError:
            pass
        exception_type: type[Exception]
        if response.status_code == 403:
            exception_type = PolicyFailure
        elif response.status_code == 422:
            exception_type = ValidationFailure
        else:
            exception_type = DependencyFailure
            message = f"Revit Bridge HTTP {response.status_code}: {message}"
        error = exception_type(message)
        if details:
            # Preserve durable rollback/side-effect metadata across the HTTP
            # boundary so the workflow failure journal can expose it.
            setattr(error, "details", details)
            if "rollback_state" in details:
                setattr(error, "rollback_state", details.get("rollback_state"))
            if "rollback_hash" in details:
                setattr(error, "rollback_hash", details.get("rollback_hash"))
        raise error


__all__ = ["RevitBridgeClient"]
