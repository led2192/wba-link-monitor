"""Offline tests for doc_scope.py: real file names and titles seen in the library.

Run:  python3 -m unittest tests/test_doc_scope.py -v
"""
import os, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import doc_scope as D  # noqa: E402

F, P, S = D.FULL, D.PART, D.SUMMARY
CASES = [
    # (file name, title, pages, cover, type, expected)
    ("SCSR25e_p108-162.pdf", "Sumitomo Chemical Sustainability Report 2025", 55, "", "Sustainability Report", P),
    ("DBS-AR17-92-96.pdf", "DBS Annual Report 2017", 5, "", "Annual Report", P),
    ("ir2025_100-111.pdf", "Bridgestone 3.0 Journey 2025", 12, "", "Integrated Report", P),
    ("honda-SR-2025-en-005.pdf", "Honda ESG Report 2025", 116, "", "ESG Report", P),
    ("ar2024_en_06_sp.pdf", "Fast Retailing", 13, "", "Integrated Report", P),
    ("ar2019e_17.pdf", "Hitachi", 8, "", "Annual Report", P),
    ("ar2025-managements-discussion-and-analysis-en.pdf", "TD BANK GROUP ANNUAL REPORT 2025", 127, "", "Annual Report", P),
    ("ceo-letter-to-shareholders-2018.pdf", "JPMorgan Chase 2018 Annual Report", 50, "", "Annual Report", P),
    ("all2.pdf", "2018 ANNUAL REPORT —FINANCIAL DATA SECTION—", 406, "", "Annual Report", P),
    ("2023_FS_Annual_Report_Highlights.pdf", "FS Italiane 2023", 40, "", "Annual Report", S),
    ("Sustainability-Report-2023-Summary.pdf", "Anima Holding Sustainability Report 2023", 16, "", "Sustainability Report", S),
    ("contents.pdf", "2019 Annual Report", 1, "", "Annual Report", P),
    # dates and fiscal spans at the end of a name are not page ranges
    ("VST-Vistra_Corp_ARS_2025-03-19.pdf", "Vistra Corp. 2024 Annual Report", 198, "", "Annual Report", F),
    ("Dalmia-Bharat-IR-FY-24-25.pdf", "Integrated Report", 215, "", "Integrated Report", F),
    ("lowes-23-ar-4-11-24.pdf", "2023 ANNUAL REPORT", 103, "", "Annual Report", F),
    # forms and legal wording that look like pieces but are whole reports
    ("RLC_SEC 17-A December 31 2024.pdf", "SEC FORM 17-A ANNUAL REPORT", 340, "", "Annual Report", F),
    ("New-Hope-Group-Appendix-4E-and-Annual-Report.pdf", "Annual Report 2025", 173, "", "Annual Report", F),
    ("bms-2022-10-K.pdf", "FORM 10-K ANNUAL REPORT PURSUANT TO SECTION 13 OR 15(d)", 447, "", "10K Form", F),
    ("Rio-Tinto-extractive-2019.pdf", "Extractive Sector Transparency Measures Act report", 30, "", "Sustainability Report", F),
    ("Mondi-integrated-report-and-financial-statements-2024.pdf", "Mondi Group Integrated report and financial statements 2024", 228, "", "Integrated Report", F),
    ("2024-Sustainability-and-Governance-Report.pdf", "2024 Sustainability and Governance Report", 50, "", "Sustainability Report", F),
    ("DE000A1ML7J1-JA-2013-EQ-E-01.pdf", "Annual Report 2013", 217, "", "Annual Report", F),
    ("25_E_00_1.pdf", "Integrated Report 2025", 128, "", "Integrated Report", F),
    ("2019-Verizon-Annual-Report.pdf", "Verizon 2019 Annual Report", 96, "", "Annual Report", F),
    # the cover starts on a printed page well past the start
    ("Business_Responsibility_and_Sustainability.pdf", "BRSR", 38, "104 Business Responsibility and Sustainability Report", "Sustainability Report", P),
]


class Scope(unittest.TestCase):
    def test_cases(self):
        for name, title, pages, cover, typ, want in CASES:
            with self.subTest(name):
                got, why = D.decide("https://example.com/files/" + name, "", title, pages, cover, typ)
                self.assertEqual(got, want, why)
                self.assertTrue(why)

    def test_short_floor_only_for_report_types(self):
        self.assertEqual(D.decide("https://x.com/a.pdf", "", "Policy", 3, "", "Code of Conduct")[0], F)


if __name__ == "__main__":
    unittest.main()
