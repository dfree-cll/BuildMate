"""Database-backed local task runner.

The runner consumes the transactional outbox, so queued work survives process
restarts.  Applications register concrete workflow steps before calling run().
"""

from __future__ import annotations

import asyncio
import logging

from backend.adapters.outbox_repository import OutboxRepository
from backend.application.workflow_runtime import WorkflowRuntime
from backend.config import get_settings
from backend.domain.contracts import TaskEnvelope

logger = logging.getLogger(__name__)


class LocalDatabaseRunner:
    def __init__(self, runtime: WorkflowRuntime, outbox: OutboxRepository | None = None) -> None:
        self.runtime = runtime
        self.outbox = outbox or OutboxRepository()
        self._stop = asyncio.Event()

    async def run_once(self) -> int:
        records = await self.outbox.claim(limit=10)
        for record in records:
            try:
                envelope = TaskEnvelope.model_validate_json(record["payload"])
                await self.runtime.execute(envelope)
                await self.outbox.mark_published(
                    record["id"],
                    claim_version=record.get("version"),
                    claim_attempts=record.get("attempts"),
                )
            except Exception as exc:
                logger.exception("local_runner.task_failed", extra={"event_id": record["id"]})
                await self.outbox.release(
                    record["id"],
                    str(exc),
                    claim_version=record.get("version"),
                    claim_attempts=record.get("attempts"),
                )
        return len(records)

    async def run(self) -> None:
        delay = max(0.1, get_settings().task_runner_poll_seconds)
        while not self._stop.is_set():
            processed = await self.run_once()
            if not processed:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass

    def stop(self) -> None:
        self._stop.set()
