"""Offline tests for citation_coverage.py: kinds, matching and the tally, no network."""
import os
import sys
import unittest

os.environ.setdefault("AIRTABLE_TOKEN", "test")
os.environ.setdefault("AIRTABLE_BASE", "appTEST")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import citation_coverage as C  # noqa: E402

LIB = C.library_index([("L1", "PT_00001", "https://co.com/reports/AR_2024_EN.pdf"),
                       ("L2", "PT_00001", "https://co.com/sustainability/"),
                       ("L3", "PT_00009", "https://shared.com/group-report-2024.pdf"),
                       ("L4", "PT_00001", "")])


class Classify(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(C.kind_of("https://x.com/a.PDF?x=1"), "PDF")
        self.assertEqual(C.kind_of("https://x.com/about"), "web page")
        self.assertEqual(C.kind_of(""), "no address")

    def test_where_a_source_is_held(self):
        self.assertEqual(C.classify("PT_00001", "http://www.co.com/reports/AR_2024_EN.pdf", LIB), ("same company, same address", "L1"))
        self.assertEqual(C.classify("PT_00001", "https://cdn.co.com/x/ar_2024_en.pdf", LIB), ("same company, same file name", "L1"))
        self.assertEqual(C.classify("PT_00001", "https://co.com/sustainability", LIB), ("same company, same address", "L2"))
        self.assertEqual(C.classify("PT_00001", "https://shared.com/group-report-2024.pdf", LIB), ("same address, another company", "L3"))
        self.assertEqual(C.classify("PT_00001", "https://co.com/other", LIB), ("not held", None))
        self.assertEqual(C.classify("PT_00001", "", LIB), ("no address", None))

    def test_tally_and_report_have_no_company_or_address(self):
        cites = [{"base": "B", "wba": "PT_00001", "year": 2024, "url": "https://co.com/reports/AR_2024_EN.pdf"},
                 {"base": "B", "wba": "PT_00001", "year": 2024, "url": "https://co.com/other"}]
        t = C.tally(cites, LIB, {"L1"})
        self.assertEqual(t["All"]["cited"], 2)
        self.assertEqual(t["All"]["held"], 1)
        self.assertEqual(t["All"]["with text"], 1)
        self.assertEqual(t["kind web page"]["not held"], 1)
        text = "\n".join(C.report(t))
        self.assertNotIn("PT_00001", text)
        self.assertNotIn("co.com", text)



class WebPages(unittest.TestCase):
    def test_where_web_citations_point(self):
        hosts = {"PT_00001": {"co.com"}}
        self.assertEqual(C.where_from("PT_00001", "https://www.co.com/esg", hosts), "the company's own site")
        self.assertEqual(C.where_from("PT_00001", "https://www.cdp.net/en/responses/1", hosts), "CDP")
        self.assertEqual(C.where_from("PT_00001", "https://x.sharepoint.com/a", hosts), "SharePoint or OneDrive")
        self.assertEqual(C.where_from("PT_00001", "https://news.example.org/a", hosts), "another site")
        self.assertEqual(C.shape_of("https://co.com"), "home page")
        self.assertEqual(C.shape_of("https://co.com/esg/policies"), "page")
        self.assertEqual(C.shape_of("https://co.com/get?id=3"), "page with parameters")
        self.assertEqual(C.shape_of("https://co.com/data.xlsx"), ".xlsx")

    def test_a_monitored_page_not_captured(self):
        lib = C.library_index([])
        lib["monitored"].add(("PT_00001", C.norm_url("https://co.com/sustainability")))
        self.assertEqual(C.classify("PT_00001", "https://co.com/sustainability/", lib), ("a monitored page, not captured", None))
        t = C.tally([{"base": "B", "wba": "PT_00001", "year": 2024, "url": "https://co.com/sustainability"}], lib, set(), {"PT_00001": {"co.com"}})
        self.assertEqual(t["web page on the company's own site"]["cited"], 1)
        self.assertNotIn("co.com", "\n".join(C.report(t)))

if __name__ == "__main__":
    unittest.main()
