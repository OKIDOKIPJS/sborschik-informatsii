"""Optional test against the real multilingual embedding model.

Run explicitly with RUN_MODEL_INTEGRATION=1. Unlike test_pipeline.py, this
test may download model weights on its first run.
"""
from __future__ import annotations

import os
import unittest

from app.draft_generator import _event_groups


def make_row(title: str) -> dict[str, str]:
    return {
        "title": title,
        "summary": title,
        "source_level": "primary",
        "published_at": "2026-10-18T10:00:00+00:00",
    }


class RealModelIntegrationTests(unittest.TestCase):
    @unittest.skipUnless(
        os.environ.get("RUN_MODEL_INTEGRATION") == "1",
        "optional: set RUN_MODEL_INTEGRATION=1 to load/download the real model",
    )
    def test_multilingual_model_groups_paraphrase_not_unrelated_story(self) -> None:
        rows = [
            make_row("Муниципалитет закрыл мост после сильного паводка в городе"),
            make_row("В городе из-за паводка ограничили движение по мосту"),
            make_row("Банк оставил процентную ставку без изменений"),
        ]
        groups = _event_groups(rows)
        self.assertEqual(
            [[row["title"] for row in group] for group in groups],
            [[rows[0]["title"], rows[1]["title"]], [rows[2]["title"]]],
        )


if __name__ == "__main__":
    unittest.main()
