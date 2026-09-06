"""Transactional outbox lease and stale-acknowledgement regressions."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from backend.adapters.outbox_repository import OutboxRepository
from backend.db.session import engine


async def _insert_pending_event() -> str:
    event_id = f"outbox_test_{uuid.uuid4().hex}"
    async with engine.begin() as conn:
        await conn.execute(text("""
            INSERT INTO outbox_events (
                id, aggregate_type, aggregate_id, tenant_id, project_id,
                event_type, routing_key, payload, status, attempts, version
            ) VALUES (
                :id, 'workflow_run', :aggregate_id, :tenant_id, NULL,
                'task.created', 'review.run', '{}', 'pending', 0, 1
            )
        """), {
            "id": event_id,
            "aggregate_id": f"task_{event_id}",
            "tenant_id": f"tenant_{event_id}",
        })
        # Make this fixture deterministic even when the shared test database
        # contains older outbox rows left by unrelated workflow tests.
        await conn.execute(text("""
            UPDATE outbox_events
            SET created_at='1970-01-01 00:00:00', available_at=CURRENT_TIMESTAMP
            WHERE id=:id
        """), {"id": event_id})
    return event_id


async def _state(event_id: str) -> dict:
    async with engine.connect() as conn:
        row = (await conn.execute(text("""
            SELECT status, attempts, version, last_error
            FROM outbox_events WHERE id=:id
        """), {"id": event_id})).mappings().one()
    return dict(row)


async def test_claim_is_compare_and_set_and_does_not_duplicate_active_lease():
    event_id = await _insert_pending_event()
    repository = OutboxRepository()

    first = await repository.claim(limit=10, lease_seconds=60)
    first_event = next(record for record in first if record["id"] == event_id)
    second = await repository.claim(limit=10, lease_seconds=60)

    assert first_event["attempts"] == 1
    assert first_event["version"] == 2
    assert not any(record["id"] == event_id for record in second)
    assert (await _state(event_id))["status"] == "processing"


async def test_concurrent_claims_have_one_winner():
    """Two publishers racing on one pending row must not both receive it."""
    event_id = await _insert_pending_event()
    repository_a = OutboxRepository()
    repository_b = OutboxRepository()

    first, second = await asyncio.gather(
        repository_a.claim(limit=100, lease_seconds=60),
        repository_b.claim(limit=100, lease_seconds=60),
    )
    winners = [
        records for records in (first, second)
        if any(record["id"] == event_id for record in records)
    ]
    assert len(winners) == 1


async def test_stale_ack_cannot_finalize_a_reclaimed_event():
    event_id = await _insert_pending_event()
    repository = OutboxRepository()
    first = await repository.claim(limit=10, lease_seconds=60)
    first_event = next(record for record in first if record["id"] == event_id)

    # Simulate the lease expiring while the first worker is unavailable.
    async with engine.begin() as conn:
        await conn.execute(text("""
            UPDATE outbox_events
            SET published_at=:published_at
            WHERE id=:id
        """), {
            "id": event_id,
            "published_at": datetime.now(timezone.utc) - timedelta(minutes=5),
        })

    second = await repository.claim(limit=10, lease_seconds=60)
    second_event = next(record for record in second if record["id"] == event_id)
    assert second_event["attempts"] == 2
    assert second_event["version"] != first_event["version"]

    stale_published = await repository.mark_published(
        event_id,
        claim_version=first_event["version"],
        claim_attempts=first_event["attempts"],
    )
    stale_released = await repository.release(
        event_id,
        "stale worker failed",
        claim_version=first_event["version"],
        claim_attempts=first_event["attempts"],
    )
    assert stale_published is False
    assert stale_released is False
    assert (await _state(event_id))["status"] == "processing"

    published = await repository.mark_published(
        event_id,
        claim_version=second_event["version"],
        claim_attempts=second_event["attempts"],
    )
    assert published is True
    assert (await _state(event_id))["status"] == "published"


async def test_unbound_legacy_ack_is_safe_noop():
    event_id = await _insert_pending_event()
    repository = OutboxRepository()
    claimed = await repository.claim(limit=10, lease_seconds=60)
    assert any(record["id"] == event_id for record in claimed)

    assert await repository.mark_published(event_id) is False
    assert await repository.release(event_id, "missing lease token") is False
    assert (await _state(event_id))["status"] == "processing"
