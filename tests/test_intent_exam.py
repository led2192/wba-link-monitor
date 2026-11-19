"""The intent exam (intent_exam.py) may only go up: the count of variants the page reads right, as measured the day
the exam was written (18 Nov 2026: the way 40 of 69, the company 67 of 69, the years 12 of 13). A change to how the
page reads a text that lowers a count fails here; one that raises it raises the floor in the same pull request."""
import os, shutil, sys, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import intent_exam as E

FLOOR = {"intent": 62, "company": 68, "years": 13}          # 19 Nov 2026 (v112, a company and a topic answered); 59 with v108; 40, 67, 12 the day before


class Scoring(unittest.TestCase):
    def test_a_field_is_counted_only_where_the_case_states_it(self):
        cases = [{"typed": "a", "intent": "fact", "company": "X", "from": 2023, "to": 2023}, {"typed": "b", "intent": "search", "typo": "Nike"}, {"typed": "c", "intent": "overview"}]
        got = [{"intent": "fact", "company": "X", "from": 2023, "to": None, "typo": None}, {"intent": "search", "company": None, "from": None, "to": None, "typo": "Nike"},
               {"intent": "search", "company": None, "from": None, "to": None, "typo": None}]
        r = E.score(cases, got)
        self.assertEqual((r["intent"], r["company"], r["years"], r["all"]), (2, [2, 2], [0, 1], 1))
        self.assertEqual(r["by_way"]["overview"], [0, 1])
        self.assertEqual(r["confusion"], {"overview read as search": 1})
        self.assertEqual([w["typed"] for w in r["wrong"]], ["a", "c"])

    def test_the_variants_are_well_formed_and_name_no_real_case_twice(self):
        import json
        data = E.load(); real = {c["typed"].lower() for c in json.load(open(os.path.join(E.HERE, "tests", "use_cases.json"), encoding="utf-8"))["cases"]}
        typed = [c["typed"].lower() for c in data["cases"]]
        self.assertGreaterEqual(len(typed), 60); self.assertEqual(len(typed), len(set(typed)))
        self.assertFalse(real & set(typed))
        for c in data["cases"]:
            self.assertIn(c["intent"], E.WAYS, c["typed"])
        for way in E.WAYS:
            self.assertGreaterEqual(sum(c["intent"] == way for c in data["cases"]), 8, way)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class NoGoingBack(unittest.TestCase):
    def test_the_page_reads_no_fewer_variants_right_than_before(self):
        data = E.load(); r = E.score(data["cases"], E.read_by_page(data))
        self.assertGreaterEqual(r["intent"], FLOOR["intent"], r["confusion"])
        self.assertGreaterEqual(r["company"][0], FLOOR["company"])
        self.assertGreaterEqual(r["years"][0], FLOOR["years"])


if __name__ == "__main__":
    unittest.main()
