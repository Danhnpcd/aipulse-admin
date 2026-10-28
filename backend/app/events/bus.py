import asyncio
from datetime import datetime, timezone
from typing import Any

from fastapi import WebSocket
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Notification, TaskEvent
from app.security.auth import sanitize_dict


class EventBus:
    """
    Persists all system/game events to `task_events` for replayability on reconnect,
    and broadcasts live events over WebSocket without ever leaking secrets.
    """

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(q)

    def publish(
        self,
        db: Session,
        *,
        event_type: str,
        entity_id: str,
        payload: dict[str, Any] | None = None,
        task_id: str | None = None,
        agent_id: str | None = None,
    ) -> TaskEvent:
        safe_payload = sanitize_dict(payload or {})
        evt = TaskEvent(
            event_type=event_type,
            entity_id=entity_id,
            task_id=task_id,
            agent_id=agent_id,
            payload=safe_payload,
            schema_version="1.0",
            timestamp=datetime.now(timezone.utc),
        )
        db.add(evt)
        db.flush()

        wire_message = self.serialize_event(evt)
        for q in list(self._subscribers):
            try:
                q.put_nowait(wire_message)
            except asyncio.QueueFull:
                pass
        return evt

    @staticmethod
    def serialize_event(evt: TaskEvent) -> dict[str, Any]:
        return {
            "seq": evt.seq,
            "event_id": evt.event_id,
            "event_type": evt.event_type,
            "timestamp": evt.timestamp.isoformat() if evt.timestamp else None,
            "entity_id": evt.entity_id,
            "task_id": evt.task_id,
            "agent_id": evt.agent_id,
            "payload": evt.payload,
            "schema_version": evt.schema_version,
        }

    def replay_events(self, db: Session, since_seq: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        stmt = (
            select(TaskEvent)
            .where(TaskEvent.seq > since_seq)
            .order_by(TaskEvent.seq.asc())
            .limit(limit)
        )
        rows = db.scalars(stmt).all()
        return [self.serialize_event(r) for r in rows]


event_bus = EventBus()


def emit_notification(
    db: Session,
    *,
    notif_type: str,
    title: str,
    message: str,
    task_id: str | None = None,
    agent_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Notification:
    safe_meta = sanitize_dict(metadata or {})
    notif = Notification(
        type=notif_type,
        title=title,
        message=message,
        task_id=task_id,
        agent_id=agent_id,
        metadata_json=safe_meta,
        is_read=False,
    )
    db.add(notif)
    db.flush()
    return notif
