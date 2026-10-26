from __future__ import annotations

import io
import json
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import test_pipeline as fixtures
from app.db import connect
from app.publisher import (_process_alive, publish, publish_preflight, publish_scheduled,
                           resolve_attempt)
from app.review_queue import (approval_blockers, edit_draft, get_draft, mark_claim_link_checked,
                              reopen_draft, review_claim_event_date, set_decision)


CHECKS = {"evidence_checked": True, "independence_checked": True, "groups_checked": True}
POST_URL = "https://max.ru/test-channel/post-1"


class Response:
    status = 200

    def __init__(self, payload=None):
        self.payload = payload if payload is not None else {
            "message": {"url": POST_URL, "body": {"mid": "mid.post-1"}},
        }

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class WorkflowReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "workflow.sqlite3"
        self.db = connect(self.path)
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.draft_id = fixtures.CollectorAndReviewTests._create_approved_digest(
            self.db, self.now - timedelta(minutes=30),
        )

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def pending(self):
        reopen_draft(self.db, self.draft_id)
        return get_draft(self.db, self.draft_id)

    def attempt(self, state="started", *, expired=False, owner=None, created=None):
        created = created or self.now
        expires = self.now + timedelta(minutes=-10 if expired else 5)
        self.db.execute(
            """INSERT INTO publish_attempts(draft_id,attempt_no,state,created_at,lease_expires_at,owner_pid)
               VALUES(?,1,?,?,?,?)""",
            (self.draft_id, state, created.isoformat(), expires.isoformat(), owner),
        )
        self.db.commit()

    def test_approval_snapshots_exact_revision_and_hash(self):
        draft = get_draft(self.db, self.draft_id)
        self.assertEqual(draft["approved_revision"], draft["revision"])
        self.assertEqual(len(draft["approved_hash"]), 64)
        event = self.db.execute("SELECT details FROM events WHERE kind='draft_approved'").fetchone()
        self.assertEqual(json.loads(event["details"])["body_hash"], draft["approved_hash"])
        self.assertEqual(publish_preflight(self.db, self.draft_id), [])

    def test_stale_expected_revision_refuses_edit_and_approval(self):
        pending = self.pending()
        edit_draft(self.db, self.draft_id, pending["body"] + "\n", expected_revision=pending["revision"])
        for action in (
            lambda: edit_draft(self.db, self.draft_id, "stale", expected_revision=pending["revision"]),
            lambda: set_decision(self.db, self.draft_id, "approved", CHECKS, expected_revision=pending["revision"]),
        ):
            with self.assertRaisesRegex(ValueError, "изменён"):
                action()
        self.assertEqual(get_draft(self.db, self.draft_id)["body"], pending["body"] + "\n")
        self.assertFalse(self.db.in_transaction)

    def test_approved_text_cannot_be_changed_by_old_entry_point(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE drafts SET body='unexpected' WHERE id=?", (self.draft_id,))
        self.db.rollback()
        self.assertEqual(publish_preflight(self.db, self.draft_id), [])

    def test_unstructured_extra_bullet_is_rejected(self):
        draft = self.pending()
        self.db.execute("UPDATE drafts SET body=? WHERE id=?",
                        (draft["body"] + "\n• ПОДТВЕРЖДЕНО — unsupported statement", self.draft_id))
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "Число пунктов"):
            set_decision(self.db, self.draft_id, "approved", CHECKS)

    def test_nonbullet_claim_with_an_unrelated_bullet_is_rejected(self):
        draft = self.pending()
        body = draft["body"].replace("• **ЗАЯВЛЕНИЕ ИСТОЧНИКА**", "**ЗАЯВЛЕНИЕ ИСТОЧНИКА**")
        body += "\n• ЗАЯВЛЕНИЕ ИСТОЧНИКА — unrelated"
        self.db.execute("UPDATE drafts SET body=? WHERE id=?", (body, self.draft_id))
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "Каждый пункт"):
            set_decision(self.db, self.draft_id, "approved", CHECKS)

    def test_approval_and_audit_rollback_together(self):
        self.pending()
        self.db.execute("""CREATE TRIGGER fail_approval_audit BEFORE INSERT ON events
            WHEN NEW.kind='draft_approved' BEGIN SELECT RAISE(ABORT,'audit failure'); END""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "audit failure"):
            set_decision(self.db, self.draft_id, "approved", CHECKS)
        self.assertEqual(get_draft(self.db, self.draft_id)["status"], "pending")
        self.assertIsNone(get_draft(self.db, self.draft_id)["approved_revision"])
        self.assertFalse(self.db.in_transaction)

    def test_concurrent_edit_and_approval_only_one_revision_can_win(self):
        draft = self.pending()
        barrier = threading.Barrier(2)
        outcomes = []

        def worker(approve):
            db = connect(self.path)
            try:
                barrier.wait(timeout=5)
                if approve:
                    set_decision(db, self.draft_id, "approved", CHECKS, expected_revision=draft["revision"])
                else:
                    edit_draft(db, self.draft_id, draft["body"] + "\n", expected_revision=draft["revision"])
                outcomes.append((approve, "ok"))
            except ValueError:
                outcomes.append((approve, "stale"))
            finally:
                db.close()

        threads = [threading.Thread(target=worker, args=(value,)) for value in (True, False)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(sorted(result for _approve, result in outcomes), ["ok", "stale"])
        current = get_draft(self.db, self.draft_id)
        if current["status"] == "approved":
            self.assertEqual(current["body"], draft["body"])
        else:
            self.assertEqual(current["body"], draft["body"] + "\n")

    def test_remote_link_check_does_not_attest_concurrently_edited_text(self):
        draft = self.pending()
        claim_id = self.db.execute("SELECT id FROM draft_claims WHERE draft_id=?", (self.draft_id,)).fetchone()[0]

        def remote_check(*_args, **_kwargs):
            other = connect(self.path)
            try:
                edit_draft(other, self.draft_id, draft["body"] + "\n", expected_revision=draft["revision"])
            finally:
                other.close()
            return {"status": "http_ok", "checked_at": self.now.isoformat()}

        with patch("app.review_queue.get_source", return_value={"id": "test"}), \
                patch("app.review_queue.check_publication_link", side_effect=remote_check):
            with self.assertRaisesRegex(ValueError, "изменён"):
                mark_claim_link_checked(self.db, self.draft_id, claim_id, True, "checked")
        self.assertIsNone(self.db.execute("SELECT link_checked_at FROM draft_claims WHERE id=?", (claim_id,)).fetchone()[0])

    def test_dry_run_is_offline_and_does_not_open_credentials(self):
        before = self.db.total_changes
        with patch("app.publisher.get_max_credentials", side_effect=AssertionError("credentials opened")), \
                patch("app.publisher.urllib.request.urlopen", side_effect=AssertionError("network")):
            self.assertEqual(publish(self.db, self.draft_id, dry_run=True)["status"], "dry_run")
        self.assertEqual(self.db.total_changes, before)
        self.assertFalse(self.db.in_transaction)

    def test_confirmation_revision_is_rechecked_after_credential_io(self):
        confirmed = get_draft(self.db, self.draft_id)
        updated_body = "Примечание редактора.\n" + confirmed["body"]

        def credentials_after_concurrent_edit():
            other = connect(self.path)
            try:
                reopen_draft(other, self.draft_id, expected_revision=confirmed["revision"])
                edit_draft(other, self.draft_id, updated_body)
                claim = other.execute(
                    "SELECT id,source_id FROM draft_claims WHERE draft_id=?", (self.draft_id,)
                ).fetchone()
                fixtures.CollectorAndReviewTests._check_test_claim_link(
                    other, self.draft_id, claim["id"], claim["source_id"],
                )
                review_claim_event_date(other, self.draft_id, claim["id"], "same_day", note="checked again")
                set_decision(other, self.draft_id, "approved", CHECKS)
            finally:
                other.close()
            return "fake-token", "1"

        with patch("app.publisher.get_max_credentials", side_effect=credentials_after_concurrent_edit), \
                patch("app.publisher.urllib.request.urlopen") as send:
            with self.assertRaisesRegex(ValueError, "изменён"):
                publish(self.db, self.draft_id, expected_revision=confirmed["revision"])
            send.assert_not_called()
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM publish_attempts").fetchone()[0], 0)
        current = get_draft(self.db, self.draft_id)
        self.assertEqual((current["status"], current["body"]), ("approved", updated_body))
        self.assertFalse(self.db.in_transaction)

    def test_stale_confirmation_blocks_read_only_preview(self):
        confirmed = get_draft(self.db, self.draft_id)
        self.pending()
        set_decision(self.db, self.draft_id, "approved", CHECKS)
        self.assertTrue(any("изменён" in blocker for blocker in publish_preflight(
            self.db, self.draft_id, expected_revision=confirmed["revision"],
        )))
        with patch("app.publisher.get_max_credentials", side_effect=AssertionError("credentials opened")), \
                patch("app.publisher.urllib.request.urlopen", side_effect=AssertionError("network")):
            with self.assertRaisesRegex(ValueError, "изменён"):
                publish(self.db, self.draft_id, dry_run=True, expected_revision=confirmed["revision"])

    def test_invalid_snapshot_blocks_manual_preview_and_scheduler(self):
        self.db.execute("UPDATE drafts SET approved_hash='' WHERE id=?", (self.draft_id,))
        self.db.commit()
        with patch("app.publisher.get_max_credentials", side_effect=AssertionError("credentials opened")):
            for dry_run in (True, False):
                with self.assertRaisesRegex(ValueError, "текущей версии"):
                    publish(self.db, self.draft_id, dry_run=dry_run)
        self.assertEqual(publish_scheduled(self.db, dry_run=True)["status"], "needs_review")

    def test_readiness_reports_expired_reviews_without_mutating(self):
        before = self.db.total_changes
        blockers = approval_blockers(self.db, self.draft_id, now=self.now + timedelta(hours=25))
        self.assertTrue(any("claim-check" in blocker for blocker in blockers))
        self.assertEqual(self.db.total_changes, before)

    def test_success_saves_durable_message_id_and_prevents_resend(self):
        with patch("app.publisher.get_max_credentials", return_value=("fake-token", "1")), \
                patch("app.publisher.urllib.request.urlopen", return_value=Response()) as send:
            result = publish(self.db, self.draft_id)
            self.assertEqual(result["message_id"], "mid.post-1")
            with self.assertRaises(ValueError):
                publish(self.db, self.draft_id)
            self.assertEqual(send.call_count, 1)
        self.assertEqual(get_draft(self.db, self.draft_id)["max_message_id"], "mid.post-1")
        attempt = self.db.execute("SELECT * FROM publish_attempts").fetchone()
        self.assertEqual((attempt["state"], attempt["message_id"]), ("succeeded", "mid.post-1"))
        self.assertTrue(attempt["body_hash"])

    def test_malformed_success_is_unknown_and_cannot_resend(self):
        with patch("app.publisher.get_max_credentials", return_value=("fake-token", "1")), \
                patch("app.publisher.urllib.request.urlopen", return_value=Response({})) as send:
            with self.assertRaises(RuntimeError):
                publish(self.db, self.draft_id)
            with self.assertRaises(ValueError):
                publish(self.db, self.draft_id)
            self.assertEqual(send.call_count, 1)
        self.assertEqual(self.db.execute("SELECT state FROM publish_attempts").fetchone()[0], "unknown")
        self.assertEqual(get_draft(self.db, self.draft_id)["status"], "approved")

    def test_http_failure_redacts_token_and_allows_explicit_retry(self):
        token = "fake-token-never-print"
        error = urllib.error.HTTPError("https://example.org", 400, "bad request", None,
                                       io.BytesIO(("rejected " + token).encode()))
        with patch("app.publisher.get_max_credentials", return_value=(token, "1")), \
                patch("app.publisher.urllib.request.urlopen", side_effect=[error, Response()]):
            with self.assertRaises(RuntimeError) as caught:
                publish(self.db, self.draft_id)
            self.assertNotIn(token, str(caught.exception))
            self.assertEqual(self.db.execute("SELECT state FROM publish_attempts").fetchone()[0], "failed")
            self.assertEqual(publish(self.db, self.draft_id)["status"], "published")
        journal = " ".join(row[0] for row in self.db.execute("SELECT details FROM events"))
        self.assertNotIn(token, journal)
        self.assertIn("[REDACTED_SECRET]", journal)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM publish_attempts").fetchone()[0], 2)

    def test_server_or_transport_failure_is_unknown(self):
        for failure in (
            urllib.error.HTTPError("https://example.org", 500, "server", None, io.BytesIO(b"failed")),
            urllib.error.URLError("fake-token transport"),
        ):
            with self.subTest(failure=type(failure).__name__), \
                    patch("app.publisher.get_max_credentials", return_value=("fake-token", "1")), \
                    patch("app.publisher.urllib.request.urlopen", side_effect=failure):
                with self.assertRaises(RuntimeError):
                    publish(self.db, self.draft_id)
                attempt = self.db.execute("SELECT state,details FROM publish_attempts ORDER BY id DESC LIMIT 1").fetchone()
                self.assertEqual(attempt["state"], "unknown")
                self.assertNotIn("fake-token", attempt["details"])
                resolve_attempt(self.db, self.draft_id, posted=False)

    def test_competing_publishers_and_live_recovery_cannot_send_twice(self):
        started, release = threading.Event(), threading.Event()
        outcomes = []

        def blocked_response(*_args, **_kwargs):
            started.set()
            if not release.wait(timeout=10):
                raise AssertionError("test did not release MAX response")
            return Response()

        def sender():
            db = connect(self.path)
            try:
                outcomes.append(publish(db, self.draft_id)["status"])
            finally:
                db.close()

        with patch("app.publisher.get_max_credentials", return_value=("fake-token", "1")), \
                patch("app.publisher.urllib.request.urlopen", side_effect=blocked_response) as send:
            thread = threading.Thread(target=sender)
            thread.start()
            try:
                self.assertTrue(started.wait(timeout=5))
                with self.assertRaises(ValueError):
                    publish(self.db, self.draft_id)
                with self.assertRaisesRegex(ValueError, "процесс ещё работает"):
                    resolve_attempt(self.db, self.draft_id, posted=False, now=self.now + timedelta(hours=1))
                with self.assertRaises(ValueError):
                    reopen_draft(self.db, self.draft_id)
            finally:
                release.set()
                thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(send.call_count, 1)
        self.assertEqual(outcomes, ["published"])

    def test_dead_started_attempt_requires_lease_expiry_and_manual_resolution(self):
        self.attempt(owner=2147483647)
        with patch("app.publisher._process_alive", return_value=False):
            with self.assertRaisesRegex(ValueError, "lease"):
                resolve_attempt(self.db, self.draft_id, posted=False, now=self.now)
            resolve_attempt(self.db, self.draft_id, posted=False, now=self.now + timedelta(minutes=6))
        self.assertEqual(self.db.execute("SELECT state FROM publish_attempts").fetchone()[0], "failed")

    def test_live_owner_is_blocked_even_after_expired_lease(self):
        self.attempt(owner=os.getpid(), expired=True)
        with self.assertRaisesRegex(ValueError, "процесс ещё работает"):
            resolve_attempt(self.db, self.draft_id, posted=False)

    def test_manual_reconciliation_keeps_original_send_date(self):
        yesterday = self.now - timedelta(days=1)
        self.attempt(state="unknown", created=yesterday)
        resolve_attempt(self.db, self.draft_id, posted=True, url=POST_URL)
        self.assertEqual(datetime.fromisoformat(get_draft(self.db, self.draft_id)["published_at"]), yesterday)

    def test_unresolved_attempt_from_yesterday_blocks_another_main_post(self):
        self.attempt(state="unknown", created=self.now - timedelta(days=1))
        other_id = fixtures.CollectorAndReviewTests._create_approved_digest(
            self.db, self.now - timedelta(minutes=15),
        )
        blockers = publish_preflight(self.db, other_id)
        self.assertTrue(any("попытка публикации" in blocker for blocker in blockers))

    def test_process_liveness_check_is_read_only(self):
        self.assertTrue(_process_alive(os.getpid()))
        self.assertFalse(_process_alive(2147483647))


if __name__ == "__main__":
    unittest.main()
