"""fact_classify_trial.py: what the model is told, how its reply is read, and the numbers printed. No model, no network."""
import os, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fact_classify_trial as T

METRICS = [("scope_1", "Scope 1 emissions", "Climate", "Sustainability"), ("revenue", "Revenue", "Results", "Finance")]
KEYS = [m[0] for m in METRICS]


class Told(unittest.TestCase):
    def test_the_instructions_list_every_metric_and_role(self):
        text = T.instructions(METRICS)
        self.assertIn("scope_1: Scope 1 emissions (Climate)", text); self.assertIn("revenue: Revenue (Results)", text)
        for role in T.ROLES:
            self.assertIn(role, text)

    def test_a_fact_is_shown_with_its_content_cut_short(self):
        self.assertEqual(T.describe((1, "Scope 1", "number", 1427, "t CO2", None, "Amadeus", None)), "Company: Amadeus\nData point: Scope 1\nKind: number\nContent: 1427 t CO2")
        text = T.describe((2, "Policy", "text", None, None, "x" * 500, None, None))
        self.assertIn("Company: unknown", text); self.assertEqual(len(text.split("Content: ")[1]), 160)

    def test_the_reply_may_only_name_a_listed_metric_or_none(self):
        s = T.schema(KEYS)
        self.assertEqual(s["properties"]["metric"]["enum"], ["scope_1", "revenue", "none"]); self.assertEqual(s["properties"]["role"]["enum"], list(T.ROLES))


class Read(unittest.TestCase):
    def test_a_reply_as_asked(self):
        self.assertEqual(T.parse_reply('{"metric": "scope_1", "role": "restated", "name": ""}', KEYS), ("scope_1", "restated", ""))
        self.assertEqual(T.parse_reply('Here: {"metric": "none", "role": "explanation", "name": "Reason for restatement"} done', KEYS), (None, "explanation", "Reason for restatement"))

    def test_a_reply_not_as_asked_counts_as_no_answer(self):
        for bad in ("", "scope_1", '{"metric": "scope_9", "role": "value", "name": ""}', '{"metric": "scope_1", "role": "maybe", "name": ""}', '{"metric": "scope_1"'):
            self.assertEqual(T.parse_reply(bad, KEYS), (None, None, None), bad)


class Numbers(unittest.TestCase):
    def test_agreement_is_counted_on_the_facts_the_name_matching_knew(self):
        rows = [("scope_1", "scope_1", "value", 4.0), ("scope_1", "revenue", "value", 6.0), ("revenue", None, "change", 5.0), (None, "revenue", "value", 9.0), (None, None, None, 30.0)]
        s = T.summarise(rows)
        self.assertEqual((s["facts"], s["answered"], s["known"], s["agree"], s["other_metric"], s["with_metric"]), (5, 4, 3, 1, 1, 3))
        self.assertEqual((s["roles"]["value"], s["roles"]["change"], s["seconds"], s["slowest"]), (3, 1, 6.0, 30.0))
        text = T.line("m", s, 12.0)
        self.assertIn("| m | 5 | 4 (80%) | 1 of 3 (33%) | 1 | 3 (75%) |", text)

    def test_nothing_of_a_fact_is_printed_and_only_official_apache_files_are_tried(self):
        src = open(T.__file__, encoding="utf-8").read()
        self.assertNotIn("print(describe", src); self.assertNotIn("print(fact", src)
        for repo, file, _ in T.MODELS.values():
            self.assertTrue(repo.startswith(("Qwen/", "ggml-org/")), repo); self.assertTrue(file.endswith(".gguf"))
        wf = open(os.path.join(os.path.dirname(T.__file__), ".github", "workflows", "fact_classify_trial.yml"), encoding="utf-8").read()
        self.assertIn("/srv/lantern/venv-llm", wf); self.assertNotIn("/srv/lantern/venv/bin/pip install", wf)      # never the other jobs' environment


if __name__ == "__main__":
    unittest.main()
