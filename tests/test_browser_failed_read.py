"""What a failed browser read writes (23 Nov 2026).

Real cases from the weekly review of 23 Nov 2026: 1,924 open PAGE_UNREACHABLE rows had the detail "200",
the code of an earlier good read left in http_status. About 200 of the High ones were files watched as
pages (Volkswagen's diversity report download, Punjab National Bank's downloadprocess.aspx): Chromium
raises "Download is starting" on them every night.
"""
import os
import sys
import types
import unittest

os.environ.setdefault("AIRTABLE_TOKEN", "test")
os.environ.setdefault("AIRTABLE_BASE", "appTEST")
os.environ.setdefault("AIRTABLE_TABLE", "monitored_links")
os.environ.setdefault("QA_STRICT", "true")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if "playwright" not in sys.modules:
    pw = types.ModuleType("playwright"); sync = types.ModuleType("playwright.sync_api")
    sync.sync_playwright = lambda: None
    sys.modules["playwright"] = pw; sys.modules["playwright.sync_api"] = sync

import playwright_monitor as P  # noqa: E402
import qa_sync  # noqa: E402

VW = "https://www.volkswagen-group.com/en/publications/more/diversity-1861/download?disposition=attachment"
DOWNLOAD = Exception("Page.goto: Download is starting\nCall log:\n  - navigating to \"%s\"" % VW)


class FailedRead(unittest.TestCase):
    def test_a_failure_clears_the_old_code(self):
        upd = P.failed_read("https://www.lvmh.com/en/press/key-documents",
                            Exception("Timeout 15000ms exceeded."), head=lambda u: self.fail("not a download"))
        self.assertEqual(upd, {P.F_STATUS: "error", P.F_HTTP: None})

    def test_a_pdf_download_is_a_document(self):
        upd = P.failed_read(VW, DOWNLOAD, head=lambda u: (200, b"%PDF-1.7"))
        self.assertEqual(upd, {P.F_STATUS: "ok", P.F_HTTP: 200, P.F_IS_PDF: True})

    def test_a_download_that_is_not_a_pdf_stays_a_failure(self):
        zip_url = "https://metinvestholding.com/Content/CmsFile/en/investorskit__FY2022_package.zip"
        self.assertEqual(P.failed_read(zip_url, DOWNLOAD, head=lambda u: (200, b"PK\x03\x04")),
                         {P.F_STATUS: "error", P.F_HTTP: None})
        self.assertEqual(P.failed_read(VW, DOWNLOAD, head=lambda u: (403, b"")),
                         {P.F_STATUS: "error", P.F_HTTP: None})

        def broken(u):
            raise RuntimeError("closed")
        self.assertEqual(P.failed_read(VW, DOWNLOAD, head=broken), {P.F_STATUS: "error", P.F_HTTP: None})


class QaSyncSeesIt(unittest.TestCase):
    BASE = {"monitor": True, "type": "sustainability_report", "url": VW, "wba_id": "PT_02010",
            "company_name": "Volkswagen", "_id": "recA2GFZeIERtZlnb"}

    def test_cleared_code_reads_no_response(self):
        out = qa_sync.derive_page(dict(self.BASE, status="error", http_status=None))
        self.assertEqual([(c, d) for c, d, k, s, _ in out], [("PAGE_UNREACHABLE", "no_response")])

    def test_a_file_row_is_neither_unreachable_nor_empty(self):
        out = qa_sync.derive_page(dict(self.BASE, status="ok", http_status=200, is_pdf=True,
                                       possible_issue="No document links found on the page"))
        self.assertEqual(out, [])
        out = qa_sync.derive_page(dict(self.BASE, status="ok", http_status=200,
                                       possible_issue="No document links found on the page"))
        self.assertEqual([c for c, *_ in out], ["PAGE_EMPTY"])


if __name__ == "__main__":
    unittest.main()
