"""Regression cases from the 22-23 October 2026 completeness drop (year rewrites)."""
import os, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fiscal_year as F  # noqa: E402
import date_documents as D  # noqa: E402


class FiscalYearReferences(unittest.TestCase):
    def test_quarterly_release_quoting_last_10k(self):
        pages = [(1, "Linde Reports First-Quarter 2024 Results. For the three months ended March 31, 2024, sales "
                     "were $8.1 billion. Risks are described in Linde plc's Form 10-K for the fiscal year ended "
                     "December 31, 2023 filed with the SEC")]
        self.assertEqual(F.decide(pages)[0], 2024)

    def test_annual_report_quoting_previous_10k(self):
        pages = [(1, "Southern Company 2024 Annual Report"),
                 (14, "Forward-looking statements are described in the Annual Report on Form 10-K for the year "
                      "ended December 31, 2023 (2023 Form 10-K). Net income for the year ended December 31, 2024 "
                      "was $4.4 billion. For the year ended December 31, 2024 revenue increased.")]
        self.assertEqual(F.decide(pages)[0], 2024)

    def test_preceding_quarter(self):
        pages = [(1, "For the quarter ended 31 March 2024 the Group's profit rose. As compared with the pre ceding "
                     "quarter ended 31 December 2023 , the Group's pre-tax profit")]
        self.assertEqual(F.decide(pages)[0], 2024)

    def test_a_10k_still_reads_its_own_year(self):
        pages = [(1, "FORM 10-K ANNUAL REPORT PURSUANT TO SECTION 13 OR 15(d) OF THE SECURITIES EXCHANGE ACT OF "
                     "1934 For the fiscal year ended December 31, 2023. This Annual Report on Form 10-K for the year "
                     "ended December 31, 2023 contains")]
        self.assertEqual(F.decide(pages)[0], 2023)

    def test_title_two_years_away_drops_below_threshold(self):
        pages = [(1, "AGMS FOR THE 2022 FINANCIAL YEAR PT TELKOM INDONESIA Annual Report 2024 187 the Financial "
                     "Year 2022 ended December 31, 2022 which have been audited. financial year ending on December "
                     "31, 2022 as long as")]
        y, _, score, _, _ = F.decide(pages)
        self.assertLess(score, 60)        # the year formula ignores it and the title year stands

    def test_sustainability_report_one_year_after_still_counts(self):
        pages = [(1, "Sustainability Report 2024. This report covers the period 1 January 2023 to 31 December 2023.")]
        y, _, score, _, _ = F.decide(pages)
        self.assertEqual(y, 2023)
        self.assertGreaterEqual(score, 60)


class CoverRule(unittest.TestCase):
    def test_ten_year_table_is_not_a_title(self):
        cover = ("Ten Year Summary 2015 2016 2017 2018 2019 2020 2021 2022 2023 2024 CONSOLIDATED INCOME STATEMENT "
                 "HK$ million Revenue 176,094 ... 2024 Annual Report 259")
        self.assertEqual(D.decide("https://doc.example.com/ir/annual/2024/en/tysum.pdf", "", cover, 2026),
                         ("2024", "cover"))

    def test_single_title_year_still_used(self):
        self.assertEqual(D.decide("https://x.com/docs/remuneration.pdf", "", "2026 Remuneration policy", 2026),
                         ("2026", "cover"))


if __name__ == "__main__":
    unittest.main()
