"""RabbitMQ consumer for bounded v2 workflows."""

from __future__ import annotations

import asyncio
import logging

from backend.config import get_settings
from backend.domain.contracts import TaskEnvelope
from backend.workers.workflows import get_default_runtime


logger = logging.getLogger(__name__)
_MAX_DELIVERY_ATTEMPTS = 3


async def run() -> None:
    from backend.core.memory import close_memory_savers, init_memory_savers
    from backend.db.session import engine

    try:
        # Compose waits for the backend to migrate the database. This separate
        # process still needs its own runtime pools before compiling QA graphs.
        await init_memory_savers(setup=False)
        await _consume()
    finally:
        await close_memory_savers()
        await engine.dispose()


async def _consume() -> None:
    import aio_pika

    settings = get_settings()
    connection = await aio_pika.connect_robust(settings.rabbitmq_url, timeout=10)
    async with connection:
        channel = await connection.channel()
        await channel.set_qos(prefetch_count=1)
        dead_exchange = await channel.declare_exchange(
            f"{settings.rabbitmq_exchange}.dlx", aio_pika.ExchangeType.DIRECT, durable=True
        )
        dead_queue = await channel.declare_queue(
            "buildmate.tasks.dead", durable=True
        )
        await dead_queue.bind(dead_exchange, routing_key="dead")
        exchange = await channel.declare_exchange(
            settings.rabbitmq_exchange, aio_pika.ExchangeType.TOPIC, durable=True
        )
        runtime = get_default_runtime()

        async def handle(message: aio_pika.IncomingMessage) -> None:
            async with message.process(requeue=False):
                try:
                    envelope = TaskEnvelope.model_validate_json(message.body)
                    await runtime.execute(envelope)
                except Exception:
                    # RabbitMQ's requeue flag has no backoff and can hot-loop
                    # a permanently invalid task. Republish a bounded number
                    # of times with an explicit attempt header, then let the
                    # queue's DLX retain the failed delivery for inspection.
                    headers = dict(message.headers or {})
                    try:
                        attempt = int(headers.get("x-buildmate-attempt", 0))
                    except (TypeError, ValueError):
                        attempt = 0
                    if attempt >= _MAX_DELIVERY_ATTEMPTS:
                        logger.exception(
                            "rabbitmq_worker.task_dead_lettered",
                            extra={"routing_key": message.routing_key, "attempt": attempt},
                        )
                        raise
                    headers["x-buildmate-attempt"] = attempt + 1
                    await exchange.publish(
                        aio_pika.Message(
                            body=message.body,
                            headers=headers,
                            content_type=message.content_type,
                            content_encoding=message.content_encoding,
                            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                        ),
                        routing_key=message.routing_key,
                    )
                    logger.warning(
                        "rabbitmq_worker.task_retry_scheduled",
                        extra={"routing_key": message.routing_key, "attempt": attempt + 1},
                    )

        bindings = {
            "buildmate.review": "review.run",
            "buildmate.modeling": "modeling.run",
            "buildmate.artifact": "artifact.process",
            "buildmate.hitl": "hitl.resume",
            "buildmate.agents": "agent.*.run",
        }
        for queue_name, routing_key in bindings.items():
            queue = await channel.declare_queue(
                queue_name,
                durable=True,
                arguments={
                    "x-dead-letter-exchange": dead_exchange.name,
                    "x-dead-letter-routing-key": "dead",
                },
            )
            await queue.bind(exchange, routing_key=routing_key)
            await queue.consume(handle)
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(run())
