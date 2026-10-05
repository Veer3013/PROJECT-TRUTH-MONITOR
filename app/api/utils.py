from __future__ import annotations

from typing import Any

from fastapi import Request
from sqlalchemy.orm import Session

from app.api.deps import ActorContext
from app.models import AuditEvent


def add_audit_event(
    db: Session,
    *,
    actor: ActorContext,
    action: str,
    resource_type: str,
    resource_id: str | None = None,
    request: Request | None = None,
    metadata: dict[str, Any] | None = None,
) -> AuditEvent:
    event = AuditEvent(
        organization_id=actor.organization_id,
        actor_user_id=actor.user_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        request_id=getattr(getattr(request, "state", None), "request_id", None),
        metadata_json=metadata or {},
    )
    db.add(event)
    return event


def commit_or_rollback(db: Session) -> None:
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise

