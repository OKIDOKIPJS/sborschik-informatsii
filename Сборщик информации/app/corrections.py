"""Подготовка заметных исправлений, связанных с опубликованным постом."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from .config import get_source, load_sources
from .logs import now_iso, record_event
from .publication_validator import check_publication_link, validate_publication_url

CORRECTION_LINK_TTL = timedelta(hours=24)


def _source_for_url(url: str) -> dict[str, object]:
    matches = []
    for source in load_sources()["sources"]:
        try:
            validate_publication_url(source, url)
        except (KeyError, TypeError, ValueError):
            continue
        matches.append(source)
    if len(matches) != 1:
        raise ValueError(
            "Ссылка на основание должна однозначно относиться к одному источнику из sources.yaml; "
            f"подходящих источников найдено: {len(matches)}"
        )
    return matches[0]


def _validate_max_post_url(url: str) -> str:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if (parts.scheme.lower() != "https" or not host
            or not (host == "max.ru" or host.endswith(".max.ru"))
            or parts.path in {"", "/"} or parts.username or parts.password
            or parts.port not in {None, 443}):
        raise ValueError("Ссылка исходного поста должна быть HTTPS-ссылкой на max.ru")
    return url


def create_correction_draft(db: sqlite3.Connection, original_id: int, reason: str, corrected: str,
                            source_url: str) -> int:
    original = db.execute("SELECT * FROM drafts WHERE id=?", (original_id,)).fetchone()
    if original is None:
        raise ValueError(f"Черновик #{original_id} не найден")
    if original["status"] != "published":
        raise ValueError("Исправление можно связать только с уже опубликованным постом")
    reason, corrected, source_url = reason.strip(), corrected.strip(), source_url.strip()
    if not reason or not corrected:
        raise ValueError("Укажите ошибочную формулировку и исправленный вариант")
    source = _source_for_url(source_url)
    validate_publication_url(source, source_url)
    if not original["max_post_url"]:
        raise ValueError("У исходного поста нет сохранённой ссылки MAX; проверьте публикацию и восстановите ссылку перед исправлением")
    source_link = _validate_max_post_url(original["max_post_url"])
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
        """INSERT INTO drafts(
             title,body,status,created_at,updated_at,correction_of_id,correction_reason,
             correction_text,correction_source_id,correction_source_url,correction_original_url
           ) VALUES(?,?,'pending',?,?,?,?,?,?,?,?)""",
        (title, body, created, created, original_id, reason, corrected, source["id"], source_url, source_link),
    )
    correction_id = int(cursor.lastrowid)
    db.commit()
    record_event(db, "correction_draft_created", "draft", correction_id,
                 {"correction_of_id": original_id, "reason": reason})
    return correction_id


def mark_correction_evidence_checked(db: sqlite3.Connection, draft_id: int,
                                     note: str, evidence: str, *,
                                     expected_revision: int | None = None) -> dict[str, str]:
    # Import lazily: review_queue also uses correction_evidence_is_current.
    from .review_queue import _assert_editable, _audit

    draft = dict(_assert_editable(db, draft_id, expected_revision))
    if draft["correction_of_id"] is None:
        raise ValueError(f"Черновик #{draft_id} не является исправлением")
    reviewed_revision = int(draft["revision"])
    if not note.strip() or not evidence.strip():
        raise ValueError("Сохраните пояснение и короткое основание редакторской проверки")
    if not all(draft[key] for key in (
        "correction_reason", "correction_text", "correction_source_id",
        "correction_source_url", "correction_original_url",
    )):
        # Recover structured fields for older, still-pending correction drafts
        # created before these fields were introduced. Legacy drafts that used
        # only an ID instead of a MAX URL remain blocked for safety.
        body_lines = draft["body"].splitlines()
        reason_prefix = "В публикации от "
        reason_marker = " была неточность: "
        reason_line = next((line for line in body_lines if line.startswith(reason_prefix)
                            and reason_marker in line), "")
        recovered_reason = reason_line.split(reason_marker, 1)[1].strip() if reason_line else ""
        corrected_line = next((line for line in body_lines if line.startswith("Исправленная информация: ")), "")
        recovered_text = corrected_line.removeprefix("Исправленная информация: ").strip()
        source_line = next((line for line in body_lines if line.startswith("Основание исправления: ")), "")
        recovered_source_url = source_line.removeprefix("Основание исправления: ").strip()
        original_line = next((line for line in body_lines if line.startswith("Исходная публикация: https://")), "")
        recovered_original_url = original_line.removeprefix("Исходная публикация: ").strip()
        if not all((recovered_reason, recovered_text, recovered_source_url, recovered_original_url)):
            raise ValueError("Старый черновик не содержит полных структурированных данных; создайте исправление заново")
        source = _source_for_url(recovered_source_url)
        _validate_max_post_url(recovered_original_url)
        # Recover in memory first. A failed or stale external check must not
        # persist a partial legacy migration or attest a different draft body.
        draft.update(correction_reason=recovered_reason, correction_text=recovered_text,
                     correction_source_id=source["id"], correction_source_url=recovered_source_url,
                     correction_original_url=recovered_original_url)
    source = get_source(load_sources(), draft["correction_source_id"])
    validate_publication_url(source, draft["correction_source_url"])
    check = check_publication_link(source, draft["correction_source_url"], origin="manual")
    checked_at = now_iso()
    review_note = f"{note.strip()} Основание редактора: {evidence.strip()}"
    # HEAD may take seconds: another CLI/editor can change, approve, or start
    # publishing this draft meanwhile. Recheck under the SQLite writer lock.
    db.execute("BEGIN IMMEDIATE")
    try:
        _assert_editable(db, draft_id, reviewed_revision)
        updated = db.execute(
            """UPDATE drafts SET correction_reason=?,correction_text=?,correction_source_id=?,
               correction_source_url=?,correction_original_url=?,
               correction_publication_check_status=?,correction_publication_checked_at=?,
               correction_link_checked_at=?,correction_link_check_note=?,updated_at=?
               WHERE id=? AND status='pending' AND revision=?""",
            (draft["correction_reason"], draft["correction_text"], draft["correction_source_id"],
             draft["correction_source_url"], draft["correction_original_url"],
             check["status"], check["checked_at"], checked_at, review_note, checked_at,
             draft_id, reviewed_revision),
        )
        if updated.rowcount != 1:
            raise ValueError("Черновик изменился во время проверки основания. Обновите карточку.")
        _audit(db, "correction_evidence_checked", "draft", draft_id, {
            "correction_of_id": int(draft["correction_of_id"]),
            "source_id": draft["correction_source_id"], "source_url": draft["correction_source_url"],
            "editor_note": review_note, "publication_check": check,
            "reviewed_revision": reviewed_revision,
        })
        db.commit()
    except Exception:
        db.rollback()
        raise
    return check


def correction_evidence_is_current(draft: sqlite3.Row, now: datetime | None = None) -> bool:
    try:
        source = get_source(load_sources(), draft["correction_source_id"])
        validate_publication_url(source, draft["correction_source_url"])
        _validate_max_post_url(draft["correction_original_url"])
    except (KeyError, TypeError, ValueError):
        return False
    if draft["correction_publication_check_status"] not in {"http_ok", "manual_check_required"}:
        return False
    if not draft["correction_link_check_note"].strip():
        return False
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    for value in (draft["correction_link_checked_at"], draft["correction_publication_checked_at"]):
        if not value:
            return False
        try:
            checked_at = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return False
        if checked_at.tzinfo is None:
            checked_at = checked_at.replace(tzinfo=timezone.utc)
        age = current.astimezone(timezone.utc) - checked_at.astimezone(timezone.utc)
        if age < timedelta(0) or age > CORRECTION_LINK_TTL:
            return False
    return True
