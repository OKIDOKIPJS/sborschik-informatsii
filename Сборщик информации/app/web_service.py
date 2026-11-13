"""Application services shared by the local editor and artifact exports."""
from __future__ import annotations

import html
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from .config import get_source, load_sources
from .draft_generator import MODEL_FALLBACK_NOTICE, OLDER_NOTE, _draft_footer, create_daily_draft
from .fetcher import fetch_enabled_sources, ingest_manual
from .logs import recent_events
from .relevance import decide_relevance, list_relevance, relevance_report
from .review_queue import (batch_parts, claim_link_check_is_current, close_batch_part,
                           defer_batch_part, draft_claims, draft_groups, edit_draft,
                           exclude_claim, get_draft, list_batches, mark_claim_link_checked,
                           review_candidate_group, review_claim_event_date,
                           review_source_revision, revision_alerts, select_batch_part,
                           set_decision)


def _rows(rows) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def draft_detail(db: sqlite3.Connection, draft_id: int) -> dict[str, Any]:
    from .publisher import publish_preflight
    from .review_queue import approval_blockers
    draft = dict(get_draft(db, draft_id))
    claims = _rows(draft_claims(db, draft_id))
    for claim in claims:
        claim["link_current"] = claim_link_check_is_current(claim)
        version = db.execute("SELECT summary FROM item_versions WHERE id=?", (claim["version_id"],)).fetchone()
        claim["summary"] = version[0] if version else ""
    draft.update(claims=claims, groups=_rows(draft_groups(db, draft_id)),
                 alerts=_rows(revision_alerts(db, draft_id)),
                 approval_blockers=approval_blockers(db, draft_id),
                 publish_blockers=publish_preflight(db, draft_id))
    part = db.execute("SELECT * FROM digest_batch_parts WHERE draft_id=?", (draft_id,)).fetchone()
    draft["part"] = dict(part) if part else None
    draft["parts"] = _rows(batch_parts(db, part["batch_id"])) if part else []
    draft["attempts"] = _rows(db.execute(
        "SELECT * FROM publish_attempts WHERE draft_id=? ORDER BY attempt_no DESC", (draft_id,)))
    return draft


def state(db: sqlite3.Connection) -> dict[str, Any]:
    drafts = _rows(db.execute(
        """SELECT d.id,d.title,d.status,d.created_at,d.updated_at,d.revision,
                  p.batch_id,p.part_no,p.disposition,
                  (SELECT COUNT(*) FROM draft_claims c WHERE c.draft_id=d.id AND c.included=1) AS claim_count
           FROM drafts d LEFT JOIN digest_batch_parts p ON p.draft_id=d.id
           ORDER BY d.id DESC LIMIT 300"""))
    config_error = None
    try:
        config = load_sources()
    except Exception as exc:
        config = {"sources": []}
        config_error = str(exc)
    sources = []
    for source in config["sources"]:
        last = db.execute("SELECT created_at,kind,details FROM events WHERE entity_type='source' AND entity_id=? "
                          "AND kind IN ('source_fetched','fetch_error') ORDER BY id DESC LIMIT 1", (source["id"],)).fetchone()
        sources.append({"id": source["id"], "name": source.get("name", source["id"]),
                        "enabled": source.get("enabled", False), "url": source.get("url", ""),
                        "level": source.get("level", ""),
                        "method": source.get("retrieval", {}).get("method", "manual"),
                        "last": dict(last) if last else None})
    counts = {row["status"]: row["n"] for row in db.execute("SELECT status,COUNT(*) AS n FROM drafts GROUP BY status")}
    return {"drafts": drafts, "counts": counts, "batches": _rows(list_batches(db)), "sources": sources,
            "config_error": config_error,
            "relevance": _rows(list_relevance(db, "all", 300)), "coverage": relevance_report(db, 7),
            "events": _rows(recent_events(db, 100)),
            "materials_count": db.execute("SELECT COUNT(*) FROM items").fetchone()[0],
            "pending_relevance": db.execute("SELECT COUNT(*) FROM relevance_assessments WHERE status='review'").fetchone()[0]}


