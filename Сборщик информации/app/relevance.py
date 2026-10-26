"""Тематическая оценка источников и редакторская очередь релевантности."""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from .logs import now_iso, record_event


def assess_relevance(item: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    config = source.get("relevance_filter") or {}
    # Backward compatibility with the original exact-substring keyword list.
    if not config:
        config = {"context_terms": source.get("include_keywords") or []}
    searchable = f"{item.get('title', '')} {item.get('summary', '')}".casefold()

    def matches(terms: Any) -> list[str]:
        found: list[str] = []
        for value in terms or []:
            term = str(value).strip()
            if term and re.search(rf"(?<!\w){re.escape(term.casefold())}(?!\w)", searchable):
                found.append(term)
        return found

    # Only explicit front-line markers are eligible for automatic inclusion.
    # A country/region plus a generic conflict word is merely a review candidate:
    # it may describe strikes, diplomacy, or consequences far from the front.
    high = matches(config.get("high_confidence_terms"))
    geography = matches(config.get("geography_terms"))
    conflict = matches(config.get("conflict_terms"))
    frontline_actions = matches(config.get("frontline_action_terms"))
    context = matches(config.get("context_terms"))
    hits = list(dict.fromkeys(high + geography + conflict + frontline_actions + context))
    if high or (geography and frontline_actions):
        confidence, status = "high", "included"
        reason = ("Найден явный маркер линии фронта; редактор всё равно проверяет материал"
                  if high else "Совпали география и маркер изменения обстановки на фронте; нужна редакторская проверка")
    elif geography and conflict:
        confidence, status = "medium", "review"
        reason = "Есть географический и военный контекст, но связь именно с линией фронта требует проверки"
    elif hits:
        confidence, status = "medium", "review"
        reason = "Есть тематические маркеры, но связь с темой требует редакторской проверки"
    else:
        # Keep unmatched feed noise out of the active queue. The complete
        # record remains in the database and can be audited under `excluded`.
        confidence, status = "low", "excluded"
        reason = "Тематические маркеры не найдены; исключено из черновиков и активной очереди, доступно для аудита"
    return {"confidence": confidence, "status": status, "matched_terms": hits, "reason": reason}


def store_assessment(db: sqlite3.Connection, item_id: int, version_id: int, source_id: str,
                     assessment: dict[str, Any], seen_at: str) -> None:
    existing = db.execute(
        "SELECT * FROM relevance_assessments WHERE version_id=?", (version_id,)
    ).fetchone()
    matched = json.dumps(assessment["matched_terms"], ensure_ascii=False)
    if existing:
        # Re-fetching unchanged content updates observation counters. Preserve
        # an editor's include/exclude decision for this immutable version.
        status = existing["status"] if existing["reviewed_at"] else assessment["status"]
        reason = existing["reason"] if existing["reviewed_at"] else assessment["reason"]
        db.execute(
            """UPDATE relevance_assessments SET confidence=?,status=?,matched_terms=?,reason=?,
               last_seen_at=?,seen_count=seen_count+1 WHERE id=?""",
            (assessment["confidence"], status, matched, reason, seen_at, existing["id"]),
        )
    else:
        db.execute(
            """INSERT INTO relevance_assessments(
                item_id,version_id,source_id,confidence,status,matched_terms,reason,
                first_seen_at,last_seen_at
            ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (item_id, version_id, source_id, assessment["confidence"], assessment["status"],
             matched, assessment["reason"], seen_at, seen_at),
        )


def list_relevance(db: sqlite3.Connection, status: str = "review", limit: int = 100) -> list[sqlite3.Row]:
    if status not in {"review", "excluded", "included", "all"}:
        raise ValueError("Статус очереди должен быть review, excluded, included или all")
    where = "" if status == "all" else "WHERE a.status=?"
    params: tuple[Any, ...] = () if status == "all" else (status,)
    return list(db.execute(
        f"""SELECT a.*,v.title,v.summary,v.url,v.published_at,i.source_name
            FROM relevance_assessments a JOIN item_versions v ON v.id=a.version_id
            JOIN items i ON i.id=a.item_id {where}
            ORDER BY CASE a.status WHEN 'review' THEN 0 ELSE 1 END,
                     a.last_seen_at DESC,a.id DESC LIMIT ?""",
        (*params, max(1, min(limit, 500))),
    ))


def decide_relevance(db: sqlite3.Connection, assessment_id: int, include: bool, note: str) -> None:
    row = db.execute("SELECT * FROM relevance_assessments WHERE id=?", (assessment_id,)).fetchone()
    if row is None:
        raise ValueError(f"Запись оценки релевантности #{assessment_id} не найдена")
    if not note.strip():
        raise ValueError("Укажите, почему материал принят или отклонён")
    status = "included" if include else "excluded"
    reviewed_at = now_iso()
    db.execute(
        """UPDATE relevance_assessments SET status=?,editor_note=?,reviewed_at=? WHERE id=?""",
        (status, note.strip(), reviewed_at, assessment_id),
    )
    db.commit()
    try:
        matched = json.loads(row["matched_terms"] or "[]")
    except json.JSONDecodeError:
        matched = []
    record_event(db, "relevance_editor_decision", "relevance_assessment", assessment_id, {
        "item_id": int(row["item_id"]), "version_id": int(row["version_id"]),
        "source_id": row["source_id"], "previous_status": row["status"], "decision": status,
        "confidence": row["confidence"], "matched_terms": matched,
        "potential_missed_relevant_item": bool(include and row["confidence"] == "low" and not matched),
        "note": note.strip(),
    })


def relevance_report(db: sqlite3.Connection, days: int = 7) -> list[dict[str, Any]]:
    if days < 1 or days > 365:
        raise ValueError("Период отчёта должен быть от 1 до 365 дней")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    totals: dict[str, dict[str, Any]] = {}
    for row in db.execute(
        """SELECT entity_id,details FROM events WHERE kind='source_fetched'
           AND created_at>=? ORDER BY id""", (cutoff,)
    ):
        try:
            details = json.loads(row["details"] or "{}")
        except json.JSONDecodeError:
            continue
        source_id = str(row["entity_id"] or details.get("source_id") or "unknown")
        data = totals.setdefault(source_id, {
            "source_id": source_id, "runs": 0, "feed_items": 0, "high_confidence": 0,
            "medium_confidence": 0, "low_confidence": 0,
            "needs_review": 0, "duplicates": 0, "processing_errors": 0, "potential_misses": 0,
            "editor_included": 0, "editor_excluded": 0,
        })
        data["runs"] += 1
        for key in ("feed_items", "high_confidence", "medium_confidence", "low_confidence",
                    "needs_review", "duplicates"):
            data[key] += int(details.get(key, 0))
        data["processing_errors"] += int(details.get("filtered_out", 0))
    for row in db.execute(
        """SELECT details FROM events WHERE kind='relevance_editor_decision'
           AND created_at>=? ORDER BY id""", (cutoff,)
    ):
        try:
            details = json.loads(row["details"] or "{}")
        except json.JSONDecodeError:
            continue
        source_id = str(details.get("source_id") or "unknown")
        data = totals.setdefault(source_id, {
            "source_id": source_id, "runs": 0, "feed_items": 0, "high_confidence": 0,
            "medium_confidence": 0, "low_confidence": 0,
            "needs_review": 0, "duplicates": 0, "processing_errors": 0, "potential_misses": 0,
            "editor_included": 0, "editor_excluded": 0,
        })
        if details.get("decision") == "included":
            data["editor_included"] += 1
        elif details.get("decision") == "excluded":
            data["editor_excluded"] += 1
        if details.get("potential_missed_relevant_item"):
            data["potential_misses"] += 1
    return sorted(totals.values(), key=lambda value: value["source_id"])
