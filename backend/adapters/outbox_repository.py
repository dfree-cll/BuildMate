"""Transactional outbox claiming used by local and RabbitMQ transports."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from backend.db.session import engine


class OutboxRepository:
    async def claim(self, *, limit: int = 10, lease_seconds: int = 60) -> list[dict]:
        """Claim records with a lease-generation compare-and-set.

        The candidate query and the update are intentionally both guarded by
        the availability/lease predicate.  A competing publisher may have
        selected the same id before this transaction gets its row lock; in
        that case the second update must observe the new ``published_at`` and
        skip the row instead of treating the renewed processing lease as a
        fresh claim.

        ``version`` is returned as the claim generation.  A worker must pass
        that generation (or the returned attempt number) to ``mark_published``
        / ``release`` so a late acknowledgement from an expired worker cannot
        mutate a lease owned by another worker.
        """
        now = datetime.now(timezone.utc)
        expired = now - timedelta(seconds=max(10, lease_seconds))
        claimed: list[dict] = []
        async with engine.begin() as conn:
            candidates = (await conn.execute(text("""
                SELECT id, status, published_at, version FROM outbox_events
                WHERE (status='pending' AND available_at <= CURRENT_TIMESTAMP)
                   OR (status='processing' AND published_at IS NOT NULL
                       AND published_at < :expired)
                ORDER BY created_at ASC LIMIT :limit
            """), {"expired": expired, "limit": max(1, min(limit, 100))})).mappings().all()
            for candidate in candidates:
                event_id = candidate["id"]
                updated = await conn.execute(text("""
                    UPDATE outbox_events
                    SET status='processing', attempts=attempts + 1,
                        published_at=CURRENT_TIMESTAMP, version=version + 1
                    WHERE id=:id AND (
                        (status='pending' AND available_at <= CURRENT_TIMESTAMP)
                        OR (status='processing' AND published_at IS NOT NULL
                            AND published_at < :expired)
                    )
                    AND version=:version
                """), {
                    "id": event_id,
                    "expired": expired,
                    "version": candidate["version"],
                })
                if updated.rowcount == 1:
                    row = (await conn.execute(text("""
                        SELECT id, aggregate_id, tenant_id, project_id,
                               routing_key, payload, attempts, version
                        FROM outbox_events WHERE id=:id
                    """), {"id": event_id})).mappings().first()
                    if row:
                        claimed.append(dict(row))
        return claimed

    async def mark_published(
        self,
        event_id: str,
        *,
        claim_version: int | None = None,
        claim_attempts: int | None = None,
    ) -> bool:
        """Mark a claimed event published only if its lease is still ours.

        ``claim_version`` is preferred because it is monotonic even when an
        event is manually retried.  ``claim_attempts`` is accepted for older
        callers that persisted only the attempt count.  Calls that provide
        neither token are a safe no-op: silently accepting an unbound ack
        would let an expired worker finalize a newer lease.
        """
        predicate, params = _claim_predicate(
            claim_version=claim_version, claim_attempts=claim_attempts
        )
        if predicate is None:
            return False
        async with engine.begin() as conn:
            result = await conn.execute(text(f"""
                UPDATE outbox_events
                SET status='published', last_error=NULL,
                    published_at=CURRENT_TIMESTAMP, version=version + 1
                WHERE id=:id AND status='processing' AND {predicate}
            """), {"id": event_id, **params})
        return result.rowcount == 1

    async def release(
        self,
        event_id: str,
        error: str,
        *,
        delay_seconds: int = 5,
        claim_version: int | None = None,
        claim_attempts: int | None = None,
    ) -> bool:
        """Release a lease for retry, guarded by the claim generation."""
        predicate, params = _claim_predicate(
            claim_version=claim_version, claim_attempts=claim_attempts
        )
        if predicate is None:
            return False
        async with engine.begin() as conn:
            result = await conn.execute(text(f"""
                UPDATE outbox_events
                SET status=CASE WHEN attempts >= 5 THEN 'dead' ELSE 'pending' END,
                    last_error=:error,
                    available_at=:available_at,
                    version=version + 1
                WHERE id=:id AND status='processing' AND {predicate}
            """), {
                "id": event_id,
                "error": error[:1000],
                "available_at": datetime.now(timezone.utc) + timedelta(seconds=max(1, delay_seconds)),
                **params,
            })
        return result.rowcount == 1


def _claim_predicate(
    *, claim_version: int | None, claim_attempts: int | None
) -> tuple[str | None, dict[str, int]]:
    """Build a bounded SQL predicate for an outbox lease token.

    Version and attempts are values returned by :meth:`claim`.  Either token
    can be used for compatibility, while supplying both binds the
    acknowledgement to the exact claim generation.  The helper returns SQL
    fragments containing only fixed column names, while values remain bound
    parameters.
    """

    if claim_version is None and claim_attempts is None:
        return None, {}
    params: dict[str, int] = {}
    clauses: list[str] = []
    if claim_version is not None:
        params["claim_version"] = int(claim_version)
        clauses.append("version=:claim_version")
    if claim_attempts is not None:
        params["claim_attempts"] = int(claim_attempts)
        clauses.append("attempts=:claim_attempts")
    return " AND ".join(clauses), params
