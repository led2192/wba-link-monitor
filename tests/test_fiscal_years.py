"""Offline tests for fiscal_year.py (the reader) and fiscal_years.py (the job): no network.

Run:  python3 -m unittest tests/test_fiscal_years.py -v
"""
import gzip, json, os, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fiscal_year as F, fiscal_years as FS  # noqa: E402


class Reader(unittest.TestCase):
    CASES = {
        "image cover, statements say 2021": ([(1, "3 STEERING THE MOMENTUM 2 This annual report has been prepared by Telecom Egypt."),
            (80, "Consolidated statement of profit or loss for the year ended 31 December 2021 2021 2020"),
            (81, "statement of financial position as at 31 December 2021"),
            (82, "for the year ended 31 December 2021 compared with the year ended 31 December 2020")], 2021),
        "scope beats FY label": ([(2, "Reporting Period: April 1, 2023 to March 31, 2024 (FY2023)"), (30, "FY2023 FY2023 FY2023 FY2023")], 2024),
        "title 2025 but covers 2024": ([(1, "Sustainability Report 2025"), (2, "This report covers the period from 1 January 2024 to 31 December 2024.")], 2024),
        "Spanish": ([(10, "ejercicio terminado el 31 de diciembre de 2023")], 2023),
        "Chinese": ([(1, "2024年年度报告"), (5, "截至2024年12月31日止年度")], 2024),
        "fiscal span": ([(1, "Annual Report 2024/25"), (9, "year ended 31 March 2025")], 2025),
        "the report covers": ([(3, "The Report covers activities from January 1 to December 31, 2023.")], 2023),
        "information in this report refers to": ([(2, "The information in this report refers to JBS operations from January 1, 2024, to December 31, 2024")], 2024),
        "fiscal year that ended": ([(2, "This covers the fiscal year that ended February 3, 2024.")], 2024),
        "maturity date only": ([(1, "Notes due 28 June 2028 issued 2 May 2023")], None),
    }

    def test_cases(self):
        for name, (pages, want) in self.CASES.items():
            with self.subTest(name):
                self.assertEqual(F.decide(pages)[0], want)

    def test_future_period_is_capped(self):
        pages = [(3, "for the year ended 31 December 2024"), (90, "auditor for the fiscal year ending December 31, 2025")]
        self.assertEqual(F.decide(pages, (2025, 6))[0], 2024)

    def test_evidence_and_period_end(self):
        y, end, score, quote, det = F.decide(self.CASES["image cover, statements say 2021"][0])
        self.assertEqual(end, "2021-12")
        self.assertTrue(quote.startswith("p.80"))
        self.assertEqual(det["tier"], "period")

    def test_first_pages_only(self):
        # documents without full text are read from cover_text as one page
        cover = ("Sustainability Report 2024 About this report This report covers the period from "
                 "1 January 2024 to 31 December 2024 unless stated otherwise.")
        y, end, score, quote, det = F.decide([(1, cover)])
        self.assertEqual((y, det["tier"]), (2024, "scope"))
        self.assertGreaterEqual(score, 60)


class Job(unittest.TestCase):
    def test_caps(self):
        self.assertEqual(FS.cap_for({"document_url": "https://x.com/uploads/2023/05/a.pdf", "first_seen": "2026-06-25"}), (2023, 12))
        self.assertEqual(FS.cap_for({"document_url": "https://x.com/a.pdf", "first_seen": "2026-06-25"}), (2026, 6))

    def test_read_part(self):
        d = tempfile.mkdtemp(); p = os.path.join(d, "pages-x.jsonl.gz")
        with gzip.open(p, "wt") as fh:
            for lid, pg, t in [("A", 1, "Annual Report 2022"), ("A", 5, "year ended 31 December 2022"),
                               ("B", 1, "Annual Report 2019"), ("C", 3, "year ended 30 June 2020")]:
                fh.write(json.dumps({"id": lid, "p": pg, "t": t}) + "\n")
        out = FS.read_part((p, {"A": None, "C": (2020, 12)}))
        self.assertEqual(set(out), {"A", "C"})
        self.assertEqual((out["A"][0], out["C"][0]), (2022, 2020))

    def test_ai_evidence_check(self):
        ok = '2024 | 2024-02 | high | "This covers the fiscal year that ended February 3, 2024."'
        self.assertTrue(FS.ai_verified(ok, "Intro. This covers the fiscal year that ended February 3, 2024. More"))
        self.assertFalse(FS.ai_verified(ok, "a different document from 2024"))
        self.assertFalse(FS.ai_verified('2023 | 2023 | high | "This covers the fiscal year that ended February 3, 2024."',
                                        "This covers the fiscal year that ended February 3, 2024."))
        self.assertFalse(FS.ai_verified("NONE | | low | no period stated", "anything"))

    def test_cover_rows_streams_pages(self):
        import monitor_core
        pages = [{"records": [{"id": "r1", "fields": {"library_id": "A", "cover_text": "x"}}], "offset": "o"},
                 {"records": [{"id": "r2", "fields": {"library_id": "B", "cover_text": "y"}}]}]
        calls = []

        class J:
            def __init__(s, d): s.d = d
            def json(s): return s.d
        old = monitor_core.airtable_request
        monitor_core.airtable_request = lambda m, u, h, payload=None, params=None: calls.append(params) or J(pages[len(calls) - 1])
        try:
            got = [r["fields"]["library_id"] for r in FS.cover_rows("t", "b", False)]
        finally:
            monitor_core.airtable_request = old
        self.assertEqual(got, ["A", "B"])
        self.assertIn(("offset", "o"), calls[1])


if __name__ == "__main__":
    unittest.main()
