"""Keep Windows 3.11 C-locale date formatting portable on every test host."""
from __future__ import annotations

import ast
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import test_pipeline as fixtures
from app import draft_generator
from app.db import connect
from app.review_queue import get_draft


APP_ROOT = Path(__file__).resolve().parents[1]


class AsciiOnlyDatetime(datetime):
    """Simulate the restrictive strftime boundary even on UTF-8 Python 3.12+."""

    def strftime(self, format: str) -> str:
        format.encode("ascii")
        return super().strftime(format)


class AsciiStrftimeTests(unittest.TestCase):
    def test_publication_label_keeps_russian_suffix_outside_strftime(self):
        with patch.object(draft_generator, "datetime", AsciiOnlyDatetime):
            label = draft_generator._published_label("2026-10-26T22:45:00+00:00")
        self.assertEqual(label, "27.10.2026, 01:45 МСК")

    def test_daily_draft_title_and_citations_use_ascii_strftime(self):
        current = AsciiOnlyDatetime.now(timezone.utc).replace(microsecond=0)
        with tempfile.TemporaryDirectory() as tmp, closing(connect(Path(tmp) / "draft.sqlite3")) as db:
            fixtures.insert_test_item(
                db, "A recent update", "https://example.org/date-test", current.isoformat(), current.isoformat(),
            )
            with patch.object(draft_generator, "datetime", AsciiOnlyDatetime), \
                    patch.dict(sys.modules, {"sentence_transformers": None}):
                draft_id = draft_generator.create_daily_draft(db)
            draft = get_draft(db, draft_id)
            self.assertIn(" в ", draft["title"])
            self.assertTrue(draft["title"].endswith(" МСК"))
            self.assertIn(" МСК", draft["body"])

    def test_approved_fixture_works_with_ascii_strftime(self):
        current = AsciiOnlyDatetime.now(timezone.utc).replace(microsecond=0)
        with tempfile.TemporaryDirectory() as tmp, closing(connect(Path(tmp) / "fixture.sqlite3")) as db:
            draft_id = fixtures.CollectorAndReviewTests._create_approved_digest(
                db, current - timedelta(minutes=30),
            )
            self.assertEqual(get_draft(db, draft_id)["status"], "approved")


class DateFormatPortabilityTests(unittest.TestCase):
    def test_all_literal_strftime_formats_in_application_and_tests_are_ascii(self):
        checked = 0
        for directory in (APP_ROOT / "app", APP_ROOT / "tests"):
            for path in directory.rglob("*.py"):
                tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
                for node in ast.walk(tree):
                    if (not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute)
                            or node.func.attr != "strftime" or not node.args):
                        continue
                    for literal in ast.walk(node.args[0]):
                        if isinstance(literal, ast.Constant) and isinstance(literal.value, str):
                            checked += 1
                            with self.subTest(path=path.relative_to(APP_ROOT), line=node.lineno):
                                self.assertTrue(
                                    literal.value.isascii(),
                                    "Keep non-ASCII labels outside strftime to support Windows C locale",
                                )
        self.assertGreater(checked, 0)

    def test_runtime_scenarios_pass_in_c_locale_subprocess(self):
        script = (
            "import locale,unittest; "
            "locale.setlocale(locale.LC_TIME,'C'); "
            "suite=unittest.defaultTestLoader.loadTestsFromName('test_date_formatting.AsciiStrftimeTests'); "
            "result=unittest.TextTestRunner().run(suite); "
            "raise SystemExit(0 if result.wasSuccessful() else 1)"
        )
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(APP_ROOT / "tests") + os.pathsep + str(APP_ROOT)
        environment["PYTHONIOENCODING"] = "utf-8"
        result = subprocess.run(
            [sys.executable, "-c", script], cwd=APP_ROOT, env=environment,
            capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
