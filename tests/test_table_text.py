"""Offline tests for table_text.py and extract_eval --page-text: a PDF made on the spot, no network."""
import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("AIRTABLE_TOKEN", "test")
os.environ.setdefault("AIRTABLE_BASE", "appTEST")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import extract_eval as E  # noqa: E402
import table_text as T    # noqa: E402


def table_pdf():
    import pymupdf
    doc = pymupdf.open(); p = doc.new_page(); y = 72
    for r in [["Emissions (tCO2e)", "2024", "2023"], ["Scope 1", "18,981", "17,402"], ["Scope 2 (market)", "5,210", "6,003"]]:
        x = 72
        for c in r:
            p.insert_text((x, y), c); x += 150
        p.draw_line((72, y + 4), (520, y + 4)); y += 20
    path = os.path.join(tempfile.mkdtemp(), "t.pdf"); doc.save(path)
    return path


class PageText(unittest.TestCase):
    def tearDown(self):
        E.PAGE_TEXT = "db"
        T.STATS.clear()

    def test_db_keeps_the_database_text(self):
        self.assertEqual(E.read_text("L1", 3, "plain"), "plain")

    def test_a_failed_page_keeps_the_database_text(self):
        E.PAGE_TEXT = "pymupdf"
        with mock.patch.object(T, "pdf_path", return_value=None):
            self.assertEqual(E.read_text("L1", 3, "plain"), "plain")

    @unittest.skipUnless(__import__("importlib").util.find_spec("pymupdf4llm"), "pymupdf4llm not installed")
    def test_pymupdf_keeps_the_table(self):
        E.PAGE_TEXT = "pymupdf"
        with mock.patch.object(T, "pdf_path", return_value=table_pdf()):
            md = E.read_text("L1", 1, "plain")
        self.assertIn("|Scope 1|18,981|17,402|", md.replace(" |", "|").replace("| ", "|"))
        self.assertEqual(T.STATS.get("pages parsed"), 1)
        self.assertGreaterEqual(T.STATS.get("tables", 0), 1)

    def test_a_parser_crash_is_counted(self):
        E.PAGE_TEXT = "pymupdf"
        with mock.patch.object(T, "pdf_path", return_value="/x.pdf"), mock.patch.dict(T.PARSERS, {"pymupdf": mock.Mock(side_effect=RuntimeError)}):
            self.assertEqual(E.read_text("L1", 1, "plain"), "plain")
        self.assertEqual(T.STATS.get("parse failed"), 1)


if __name__ == "__main__":
    unittest.main()
