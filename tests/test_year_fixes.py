"""Regression cases from the 22-23 October 2026 completeness drop (year rewrites)."""
import os, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fiscal_year as F  # noqa: E402
import date_documents as D  # noqa: E402


class FiscalYearReferences(unittest.TestCase):
    def test_sustainability_report_citing_its_10k_keeps_that_year(self):
        # measured: in US ESG reports the 10-K cited is the reporting year (McDonald's, PBF, Domino's)
        pages = [(1, "2022 ESG REPORT. Further information is in our Annual Report on Form 10-K for the year "
                     "ended December 31, 2021 (the 2021 Form 10-K).")]
        self.assertEqual(F.decide(pages)[0], 2021)

    def test_preceding_quarter(self):
        pages = [(1, "For the quarter ended 31 March 2024 the Group's profit rose. As compared with the pre ceding "
                     "quarter ended 31 December 2023 , the Group's pre-tax profit")]
        self.assertEqual(F.decide(pages)[0], 2024)

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
