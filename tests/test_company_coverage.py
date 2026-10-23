"""Offline tests for company_coverage.py: the latest document a company received.

Run:  python3 -m unittest tests/test_company_coverage.py -v
"""
import os, sys, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("AIRTABLE_TOKEN", "test")
os.environ.setdefault("AIRTABLE_BASE", "appTEST")
import company_coverage as CC  # noqa: E402


def lib(lid, created, **kw):
    return {"id": "rec" + lid, "fields": dict({"wba_id": "PT_1", "library_id": lid, "created_time": created,
                                               "file": [{"url": "x"}], "doc_label": f"2025 | Acme | Report {lid}",
                                               "source_type": "Annual Report"}, **kw)}


class LatestDocument(unittest.TestCase):
    def test_origin(self):
        self.assertEqual(CC.origin({"Reviewer notes": "Uploaded by hand (a.pdf)."}), "added by hand (file)")
        self.assertEqual(CC.origin({"Reviewer notes": "From Source Library 2025 (record rec1): ..."}),
                         "imported from the Source Library")
        self.assertEqual(CC.origin({"Reviewer notes": "Added from the SEC regulator database"}), "regulator filing")
        self.assertEqual(CC.origin({"page_type": "reports_hub"}), "found by the robot on a reports hub")
        self.assertEqual(CC.origin({}), "added by hand (link)")

    def test_text_drops_the_company(self):
        f = {"doc_label": "2025 | Acea | Consolidated Financial Statements 2025", "library_id": "PT_00015-46",
             "Reviewer notes": "Uploaded by hand (x.pdf)."}
        self.assertEqual(CC.latest_doc_text(f, "Annual Report"),
                         "2025 | Consolidated Financial Statements 2025 (Annual Report); added by hand (file); PT_00015-46")

    def test_newest_with_a_file_wins_and_discarded_rows_do_not_count(self):
        rows = [lib("a", "2026-09-01T10:00:00.000Z", page_type="reports_hub"),
                lib("b", "2026-10-20T08:00:00.000Z", file=None),                    # no file: not downloaded
                lib("c", "2026-10-21T08:00:00.000Z", discard=True),                 # discarded
                lib("d", "2026-10-15T09:30:00.000Z", **{"Reviewer notes": "Uploaded by hand (d.pdf)."})]

        def fake_sweep(table, fields, label, formula=None):
            return rows if table == CC.LIB else []
        with mock.patch.object(CC, "sweep", fake_sweep):
            agg = CC.aggregate()
        out = CC.compose(agg["PT_1"], 2024)
        self.assertEqual(out["latest_doc_added"], "2026-10-15")
        self.assertEqual(out["latest_doc"], "2025 | Report d (Annual Report); added by hand (file); d")

    def test_company_with_nothing(self):
        out = CC.compose(CC.empty_stats(), 2024)
        self.assertIsNone(out["latest_doc_added"])
        self.assertEqual(out["latest_doc"], "")


if __name__ == "__main__":
    unittest.main()
