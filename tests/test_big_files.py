"""Offline tests: documents too big to attach are read from their own address."""
import os, sys, unittest

os.environ.setdefault("AIRTABLE_TOKEN", "x"); os.environ.setdefault("AIRTABLE_BASE", "x")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import build_fulltext as FT  # noqa: E402
import build_cover_text as CT  # noqa: E402

URL = "https://www.clpgroup.com/x/3-3-financial-reports/2025/e_2025%20Annual%20Report.pdf"


class BigFiles(unittest.TestCase):
    def test_cover_reads_the_address_when_there_is_no_attachment(self):
        self.assertEqual(CT.attachment_url({"fields": {"document_url": URL}}), URL)
        self.assertEqual(CT.attachment_url({"fields": {"file": [{"url": "https://dl/a.pdf"}], "document_url": URL}}),
                         "https://dl/a.pdf")

    def test_fulltext_keeps_too_big_rows_and_downloads_from_the_address(self):
        rec = {"id": "recX", "fields": {"library_id": "PT_00440-abc", "document_url": URL, "file_status": "too_big",
                                        "source_type_check": "Annual Report"}}
        d = FT.row_doc(rec)
        self.assertIsNotNone(d)
        self.assertEqual(d["att"], "")
        FT.fresh_attachments = lambda ids: {}
        task = FT.tasks_for([d], 150)[0]
        self.assertEqual(task["url"], URL)

    def test_rows_without_file_or_too_big_are_still_skipped(self):
        self.assertIsNone(FT.row_doc({"id": "recY", "fields": {"library_id": "L", "document_url": URL}}))
        self.assertIsNone(FT.row_doc({"id": "recZ", "fields": {"library_id": "L", "file_status": "blocked",
                                                                "document_url": URL}}))


if __name__ == "__main__":
    unittest.main()
