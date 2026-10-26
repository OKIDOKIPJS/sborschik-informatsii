"""Очередь редакторской проверки и одобрения."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from functools import wraps
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import urlsplit

from .config import get_source, load_sources
from .corrections import correction_evidence_is_current
from .draft_generator import _source_summary_excerpt
from .logs import now_iso
from .publication_validator import check_publication_link

MOSCOW = timezone(timedelta(hours=3))
LINK_REVIEW_TTL = timedelta(hours=24)


def _audit(db: sqlite3.Connection, kind: str, entity_type: str | None = None,
           entity_id: str | int | None = None, details: object = "") -> None:
    """Write an audit record in the caller's transaction, without committing."""
    db.execute(
        "INSERT INTO events(created_at,kind,entity_type,entity_id,details) VALUES(?,?,?,?,?)",
        (now_iso(), kind, entity_type, str(entity_id) if entity_id is not None else None,
         json.dumps(details, ensure_ascii=False) if not isinstance(details, str) else details),
    )


def _assert_editable(db: sqlite3.Connection, draft_id: int,
                     expected_revision: int | None = None) -> sqlite3.Row:
    draft = get_draft(db, draft_id)
    if draft["status"] != "pending":
        raise ValueError(f"Изменение возможно только для pending-черновика; сейчас {draft['status']}")
    if expected_revision is not None and draft["revision"] != expected_revision:
        raise ValueError("Черновик уже изменён другим действием. Обновите карточку и повторите проверку.")
    if db.execute("SELECT 1 FROM publish_attempts WHERE draft_id=? AND state IN ('started','unknown') LIMIT 1",
                  (draft_id,)).fetchone():
        raise ValueError("Есть незавершённая попытка публикации; сначала проверьте канал и выполните publish-resolve.")
    return draft


def _pending_mutation(function):
    """Serialize editor changes and their audit event, including CLI calls."""
    @wraps(function)
    def wrapped(db, draft_id, *args, expected_revision=None, **kwargs):
        db.execute("BEGIN IMMEDIATE")
        try:
            _assert_editable(db, draft_id, expected_revision)
            result = function(db, draft_id, *args, **kwargs)
            db.commit()
            return result
        except Exception:
            db.rollback()
            raise
    return wrapped


def _batch_mutation(function):
    @wraps(function)
    def wrapped(db, batch_id, draft_id, *args, expected_revision=None, **kwargs):
        db.execute("BEGIN IMMEDIATE")
        try:
            draft = get_draft(db, draft_id)
            if draft["status"] not in {"pending", "approved"}:
                raise ValueError("Часть должна быть pending или approved")
            if expected_revision is not None and draft["revision"] != expected_revision:
                raise ValueError("Черновик уже изменён. Обновите карточку.")
            if db.execute(
                """SELECT 1 FROM publish_attempts a JOIN digest_batch_parts p ON p.draft_id=a.draft_id
                   WHERE p.batch_id=? AND a.state IN ('started','unknown') LIMIT 1""", (batch_id,)
            ).fetchone():
                raise ValueError("В партии есть незавершённая публикация; сначала выполните publish-resolve")
            result = function(db, batch_id, draft_id, *args, **kwargs)
            db.commit()
            return result
        except Exception:
            db.rollback()
            raise
    return wrapped


