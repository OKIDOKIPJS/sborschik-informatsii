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
from .review_queue import _audit, approval_blockers, approval_fingerprint, get_draft

API_URL = "https://platform-api2.max.ru/messages"
MOSCOW = timezone(timedelta(hours=3))
PUBLISH_AT = time(14, 0)
PUBLISH_LEASE = timedelta(minutes=5)


def _redact(value: object, token: str | None) -> str:
    text = str(value)
    if token:
        for secret in {token, urllib.parse.quote(token, safe=""), urllib.parse.quote_plus(token)}:
            text = text.replace(secret, "[REDACTED_SECRET]")
    return text[:2000]


def _process_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            # An access-denied process is alive; unknown errors fail closed.
            return ctypes.get_last_error() != 87
        try:
            exit_code = wintypes.DWORD()
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)) or exit_code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


def publish_preflight(db: sqlite3.Connection, draft_id: int,
                      now: datetime | None = None, *,
                      expected_revision: int | None = None) -> list[str]:
    """Return publication blockers without secrets, network, or state changes."""
    draft = get_draft(db, draft_id)
    blockers = approval_blockers(db, draft_id)
    if expected_revision is not None and draft["revision"] != expected_revision:
        blockers.insert(0, "Черновик уже изменён. Обновите карточку и подтвердите публикацию текущей версии.")
    if draft["status"] != "approved":
        blockers.insert(0, "Публиковать можно только черновик со статусом approved")
    for check in (_assert_selected_batch_part, _assert_daily_slot_available):
        try:
            check(db, draft, now=now)
        except ValueError as exc:
            blockers.append(str(exc))
    previous = db.execute(
        "SELECT state FROM publish_attempts WHERE draft_id=? ORDER BY attempt_no DESC LIMIT 1", (draft_id,)
    ).fetchone()
    if previous and previous["state"] in {"started", "unknown", "succeeded"}:
        blockers.append("Есть незавершённая или успешная попытка. Проверьте канал и выполните publish-resolve.")
    return list(dict.fromkeys(blockers))


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
    if not isinstance(body, dict) or not isinstance(body.get("mid"), str) or not body["mid"].strip():
        raise ValueError("В ответе MAX отсутствует ID созданного сообщения (message.body.mid)")
    message_url = message.get("url")
    if not isinstance(message_url, str) or not _valid_max_post_url(message_url):
        raise ValueError("В ответе MAX отсутствует действительная публичная HTTPS-ссылка на пост")
    return message_url, body["mid"]


