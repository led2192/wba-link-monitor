"""Offline tests for regulator_filings.py: filing rows are web documents, old rows are relabelled."""
import os, sys, unittest

os.environ.setdefault("AIRTABLE_TOKEN", "x"); os.environ.setdefault("AIRTABLE_BASE", "x")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import regulator_filings as R  # noqa: E402

SEC = "https://www.sec.gov/Archives/edgar/data/1413447/000141344725000010/nxpi-20241231.htm"


class Filings(unittest.TestCase):
    def test_a_filing_row_is_a_web_document(self):
        comp = {"fields": {"wba_id": "PT_9", "company_name": "NXP"}}
        rows, notes = R.rows_for(comp, [(2024, "Form 10-K", SEC, "2025-02-20")], set(), set(), "SEC")
        self.assertEqual(rows[0]["file_status"], "web_document")
        self.assertEqual(rows[0]["doc_year"], "2024")
        rows, _ = R.rows_for(comp, [(2024, "Form 10-K", SEC, "2025-02-20")], {2024}, set(), "SEC")
        self.assertEqual(rows, [])                                                   # the year is held: gaps only

    def test_mark_web_relabels_only_filings(self):
        got, sent = [{"id": "rec1", "fields": {"document_url": SEC}},
                     {"id": "rec2", "fields": {"document_url": "https://www.sec.gov/ix?doc=/Archives/edgar/data/1/2/x.htm"}}], []
        real_get, real_write = R.at_get, R.at_write
        R.at_get = lambda table, fields, formula=None: got
        R.at_write = lambda table, method, records: sent.extend(records) or records
        try:
            R.mark_web(False); self.assertEqual(sent, [])                            # dry run writes nothing
            R.mark_web(True)
        finally:
            R.at_get, R.at_write = real_get, real_write
        self.assertEqual(sent, [{"id": "rec1", "fields": {"file_status": "web_document"}}])


if __name__ == "__main__":
    unittest.main()
