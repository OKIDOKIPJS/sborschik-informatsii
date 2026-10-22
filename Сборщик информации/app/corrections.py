"""Подготовка заметных исправлений, связанных с опубликованным постом."""
from __future__ import annotations

import sqlite3

from .logs import now_iso, record_event


def create_correction_draft(db: sqlite3.Connection, original_id: int, reason: str, corrected: str,
                            source_url: str) -> int:
    original = db.execute("SELECT * FROM drafts WHERE id=?", (original_id,)).fetchone()
    if original is None:
        raise ValueError(f"Черновик #{original_id} не найден")
    if original["status"] != "published":
        raise ValueError("Исправление можно связать только с уже опубликованным постом")
    reason, corrected, source_url = reason.strip(), corrected.strip(), source_url.strip()
    if not reason or not corrected or not source_url.startswith(("https://", "http://")):
        raise ValueError("Укажите ошибочную формулировку, исправленный вариант и ссылку на основание")
    source_link = original["max_post_url"] or f"ID исходного поста: #{original_id}"
    title = f"ИСПРАВЛЕНИЕ к публикации #{original_id}"
    body = (f"❗ ИСПРАВЛЕНИЕ\n\n"
            f"В публикации от {original['published_at']} была неточность: {reason}\n\n"
            f"Исправленная информация: {corrected}\n\n"
            f"Основание исправления: {source_url}\n\n"
            f"Исходная публикация: {source_link}")
    if len(body) > 4000:
        raise ValueError("Черновик исправления превышает лимит MAX в 4000 символов")
    created = now_iso()
    cursor = db.execute(
        """INSERT INTO drafts(title,body,status,created_at,updated_at,correction_of_id)
           VALUES(?,?,'pending',?,?,?)""",
        (title, body, created, created, original_id),
    )
    correction_id = int(cursor.lastrowid)
    db.commit()
    record_event(db, "correction_draft_created", "draft", correction_id,
                 {"correction_of_id": original_id, "reason": reason})
    return correction_id
