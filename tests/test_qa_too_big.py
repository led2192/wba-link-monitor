"""A file over the upload limit whose text was read from its link is not a fetch failure (23 Nov 2026).

Real cases from the weekly review of 23 Nov 2026: Gold Fields' 2025 annual financial statements and
PACCAR's 2025 annual report were open High DOC_FETCH_FAIL too_big rows, yet their text had been read
from the link (cover_status ok), so both documents were usable. A too_big file never read stays a failure.
"""
import datetime as dt
import os
import sys
import unittest

os.environ.setdefault("QA_STRICT", "true")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import qa_sync

TODAY = dt.date(2026, 11, 23)
BASE = {"wba_id": "PT_00001", "company_name": "Gold Fields", "_id": "recX", "created_time": "2026-11-19T01:49:25.000Z",
        "doc_language": "English", "source_type_check": "Annual Report", "year": 2025}


def fetch_rows(f):
    return [(c, d, s) for c, d, k, s, _ in qa_sync.derive_doc(f, TODAY)[0] if c == "DOC_FETCH_FAIL"]


class TooBigReadFromLink(unittest.TestCase):
    def test_read_from_link_is_counted_not_failed(self):
        for lid, url in (("PT_00001-afs25", "https://www.goldfields.com/pdf/investors/integrated-annual-reports/2025/afs-2025.pdf"),
                         ("PT_00002-ar25", "https://www.paccar.com/media/hr1cr1xy/2025-annual-report-final.pdf")):
            f = dict(BASE, library_id=lid, document_url=url, file_status="too_big", cover_status="ok")
            self.assertEqual(fetch_rows(f), [])
            self.assertEqual(qa_sync.derive_doc(f, TODAY)[3]["too_big_read_from_link"], 1)

    def test_too_big_not_read_stays_a_failure(self):
        f = dict(BASE, library_id="PT_00003-x", document_url="https://www.bankmandiri.co.id/sr-mandiri-2024.pdf",
                 file_status="too_big", cover_status="download_fail")
        self.assertEqual(fetch_rows(f), [("DOC_FETCH_FAIL", "too_big", "High")])
        f = dict(f, cover_status="")
        self.assertEqual(fetch_rows(f), [("DOC_FETCH_FAIL", "too_big", "High")])

    def test_other_fetch_failures_unchanged(self):
        f = dict(BASE, library_id="PT_00004-y", document_url="https://www.ferrari.com/annual-report-2024.pdf",
                 file_status="blocked", cover_status="ok")
        self.assertEqual(fetch_rows(f), [("DOC_FETCH_FAIL", "blocked", "High")])


if __name__ == "__main__":
    unittest.main()
