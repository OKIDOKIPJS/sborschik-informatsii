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
from app.cli import confirm_claim_link_review
from app.config import get_source, load_sources
from app.corrections import (correction_evidence_is_current, create_correction_draft,
                             mark_correction_evidence_checked)
from app.db import connect
from app.deduplicator import insert_normalized
from app.draft_generator import (MODEL_FALLBACK_NOTICE, _cluster_rows, _event_groups,
                                 _event_groups_with_status,
                                 _render_group, create_daily_draft)
from app.normalizer import normalize_item, normalize_url
from app.publisher import _extract_channel_post, publish, publish_scheduled, resolve_attempt
from app.publication_validator import validate_publication_url
from app.publication_validator import _AllowlistedRedirectHandler
from app.relevance import assess_relevance
from app.review_queue import (claim_link_check_is_current, mark_claim_link_checked,
                              edit_draft, exclude_claim, review_candidate_group, review_claim_event_date,
                              select_batch_part, set_decision)
from app.schedule import install_schedule

MOSCOW = timezone(timedelta(hours=3))


def make_row(name: str, *, level: str = "primary", published_at: str | None = None) -> dict[str, str]:
    return {
        "title": name,
        "summary": name,
        "source_level": level,
        "published_at": published_at or "2026-10-18T10:00:00+00:00",
    }


