"""RabbitMQ task transport with durable topic queues and publisher confirms."""

from __future__ import annotations

from backend.domain.contracts import TaskEnvelope
from backend.domain.errors import DependencyFailure


class RabbitMQTaskQueue:
    def __init__(self, url: str, exchange_name: str = "buildmate.tasks") -> None:
        if not url:
            raise ValueError("rabbitmq_url is required")
        self._url = url
        self._exchange_name = exchange_name
        self._connection = None
        self._channel = None
        self._exchange = None

    async def connect(self) -> None:
        try:
            import aio_pika
            self._connection = await aio_pika.connect_robust(self._url, timeout=10)
            self._channel = await self._connection.channel(publisher_confirms=True)
            self._exchange = await self._channel.declare_exchange(
                self._exchange_name, aio_pika.ExchangeType.TOPIC, durable=True
            )
        except (ImportError, OSError, TimeoutError, ConnectionError) as exc:
            raise DependencyFailure(f"RabbitMQ unavailable: {str(exc)[:300]}") from exc

    async def publish(self, envelope: TaskEnvelope) -> None:
        if self._exchange is None:
            await self.connect()
        try:
            import aio_pika
            routing_key = {
                "wall_pipeline": "review.run",
                "modeling": "modeling.run",
                "knowledge_ingest": "artifact.process",
                "hitl_resume": "hitl.resume",
            }.get(envelope.workflow, f"agent.{envelope.workflow}.run")
            message = aio_pika.Message(
                body=envelope.model_dump_json().encode("utf-8"),
                content_type="application/json",
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                message_id=envelope.task_id,
                correlation_id=envelope.correlation_id,
                headers={"schema_version": envelope.schema_version},
            )
            await self._exchange.publish(message, routing_key=routing_key, timeout=10)
        except (OSError, TimeoutError, ConnectionError) as exc:
            raise DependencyFailure(f"RabbitMQ publish failed: {str(exc)[:300]}") from exc

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
