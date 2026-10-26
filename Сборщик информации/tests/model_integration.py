"""Optional test against the real multilingual embedding model.

Run explicitly with RUN_MODEL_INTEGRATION=1. Unlike test_pipeline.py, this
test may download model weights on its first run.
"""
from __future__ import annotations

import os
import unittest

from app.draft_generator import DEFAULT_MODEL, _cluster_rows


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
    def test_multilingual_model_groups_frontline_paraphrases_not_hard_negatives(self) -> None:
        from sentence_transformers import SentenceTransformer

        cases = [
            ("English same event", True,
             "Fighting intensified near Pokrovsk after Russian forces advanced toward several settlements.",
             "Russia reported advances west of Pokrovsk, while fighting continued near nearby villages."),
            ("Russian same event", True,
             "Минобороны сообщило о продвижении российских войск на Покровском направлении.",
             "Российские войска продвинулись к западу от Покровска, заявило ведомство."),
            ("same city, different event", False,
             "A missile strike damaged a power substation near Kharkiv overnight.",
             "Ukrainian forces repelled a ground assault north of Kharkiv this morning."),
            ("similar drone reports, different places and targets", False,
             "Russian drones struck port infrastructure in Odesa.",
             "Ukrainian drones hit an oil depot in Belgorod."),
            ("same front, control report versus civilian evacuation", False,
             "Russian forces claimed control of a village near Kupiansk.",
             "A civilian evacuation was announced near Kupiansk after shelling."),
            ("Russian same-city hard negative", False,
             "Ракетный удар повредил электроподстанцию возле Харькова.",
             "Украинские военные сообщили об отражении наземной атаки севернее Харькова."),
            ("English same direction, different event type", False,
             "Fighting continued near Pokrovsk after an assault was repelled.",
             "An evacuation was ordered near Pokrovsk following a power outage."),
            ("same site, separate dated events", False,
             "A Russian strike hit a railway station in Kherson on Monday.",
             "A Ukrainian drone attack damaged an ammunition depot in Kherson on Thursday."),
            ("Russian same city, separate civilian and military events", False,
             "В Белгороде сообщили о повреждении жилого дома после атаки беспилотника.",
             "Российские войска продвинулись на несколько километров возле Белгорода."),
            ("Russian paraphrase of a front-line development", True,
             "На Покровском направлении продолжаются бои за несколько населённых пунктов.",
             "В районе Покровска идут столкновения вокруг ряда сёл."),
            ("cross-language paraphrase, same reported development", True,
             "Russian forces advanced near Pokrovsk, according to the ministry.",
             "Минобороны сообщило о продвижении российских войск возле Покровска."),
            ("similar attack verbs, different geography", False,
             "Ukrainian forces repelled an assault near Kupiansk.",
             "Russian forces repelled an assault near Vuhledar."),
            ("shared name, unrelated diplomatic event", False,
             "Fighting was reported near Donetsk on Tuesday.",
             "Diplomats met in Donetsk to discuss a prisoner exchange."),
            ("same place and subject, contradictory control claims", False,
             "The ministry said its forces captured Novoselivske near Luhansk.",
             "Ukrainian officials said their troops still control Bilohorivka in Luhansk region."),
        ]
        texts = [text for _, _, left, right in cases for text in (left, right)]
        model_name = os.environ.get("EVENT_MATCH_MODEL", DEFAULT_MODEL)
        model = SentenceTransformer(model_name, device="cpu")
        vectors = model.encode(texts, normalize_embeddings=True, convert_to_numpy=True,
                               show_progress_bar=False)
        threshold = float(os.environ.get("EVENT_MATCH_THRESHOLD", "0.70"))

        for pair_no, (label, should_merge, left, right) in enumerate(cases):
            rows = [make_row(left), make_row(right)]
            left_vector, right_vector = vectors[pair_no * 2:pair_no * 2 + 2]
            similarity = float(left_vector @ right_vector)
            groups = _cluster_rows(rows, lambda _a, _b, score=similarity: score, threshold)
            with self.subTest(case=label, similarity=round(similarity, 3), threshold=threshold):
                self.assertEqual(len(groups) == 1, should_merge)


if __name__ == "__main__":
    unittest.main()