def publish(db: sqlite3.Connection, draft_id: int, dry_run: bool = False,
            now: datetime | None = None, *,
            expected_revision: int | None = None) -> dict[str, str]:
    # Freeze the version at invocation even for CLI callers without an explicit
    # browser confirmation. Credential storage may involve slow external I/O.
    if expected_revision is None:
        expected_revision = int(get_draft(db, draft_id)["revision"])
    blockers = publish_preflight(db, draft_id, now=now, expected_revision=expected_revision)
    if blockers:
        raise ValueError("; ".join(blockers))
    # Preview has the same editorial/slot checks and never opens credential
    # storage or contacts MAX. A read transaction gives it a consistent view.
    if dry_run:
        db.execute("BEGIN")
        try:
            blockers = publish_preflight(db, draft_id, now=now, expected_revision=expected_revision)
            if blockers:
                raise ValueError("; ".join(blockers))
            return {"status": "dry_run", "text": get_draft(db, draft_id)["body"]}
        finally:
            db.rollback()
    token, channel_id = get_max_credentials()
    if not token or not channel_id:
        raise RuntimeError("Настройте MAX credentials командой python -m app credentials set-max")
    # Recheck every invariant while reserving the attempt, after credential I/O.
    db.execute("BEGIN IMMEDIATE")
    try:
        blockers = publish_preflight(db, draft_id, now=now, expected_revision=expected_revision)
        if blockers:
            raise ValueError("; ".join(blockers))
        draft = get_draft(db, draft_id)
        previous = db.execute(
            "SELECT attempt_no FROM publish_attempts WHERE draft_id=? ORDER BY attempt_no DESC LIMIT 1", (draft_id,)
        ).fetchone()
        attempt_no = previous["attempt_no"] + 1 if previous else 1
        lease_expires = (datetime.now(timezone.utc) + PUBLISH_LEASE).isoformat(timespec="seconds")
        started_at = (now.isoformat(timespec="seconds") if now else now_iso())
        attempt_id = int(db.execute(
            """INSERT INTO publish_attempts(draft_id,attempt_no,state,created_at,lease_expires_at,
                   owner_pid,draft_revision,body_hash) VALUES(?,?,'started',?,?,?,?,?)""",
            (draft_id, attempt_no, started_at, lease_expires, os.getpid(), draft["revision"],
             approval_fingerprint(db, draft)),
        ).lastrowid)
        _audit(db, "publish_started", "draft", draft_id,
               {"attempt_no": attempt_no, "revision": draft["revision"], "lease_expires_at": lease_expires})
        db.commit()
    except Exception:
        db.rollback()
        raise
    try:
        request = urllib.request.Request(
            f"{API_URL}?{urllib.parse.urlencode({'chat_id': channel_id})}",
            data=json.dumps({"text": draft["body"], "format": "markdown", "notify": True},
                            ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": token, "Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            if not 200 <= getattr(response, "status", 200) < 300:
                raise ValueError("MAX вернул неожиданный HTTP status")
            result = json.loads(response.read().decode("utf-8"))
            message_url, api_message_id = _extract_channel_post(result)
    except urllib.error.HTTPError as exc:
        detail = _redact(exc.read(8192).decode("utf-8", errors="replace"), token)
        state = "failed" if 400 <= exc.code < 500 and exc.code != 408 else "unknown"
        _finish_failed_attempt(db, draft_id, attempt_id, state, f"HTTP {exc.code}: {detail}")
        raise RuntimeError(f"MAX API вернул HTTP {exc.code}: {detail}") from None
    except Exception as exc:
        detail = _redact(f"{type(exc).__name__}: {exc}", token)
        _finish_failed_attempt(db, draft_id, attempt_id, "unknown", detail)
        raise RuntimeError(
            "Не удалось достоверно узнать результат отправки. Проверьте канал и выполните publish-resolve; "
            "не отправляйте повторно вслепую."
        ) from None
    db.execute("BEGIN IMMEDIATE")
    try:
        changed = db.execute(
            "UPDATE publish_attempts SET state='succeeded',details=?,message_id=? WHERE id=? AND state='started'",
            (message_url, api_message_id, attempt_id),
        ).rowcount
        if changed != 1:
            raise RuntimeError("Состояние попытки изменилось во время отправки; требуется сверка канала")
        published_at = now_iso()
        db.execute(
            """UPDATE drafts SET status='published',published_at=?,updated_at=?,max_post_url=?,max_message_id=?
               WHERE id=?""", (published_at, published_at, message_url, api_message_id, draft_id),
        )
        _audit(db, "draft_published", "draft", draft_id,
               {"max_post_url": message_url, "api_message_id": api_message_id,
                "attempt_no": attempt_no, "revision": draft["revision"]})
        db.commit()
    except Exception:
        db.rollback()
        # The durable started attempt remains reserved if local persistence
        # fails after MAX accepted the message. Recovery must inspect the channel.
        raise
    return {"status": "published", "url": message_url, "message_id": api_message_id}


def _finish_failed_attempt(db: sqlite3.Connection, draft_id: int, attempt_id: int,
                           state: str, detail: str) -> None:
    db.execute("BEGIN IMMEDIATE")
    try:
        changed = db.execute(
            "UPDATE publish_attempts SET state=?,details=? WHERE id=? AND state='started'",
            (state, detail, attempt_id),
        ).rowcount
        if changed != 1:
            raise RuntimeError("Состояние попытки изменилось; проверьте журнал и канал")
        _audit(db, "publish_error" if state == "failed" else "publish_outcome_unknown", "draft", draft_id,
               {"attempt_id": attempt_id, "state": state, "error": detail})
        db.commit()
    except Exception:
        db.rollback()
        raise


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
        """SELECT d.id,a.state,a.created_at FROM publish_attempts a JOIN drafts d ON d.id=a.draft_id
           WHERE d.correction_of_id IS NULL AND d.id<>?
             AND (a.state IN ('started','unknown') OR
                  (a.state='succeeded' AND a.created_at>=? AND a.created_at<?))
           ORDER BY a.created_at LIMIT 1""",
        (draft["id"], start_utc, end_utc),
    ).fetchone()
    if active:
        raise ValueError(
            f"Уже есть попытка публикации основного поста #{active['id']} от {active['created_at']} "
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
    return not approval_blockers(db, int(draft["id"]))


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


def resolve_attempt(db: sqlite3.Connection, draft_id: int, posted: bool, url: str = "",
                    *, message_id: str = "", published_at: str | None = None,
                    now: datetime | None = None) -> None:
    """Reconcile unknown/dead attempts only after an explicit channel check.

    A live started owner cannot be released even after its lease expires. A dead
    owner must also outlive the lease. Legacy attempts use created_at + 5 minutes.
    """
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    db.execute("BEGIN IMMEDIATE")
    try:
        attempt = db.execute(
            "SELECT * FROM publish_attempts WHERE draft_id=? ORDER BY attempt_no DESC LIMIT 1", (draft_id,)
        ).fetchone()
        if attempt is None or attempt["state"] not in {"started", "unknown"}:
            raise ValueError("Для черновика нет неопределённой попытки публикации")
        if attempt["state"] == "started":
            try:
                expires = (datetime.fromisoformat(attempt["lease_expires_at"]) if attempt["lease_expires_at"]
                           else datetime.fromisoformat(attempt["created_at"]) + PUBLISH_LEASE)
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                raise ValueError("У попытки повреждено время lease; требуется ручная диагностика базы") from None
            if _process_alive(attempt["owner_pid"]):
                raise ValueError("Отправляющий процесс ещё работает. Дождитесь его завершения; живую started попытку освобождать нельзя.")
            if current < expires:
                raise ValueError(f"Резерв попытки действует до {expires.isoformat()}. Дождитесь истечения lease, затем проверьте канал.")
        if posted:
            if not _valid_max_post_url(url):
                raise ValueError("Для разрешения как опубликованного укажите действительную ссылку на пост HTTPS на max.ru")
            # Keep the date of the original send when reconciliation happens on
            # another day; an operator may supply the verified actual timestamp.
            timestamp = published_at or attempt["created_at"]
            try:
                observed = datetime.fromisoformat(timestamp)
                if observed.tzinfo is None:
                    observed = observed.replace(tzinfo=timezone.utc)
                if observed > current:
                    raise ValueError("Время публикации не может быть в будущем")
            except (TypeError, ValueError):
                raise ValueError("Укажите действительное время публикации ISO-8601, не позже текущего") from None
            timestamp = observed.astimezone(timezone.utc).isoformat(timespec="seconds")
            db.execute(
                """UPDATE drafts SET status='published',published_at=?,updated_at=?,max_post_url=?,max_message_id=?
                   WHERE id=?""", (timestamp, now_iso(), url, message_id, draft_id),
            )
            db.execute("UPDATE publish_attempts SET state='succeeded',details=?,message_id=? WHERE id=?",
                       (url, message_id, attempt["id"]))
            _audit(db, "publish_resolved_as_posted", "draft", draft_id,
                   {"url": url, "message_id": message_id, "published_at": timestamp,
                    "timestamp_basis": "editor" if published_at else "attempt_created_at"})
        else:
            if url.strip() or message_id.strip() or published_at:
                raise ValueError("Ссылка, message_id и время применимы только вместе с --posted")
            db.execute("UPDATE publish_attempts SET state='failed',details='Редактор проверил канал: публикации нет' WHERE id=?",
                       (attempt["id"],))
            _audit(db, "publish_resolved_as_not_posted", "draft", draft_id)
        db.commit()
    except Exception:
        db.rollback()
        raise
