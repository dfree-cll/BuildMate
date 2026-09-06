"""Task-local PostgreSQL RLS context."""

from __future__ import annotations

from contextvars import ContextVar

from backend.domain.contracts import RequestContext

_tenant_id: ContextVar[str] = ContextVar("buildmate_rls_tenant_id", default="")
_user_id: ContextVar[str] = ContextVar("buildmate_rls_user_id", default="")


def activate_rls_context(context: RequestContext) -> None:
    _tenant_id.set(context.tenant_id)
    _user_id.set(context.user_id)


def current_rls_context() -> tuple[str, str]:
    return _tenant_id.get(), _user_id.get()
