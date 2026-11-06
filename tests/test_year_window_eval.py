"""Offline tests for year_window_eval.py: address matching, the learned window and the tally, with no
network and no database."""
import collections
import os
import sys
import unittest

os.environ.setdefault("AIRTABLE_TOKEN", "test")
os.environ.setdefault("AIRTABLE_BASE", "appTEST")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import year_window_eval as Y  # noqa: E402


class Urls(unittest.TestCase):
    def test_the_same_file_matches_whatever_the_scheme_or_www(self):
        self.assertEqual(Y.norm_url("https://www.Example.com/Reports/AR-2024.pdf#page=3"),
                         Y.norm_url("http://example.com/reports/ar-2024.pdf/"))
        self.assertEqual(Y.norm_url(""), "")

    def test_the_query_is_kept(self):
        self.assertNotEqual(Y.norm_url("x.com/get?id=1"), Y.norm_url("x.com/get?id=2"))


class Window(unittest.TestCase):
    def test_a_period_attribute(self):
        w, share = Y.learn_window(collections.Counter({0: 90, 1: 8, -1: 2}))
        self.assertEqual(w, (0, 1))
        self.assertEqual(Y.kind_of(w), "period")

    def test_a_standing_attribute_reaches_back(self):
        w, share = Y.learn_window(collections.Counter({0: 60, -1: 30, -2: 8, 1: 2}))
        self.assertEqual(w, (-2, 0))
        self.assertEqual(Y.kind_of(w), "standing")
        self.assertGreaterEqual(share, Y.COVER)

    def test_one_year_only(self):
        w, _ = Y.learn_window(collections.Counter({0: 100}))
        self.assertEqual(w, (0, 0))
        self.assertEqual(Y.kind_of(w), "period")

    def test_too_spread_has_no_window(self):
        self.assertIsNone(Y.learn_window(collections.Counter({0: 50, -5: 50})))
        self.assertEqual(Y.kind_of(None), "unclear")


class Tally(unittest.TestCase):
    def test_offsets_use_our_years_and_the_fiscal_shift(self):
        sources = {"rec1": {"url": "https://co.com/ar2024.pdf", "year": "2024"},
                   "rec2": {"url": "https://co.com/policy.pdf", "year": "2022"},
                   "rec3": {"url": "https://other.com/x.pdf", "year": "2023"}}
        docs = {"PT_00001": {Y.norm_url("co.com/ar2024.pdf"): 2025, Y.norm_url("co.com/policy.pdf"): 2023}}
        answers = [{"wba": "PT_00001", "attribute": "GMC_1", "year": 2024, "source_ids": ["rec1", "rec2", "rec3"]}]
        shifted = lambda w, y: y + 1                       # a March year end: reporting 2024 is documents of 2025
        t = Y.tally(answers, sources, docs, shifted)["GMC_1"]
        self.assertEqual(t["c"]["cited"], 3)
        self.assertEqual(t["c"]["matched"], 2)
        self.assertEqual(t["c"]["multi"], 1)
        self.assertEqual(dict(t["off"]), {0: 1, -2: 1})
        self.assertEqual(t["c"]["in_today"], 1)
        self.assertEqual(dict(t["src"]), {-1: 1})          # not ours: the analysts' Source Year

    def test_the_report_has_no_company(self):
        sources = {f"r{i}": {"url": f"co.com/{i}.pdf", "year": "2024"} for i in range(30)}
        docs = {"PT_00002": {Y.norm_url(f"co.com/{i}.pdf"): 2024 - (i % 3 == 0) for i in range(30)}}
        answers = [{"wba": "PT_00002", "attribute": "GMC_2", "year": 2024, "source_ids": [f"r{i}"]} for i in range(30)]
        md, windows = Y.report(Y.tally(answers, sources, docs, lambda w, y: y), {"GMC_2": "A policy"}, 20)
        text = "\n".join(md)
        self.assertNotIn("PT_00002", text)
        self.assertNotIn("co.com", text)
        self.assertEqual(windows["GMC_2"]["kind"], "standing")
        self.assertEqual(windows["GMC_2"]["window"], [-1, 0])



class FileNames(unittest.TestCase):
    def test_the_same_file_in_another_folder_matches_by_name(self):
        self.assertEqual(Y.file_key("https://cdn.co.com/x/AR_2024_EN.pdf?v=2"), "ar_2024_en.pdf")
        self.assertEqual(Y.file_key("https://co.com/report.pdf"), "report.pdf")
        self.assertEqual(Y.file_key("https://co.com/a.pdf"), "")            # too short to be distinctive
        self.assertEqual(Y.file_key("https://co.com/investors/"), "")       # not a file
        sources = {"r1": {"url": "https://cdn.co.com/x/AR_2024_EN.pdf", "year": "2024"}}
        docs = {"PT_00003": {Y.norm_url("https://co.com/files/ar_2024_en.pdf"): 2024, "file:ar_2024_en.pdf": 2024}}
        t = Y.tally([{"wba": "PT_00003", "attribute": "GMC_3", "year": 2024, "source_ids": ["r1"]}], sources, docs, lambda w, y: y)["GMC_3"]
        self.assertEqual(t["c"]["matched"], 1)
        self.assertEqual(t["c"]["matched by file name"], 1)
        self.assertEqual(dict(t["sy"]), {0: 1})

if __name__ == "__main__":
    unittest.main()
