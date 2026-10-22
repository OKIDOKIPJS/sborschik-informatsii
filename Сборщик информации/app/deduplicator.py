"""Выявление совпадений по канонической ссылке и отпечатку текста."""
from __future__ import annotations

import sqlite3
from typing import Any

from .logs import record_event


def insert_normalized(db: sqlite3.Connection, raw_id: int, item: dict[str, Any]) -> tuple[int | None, str]:
    found = db.execute(
        """SELECT id FROM items WHERE source_id=? AND (url=? OR content_hash=?)
           ORDER BY CASE WHEN url=? THEN 0 ELSE 1 END LIMIT 1""",
        (item["source_id"], item["url"], item["content_hash"], item["url"]),
    ).fetchone()
    if found:
        record_event(db, "duplicate_skipped", "item", found["id"],
                     {"source_id": item["source_id"], "incoming_url": item["url"],
                      "matched_by": "same_source_url_or_content_hash"})
        return int(found["id"]), "duplicate"
    same_url = list(db.execute(
        "SELECT id,source_id,source_name FROM items WHERE url=? AND source_id<>? ORDER BY id",
        (item["url"], item["source_id"]),
    ))
    cursor = db.execute(
        """INSERT INTO items(raw_id,source_id,source_name,source_type,source_level,title,summary,url,
           published_at,fetched_at,content_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (raw_id, item["source_id"], item["source_name"], item["source_type"], item.get("source_level", ""), item["title"],
         item["summary"], item["url"], item["published_at"], item["fetched_at"], item["content_hash"]),
    )
    db.commit()
    record_event(db, "item_normalized", "item", cursor.lastrowid,
                 {"source_id": item["source_id"], "url": item["url"],
                  "same_url_other_sources": [
                      {"item_id": int(row["id"]), "source_id": row["source_id"],
                       "source_name": row["source_name"]} for row in same_url
                  ],
                  "same_url_is_independent_confirmation": False if same_url else None})
    if same_url:
        record_event(db, "cross_source_same_url", "item", cursor.lastrowid, {
            "url": item["url"], "source_id": item["source_id"],
            "other_items": [int(row["id"]) for row in same_url],
            "independent_confirmation": False,
        })
    return int(cursor.lastrowid), "inserted"