def approval_fingerprint(db: sqlite3.Connection, draft: sqlite3.Row) -> str:
    """Hash the exact text and evidence which the editor approved."""
    fields = ("title", "body", "correction_of_id", "requires_group_review",
              "correction_reason", "correction_text", "correction_source_id", "correction_source_url",
              "correction_original_url", "correction_publication_check_status",
              "correction_publication_checked_at", "correction_link_checked_at", "correction_link_check_note")
    snapshot = {key: draft[key] for key in fields}
    for table in ("draft_claims", "draft_groups", "revision_alerts"):
        snapshot[table] = [dict(row) for row in db.execute(
            f"SELECT * FROM {table} WHERE draft_id=? ORDER BY id", (draft["id"],)
        )]
    return hashlib.sha256(json.dumps(snapshot, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def list_drafts(db: sqlite3.Connection, status: str | None = None) -> list[sqlite3.Row]:
    if status:
        return list(db.execute("SELECT id,title,status,created_at,updated_at FROM drafts WHERE status=? ORDER BY id DESC", (status,)))
    return list(db.execute("SELECT id,title,status,created_at,updated_at FROM drafts ORDER BY id DESC"))


def list_batches(db: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(db.execute(
        """SELECT b.id,b.batch_date,b.created_at,COUNT(p.draft_id) AS part_count,
                  SUM(CASE WHEN p.disposition='selected' THEN 1 ELSE 0 END) AS selected_count
           FROM digest_batches b LEFT JOIN digest_batch_parts p ON p.batch_id=b.id
           GROUP BY b.id ORDER BY b.created_at DESC"""
    ))


def batch_parts(db: sqlite3.Connection, batch_id: str) -> list[sqlite3.Row]:
    if db.execute("SELECT 1 FROM digest_batches WHERE id=?", (batch_id,)).fetchone() is None:
        raise ValueError(f"Партия {batch_id!r} не найдена")
    return list(db.execute(
        """SELECT p.*,d.title,d.status,d.created_at,
                  MIN(c.published_at) AS oldest_published_at,
                  MAX(c.published_at) AS newest_published_at
           FROM digest_batch_parts p JOIN drafts d ON d.id=p.draft_id
           LEFT JOIN draft_claims c ON c.draft_id=d.id AND c.included=1
           WHERE p.batch_id=? GROUP BY p.draft_id ORDER BY p.part_no""", (batch_id,)
    ))


@_batch_mutation
def select_batch_part(db: sqlite3.Connection, batch_id: str, draft_id: int) -> None:
    parts = batch_parts(db, batch_id)
    target = next((part for part in parts if int(part["draft_id"]) == draft_id), None)
    if target is None:
        raise ValueError(f"Черновик #{draft_id} не относится к партии {batch_id}")
    if target["disposition"] in {"closed", "deferred"}:
        raise ValueError("Закрытую или отложенную часть нельзя выбрать; после переноса создайте новую партию")
    if target["status"] not in {"pending", "approved"}:
        raise ValueError(f"Часть должна быть pending или approved; сейчас {target['status']}")
    now = now_iso()
    previous = [int(part["draft_id"]) for part in parts if part["disposition"] == "selected"]
    db.execute("UPDATE digest_batch_parts SET disposition='open',updated_at=? WHERE batch_id=? AND disposition='selected'",
               (now, batch_id))
    db.execute("UPDATE digest_batch_parts SET disposition='selected',deferred_until=NULL,note='',updated_at=? WHERE draft_id=?",
               (now, draft_id))
    _audit(db, "batch_part_selected", "draft", draft_id,
                 {"batch_id": batch_id, "part_no": target["part_no"], "previously_selected": previous})


@_batch_mutation
def close_batch_part(db: sqlite3.Connection, batch_id: str, draft_id: int, reason: str) -> None:
    part = next((part for part in batch_parts(db, batch_id) if int(part["draft_id"]) == draft_id), None)
    if part is None:
        raise ValueError(f"Черновик #{draft_id} не относится к партии {batch_id}")
    if part["disposition"] == "selected":
        raise ValueError("Сначала выберите другую часть, затем закройте эту часть")
    if not reason.strip():
        raise ValueError("Укажите причину закрытия")
    now = now_iso()
    db.execute("UPDATE digest_batch_parts SET disposition='closed',deferred_until=NULL,note=?,updated_at=? WHERE draft_id=?",
               (reason.strip(), now, draft_id))
    db.execute("UPDATE drafts SET status='rejected',updated_at=? WHERE id=? AND status IN ('pending','approved')",
               (now, draft_id))
    _audit(db, "batch_part_closed", "draft", draft_id,
                 {"batch_id": batch_id, "part_no": part["part_no"], "reason": reason.strip()})


@_batch_mutation
def defer_batch_part(db: sqlite3.Connection, batch_id: str, draft_id: int,
                     until_date: str, note: str) -> None:
    part = next((part for part in batch_parts(db, batch_id) if int(part["draft_id"]) == draft_id), None)
    if part is None:
        raise ValueError(f"Черновик #{draft_id} не относится к партии {batch_id}")
    if part["disposition"] == "selected":
        raise ValueError("Сначала выберите другую часть, затем перенесите эту часть")
    if not note.strip():
        raise ValueError("Укажите причину переноса")
    try:
        target_day = date.fromisoformat(until_date)
    except ValueError as exc:
        raise ValueError("Дата переноса должна быть в формате ГГГГ-ММ-ДД") from exc
    if target_day < datetime.now(MOSCOW).date():
        raise ValueError("Дата переноса не может быть в прошлом")
    published = [datetime.fromisoformat(row[0]) for row in db.execute(
        "SELECT published_at FROM draft_claims WHERE draft_id=? AND included=1", (draft_id,)
    )]
    if not published:
        raise ValueError("Нельзя перенести часть без включённых утверждений")
    expires_at = min(value + timedelta(hours=96) for value in published)
    target_morning = datetime.combine(target_day, time.min, tzinfo=MOSCOW)
    if target_morning > expires_at.astimezone(MOSCOW):
        raise ValueError(
            f"Перенос выходит за срок актуальности: самый ранний материал опубликован "
            f"{min(published).astimezone(MOSCOW).strftime('%d.%m.%Y %H:%M МСК')}; "
            f"96 часов истекают {expires_at.astimezone(MOSCOW).strftime('%d.%m.%Y %H:%M МСК')}"
        )
    now = now_iso()
    db.execute("UPDATE digest_batch_parts SET disposition='deferred',deferred_until=?,note=?,updated_at=? WHERE draft_id=?",
               (target_day.isoformat(), note.strip(), now, draft_id))
    db.execute("UPDATE drafts SET status='rejected',updated_at=? WHERE id=? AND status IN ('pending','approved')",
               (now, draft_id))
    _audit(db, "batch_part_deferred", "draft", draft_id, {
        "batch_id": batch_id, "part_no": part["part_no"], "until": target_day.isoformat(),
        "note": note.strip(), "oldest_publication": min(published).isoformat(),
        "expires_at": expires_at.isoformat(),
    })


def get_draft(db: sqlite3.Connection, draft_id: int) -> sqlite3.Row:
    row = db.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
    if row is None:
        raise ValueError(f"Черновик {draft_id} не найден")
    return row


def draft_claims(db: sqlite3.Connection, draft_id: int) -> list[sqlite3.Row]:
    get_draft(db, draft_id)
    return list(db.execute(
        """SELECT c.*, g.group_no,v.version_no FROM draft_claims c
           JOIN draft_groups g ON g.id=c.group_id
           LEFT JOIN item_versions v ON v.id=c.version_id
           WHERE c.draft_id=? ORDER BY g.group_no,c.id""", (draft_id,)
    ))


def revision_alerts(db: sqlite3.Connection, draft_id: int) -> list[sqlite3.Row]:
    get_draft(db, draft_id)
    return list(db.execute(
        """SELECT a.*,c.claim_text,old.version_no AS previous_version,
                  current.version_no AS current_version,current.title AS current_title,
                  current.summary AS current_summary,current.published_at AS current_published_at,
                  current.changed_fields
           FROM revision_alerts a JOIN draft_claims c ON c.id=a.claim_id
           JOIN item_versions old ON old.id=a.previous_version_id
           JOIN item_versions current ON current.id=a.current_version_id
           WHERE a.draft_id=? ORDER BY a.id""", (draft_id,)
    ))


@_pending_mutation
def review_source_revision(db: sqlite3.Connection, draft_id: int, claim_id: int,
                           mode: str, note: str) -> None:
    draft = get_draft(db, draft_id)
    if draft["status"] != "pending":
        raise ValueError("Проверить новую версию источника можно только в pending-черновике")
    if mode not in {"keep", "refresh"}:
        raise ValueError("Решение должно быть keep или refresh")
    if not note.strip():
        raise ValueError("Укажите, что именно редактор перепроверил")
    alert = db.execute(
        """SELECT a.*,c.group_id,c.item_id,c.version_id FROM revision_alerts a
           JOIN draft_claims c ON c.id=a.claim_id
           WHERE a.draft_id=? AND a.claim_id=? AND a.resolution IS NULL
           ORDER BY a.id DESC LIMIT 1""", (draft_id, claim_id)
    ).fetchone()
    if alert is None:
        raise ValueError(f"Для утверждения #{claim_id} нет неразрешённого предупреждения о новой версии")
    reviewed_at = now_iso()
    if mode == "refresh":
        version = db.execute("SELECT * FROM item_versions WHERE id=?", (alert["current_version_id"],)).fetchone()
        claim = db.execute("SELECT * FROM draft_claims WHERE id=?", (claim_id,)).fetchone()
        if not version["published_at"]:
            raise ValueError("У новой версии отсутствует дата публикации. Исключите утверждение или оставьте прежнюю версию после проверки.")
        try:
            payload = json.loads(version["payload"] or "{}")
            check = payload.get("publication_check", {})
        except (TypeError, json.JSONDecodeError):
            check = {}
        if check.get("status") == "http_ok_feed":
            validation = f"запись есть в RSS/Atom, ссылка отвечает HTTP {check.get('http_status', '2xx')}"
        elif check.get("status") == "http_ok":
            validation = f"ссылка отвечает HTTP {check.get('http_status', '2xx')}"
        elif check.get("status") == "manual_check_required":
            validation = "ссылку нужно проверить вручную"
        else:
            validation = "проверка ссылки не сохранена"
        if claim["source_level"] == "analysis_osint":
            status, attribution = "ОЦЕНКА АНАЛИТИКА", f"{claim['source_name']} оценивает"
        else:
            status, attribution = "ЗАЯВЛЕНИЕ ИСТОЧНИКА", f"{claim['source_name']} сообщает"
        published = datetime.fromisoformat(version["published_at"]).astimezone(MOSCOW).strftime("%d.%m.%Y, %H:%M МСК")
        replacement = [
            f"• **{status}** — {attribution}: {version['title'].rstrip('. ')}.",
        ]
        summary_excerpt = _source_summary_excerpt(version)
        if summary_excerpt:
            replacement.append(f"— Выжимка из описания {claim['source_name']} (до двух предложений): {summary_excerpt}")
        replacement.extend([
            "— Дата события не проверена редактором; требуется уточнить до одобрения.",
            f"— {claim['source_name']}, опубликовано {published}; {validation}: {version['url']}",
        ])
        lines, statement_index, evidence_index = _claim_block(draft["body"], claim)
        lines[statement_index:evidence_index + 1] = replacement
        updated_body = "\n".join(lines)
        if len(updated_body) > 4000:
            raise ValueError("После обновления утверждения черновик превышает лимит MAX; сократите текст вручную")
        db.execute(
            """UPDATE draft_claims SET version_id=?,claim_text=?,published_at=?,source_url=?,
               publication_check_status=?,publication_checked_at=?,link_checked_at=NULL,link_check_note='',
               event_date=NULL,event_date_status='unreviewed',event_date_note='',event_date_reviewed_at=NULL
               WHERE id=?""",
            (version["id"], version["title"].rstrip(". "), version["published_at"], version["url"],
             check.get("status", ""), check.get("checked_at"), claim_id),
        )
        db.execute(
            """UPDATE draft_groups SET review_same_event=NULL,sources_independent=NULL,
               review_note='',reviewed_at=NULL WHERE id=?""", (alert["group_id"],)
        )
        db.execute("UPDATE drafts SET body=?,updated_at=? WHERE id=?", (updated_body, reviewed_at, draft_id))
    db.execute(
        """UPDATE revision_alerts SET resolution=?,note=?,reviewed_at=?
           WHERE draft_id=? AND claim_id=? AND resolution IS NULL AND current_version_id<=?""",
        ("refreshed" if mode == "refresh" else "kept", note.strip(), reviewed_at,
         draft_id, claim_id, alert["current_version_id"]),
    )
    _audit(db, "source_revision_reviewed", "draft_claim", claim_id, {
        "draft_id": draft_id, "mode": mode, "current_version_id": alert["current_version_id"],
        "note": note.strip(),
    })


def draft_groups(db: sqlite3.Connection, draft_id: int) -> list[sqlite3.Row]:
    get_draft(db, draft_id)
    return list(db.execute(
        """SELECT g.*,SUM(CASE WHEN c.included=1 THEN 1 ELSE 0 END) AS claim_count FROM draft_groups g
           LEFT JOIN draft_claims c ON c.group_id=g.id
           WHERE g.draft_id=? GROUP BY g.id ORDER BY g.group_no""", (draft_id,)
    ))


def _claim_statement_index(lines: list[str], claim_text: str, evidence_index: int) -> int | None:
    # Prefer the actual post bullet: a source-provided synopsis may repeat the
    # headline and must not become the start of the claim block for edits.
    statement_index = next((index for index in range(evidence_index, -1, -1)
                            if lines[index].startswith("•") and claim_text in lines[index]), None)
    if statement_index is not None:
        return statement_index
    # Backward compatibility for older drafts whose claim and URL share a line.
    return next((index for index in range(evidence_index, -1, -1)
                 if claim_text in lines[index]), None)


def _claim_block(body: str, claim: sqlite3.Row) -> tuple[list[str], int, int]:
    """Return body lines and the claim's statement/evidence line positions."""
    lines = body.splitlines()
    # Prefer a source-specific bullet and find its citation before the next
    # bullet. This is important when different publishers point to the same
    # canonical URL: a global first-match search would bind both claims to one
    # publisher's statement.
    bullet_positions = [index for index, line in enumerate(lines) if line.startswith("•")]
    for position, statement_index in enumerate(bullet_positions):
        line = lines[statement_index]
        if claim["claim_text"] not in line or claim["source_name"] not in line:
            continue
        block_end = bullet_positions[position + 1] if position + 1 < len(bullet_positions) else len(lines)
        evidence_index = next(
            (index for index in range(statement_index, block_end) if claim["source_url"] in lines[index]),
            None,
        )
        if evidence_index is not None:
            return lines, statement_index, evidence_index

    # Backward compatibility for older drafts whose source attribution or
    # citation layout differs from the current claim-block template.
    evidence_index = next((index for index, line in enumerate(lines)
                           if claim["source_url"] in line), None)
    if evidence_index is None:
        raise ValueError(f"Утверждение #{claim['id']}: его ссылка отсутствует в тексте черновика")
    statement_index = _claim_statement_index(lines, claim["claim_text"], evidence_index)
    if statement_index is None:
        raise ValueError(f"Утверждение #{claim['id']}: его формулировка отсутствует в тексте черновика")
    return lines, statement_index, evidence_index


def _event_date_line(status: str, event_date: str | None, published_at: str | None = None) -> str:
    if status == "explicit":
        if published_at and event_date:
            published_date = datetime.fromisoformat(published_at).astimezone(MOSCOW).date().isoformat()
            if event_date < published_date:
                return f"— Событие произошло {event_date}; материал опубликован {published_date}."
        return f"— Дата события по материалу: {event_date} (указана редактором)."
    if status == "same_day":
        return f"— Дата события: {event_date} (редактор подтвердил совпадение с датой публикации)."
    if status == "unknown":
        return "— Дата события не установлена; не выдавать это сообщение за новое событие."
    return "— Дата события не проверена редактором; требуется уточнить до одобрения."


@_pending_mutation
def review_claim_event_date(db: sqlite3.Connection, draft_id: int, claim_id: int,
                            mode: str, event_date: str | None = None, note: str = "") -> None:
    draft = get_draft(db, draft_id)
    if draft["status"] != "pending":
        raise ValueError("Дату события можно уточнять только в pending-черновике")
    claim = db.execute(
        "SELECT * FROM draft_claims WHERE id=? AND draft_id=? AND included=1",
        (claim_id, draft_id),
    ).fetchone()
    if claim is None:
        raise ValueError(f"Активное утверждение #{claim_id} не найдено в черновике #{draft_id}")
    if not note.strip():
        raise ValueError("Добавьте краткое основание для решения о дате события")
    published_date = datetime.fromisoformat(claim["published_at"]).astimezone(MOSCOW).date()
    if mode == "same_day":
        resolved_date = published_date.isoformat()
        status = "same_day"
    elif mode == "explicit":
        try:
            parsed_date = date.fromisoformat(event_date or "")
        except ValueError as exc:
            raise ValueError("Дата события должна быть в формате ГГГГ-ММ-ДД") from exc
        if parsed_date > published_date:
            raise ValueError("Дата события не может быть позже даты публикации материала")
        resolved_date = parsed_date.isoformat()
        status = "explicit"
    elif mode == "unknown":
        resolved_date = None
        status = "unknown"
    else:
        raise ValueError("Режим даты события должен быть explicit, same_day или unknown")

    lines, statement_index, evidence_index = _claim_block(draft["body"], claim)
    lines = [line for index, line in enumerate(lines)
             if not (statement_index < index <= evidence_index and line.startswith("— Дата события"))]
    if statement_index == evidence_index:
        while statement_index + 1 < len(lines) and lines[statement_index + 1].startswith("— Дата события"):
            del lines[statement_index + 1]
    new_evidence_index = next(index for index, line in enumerate(lines) if claim["source_url"] in line)
    new_statement_index = _claim_statement_index(lines, claim["claim_text"], new_evidence_index)
    if new_statement_index is None:
        raise ValueError(f"Утверждение #{claim_id}: его формулировка отсутствует в тексте черновика")
    lines.insert(new_statement_index + 1, _event_date_line(status, resolved_date, claim["published_at"]))
    updated_body = "\n".join(lines)
    if len(updated_body) > 4000:
        raise ValueError("После добавления даты события черновик превышает лимит MAX 4000 символов; сократите текст и повторите")
    reviewed_at = now_iso()
    db.execute(
        """UPDATE draft_claims SET event_date=?,event_date_status=?,event_date_note=?,event_date_reviewed_at=?
           WHERE id=?""",
        (resolved_date, status, note.strip(), reviewed_at, claim_id),
    )
    db.execute("UPDATE drafts SET body=?,updated_at=? WHERE id=?", (updated_body, reviewed_at, draft_id))
    _audit(db, "claim_event_date_reviewed", "draft_claim", claim_id, {
        "draft_id": draft_id, "event_date": resolved_date, "status": status,
        "note": note.strip(),
    })


@_pending_mutation
def exclude_claim(db: sqlite3.Connection, draft_id: int, claim_id: int, reason: str) -> None:
    draft = get_draft(db, draft_id)
    if draft["status"] != "pending":
        raise ValueError("Исключать утверждения можно только из pending-черновика")
    claim = db.execute(
        "SELECT * FROM draft_claims WHERE id=? AND draft_id=? AND included=1",
        (claim_id, draft_id),
    ).fetchone()
    if claim is None:
        raise ValueError(f"Активное утверждение #{claim_id} не найдено в черновике #{draft_id}")
    if not reason.strip():
        raise ValueError("Укажите причину исключения утверждения")
    lines, statement_index, evidence_index = _claim_block(draft["body"], claim)
    if statement_index == evidence_index and evidence_index + 1 < len(lines) \
            and lines[evidence_index + 1].startswith("— Дата события"):
        evidence_index += 1
    del lines[statement_index:evidence_index + 1]
    updated_body = "\n".join(lines)
    excluded_at = now_iso()
    db.execute(
        """UPDATE draft_claims SET included=0,exclusion_reason=?,excluded_at=? WHERE id=?""",
        (reason.strip(), excluded_at, claim_id),
    )
    db.execute(
        """UPDATE revision_alerts SET resolution='excluded',note=?,reviewed_at=?
           WHERE draft_id=? AND claim_id=? AND resolution IS NULL""",
        (reason.strip(), excluded_at, draft_id, claim_id),
    )
    db.execute(
        """UPDATE draft_groups SET review_same_event=NULL,sources_independent=NULL,
           review_note='',reviewed_at=NULL WHERE id=?""", (claim["group_id"],)
    )
    db.execute("UPDATE drafts SET body=?,updated_at=? WHERE id=?", (updated_body, excluded_at, draft_id))
    _audit(db, "claim_excluded", "draft_claim", claim_id,
                 {"draft_id": draft_id, "reason": reason.strip()})


def claim_link_check_is_current(claim: sqlite3.Row, now: datetime | None = None) -> bool:
    if claim["publication_check_status"] not in {"http_ok", "manual_check_required"}:
        return False
    if not claim["link_check_note"].strip():
        return False
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    for value in (claim["link_checked_at"], claim["publication_checked_at"]):
        if not value:
            return False
        try:
            checked_at = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return False
        if checked_at.tzinfo is None:
            checked_at = checked_at.replace(tzinfo=timezone.utc)
        age = current.astimezone(timezone.utc) - checked_at.astimezone(timezone.utc)
        if age < timedelta(0) or age > LINK_REVIEW_TTL:
            return False
    return True


def mark_claim_link_checked(db: sqlite3.Connection, draft_id: int, claim_id: int,
                            checked: bool, note: str,
                            expected_revision: int | None = None) -> dict[str, str]:
    draft = _assert_editable(db, draft_id, expected_revision)
    revision = int(draft["revision"])
    claim = db.execute(
        "SELECT * FROM draft_claims WHERE id=? AND draft_id=? AND included=1", (claim_id, draft_id)
    ).fetchone()
    if claim is None:
        raise ValueError(f"Утверждение #{claim_id} не относится к черновику #{draft_id}")
    if not checked or not note.strip():
        raise ValueError("Подтвердите открытие ссылки и добавьте пояснение проверки (claim-check --note)")
    parts = urlsplit(claim["source_url"])
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError(f"У утверждения #{claim_id} нет действительной HTTP(S)-ссылки")
    failure = None
    publication_check = None
    try:
        source = get_source(load_sources(), claim["source_id"])
        publication_check = check_publication_link(source, claim["source_url"], origin="manual")
    except Exception as exc:
        failure = exc
    # Do not hold SQLite's write lock while a remote site is answering. Refuse
    # to attest any different text/evidence than the one checked above.
    db.execute("BEGIN IMMEDIATE")
    try:
        _assert_editable(db, draft_id, revision)
        if failure is not None:
            db.execute(
                """UPDATE draft_claims SET link_checked_at=NULL,link_check_note='',
                   publication_check_status='',publication_checked_at=NULL WHERE id=?""", (claim_id,)
            )
            _audit(db, "claim_link_check_failed", "draft_claim", claim_id,
                   {"draft_id": draft_id, "source_url": claim["source_url"],
                    "error": f"{type(failure).__name__}: {failure}"})
        else:
            db.execute(
                """UPDATE draft_claims SET link_checked_at=?,link_check_note=?,
                   publication_check_status=?,publication_checked_at=? WHERE id=?""",
                (now_iso(), note.strip(), publication_check["status"], publication_check["checked_at"], claim_id),
            )
            _audit(db, "claim_link_checked", "draft_claim", claim_id,
                   {"draft_id": draft_id, "source_url": claim["source_url"],
                    "editor_note": note.strip(), "publication_check": publication_check})
        db.commit()
    except Exception:
        db.rollback()
        raise
    if failure is not None:
        raise failure
    return publication_check


@_pending_mutation
def review_candidate_group(db: sqlite3.Connection, draft_id: int, group_no: int,
                           same_event: bool, independent: bool, note: str = "") -> None:
    draft = get_draft(db, draft_id)
    if draft["status"] != "pending":
        raise ValueError("Проверять группы можно только в pending-черновике")
    if not note.strip():
        raise ValueError("Добавьте краткое обоснование редакторского решения о группе")
    group = db.execute(
        """SELECT g.id,SUM(CASE WHEN c.included=1 THEN 1 ELSE 0 END) AS claim_count FROM draft_groups g
           LEFT JOIN draft_claims c ON c.group_id=g.id
           WHERE g.draft_id=? AND g.group_no=? GROUP BY g.id""", (draft_id, group_no)
    ).fetchone()
    if group is None or int(group["claim_count"]) < 2:
        raise ValueError(f"Группа #{group_no} не найдена или не является группой-кандидатом")
    if independent and db.execute(
        """SELECT 1 FROM draft_claims WHERE group_id=? AND included=1
           AND same_url_other_source=1 LIMIT 1""", (group["id"],)
    ).fetchone():
        raise ValueError(
            "В группе есть одинаковая ссылка у разных источников. Эти записи не являются независимыми; "
            "зафиксируйте --not-independent."
        )
    if independent and db.execute(
        """SELECT 1 FROM draft_claims WHERE group_id=? AND included=1
           GROUP BY source_id HAVING COUNT(*)>1 LIMIT 1""", (group["id"],)
    ).fetchone():
        raise ValueError(
            "В группе несколько материалов одного издателя. Они не являются независимыми источниками; "
            "зафиксируйте --not-independent."
        )
    reviewed_at = now_iso()
    db.execute(
        """UPDATE draft_groups SET review_same_event=?,sources_independent=?,review_note=?,reviewed_at=?
           WHERE id=?""",
        (int(same_event), int(independent), note.strip(), reviewed_at, group["id"]),
    )
    _audit(db, "candidate_group_reviewed", "draft_group", group["id"], {
        "draft_id": draft_id, "group_no": group_no, "same_event": same_event,
        "sources_independent": independent, "note": note.strip(),
    })


@_pending_mutation
def edit_draft(db: sqlite3.Connection, draft_id: int, text: str) -> None:
    draft = get_draft(db, draft_id)
    if draft["status"] != "pending":
        raise ValueError("Редактировать можно только черновик со статусом pending")
    if len(text) > 4000:
        raise ValueError("MAX ограничивает текст поста 4000 символами")
    db.execute("UPDATE drafts SET body=?,updated_at=? WHERE id=?", (text, now_iso(), draft_id))
    # Any prose edit can alter the relationship between evidence and wording.
    # Force a fresh link review, event-date review, and candidate-group review.
    db.execute(
        """UPDATE draft_claims SET link_checked_at=NULL,link_check_note='',
           event_date_status='unreviewed',event_date=NULL,event_date_note='',event_date_reviewed_at=NULL
           WHERE draft_id=? AND included=1""", (draft_id,)
    )
    db.execute(
        """UPDATE draft_groups SET review_same_event=NULL,sources_independent=NULL,
           review_note='',reviewed_at=NULL WHERE draft_id=?""", (draft_id,)
    )
    if draft["correction_of_id"] is not None:
        db.execute(
            """UPDATE drafts SET correction_publication_check_status='',
               correction_publication_checked_at=NULL,correction_link_checked_at=NULL,
               correction_link_check_note='' WHERE id=?""", (draft_id,)
        )
    _audit(db, "draft_corrected", "draft", draft_id,
                 {"previous_text": draft["body"], "corrected_text": text})


def validate_approval(db: sqlite3.Connection, draft: sqlite3.Row,
                      checks: dict[str, bool], now: datetime | None = None) -> dict[str, bool]:
    """Validate text and evidence without changing the draft or audit log."""
    decision = "approved"
    draft_id = int(draft["id"])
    checks = dict(checks)
    if not draft["body"].strip() or len(draft["body"]) > 4000:
        raise ValueError("Текст должен содержать от 1 до 4000 символов")
    if decision == "approved":
        if checks.get("evidence_checked") is not True:
            raise ValueError("Перед одобрением подтвердите проверку содержания публикаций и ссылок: --evidence-checked")
        if checks.get("independence_checked") is not True:
            raise ValueError("Перед одобрением подтвердите проверку независимости оснований: --independence-checked")
    if decision == "approved" and draft["correction_of_id"]:
        original = db.execute(
            "SELECT status,max_post_url FROM drafts WHERE id=?", (draft["correction_of_id"],)
        ).fetchone()
        if original is None or original["status"] != "published" or not original["max_post_url"]:
            raise ValueError("Исходный пост исправления должен оставаться опубликованным и иметь ссылку MAX")
        if ("ИСПРАВЛЕНИЕ" not in draft["body"]
                or not draft["correction_reason"] or draft["correction_reason"] not in draft["body"]
                or not draft["correction_text"] or draft["correction_text"] not in draft["body"]
                or not draft["correction_source_id"] or not draft["correction_source_url"]
                or draft["correction_source_url"] not in draft["body"]
                or draft["correction_original_url"] != original["max_post_url"]
                or draft["correction_original_url"] not in draft["body"]):
            raise ValueError(
                "В черновике исправления должны совпадать сохранённые ошибочная и исправленная формулировки, "
                "разрешённое основание и ссылка на исходный пост"
            )
        if not correction_evidence_is_current(draft, now=now):
            raise ValueError(
                "Перед одобрением исправления выполните review correction-check ID --note, "
                "откройте ссылку и сохраните короткое основание проверки; отметка действует 24 часа."
            )
    if decision == "approved" and not draft["correction_of_id"]:
        unresolved_revisions = db.execute(
            "SELECT COUNT(*) FROM revision_alerts WHERE draft_id=? AND resolution IS NULL",
            (draft_id,),
        ).fetchone()[0]
        if unresolved_revisions:
            raise ValueError(
                "У источника появилась новая версия материала. Проверьте её командой "
                "review revision-check либо исключите утверждение перед одобрением."
            )
        claims = [claim for claim in draft_claims(db, draft_id) if claim["included"]]
        if not claims:
            raise ValueError("В черновике нет структурированных утверждений с источниками")
        for claim in claims:
            if not all(claim[key] for key in ("claim_text", "source_id", "source_name", "published_at", "source_url")):
                raise ValueError(f"Утверждение #{claim['id']}: не сохранены текст, источник, дата публикации или ссылка")
            parts = urlsplit(claim["source_url"])
            if parts.scheme not in {"http", "https"} or not parts.netloc:
                raise ValueError(f"Утверждение #{claim['id']}: ссылка должна быть действительной HTTP(S)-ссылкой")
            if not claim_link_check_is_current(claim, now=now):
                raise ValueError(
                    f"Утверждение #{claim['id']}: повторно откройте ссылку, проверьте её доступность "
                    "и соответствие утверждению, затем выполните интерактивную команду review claim-check --note. "
                    "Проверка ссылки действует 24 часа."
                )
            if claim["claim_text"] not in draft["body"] or claim["source_url"] not in draft["body"]:
                raise ValueError(f"Утверждение #{claim['id']}: сохраните его формулировку и собственную ссылку в тексте черновика")
            if (claim["event_date_status"] not in {"explicit", "same_day"}
                    or not claim["event_date"] or not claim["event_date_reviewed_at"]):
                raise ValueError(
                    f"Утверждение #{claim['id']}: уточните дату события командой review event-date "
                    "или исключите пункт через review claim-exclude"
                )
            expected_date_line = _event_date_line(
                claim["event_date_status"], claim["event_date"], claim["published_at"]
            )
            claim_lines, statement_index, evidence_index = _claim_block(draft["body"], claim)
            if expected_date_line not in claim_lines[statement_index:evidence_index + 1]:
                # Legacy grouped rows can put the claim and evidence URL on one line.
                if not (statement_index == evidence_index and evidence_index + 1 < len(claim_lines)
                        and claim_lines[evidence_index + 1] == expected_date_line):
                    raise ValueError(f"Утверждение #{claim['id']}: дата события должна быть указана рядом с ним в тексте")

        candidate_groups = [group for group in draft_groups(db, draft_id) if int(group["claim_count"]) > 1]
        for group in candidate_groups:
            if (group["review_same_event"] is None or group["sources_independent"] is None
                    or not group["reviewed_at"] or not group["review_note"].strip()):
                raise ValueError(
                    f"Группа-кандидат #{group['group_no']}: отдельно зафиксируйте, одно ли это событие "
                    "и независимы ли источники командой review group-check"
                )
        if draft["requires_group_review"] and not candidate_groups:
            if not checks.get("groups_checked"):
                raise ValueError(
                    "Автоматическая группировка недоступна или не выполнена. Проверьте вручную возможные "
                    "повторы и подтвердите это флагом --groups-checked."
                )
        else:
            checks["groups_checked"] = True

        lines = draft["body"].splitlines()
        bullet_positions = [index for index, line in enumerate(lines) if line.startswith("•")]
        if not bullet_positions:
            raise ValueError("В черновике нет пунктов со статусами; исправьте структуру перед одобрением")
        if len(bullet_positions) != len(claims):
            raise ValueError(
                "Число пунктов в тексте не совпадает с числом включённых утверждений; "
                "удалите неподкреплённые пункты или восстановите потерянные утверждения."
            )
        group_by_id = {int(group["id"]): group for group in draft_groups(db, draft_id)}
        seen_statements: set[int] = set()
        for claim in claims:
            claim_lines, statement_index, _evidence_index = _claim_block(draft["body"], claim)
            if statement_index in seen_statements:
                raise ValueError("Несколько утверждений связаны с одним пунктом текста; разделите их перед одобрением")
            seen_statements.add(statement_index)
            bullet = claim_lines[statement_index].upper()
            is_confirmed = "ПОДТВЕРЖДЕНО" in bullet
            is_source_statement = "ЗАЯВЛЕНИЕ ИСТОЧНИКА" in bullet
            is_analyst_assessment = "ОЦЕНКА АНАЛИТИКА" in bullet
            if sum((is_confirmed, is_source_statement, is_analyst_assessment)) != 1:
                raise ValueError(
                    f"Утверждение #{claim['id']}: укажите ровно один статус — подтверждённое, "
                    "заявление источника или оценка аналитика."
                )
            if claim["source_level"] == "analysis_osint":
                if not is_analyst_assessment:
                    raise ValueError(
                        f"Утверждение #{claim['id']}: аналитический материал должен быть обозначен как оценка аналитика."
                    )
            elif is_analyst_assessment:
                raise ValueError(
                    f"Утверждение #{claim['id']}: статус «оценка аналитика» не соответствует уровню источника."
                )
            if is_confirmed:
                group = group_by_id.get(int(claim["group_id"]))
                independent_sources = db.execute(
                    """SELECT COUNT(DISTINCT source_id) AS source_count,
                              SUM(CASE WHEN same_url_other_source=1 THEN 1 ELSE 0 END) AS repeated_urls,
                              SUM(CASE WHEN source_level='analysis_osint' THEN 1 ELSE 0 END) AS analyses
                       FROM draft_claims WHERE group_id=? AND included=1""", (claim["group_id"],)
                ).fetchone()
                if (group is None or group["review_same_event"] != 1 or group["sources_independent"] != 1
                        or int(group["claim_count"]) < 2
                        or int(independent_sources["source_count"] or 0) < 2
                        or int(independent_sources["repeated_urls"] or 0) > 0
                        or int(independent_sources["analyses"] or 0) > 0):
                    raise ValueError(
                        f"Утверждение #{claim['id']}: статус «подтверждено» допустим только при нескольких "
                        "разных, независимо проверенных неаналитических источниках об одном событии."
                    )
        if seen_statements != set(bullet_positions):
            raise ValueError("Каждый пункт текста должен однозначно соответствовать сохранённому утверждению")
    return checks


@_pending_mutation
def set_decision(db: sqlite3.Connection, draft_id: int, decision: str,
                 checks: dict[str, bool] | None = None) -> None:
    if decision not in {"approved", "rejected"}:
        raise ValueError("Решение должно быть approved или rejected")
    draft = get_draft(db, draft_id)
    checks = validate_approval(db, draft, checks or {}) if decision == "approved" else {}
    fingerprint = approval_fingerprint(db, draft) if decision == "approved" else ""
    db.execute(
        """UPDATE drafts SET status=?,updated_at=?,approved_revision=?,approved_hash=?
           WHERE id=? AND status='pending' AND revision=?""",
        (decision, now_iso(), draft["revision"] if decision == "approved" else None,
         fingerprint, draft_id, draft["revision"]),
    )
    _audit(db, f"draft_{decision}", "draft", draft_id,
           {"editor_checks": checks, "revision": draft["revision"], "body_hash": fingerprint})


def _approval_event_checks(db: sqlite3.Connection, draft_id: int) -> dict[str, bool]:
    event = db.execute(
        """SELECT details FROM events WHERE kind='draft_approved' AND entity_type='draft'
           AND entity_id=? ORDER BY id DESC LIMIT 1""", (str(draft_id),)
    ).fetchone()
    try:
        details = json.loads(event["details"]) if event else {}
        checks = details.get("editor_checks", {})
        return checks if isinstance(checks, dict) else {}
    except (TypeError, AttributeError, json.JSONDecodeError):
        return {}


def approval_blockers(db: sqlite3.Connection, draft_id: int,
                      now: datetime | None = None) -> list[str]:
    """Read-only readiness for the editor; approval still requires attestation."""
    draft = get_draft(db, draft_id)
    blockers: list[str] = []
    if draft["status"] not in {"pending", "approved"}:
        blockers.append(f"Черновик имеет статус {draft['status']}")
    checks = {"evidence_checked": True, "independence_checked": True, "groups_checked": True}
    if draft["status"] == "approved":
        checks = _approval_event_checks(db, draft_id)
        if (draft["approved_revision"] != draft["revision"] or not draft["approved_hash"]
                or draft["approved_hash"] != approval_fingerprint(db, draft)):
            blockers.append("Одобрение не относится к текущей версии. Верните черновик на проверку и одобрите заново.")
    for claim in draft_claims(db, draft_id):
        if not claim["included"]:
            continue
        if not claim_link_check_is_current(claim, now=now):
            blockers.append(f"Утверждение #{claim['id']}: требуется свежая проверка ссылки (claim-check)")
        if (claim["event_date_status"] not in {"explicit", "same_day"}
                or not claim["event_date"] or not claim["event_date_reviewed_at"]):
            blockers.append(f"Утверждение #{claim['id']}: требуется проверка даты события (event-date)")
    if db.execute("SELECT 1 FROM revision_alerts WHERE draft_id=? AND resolution IS NULL LIMIT 1",
                  (draft_id,)).fetchone():
        blockers.append("Есть непроверенная новая версия источника (revision-check)")
    for group in draft_groups(db, draft_id):
        if int(group["claim_count"] or 0) > 1 and (
            group["review_same_event"] is None or group["sources_independent"] is None
            or not group["reviewed_at"] or not group["review_note"].strip()
        ):
            blockers.append(f"Группа #{group['group_no']}: требуется редакторская проверка (group-check)")
    try:
        validate_approval(db, draft, checks, now=now)
    except (ValueError, TypeError, KeyError) as exc:
        blockers.append(str(exc))
    return list(dict.fromkeys(blockers))


def reopen_draft(db: sqlite3.Connection, draft_id: int,
                 expected_revision: int | None = None) -> None:
    """Explicitly revoke approval without changing the saved text or evidence."""
    db.execute("BEGIN IMMEDIATE")
    try:
        draft = get_draft(db, draft_id)
        if draft["status"] not in {"approved", "rejected"}:
            raise ValueError("Вернуть на проверку можно approved или rejected черновик")
        if expected_revision is not None and draft["revision"] != expected_revision:
            raise ValueError("Черновик уже изменён. Обновите карточку.")
        if db.execute("SELECT 1 FROM publish_attempts WHERE draft_id=? AND state IN ('started','unknown','succeeded') LIMIT 1",
                      (draft_id,)).fetchone():
            raise ValueError("Сначала разрешите незавершённую попытку публикации; опубликованный текст не переоткрывается.")
        db.execute("""UPDATE drafts SET status='pending',revision=revision+1,approved_revision=NULL,
                   approved_hash='',updated_at=? WHERE id=?""", (now_iso(), draft_id))
        _audit(db, "draft_reopened", "draft", draft_id, {"previous_status": draft["status"]})
        db.commit()
    except Exception:
        db.rollback()
        raise
