"""Exercise the public editor/operations commands against isolated runtime data."""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from app.cli import main
from app.db import connect


class EditorCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "app.sqlite3"
        self.env = patch.dict(os.environ, {"APP_DATA_DIR": str(self.root)})
        self.env.start()
        self.addCleanup(self.env.stop)
        for name in ("app.db.DB_PATH", "app.diagnostics.DB_PATH", "app.config.DB_PATH"):
            patcher = patch(name, self.path)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.network = patch("urllib.request.urlopen", side_effect=AssertionError("No network in CLI tests"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(list(args))
        return code, out.getvalue(), err.getvalue()

    def seed(self):
        db = connect(self.path)
        db.execute("INSERT INTO drafts(title,body,status,created_at,updated_at) VALUES('Тест','Текст','pending','now','now')")
        db.commit()
        db.close()

    def test_doctor_reports_invalid_config_without_creating_database(self):
        invalid = self.root / "bad.yaml"
        invalid.write_text("sources: broken", encoding="utf-8")
        with patch.dict(os.environ, {"SOURCES_FILE": str(invalid)}), patch("app.credentials.get_max_credentials", return_value=(None, None)):
            code, out, err = self.run_cli("doctor", "--json")
        self.assertEqual(code, 1)
        report = json.loads(out)
        self.assertTrue(report["offline"])
        self.assertTrue(any(c["name"] == "config" and c["status"] == "error" for c in report["checks"]))
        self.assertFalse(self.path.exists())
        self.assertEqual(err, "")

    def test_backup_verify_and_restore_via_cli_preserve_original(self):
        self.seed()
        backup = self.root / "snapshot.sqlite3"
        restored = self.root / "recovered.sqlite3"
        for args in (("backup", "create", "--output", str(backup)),
                     ("backup", "verify", str(backup)),
                     ("backup", "restore", str(backup), "--to", str(restored))):
            code, out, err = self.run_cli(*args)
            self.assertEqual(code, 0, err)
            self.assertIsInstance(json.loads(out), dict)
        self.assertTrue(restored.is_file())
        self.assertEqual(self.run_cli("backup", "restore", str(backup), "--to", str(restored))[0], 1)

    def test_edit_utf8_file_export_and_existing_export_are_safe(self):
        self.seed()
        text_file = self.root / "post.txt"
        text_file.write_text('Тест редактора: «кавычки», перенос\n<script>не исполнять</script>', encoding="utf-8-sig")
        code, _, err = self.run_cli("review", "edit", "1", "--text-file", str(text_file))
        self.assertEqual(code, 0, err)
        exported = self.root / "draft.html"
        code, _, err = self.run_cli("export", "1", "--format", "html", "--output", str(exported))
        self.assertEqual(code, 0, err)
        html = exported.read_text(encoding="utf-8")
        self.assertIn("Тест редактора", html)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertEqual(self.run_cli("export", "1", "--output", str(exported))[0], 1)
        self.assertEqual(exported.read_text(encoding="utf-8"), html)


if __name__ == "__main__":
    unittest.main()
