from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app import corrections
from app.db import connect


class CorrectionEvidenceReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "editor.sqlite3"
        self.db = connect(self.path)
        self.addCleanup(self.db.close)
        self.peer = connect(self.path)
        self.addCleanup(self.peer.close)
        self.source = {"id": "test", "name": "Test", "url": "https://example.org",
                       "allowed_domains": ["example.org"]}
        self.config = patch("app.corrections.load_sources", return_value={"sources": [self.source]})
        self.config.start()
        self.addCleanup(self.config.stop)
        now = datetime.now(timezone.utc).isoformat()
        original = self.db.execute(
            "INSERT INTO drafts(title,body,status,created_at,updated_at,published_at,max_post_url) "
            "VALUES('Original','Original text','published',?,?,?,'https://max.ru/test/post/1')",
            (now, now, now),
        ).lastrowid
        self.db.commit()
        self.draft_id = corrections.create_correction_draft(
            self.db, original, "Ошибочная дата", "Исправленная дата", "https://example.org/report"
        )
        self.revision = self.draft()["revision"]

    def draft(self):
        return self.db.execute("SELECT * FROM drafts WHERE id=?", (self.draft_id,)).fetchone()

    def checked(self, *_args, **_kwargs):
        return {"status": "http_ok", "checked_at": datetime.now(timezone.utc).isoformat()}

    def review(self, **kwargs):
        return corrections.mark_correction_evidence_checked(
            self.db, self.draft_id, "Источник прочитан редактором", "Сверены дата и исправление", **kwargs
        )

    def assert_no_attestation(self):
        draft = self.draft()
        self.assertIsNone(draft["correction_link_checked_at"])
        self.assertEqual(draft["correction_link_check_note"], "")
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM events WHERE kind='correction_evidence_checked' AND entity_id=?",
            (str(self.draft_id),),
        ).fetchone()[0], 0)

    def test_success_saves_attestation_and_audit_in_one_revision(self):
        with patch("app.corrections.check_publication_link", side_effect=self.checked):
            result = self.review(expected_revision=self.revision)
        draft = self.draft()
        self.assertEqual(result["status"], "http_ok")
        self.assertEqual(draft["revision"], self.revision + 1)
        self.assertTrue(draft["correction_link_checked_at"])
        self.assertIn("Сверены дата", draft["correction_link_check_note"])
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM events WHERE kind='correction_evidence_checked'"
        ).fetchone()[0], 1)

    def test_already_approved_draft_is_rejected_before_http(self):
        self.peer.execute("UPDATE drafts SET status='approved' WHERE id=?", (self.draft_id,))
        self.peer.commit()
        with patch("app.corrections.check_publication_link") as checker:
            with self.assertRaisesRegex(ValueError, "pending"):
                self.review()
            checker.assert_not_called()
        self.assert_no_attestation()

    def test_expected_revision_is_checked_before_http(self):
        with patch("app.corrections.check_publication_link") as checker:
            with self.assertRaisesRegex(ValueError, "изменён"):
                self.review(expected_revision=self.revision - 1)
            checker.assert_not_called()

    def test_changed_pending_body_during_http_cannot_be_attested(self):
        def checking(*args, **kwargs):
            self.peer.execute("UPDATE drafts SET body='Another editor changed the text' WHERE id=?", (self.draft_id,))
            self.peer.commit()
            return self.checked()

        with patch("app.corrections.check_publication_link", side_effect=checking):
            with self.assertRaisesRegex(ValueError, "изменён"):
                self.review(expected_revision=self.revision)
        self.assertEqual(self.draft()["body"], "Another editor changed the text")
        self.assert_no_attestation()

    def test_approval_during_http_cannot_receive_attestation(self):
        def checking(*args, **kwargs):
            self.peer.execute("UPDATE drafts SET status='approved' WHERE id=?", (self.draft_id,))
            self.peer.commit()
            return self.checked()

        with patch("app.corrections.check_publication_link", side_effect=checking):
            with self.assertRaisesRegex(ValueError, "pending"):
                self.review()
        self.assertEqual(self.draft()["status"], "approved")
        self.assert_no_attestation()

    def test_new_unknown_publish_attempt_during_http_blocks_attestation(self):
        def checking(*args, **kwargs):
            self.peer.execute(
                "INSERT INTO publish_attempts(draft_id,attempt_no,state,created_at) VALUES(?,1,'unknown',?)",
                (self.draft_id, datetime.now(timezone.utc).isoformat()),
            )
            self.peer.commit()
            return self.checked()

        with patch("app.corrections.check_publication_link", side_effect=checking):
            with self.assertRaisesRegex(ValueError, "незавершённая"):
                self.review()
        self.assert_no_attestation()

    def test_failed_legacy_check_does_not_persist_partial_recovery(self):
        self.db.execute("UPDATE drafts SET correction_reason='',correction_text='',correction_source_id='',"
                        "correction_source_url='',correction_original_url='' WHERE id=?", (self.draft_id,))
        self.db.commit()
        revision = self.draft()["revision"]
        with patch("app.corrections.check_publication_link", side_effect=ValueError("HTTP 404")):
            with self.assertRaisesRegex(ValueError, "404"):
                self.review()
        draft = self.draft()
        self.assertEqual(draft["correction_source_id"], "")
        self.assertEqual(draft["revision"], revision)
        self.assert_no_attestation()

    def test_audit_failure_rolls_back_evidence_and_revision(self):
        with patch("app.corrections.check_publication_link", side_effect=self.checked), \
                patch("app.review_queue._audit", side_effect=RuntimeError("audit unavailable")):
            with self.assertRaisesRegex(RuntimeError, "audit unavailable"):
                self.review()
        self.assertEqual(self.draft()["revision"], self.revision)
        self.assert_no_attestation()


if __name__ == "__main__":
    unittest.main()
