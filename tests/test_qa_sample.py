"""Offline tests for qa_sample.py (pure helpers). Run: QA_STRICT=true python3 -m unittest tests/test_qa_sample.py"""
import json, os, random, sys, unittest

os.environ["QA_STRICT"] = "true"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import qa_sample as S  # noqa: E402


class SampleTests(unittest.TestCase):
    def test_split_is_stable_and_roughly_70_15_15(self):
        self.assertEqual(S.split_for("x|document|2026-10-22"), S.split_for("x|document|2026-10-22"))
        c = {"eval": 0, "fewshot": 0, "regression": 0}
        for i in range(4000):
            c[S.split_for(f"PT_{i}-abc|document|2026-10-22")] += 1
        self.assertTrue(2600 < c["eval"] < 3000 and 450 < c["fewshot"] < 750 and 450 < c["regression"] < 750, c)

    def test_pair_parsing(self):
        self.assertEqual(S.pair_of("URL says Integrated Report; AI says Sustainability Report"),
                         ("Integrated Report", "Sustainability Report"))
        self.assertEqual(S.pair_of("garbage"), ("", ""))

    def test_least_labelled_pair_first_and_round_robin(self):
        f = lambda lid, a, b: {"library_id": lid, "record_id": "rec" + lid, "message": f"URL says {a}; AI says {b}"}
        fails = [f(f"A{i}", "Annual Report", "Statutory Documents") for i in range(5)] + \
                [f(f"B{i}", "Integrated Report", "Sustainability Report") for i in range(5)] + \
                [f("C0", "Code of Conduct", "Policy Documents")]
        labelled = {("Integrated Report", "Sustainability Report"): 10, ("Annual Report", "Statutory Documents"): 2}
        out = S.pick_disagreements(fails, labelled, 4, random.Random(1), exclude={"A0"})
        pairs = [S.pair_of(x["message"]) for x in out]
        self.assertEqual(pairs[0], ("Code of Conduct", "Policy Documents"))       # never labelled
        self.assertEqual(pairs[1], ("Annual Report", "Statutory Documents"))
        self.assertEqual(pairs[2], ("Integrated Report", "Sustainability Report"))
        self.assertNotIn("A0", [x["library_id"] for x in out])
        self.assertEqual(len(out), 4)

    def test_card_hides_values_on_controls(self):
        doc = {"_id": "recX", "library_id": "PT_1-abc", "source_type": "Annual Report",
               "source_type_check": "Annual Report", "year": 2024, "year_source": 3, "year_score": 85,
               "source_title": {"state": "generated", "value": "Annual Report 2024"}, "cover_text": "x" * 900}
        lid, row = S.card(doc, "agreement_control", "2026-10-22")
        self.assertEqual(lid, "PT_1-abc|document|2026-10-22")
        self.assertNotIn("robot_value", row); self.assertNotIn("ai_value", row)
        self.assertEqual(json.loads(row["hidden_values"])["effective"], "Annual Report")
        self.assertEqual(row["year_value"], "2024 (source 3, score 85)")
        self.assertEqual(row["title_value"], "Annual Report 2024")
        self.assertEqual(len(row["cover_snippet"]), 400)
        lid, row = S.card(doc, "high_stakes_disagreement", "2026-10-22")
        self.assertEqual((row["robot_value"], row["ai_value"]), ("Annual Report", "Annual Report"))
        self.assertNotIn("hidden_values", row)


if __name__ == "__main__":
    unittest.main()
