"""Хранилище SQLite приложения."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .config import DB_PATH

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS raw_items (
    id INTEGER PRIMARY KEY,
    source_id TEXT NOT NULL,
    title TEXT NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL,
    published_at TEXT,
    fetched_at TEXT NOT NULL,
    origin TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '',
    UNIQUE(source_id, url)
);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    raw_id INTEGER NOT NULL REFERENCES raw_items(id),
    source_id TEXT NOT NULL,
    source_name TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_level TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL,
    published_at TEXT,
    fetched_at TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    UNIQUE(source_id, url),
    UNIQUE(source_id, content_hash)
);
CREATE TABLE IF NOT EXISTS item_versions (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES items(id),
    version_no INTEGER NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL,
    published_at TEXT,
    fetched_at TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    raw_text TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL DEFAULT '',
    changed_fields TEXT NOT NULL DEFAULT '[]',
    detected_at TEXT NOT NULL,
    last_checked_at TEXT NOT NULL,
    UNIQUE(item_id, version_no)
);
CREATE TABLE IF NOT EXISTS relevance_assessments (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES items(id),
    version_id INTEGER NOT NULL REFERENCES item_versions(id),
    source_id TEXT NOT NULL,
    confidence TEXT NOT NULL CHECK(confidence IN ('high','medium','low')),
    status TEXT NOT NULL CHECK(status IN ('included','review','excluded')),
    matched_terms TEXT NOT NULL DEFAULT '[]',
    reason TEXT NOT NULL DEFAULT '',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    seen_count INTEGER NOT NULL DEFAULT 1,
    editor_note TEXT NOT NULL DEFAULT '',
    reviewed_at TEXT,
    UNIQUE(version_id)
);
CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','approved','rejected','published')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    published_at TEXT,
    max_post_url TEXT,
    correction_of_id INTEGER REFERENCES drafts(id),
    requires_group_review INTEGER NOT NULL DEFAULT 0,
    correction_reason TEXT NOT NULL DEFAULT '',
    correction_text TEXT NOT NULL DEFAULT '',
    correction_source_id TEXT NOT NULL DEFAULT '',
    correction_source_url TEXT NOT NULL DEFAULT '',
    correction_original_url TEXT NOT NULL DEFAULT '',
    correction_publication_check_status TEXT NOT NULL DEFAULT '',
    correction_publication_checked_at TEXT,
    correction_link_checked_at TEXT,
    correction_link_check_note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS digest_batches (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    batch_date TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS digest_batch_parts (
    draft_id INTEGER PRIMARY KEY REFERENCES drafts(id) ON DELETE CASCADE,
    batch_id TEXT NOT NULL REFERENCES digest_batches(id) ON DELETE CASCADE,
    part_no INTEGER NOT NULL,
    disposition TEXT NOT NULL DEFAULT 'open'
        CHECK(disposition IN ('open','selected','closed','deferred')),
    deferred_until TEXT,
    note TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    UNIQUE(batch_id, part_no)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_selected_part_per_batch
    ON digest_batch_parts(batch_id) WHERE disposition='selected';
CREATE TABLE IF NOT EXISTS draft_items (
    draft_id INTEGER NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
    item_id INTEGER NOT NULL REFERENCES items(id),
    PRIMARY KEY(draft_id, item_id)
);
CREATE TABLE IF NOT EXISTS draft_groups (
    id INTEGER PRIMARY KEY,
    draft_id INTEGER NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
    group_no INTEGER NOT NULL,
    review_same_event INTEGER CHECK(review_same_event IN (0,1)),
    sources_independent INTEGER CHECK(sources_independent IN (0,1)),
    review_note TEXT NOT NULL DEFAULT '',
    reviewed_at TEXT,
    UNIQUE(draft_id, group_no)
);
CREATE TABLE IF NOT EXISTS draft_claims (
    id INTEGER PRIMARY KEY,
    draft_id INTEGER NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
    group_id INTEGER NOT NULL REFERENCES draft_groups(id) ON DELETE CASCADE,
    item_id INTEGER NOT NULL REFERENCES items(id),
    version_id INTEGER REFERENCES item_versions(id),
    claim_text TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_name TEXT NOT NULL,
    source_level TEXT NOT NULL DEFAULT '',
    published_at TEXT NOT NULL,
    source_url TEXT NOT NULL,
    same_url_other_source INTEGER NOT NULL DEFAULT 0 CHECK(same_url_other_source IN (0,1)),
    publication_check_status TEXT NOT NULL DEFAULT '',
    publication_checked_at TEXT,
    link_checked_at TEXT,
    link_check_note TEXT NOT NULL DEFAULT '',
    event_date TEXT,
    event_date_status TEXT NOT NULL DEFAULT 'unreviewed',
    event_date_note TEXT NOT NULL DEFAULT '',
    event_date_reviewed_at TEXT,
    included INTEGER NOT NULL DEFAULT 1 CHECK(included IN (0,1)),
    exclusion_reason TEXT NOT NULL DEFAULT '',
    excluded_at TEXT,
    UNIQUE(draft_id, item_id)
);
CREATE TABLE IF NOT EXISTS revision_alerts (
    id INTEGER PRIMARY KEY,
    draft_id INTEGER NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
    claim_id INTEGER NOT NULL REFERENCES draft_claims(id) ON DELETE CASCADE,
    previous_version_id INTEGER NOT NULL REFERENCES item_versions(id),
    current_version_id INTEGER NOT NULL REFERENCES item_versions(id),
    created_at TEXT NOT NULL,
    resolution TEXT CHECK(resolution IN ('kept','refreshed','excluded')),
    note TEXT NOT NULL DEFAULT '',
    reviewed_at TEXT,
    UNIQUE(draft_id, claim_id, current_version_id)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    entity_type TEXT,
    entity_id TEXT,
    details TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS publish_attempts (
    id INTEGER PRIMARY KEY,
    draft_id INTEGER NOT NULL REFERENCES drafts(id),
    attempt_no INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('started','succeeded','failed','unknown')),
    created_at TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '',
    UNIQUE(draft_id, attempt_no)
);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    db_file = path or DB_PATH
    db_file.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_file)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(SCHEMA)
    # Older databases may already contain repeated RSS observations. Keep the
    # earliest row ID (so existing references remain valid), move item links to
    # it, then remove duplicate raw rows before enforcing the uniqueness rule.
    duplicate_raw = connection.execute(
        """SELECT source_id,url,MIN(id) AS keep_id FROM raw_items
           GROUP BY source_id,url HAVING COUNT(*) > 1"""
    ).fetchall()
    for duplicate in duplicate_raw:
        ids = [row[0] for row in connection.execute(
            "SELECT id FROM raw_items WHERE source_id=? AND url=? ORDER BY id",
            (duplicate["source_id"], duplicate["url"]),
        )]
        keep_id = int(duplicate["keep_id"])
        latest = connection.execute(
            """SELECT title,text,published_at,fetched_at,origin,payload FROM raw_items
               WHERE source_id=? AND url=? ORDER BY fetched_at DESC,id DESC LIMIT 1""",
            (duplicate["source_id"], duplicate["url"]),
        ).fetchone()
        connection.execute("UPDATE items SET raw_id=? WHERE raw_id IN (%s)" %
                           ",".join("?" for _ in ids), (keep_id, *ids))
        connection.execute(
            """UPDATE raw_items SET title=?,text=?,published_at=?,fetched_at=?,origin=?,payload=?
               WHERE id=?""",
            (*latest, keep_id),
        )
        connection.execute("DELETE FROM raw_items WHERE source_id=? AND url=? AND id<>?",
                           (duplicate["source_id"], duplicate["url"], keep_id))
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_raw_items_source_url ON raw_items(source_id,url)"
    )
    # Small forward-compatible migrations for databases created by earlier versions.
    columns = {row[1] for row in connection.execute("PRAGMA table_info(items)")}
    if "source_level" not in columns:
        connection.execute("ALTER TABLE items ADD COLUMN source_level TEXT NOT NULL DEFAULT ''")
    # Older schemas made URL and content_hash globally unique. Rebuild the
    # normalized table so identical pages from different publishers survive as
    # separate evidence records, while same-source duplicates remain blocked.
    old_items_schema = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='items'"
    ).fetchone()[0]
    if "UNIQUE(url)" in old_items_schema or "UNIQUE(content_hash)" in old_items_schema:
        connection.commit()
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            """CREATE TABLE items_rebuilt (
                id INTEGER PRIMARY KEY,
                raw_id INTEGER NOT NULL REFERENCES raw_items(id),
                source_id TEXT NOT NULL,
                source_name TEXT NOT NULL,
                source_type TEXT NOT NULL,
                source_level TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                url TEXT NOT NULL,
                published_at TEXT,
                fetched_at TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                UNIQUE(source_id,url),
                UNIQUE(source_id,content_hash)
            )"""
        )
        connection.execute(
            """INSERT INTO items_rebuilt(id,raw_id,source_id,source_name,source_type,source_level,
               title,summary,url,published_at,fetched_at,content_hash)
               SELECT id,raw_id,source_id,source_name,source_type,source_level,title,summary,url,
                      published_at,fetched_at,content_hash FROM items"""
        )
        connection.execute("DROP TABLE items")
        connection.execute("ALTER TABLE items_rebuilt RENAME TO items")
        connection.commit()
        connection.execute("PRAGMA foreign_keys=ON")
    else:
        connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_items_source_url ON items(source_id,url)")
        connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_items_source_hash ON items(source_id,content_hash)")
    draft_columns = {row[1] for row in connection.execute("PRAGMA table_info(drafts)")}
    if "correction_of_id" not in draft_columns:
        connection.execute("ALTER TABLE drafts ADD COLUMN correction_of_id INTEGER REFERENCES drafts(id)")
    if "requires_group_review" not in draft_columns:
        connection.execute("ALTER TABLE drafts ADD COLUMN requires_group_review INTEGER NOT NULL DEFAULT 0")
        connection.execute(
            "UPDATE drafts SET requires_group_review=1 WHERE correction_of_id IS NULL "
            "AND body LIKE '%Объединение по локальной модели%'"
        )
    correction_columns = {
        "correction_reason": "ALTER TABLE drafts ADD COLUMN correction_reason TEXT NOT NULL DEFAULT ''",
        "correction_text": "ALTER TABLE drafts ADD COLUMN correction_text TEXT NOT NULL DEFAULT ''",
        "correction_source_id": "ALTER TABLE drafts ADD COLUMN correction_source_id TEXT NOT NULL DEFAULT ''",
        "correction_source_url": "ALTER TABLE drafts ADD COLUMN correction_source_url TEXT NOT NULL DEFAULT ''",
        "correction_original_url": "ALTER TABLE drafts ADD COLUMN correction_original_url TEXT NOT NULL DEFAULT ''",
        "correction_publication_check_status": "ALTER TABLE drafts ADD COLUMN correction_publication_check_status TEXT NOT NULL DEFAULT ''",
        "correction_publication_checked_at": "ALTER TABLE drafts ADD COLUMN correction_publication_checked_at TEXT",
        "correction_link_checked_at": "ALTER TABLE drafts ADD COLUMN correction_link_checked_at TEXT",
        "correction_link_check_note": "ALTER TABLE drafts ADD COLUMN correction_link_check_note TEXT NOT NULL DEFAULT ''",
    }
    for name, statement in correction_columns.items():
        if name not in draft_columns:
            connection.execute(statement)
    # Preserve reviewable evidence for drafts created before structured claims.
    # Legacy grouped drafts are conservatively migrated as one candidate group,
    # because their exact machine-generated cluster membership was not stored.
    drafts_without_claims = connection.execute(
        """SELECT d.id, d.requires_group_review FROM drafts d
           WHERE NOT EXISTS (SELECT 1 FROM draft_claims c WHERE c.draft_id=d.id)"""
    ).fetchall()
    for draft in drafts_without_claims:
        linked_items = connection.execute(
            """SELECT i.* FROM draft_items di JOIN items i ON i.id=di.item_id
               WHERE di.draft_id=? ORDER BY i.published_at, i.id""", (draft["id"],)
        ).fetchall()
        if not linked_items:
            continue
        item_groups = [linked_items] if draft["requires_group_review"] else [[item] for item in linked_items]
        for group_no, group_items in enumerate(item_groups, start=1):
            group_cursor = connection.execute(
                "INSERT INTO draft_groups(draft_id,group_no) VALUES(?,?)",
                (draft["id"], group_no),
            )
            group_id = int(group_cursor.lastrowid)
            for item in group_items:
                connection.execute(
                    """INSERT OR IGNORE INTO draft_claims(
                        draft_id,group_id,item_id,claim_text,source_id,source_name,source_level,
                        published_at,source_url
                    ) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (draft["id"], group_id, item["id"], item["title"].rstrip(". "), item["source_id"],
                     item["source_name"], item["source_level"], item["published_at"], item["url"]),
                )
    claim_columns = {row[1] for row in connection.execute("PRAGMA table_info(draft_claims)")}
    claim_migrations = {
        "same_url_other_source": "ALTER TABLE draft_claims ADD COLUMN same_url_other_source INTEGER NOT NULL DEFAULT 0 CHECK(same_url_other_source IN (0,1))",
        "version_id": "ALTER TABLE draft_claims ADD COLUMN version_id INTEGER REFERENCES item_versions(id)",
        "event_date": "ALTER TABLE draft_claims ADD COLUMN event_date TEXT",
        "event_date_status": "ALTER TABLE draft_claims ADD COLUMN event_date_status TEXT NOT NULL DEFAULT 'unreviewed'",
        "event_date_note": "ALTER TABLE draft_claims ADD COLUMN event_date_note TEXT NOT NULL DEFAULT ''",
        "event_date_reviewed_at": "ALTER TABLE draft_claims ADD COLUMN event_date_reviewed_at TEXT",
        "included": "ALTER TABLE draft_claims ADD COLUMN included INTEGER NOT NULL DEFAULT 1 CHECK(included IN (0,1))",
        "exclusion_reason": "ALTER TABLE draft_claims ADD COLUMN exclusion_reason TEXT NOT NULL DEFAULT ''",
        "excluded_at": "ALTER TABLE draft_claims ADD COLUMN excluded_at TEXT",
        "link_check_note": "ALTER TABLE draft_claims ADD COLUMN link_check_note TEXT NOT NULL DEFAULT ''",
    }
    for name, statement in claim_migrations.items():
        if name not in claim_columns:
            connection.execute(statement)

    # Backfill an immutable version 1 for every existing normalized item.
    # Its values come from the current raw row; old draft_claims retain their
    # own text/date/link snapshots and are linked to this baseline version.
    import hashlib
    import json
    from datetime import datetime, timezone
    for item in connection.execute(
        """SELECT i.*,r.text AS raw_text,r.payload AS raw_payload FROM items i
           JOIN raw_items r ON r.id=i.raw_id
           WHERE NOT EXISTS (SELECT 1 FROM item_versions v WHERE v.item_id=i.id)"""
    ).fetchall():
        fingerprint_data = json.dumps(
            [item["url"], item["title"], item["summary"], item["published_at"]],
            ensure_ascii=False, separators=(",", ":"),
        )
        fingerprint = hashlib.sha256(fingerprint_data.encode("utf-8")).hexdigest()
        checked_at = item["fetched_at"] or datetime.now(timezone.utc).isoformat(timespec="seconds")
        connection.execute(
            """INSERT OR IGNORE INTO item_versions(
                item_id,version_no,title,summary,url,published_at,fetched_at,content_hash,
                fingerprint,raw_text,payload,changed_fields,detected_at,last_checked_at
            ) VALUES(?,1,?,?,?,?,?,?,?,?,?,'[]',?,?)""",
            (item["id"], item["title"], item["summary"], item["url"], item["published_at"],
             item["fetched_at"], item["content_hash"], fingerprint, item["raw_text"],
             item["raw_payload"], checked_at, checked_at),
        )
    connection.execute(
        """UPDATE draft_claims SET version_id=(
             SELECT v.id FROM item_versions v WHERE v.item_id=draft_claims.item_id
             ORDER BY v.version_no LIMIT 1
           ) WHERE version_id IS NULL"""
    )
    # Migrate previously generated multipart drafts into explicit batches using
    # the part_ids already preserved in draft_created event details.
    from datetime import datetime, timedelta, timezone
    moscow = timezone(timedelta(hours=3))
    for event in connection.execute(
        "SELECT entity_id,created_at,details FROM events WHERE kind='draft_created' AND entity_type='draft'"
    ).fetchall():
        try:
            details = json.loads(event["details"] or "{}")
            part_ids = [int(value) for value in details.get("part_ids", [])]
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not part_ids:
            continue
        batch_id = f"legacy-{part_ids[0]}"
        try:
            batch_date = datetime.fromisoformat(event["created_at"]).astimezone(moscow).date().isoformat()
        except (TypeError, ValueError):
            batch_date = str(event["created_at"])[:10]
        connection.execute(
            "INSERT OR IGNORE INTO digest_batches(id,created_at,batch_date) VALUES(?,?,?)",
            (batch_id, event["created_at"], batch_date),
        )
        for part_no, part_draft_id in enumerate(part_ids, start=1):
            connection.execute(
                """INSERT OR IGNORE INTO digest_batch_parts(draft_id,batch_id,part_no,updated_at)
                   SELECT id,?,?,updated_at FROM drafts WHERE id=?""",
                (batch_id, part_no, part_draft_id),
            )
    connection.commit()
    return connection


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None
