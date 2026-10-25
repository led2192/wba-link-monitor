import os
import unittest

os.environ.setdefault("AIRTABLE_TOKEN", "test")
os.environ.setdefault("AIRTABLE_BASE", "appTEST")

import fix_wrong_attachments as F


class Verdict(unittest.TestCase):
    def test_adb_home_page_stored_as_the_pdf(self):
        # 25 Oct 2026: 79 ADB rows held the same 215 KB copy of adb.org's home page
        f = {"document_url": "https://www.adb.org/sites/default/files/institutional-document/971966/adb-sustainability-report-2024.pdf",
             "file": [{"filename": "adb-sustainability-report-2024.pdf", "size": 215327, "type": "text/html"}]}
        self.assertEqual(F.verdict(f), "web_page")

    def test_real_files_of_other_kinds_are_left_alone(self):
        self.assertEqual(F.verdict({"document_url": "https://x.com/a.xlsx",
                                    "file": [{"filename": "a.xlsx", "type": "application/binary"}]}), "other_file")
        self.assertEqual(F.verdict({"document_url": "https://x.com/report-page",
                                    "file": [{"filename": "page.html", "type": "text/html"}]}), "other_file")

    def test_json_errors_and_images_at_a_pdf_address(self):
        for kind in ("application/json", "image/jpeg", "text/plain"):
            self.assertEqual(F.verdict({"document_url": "https://x.com/report.pdf", "file": [{"type": kind}]}), "web_page")

    def test_note(self):
        self.assertIn("210 KB of HTML", F.note_for("2026-10-25", 215327))


if __name__ == "__main__":
    unittest.main()
