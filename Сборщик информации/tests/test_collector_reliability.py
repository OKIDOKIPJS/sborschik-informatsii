from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from xml.sax.saxutils import escape

from app import fetcher
from app.db import connect


class FakeResponse:
    status = 200

    def __init__(self, url: str, payload: bytes = b""):
        self.url = url
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def geturl(self):
        return self.url

    def read(self, size=-1):
        return self.payload if size < 0 else self.payload[:size]


class CollectorReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = connect(Path(self.tmp.name) / "collector.sqlite3")
        self.addCleanup(self.db.close)
        self.source = {
            "id": "test", "name": "Test source", "type": "official", "level": "primary",
            "url": "https://example.org", "allowed_domains": ["example.org"], "enabled": True,
            "retrieval": {"method": "rss", "endpoint": "https://feeds.example.org/rss", "verified": True},
            "relevance_filter": {"high_confidence_terms": ["Ukraine war"], "context_terms": ["Ukraine"]},
        }

    @staticmethod
    def raw(url="https://example.org/report/1", title="Ukraine war update", published=None):
        return {"url": url, "title": title, "summary": "Daily report",
                "published_at": published or datetime.now(timezone.utc).isoformat()}

    @staticmethod
    def link_check(*_args):
        return {"status": "http_ok_feed", "checked_at": datetime.now(timezone.utc).isoformat()}

    @contextmanager
    def fake_http(self, payload: bytes):
        endpoint = self.source["retrieval"]["endpoint"]

        def urlopen(request, timeout=0):
            if request.full_url != endpoint or request.get_method() != "GET":
                raise AssertionError(f"Unexpected feed HTTP request: {request.full_url}")
            return FakeResponse(endpoint, payload)

        class FakeOpener:
            def open(_self, request, timeout=0):
                if request.get_method() != "HEAD" or not request.full_url.startswith("https://example.org/"):
                    raise AssertionError(f"Unexpected link HTTP request: {request.full_url}")
                return FakeResponse(request.full_url)

        with patch("urllib.request.urlopen", side_effect=urlopen), \
                patch("urllib.request.build_opener", return_value=FakeOpener()):
            yield

    @staticmethod
    def feed(items):
        records = "".join(
            f"<item><title>{escape(item['title'])}</title><link>{escape(item['url'])}</link>"
            f"<pubDate>{escape(item['published_at'])}</pubDate>"
            f"<description>{escape(item['summary'])}</description></item>" for item in items
        )
        return f"<rss version='2.0'><channel>{records}</channel></rss>".encode()

    def test_distinct_urls_with_identical_text_keep_separate_versions_and_dates(self):
        first = self.raw(published="2026-10-25T10:00:00Z")
        second = self.raw("https://example.org/report/2", published="2026-10-26T10:00:00Z")
        with patch("app.fetcher.check_publication_link", side_effect=self.link_check):
            self.assertEqual(fetcher._store(self.db, self.source, first, "rss"), "inserted")
            self.assertEqual(fetcher._store(self.db, self.source, second, "rss"), "inserted")
            self.assertEqual(fetcher._store(self.db, self.source, second, "rss"), "duplicate")
            revised = {**first, "summary": "Corrected report"}
            self.assertEqual(fetcher._store(self.db, self.source, revised, "rss"), "revised")

        items = self.db.execute("SELECT id,url,content_hash FROM items ORDER BY id").fetchall()
        self.assertEqual(len(items), 2)
        self.assertNotEqual(items[0]["content_hash"], items[1]["content_hash"])
        versions = self.db.execute(
            "SELECT item_id,version_no,published_at,summary FROM item_versions ORDER BY item_id,version_no"
        ).fetchall()
        self.assertEqual([(v["item_id"], v["version_no"]) for v in versions],
                         [(items[0]["id"], 1), (items[0]["id"], 2), (items[1]["id"], 1)])
        self.assertEqual(versions[-1]["published_at"], "2026-10-26T10:00:00+00:00")
        self.assertEqual(versions[1]["summary"], "Corrected report")

    def test_new_hashes_coexist_with_legacy_content_only_hashes(self):
        first = self.raw()
        with patch("app.fetcher.check_publication_link", side_effect=self.link_check):
            fetcher._store(self.db, self.source, first, "rss")
            legacy_hash = hashlib.sha256(b"test\nukraine war update daily report").hexdigest()
            self.db.execute("UPDATE items SET content_hash=?", (legacy_hash,))
            self.db.commit()
            self.assertEqual(fetcher._store(self.db, self.source, first, "rss"), "duplicate")
            self.assertEqual(fetcher._store(
                self.db, self.source, {**first, "url": "https://example.org/report/2"}, "rss"
            ), "inserted")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM items").fetchone()[0], 2)

    def test_relevance_counters_use_canonical_url_for_all_confidence_levels(self):
        items = [
            self.raw("https://example.org/a/?utm_source=x#fragment", "Ukraine war update"),
            self.raw("https://example.org/b?gclid=x", "Ukraine context"),
            self.raw("https://example.org/c?utm_campaign=x", "Unrelated update"),
        ]
        with self.fake_http(self.feed(items)):
            result = fetcher.fetch_enabled_sources(self.db, [self.source])[0]
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["count"], 3)
        self.assertEqual((result["high_confidence"], result["medium_confidence"],
                          result["low_confidence"], result["needs_review"]), (1, 1, 1, 1))
        event = self.db.execute("SELECT details FROM events WHERE kind='source_fetched'").fetchone()
        self.assertEqual(json.loads(event["details"])["needs_review"], 1)
        self.db.execute("UPDATE relevance_assessments SET status='excluded',reviewed_at='2026-10-26T00:00:00Z' "
                        "WHERE confidence='medium'")
        self.db.commit()
        with self.fake_http(self.feed(items)):
            repeated = fetcher.fetch_enabled_sources(self.db, [self.source])[0]
        self.assertEqual((repeated["duplicates"], repeated["editor_excluded"], repeated["needs_review"]), (3, 2, 0))

    def test_malformed_or_future_link_cache_is_refreshed(self):
        now = datetime.now(timezone.utc)
        bad_payloads = [[], {"publication_check": []},
                        {"publication_check": {"status": "http_ok_feed", "checked_at": "not-a-date"}},
                        {"publication_check": {"status": "http_ok_feed", "checked_at": now.replace(tzinfo=None).isoformat()}},
                        {"publication_check": {"status": "http_ok_feed", "checked_at": (now + timedelta(days=1)).isoformat()}},
                        {"publication_check": {"status": "unknown", "checked_at": now.isoformat()}}]
        raw = self.raw()
        with patch("app.fetcher.check_publication_link", side_effect=self.link_check) as checker:
            fetcher._store(self.db, self.source, raw, "rss")
            for payload in bad_payloads:
                with self.subTest(payload=payload):
                    self.db.execute("UPDATE raw_items SET payload=?", (json.dumps(payload),))
                    self.db.commit()
                    checker.reset_mock()
                    self.assertEqual(fetcher._store(self.db, self.source, raw, "rss"), "duplicate")
                    checker.assert_called_once()
            checker.reset_mock()
            fetcher._store(self.db, self.source, raw, "rss")
            checker.assert_not_called()

    def test_cached_head_does_not_bypass_changed_allowed_domains(self):
        with patch("app.fetcher.check_publication_link", side_effect=self.link_check) as checker:
            fetcher._store(self.db, self.source, self.raw(), "rss")
            checker.reset_mock()
            restricted_source = {**self.source, "allowed_domains": ["other.example"]}
            with self.assertRaisesRegex(ValueError, "Домен ссылки"):
                fetcher._store(self.db, restricted_source, self.raw(), "rss")
            checker.assert_not_called()

    def test_malformed_feed_entries_are_counted_and_partial_success_is_visible(self):
        good = self.raw()
        bad = self.raw("", "Broken entry")
        with self.fake_http(self.feed([good, bad])):
            result = fetcher.fetch_enabled_sources(self.db, [self.source])[0]
        self.assertEqual((result["status"], result["count"], result["filtered_out"]), ("partial", 1, 1))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM events WHERE kind='item_rejected'").fetchone()[0], 1)
        with self.fake_http(self.feed([bad])):
            failure = fetcher.fetch_enabled_sources(self.db, [self.source])[0]
        self.assertEqual((failure["status"], failure["count"], failure["filtered_out"]), ("error", 0, 1))

    def test_html_instead_of_feed_is_a_source_error(self):
        with self.fake_http(b"<html><body>Temporarily unavailable</body></html>"):
            result = fetcher.fetch_enabled_sources(self.db, [self.source])[0]
        self.assertEqual(result["status"], "error")
        self.assertIn("RSS", result["error"])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM items").fetchone()[0], 0)

    def test_reliefweb_country_guard_rejects_absent_and_other_country(self):
        def report(country):
            fields = {"title": "Report", "url": "https://reliefweb.int/report/test",
                      "source": [{"shortname": "OCHA"}], "date": {"original": "2026-10-26T00:00:00Z"}}
            if country is not None:
                fields["primary_country"] = [{"iso3": country}]
            return {"fields": fields}

        payload = json.dumps({"data": [report(None), report("POL"), report("UKR")]}).encode()
        retrieval = {"endpoint": "https://api.reliefweb.int/v2/reports", "query": {"limit": 100}}
        with patch.dict("os.environ", {"RELIEFWEB_APPNAME": "offline-test"}), \
                patch("urllib.request.urlopen", return_value=FakeResponse(retrieval["endpoint"], payload)):
            items = fetcher._fetch_reliefweb_api(self.source, retrieval)
        self.assertEqual(len(items), 1)


if __name__ == "__main__":
    unittest.main()
