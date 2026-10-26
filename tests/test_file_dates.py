import datetime as dt
import io
import os
import unittest

os.environ.setdefault("AIRTABLE_TOKEN", "test")
os.environ.setdefault("AIRTABLE_BASE", "appTEST")

import file_dates as F

D = dt.date


def pdf_with(info):
    from pypdf import PdfWriter
    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    if info:
        w.add_metadata(info)
    b = io.BytesIO()
    w.write(b)
    return b.getvalue()


class Dates(unittest.TestCase):
    def test_pdf_date_strings(self):
        self.assertEqual(F.pdf_date("D:20250312103000+01'00'"), D(2025, 3, 12))
        self.assertEqual(F.pdf_date("D:202503"), D(2025, 3, 1))
        self.assertEqual(F.pdf_date(dt.datetime(2026, 6, 2, 9, 0)), D(2026, 6, 2))
        self.assertIsNone(F.pdf_date("D:19700101000000Z"))          # clock reset, not a date
        self.assertIsNone(F.pdf_date("D:20991231"))                 # in the future
        self.assertIsNone(F.pdf_date("garbage"))
        self.assertIsNone(F.pdf_date(None))

    def test_read_from_document_information(self):
        data = pdf_with({"/CreationDate": "D:20260528120000+01'00'", "/ModDate": "D:20260601080000Z"})
        self.assertEqual(F.read_dates(data), (D(2026, 5, 28), D(2026, 6, 1), "pdf info"))

    def test_no_dates(self):
        data = pdf_with({"/Title": "Report"})
        self.assertEqual(F.read_dates(data), (None, None, "none"))


if __name__ == "__main__":
    unittest.main()
