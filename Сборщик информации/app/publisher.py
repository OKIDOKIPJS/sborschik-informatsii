"""Публикация одобренного черновика в канал MAX."""
from __future__ import annotations

import json
import os
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import urlsplit

from .credentials import get_max_credentials
from .logs import now_iso, record_event
from .review_queue import claim_link_check_is_current, get_draft

API_URL = "https://platform-api2.max.ru/messages"
MOSCOW = timezone(timedelta(hours=3))
PUBLISH_AT = time(14, 0)


def _valid_max_post_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        return bool(
            parts.scheme.lower() == "https" and host
            and (host == "max.ru" or host.endswith(".max.ru"))
            and parts.path not in {"", "/"}
            and not parts.username and not parts.password
            and parts.port in {None, 443}
        )
    except ValueError:
        return False


def _extract_channel_post(result: object) -> tuple[str, str]:
    """Validate the documented channel-post response before recording success."""
    if not isinstance(result, dict):
        raise ValueError("MAX вернул JSON не в виде объекта")
    message = result.get("message")
    if not isinstance(message, dict):
        raise ValueError("В ответе MAX отсутствует объект созданного сообщения")
    body = message.get("body")
    if not isinstance(body, dict) or not body.get("mid"):
        raise ValueError("В ответе MAX отсутствует ID созданного сообщения (message.body.mid)")
    message_url = message.get("url")
    if not isinstance(message_url, str) or not _valid_max_post_url(message_url):
        raise ValueError("В ответе MAX отсутствует действительная публичная HTTPS-ссылка на пост")
    return message_url, str(body["mid"])


