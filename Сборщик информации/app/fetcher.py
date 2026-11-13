"""Получение материалов из разрешённых RSS и ручной ввод."""
from __future__ import annotations

import json
import os
import sqlite3
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any

from .deduplicator import insert_normalized
from .logs import now_iso, record_event
from .normalizer import normalize_item, normalize_url
from .publication_validator import check_publication_link, validate_publication_url
from .relevance import assess_relevance, store_assessment

USER_AGENT = "SborshchikInformatsii/0.1 (RSS reader; contact: channel owner)"


def _tag_text(element: ET.Element | None) -> str:
    return "".join(element.itertext()).strip() if element is not None else ""


def parse_feed(xml_data: bytes) -> list[dict[str, str | None]]:
    root = ET.fromstring(xml_data)
    if root.tag not in {"rss", "{http://www.w3.org/2005/Atom}feed"}:
        raise ValueError("Источник вернул документ, не являющийся RSS 2.0 или Atom")
    if root.tag == "rss" and root.find("channel") is None:
        raise ValueError("RSS не содержит обязательный элемент channel")
    items: list[dict[str, str | None]] = []
    # RSS 2.0
    for node in root.findall(".//item"):
        items.append({
            "title": _tag_text(node.find("title")),
            "url": _tag_text(node.find("link")),
            "published_at": _tag_text(node.find("pubDate")) or _tag_text(node.find("date")) or None,
            "summary": _tag_text(node.find("description")),
        })
    # Atom
    if not items:
        ns = {"a": "http://www.w3.org/2005/Atom"}
        for node in root.findall("a:entry", ns):
            link = next((el.attrib.get("href", "") for el in node.findall("a:link", ns)
                         if el.attrib.get("rel", "alternate") == "alternate"), "")
            items.append({
                "title": _tag_text(node.find("a:title", ns)),
                "url": link,
                "published_at": _tag_text(node.find("a:published", ns)) or _tag_text(node.find("a:updated", ns)) or None,
                "summary": _tag_text(node.find("a:summary", ns)) or _tag_text(node.find("a:content", ns)),
            })
    # Keep malformed entries in the result: _store rejects them explicitly and
    # the collection report must include them in filtered_out/item_rejected.
    return items


