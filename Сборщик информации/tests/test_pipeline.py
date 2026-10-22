from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app import fetcher
from app.db import connect
from app.deduplicator import insert_normalized
from app.draft_generator import _cluster_rows, _event_groups, create_daily_draft
from app.normalizer import normalize_item, normalize_url
from app.publisher import publish, publish_scheduled
from app.review_queue import (mark_claim_link_checked, review_claim_event_date,
                              select_batch_part, set_decision)

MOSCOW = timezone(timedelta(hours=3))


def make_row(name: str, *, level: str = "primary", published_at: str | None = None) -> dict[str, str]:
    return {
        "title": name,
        "summary": name,
        "source_level": level,
        "published_at": published_at or "2026-10-18T10:00:00+00:00",
    }


def insert_test_item(db, title: str, url: str, published_at: str, fetched_at: str,
                     source_id: str = "test") -> tuple[int, int]:
    """Seed a normalized item, immutable version, and editor-approved relevance result."""
    source = {"id": source_id, "name": "Test source", "type": "official", "level": "primary"}
    raw_id = db.execute(
        "INSERT INTO raw_items(source_id,title,url,published_at,fetched_at,origin,payload) VALUES(?,?,?,?,?,?,?)",
        (source_id, title, url, published_at, fetched_at, "test",
         json.dumps({"publication_check": {"status": "http_ok", "checked_at": fetched_at}})),
    ).lastrowid
    item = normalize_item({
        "source_id": source_id, "source_name": source["name"], "source_type": source["type"],
        "source_level": source["level"], "title": title, "summary": title, "url": url,
        "published_at": published_at, "fetched_at": fetched_at,
    })
    item_id, result = insert_normalized(db, raw_id, item)
    if item_id is None or result != "inserted":
        raise AssertionError("test fixture item unexpectedly deduplicated")
    payload = json.dumps({"publication_check": {"status": "http_ok", "checked_at": fetched_at}})
    version_id = db.execute(
        """INSERT INTO item_versions(
               item_id,version_no,title,summary,url,published_at,fetched_at,content_hash,fingerprint,
               raw_text,payload,changed_fields,detected_at,last_checked_at
           ) VALUES(?,1,?,?,?,?,?,?,?,?,?,'[]',?,?)""",
        (item_id, item["title"], item["summary"], item["url"], item["published_at"],
         item["fetched_at"], item["content_hash"], item["content_hash"], item["summary"],
         payload, fetched_at, fetched_at),
    ).lastrowid
    db.execute(
        """INSERT INTO relevance_assessments(
               item_id,version_id,source_id,confidence,status,matched_terms,reason,first_seen_at,last_seen_at
           ) VALUES(?,?,?,'high','included','[]','test fixture',?,?)""",
        (item_id, version_id, source_id, fetched_at, fetched_at),
    )
    db.commit()
    return int(item_id), int(version_id)


class FeedParsingTests(unittest.TestCase):
    def test_parses_rss_and_atom_dates_and_links(self) -> None:
        rss = b"""<rss version='2.0'><channel><item>
          <title>Update &amp; details</title><link>https://example.org/a</link>
          <pubDate>Sun, 18 Oct 2026 10:00:00 GMT</pubDate><description>Summary</description>
        </item></channel></rss>"""
        atom = b"""<feed xmlns='http://www.w3.org/2005/Atom'><entry>
          <title>Atom entry</title><link rel='alternate' href='https://example.org/b'/>
          <published>2026-10-18T10:00:00Z</published><summary>Brief</summary>
        </entry></feed>"""

        self.assertEqual(fetcher.parse_feed(rss)[0]["title"], "Update & details")
        self.assertEqual(fetcher.parse_feed(rss)[0]["url"], "https://example.org/a")
        self.assertEqual(fetcher.parse_feed(atom)[0]["published_at"], "2026-10-18T10:00:00Z")
        self.assertEqual(fetcher.parse_feed(atom)[0]["url"], "https://example.org/b")