def publish(db: sqlite3.Connection, draft_id: int, dry_run: bool = False,
            now: datetime | None = None) -> dict[str, str]:
    draft = get_draft(db, draft_id)
    if draft["status"] != "approved":
        raise ValueError("Публиковать можно только черновик со статусом approved")
    _assert_selected_batch_part(db, draft, now)
    token, channel_id = get_max_credentials()
    if not dry_run and (not token or not channel_id):
        raise RuntimeError(
            "Для публикации настройте MAX credentials командой "
            "python -m app credentials set-max либо задайте MAX_BOT_TOKEN и MAX_CHANNEL_ID"
        )
    if dry_run:
        _assert_daily_slot_available(db, draft, now=now)
        return {"status": "dry_run", "text": draft["body"]}
    # BEGIN IMMEDIATE serializes competing manual/scheduled publishers. The
    # started attempt is the daily-slot reservation until success or resolution.
    db.execute("BEGIN IMMEDIATE")
    try:
        draft = get_draft(db, draft_id)
        if draft["status"] != "approved":
            raise ValueError("Публиковать можно только черновик со статусом approved")
        _assert_selected_batch_part(db, draft, now)
        previous = db.execute("SELECT * FROM publish_attempts WHERE draft_id=? ORDER BY attempt_no DESC LIMIT 1",
                              (draft_id,)).fetchone()
        if previous and previous["state"] in {"started", "unknown", "succeeded"}:
            raise ValueError("Для этого черновика уже есть незавершённая или успешная попытка. Сначала проверьте канал и выполните publish-resolve.")
        _assert_daily_slot_available(db, draft, now=now)
        attempt_no = (previous["attempt_no"] + 1) if previous else 1
        started_at = now_iso()
        db.execute("INSERT INTO publish_attempts(draft_id,attempt_no,state,created_at) VALUES(?,?,'started',?)",
                   (draft_id, attempt_no, started_at))
        db.commit()
    except Exception:
        db.rollback()
        raise
    query = urllib.parse.urlencode({"chat_id": channel_id})
    request = urllib.request.Request(
        f"{API_URL}?{query}",
        data=json.dumps({"text": draft["body"], "format": "markdown", "notify": True},
                        ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": token, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
            message_url, api_message_id = _extract_channel_post(result)
    except urllib.error.HTTPError as exc:
        detail = exc.read(2000).decode("utf-8", errors="replace")
        state = "failed" if 400 <= exc.code < 500 else "unknown"
        db.execute("UPDATE publish_attempts SET state=?,details=? WHERE draft_id=? AND attempt_no=?",
                   (state, f"HTTP {exc.code}: {detail}", draft_id, attempt_no))
        db.commit()
        record_event(db, "publish_error", "draft", draft_id,
                     {"http_status": exc.code, "detail": detail})
        raise RuntimeError(f"MAX API вернул HTTP {exc.code}: {detail}") from exc
    except Exception as exc:
        db.execute("UPDATE publish_attempts SET state='unknown',details=? WHERE draft_id=? AND attempt_no=?",
                   (f"{type(exc).__name__}: {exc}", draft_id, attempt_no))
        db.commit()
        record_event(db, "publish_outcome_unknown", "draft", draft_id,
                     {"attempt_no": attempt_no, "error": f"{type(exc).__name__}: {exc}"})
        raise RuntimeError("Не удалось достоверно узнать результат отправки. Проверьте канал и выполните publish-resolve; не отправляйте повторно вслепую.") from exc
    published_at = now_iso()
    db.execute("UPDATE drafts SET status='published',published_at=?,updated_at=?,max_post_url=? WHERE id=?",
               (published_at, published_at, message_url, draft_id))
    db.execute("UPDATE publish_attempts SET state='succeeded',details=? WHERE draft_id=? AND attempt_no=?",
               (message_url, draft_id, attempt_no))
    db.commit()
    record_event(db, "draft_published", "draft", draft_id,
                 {"max_post_url": message_url, "api_message_id": api_message_id})
    return {"status": "published", "url": message_url}


def _assert_selected_batch_part(db: sqlite3.Connection, draft: sqlite3.Row,
                                now: datetime | None = None) -> None:
    if draft["correction_of_id"] is not None:
        return
    part = db.execute(
        """SELECT p.disposition,b.batch_date FROM digest_batch_parts p
           JOIN digest_batches b ON b.id=p.batch_id WHERE p.draft_id=?""", (draft["id"],)
    ).fetchone()
    if part is None:  # legacy draft created before batch support
        return
    if part["disposition"] != "selected":
        raise ValueError("Часть партии не выбрана для публикации. Используйте review select-part.")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    if part["batch_date"] != current.astimezone(MOSCOW).date().isoformat():
        raise ValueError("Выбранная часть относится к другой дате. Для переноса создайте новую партию из актуальных материалов.")


def _assert_daily_slot_available(db: sqlite3.Connection, draft: sqlite3.Row,
                                 now: datetime | None = None) -> None:
    """Enforce one main post per Moscow date for every publication entry point.

    Corrections are explicitly exempt and remain linked to their original post.
    """
    if draft["correction_of_id"] is not None:
        return
    start_utc, end_utc, local_date = _day_bounds_utc(now)
    published = db.execute(
        """SELECT id FROM drafts WHERE correction_of_id IS NULL AND status='published'
           AND published_at>=? AND published_at<? ORDER BY published_at LIMIT 1""",
        (start_utc, end_utc),
    ).fetchone()
    if published:
        raise ValueError(
            f"Основной пост за {local_date.isoformat()} по Москве уже опубликован "
            f"(черновик #{published['id']}); дневной лимит — один пост."
        )
    active = db.execute(
        """SELECT d.id,a.state FROM publish_attempts a JOIN drafts d ON d.id=a.draft_id
           WHERE d.correction_of_id IS NULL AND d.id<>? AND a.state IN ('started','unknown','succeeded')
             AND a.created_at>=? AND a.created_at<? ORDER BY a.created_at LIMIT 1""",
        (draft["id"], start_utc, end_utc),
    ).fetchone()
    if active:
        raise ValueError(
            f"Уже есть попытка публикации основного поста #{active['id']} за {local_date.isoformat()} "
            f"со статусом {active['state']}; проверьте её результат перед другой отправкой."
        )


def _day_bounds_utc(now: datetime | None = None) -> tuple[str, str, date]:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    local_date = current.astimezone(MOSCOW).date()
    start_local = datetime.combine(local_date, time.min, tzinfo=MOSCOW)
    end_local = start_local + timedelta(days=1)
    return (start_local.astimezone(timezone.utc).isoformat(timespec="seconds"),
            end_local.astimezone(timezone.utc).isoformat(timespec="seconds"), local_date)


def _approval_checks_complete(db: sqlite3.Connection, draft: sqlite3.Row) -> bool:
    if db.execute(
        "SELECT 1 FROM revision_alerts WHERE draft_id=? AND resolution IS NULL LIMIT 1",
        (draft["id"],),
    ).fetchone():
        return False
    event = db.execute(
        """SELECT details FROM events WHERE kind='draft_approved' AND entity_type='draft' AND entity_id=?
           ORDER BY id DESC LIMIT 1""", (str(draft["id"]),)
    ).fetchone()
    if event is None:
        return False
    try:
        details = json.loads(event["details"] or "{}")
        checks = details.get("editor_checks", {})
    except (AttributeError, TypeError, json.JSONDecodeError):
        return False
    required = ["evidence_checked", "independence_checked"]
    if draft["requires_group_review"]:
        required.append("groups_checked")
    if not all(checks.get(key) is True for key in required):
        return False
    claims = list(db.execute(
        "SELECT * FROM draft_claims WHERE draft_id=? AND included=1", (draft["id"],)
    ))
    if not claims or any(
        not all(claim[key] for key in ("claim_text", "source_id", "source_name", "published_at", "source_url"))
        or claim["event_date_status"] not in {"explicit", "same_day"}
        or not claim["event_date"] or not claim["event_date_reviewed_at"]
        or not claim_link_check_is_current(claim)
        for claim in claims
    ):
        return False
    unchecked_groups = db.execute(
        """SELECT COUNT(*) FROM (
             SELECT g.id FROM draft_groups g JOIN draft_claims c ON c.group_id=g.id
             WHERE g.draft_id=? GROUP BY g.id
             HAVING SUM(CASE WHEN c.included=1 THEN 1 ELSE 0 END)>1
               AND (g.reviewed_at IS NULL OR g.review_note='' OR g.review_same_event IS NULL
                    OR g.sources_independent IS NULL)
           )""", (draft["id"],)
    ).fetchone()[0]
    return unchecked_groups == 0


def publish_scheduled(db: sqlite3.Connection, dry_run: bool = False,
                      now: datetime | None = None) -> dict[str, object]:
    """Publish at most one approved regular digest created on the current Moscow date."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    start_utc, end_utc, local_date = _day_bounds_utc(current)
    if not dry_run and current.astimezone(MOSCOW).time().replace(tzinfo=None) < PUBLISH_AT:
        record_event(db, "scheduled_publish_skipped", details={
            "reason": "before_scheduled_time", "moscow_date": local_date.isoformat(),
            "scheduled_time": PUBLISH_AT.strftime("%H:%M"),
        })
        return {"status": "not_due", "reason": "Плановая публикация разрешена не раньше 14:00 по Москве."}
    already_published = db.execute(
        """SELECT id FROM drafts WHERE correction_of_id IS NULL AND status='published'
           AND published_at>=? AND published_at<? ORDER BY published_at LIMIT 1""",
        (start_utc, end_utc),
    ).fetchone()
    if already_published:
        record_event(db, "scheduled_publish_skipped", "draft", already_published["id"],
                     {"reason": "daily_post_already_published", "moscow_date": local_date.isoformat()})
        return {"status": "skipped", "reason": "На сегодня уже опубликован основной пост.",
                "draft_id": str(already_published["id"])}

    chosen_rows = list(db.execute(
        """SELECT d.id,d.status,p.part_no,p.disposition,b.id AS batch_id
           FROM digest_batches b JOIN digest_batch_parts p ON p.batch_id=b.id
           JOIN drafts d ON d.id=p.draft_id
           WHERE b.batch_date=? AND p.disposition='selected' AND d.correction_of_id IS NULL""",
        (local_date.isoformat(),),
    ))
    if not chosen_rows:
        record_event(db, "scheduled_publish_skipped", details={
            "reason": "no_selected_batch_part", "moscow_date": local_date.isoformat(),
        })
        return {"status": "skipped", "reason": "На сегодня не выбрана часть партии сводки."}
    if len(chosen_rows) > 1:
        candidates = [
            {"draft_id": int(row["id"]), "batch_id": row["batch_id"], "part_no": int(row["part_no"])}
            for row in chosen_rows
        ]
        record_event(db, "scheduled_publish_blocked", details={
            "reason": "multiple_selected_batch_parts", "moscow_date": local_date.isoformat(),
            "candidates": candidates,
        })
        return {
            "status": "needs_review",
            "reason": "На сегодня выбрано несколько частей из разных партий; снимите лишний выбор.",
            "candidates": candidates,
        }
    chosen = chosen_rows[0]
    if chosen["status"] != "approved":
        record_event(db, "scheduled_publish_blocked", details={
            "reason": "selected_part_not_approved", "draft_id": int(chosen["id"]),
            "batch_id": chosen["batch_id"], "moscow_date": local_date.isoformat(),
        })
        return {"status": "skipped", "reason": f"Выбранная часть #{chosen['part_no']} не одобрена; другая часть автоматически не выбрана.",
                "draft_id": str(chosen["id"]), "batch_id": chosen["batch_id"]}

    draft_id = int(chosen["id"])
    draft = get_draft(db, draft_id)
    if not _approval_checks_complete(db, draft):
        record_event(db, "scheduled_publish_blocked", "draft", draft_id, {
            "reason": "editorial_approval_checks_missing", "moscow_date": local_date.isoformat(),
        })
        return {"status": "needs_review", "reason": "Не найдены все отметки редакторской проверки; публикация остановлена.",
                "draft_id": str(draft_id)}

    if dry_run:
        preview = publish(db, draft_id, dry_run=True, now=current)
        return {**preview, "draft_id": str(draft_id)}
    record_event(db, "scheduled_publish_selected", "draft", draft_id,
                 {"moscow_date": local_date.isoformat(), "batch_id": chosen["batch_id"],
                  "part_no": chosen["part_no"]})
    return {**publish(db, draft_id, now=current), "draft_id": str(draft_id)}


def resolve_attempt(db: sqlite3.Connection, draft_id: int, posted: bool, url: str = "") -> None:
    attempt = db.execute("SELECT * FROM publish_attempts WHERE draft_id=? ORDER BY attempt_no DESC LIMIT 1",
                         (draft_id,)).fetchone()
    if attempt is None or attempt["state"] not in {"started", "unknown"}:
        raise ValueError("Для черновика нет неопределённой попытки публикации")
    if posted:
        if not _valid_max_post_url(url):
            raise ValueError("Для разрешения как опубликованного укажите действительную ссылку на пост HTTPS на max.ru")
        published_at = now_iso()
        db.execute("UPDATE drafts SET status='published',published_at=?,updated_at=?,max_post_url=? WHERE id=?",
                   (published_at, published_at, url, draft_id))
        db.execute("UPDATE publish_attempts SET state='succeeded',details=? WHERE id=?", (url, attempt["id"]))
        record_event(db, "publish_resolved_as_posted", "draft", draft_id, {"url": url})
    else:
        if url.strip():
            raise ValueError("--url применим только вместе с --posted")
        db.execute("UPDATE publish_attempts SET state='failed',details='Редактор проверил канал: публикации нет' WHERE id=?",
                   (attempt["id"],))
        record_event(db, "publish_resolved_as_not_posted", "draft", draft_id)
    db.commit()
