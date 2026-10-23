"""Offline tests for blocked_probe.py (no network)."""
import os, sys, unittest

os.environ.setdefault("AIRTABLE_TOKEN", "x"); os.environ.setdefault("AIRTABLE_BASE", "x")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import blocked_probe as B  # noqa: E402


class Reached(unittest.TestCase):
    def test_document_needs_a_real_pdf(self):
        doc = {"kind": "doc"}
        self.assertTrue(B.ok(doc, 200, True, 0))
        self.assertFalse(B.ok(doc, 200, False, 0))      # a challenge page served with 200
        self.assertFalse(B.ok(doc, 403, True, 0))

    def test_page_needs_pdf_links(self):
        page = {"kind": "page"}
        self.assertTrue(B.ok(page, 200, False, 4))
        self.assertFalse(B.ok(page, 200, False, 0))
        self.assertFalse(B.ok(page, None, False, 9))

    def test_pdf_links_found_in_html(self):
        html = b'<a href="/x/report-2025.pdf">a</a> <a href=\'https://y/z.PDF?dl=1\'>b</a> <a href="/page">c</a>'
        self.assertEqual(len(set(B.PDF_LINK.findall(html))), 2)


if __name__ == "__main__":
    unittest.main()