class NormalizationAndDedupTests(unittest.TestCase):
    def test_normalizes_tracking_parameters_and_fragment(self) -> None:
        self.assertEqual(
            normalize_url("HTTPS://Example.org/a/?utm_source=news&x=2#fragment"),
            "https://example.org/a?x=2",
        )

    def test_same_canonical_url_is_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.sqlite3")
            source = {"id": "test", "name": "Test", "type": "media", "level": "independent_confirmation"}
            raw_id = db.execute(
                "INSERT INTO raw_items(source_id,title,url,fetched_at,origin) VALUES(?,?,?,?,?)",
                ("test", "Title", "https://example.org/story", "2026-10-18T10:00:00+00:00", "manual"),
            ).lastrowid
            item = normalize_item({
                "source_id": source["id"], "source_name": source["name"], "source_type": source["type"],
                "source_level": source["level"], "title": "Title", "summary": "Text",
                "url": "https://example.org/story?utm_campaign=x",
                "published_at": "2026-10-18T10:00:00Z", "fetched_at": "2026-10-18T10:01:00Z",
            })
            first = insert_normalized(db, raw_id, item)
            second = insert_normalized(db, raw_id, item)
            self.assertEqual(first[1], "inserted")
            self.assertEqual(second[1], "duplicate")
            self.assertEqual(db.execute("SELECT count(*) FROM items").fetchone()[0], 1)
            db.close()


class SemanticGroupingTests(unittest.TestCase):
    def test_complete_link_avoids_chaining_different_events(self) -> None:
        rows = [make_row("A"), make_row("B"), make_row("C")]
        values = {(0, 1): 0.91, (0, 2): 0.71, (1, 2): 0.91}
        groups = _cluster_rows(rows, lambda a, b: values.get((min(a, b), max(a, b)), 1.0), 0.84)
        self.assertEqual([[row["title"] for row in group] for group in groups], [["A", "B"], ["C"]])

    def test_never_combines_analytics_or_events_outside_four_days(self) -> None:
        recent = "2026-10-18T10:00:00+00:00"
        old = (datetime.fromisoformat(recent) - timedelta(days=4, seconds=1)).isoformat()
        rows = [make_row("official", published_at=recent), make_row("analytic", level="analysis_osint", published_at=recent),
                make_row("old", published_at=old)]
        groups = _cluster_rows(rows, lambda _a, _b: 0.99, 0.84)
        self.assertEqual([[row["title"] for row in group] for group in groups],
                         [["official"], ["analytic"], ["old"]])

    def test_event_groups_uses_embeddings_to_separate_paraphrase_and_unrelated_story(self) -> None:
        class Vector(list):
            def __matmul__(self, other):
                return sum(left * right for left, right in zip(self, other))

        class FakeModel:
            def __init__(self, *_args, **_kwargs):
                pass

            def encode(self, texts, **_kwargs):
                self.texts = texts
                return [
                    Vector([1.0, 0.0]),
                    Vector([0.99, 0.01]),
                    Vector([0.0, 1.0]),
                ]

        fake_module = types.ModuleType("sentence_transformers")
        fake_module.SentenceTransformer = FakeModel
        rows = [
            make_row("Municipality closes bridge after severe flooding"),
            make_row("Traffic halted on bridge in city after flood"),
            make_row("Central bank keeps interest rate unchanged"),
        ]
        with patch.dict(sys.modules, {"sentence_transformers": fake_module}), \
                patch.dict(os.environ, {"EVENT_MATCH_THRESHOLD": "0.70"}):
            groups = _event_groups(rows)
        self.assertEqual([[row["title"] for row in group] for group in groups],
                         [[rows[0]["title"], rows[1]["title"]], [rows[2]["title"]]])