def insert_test_item(db, title: str, url: str, published_at: str, fetched_at: str,
                     source_id: str = "test", summary: str | None = None) -> tuple[int, int]:
    """Seed a normalized item, immutable version, and editor-approved relevance result."""
    source = {"id": source_id, "name": "Test source", "type": "official", "level": "primary"}
    raw_id = db.execute(
        "INSERT INTO raw_items(source_id,title,url,published_at,fetched_at,origin,payload) VALUES(?,?,?,?,?,?,?)",
        (source_id, title, url, published_at, fetched_at, "test",
         json.dumps({"publication_check": {"status": "http_ok", "checked_at": fetched_at}})),
    ).lastrowid
    item = normalize_item({
        "source_id": source_id, "source_name": source["name"], "source_type": source["type"],
        "source_level": source["level"], "title": title, "summary": summary or title, "url": url,
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
    def test_borderline_multilingual_pair_needs_same_place_and_other_places_block_merge(self) -> None:
        same_place = [
            make_row("Бои продолжаются на Покровском направлении."),
            make_row("В районе Покровска идут столкновения вокруг ряда сёл."),
        ]
        different_places = [
            make_row("Ukrainian forces repelled an assault near Kupiansk."),
            make_row("Russian forces repelled an assault near Vuhledar."),
        ]
        near_threshold = _cluster_rows(same_place, lambda _a, _b: 0.687, 0.70)
        misleadingly_similar = _cluster_rows(different_places, lambda _a, _b: 0.99, 0.70)
        self.assertEqual(len(near_threshold), 1)
        self.assertEqual(len(misleadingly_similar), 2)

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

    def test_event_groups_falls_back_to_separate_items_when_model_is_missing(self) -> None:
        rows = [make_row("First report"), make_row("Second report")]
        with patch.dict(sys.modules, {"sentence_transformers": None}):
            groups, warning = _event_groups_with_status(rows)
        self.assertEqual([[row["title"] for row in group] for group in groups],
                         [["First report"], ["Second report"]])
        self.assertIn("не установлен", warning)


class DraftContentAndRelevanceTests(unittest.TestCase):
    def test_max_success_response_requires_channel_post_id_and_public_url(self) -> None:
        self.assertEqual(
            _extract_channel_post({"message": {"url": "https://max.ru/channel/post-1",
                                                "body": {"mid": "mid.post-1"}}}),
            ("https://max.ru/channel/post-1", "mid.post-1"),
        )
        for payload in ({}, {"message": {"body": {}}},
                        {"message": {"url": "https://evil.example/post", "body": {"mid": "1"}}}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                _extract_channel_post(payload)

    def test_browser_claim_review_requires_explicit_confirmation(self) -> None:
        with patch("app.cli.webbrowser.open", return_value=True) as open_browser, \
                patch("builtins.input", side_effect=["ПРОВЕРЕНО", "Страница сообщает о продвижении на направлении."]):
            evidence = confirm_claim_link_review("https://example.org/story")
        open_browser.assert_called_once_with("https://example.org/story", new=2)
        self.assertEqual(evidence, "Страница сообщает о продвижении на направлении.")
        with patch("app.cli.webbrowser.open", return_value=True), \
                patch("builtins.input", return_value="да"):
            with self.assertRaisesRegex(ValueError, "не подтверждена"):
                confirm_claim_link_review("https://example.org/story")

    def test_claim_review_rejects_a_link_outside_source_domain_before_opening(self) -> None:
        source = {"id": "test", "url": "https://example.org", "allowed_domains": ["example.org"]}
        with self.assertRaisesRegex(ValueError, "разрешённые домены"):
            validate_publication_url(source, "https://not-example.org/story")

    def test_publication_checker_blocks_off_domain_and_https_downgrade_redirects(self) -> None:
        handler = _AllowlistedRedirectHandler(["example.org"])
        request = fetcher.urllib.request.Request("https://example.org/story")
        with self.assertRaisesRegex(ValueError, "Перенаправление"):
            handler.redirect_request(request, None, 302, "Found", {}, "https://attacker.example/story")
        with self.assertRaisesRegex(ValueError, "Перенаправление"):
            handler.redirect_request(request, None, 302, "Found", {}, "http://example.org/story")

    def test_draft_includes_attributed_feed_summary(self) -> None:
        row = {
            "source_level": "primary", "source_type": "official", "source_name": "Test source",
            "payload": "{}", "title": "Front line update",
            "summary": "The ministry reported a change. A second sentence adds context. A third sentence is not included.",
            "published_at": "2026-10-18T10:00:00+00:00", "same_url_other_source": 0,
            "url": "https://example.org/story",
        }
        rendered = _render_group([row])
        self.assertIn("Front line update", rendered)
        self.assertIn("Выжимка из описания Test source (до двух предложений): The ministry reported a change. A second sentence adds context.", rendered)
        self.assertNotIn("A third sentence", rendered)
        self.assertIn("https://example.org/story", rendered)

    def test_sensitive_source_summary_is_not_copied_into_draft(self) -> None:
        row = {
            "source_level": "primary", "source_type": "official", "source_name": "Test source",
            "payload": "{}", "title": "Operational update",
            "summary": "Coordinates: 48.123456, 37.654321. Unit positions were reported.",
            "published_at": "2026-10-18T10:00:00+00:00", "same_url_other_source": 0,
            "url": "https://example.org/story",
        }
        rendered = _render_group([row])
        self.assertNotIn("48.123456", rendered)
        self.assertNotIn("37.654321", rendered)
        self.assertIn("Описание источника скрыто", rendered)

    def test_excluding_claim_also_removes_summary_that_repeats_its_title(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.sqlite3")
            now = datetime.now(timezone.utc).replace(microsecond=0)
            title = "Front line report"
            insert_test_item(
                db, title, "https://example.org/repeated-title", now.isoformat(), now.isoformat(),
                summary="Front line report: the source supplied an additional detail.",
            )
            draft_id = create_daily_draft(db)
            claim_id = int(db.execute(
                "SELECT id FROM draft_claims WHERE draft_id=?", (draft_id,),
            ).fetchone()["id"])
            exclude_claim(db, draft_id, claim_id, "Не относится к итоговой сводке")
            body = db.execute("SELECT body FROM drafts WHERE id=?", (draft_id,)).fetchone()["body"]
            self.assertNotIn(title, body)
            self.assertNotIn("additional detail", body)
            db.close()

    def test_generic_conflict_match_is_review_only_and_unmatched_noise_is_excluded(self) -> None:
        source = {"relevance_filter": {
            "high_confidence_terms": ["front line", "frontline"],
            "geography_terms": ["Donetsk"], "conflict_terms": ["attack", "fighting"],
            "frontline_action_terms": ["advanced", "captured"],
            "context_terms": ["war in Ukraine"],
        }}
        broad = assess_relevance({"title": "War in Ukraine", "summary": ""}, source)
        regional = assess_relevance({"title": "Attack reported in Donetsk", "summary": ""}, source)
        frontline = assess_relevance({"title": "Fighting on the front line near Donetsk", "summary": ""}, source)
        front_change = assess_relevance({"title": "Forces advanced near Donetsk", "summary": ""}, source)
        self.assertEqual((broad["confidence"], broad["status"]), ("medium", "review"))
        self.assertEqual((regional["confidence"], regional["status"]), ("medium", "review"))
        self.assertEqual((frontline["confidence"], frontline["status"]), ("high", "included"))
        self.assertEqual((front_change["confidence"], front_change["status"]), ("high", "included"))

    def test_configured_front_filter_does_not_auto_include_broad_war_coverage(self) -> None:
        source = get_source(load_sources(), "uk_mod_govuk")
        broad = assess_relevance({"title": "War in Ukraine", "summary": ""}, source)
        frontline = assess_relevance(
            {"title": "Fighting on the front line in Donetsk", "summary": ""}, source,
        )
        control_change = assess_relevance(
            {"title": "Forces advanced near Pokrovsk", "summary": ""}, source,
        )
        unrelated = assess_relevance({"title": "Government procurement annual report", "summary": ""}, source)
        self.assertEqual(broad["status"], "review")
        self.assertEqual(unrelated["status"], "excluded")
        self.assertEqual(frontline["status"], "included")
        self.assertEqual(control_change["status"], "included")


class CollectorAndReviewTests(unittest.TestCase):
    @staticmethod
    def _check_test_claim_link(db, draft_id: int, claim_id: int, source_id: str) -> dict[str, str]:
        source = {"id": source_id, "url": "https://example.org", "allowed_domains": ["example.org"]}
        source_url = db.execute("SELECT source_url FROM draft_claims WHERE id=?", (claim_id,)).fetchone()["source_url"]
        check = {"status": "http_ok", "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "http_status": "200", "final_url": source_url}
        with patch("app.review_queue.get_source", return_value=source), \
                patch("app.review_queue.check_publication_link", return_value=check) as live_check:
            result = mark_claim_link_checked(
                db, draft_id, claim_id, checked=True,
                note="Страница содержит соответствующее сообщение источника.",
            )
        live_check.assert_called_once_with(source, source_url, origin="manual")
        return result

    def test_multiple_articles_from_same_publisher_are_not_independent_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.sqlite3")
            now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            draft_id = db.execute(
                "INSERT INTO drafts(title,body,status,created_at,updated_at) VALUES('test','test','pending',?,?)",
                (now, now),
            ).lastrowid
            group_id = db.execute(
                "INSERT INTO draft_groups(draft_id,group_no) VALUES(?,1)", (draft_id,),
            ).lastrowid
            for suffix in ("first", "second"):
                item_id, _version_id = insert_test_item(
                    db, suffix, f"https://example.org/{suffix}", now, now, "same_publisher",
                )
                db.execute(
                    "INSERT INTO draft_claims(draft_id,group_id,item_id,claim_text,source_id,source_name,"
                    "published_at,source_url) VALUES(?,?,?,?,?,?,?,?)",
                    (draft_id, group_id, item_id, suffix, "same_publisher", "Same publisher", now,
                     f"https://example.org/{suffix}"),
                )
            db.commit()
            with self.assertRaisesRegex(ValueError, "одного издателя"):
                review_candidate_group(db, draft_id, 1, same_event=True, independent=True,
                                       note="два материала одной редакции")
            review_candidate_group(db, draft_id, 1, same_event=True, independent=False,
                                   note="два материала одной редакции")
            db.close()

    def test_correction_requires_allowlisted_evidence_link_review_and_original_post_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.sqlite3")
            now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            original_id = db.execute(
                """INSERT INTO drafts(title,body,status,created_at,updated_at,published_at,max_post_url)
                   VALUES('original','post','published',?,?,?,'https://max.ru/channel/original')""",
                (now, now, now),
            ).lastrowid
            with self.assertRaisesRegex(ValueError, "источников найдено: 0"):
                create_correction_draft(db, original_id, "старое", "новое", "https://evil.example/evidence")
            correction_id = create_correction_draft(
                db, original_id, "Неверная дата", "Правильная дата", "https://www.gov.uk/example-evidence",
            )
            correction = db.execute("SELECT * FROM drafts WHERE id=?", (correction_id,)).fetchone()
            self.assertEqual(correction["correction_source_id"], "uk_mod_govuk")
            # Simulate an older pending correction which stored only its body.
            db.execute(
                """UPDATE drafts SET correction_reason='',correction_text='',correction_source_id='',
                   correction_source_url='',correction_original_url='' WHERE id=?""", (correction_id,),
            )
            db.commit()
            with self.assertRaisesRegex(ValueError, "основание"):
                set_decision(db, correction_id, "approved", {
                    "evidence_checked": True, "independence_checked": True,
                })
            check = {"status": "http_ok", "checked_at": now, "http_status": "200"}
            with patch("app.corrections.check_publication_link", return_value=check):
                mark_correction_evidence_checked(
                    db, correction_id, "Сверил исправление", "На странице указана правильная дата.",
                )
            correction = db.execute("SELECT * FROM drafts WHERE id=?", (correction_id,)).fetchone()
            self.assertTrue(correction_evidence_is_current(correction))
            edit_draft(db, correction_id, correction["body"] + "\nДополнение редактора.")
            correction = db.execute("SELECT * FROM drafts WHERE id=?", (correction_id,)).fetchone()
            self.assertFalse(correction_evidence_is_current(correction))
            with patch("app.corrections.check_publication_link", return_value=check):
                mark_correction_evidence_checked(
                    db, correction_id, "Повторно сверил исправление", "Сохранённый текст соответствует странице.",
                )
            set_decision(db, correction_id, "approved", {
                "evidence_checked": True, "independence_checked": True,
            })
            self.assertEqual(db.execute("SELECT status FROM drafts WHERE id=?", (correction_id,)).fetchone()[0],
                             "approved")
            db.close()

    def test_schedule_install_enables_wake_and_catch_up_settings(self) -> None:
        from app import schedule

        with (
            patch.object(schedule, "_ensure_windows"),
            patch.object(schedule, "_ensure_moscow_timezone"),
            patch.object(schedule.Path, "is_file", return_value=True),
            patch.object(schedule, "_run_powershell") as run_script,
            patch.object(schedule, "schedule_status", return_value=[]),
        ):
            install_schedule("C:/python/python.exe")
        script = run_script.call_args.args[0]
        self.assertIn("-WakeToRun", script)
        self.assertIn("-StartWhenAvailable", script)
        self.assertIn("2099-12-31T23:59:59", script)
        self.assertIn("Сборщик информации - Сбор каждый час", script)
        self.assertIn("-m app fetch", script)
        self.assertIn("$hour -lt 24", script)
        self.assertIn("New-ScheduledTaskTrigger -Daily", script)
        self.assertNotIn("25000", script)
        self.assertIn("Сборщик информации - Публикация 14-00", script)
        self.assertIn("Unregister-ScheduledTask", script)

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
            legacy.execute(
                """CREATE TABLE draft_claims (
                   id INTEGER PRIMARY KEY, draft_id INTEGER NOT NULL, group_id INTEGER NOT NULL,
                   item_id INTEGER NOT NULL, claim_text TEXT NOT NULL, source_id TEXT NOT NULL,
                   source_name TEXT NOT NULL, source_level TEXT NOT NULL DEFAULT '',
                   published_at TEXT NOT NULL, source_url TEXT NOT NULL,
                   same_url_other_source INTEGER NOT NULL DEFAULT 0,
                   publication_check_status TEXT NOT NULL DEFAULT '', publication_checked_at TEXT,
                   link_checked_at TEXT, UNIQUE(draft_id,item_id))"""
            )
            legacy.commit()
            legacy.close()

            db = connect(database)
            migrated = db.execute("SELECT correction_of_id,requires_group_review FROM drafts WHERE id=1").fetchone()
            self.assertIsNone(migrated["correction_of_id"])
            self.assertEqual(migrated["requires_group_review"], 1)
            claim_columns = {row[1] for row in db.execute("PRAGMA table_info(draft_claims)")}
            self.assertIn("link_check_note", claim_columns)
            db.close()

    def test_draft_creation_survives_missing_model_and_requires_duplicate_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.sqlite3")
            now = datetime.now(timezone.utc).replace(microsecond=0)
            current = now.isoformat()
            for number, title in enumerate(("Front line report A", "Front line report B"), start=1):
                insert_test_item(db, title, f"https://example.org/{number}", current, current)

            with patch.dict(sys.modules, {"sentence_transformers": None}):
                draft_id = create_daily_draft(db)

            draft = db.execute("SELECT body,requires_group_review FROM drafts WHERE id=?", (draft_id,)).fetchone()
            self.assertIn(MODEL_FALLBACK_NOTICE, draft["body"])
            self.assertEqual(draft["requires_group_review"], 1)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM draft_groups g JOIN draft_claims c ON c.group_id=g.id "
                "WHERE g.draft_id=? GROUP BY g.id HAVING COUNT(*)>1", (draft_id,),
            ).fetchall(), [])

            claims = db.execute("SELECT * FROM draft_claims WHERE draft_id=?", (draft_id,)).fetchall()
            for claim in claims:
                self._check_test_claim_link(db, draft_id, int(claim["id"]), claim["source_id"])
                review_claim_event_date(
                    db, draft_id, int(claim["id"]), "same_day",
                    note="test confirms event and publication date match",
                )
            checks = {"evidence_checked": True, "independence_checked": True}
            with self.assertRaisesRegex(ValueError, "groups-checked"):
                set_decision(db, draft_id, "approved", checks)
            set_decision(db, draft_id, "approved", {**checks, "groups_checked": True})
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
        CollectorAndReviewTests._check_test_claim_link(db, draft_id, claim_id, source_id)
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

    def test_malformed_success_response_is_marked_unknown_not_published(self) -> None:
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _size=-1):
                return b'{"unexpected":"shape"}'

        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "test.sqlite3")
            scheduled = datetime(2026, 10, 18, 11, 0, tzinfo=timezone.utc)
            draft_id = self._create_approved_digest(db, scheduled - timedelta(minutes=30))
            with patch.dict(os.environ, {"MAX_BOT_TOKEN": "test-token", "MAX_CHANNEL_ID": "-100"}), patch("app.publisher.urllib.request.urlopen", return_value=Response()):
                with self.assertRaisesRegex(RuntimeError, "результат"):
                    publish(db, draft_id, now=scheduled)
            self.assertEqual(db.execute(
                "SELECT state FROM publish_attempts WHERE draft_id=?", (draft_id,),
            ).fetchone()[0], "unknown")
            self.assertEqual(db.execute("SELECT status FROM drafts WHERE id=?", (draft_id,)).fetchone()[0],
                             "approved")
            with self.assertRaisesRegex(ValueError, "действительную ссылку"):
                resolve_attempt(db, draft_id, posted=True, url="https://evil.example/post")
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

        class FakeOpener:
            def open(self, request, timeout=0):
                return fake_urlopen(request, timeout=timeout)

        with tempfile.TemporaryDirectory() as tmp, patch("app.fetcher.urllib.request.urlopen", fake_urlopen), patch("app.publication_validator.urllib.request.build_opener", return_value=FakeOpener()):
            db = connect(Path(tmp) / "test.sqlite3")
            try:
                result = fetcher.fetch_enabled_sources(db, [source])[0]
                self.assertEqual((result["count"], result["duplicates"], result["filtered_out"]), (1, 1, 0))
                row = db.execute("SELECT * FROM items").fetchone()
                self.assertEqual(row["url"], "https://example.org/story")
                raw = db.execute("SELECT payload FROM raw_items WHERE id=?", (row["raw_id"],)).fetchone()
                self.assertEqual(json.loads(raw["payload"])["publication_check"]["status"], "http_ok_feed")
            finally:
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
            claim = db.execute("SELECT source_id FROM draft_claims WHERE id=?", (claim_id,)).fetchone()
            self._check_test_claim_link(db, draft_id, claim_id, claim["source_id"])
            checked_claim = db.execute("SELECT * FROM draft_claims WHERE id=?", (claim_id,)).fetchone()
            self.assertEqual(checked_claim["publication_check_status"], "http_ok")
            self.assertTrue(checked_claim["link_check_note"])
            self.assertTrue(claim_link_check_is_current(checked_claim))
            stale_time = datetime.now(timezone.utc) + timedelta(hours=25)
            self.assertFalse(claim_link_check_is_current(checked_claim, now=stale_time))
            source = {"id": claim["source_id"], "url": "https://example.org",
                      "allowed_domains": ["example.org"]}
            with patch("app.review_queue.get_source", return_value=source), \
                    patch("app.review_queue.check_publication_link", side_effect=ValueError("HTTP 404")):
                with self.assertRaisesRegex(ValueError, "404"):
                    mark_claim_link_checked(
                        db, draft_id, claim_id, checked=True,
                        note="Повторная проверка ссылки.",
                    )
            failed_recheck = db.execute("SELECT * FROM draft_claims WHERE id=?", (claim_id,)).fetchone()
            self.assertFalse(claim_link_check_is_current(failed_recheck))
            self._check_test_claim_link(db, draft_id, claim_id, claim["source_id"])
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
