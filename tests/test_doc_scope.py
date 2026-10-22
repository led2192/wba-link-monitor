"""Offline tests for doc_scope.py: real file names and titles seen in the library.

Run:  python3 -m unittest tests/test_doc_scope.py -v
"""
import os, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import doc_scope as D  # noqa: E402

F, P, S = D.FULL, D.PART, D.SUMMARY
ACEA = 'CONSOLIDATED FINANCIAL STATEMENTS ACEA GROUP 2025 The Annual Financial Report has been translated into English Letter to Shareholders 4 1. REPORT ON OPERATIONS 7 Corporate bodies 9 Financial highlights 10 Summary of results 15 Main risks and uncertainties 63 2. SEPARATE FINANCIAL STATEMENTS 180 Income statement 188 Notes to the income statement 192 Table of contents SUSTAINABILITY REPORT 74 1. General information 76 1.2 Governance 76 2. Environmental information 99 2.2 Climate change 106 3. Social information 133 4. Governance information 153 5. Annex 157 Business outlook 177 3. CONSOLIDATED FINANCIAL STATEMENTS 256 Consolidated income statement 268 Annexes 353 4. REPORT ON CORPORATE GOVERNANCE AND OWNERSHIP STRUCTURE 385 Board of Directors 395 Tables 443 Letter to Shareholders Dear Shareholders'
UK_NAV = "Strategic report Governance Financial statements Contents Strategic report 2 Chair's statement 4 Chief Executive's review 8 Directors' report 120 Governance report 98 Remuneration report 130 Financial statements 160 Independent auditor's report 162 Consolidated income statement 172 Notes 180 Shareholder information 250"
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
    # a whole statutory annual report named after its financial statements: the contents page lists the
    # management report and the financial statements, and its page numbers reach the end of the file
    ("2025-consolidated-financial-statements.pdf", "CONSOLIDATED FINANCIAL STATEMENTS ACEA GROUP 2025", 452, ACEA, "Annual Report", F),
    # ...but a governance section of a split UK report lists the whole report's pages, far past its own 62
    ("centrica-ar2025-governance.pdf", "Centrica plc Annual Report and Accounts 2025", 62, UK_NAV, "Annual Report", P),
    # a standalone financial statements volume with no management report stays a part
    ("Investec-Annual-financial-statements-2024.pdf", "Investec annual report 2024", 196, "", "Annual Report", P),
    # Form 10-K file names
    ("MRNA_(Moderna_Inc.)_Annual_report_pursuant_to_Section_13_or_15(d).pdf", "Moderna, Inc. Form 10-K", 161, "", "10K Form", F),
    ("SEC_10-K_2010.pdf", "EQT CORPORATION FORM 10-K", 155, "", "10K Form", F),
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
