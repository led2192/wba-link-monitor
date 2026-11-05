import collections
import os
import unittest

os.environ.setdefault("AIRTABLE_TOKEN", "test")
os.environ.setdefault("AIRTABLE_BASE", "appTEST")

import pack_eval as P


class Pack(unittest.TestCase):
    def test_evidence_runs_of_six_words(self):
        ev = "The Sustainability Committee of the Board oversees the climate strategy every year"
        self.assertTrue(P.holds("... the sustainability committee of the board oversees the plan", P.grams(ev)))
        self.assertFalse(P.holds("the committee meets", P.grams(ev)))
        self.assertFalse(P.holds("anything", P.grams("too short")))

    def test_pick_spreads_over_pages(self):
        pas = [("D", 1, 0, "a"), ("D", 1, 1, "b"), ("D", 1, 2, "c"), ("D", 2, 0, "d"), ("E", 7, 0, "e")]
        got = P.pick(pas, [5, 4, 3, 2, 1], k=4, per_page=2)
        self.assertEqual(got, [0, 1, 3, 4])                 # the third passage of page 1 waits its turn
        self.assertEqual(P.pick(pas, [0, 0, 0, 0, 0]), [])  # nothing scores: nothing sent

    def test_cut_keeps_pages(self):
        body = " ".join(f"Sentence number {i} about the board and climate." for i in range(60))
        pas = P.cut([("D", 4, body)])
        self.assertGreater(len(pas), 1)
        self.assertTrue(all(p[0] == "D" and p[1] == 4 for p in pas))

    def test_number_passages_keep_table_rows(self):
        body = "Greenhouse gas emissions\n" + "\n".join(f"Scope {i} emissions tCO2e 12,30{i} 11,90{i}" for i in (1, 2, 3)) + "\n" + "Some prose " * 80
        pas = P.cut([("D", 4, body)], "number")
        self.assertTrue(any("Scope 1 emissions tCO2e 12,301 11,901" in p[3].splitlines() for p in pas))

    def test_report_totals(self):
        s = collections.Counter({"kind narrative": 1, "answers": 10, "today": 5, "pack": 7, "either": 8,
                                 "today words": 24000, "pack words": 18000, "today pages": 60, "pack pages": 120,
                                 "today docs": 20, "pack docs": 30})
        class A: pack, per_page = 15, 2
        md = "\n".join(P.report({"GMC_01930.1": s}, A))
        self.assertIn("| GMC_01930.1 | narrative | 10 | 50% | 70% | 80% | 2,400 / 1,800 | 6 / 12 | 2 / 3 |", md)


if __name__ == "__main__":
    unittest.main()
