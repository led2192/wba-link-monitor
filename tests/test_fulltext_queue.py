"""Offline test: the full-text queue reads newest documents first, undated next, old years last."""
import os, sys, unittest

os.environ.setdefault("AIRTABLE_TOKEN", "x"); os.environ.setdefault("AIRTABLE_BASE", "x")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import build_fulltext as FT  # noqa: E402


class Queue(unittest.TestCase):
    def test_newest_first_undated_next_old_last(self):
        docs = [{"id": "c", "year": 2019}, {"id": "a", "year": 2026}, {"id": "u", "year": ""},
                {"id": "b", "year": 2026}, {"id": "d", "year": 2021}, {"id": "e", "year": 2012}]
        self.assertEqual([d["id"] for d in sorted(docs, key=FT.queue_key)], ["a", "b", "d", "u", "c", "e"])

    def test_a_regulator_filing_is_read_from_its_address(self):
        url = "https://www.sec.gov/Archives/edgar/data/50863/000005086325000009/intc-20241228.htm"
        rec = {"id": "recF", "fields": {"library_id": "PT_1-abc", "wba_id": "PT_1", "document_url": url,
                                        "file_status": "not_pdf", "source_type": "Annual Report", "year": 2024}}
        d = FT.row_doc(rec)
        self.assertTrue(d["web"]); self.assertEqual(d["url"], url)
        rec["fields"]["document_url"] = "https://www.merckgroup.com/x.html"                  # a page that is not a filing
        self.assertIsNone(FT.row_doc(rec))
        self.assertIn("sec.gov/Archives", FT.HAS_PDF)

    def test_extract_a_filing(self):
        import web_filing as W
        real = W.fetch_html
        W.fetch_html = lambda url, max_bytes, timeout=90: ("ok", "<p>" + "Net revenue 53,101 for the year. " * 20 + "</p>"
                                                                 "<div style='page-break-before:always'><p>" + "Item 8. " * 20 + "</p></div>")
        try:
            out = FT.extract_one({"id": "PT_1-abc", "rec": "recF", "url": "https://www.sec.gov/Archives/edgar/data/1/2/x.htm", "web": True})
        finally:
            W.fetch_html = real
        self.assertEqual((out["doc"]["status"], out["doc"]["pages"]), ("ok", 2))
        self.assertIn("53,101", out["pages"][0][1])


if __name__ == "__main__":
    unittest.main()
