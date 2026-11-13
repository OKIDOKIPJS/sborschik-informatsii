from __future__ import annotations

import http.client
import json
import socket
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from pathlib import Path
from unittest.mock import patch

from app.backup import verify_backup
from app.db import connect
from app.draft_generator import OLDER_NOTE, _draft_footer
from app.web import EditorServer, MAX_BODY
from app.web_service import clean_publication_text


class EditorHttpTests(unittest.TestCase):
    """Real loopback HTTP and temporary SQLite; external calls are forbidden."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "editor.sqlite3"
        self.db = connect(self.path)
        self.addCleanup(self.db.close)
        self.source = {"id": "test", "name": "Publisher", "enabled": False,
                       "url": "https://example.org", "level": "primary",
                       "retrieval": {"method": "manual"}}
        self.config_patch = patch("app.web_service.load_sources", return_value={"sources": [self.source]})
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        self.http_guard = patch("urllib.request.urlopen", side_effect=AssertionError("External HTTP forbidden in web tests"))
        self.http_guard.start()
        self.addCleanup(self.http_guard.stop)
        self.credentials_guard = patch("app.publisher.get_max_credentials", side_effect=AssertionError("Credentials forbidden in web tests"))
        self.credentials_guard.start()
        self.addCleanup(self.credentials_guard.stop)
        self.now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.claim_text = "Claim <img src=x onerror=alert(2)>"
        self.source_url = "https://example.org/report?x=1&y=2"
        self.event_line = "— Событие произошло 2026-10-25; материал опубликован 2026-10-26."
        self.body = "\n\n".join((
            "Title",
            "Окно отбора: публикации за последние 24 часа. Это дата публикации, а не обязательно дата события.",
            OLDER_NOTE,
            f"• **ЗАЯВЛЕНИЕ ИСТОЧНИКА** — Publisher сообщает: {self.claim_text}.\n"
            f"{self.event_line}\n"
            f"— Publisher, опубликовано 26.10.2026, 10:00 МСК; запись есть в RSS/Atom, ссылка отвечает HTTP 200: {self.source_url}",
            _draft_footer(),
        ))
        self.draft_id = self.seed_draft()
        self.server = EditorServer(("127.0.0.1", 0), self.path)
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        status, headers, _body = self.request("GET", "/", authenticated=False)
        self.assertEqual(status, 200)
        cookie = SimpleCookie()
        cookie.load(headers["set-cookie"])
        self.cookie = f"editor_session={cookie['editor_session'].value}"
        status, _headers, body = self.request("GET", "/api/session")
        self.assertEqual(status, 200)
        self.csrf = json.loads(body)["csrf"]

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.assertFalse(self.thread.is_alive())

    def seed_draft(self):
        raw_id = self.db.execute(
            "INSERT INTO raw_items(source_id,title,url,published_at,fetched_at,origin) VALUES('test',?,?,?,?,'test')",
            (self.claim_text, self.source_url, self.now, self.now),
        ).lastrowid
        item_id = self.db.execute(
            "INSERT INTO items(raw_id,source_id,source_name,source_type,source_level,title,summary,url,published_at,fetched_at,content_hash) "
            "VALUES(?,'test','Publisher','official','primary',?,'Description',?,?,?,'web-fixture')",
            (raw_id, self.claim_text, self.source_url, self.now, self.now),
        ).lastrowid
        version_id = self.db.execute(
            "INSERT INTO item_versions(item_id,version_no,title,summary,url,published_at,fetched_at,content_hash,fingerprint,detected_at,last_checked_at) "
            "VALUES(?,1,?,'Description',?,?,?,'web-fixture','web-fixture',?,?)",
            (item_id, self.claim_text, self.source_url, self.now, self.now, self.now, self.now),
        ).lastrowid
        draft_id = self.db.execute(
            "INSERT INTO drafts(title,body,status,created_at,updated_at) VALUES(?,?,'pending',?,?)",
            ("<script>alert(1)</script>", self.body, self.now, self.now),
        ).lastrowid
        group_id = self.db.execute("INSERT INTO draft_groups(draft_id,group_no) VALUES(?,1)", (draft_id,)).lastrowid
        self.claim_id = self.db.execute(
            "INSERT INTO draft_claims(draft_id,group_id,item_id,version_id,claim_text,source_id,source_name,source_level,published_at,source_url) "
            "VALUES(?,?,?,?,?,'test','Publisher','primary',?,?)",
            (draft_id, group_id, item_id, version_id, self.claim_text, self.now, self.source_url),
        ).lastrowid
        self.db.commit()
        return int(draft_id)

    def draft(self):
        return self.db.execute("SELECT * FROM drafts WHERE id=?", (self.draft_id,)).fetchone()

    def request(self, method, path, body=None, headers=None, authenticated=True):
        request_headers = {}
        if authenticated:
            request_headers["Cookie"] = self.cookie
            if method == "POST":
                request_headers.update({"X-Editor-CSRF": self.csrf, "Origin": self.origin})
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request_headers.update(headers or {})
        payload = body.encode("utf-8") if isinstance(body, str) else (body or b"")
        request_headers.setdefault("Host", f"127.0.0.1:{self.server.server_port}")
        request_headers.setdefault("Content-Length", str(len(payload)))
        request_headers.setdefault("Connection", "close")
        wire_headers = "\r\n".join(f"{key}: {value}" for key, value in request_headers.items())
        wire = f"{method} {path} HTTP/1.1\r\n{wire_headers}\r\n\r\n".encode("latin-1") + payload
        # Send a complete short request together. On Windows an auth rejection
        # can close a socket before http.client sends a separate body packet.
        with socket.create_connection(("127.0.0.1", self.server.server_port), timeout=4) as connection:
            connection.sendall(wire)
            response = http.client.HTTPResponse(connection)
            response.begin()
            return response.status, {key.lower(): value for key, value in response.getheaders()}, response.read()

    def action(self, action, **data):
        payload = {"id": self.draft_id, "revision": self.draft()["revision"], **data}
        return self.request("POST", f"/api/actions/{action}", payload)

    def test_cookie_session_and_security_headers(self):
        status, headers, body = self.request("GET", "/", authenticated=False)
        self.assertEqual(status, 200)
        self.assertIn("HttpOnly", headers["set-cookie"])
        self.assertIn("SameSite=Strict", headers["set-cookie"])
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        self.assertIn("frame-ancestors 'none'", headers["content-security-policy"])
        self.assertNotIn(self.csrf.encode(), body)
        status, _headers, _body = self.request("GET", "/api/session", authenticated=False)
        self.assertEqual(status, 403)

    def test_host_restriction_rejects_dns_rebinding(self):
        for path in ("/", "/api/state"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path, headers={"Host": "attacker.example"})[0], 403)
        self.assertEqual(self.request("POST", "/api/backup", {}, headers={"Host": "attacker.example"})[0], 403)

    def test_localhost_host_alias_is_accepted(self):
        status, _headers, _body = self.request("GET", "/api/session", headers={"Host": f"localhost:{self.server.server_port}"})
        self.assertEqual(status, 200)

    def test_post_requires_cookie_csrf_and_allowed_origin(self):
        scenarios = [({"Cookie": "editor_session=wrong"}, 403),
                     ({"X-Editor-CSRF": ""}, 403),
                     ({"X-Editor-CSRF": "wrong"}, 403),
                     ({"Origin": "https://attacker.example"}, 403),
                     ({"Origin": "null"}, 403)]
        for headers, expected in scenarios:
            with self.subTest(headers=headers):
                self.assertEqual(self.request("POST", "/api/backup", {}, headers=headers)[0], expected)
        self.assertEqual(self.request("POST", "/api/backup", {}, authenticated=False)[0], 403)

    def test_malformed_nonascii_cookie_and_csrf_return_403_without_crash(self):
        self.assertEqual(self.request("GET", "/api/state", headers={"Cookie": 'editor_session="é"'})[0], 403)
        self.assertEqual(self.request("POST", "/api/backup", {}, headers={"X-Editor-CSRF": "é"})[0], 403)
        self.assertEqual(self.request("GET", "/api/session")[0], 200)

    def test_post_rejects_invalid_type_json_utf8_and_length(self):
        scenarios = [
            ({}, {"Content-Type": "text/plain"}, 415),
            (b"not-json", {"Content-Type": "application/json"}, 400),
            (b"\xff", {"Content-Type": "application/json"}, 400),
            ([], {}, 400),
            ({}, {"Content-Length": str(MAX_BODY + 1)}, 413),
            ({}, {"Content-Length": "0"}, 413),
            ({}, {"Content-Length": "-1"}, 413),
            ({}, {"Content-Length": "invalid"}, 400),
            ({}, {"Transfer-Encoding": "chunked"}, 400),
        ]
        for body, headers, expected in scenarios:
            with self.subTest(headers=headers, body=body):
                self.assertEqual(self.request("POST", "/api/backup", body, headers=headers)[0], expected)

    def test_static_allowlist_and_path_traversal(self):
        for path, kind in (("/app.js", "text/javascript"), ("/style.css", "text/css")):
            status, headers, body = self.request("GET", path)
            self.assertEqual(status, 200)
            self.assertTrue(headers["content-type"].startswith(kind))
            self.assertGreater(len(body), 100)
        for path in ("/../sources.yaml", "/%2e%2e/sources.yaml", "/app/credentials.py", "/static/../web.py", "/api/export/../1"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0], 404)

    def test_state_and_draft_detail_include_evidence_and_blockers(self):
        status, _headers, body = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        state = json.loads(body)
        self.assertEqual(state["materials_count"], 1)
        self.assertEqual(state["drafts"][0]["id"], self.draft_id)
        self.assertEqual(state["sources"][0]["id"], "test")
        status, _headers, body = self.request("GET", f"/api/drafts/{self.draft_id}")
        self.assertEqual(status, 200)
        draft = json.loads(body)
        self.assertEqual(draft["claims"][0]["source_url"], self.source_url)
        self.assertFalse(draft["claims"][0]["link_current"])
        self.assertTrue(draft["approval_blockers"])
        self.assertTrue(draft["publish_blockers"])

    def test_bad_configuration_keeps_saved_drafts_accessible(self):
        with patch("app.web_service.load_sources", side_effect=ValueError("Invalid source configuration")):
            status, _headers, body = self.request("GET", "/api/state")
        state = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(state["drafts"][0]["id"], self.draft_id)
        self.assertEqual(state["sources"], [])
        self.assertIn("Invalid source", state["config_error"])

    def test_exports_preserve_data_and_escape_html(self):
        status, headers, body = self.request("GET", f"/api/export/{self.draft_id}?format=json")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["body"], self.body)
        self.assertIn(f"draft-{self.draft_id}.json", headers["content-disposition"])
        status, headers, body = self.request("GET", f"/api/export/{self.draft_id}?format=txt")
        self.assertEqual(status, 200)
        self.assertEqual(body.decode(), self.body)
        self.assertTrue(headers["content-type"].startswith("text/plain"))
        status, headers, body = self.request("GET", f"/api/export/{self.draft_id}?format=html")
        self.assertEqual(status, 200)
        document = body.decode()
        self.assertIn("&lt;script&gt;", document)
        self.assertIn("&lt;img", document)
        self.assertNotIn("<script>", document)
        self.assertNotIn("<img src=x", document)
        self.assertIn("https://example.org/report?x=1&amp;y=2", document)
        self.assertEqual(self.request("GET", f"/api/export/{self.draft_id}?format=exe")[0], 400)

    def test_stale_revision_cannot_overwrite_newer_text(self):
        old_revision = self.draft()["revision"]
        self.assertEqual(self.action("edit", text="Newer editor text")[0], 200)
        status, _headers, body = self.action("edit", revision=old_revision, text="Stale editor text")
        self.assertEqual(status, 400)
        self.assertIn("измен", json.loads(body)["error"])
        self.assertEqual(self.draft()["body"], "Newer editor text")

    def test_approve_cannot_bypass_evidence_or_boolean_check_types(self):
        for checks in ({}, {"evidence_checked": "true", "independence_checked": True},
                       {"evidence_checked": True, "independence_checked": True, "groups_checked": True}):
            with self.subTest(checks=checks):
                status, _headers, _body = self.action("approve", checks=checks)
                self.assertEqual(status, 400)
                self.assertEqual(self.draft()["status"], "pending")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM events WHERE kind='draft_approved'").fetchone()[0], 0)

    def test_pending_draft_cannot_publish_even_with_http_confirmation(self):
        status, _headers, _body = self.action("publish", confirmed=True)
        self.assertEqual(status, 400)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM publish_attempts").fetchone()[0], 0)

    def test_publish_and_preview_routes_forward_to_fake_publisher_only(self):
        with patch("app.publisher.publish", return_value={"status": "dry_run", "text": "Preview"}) as publisher:
            status, _headers, body = self.action("dry-run")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["status"], "dry_run")
            self.assertTrue(publisher.call_args.kwargs["dry_run"])
            publisher.reset_mock()
            self.assertEqual(self.action("publish")[0], 400)
            publisher.assert_not_called()
            publisher.return_value = {"status": "published", "url": "https://max.ru/test/post/1"}
            self.assertEqual(self.action("publish", confirmed=True)[0], 200)
            self.assertFalse(publisher.call_args.kwargs["dry_run"])

    def test_backup_http_creates_verified_snapshot_only_inside_temp_runtime(self):
        status, _headers, body = self.request("POST", "/api/backup", {})
        self.assertEqual(status, 200)
        result = json.loads(body)
        backup = Path(result["backup_path"])
        self.assertEqual(backup.parent.resolve(), (self.path.parent / "backups").resolve())
        self.assertTrue(backup.is_file())
        self.assertEqual(verify_backup(backup)["status"], "ok")
        with closing(sqlite3.connect(backup)) as snapshot:
            self.assertEqual(snapshot.execute("SELECT body FROM drafts WHERE id=?", (self.draft_id,)).fetchone()[0], self.body)
        self.assertEqual(self.draft()["body"], self.body)

    def test_clean_text_is_idempotent_and_keeps_claim_date_source_url(self):
        cleaned = clean_publication_text(self.body)
        self.assertEqual(clean_publication_text(cleaned), cleaned)
        self.assertIn(self.claim_text, cleaned)
        self.assertIn(self.event_line, cleaned)
        self.assertIn(self.source_url, cleaned)
        self.assertIn("Материалы старше суток", cleaned)
        self.assertNotIn("HTTP 200", cleaned)
        self.assertNotIn(_draft_footer(), cleaned)
        self.assertEqual(self.action("clean-text")[0], 200)
        self.assertEqual(self.draft()["body"], cleaned)
        self.assertEqual(self.action("clean-text")[0], 200)
        self.assertEqual(self.draft()["body"], cleaned)

    def test_unexpected_service_errors_are_redacted_and_server_recovers(self):
        with patch("app.web_service.state", side_effect=RuntimeError("internal-secret-sentinel")):
            status, _headers, body = self.request("GET", "/api/state")
        self.assertEqual(status, 500)
        self.assertNotIn(b"internal-secret-sentinel", body)
        self.assertEqual(self.request("GET", "/api/state")[0], 200)

    def test_missing_main_asset_returns_json_error_instead_of_dropping_connection(self):
        with patch("app.web.STATIC", Path(self.tmp.name) / "missing-assets"):
            status, headers, body = self.request("GET", "/", authenticated=False)
        self.assertEqual(status, 500)
        self.assertTrue(headers["content-type"].startswith("application/json"))
        self.assertIn("error", json.loads(body))

    def test_non_loopback_bind_is_refused(self):
        with self.assertRaisesRegex(ValueError, "127.0.0.1"):
            EditorServer(("0.0.0.0", 0), self.path)


if __name__ == "__main__":
    unittest.main()
