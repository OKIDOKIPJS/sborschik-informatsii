"""Журнал событий, ошибок, публикаций и исправлений."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import sqlite3


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def record_event(
    db: sqlite3.Connection,
    kind: str,
    entity_type: str | None = None,
    entity_id: str | int | None = None,
    details: str | dict[str, Any] = "",
) -> None:
    serialized = json.dumps(details, ensure_ascii=False) if isinstance(details, dict) else details
    db.execute(
        "INSERT INTO events(created_at,kind,entity_type,entity_id,details) VALUES(?,?,?,?,?)",
        (now_iso(), kind, entity_type, str(entity_id) if entity_id is not None else None, serialized),
    )
    db.commit()


def recent_events(db: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    return list(db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)))