def clean_publication_text(body: str) -> str:
    """Remove only known presentation hints, preserving claims, dates and citations."""
    lines = []
    for line in body.splitlines():
        if line in {_draft_footer(), MODEL_FALLBACK_NOTICE}:
            continue
        if line == OLDER_NOTE:
            line = "Материалы старше суток"
        if line.startswith("Окно отбора:") and ". Это дата публикации" in line:
            line = line.split(". Это дата публикации", 1)[0] + "."
        if line.startswith("— ") and ", опубликовано " in line:
            line = re.sub(r"; (?:запись есть в RSS/Atom, ссылка отвечает HTTP [^:]+|ссылка отвечает HTTP [^:]+|ссылку нужно проверить вручную|проверка ссылки не сохранена): (https?://)", r"; \1", line)
        lines.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def perform(db: sqlite3.Connection, action: str, data: dict[str, Any]) -> dict[str, Any]:
    """Explicit action allowlist: no arbitrary SQL, commands or filesystem paths."""
    if action == "fetch":
        return {"results": fetch_enabled_sources(db, load_sources()["sources"])}
    if action == "draft":
        return {"draft_id": create_daily_draft(db, int(data.get("hours", 24)))}
    if action == "ingest":
        source = get_source(load_sources(), str(data.get("source", "")))
        result = ingest_manual(db, source, str(data.get("title", "")), str(data.get("url", "")),
                               str(data.get("published_at", "")), str(data.get("summary", "")))
        return {"result": result}
    if action == "relevance":
        if not isinstance(data.get("include"), bool):
            raise ValueError("Укажите решение о включении материала")
        decide_relevance(db, int(data["id"]), data["include"], str(data.get("note", "")))
        return {"status": "saved"}
    draft_id = int(data["id"])
    draft = get_draft(db, draft_id)
    if not isinstance(data.get("revision"), int) or data["revision"] != draft["revision"]:
        raise ValueError("Черновик изменился. Обновите карточку перед сохранением решения.")
    note = str(data.get("note", "")).strip()
    claim_id = int(data.get("claim_id", 0))
    if action in {"edit", "clean-text"}:
        body = clean_publication_text(draft["body"]) if action == "clean-text" else str(data["text"])
        edit_draft(db, draft_id, body, expected_revision=data["revision"])
    elif action in {"approve", "reject"}:
        checks = data.get("checks", {})
        if not isinstance(checks, dict) or any(not isinstance(v, bool) for v in checks.values()):
            raise ValueError("Подтверждения редактора должны быть логическими значениями")
        set_decision(db, draft_id, "approved" if action == "approve" else "rejected", checks,
                     expected_revision=data["revision"])
    elif action == "reopen":
        from .review_queue import reopen_draft
        reopen_draft(db, draft_id, expected_revision=data["revision"])
    elif action == "claim-check":
        if data.get("confirmed") is not True or len(note) < 8 or len(note) > 1000:
            raise ValueError("Подтвердите чтение источника и сохраните основание проверки (8–1000 символов)")
        mark_claim_link_checked(db, draft_id, claim_id, True, note, expected_revision=data["revision"])
    elif action == "event-date":
        review_claim_event_date(db, draft_id, claim_id, str(data.get("mode", "explicit")), data.get("date"), note,
                                expected_revision=data["revision"])
    elif action == "exclude":
        exclude_claim(db, draft_id, claim_id, note, expected_revision=data["revision"])
    elif action == "group":
        if not isinstance(data.get("same_event"), bool) or not isinstance(data.get("independent"), bool):
            raise ValueError("Выберите оба решения по группе")
        review_candidate_group(db, draft_id, int(data["group_no"]), data["same_event"], data["independent"], note,
                               expected_revision=data["revision"])
    elif action == "revision":
        review_source_revision(db, draft_id, claim_id, str(data["mode"]), note, expected_revision=data["revision"])
    elif action in {"select", "close", "defer"}:
        part = db.execute("SELECT batch_id FROM digest_batch_parts WHERE draft_id=?", (draft_id,)).fetchone()
        if not part:
            raise ValueError("Черновик не относится к партии")
        if action == "select":
            select_batch_part(db, part["batch_id"], draft_id, expected_revision=data["revision"])
        elif action == "close":
            close_batch_part(db, part["batch_id"], draft_id, note, expected_revision=data["revision"])
        else:
            defer_batch_part(db, part["batch_id"], draft_id, str(data["date"]), note,
                             expected_revision=data["revision"])
    elif action in {"dry-run", "publish"}:
        from .publisher import publish
        if action == "publish" and data.get("confirmed") is not True:
            raise ValueError("Подтвердите публикацию в MAX")
        return publish(db, draft_id, dry_run=action == "dry-run", expected_revision=data["revision"])
    elif action == "resolve":
        from .publisher import resolve_attempt
        if data.get("confirmed") is not True or not isinstance(data.get("posted"), bool):
            raise ValueError("Подтвердите результат ручной проверки канала")
        resolve_attempt(db, draft_id, data["posted"], str(data.get("url", "")))
    elif action == "correction":
        from .corrections import create_correction_draft
        return {"draft_id": create_correction_draft(db, draft_id, note, str(data["text"]), str(data["url"]))}
    elif action == "correction-check":
        from .corrections import mark_correction_evidence_checked
        if data.get("confirmed") is not True:
            raise ValueError("Подтвердите чтение основания исправления")
        mark_correction_evidence_checked(db, draft_id, note, str(data.get("evidence", "")),
                                         expected_revision=data["revision"])
    else:
        raise ValueError("Неизвестное действие")
    return {"status": "saved", "draft_id": draft_id}


