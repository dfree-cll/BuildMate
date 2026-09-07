"""Publish transactional outbox events to RabbitMQ."""

from __future__ import annotations

import asyncio

from backend.adapters.outbox_repository import OutboxRepository
from backend.adapters.rabbitmq_queue import RabbitMQTaskQueue
from backend.config import get_settings
from backend.domain.contracts import TaskEnvelope


async def run() -> None:
    settings = get_settings()
    queue = RabbitMQTaskQueue(settings.rabbitmq_url, settings.rabbitmq_exchange)
    outbox = OutboxRepository()
    await queue.connect()
    try:
        while True:
            records = await outbox.claim(limit=50)
            for record in records:
                try:
                    await queue.publish(TaskEnvelope.model_validate_json(record["payload"]))
                    await outbox.mark_published(
                        record["id"],
                        claim_version=record.get("version"),
                        claim_attempts=record.get("attempts"),
                    )
                except Exception as exc:
                    await outbox.release(
                        record["id"],
                        str(exc),
                        claim_version=record.get("version"),
                        claim_attempts=record.get("attempts"),
                    )
            if not records:
                await asyncio.sleep(max(0.1, settings.task_runner_poll_seconds))
    finally:
        await queue.close()


if __name__ == "__main__":
    asyncio.run(run())
