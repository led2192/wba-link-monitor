"""Offline tests for content_index.py: which pages are an index, which pages its rows cite, and how a printed
page becomes a PDF page."""
import os, sys, unittest

os.environ.setdefault("AIRTABLE_TOKEN", "x"); os.environ.setdefault("AIRTABLE_BASE", "x")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import content_index as C  # noqa: E402

GRI = """GRI content index
Statement of use: Testco has reported in accordance with the GRI Standards for the period 1 January to 31 December 2024.
GRI 2: General Disclosures 2021
2-1 Organizational details 4
2-6 Activities, value chain and other business relationships 6-8
2-7 Employees 45, 112
GRI 201: Economic Performance 2016
201-1 Direct economic value generated and distributed 12
GRI 305: Emissions 2016
305-1 Direct (Scope 1) GHG emissions 84
305-2 Energy indirect (Scope 2) GHG emissions 84-86
305-3 Other indirect (Scope 3) GHG emissions 87
305-4 GHG emissions intensity 88
GRI 405: Diversity and Equal Opportunity 2016
405-1 Diversity of governance bodies and employees 46
"""
ESRS = """ESRS index
ESRS 2 GOV-1 The role of the administrative bodies 22
E1-5 Energy consumption and mix 108
E1-6 Gross Scopes 1, 2, 3 and Total GHG emissions 112-115
S1-6 Characteristics of the undertaking's employees 140
"""


class Index(unittest.TestCase):
    def test_index_pages_are_recognised(self):
        self.assertTrue(C.is_index_page(GRI)); self.assertTrue(C.is_index_page(ESRS))
        self.assertFalse(C.is_index_page("Our Scope 1 emissions fell to 4,074 tCO2e in 2024 (2023: 4,500). See page 84 for details."))
        self.assertFalse(C.is_index_page(""))

    def test_cited_pages_for_each_attribute(self):
        self.assertEqual(C.cited_printed_pages(GRI, ["305-1"], ["direct (scope 1)"]), [84])
        self.assertEqual(sorted(C.cited_printed_pages(GRI, ["305-2"], ["scope 2"])), [84, 85, 86])
        self.assertEqual(sorted(C.cited_printed_pages(GRI, ["2-7", "405-1"], ["employees"])), [45, 46, 112])
        self.assertEqual(C.cited_printed_pages(GRI, ["201-1"], ["economic value generated"]), [12])     # not the 2016 of the standard
        self.assertEqual(sorted(C.cited_printed_pages(ESRS, ["E1-6"], ["gross scopes 1, 2, 3"])), [112, 113, 114, 115])
        self.assertEqual(C.cited_printed_pages(ESRS, ["S1-6"], ["employees"]), [140])
        self.assertEqual(C.cited_printed_pages(GRI, ["E1-5"], ["taxonomy"]), [])

    def test_the_code_digits_are_never_a_page(self):
        self.assertEqual(C.cited_printed_pages("305-1 Direct (Scope 1) GHG emissions 305-2 Energy indirect 84", ["305-1"], []), [84])
        self.assertEqual(C.cited_printed_pages("Scope 1 emissions p. 84 and GRI 305-1", [], ["scope 1"]), [84])

    def test_printed_offset_from_page_numbers(self):
        samples = [(p, f"{p - 2} Testco Sustainability Report 2024\nEnvironment", "text ends here") for p in range(5, 40)]
        self.assertEqual(C.printed_offset(samples), 2)
        samples = [(p, "Testco", f"some text\n{p - 3}") for p in range(5, 40)]
        self.assertEqual(C.printed_offset(samples), 3)
        self.assertIsNone(C.printed_offset([(5, "3 Testco", "x")]))                   # one page agrees: not enough
        self.assertIsNone(C.printed_offset([(p, "Testco", "no number") for p in range(1, 20)]))

    def test_pdf_pages_for(self):
        self.assertEqual(C.pdf_pages_for(84, 2), {86: 1.0})
        self.assertEqual(C.pdf_pages_for(84, None), {84: 0.5, 85: 0.5, 86: 0.5, 87: 0.5, 88: 0.5})
        self.assertEqual(C.pdf_pages_for(84, None, page_count=85), {84: 0.5, 85: 0.5})


class Cited(unittest.TestCase):
    def test_cited_pages_of_a_document(self):
        class Cur:
            def __init__(self):
                self.sql = []
            def execute(self, sql, params=None):
                self.sql.append(sql)
            def fetchall(self):
                sql = self.sql[-1]
                if "gri | esrs" in sql:
                    return [("L1", GRI), ("L1", "Environmental performance table Scope 1 4,074 Scope 2 41,709")]
                return [(p, f"{p - 2} Testco", "end") for p in range(1, 120)]
        attr = {"index": {"codes": ["305-1"], "terms": ["direct (scope 1)"]}}
        self.assertEqual(C.cited(Cur(), ["L1"], attr), {("L1", 86): 1.0})
        self.assertEqual(C.cited(Cur(), ["L1"], {"labels": ["x"]}), {})


if __name__ == "__main__":
    unittest.main()