def export_draft(db: sqlite3.Connection, draft_id: int, format: str = "json") -> tuple[bytes, str, str]:
    detail = draft_detail(db, draft_id)
    if format == "json":
        return json.dumps(detail, ensure_ascii=False, indent=2).encode("utf-8"), "application/json; charset=utf-8", f"draft-{draft_id}.json"
    if format == "txt":
        return detail["body"].encode("utf-8"), "text/plain; charset=utf-8", f"draft-{draft_id}.txt"
    if format != "html":
        raise ValueError("Формат экспорта: json, html или txt")
    evidence = "".join(f"<tr><td>{html.escape(str(c['claim_text']))}</td><td>{html.escape(c['source_name'])}</td>"
                       f"<td>{html.escape(c['published_at'])}</td><td>{html.escape(c['event_date'] or 'Не установлена')}</td>"
                       f"<td>{html.escape(c['source_url'])}</td><td>{html.escape(c['link_check_note'])}</td></tr>"
                       for c in detail["claims"] if c["included"])
    document = ("<!doctype html><html lang='ru'><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
                "<title>Экспорт сводки</title><style>body{max-width:1100px;margin:40px auto;font:16px/1.6 system-ui;padding:20px}"
                "pre{white-space:pre-wrap;background:#f5f6f7;padding:24px}table{border-collapse:collapse;width:100%;font-size:13px}"
                "td,th{padding:12px;border:1px solid #ddd;text-align:left;overflow-wrap:anywhere}</style>"
                f"<h1>{html.escape(detail['title'])}</h1><p>Статус: {html.escape(detail['status'])}. "
                "Экспорт сохраняет состояние редакторской проверки; он не является подтверждением публикации.</p>"
                f"<pre>{html.escape(detail['body'])}</pre><h2>Основания</h2><table><tr><th>Утверждение</th><th>Источник</th>"
                f"<th>Публикация</th><th>Событие</th><th>URL</th><th>Проверка редактора</th></tr>{evidence}</table></html>")
    return document.encode("utf-8"), "text/html; charset=utf-8", f"draft-{draft_id}.html"
