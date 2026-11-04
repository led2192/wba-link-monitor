import os
import unittest

os.environ.setdefault("AIRTABLE_TOKEN", "test")
os.environ.setdefault("AIRTABLE_BASE", "appTEST")

import anchor_eval as A


class LinkText(unittest.TestCase):
    def test_type_and_year_from_link_text(self):
        self.assertEqual(A.text_type("Sustainability Report 2023 (PDF, 12 MB)"), "Sustainability Report")
        self.assertEqual(A.text_type("Annual Report 2024"), "Annual Report")
        self.assertEqual(A.text_type("Download"), "")
        self.assertEqual(A.text_year("Annual Report 2024/25"), "2024")
        self.assertEqual(A.text_year("Results 2023 and outlook 2025"), "2025")
        self.assertEqual(A.text_year("Report (1999)"), "")

    def test_new_links_parsing_keeps_the_newest_text_and_drops_generic_ones(self):
        nl = ("2026-10-01: acme.com/ar24.pdf  [Annual Report 2024] ; acme.com/x.pdf  [Download] ; acme.com/y.pdf\n"
              "2026-09-01: acme.com/ar24.pdf  [Old text]")
        self.assertEqual(A.parse_new_links(nl), {"acme.com/ar24.pdf": "Annual Report 2024"})


class Filter(unittest.TestCase):
    def test_rows_with_blank_origin_count_as_found_on_pages(self):
        # 4 Nov 2026: origin = 'discovery' alone matched no row; harvested rows from before the field are blank
        self.assertIn("{origin}=''", A.FOUND_ON_PAGES)


if __name__ == "__main__":
    unittest.main()