class CollectorAndReviewTests(unittest.TestCase):
    def test_schema_migration_marks_legacy_grouped_drafts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "legacy.sqlite3"
            legacy = sqlite3.connect(database)
            legacy.execute(
                """CREATE TABLE drafts (
                   id INTEGER PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL,
                   status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                   published_at TEXT, max_post_url TEXT)"""
            )
            legacy.execute(
                "INSERT INTO drafts(title,body,status,created_at,updated_at) VALUES(?,?,'pending','a','a')",
                ("old draft", "Объединение по локальной модели"),
            )
            legacy.commit()
            legacy.close()

            db = connect(database)
            migrated = db.execute("SELECT correction_of_id,requires_group_review FROM drafts WHERE id=1").fetchone()
            self.assertIsNone(migrated["correction_of_id"])
            self.assertEqual(migrated["requires_group_review"], 1)
            db.close()

    @staticmethod
    def _create_approved_digest(db, created_at: datetime) -> int:
        sequence = int(db.execute("SELECT COUNT(*) FROM items").fetchone()[0]) + 1
        source_id = f"schedule_test_{sequence}"
        source_url = f"https://example.org/story/{sequence}"
        published_at = created_at - timedelta(hours=1)
        item_id, version_id = insert_test_item(
            db, "test", source_url, published_at.isoformat(), created_at.isoformat(), source_id,
        )
        published_label = published_at.astimezone(MOSCOW).strftime("%d.%m.%Y, %H:%M МСК")
        text = ("• **ЗАЯВЛЕНИЕ ИСТОЧНИКА** — Test source сообщает: test.\n"
                f"— Test source, опубликовано {published_label}; ссылка отвечает HTTP 200: {source_url}")
        cursor = db.execute(
            "INSERT INTO drafts(title,body,status,created_at,updated_at) VALUES(?,?,'pending',?,?)",
            ("Daily test", text, created_at.isoformat(), created_at.isoformat()),
        )
        draft_id = int(cursor.lastrowid)
        batch_id = f"batch-{draft_id}"
        db.execute("INSERT INTO digest_batches(id,created_at,batch_date) VALUES(?,?,?)",
                   (batch_id, created_at.isoformat(), created_at.astimezone(MOSCOW).date().isoformat()))
        db.execute("""INSERT INTO digest_batch_parts(draft_id,batch_id,part_no,disposition,updated_at)
                      VALUES(?,?,1,'selected',?)""",
                   (draft_id, batch_id, created_at.isoformat()))
        group_id = int(db.execute(
            "INSERT INTO draft_groups(draft_id,group_no) VALUES(?,1)", (draft_id,)
        ).lastrowid)
        claim_id = int(db.execute(
            """INSERT INTO draft_claims(
                   draft_id,group_id,item_id,version_id,claim_text,source_id,source_name,source_level,
                   published_at,source_url,publication_check_status,publication_checked_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (draft_id, group_id, item_id, version_id, "test", source_id, "Test source", "primary",
             published_at.isoformat(), source_url, "http_ok", created_at.isoformat()),
        ).lastrowid)
        db.commit()
        mark_claim_link_checked(db, draft_id, claim_id)
        review_claim_event_date(db, draft_id, claim_id, "same_day",
                                note="test fixture confirms same publication and event date")
        set_decision(db, draft_id, "approved", {
            "evidence_checked": True, "independence_checked": True, "groups_checked": False,
        })
        return draft_id

    def test_scheduled_publish_requires_approval_and_is_daily_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.sqlite3")
            scheduled = datetime(2026, 10, 18, 11, 0, tzinfo=timezone.utc)  # 14:00 Moscow
            draft_id = self._create_approved_digest(db, scheduled - timedelta(minutes=30))

            early = publish_scheduled(db, now=scheduled - timedelta(minutes=1))
            self.assertEqual(early["status"], "not_due")
            preview = publish_scheduled(db, dry_run=True, now=scheduled)
            self.assertEqual(preview["status"], "dry_run")

            with patch("app.publisher.publish", return_value={"status": "published", "url": "https://max.example/post/1"}) as send:
                result = publish_scheduled(db, now=scheduled)
            self.assertEqual(result["draft_id"], str(draft_id))
            self.assertEqual(send.call_count, 1)

            db.execute("UPDATE drafts SET status='published',published_at=? WHERE id=?",
                       (scheduled.isoformat(), draft_id))
            db.commit()
            repeated = publish_scheduled(db, now=scheduled + timedelta(minutes=5))
            self.assertEqual(repeated["status"], "skipped")
            db.close()

    def test_scheduled_publish_refuses_ambiguous_approved_drafts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.sqlite3")
            scheduled = datetime(2026, 10, 18, 11, 0, tzinfo=timezone.utc)
            self._create_approved_digest(db, scheduled - timedelta(minutes=30))
            self._create_approved_digest(db, scheduled - timedelta(minutes=20))
            with patch("app.publisher.publish") as send:
                result = publish_scheduled(db, now=scheduled)
            self.assertEqual(result["status"], "needs_review")
            send.assert_not_called()
            db.close()

    def test_atom_collection_normalizes_links_and_records_duplicate(self) -> None:
        source = {
            "id": "test", "name": "Test source", "type": "official", "level": "primary",
            "url": "https://example.org", "allowed_domains": ["example.org"], "enabled": True,
            "retrieval": {"method": "rss", "endpoint": "https://feeds.example.org/atom.xml"},
            "include_keywords": ["Ukraine"],
        }
        published = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        feed = f"""<feed xmlns='http://www.w3.org/2005/Atom'><entry>
          <title>Update about Ukraine</title><link rel='alternate' href='https://example.org/story?utm_source=test'/>
          <published>{published}</published><summary>Summary</summary></entry>
          <entry><title>Update about Ukraine</title><link rel='alternate' href='https://example.org/story?utm_source=test'/>
          <published>{published}</published><summary>Summary</summary></entry></feed>""".encode()

        class Response:
            status = 200

            def __init__(self, url: str, body: bytes = b""):
                self.url, self.body = url, body

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, size=-1):
                return self.body if size < 0 else self.body[:size]

            def geturl(self):
                return self.url

        def fake_urlopen(request, timeout=0):
            if request.full_url == source["retrieval"]["endpoint"]:
                return Response(request.full_url, feed)
            if request.get_method() == "HEAD" and request.full_url.startswith("https://example.org/story"):
                return Response("https://example.org/story")
            raise AssertionError(f"Unexpected URL: {request.full_url}; timeout={timeout}")

        with tempfile.TemporaryDirectory() as tmp, patch("app.fetcher.urllib.request.urlopen", fake_urlopen):
            db = connect(Path(tmp) / "test.sqlite3")
            result = fetcher.fetch_enabled_sources(db, [source])[0]
            self.assertEqual((result["count"], result["duplicates"], result["filtered_out"]), (1, 1, 0))
            row = db.execute("SELECT * FROM items").fetchone()
            self.assertEqual(row["url"], "https://example.org/story")
            raw = db.execute("SELECT payload FROM raw_items WHERE id=?", (row["raw_id"],)).fetchone()
            self.assertEqual(json.loads(raw["payload"])["publication_check"]["status"], "http_ok_feed")
            db.close()

    def test_draft_excludes_out_of_window_items_and_requires_review_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.sqlite3")
            now = datetime.now(timezone.utc).replace(microsecond=0)
            current = now.isoformat()
            records = [
                ("Recent update", current),
                ("Old update", (now - timedelta(days=5)).isoformat()),
                ("Future update", (now + timedelta(hours=1)).isoformat()),
            ]
            for number, (title, published_at) in enumerate(records, start=1):
                insert_test_item(db, title, f"https://example.org/{number}", published_at, current)
            draft_id = create_daily_draft(db)
            draft = db.execute("SELECT body FROM drafts WHERE id=?", (draft_id,)).fetchone()
            self.assertIn("Recent update", draft["body"])
            self.assertNotIn("Old update", draft["body"])
            self.assertNotIn("Future update", draft["body"])
            with self.assertRaisesRegex(ValueError, "evidence-checked"):
                set_decision(db, draft_id, "approved", {})

            claim_id = int(db.execute(
                "SELECT id FROM draft_claims WHERE draft_id=? AND included=1", (draft_id,)
            ).fetchone()["id"])
            checks = {"evidence_checked": True, "independence_checked": True}
            with self.assertRaisesRegex(ValueError, "claim-check"):
                set_decision(db, draft_id, "approved", checks)
            mark_claim_link_checked(db, draft_id, claim_id)
            with self.assertRaisesRegex(ValueError, "event-date"):
                set_decision(db, draft_id, "approved", checks)
            review_claim_event_date(db, draft_id, claim_id, "same_day",
                                    note="test fixture confirms same publication and event date")
            set_decision(db, draft_id, "approved", checks)
            batch_id = db.execute(
                "SELECT batch_id FROM digest_batch_parts WHERE draft_id=?", (draft_id,)
            ).fetchone()["batch_id"]
            select_batch_part(db, batch_id, draft_id)
            self.assertEqual(publish(db, draft_id, dry_run=True)["status"], "dry_run")
            audit = db.execute("SELECT details FROM events WHERE kind='draft_approved'").fetchone()["details"]
            self.assertIn("evidence_checked", audit)
            db.close()


if __name__ == "__main__":
    unittest.main()