def _version_fingerprint(item: dict[str, Any]) -> str:
    content = json.dumps(
        [item["url"], item["title"], item["summary"], item["published_at"]],
        ensure_ascii=False, separators=(",", ":"),
    )
    import hashlib
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _append_version(db: sqlite3.Connection, item_id: int, item: dict[str, Any], raw_text: str,
                    payload: dict[str, Any], changed_fields: list[str], detected_at: str) -> int:
    latest = db.execute(
        "SELECT * FROM item_versions WHERE item_id=? ORDER BY version_no DESC LIMIT 1", (item_id,)
    ).fetchone()
    version_no = int(latest["version_no"] + 1) if latest else 1
    cursor = db.execute(
        """INSERT INTO item_versions(
            item_id,version_no,title,summary,url,published_at,fetched_at,content_hash,
            fingerprint,raw_text,payload,changed_fields,detected_at,last_checked_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (item_id, version_no, item["title"], item["summary"], item["url"],
         item["published_at"], item["fetched_at"], item["content_hash"],
         _version_fingerprint(item), raw_text,
         json.dumps(payload, ensure_ascii=False, default=str),
         json.dumps(changed_fields, ensure_ascii=False), detected_at, detected_at),
    )
    return int(cursor.lastrowid)


def _notify_drafts_of_revision(db: sqlite3.Connection, item_id: int,
                               current_version_id: int, detected_at: str) -> tuple[list[int], list[dict[str, Any]]]:
    affected = db.execute(
        """SELECT c.id AS claim_id,c.draft_id,c.version_id,d.status FROM draft_claims c
           JOIN drafts d ON d.id=c.draft_id
           WHERE c.item_id=? AND c.version_id IS NOT NULL AND c.version_id<>? AND c.included=1
             AND d.status IN ('pending','approved')""",
        (item_id, current_version_id),
    ).fetchall()
    affected_drafts: list[int] = []
    for row in affected:
        draft_id = int(row["draft_id"])
        db.execute(
            """INSERT OR IGNORE INTO revision_alerts(
                draft_id,claim_id,previous_version_id,current_version_id,created_at
            ) VALUES(?,?,?,?,?)""",
            (draft_id, int(row["claim_id"]), int(row["version_id"]), current_version_id, detected_at),
        )
        # An approved but not yet published post must return to editorial review.
        if row["status"] == "approved":
            db.execute("UPDATE drafts SET status='pending',updated_at=? WHERE id=?",
                       (detected_at, draft_id))
        affected_drafts.append(draft_id)
    published = db.execute(
        """SELECT DISTINCT d.id,d.max_post_url FROM draft_claims c JOIN drafts d ON d.id=c.draft_id
           WHERE c.item_id=? AND c.version_id<>? AND c.included=1 AND d.status='published'
           ORDER BY d.id""", (item_id, current_version_id)
    ).fetchall()
    return sorted(set(affected_drafts)), [
        {"draft_id": int(row["id"]), "max_post_url": row["max_post_url"] or ""} for row in published
    ]


def _store(db: sqlite3.Connection, source: dict[str, Any], raw: dict[str, Any], origin: str) -> str:
    fetched_at = now_iso()
    raw_text = str(raw.get("summary") or "")
    normalized = normalize_item({
        **raw, "source_id": source["id"], "source_name": source["name"],
        "source_type": source.get("type", "unknown"), "source_level": source.get("level", ""),
        "fetched_at": fetched_at,
    })
    # A recently cached HEAD result cannot override an updated domain policy.
    validate_publication_url(source, normalized["url"])
    assessment = assess_relevance(normalized, source)
    if origin == "manual":
        assessment = {"confidence": "high", "status": "included", "matched_terms": [],
                      "reason": "Материал добавлен редактором вручную"}
    cached = db.execute(
        "SELECT id,payload,origin FROM raw_items WHERE source_id=? AND url=?",
        (source["id"], normalized["url"]),
    ).fetchone()
    publication_check = None
    if cached and cached["origin"] == origin:
        try:
            cached_payload = json.loads(cached["payload"] or "{}")
            candidate = cached_payload.get("publication_check", {}) if isinstance(cached_payload, dict) else {}
            if not isinstance(candidate, dict):
                candidate = {}
            checked_at = datetime.fromisoformat(candidate.get("checked_at", ""))
            if checked_at.tzinfo is None:
                raise ValueError("У даты проверки ссылки отсутствует часовой пояс")
            age = datetime.now(timezone.utc) - checked_at.astimezone(timezone.utc)
            if (timedelta(0) <= age < timedelta(hours=12)
                    and candidate.get("status") in {"http_ok", "http_ok_feed", "manual_check_required"}):
                publication_check = candidate
        except (ValueError, TypeError, json.JSONDecodeError):
            publication_check = None
    if publication_check is None:
        publication_check = check_publication_link(source, normalized["url"], origin)
    stored_raw = {**raw, "publication_check": publication_check}
    raw_values = (normalized["title"], raw_text, normalized["url"],
                  normalized["published_at"], fetched_at, origin,
                  json.dumps(stored_raw, ensure_ascii=False, default=str))
    identity = db.execute("SELECT id FROM items WHERE source_id=? AND url=?",
                          (source["id"], normalized["url"])).fetchone()
    if cached:
        # Keep the raw cache current, while immutable item_versions preserve
        # every distinct source revision used by drafts.
        raw_id = int(cached["id"])
        db.execute(
            """UPDATE raw_items SET title=?,text=?,url=?,published_at=?,fetched_at=?,origin=?,payload=?
               WHERE id=?""", (*raw_values, raw_id),
        )
    else:
        cursor = db.execute(
            """INSERT INTO raw_items(source_id,title,text,url,published_at,fetched_at,origin,payload)
               VALUES(?,?,?,?,?,?,?,?)""",
            (source["id"], *raw_values),
        )
        raw_id = int(cursor.lastrowid)
    db.commit()
    version_id: int | None = None
    if identity:
        item_id = int(identity["id"])
        latest = db.execute(
            "SELECT * FROM item_versions WHERE item_id=? ORDER BY version_no DESC LIMIT 1", (item_id,)
        ).fetchone()
        if latest is None:
            version_id = _append_version(db, item_id, normalized, raw_text, stored_raw, [], fetched_at)
            result = "inserted"
        else:
            changed_fields = [field for field in ("title", "summary", "published_at", "url")
                              if latest[field] != normalized[field]]
            if not changed_fields:
                version_id = int(latest["id"])
                db.execute("UPDATE item_versions SET last_checked_at=? WHERE id=?",
                           (fetched_at, latest["id"]))
                record_event(db, "source_item_seen_again", "source", source["id"],
                             {"url": normalized["url"], "version": latest["version_no"],
                              "last_checked_at": fetched_at})
                result = "duplicate"
            else:
                version_id = _append_version(db, item_id, normalized, raw_text, stored_raw,
                                              changed_fields, fetched_at)
                affected_drafts, published_drafts = _notify_drafts_of_revision(
                    db, item_id, version_id, fetched_at
                )
                record_event(db, "source_revision_detected", "item", item_id, {
                    "source_id": source["id"], "url": normalized["url"],
                    "previous_version": latest["version_no"],
                    "current_version": int(latest["version_no"]) + 1,
                    "changed_fields": changed_fields,
                    "affected_drafts": affected_drafts,
                    "published_drafts": published_drafts,
                })
                result = "revised"
    else:
        item_id, result = insert_normalized(db, raw_id, normalized)
        if result == "inserted" and item_id is not None:
            version_id = _append_version(db, item_id, normalized, raw_text, stored_raw, [], fetched_at)
        elif item_id is not None:
            latest = db.execute(
                "SELECT id FROM item_versions WHERE item_id=? ORDER BY version_no DESC LIMIT 1", (item_id,)
            ).fetchone()
            version_id = int(latest["id"]) if latest else _append_version(
                db, item_id, normalized, raw_text, stored_raw, [], fetched_at
            )
    if item_id is not None and version_id is not None:
        store_assessment(db, item_id, version_id, source["id"], assessment, fetched_at)
    db.commit()
    record_event(db, "publication_link_checked", "source", source["id"],
                 {"url": normalized["url"], "origin": origin, **publication_check})
    return result


def _fetch_reliefweb_api(source: dict[str, Any], retrieval: dict[str, Any]) -> list[dict[str, Any]]:
    appname_env = str(retrieval.get("appname_env") or "RELIEFWEB_APPNAME")
    appname = os.environ.get(appname_env, "").strip()
    if not appname:
        raise RuntimeError(
            f"ReliefWeb API требует предварительно одобренный appname; задайте переменную {appname_env}."
        )

    endpoint = str(retrieval.get("endpoint") or "")
    endpoint_parts = urllib.parse.urlsplit(endpoint)
    if endpoint_parts.scheme != "https" or endpoint_parts.netloc != "api.reliefweb.int" or endpoint_parts.path != "/v2/reports":
        raise ValueError("Для ReliefWeb API разрешён только официальный HTTPS endpoint /v2/reports")
    query_args = urllib.parse.parse_qsl(endpoint_parts.query, keep_blank_values=True)
    query_args.append(("appname", appname))
    request_url = urllib.parse.urlunsplit((
        endpoint_parts.scheme, endpoint_parts.netloc, endpoint_parts.path,
        urllib.parse.urlencode(query_args), endpoint_parts.fragment,
    ))
    query = retrieval.get("query") or {}
    if not isinstance(query, dict):
        raise ValueError("Для ReliefWeb retrieval.query должен быть объектом")
    request = urllib.request.Request(
        request_url,
        data=json.dumps(query, ensure_ascii=False).encode("utf-8"),
        headers={"User-Agent": USER_AGENT, "Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = response.read(5_000_001)
    if len(payload) > 5_000_000:
        raise ValueError("Ответ ReliefWeb API превышает ограничение размера 5 MB")
    decoded = json.loads(payload.decode("utf-8"))
    if not isinstance(decoded, dict) or not isinstance(decoded.get("data"), list):
        raise ValueError("ReliefWeb API вернул неожиданный формат ответа")

    items: list[dict[str, Any]] = []
    for entry in decoded["data"]:
        fields = entry.get("fields", {}) if isinstance(entry, dict) else {}
        if not isinstance(fields, dict):
            continue
        # Keep the source and country guard locally as well as in the API query.
        source_fields = fields.get("source") or []
        if isinstance(source_fields, dict):
            source_fields = [source_fields]
        shortnames = {str(value.get("shortname", "")).casefold()
                      for value in source_fields if isinstance(value, dict)}
        if "ocha" not in shortnames:
            continue
        countries = fields.get("primary_country") or []
        if isinstance(countries, dict):
            countries = [countries]
        country_codes = {str(value.get("iso3", "")).casefold()
                         for value in countries if isinstance(value, dict)}
        if "ukr" not in country_codes:
            continue
        title = fields.get("title")
        url = fields.get("url")
        publication = fields.get("date", {})
        published_at = publication.get("original") if isinstance(publication, dict) else None
        if title and url:
            items.append({
                "title": title,
                "url": url,
                "published_at": published_at,
                "summary": "",
            })
    return items


def ingest_manual(db: sqlite3.Connection, source: dict[str, Any], title: str, url: str,
                  published_at: str | None, summary: str) -> str:
    if not published_at:
        raise ValueError("Для ручного материала обязательна дата публикации (--published-at)")
    result = _store(db, source, {"title": title, "url": url,
                                 "published_at": published_at, "summary": summary}, "manual")
    record_event(db, "manual_ingest", "source", source["id"], {"url": url, "result": result})
    return result


def fetch_enabled_sources(db: sqlite3.Connection, sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for source in sources:
        if not source.get("enabled", False):
            results.append({"source": source.get("id"), "status": "disabled", "count": 0})
            continue
        retrieval = source.get("retrieval", {})
        method = retrieval.get("method")
        if method not in {"rss", "reliefweb_api"} or not retrieval.get("endpoint"):
            results.append({"source": source["id"], "status": "manual_or_unsupported", "count": 0})
            continue
        try:
            if method == "rss":
                request = urllib.request.Request(retrieval["endpoint"], headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(request, timeout=20) as response:
                    payload = response.read(5_000_001)
                if len(payload) > 5_000_000:
                    raise ValueError("RSS превышает ограничение размера 5 MB")
                feed_items = parse_feed(payload)
            else:
                feed_items = _fetch_reliefweb_api(source, retrieval)
            item_origin = "rss" if method == "rss" else "reliefweb_api"
            inserted = revised = duplicates = skipped = 0
            high_confidence = medium_confidence = low_confidence = needs_review = editor_excluded = 0
            for item in feed_items:
                try:
                    result = _store(db, source, item, item_origin)
                    inserted += result == "inserted"
                    revised += result == "revised"
                    duplicates += result == "duplicate"
                    stored = db.execute(
                        """SELECT a.confidence,a.status FROM relevance_assessments a
                           JOIN items i ON i.id=a.item_id WHERE i.source_id=? AND i.url=?
                           ORDER BY a.id DESC LIMIT 1""", (source["id"], normalize_url(str(item["url"]))),
                    ).fetchone()
                    if stored:
                        high_confidence += stored["confidence"] == "high"
                        medium_confidence += stored["confidence"] == "medium"
                        low_confidence += stored["confidence"] == "low"
                        needs_review += stored["status"] == "review"
                        editor_excluded += stored["status"] == "excluded"
                except Exception as exc:
                    skipped += 1
                    record_event(db, "item_rejected", "source", source["id"],
                                 {"url": item.get("url"), "error": str(exc)})
            status = "partial" if skipped else "ok"
            if skipped and not (inserted or revised or duplicates):
                status = "error"
            record_event(db, "source_fetched", "source", source["id"],
                         {"items": len(feed_items), "feed_items": len(feed_items), "inserted": inserted,
                          "revised": revised, "duplicates": duplicates, "filtered_out": skipped,
                          "high_confidence": high_confidence, "medium_confidence": medium_confidence,
                          "low_confidence": low_confidence, "needs_review": needs_review,
                          "editor_excluded": editor_excluded, "status": status})
            results.append({"source": source["id"], "status": status, "count": inserted,
                            "revised": revised, "duplicates": duplicates, "filtered_out": skipped,
                            "high_confidence": high_confidence, "medium_confidence": medium_confidence,
                            "low_confidence": low_confidence, "needs_review": needs_review,
                            "editor_excluded": editor_excluded})
            if status == "error":
                results[-1]["error"] = "Не удалось обработать ни одну запись источника; подробности в item_rejected"
        except Exception as exc:
            record_event(db, "fetch_error", "source", source["id"], str(exc))
            results.append({"source": source["id"], "status": "error", "error": str(exc), "count": 0})
    return results
